"""Gold position ownership and protection checks before emergency orders."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MethodType, SimpleNamespace

from lab.authority import Authority
from lab.gold import GoldBar
from lab.gold_paper import GOLD, GoldPaperStrategy
from lab.halt import read_halt


class FakeGoldRuntime:
    def __init__(self, path, opening_id):
        self.config = SimpleNamespace(runtime=path)
        self.known_order_ids = {"ours"}
        self.emergency_exit = False
        self.emergency_order_id = None
        self.approval_pending = False
        self.broker_reconciled = False
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
        self.decisions = 0
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

    def _decide(self):
        self.decisions += 1


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


def test_monitor_retries_a_supervised_order_after_operator_approval(tmp_path):
    fake = FakeGoldRuntime(tmp_path, "ours")
    fake.portfolio = SimpleNamespace(net_position=lambda _instrument: 0)
    fake.cache = SimpleNamespace(
        orders_open=lambda **_kwargs: [],
        positions_open=lambda **_kwargs: [],
        order=lambda _order_id: None,
    )
    fake.approval_pending = True

    GoldPaperStrategy._monitor(fake, None)

    assert fake.decisions == 1
    assert fake.broker_reconciled is True


def test_gold_uses_mandate_issued_after_strategy_start(tmp_path):
    identity = {"tickers": ["XAUUSD"]}
    authority = Authority(tmp_path, identity)
    startup = authority.issue("supervised")
    authority.issue("autonomous")
    fake = SimpleNamespace(config=SimpleNamespace(authority=authority))

    assert authority.read()["nonce"] != startup["nonce"]
    assert GoldPaperStrategy._authorize_order(fake, {"ticker": "XAUUSD", "qty": 1})


def test_gold_consumes_a_supervised_approval(tmp_path):
    authority = Authority(tmp_path, {"tickers": ["XAUUSD"]})
    authority.issue("supervised")
    fake = SimpleNamespace(config=SimpleNamespace(authority=authority))
    order = {"ticker": "XAUUSD", "side": "BUY", "qty": 1}

    assert not GoldPaperStrategy._authorize_order(fake, order)
    request = authority.requests()[0]
    authority.approve(request["id"])
    assert GoldPaperStrategy._authorize_order(fake, order)


def _decision_runtime(tmp_path, authority):
    now = datetime.now(UTC)
    submitted = []
    statuses = []
    fake = SimpleNamespace(
        approval_pending=False,
        config=SimpleNamespace(
            runtime=tmp_path,
            authority=authority,
            notification_health=None,
            capital_usd=Decimal("1000000"),
            max_order_value=Decimal("50000"),
        ),
        bars=[
            GoldBar(
                date.today() - timedelta(days=1), Decimal("3900"), Decimal("4050"), Decimal("3850"), Decimal("4000")
            )
        ],
        gold_quote=Decimal("4000"),
        gold_quote_at=now,
        fx_quote=Decimal("1.10"),
        fx_quote_at=now,
        last_submitted=None,
        known_order_ids=set(),
        cache=SimpleNamespace(
            instrument=lambda _instrument: SimpleNamespace(
                min_quantity=1, size_increment=1, price_increment=Decimal("0.01")
            ),
            orders_open_count=lambda **_kwargs: 0,
        ),
        portfolio=SimpleNamespace(net_position=lambda _instrument: 0),
        order_factory=SimpleNamespace(bracket=lambda **_kwargs: [SimpleNamespace(client_order_id="entry-1")]),
        _paper_account_ok=lambda: True,
        _equity_usd=lambda: Decimal("1100000"),
        _persist_state=lambda: None,
        _status=lambda state, **details: statuses.append((state, details)),
        submit_order_list=lambda bracket: submitted.append(bracket),
    )
    fake._authorize_order = MethodType(GoldPaperStrategy._authorize_order, fake)
    return fake, submitted, statuses


def test_autonomous_gold_decision_submits_bracket(tmp_path, monkeypatch):
    authority = Authority(tmp_path, {"tickers": ["XAUUSD"]})
    authority.issue("autonomous")
    fake, submitted, statuses = _decision_runtime(tmp_path, authority)
    plan = SimpleNamespace(
        session=date.today() - timedelta(days=1),
        reference_usd=Decimal("4000"),
        allocated_usd=Decimal("10000"),
        atr_usd=Decimal("20"),
    )
    monkeypatch.setattr("lab.gold_paper.plan_gold_trade", lambda *_args, **_kwargs: plan)

    GoldPaperStrategy._decide(fake)

    assert len(submitted) == 1
    assert statuses[-1][0] == "gold bracket submitted"


def test_supervised_gold_decision_submits_after_approval(tmp_path, monkeypatch):
    authority = Authority(tmp_path, {"tickers": ["XAUUSD"]})
    authority.issue("supervised")
    fake, submitted, statuses = _decision_runtime(tmp_path, authority)
    plan = SimpleNamespace(
        session=date.today() - timedelta(days=1),
        reference_usd=Decimal("4000"),
        allocated_usd=Decimal("10000"),
        atr_usd=Decimal("20"),
    )
    monkeypatch.setattr("lab.gold_paper.plan_gold_trade", lambda *_args, **_kwargs: plan)

    GoldPaperStrategy._decide(fake)
    authority.approve(authority.requests()[0]["id"])
    GoldPaperStrategy._decide(fake)

    assert len(submitted) == 1
    assert statuses[-1][0] == "gold bracket submitted"
