"""
Find signals by name: the built-in ones, plus research hypotheses written by the
AI swarm (research/hypotheses/<id>/signal.py). Generated code is re-checked by the
sandbox's static analysis every time it is loaded, and then runs ONLY inside a
locked-down worker process (lab.research.isolation): the lab never imports it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from lab.signals.base import Signal
from lab.signals.builtin import BUILTIN

# Hypothesis states that may be backtested by name / paper traded.
LOADABLE = {"candidate", "promoted", "failed_test", "failed_train", "validated"}
TRADABLE = {"promoted"}


class SignalNotFound(Exception):
    pass


@dataclass(frozen=True)
class SignalSpec:
    """Picklable recipe for building a fresh Signal instance (needed for parallel runs)."""

    name: str
    params: dict = field(default_factory=dict)
    path: str | None = None  # source file for research signals
    sha256: str | None = None  # promoted signals: the approved source; make() refuses anything else

    def make(self) -> Signal:
        if self.path is None:
            return BUILTIN[self.name](**self.params)
        from lab.research.isolation import load_isolated

        return load_isolated(Path(self.path), self.params, expect_sha256=self.sha256)

    def __hash__(self):
        return hash((self.name, json.dumps(self.params, sort_keys=True), self.path))


def hypothesis_status(research_dir: Path, hid: str) -> str | None:
    p = research_dir / "hypotheses" / hid / "status.json"
    return json.loads(p.read_text())["status"] if p.exists() else None


def resolve(name: str, research_dir: Path, params: dict | None = None, *, require_tradable: bool = False) -> SignalSpec:
    params = params or {}
    if name in BUILTIN:
        BUILTIN[name](**params)  # validates parameters
        return SignalSpec(name, params)
    status = hypothesis_status(research_dir, name)
    if status is None:
        known = sorted(BUILTIN) + available_research(research_dir)
        raise SignalNotFound(f"unknown signal {name!r}. Known: {', '.join(known)}")
    if require_tradable and status not in TRADABLE:
        raise SignalNotFound(
            f"{name} is {status!r}; only promoted hypotheses can be paper traded (lab research promote {name})"
        )
    if status not in LOADABLE and status not in TRADABLE:
        raise SignalNotFound(f"{name} is {status!r} and has no validated code yet")
    path = research_dir / "hypotheses" / name / "signal.py"
    from lab.research.approval import ApprovalError, verify_approval
    from lab.research.isolation import load_isolated

    digest = None
    if require_tradable:
        try:
            digest = verify_approval(path.parent, params)
        except ApprovalError as e:
            raise SignalNotFound(str(e)) from e
    load_isolated(path, params, expect_sha256=digest).close()  # checks the code and its parameters, in the worker
    return SignalSpec(name, params, str(path), digest)


def available_research(research_dir: Path) -> list[str]:
    d = research_dir / "hypotheses"
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if hypothesis_status(research_dir, p.name) in LOADABLE | TRADABLE)
