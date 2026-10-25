"""Offline-only multi-market proof of concept: python -m lab.markets demo.

Does not download data or connect to an exchange. The built-in fixture is
synthetic and must never be represented as a validated profitable strategy.
"""

from __future__ import annotations

import argparse
import csv
import json
from contextlib import nullcontext
from dataclasses import replace
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


def native_demo(balance: Decimal = Decimal("1000")) -> dict:
    """Native Nautilus market-data smoke run; no strategies, orders, accounts or keys."""
    from lab.markets.nautilus_data import build_causal_tape
    from lab.markets.nautilus_instruments import LinearContractDefinition
    from lab.markets.nautilus_runtime import replay_native_observations

    instrument = replace(fixture_instrument(), symbol="BTC-USD-SWAP")
    spec = LinearContractDefinition(
        instrument=instrument,
        base_currency="BTC",
        settlement_currency="USD",
        maker_fee_bps=Decimal("2"),
        taker_fee_bps=Decimal("5"),
        definition_ns=0,
        observed_ns=0,
        metadata_source="offline_fixture",
    )
    source = Path(str(files("lab.markets").joinpath("fixture.csv")))
    observations = []
    for trade, price in read_trades(source):
        observations.append(MarkPrice(f"mark-{trade.trade_id}", trade.exchange_ns, trade.received_ns, price))
        observations.append(trade)
    tape = build_causal_tape(instrument, observations)
    result = replay_native_observations(spec, tape, collateral=balance)
    return {
        "mode": "offline_sim",
        "input_kind": "synthetic_fixture",
        "engine": "nautilus_backtest_native",
        "instrument": result.native_instrument_id,
        "trade_observations": result.trade_observations,
        "mark_observations": result.mark_observations,
        "first_received_ns": result.first_received_ns,
        "last_received_ns": result.last_received_ns,
        "maker_fee_rate": str(result.maker_fee_rate),
        "taker_fee_rate": str(result.taker_fee_rate),
        "native_data_engine_used": result.native_data_engine_used,
        "event_integrity_verified": result.event_integrity_verified,
        "clock_receipt_verified": result.clock_receipt_verified,
        "fees_charged_verified": result.fees_charged_verified,
        "strategies_qualified": result.strategies_qualified,
        "orders_routed": False,
        "broker_execution_enabled": result.broker_execution_enabled,
        "market_data_validated": False,
        "qualified_for_live_trading": False,
    }


