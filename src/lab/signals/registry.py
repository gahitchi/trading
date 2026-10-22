"""
Find signals by name: the built-in ones, plus research hypotheses written by the
AI swarm (research/hypotheses/<id>/signal.py). Generated code is re-checked by
the sandbox's static analysis every time it is loaded.
"""

from __future__ import annotations

import importlib.util
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

    def make(self) -> Signal:
        if self.path is None:
            return BUILTIN[self.name](**self.params)
        return load_class(Path(self.path))(**self.params)

    def __hash__(self):
        return hash((self.name, json.dumps(self.params, sort_keys=True), self.path))


def load_class(path: Path) -> type[Signal]:
    from lab.research.sandbox import static_check

    report = static_check(path.read_text())
    if not report.ok:
        raise SignalNotFound(f"{path}: fails static checks:\n{report.text()}")
    spec = importlib.util.spec_from_file_location(f"lab_research_{path.parent.name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    classes = [c for c in vars(mod).values() if isinstance(c, type) and issubclass(c, Signal) and c is not Signal]
    if len(classes) != 1:
        raise SignalNotFound(f"{path}: expected one Signal subclass")
    return classes[0]


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
    load_class(path)(**params)
    return SignalSpec(name, params, str(path))


def available_research(research_dir: Path) -> list[str]:
    d = research_dir / "hypotheses"
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if hypothesis_status(research_dir, p.name) in LOADABLE | TRADABLE)
