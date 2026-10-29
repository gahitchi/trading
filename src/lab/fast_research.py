"""
Research discipline for recorded market data (lab record): a forward holdout and a count of every trial.

The holdout is prospective. The first research run on an instrument reserves everything recorded after the
last event in the catalog at that moment; research (micro-study, fast backtest) only ever sees data before
that boundary, and the recorder keeps adding unseen data behind it. A frozen strategy configuration may be tested
on the holdout once per boundary (`lab fast backtest --holdout`); every reuse, including changed parameters or
an interrupted evaluation, needs a logged reason. Moving the
boundary later (`lab fast release`) turns holdout data into research data and needs a logged reason too. It
never moves earlier: data research has seen can't become a holdout again.

CLI evaluations are reserved by lab.trials before work, including failed and interrupted attempts; completed
and failed outcomes are mirrored into the trial log. A result can be read against how many configurations
were tried, without silently dropping evaluations that never published a result.

State, under the lab's results directory:
  fast_partition.json        the boundary per instrument, and its history
  fast_holdout_ledger.jsonl  every holdout run
  fast_holdout_attempts.json atomic reservations and outcomes, including interrupted attempts
  research_attempts/         authoritative CLI attempt lifecycle and code/input provenance
  fast_trials.jsonl          mirrored terminal outcomes and legacy completed runs
"""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from lab.persistence import append_json, atomic_json, file_lock, parse_json, read_json

PARTITION = "fast_partition.json"
LEDGER = "fast_holdout_ledger.jsonl"
HOLDOUT_STATE = "fast_holdout_attempts.json"
HOLDOUT_LOCK = "fast_holdout_evaluation.lock"
TRIALS = "fast_trials.jsonl"
DAY_NS = 86_400_000_000_000
RETURN_BASIS = "daily-net-equity-v1"


class HoldoutGuard(ValueError):
    """A run would let research see the holdout, or repeat a holdout test without a reason."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).isoformat(timespec="seconds")


def last_recorded(catalog_path: str, instrument_id: str) -> int | None:
    from nautilus_trader.model import NautilusDataType
    from nautilus_trader.persistence import ParquetDataCatalog

    return ParquetDataCatalog(catalog_path).query_last_timestamp(NautilusDataType.OrderBookDelta, instrument_id)


def _read_partition(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "instruments": {}}
    try:
        part = read_json(path)
        if part["version"] != 1 or not isinstance(part["instruments"], dict):
            raise ValueError("unknown partition version")
        return part
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise HoldoutGuard(f"unreadable {path}; restore it, the holdout depends on it: {e}") from e


def boundary(results_dir: Path, catalog_path: str, instrument_id: str) -> int:
    """The instrument's holdout start (ns, ts_init). Reserved on first use: everything after what is recorded now."""
    path = Path(results_dir) / PARTITION
    with file_lock(path.with_name(path.name + ".lock")):
        part = _read_partition(path)
        entry = part["instruments"].get(instrument_id)
        if entry is not None:
            return int(entry["holdout_start"])
        last = last_recorded(catalog_path, instrument_id)
        if last is None:
            raise ValueError(f"no recorded order-book data for {instrument_id} in {catalog_path}")
        start = last + 1
        part["instruments"][instrument_id] = {
            "holdout_start": start,
            "history": [{"at": _now(), "holdout_start": start, "reason": "first research run"}],
        }
        atomic_json(path, part)
        return start


def research_window(
    results_dir: Path, catalog_path: str, instrument_id: str, start: int | None, end: int | None
) -> tuple[int | None, int]:
    """(start, end) for a research run: the requested window, which must lie before the holdout."""
    b = boundary(results_dir, catalog_path, instrument_id)
    if (start is not None and start >= b) or (end is not None and end >= b):
        raise HoldoutGuard(
            f"{instrument_id} data from {_iso(b)} is the holdout; research may only use data before it "
            "(`lab fast backtest --holdout` tests a finished configuration once)"
        )
    return start, b - 1 if end is None else end


def fingerprint(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]


def equity_return_stats(daily_equity: dict, capital: float) -> dict:
    """Daily net equity returns, including unrealized P&L. Absent/invalid observations never become zero returns.

    Keys are UTC epoch days observed in the recording; days without recorded books are not manufactured.
    """
    import pandas as pd

    returns, previous, previous_day = {}, capital, None
    for day, equity in sorted(daily_equity.items()):
        date = datetime.fromtimestamp(day * 86_400, UTC).date().isoformat()
        valid = (
            equity is not None
            and previous is not None
            and math.isfinite(equity)
            and math.isfinite(previous)
            and previous > 0
            and (previous_day is None or day == previous_day + 1)
        )
        value = equity / previous - 1 if valid else None
        returns[date] = value if value is not None and math.isfinite(value) else None
        previous = equity if equity is not None and math.isfinite(equity) else None
        previous_day = day
    result = {
        "basis": RETURN_BASIS, "days": len(returns), "daily_returns": returns,
        "sr_daily": None, "sharpe": None, "skew": None, "kurt": None,
    }  # fmt: skip
    x = pd.Series(returns, dtype=float)
    if len(x) < 2 or x.isna().any():
        return result
    sd = float(x.std())
    if not math.isfinite(sd) or sd <= 0:
        return result
    sr = float(x.mean() / sd)
    if not math.isfinite(sr):
        return result
    result.update(sr_daily=sr, sharpe=sr * math.sqrt(365))
    if len(x) >= 4:
        skew, kurt = float(x.skew()), float(x.kurt()) + 3
        result.update(skew=skew if math.isfinite(skew) else None, kurt=kurt if math.isfinite(kurt) else None)
    return result


