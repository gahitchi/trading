"""Download broker daily London Gold history before starting the PAPER node."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nautilus_trader.adapters.interactive_brokers import (
    HistoricalInteractiveBrokersClient,
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersInstrumentProvider,
    InteractiveBrokersInstrumentProviderConfig,
    SymbologyMethod,
)

from lab.gold_paper import FX, GOLD
from lab.persistence import atomic_json
from lab.safety import check_paper_settings


async def download_gold_history(paper, destination: Path) -> int:
    """Read closed daily MID bars from IBKR; fail before trading on missing history."""
    check_paper_settings(paper.port, paper.account_id)
    provider_config = InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.SIMPLIFIED, load_ids={GOLD}
    )
    provider = InteractiveBrokersInstrumentProvider(provider_config)
    data_config = InteractiveBrokersDataClientConfig(
        host=paper.host,
        port=paper.port,
        client_id=paper.client_id + 2,
        instrument_provider=provider_config,
        use_regular_trading_hours=False,
    )
    client = HistoricalInteractiveBrokersClient(provider, data_config)
    instruments = await client.request_instruments(instrument_ids=[GOLD])
    if len(instruments) != 1:
        raise ValueError("IBKR did not resolve the London Gold contract")
    now = datetime.now(UTC)
    start = now - timedelta(days=350)
    bars = await client.request_bars(
        ["1-DAY-MID"],
        now,
        start,
        instrument_ids=[GOLD],
        use_rth=False,
        timeout=60,
    )
    complete = [b for b in bars if start.timestamp() <= b.ts_event / 1e9 <= now.timestamp() - 15 * 60]
    complete = list({b.ts_event: b for b in complete}.values())
    complete.sort(key=lambda b: b.ts_event)
    if len(complete) < 201:
        raise ValueError(f"IBKR returned only {len(complete)} complete gold daily bars; need 201")
    atomic_json(
        destination,
        {
            "fetched_at": now.isoformat(),
            "source": "IBKR-XAUUSD-CMDTY-SMART-MID",
            "bars": [
                {
                    "end": datetime.fromtimestamp(b.ts_event / 1e9, UTC).isoformat(),
                    "open": str(b.open),
                    "high": str(b.high),
                    "low": str(b.low),
                    "close": str(b.close),
                }
                for b in complete
            ],
        },
    )
    return len(complete)


async def download_gold_cycle(host: str, port: int, client_id: int, paper_account: str):
    """Fetch recent completed gold bars and fresh gold/FX quotes via IBKR."""
    check_paper_settings(port, paper_account)
    provider_config = InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.SIMPLIFIED, load_ids={GOLD, FX}
    )
    provider = InteractiveBrokersInstrumentProvider(provider_config)
    data_config = InteractiveBrokersDataClientConfig(
        host=host,
        port=port,
        client_id=client_id,
        instrument_provider=provider_config,
        use_regular_trading_hours=False,
    )
    client = HistoricalInteractiveBrokersClient(provider, data_config)
    instruments = await client.request_instruments(instrument_ids=[GOLD, FX])
    if len(instruments) != 2:
        raise ValueError("IBKR did not resolve both gold and EUR/USD")
    now = datetime.now(UTC)
    daily = await client.request_bars(
        ["1-DAY-MID"], now, now - timedelta(days=7), instrument_ids=[GOLD], use_rth=False, timeout=30
    )
    gold_quote = await client.request_bars(
        ["1-MINUTE-MID"], now, now - timedelta(minutes=40), instrument_ids=[GOLD], use_rth=False, timeout=30
    )
    fx_quote = await client.request_bars(
        ["1-MINUTE-MID"], now, now - timedelta(minutes=40), instrument_ids=[FX], use_rth=False, timeout=30
    )
    return daily, gold_quote, fx_quote
