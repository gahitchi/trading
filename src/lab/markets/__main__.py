"""Offline-only multi-market proof of concept: python -m lab.markets demo.

Does not download data or connect to an exchange. The built-in fixture is
synthetic and must never be represented as a validated profitable strategy.
"""

from __future__ import annotations

import argparse
import csv
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

from lab.markets.domain import AssetClass, ContractKind, Instrument, MarkPrice, OrderIntent, Side, Trade
from lab.markets.journal import EventJournal
from lab.markets.minute import MinuteBuilder
from lab.markets.recovery import recover_offline
from lab.markets.simulator import OfflineSimulator


def timestamp_ns(raw: str) -> int:
    stamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamps must include timezone, preferably Z")
    utc = stamp.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    ns = ((delta.days * 86_400 + delta.seconds) * 1_000_000_000) + delta.microseconds * 1000
    if ns < 0:
        raise ValueError("market replay timestamps must be nonnegative Unix nanoseconds")
    return ns


def read_trades(path: Path) -> list[tuple[Trade, Decimal]]:
    trades = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        expected = {"timestamp", "price", "quantity", "trade_id", "mark_price"}
        if not reader.fieldnames or len(reader.fieldnames) != len(expected) or set(reader.fieldnames) != expected:
            raise ValueError("CSV fields must be timestamp,price,quantity,trade_id,mark_price")
        for row in reader:
            stamp = timestamp_ns(row["timestamp"])
            trades.append(
                (
                    Trade(
                        trade_id=row["trade_id"],
                        exchange_ns=stamp,
                        received_ns=stamp,
                        price=Decimal(row["price"]),
                        quantity=Decimal(row["quantity"]),
                    ),
                    Decimal(row["mark_price"]),
                )
            )
    if len(trades) < 3:
        raise ValueError("at least three observed trades are required for enter/exit demonstration")
    return trades


def fixture_instrument() -> Instrument:
    return Instrument(
        venue="SIM",
        symbol="BTC-USD-LINEAR",
        asset_class=AssetClass.CRYPTO,
        kind=ContractKind.LINEAR_DERIVATIVE,
        quote_currency="USD",
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        multiplier=Decimal("1"),
    )


def demo(
    path: Path,
    quantity: Decimal,
    side: Side,
    starting_balance: Decimal,
    *,
    journal: EventJournal | None = None,
) -> dict:
    instrument = fixture_instrument()
    sim = OfflineSimulator(instrument, starting_balance=starting_balance, journal=journal)
    bars = MinuteBuilder()
    trades = read_trades(path)
    completed = []
    for i, (trade, mark) in enumerate(trades):
        completed.extend(bars.push(trade))
        sim.on_mark(MarkPrice(f"mark-{trade.trade_id}", trade.exchange_ns, trade.received_ns, mark))
        sim.on_trade(trade)
        if i == 0:
            sim.submit(OrderIntent("entry1", instrument.instrument_id, side, quantity, trade.exchange_ns))
        if i == len(trades) - 2 and sim.position:
            closing = Side.BUY if sim.position < 0 else Side.SELL
            sim.submit(
                OrderIntent(
                    "exit1", instrument.instrument_id, closing, abs(sim.position), trade.exchange_ns, reduce_only=True
                )
            )
    completed.extend(bars.flush(trades[-1][0].received_ns + 60_000_000_000))
    result = sim.report()
    result.update(
        {
            "input": str(path),
            "input_kind": "synthetic_fixture" if path.name == "fixture.csv" else "operator_supplied_csv",
            "market_data_validated": False,
            "minute_bars": len(completed),
            "missing_minutes": sum(x.missing_minutes_before for x in completed),
            "trades_received": len(trades),
            "mark_feed": "explicit CSV mark_price (synthetic/unverified)",
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline multi-market reference lab, NO exchange connectivity")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo", help="run a synthetic local linear-contract scenario")
    d.add_argument("--csv", type=Path, default=None, help="CSV with timestamp,price,quantity,trade_id,mark_price")
    d.add_argument("--quantity", type=Decimal, default=Decimal("0.001"))
    d.add_argument("--side", choices=["buy", "sell"], default="sell")
    d.add_argument("--balance", type=Decimal, default=Decimal("1000"))
    d.add_argument("--journal", type=Path, help="optional durable SQLite journal; refuses overwriting old sessions")
    recover = sub.add_parser("recover", help="verify and resume a durable OFFLINE_SIM journal; no broker orders")
    recover.add_argument("--journal", type=Path, required=True)
    recover.add_argument("--balance", type=Decimal, default=Decimal("1000"))
    c = sub.add_parser("candles", help="READ-ONLY OKX EEA public confirmed 1m candles")
    c.add_argument("--instrument", default="BTC-USDT")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("--store", type=Path, help="save verified local checksum-addressed Parquet partitions")
    args = parser.parse_args()
    if args.command == "demo":
        sample = files("lab.markets").joinpath("fixture.csv")
        path = args.csv if args.csv is not None else Path(str(sample))
        with EventJournal(args.journal) if args.journal is not None else nullcontext() as journal:
            print(json.dumps(demo(path, args.quantity, Side(args.side), args.balance, journal=journal), indent=2))
    elif args.command == "recover":
        with EventJournal(args.journal) as journal:
            sim = recover_offline(fixture_instrument(), journal, starting_balance=args.balance)
            result = sim.report()
            result["recovery_verified"] = True
            result["journal_events"] = len(journal.read_all())
            result["external_execution_enabled"] = False
            print(json.dumps(result, indent=2))
    elif args.command == "candles":
        from lab.markets.okx_public import latest_confirmed_candles

        observed = latest_confirmed_candles(args.instrument, limit=args.limit)
        manifests = ()
        if args.store is not None and observed:
            from lab.markets.catalog import write_confirmed_candles

            manifests = write_confirmed_candles(args.store, observed)
        print(
            json.dumps(
                {
                    "source": "OKX_EEA_PUBLIC_CANDLES",
                    "instrument": args.instrument,
                    "count": len(observed),
                    "unconfirmed_excluded": True,
                    "execution_supported": False,
                    "catalog_manifests": [str(path) for path in manifests],
                    "catalog_qualified_for_execution": False,
                    "candles": [
                        {
                            "start_ns": bar.start_ns,
                            "open": str(bar.open),
                            "high": str(bar.high),
                            "low": str(bar.low),
                            "close": str(bar.close),
                            "volume_native": str(bar.volume_native),
                        }
                        for bar in observed
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
