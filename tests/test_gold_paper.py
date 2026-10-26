"""Gold position ownership and protection checks before emergency orders."""

from types import SimpleNamespace

from lab.gold_paper import GOLD, GoldPaperStrategy
from lab.halt import read_halt


class FakeGoldRuntime:
    def __init__(self, path, opening_id):
        self.config = SimpleNamespace(runtime=path)
        self.known_order_ids = {"ours"}
        self.emergency_exit = False
        self.portfolio = SimpleNamespace(net_position=lambda _instrument: 1)
        self.cache = SimpleNamespace(
            orders_open=lambda **_kwargs: [],
            positions_open=lambda **_kwargs: [SimpleNamespace(opening_order_id=opening_id)],
        )
        self.clock = SimpleNamespace(timestamp_ns=lambda: 1_000_000_000)
        self.status = None
        self.closed = []

    def _schedule_monitor(self, _seconds):
        pass

    def _paper_account_ok(self):
        return True

    def _status(self, state, **_details):
        self.status = state

    def close_all_positions(self, instrument):
        self.closed.append(instrument)


def test_foreign_gold_position_is_never_closed_by_the_lab(tmp_path):
    fake = FakeGoldRuntime(tmp_path, "other-client")
    GoldPaperStrategy._monitor(fake, None)
    assert fake.closed == []
    assert "foreign" in fake.status
    assert read_halt(tmp_path / "HALT")["kind"] == "reconciliation_mismatch"


def test_owned_gold_without_stop_or_target_is_closed(tmp_path):
    fake = FakeGoldRuntime(tmp_path, "ours")
    GoldPaperStrategy._monitor(fake, None)
    assert fake.closed == [GOLD]
    assert "closing" in fake.status
    assert read_halt(tmp_path / "HALT")["kind"] == "reconciliation_mismatch"
