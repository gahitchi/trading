"""Offline-only multi-market proof of concept: python -m lab.markets demo.

Does not download data or connect to an exchange. The built-in fixture is
synthetic and must never be represented as a validated profitable strategy.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

from lab.markets.domain import AssetClass, ContractKind, Instrument, OrderIntent, Side, Trade
from lab.markets.minute import MinuteBuilder
from lab.markets.simulator import OfflineSimulator


def timestamp_ns(raw: str) -> int:
    stamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamps must include timezone, preferably Z")
    utc = stamp.astimezone(UTC)
    epoch_s = int(utc.timestamp())
    return epoch_s * 1_000_000_000 + utc.microsecond * 1000


def read_trades(path: Path) -> list[Trade]:
    trades = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or set(reader.fieldnames) != {"timestamp", "price", "quantity", "trade_id"}:
            raise ValueError("CSV fields must be timestamp,price,quantity,trade_id")
        for row in reader:
            stamp = timestamp_ns(row["timestamp"])
            trades.append(
                Trade(
                    trade_id=row["trade_id"],
                    exchange_ns=stamp,
                    received_ns=stamp,
                    price=Decimal(row["price"]),
                    quantity=Decimal(row["quantity"]),
                )
            )
    if len(trades) < 3:
        raise ValueError("at least three observed trades are required for enter/exit demonstration")
    return trades


def demo(path: Path, quantity: Decimal, side: Side, starting_balance: Decimal) -> dict:
    instrument = Instrument(
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
    sim = OfflineSimulator(instrument, starting_balance=starting_balance)
    bars = MinuteBuilder()
    trades = read_trades(path)
    completed = []
    for i, trade in enumerate(trades):
        completed.extend(bars.push(trade))
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
    completed.extend(bars.flush(trades[-1].received_ns + 60_000_000_000))
    result = sim.report()
    result.update(
        {
            "input": str(path),
            "input_kind": "synthetic_fixture" if path.name == "fixture.csv" else "operator_supplied_csv",
            "market_data_validated": False,
            "minute_bars": len(completed),
            "missing_minutes": sum(x.missing_minutes_before for x in completed),
            "trades_received": len(trades),
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline multi-market reference lab, NO exchange connectivity")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo", help="run a synthetic local linear-contract scenario")
    d.add_argument("--csv", type=Path, default=None, help="CSV with timestamp,price,quantity,trade_id")
    d.add_argument("--quantity", type=Decimal, default=Decimal("0.001"))
    d.add_argument("--side", choices=["buy", "sell"], default="sell")
    d.add_argument("--balance", type=Decimal, default=Decimal("1000"))
    c = sub.add_parser("candles", help="READ-ONLY OKX EEA public confirmed 1m candles")
    c.add_argument("--instrument", default="BTC-USDT")
    c.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.command == "demo":
        sample = files("lab.markets").joinpath("fixture.csv")
        path = args.csv if args.csv is not None else Path(str(sample))
        print(json.dumps(demo(path, args.quantity, Side(args.side), args.balance), indent=2))
    elif args.command == "candles":
        from lab.markets.okx_public import latest_confirmed_candles

        observed = latest_confirmed_candles(args.instrument, limit=args.limit)
        print(
            json.dumps(
                {
                    "source": "OKX_EEA_PUBLIC_CANDLES",
                    "instrument": args.instrument,
                    "count": len(observed),
                    "unconfirmed_excluded": True,
                    "execution_supported": False,
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
