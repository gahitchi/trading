"""
Paper trading of the funding-carry strategy (lab.carry_study) on live public data: simulated fills, no keys, no
orders.

Every rebalance hour (00, 08, 16 UTC by default) a step:

1. updates the hourly history of every pair in the universe (lab.carry_data), so the funding settled at that hour
   is in it;
2. adds each open position's funding settlements since it was last counted (quantity x rate x the perpetual's
   close at that hour, a proxy for the mark price);
3. decides the positions with the exact rules and settings that passed the holdout: lab.carry_study.top_positions
   over the whole history, read at the latest rebalance hour. The decision is a function of the data, so a restart
   recomputes it instead of trusting state;
4. trades the difference at the live best bid and ask (Binance spot and USD-M book tickers): an entry buys spot at
   the ask and sells the same quantity of perpetual at the bid, an exit does the reverse, each paying the taker fee.
   The touch is assumed deep enough for the order: book depth isn't modelled;
5. marks open positions at the mids and writes the state and a ledger.

The kill switch (`lab kill`) applies: while it is set no position opens; with `--flatten` every position closes at
the next step. Each slot holds capital / top / (1 + 1/leverage) of notional per leg: the spot leg is paid in full and
the short perpetual is margined at `leverage`.

State, under <results>/carry/:
  paper.json           positions, realized P&L, equity, the last step
  paper-ledger.jsonl   every fill, funding settlement and step
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from lab import carry_data
from lab import carry_study as cs
from lab.persistence import append_json, atomic_json, file_lock, nullable_numbers, read_json
from lab.safety import read_kill_switch
from lab.survey import BINANCE_FAPI, get_json

STATE = "paper.json"
LEDGER = "paper-ledger.jsonl"
SETTLE_DELAY_S = 60  # after the rebalance hour, so its funding settlement and kline have been published


@dataclass(frozen=True)
class PaperSettings:
    capital: float = 10_000.0
    leverage: float = 3.0
    # the configuration tested on the holdout
    carry: cs.CarrySettings = field(
        default_factory=lambda: cs.CarrySettings(top=10, min_turnover_m=20.0, slippage_bps=5.0)
    )

    def __post_init__(self):
        if not self.carry.top or self.carry.mode != "cash":
            raise ValueError("paper trading runs the cash trade with --top slots")
        if self.capital <= 0 or self.leverage <= 0:
            raise ValueError("capital and leverage must be positive")

    @property
    def notional(self) -> float:
        """Per leg and slot, so that spot plus perpetual margin of every slot add up to the capital."""
        return self.capital / self.carry.top / (1 + 1 / self.leverage)


def _iso(ts: pd.Timestamp) -> str:
    return ts.isoformat()


def book_tickers(get=get_json) -> tuple[dict, dict]:
    """({symbol: (bid, ask)} on spot, the same on the USD-M perpetuals), two-sided quotes only."""
    get = carry_data.patient(get)

    def parse(rows):
        out = {}
        for r in rows:
            bid, ask = float(r["bidPrice"] or 0), float(r["askPrice"] or 0)
            if 0 < bid < ask:
                out[r["symbol"]] = (bid, ask)
        return out

    return parse(get(f"{carry_data.BINANCE_SPOT}/ticker/bookTicker")), parse(get(f"{BINANCE_FAPI}/ticker/bookTicker"))


def fetch(root: Path, symbols: list[str], now: pd.Timestamp, log=print) -> None:
    """Brings the cached history of every symbol up to now; a pair that fails is reported and left as it is."""
    from urllib.error import HTTPError

    for sym in symbols:
        for (venue, market), kinds in carry_data.SERIES.items():
            if venue != "binance":
                continue
            for kind in kinds:
                try:
                    carry_data.update(root, venue, market, kind, sym, pd.Timestamp("2023-01-01", tz="UTC"), now)
                except (HTTPError, ValueError) as e:
                    log(f"{sym} {market} {kind}: not updated ({e})")


def targets(root: Path, symbols: list[str], s: cs.CarrySettings, now: pd.Timestamp) -> tuple[set[str], pd.Timestamp]:
    """The symbols the strategy holds after its latest rebalance hour at or before `now`, and that hour."""
    hs = {}
    for sym in symbols:
        lg = cs.legs(root, s.mode, sym)
        if lg is not None:
            lg = cs.truncate(lg, now + pd.Timedelta(microseconds=1))  # what is known at `now`
        h = cs.hourly(lg) if lg is not None and len(lg.long) and len(lg.short) else None
        if h is not None and len(h):
            hs[sym] = h
    if not hs:
        return set(), now
    ws = cs.top_positions(hs, s)
    decided = max(w.index[-1] for w in ws.values())
    decided = decided - pd.Timedelta(hours=decided.hour % s.rebalance_h)
    held = {sym for sym, w in ws.items() if decided in w.index and w[decided] > 0}
    return held, decided


def _new_state(p: PaperSettings) -> dict:
    return {"version": 1, "capital": p.capital, "realized": 0.0, "positions": {}, "steps": 0}


def load_state(results: Path, p: PaperSettings) -> dict:
    path = Path(results) / STATE
    return read_json(path) if path.exists() else _new_state(p)


def _accrue_funding(root: Path, pos: dict, sym: str, now: pd.Timestamp) -> list[dict]:
    funding = carry_data.load(root, "binance", "perp", "funding", sym)
    perp = carry_data.load(root, "binance", "perp", "close", sym)
    since = pd.Timestamp(pos["funded_to"])
    events = []
    for ts, rate in funding[(funding.index > since) & (funding.index <= now)].items():
        mark = perp.asof(ts) if len(perp) and perp.index[0] <= ts else pos["perp_entry"]
        amount = pos["qty"] * rate * float(mark)  # shorts receive positive funding
        pos["funding"] += amount
        pos["funded_to"] = _iso(ts)
        events.append({"event": "funding", "symbol": sym, "at": _iso(ts), "rate": rate, "amount": amount})
    return events


def _close_pnl(pos: dict, spot_bid: float, perp_ask: float, s: cs.CarrySettings) -> tuple[float, float]:
    """(P&L of the position if closed at these prices, the exit fees)."""
    q = pos["qty"]
    fees = q * spot_bid * s.spot_fee_bps / 1e4 + q * perp_ask * s.perp_fee_bps / 1e4
    pnl = q * (spot_bid - pos["spot_entry"]) - q * (perp_ask - pos["perp_entry"]) + pos["funding"] - pos["fees"] - fees
    return pnl, fees


def step(
    root: Path, results: Path, p: PaperSettings, now: pd.Timestamp, *, kill_path: Path, quotes=None, refresh=True,
    log=print,
) -> dict:  # fmt: skip
    """One rebalance: fetch, accrue funding, decide, trade the difference, mark. Returns the new state."""
    s = p.carry
    results = Path(results) / "carry"
    with file_lock(results / (STATE + ".lock")):
        state = load_state(results, p)
        symbols = carry_data.universe() if refresh else carry_data.cached_symbols(root)
        if refresh:
            fetch(root, symbols, now, log)
        ledger = []
        for sym, pos in state["positions"].items():
            ledger += _accrue_funding(root, pos, sym, now)

        want, decided = targets(root, symbols, s, now)
        kill = read_kill_switch(kill_path)
        held = set(state["positions"])
        if kill:
            want = set() if kill["mode"] == "flatten" else held & want
        spot_q, perp_q = quotes if quotes is not None else book_tickers()

        for sym in sorted(held - want):
            if sym not in spot_q or sym not in perp_q:
                log(f"{sym}: no live quote, exit postponed")
                continue
            pos = state["positions"].pop(sym)
            pnl, fees = _close_pnl(pos, spot_q[sym][0], perp_q[sym][1], s)
            state["realized"] += pnl
            ledger.append(
                {"event": "exit", "symbol": sym, "qty": pos["qty"], "spot": spot_q[sym][0], "perp": perp_q[sym][1],
                 "fees": fees, "funding": pos["funding"], "pnl": pnl}
            )  # fmt: skip
        for sym in sorted(want - held):
            if sym not in spot_q or sym not in perp_q:
                log(f"{sym}: no live quote, entry skipped")
                continue
            spot_ask, perp_bid = spot_q[sym][1], perp_q[sym][0]
            qty = p.notional / spot_ask
            fees = qty * spot_ask * s.spot_fee_bps / 1e4 + qty * perp_bid * s.perp_fee_bps / 1e4
            state["positions"][sym] = {
                "qty": qty, "spot_entry": spot_ask, "perp_entry": perp_bid, "opened": _iso(now),
                "funded_to": _iso(now), "funding": 0.0, "fees": fees,
            }  # fmt: skip
            ledger.append(
                {"event": "entry", "symbol": sym, "qty": qty, "spot": spot_ask, "perp": perp_bid, "fees": fees}
            )

        unrealized = 0.0
        for sym, pos in state["positions"].items():
            if sym in spot_q and sym in perp_q:
                mid = (sum(spot_q[sym]) / 2, sum(perp_q[sym]) / 2)
                pos["unrealized"] = _close_pnl(pos, mid[0], mid[1], s)[0]
            unrealized += pos.get("unrealized", 0.0)
        state.update(
            steps=state["steps"] + 1, at=_iso(now), decided=_iso(decided), kill=kill,
            equity=p.capital + state["realized"] + unrealized, unrealized=unrealized,
        )  # fmt: skip
        state = nullable_numbers(state)
        for e in ledger:
            append_json(results / LEDGER, {"at": _iso(now), **nullable_numbers(e)})
        append_json(
            results / LEDGER,
            {"at": _iso(now), "event": "step", "decided": _iso(decided), "held": sorted(state["positions"]),
             "equity": state["equity"], "realized": state["realized"], "kill": kill},
        )  # fmt: skip
        atomic_json(results / STATE, state)
        return state


def next_run(now: pd.Timestamp, rebalance_h: int) -> pd.Timestamp:
    """The next rebalance hour after now, plus the settlement delay."""
    t = now.floor("h") + pd.Timedelta(hours=1)
    while t.hour % rebalance_h:
        t += pd.Timedelta(hours=1)
    return t + pd.Timedelta(seconds=SETTLE_DELAY_S)


def run(root: Path, results: Path, p: PaperSettings, kill_path: Path, *, once=False, log=print, sleep=time.sleep):
    while True:
        now = pd.Timestamp(datetime.now(UTC))
        st = step(root, results, p, now, kill_path=kill_path, log=log)
        log(
            f"{now:%Y-%m-%d %H:%M} UTC: {len(st['positions'])} position(s) "
            f"({' '.join(sorted(st['positions'])) or 'none'}), equity {st['equity']:,.2f}, realized {st['realized']:+,.2f}"
            + (f", KILL SWITCH {st['kill']['mode']}" if st["kill"] else "")
        )
        if once:
            return st
        wake = next_run(pd.Timestamp(datetime.now(UTC)), p.carry.rebalance_h)
        sleep(max(0.0, (wake - pd.Timestamp(datetime.now(UTC))).total_seconds()))
