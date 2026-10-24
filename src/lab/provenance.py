"""Content identity for strategy evidence and reproducible research artifacts."""

import hashlib
import importlib.metadata
import platform
from pathlib import Path

import pandas as pd

# Modules that cannot change a backtest's numbers: the operator surface, the live paper runtime's plumbing and
# the language-model orchestration (Gemini audit F-R16). Everything else, including any module added later,
# counts as simulation code: an unlisted new module invalidates evidence rather than slipping past it.
NOT_SIMULATION = frozenset(
    {
        "app.py",
        "cli.py",
        "report.py",
        "paper_report.py",
        "notify.py",
        "capture.py",
        "runtime_ops.py",
        "paper.py",
        "authority.py",
        "safety.py",
        "halt.py",
        "agents/alerts.py",
        "agents/broker_bars.py",
        "agents/readiness.py",
        "agents/smoke.py",
        "research/orchestrator.py",
        "research/providers.py",
        "research/roles.py",
        "sim/__init__.py",
        "sim/broker.py",
        "sim/clients.py",
        "sim/harness.py",
        "sim/intraday.py",
    }
)


def _digest(include) -> str:
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        name = p.relative_to(root).as_posix()
        if include(name):
            digest.update(name.encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


def runtime_digest() -> str:
    """Every module: binds a live paper runtime to the exact code it runs (preflight, upgrade)."""
    return _digest(lambda name: True)


def simulation_digest() -> str:
    """The modules that shape simulated results and validation evidence: trial identity, approvals and the
    deployment context. A cosmetic edit to the app or the command line no longer invalidates them."""
    return _digest(lambda name: name not in NOT_SIMULATION)


def frame_digest(frames):
    digest = hashlib.sha256()
    for name, frame in sorted(frames.items()):
        digest.update(name.encode())
        digest.update(str(list(frame.columns)).encode())
        digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return digest.hexdigest()


def environment():
    packages = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata.get("Name")}
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": dict(sorted(packages.items())),
        "source_sha256": runtime_digest(),
    }
