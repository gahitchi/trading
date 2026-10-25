"""Shared, nontrading public-market preview for terminal and local dashboard.

Every observation is separately timestamped. This is not a reconciled venue
feed, research approval, tradable quote, execution algorithm, or margin source.
"""

from __future__ import annotations

from decimal import Decimal

from lab.markets import okx_microstructure as api


def public_preview(instrument_id: str, *, depth: int = 5) -> dict:
    book = api.current_public_book(instrument_id, depth=depth)
    funding = api.current_public_funding(instrument_id)
    spread_bps = None
    if book.spread is not None:
        midpoint = (book.best_bid + book.best_ask) / 2
        spread_bps = str(book.spread / midpoint * Decimal("10000"))
    return {
        "instrument": instrument_id,
        "source": "OKX_EEA_PUBLIC_REST_SNAPSHOTS",
        "mode": "read_only_public",
        "book": {
            "exchange_ns": book.exchange_ns,
            "received_ns": book.received_ns,
            "seq_id": book.sequence_id,
            "best_bid": str(book.best_bid),
            "best_ask": str(book.best_ask),
            "spread": str(book.spread) if book.spread is not None else None,
            "spread_bps": spread_bps,
            "crossed_or_locked": book.crossed_or_locked,
            "bids": [
                {"price": str(level.price), "native_size": str(level.size_native), "orders": level.order_count}
                for level in book.bids
            ],
            "asks": [
                {"price": str(level.price), "native_size": str(level.size_native), "orders": level.order_count}
                for level in book.asks
            ],
        },
        "funding": {
            "exchange_ns": funding.exchange_ns,
            "received_ns": funding.received_ns,
            "indicated_rate": str(funding.indicated_rate),
            "funding_time_ns": funding.funding_time_ns,
            "next_funding_time_ns": funding.next_funding_time_ns,
            "realized_payment_qualified": False,
        },
        "feeds_synchronized": False,
        "complete_depth_or_replay": False,
        "snapshot_tradable": False,
        "authenticated_account_qualified": False,
        "orders_routed": False,
        "live_trading_enabled": False,
    }
