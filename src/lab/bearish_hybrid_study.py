"""Prospectively registered, read-only bearish hybrid research entry point.

Never opens candidate archive bytes before confirming an immutable family
member and reserving the attempt. No live orders, downloader or edge claim.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from pathlib import Path

from lab.bearish_ensemble_replay import ReplayAssumptions
from lab.bearish_hybrid import MINUTE_NS, Costs, Risk, Settings, VirtualAccount
from lab.bearish_hybrid_dynamic import DynamicHybridReport, ProfitPolicy, replay_hybrid_dynamic
from lab.bearish_hybrid_history import load_hybrid_archive
from lab.bearish_hybrid_replay import DAY_NS
from lab.research_family import load_family
from lab.trials import trial_run

KIND = "bearish-hybrid-dynamic-study"
VERSION = "hybrid-registered-research-v1"


def _digest(value: str | None, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True)
class StudySpec:
    symbol: str
    start_ns: int  # UTC midnight before first completed minute-end bar
    end_ns: int  # UTC midnight ending last completed bar, inclusive
    boundary_ns: int  # older, separately frozen protected boundary; not extended here
    archive_sha256: str  # independently pinned; does not authenticate the exchange
    funding_verified: bool = False
    external_attestation_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.symbol, str)
            or not 5 <= len(self.symbol) <= 20
            or not self.symbol.isascii()
            or not self.symbol.isupper()
            or not self.symbol.isalnum()
            or not self.symbol.endswith("USDT")
        ):
            raise ValueError("study requires an uppercase USDT perpetual symbol")
        # Prior BTC history was inspected by multiple retired bearish trials.
        # Do not relabel it a new independent out-of-sample research family.
        if self.symbol == "BTCUSDT":
            raise ValueError("BTCUSDT is excluded: protected/previously inspected BTC evidence")
        if (
            any(type(n) is not int for n in (self.start_ns, self.end_ns, self.boundary_ns))
            or self.start_ns <= 0
            or self.start_ns % DAY_NS
            or self.end_ns % DAY_NS
            or self.boundary_ns % DAY_NS
            or not self.start_ns < self.end_ns < self.boundary_ns
            or self.end_ns - self.start_ns > 69 * DAY_NS
        ):
            raise ValueError("study needs a finite, whole-day pre-holdout research window (max 69 days)")
        _digest(self.archive_sha256, "archive digest")
        if type(self.funding_verified) is not bool:
            raise ValueError("funding_verified must be boolean")
        if self.external_attestation_sha256 is not None:
            _digest(self.external_attestation_sha256, "external evidence digest")
        if self.funding_verified and self.external_attestation_sha256 is None:
            raise ValueError("positive funding verification needs separately pinned external evidence")


def planned_study_member(
    spec: StudySpec,
    *,
    costs: Costs,
    account: VirtualAccount,
    policy: ProfitPolicy,
    settings: Settings | None = None,
    risk: Risk | None = None,
    assumptions: ReplayAssumptions | None = None,
) -> dict:
    """Exact member to freeze via research_family.register_family before data access.

    Every risk, cost and reinvestment choice is part of the full family key;
    no candidate outcome is read here. The caller explicitly registers a
    whole precommitted experiment family, not an implicit per-run plan.
    """
    if not isinstance(spec, StudySpec) or not isinstance(policy, ProfitPolicy):
        raise ValueError("explicit research specification and profit policy required")
    if not isinstance(costs, Costs) or not isinstance(account, VirtualAccount):
        raise ValueError("explicit cost and virtual account assumptions required")
    if settings is not None and not isinstance(settings, Settings):
        raise ValueError("invalid hybrid settings")
    if risk is not None and not isinstance(risk, Risk):
        raise ValueError("invalid risk settings")
    if assumptions is not None and not isinstance(assumptions, ReplayAssumptions):
        raise ValueError("invalid replay assumptions")
    account.check()
    if account.equity <= 0 or not isfinite(costs.round_trip_bps):
        raise ValueError("positive virtual capital and a finite explicit cost schedule required")
    return {
        "kind": KIND,
        "instrument": spec.symbol,
        "window": [spec.start_ns, spec.end_ns],
        "params": {
            "protocol": VERSION,
            "spec": asdict(spec),
            "costs": asdict(costs),
            "virtual_account": asdict(account),
            "profit_policy": asdict(policy),
            "signal_settings": asdict(settings or Settings()),
            "risk": asdict(risk or Risk()),
            "replay_assumptions": asdict(assumptions or ReplayAssumptions()),
            "actual_capital": 0.0,
        },
    }


def run_registered_study(
    results_dir: Path,
    archive_path: Path,
    spec: StudySpec,
    *,
    costs: Costs,
    account: VirtualAccount,
    policy: ProfitPolicy,
    settings: Settings | None = None,
    risk: Risk | None = None,
    assumptions: ReplayAssumptions | None = None,
) -> DynamicHybridReport:
    """Reserve an identified attempt BEFORE reading the external archive.

    Failed/missing/tampered archives still leave counted attempts. Neither
    SHA-256 nor a caller attestation independently verifies exchange timing.
    This wrapper does not download data or authorize its use as a holdout.
    """
    member = planned_study_member(
        spec, costs=costs, account=account, policy=policy, settings=settings, risk=risk, assumptions=assumptions
    )
    family = load_family(results_dir)
    if family is None or member not in family["plan"]["members"]:
        raise ValueError("study must exactly match a frozen, prospective research family member")
    with trial_run(
        results_dir, KIND, spec.symbol, member["params"], tuple(member["window"]), holdout=False
    ) as attempt:
        # trial_run is authoritative and reserves before yielding to this block.
        raw = Path(archive_path).read_bytes()
        attempt.inputs.blob("hybrid-source-archive", raw)
        observations = load_hybrid_archive(
            raw,
            expected_sha256=spec.archive_sha256,
            boundary_ns=spec.boundary_ns,
            funding_verified=spec.funding_verified,
            expected_symbol=spec.symbol,
        )
        if (
            not observations
            or observations[0].bar.end_ns != spec.start_ns + MINUTE_NS
            or observations[-1].bar.end_ns != spec.end_ns
        ):
            raise ValueError("archive does not cover exactly the registered complete-day research window")
        report = replay_hybrid_dynamic(
            observations,
            boundary_ns=spec.boundary_ns,
            costs=costs,
            account=account,
            profit_policy=policy,
            settings=settings,
            risk=risk,
            assumptions=assumptions,
        )
        attempt.result = {
            "status": "research_only",
            "qualified": False,
            "archive_sha256": spec.archive_sha256,
            "archive_origin_authenticated": False,
            "completed_trades": report.completed_trades,
            "candidate_signals": report.candidate_signals,
            "positive_days": report.positive_days,
            "negative_days": report.negative_days,
            "flat_days": report.flat_days,
            "total_virtual_equity": report.ending_total_virtual_equity,
            "initial_virtual_equity": report.initial_virtual_equity,
            "funded_with_real_cash": False,
        }
        return report