def native_fill_parity_demo() -> dict:
    """Real Nautilus matching, synthetic quotes, and exact account parity; never a broker run."""
    from lab.markets.native_fill_parity import run_native_fill_parity, synthetic_quotes
    from lab.markets.nautilus_instruments import LinearContractDefinition

    instrument = replace(fixture_instrument(), symbol="BTC-USD-SWAP")
    spec = LinearContractDefinition(
        instrument=instrument,
        base_currency="BTC",
        settlement_currency="USD",
        maker_fee_bps=Decimal("2"),
        taker_fee_bps=Decimal("5"),
        definition_ns=0,
        observed_ns=0,
        metadata_source="offline_fixture",
    )
    result = run_native_fill_parity(spec, synthetic_quotes(spec))
    return {
        "mode": "offline_sim",
        "input_kind": result.source,
        "engine": "nautilus_backtest_native",
        "instrument": result.instrument,
        "fills": result.fills,
        "quantity": str(result.quantity),
        "entry_price": str(result.entry_price),
        "exit_price": str(result.exit_price),
        "gross_pnl": str(result.gross_pnl),
        "charged_fees": str(result.charged_fees),
        "net_pnl": str(result.net_pnl),
        "ending_balance": str(result.ending_balance),
        "expected_ending_balance": str(result.expected_ending_balance),
        "execution_cost_parity_verified": result.execution_cost_parity_verified,
        "broker_execution_enabled": False,
        "qualified_for_live_trading": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline multi-market reference lab, NO exchange connectivity")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo", help="run a synthetic local linear-contract scenario")
    d.add_argument("--csv", type=Path, default=None, help="CSV with timestamp,price,quantity,trade_id,mark_price")
    d.add_argument("--quantity", type=Decimal, default=Decimal("0.001"))
    d.add_argument("--side", choices=["buy", "sell"], default="sell")
    d.add_argument("--balance", type=Decimal, default=Decimal("1000"))
    d.add_argument("--journal", type=Path, help="optional durable SQLite journal; refuses overwriting old sessions")
    sub.add_parser(
        "native-fill-parity",
        help="verify two real native synthetic fills, charged fees and independent Decimal collateral parity",
    )
    nd = sub.add_parser("native-demo", help="read-only native Nautilus replay of a bundled synthetic fixture")
    nd.add_argument("--balance", type=Decimal, default=Decimal("1000"))
    recover = sub.add_parser("recover", help="verify and resume a durable OFFLINE_SIM journal; no broker orders")
    recover.add_argument("--journal", type=Path, required=True)
    recover.add_argument("--balance", type=Decimal, default=Decimal("1000"))
    c = sub.add_parser("candles", help="READ-ONLY OKX EEA public confirmed 1m candles")
    c.add_argument("--instrument", default="BTC-USDT")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("--store", type=Path, help="save verified local checksum-addressed Parquet partitions")
    h = sub.add_parser("history", help="READ-ONLY paginated EEA historical 1m candles")
    h.add_argument("--instrument", default="BTC-USDT")
    h.add_argument("--pages", type=int, default=5)
    h.add_argument("--limit", type=int, default=100)
    h.add_argument("--store", type=Path, help="save validated checksum-addressed daily Parquet partitions")
    o = sub.add_parser("observations", help="READ-ONLY EEA latest trades + independent swap mark (NOT a quote)")
    o.add_argument("--instrument", default="BTC-USDT-SWAP")
    o.add_argument("--limit", type=int, default=20)
    micro = sub.add_parser("microstructure", help="READ-ONLY EEA book + indicative funding, no trading")
    micro.add_argument("--instrument", default="BTC-USDT-SWAP")
    micro.add_argument("--depth", type=int, default=5)
    args = parser.parse_args()
    if args.command == "demo":
        sample = files("lab.markets").joinpath("fixture.csv")
        path = args.csv if args.csv is not None else Path(str(sample))
        with EventJournal(args.journal) if args.journal is not None else nullcontext() as journal:
            print(json.dumps(demo(path, args.quantity, Side(args.side), args.balance, journal=journal), indent=2))
    elif args.command == "native-fill-parity":
        print(json.dumps(native_fill_parity_demo(), indent=2))
    elif args.command == "native-demo":
        print(json.dumps(native_demo(args.balance), indent=2))
    elif args.command == "recover":
        with EventJournal(args.journal) as journal:
            sim = recover_offline(fixture_instrument(), journal, starting_balance=args.balance)
            result = sim.report()
            result["recovery_verified"] = True
            result["journal_events"] = len(journal.read_all())
            result["external_execution_enabled"] = False
            print(json.dumps(result, indent=2))
    elif args.command == "observations":
        from lab.markets.okx_observations import current_public_mark, recent_public_trades

        trade_events = recent_public_trades(args.instrument, limit=args.limit)
        mark = current_public_mark(args.instrument)
        print(
            json.dumps(
                {
                    "source": "OKX_EEA_PUBLIC_SNAPSHOT",
                    "instrument": args.instrument,
                    "execution_supported": False,
                    "quotes_available": False,
                    "funding_verified": False,
                    "account_capabilities_qualified": False,
                    "not_a_synchronized_order_book": True,
                    "mark": {
                        "exchange_ns": mark.exchange_ns,
                        "received_ns": mark.received_ns,
                        "price": str(mark.price),
                    },
                    "trades": [
                        {
                            "id": trade.trade_id,
                            "exchange_ns": trade.exchange_ns,
                            "received_ns": trade.received_ns,
                            "price": str(trade.price),
                            "size_native": str(trade.quantity),
                            "taker_side": str(trade.taker_side),
                            "venue_source": trade.venue_source,
                        }
                        for trade in trade_events
                    ],
                },
                indent=2,
            )
        )
    elif args.command == "microstructure":
        from lab.markets.preview import public_preview

        print(json.dumps(public_preview(args.instrument, depth=args.depth), indent=2))
    elif args.command == "history":
        from lab.markets.catalog import write_confirmed_candles
        from lab.markets.okx_public import historical_confirmed_candles

        observed = historical_confirmed_candles(args.instrument, pages=args.pages, limit=args.limit)
        manifests = write_confirmed_candles(args.store, observed) if args.store is not None and observed else ()
        gap_count = sum(
            (observed[i].start_ns - observed[i - 1].start_ns) // 60_000_000_000 - 1 for i in range(1, len(observed))
        )
        print(
            json.dumps(
                {
                    "instrument": args.instrument,
                    "source": "OKX_EEA_PUBLIC_HISTORICAL_CANDLES",
                    "count": len(observed),
                    "start_ns": observed[0].start_ns if observed else None,
                    "end_ns": observed[-1].end_ns if observed else None,
                    "missing_minutes_between_observations": gap_count,
                    "catalog_manifests": [str(path) for path in manifests],
                    "complete_history_claimed": False,
                    "execution_supported": False,
                    "fees_funding_quotes_available": False,
                },
                indent=2,
            )
        )
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