def fast_deflated_sharpe(
    results_dir: Path, instrument_id: str, window: tuple, current: dict
) -> tuple[float | None, str]:
    """IID diagnostic using every logged fast research configuration for this instrument, never the holdout.

    No selection of only the trials with favourable or available statistics: incompatible windows, old logs
    without daily equity, and too little data make the diagnostic unavailable. Correlation between trials and
    serial dependence still require separate qualification before this statistic can support a gate.
    """
    from lab.carry_study import expected_max_sharpe, probabilistic_sharpe
    from lab.trials import history, incomparable_provenance

    latest, selected = {}, []
    for trial in history(results_dir):
        if trial["kind"] == "fast-backtest" and trial["instrument"] == instrument_id and not trial["holdout"]:
            if trial.get("status") in ("reserved", "failed") or trial["result"].get("status") == "unavailable":
                return None, "incomplete or unavailable research attempts remain in the trial history"
            selected.append(trial)
            latest[trial["fingerprint"]] = trial
    if reason := incomparable_provenance(selected):
        return None, reason
    srs = []
    for trial in latest.values():
        if tuple(trial["window"]) != tuple(window):
            return None, "research windows differ; rerun configurations on the same recorded window"
        stats = trial["result"].get("return_stats", {})
        sr = stats.get("sr_daily")
        if stats.get("basis") != RETURN_BASIS or sr is None or not math.isfinite(sr):
            return None, "missing daily equity statistics in the research trial history"
        if stats.get("daily_returns", {}).keys() != current.get("daily_returns", {}).keys():
            return None, "research observation days differ"
        srs.append(sr)
    if len(srs) < 2:
        return None, "at least two research configurations with daily equity statistics are required"
    values = [current.get(k) for k in ("sr_daily", "skew", "kurt")]
    if current.get("days", 0) < 4 or any(v is None or not math.isfinite(v) for v in values):
        return None, "at least four days with finite return moments are required"
    score = probabilistic_sharpe(values[0], current["days"], values[1], values[2], expected_max_sharpe(srs))
    return (score, "") if math.isfinite(score) else (None, "undefined Sharpe uncertainty")


def _ledger(results_dir: Path) -> list[dict]:
    path = Path(results_dir) / LEDGER
    if not path.exists():
        return []
    try:
        rows = [parse_json(line) for line in path.read_text().splitlines() if line.strip()]
        if any(
            not isinstance(r, dict)
            or not {"instrument", "holdout_start", "fingerprint", "at"} <= r.keys()
            or not isinstance(r["instrument"], str)
            or type(r["holdout_start"]) is not int
            or not isinstance(r["fingerprint"], str)
            or not isinstance(r["at"], str)
            for r in rows
        ):
            raise ValueError("invalid holdout ledger entry")
        return rows
    except (OSError, ValueError, TypeError) as e:
        raise HoldoutGuard(f"unreadable holdout ledger {path}; restore it before evaluating: {e}") from e


def _holdout_state(results_dir: Path) -> dict:
    path = Path(results_dir) / HOLDOUT_STATE
    if not path.exists():
        return {"version": 1, "attempts": []}
    try:
        state = read_json(path)
        if state["version"] != 1 or not isinstance(state["attempts"], list):
            raise ValueError("unknown holdout state format")
        ids = set()
        for entry in state["attempts"]:
            if (
                not isinstance(entry["instrument"], str)
                or not isinstance(entry["at"], str)
                or not isinstance(entry["again"], str)
                or type(entry["holdout_start"]) is not int
                or type(entry["holdout_end"]) is not int
                or entry["holdout_end"] < entry["holdout_start"]
                or entry["status"] not in ("reserved", "failed", "completed")
                or not isinstance(entry["params"], dict)
                or entry["fingerprint"] != fingerprint(entry["params"])
                or not isinstance(entry["attempt_id"], str)
                or entry["attempt_id"] in ids
                or (entry["status"] == "completed" and not isinstance(entry["result"], dict))
            ):
                raise ValueError("invalid holdout attempt")
            ids.add(entry["attempt_id"])
        return state
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise HoldoutGuard(f"unreadable holdout state {path}; restore it before evaluating: {e}") from e


def _holdout_scope(instrument: str) -> str:
    return "carry" if instrument.startswith("carry-") else instrument


