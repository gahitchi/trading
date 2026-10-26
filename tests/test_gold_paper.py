"""Gold position ownership and protection checks before emergency orders."""

from types import SimpleNamespace

from lab.gold_paper import GOLD, GoldPaperStrategy
from lab.halt import read_halt


class FakeGoldRuntime:
    def __init__(self, path, opening_id):
        self.config = SimpleNamespace(runtime=path)
        self.known_order_ids = {"ours"}
        self.emergency_exit = False
        self.emergency_order_id = None
        self.portfolio = SimpleNamespace(net_position=lambda _instrument: 1)
        self.cache = SimpleNamespace(
            orders_open=lambda **_kwargs: [],
            positions_open=lambda **_kwargs: [SimpleNamespace(opening_order_id=opening_id)],
            order=lambda _order_id: None,
        )
        self.clock = SimpleNamespace(timestamp_ns=lambda: 1_000_000_000)
        self.status = None
        self.submitted = []
        self.persisted_ids = set()
        self.order_factory = SimpleNamespace(
            market=lambda instrument, _side, _quantity, **_kwargs: SimpleNamespace(
                instrument_id=instrument, client_order_id="exit-1"
            )
        )

    def _schedule_monitor(self, _seconds):
        pass

    def _paper_account_ok(self):
        return True

    def _status(self, state, **_details):
        self.status = state

    def _persist_state(self):
        self.persisted_ids = set(self.known_order_ids)

    def submit_order(self, order):
        assert str(order.client_order_id) in self.persisted_ids
        self.submitted.append(order.instrument_id)


def test_foreign_gold_position_is_never_closed_by_the_lab(tmp_path):
    fake = FakeGoldRuntime(tmp_path, "other-client")
    GoldPaperStrategy._monitor(fake, None)
    assert fake.submitted == []
    assert "foreign" in fake.status
    assert read_halt(tmp_path / "HALT")["kind"] == "reconciliation_mismatch"


def test_owned_gold_without_stop_or_target_is_closed(tmp_path):
    fake = FakeGoldRuntime(tmp_path, "ours")
    GoldPaperStrategy._monitor(fake, None)
    assert fake.submitted == [GOLD]
    assert "exit-1" in fake.persisted_ids
    assert "closing" in fake.status
    GoldPaperStrategy._monitor(fake, None)
    assert fake.submitted == [GOLD]
    assert "unresolved" in fake.status
    assert read_halt(tmp_path / "HALT")["kind"] == "reconciliation_mismatch"


def test_stale_buy_emergency_cover_is_not_treated_as_an_entry(tmp_path):
    """An aged BUY market cover for a short must remain working until reconciled."""
    from nautilus_trader.model import OrderSide, OrderType

    fake = FakeGoldRuntime(tmp_path, "ours")
    fake.portfolio.net_position = lambda _instrument: -1
    fake.emergency_exit = True
    fake.known_order_ids.add("cover")
    fake.clock.timestamp_ns = lambda: 3_601_000_000_000
    fake.cache.orders_open = lambda **_kwargs: [
        SimpleNamespace(
            client_order_id="cover",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            ts_init=1_000_000_000,
        )
    ]
    cancelled = []
    fake.cancel_order = lambda order_id: cancelled.append(str(order_id))

    GoldPaperStrategy._monitor(fake, None)

    assert cancelled == []
    assert "waiting for emergency gold exit" in fake.status


def test_stale_buy_limit_entry_is_still_cancelled(tmp_path):
    from nautilus_trader.model import OrderSide, OrderType

    fake = FakeGoldRuntime(tmp_path, "ours")
    fake.portfolio.net_position = lambda _instrument: 0
    fake.known_order_ids.add("entry")
    fake.clock.timestamp_ns = lambda: 3_601_000_000_000
    fake.cache.orders_open = lambda **_kwargs: [
        SimpleNamespace(
            client_order_id="entry",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            ts_init=1_000_000_000,
        )
    ]
    cancelled = []
    fake.cancel_order = lambda order_id: cancelled.append(str(order_id))

    GoldPaperStrategy._monitor(fake, None)

    assert cancelled == ["entry"]