def check_holdout(results_dir: Path, instrument_id: str, params: dict, holdout_start: int, again: str = "") -> bool:
    """Read-only preflight, not a reservation. A new configuration on used data is also holdout reuse."""
    seen = [
        e
        for e in [*_ledger(results_dir), *_holdout_state(results_dir)["attempts"]]
        if _holdout_scope(e["instrument"]) == _holdout_scope(instrument_id) and e["holdout_start"] == holdout_start
    ]
    if seen and not again.strip():
        raise HoldoutGuard(
            f"the {instrument_id} holdout from {_iso(holdout_start)} was already reserved or evaluated "
            f"({seen[0]['at']}); repeat only with --again REASON"
        )
    return bool(seen)


@dataclass
class HoldoutEvaluation:
    result: dict | None = None
    reused: bool = False


@contextmanager
def holdout_run(
    results_dir: Path,
    instrument_id: str,
    params: dict,
    holdout_start: int,
    again: str = "",
    *,
    holdout_end: int,
    partition: str = PARTITION,
):
    """Serialize evaluations and releases, and fsync the frozen attempt before the caller can read holdout data.

    A killed process leaves a reserved attempt, which still consumes the holdout. A repeat requires --again,
    even for different parameters or carry mode. The atomic state is authoritative if the JSONL append fails.
    """
    if type(holdout_end) is not int or holdout_end < holdout_start:
        raise HoldoutGuard("no data in the frozen holdout window")
    frozen_params = json.loads(json.dumps(params, allow_nan=False))
    with file_lock(Path(results_dir) / HOLDOUT_LOCK):
        try:
            raw = read_json(Path(results_dir) / partition)
            if partition == PARTITION:
                actual = int(raw["instruments"][instrument_id]["holdout_start"])
            elif partition == "carry_partition.json":
                import pandas as pd

                actual = int(pd.Timestamp(raw["holdout_start"]).value)
            else:
                raise ValueError("unknown holdout partition")
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise HoldoutGuard(f"unreadable holdout partition: {e}") from e
        if actual != holdout_start:
            raise HoldoutGuard("holdout boundary changed before reservation; run the command again")
        reused = check_holdout(results_dir, instrument_id, frozen_params, holdout_start, again)
        state = _holdout_state(results_dir)
        entry = {
            "attempt_id": uuid4().hex,
            "at": _now(),
            "instrument": instrument_id,
            "fingerprint": fingerprint(frozen_params),
            "params": frozen_params,
            "holdout_start": holdout_start,
            "holdout_end": holdout_end,
            "again": again,
            "status": "reserved",
            "reused": reused,
        }
        state["attempts"].append(entry)
        atomic_json(Path(results_dir) / HOLDOUT_STATE, state)
        evaluation = HoldoutEvaluation(reused=reused)
        try:
            yield evaluation
            if not isinstance(evaluation.result, dict):
                raise HoldoutGuard("holdout evaluation ended without a result")
        except BaseException as e:
            entry.update(status="failed", finished_at=_now(), error=type(e).__name__)
            try:
                atomic_json(Path(results_dir) / HOLDOUT_STATE, state)
                append_json(Path(results_dir) / LEDGER, entry)
            except OSError:
                pass  # the fsynced reservation still blocks silent reuse; preserve the caller's exception
            raise
        else:
            entry.update(status="completed", finished_at=_now(), result=evaluation.result)
            atomic_json(Path(results_dir) / HOLDOUT_STATE, state)
            append_json(Path(results_dir) / LEDGER, entry)


def register_trial(
    results_dir: Path, kind: str, instrument_id: str, params: dict, window: tuple, result: dict, *, holdout=False
) -> int:
    """Legacy completed-result writer. CLI evaluations reserve with lab.trials.trial_run before data access."""
    from lab.trials import count_configurations

    path = Path(results_dir) / TRIALS
    fp = fingerprint(params)
    append_json(
        path,
        {
            "at": _now(),
            "kind": kind,
            "instrument": instrument_id,
            "holdout": holdout,
            "fingerprint": fp,
            "params": params,
            "window": list(window),
            "result": result,
        },
    )
    return count_configurations(results_dir, kind, instrument_id)


def release(results_dir: Path, catalog_path: str, instrument_id: str, reason: str) -> tuple[int, int]:
    """Moves the boundary to what is recorded now: the old holdout becomes research data and a new holdout
    starts. Returns (old, new) boundaries."""
    if not reason.strip():
        raise ValueError("a reason is required: it is recorded with the new boundary")
    with file_lock(Path(results_dir) / HOLDOUT_LOCK):
        path = Path(results_dir) / PARTITION
        old = boundary(results_dir, catalog_path, instrument_id)
        last = last_recorded(catalog_path, instrument_id)
        if last is None or last + 1 <= old:
            raise HoldoutGuard(f"nothing has been recorded for {instrument_id} since the holdout began ({_iso(old)})")
        with file_lock(path.with_name(path.name + ".lock")):
            part = _read_partition(path)
            entry = part["instruments"][instrument_id]
            if int(entry["holdout_start"]) != old:
                raise HoldoutGuard("the boundary changed while releasing; run it again")
            entry["holdout_start"] = last + 1
            entry["history"].append({"at": _now(), "holdout_start": last + 1, "reason": reason})
            atomic_json(path, part)
        return old, last + 1
