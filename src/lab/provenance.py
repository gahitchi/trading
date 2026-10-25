"""Content identity for strategy evidence and reproducible research artifacts."""

import hashlib
import importlib.metadata
import platform
from pathlib import Path

import pandas as pd

# The operator surface only: modules that cannot change a simulated result, a validation verdict or how the
# agents behave (Gemini audit F-R16, narrowed after ChatGPT C006-F1). Configuration, accounting, execution
# state, safety, authority, notifications (an alert outage blocks additions) and every module added later
# count as simulation code, so a change to any of them invalidates trials and approvals. The live runtime is
# bound to all code through runtime_digest().
NOT_SIMULATION = frozenset(
    {
        "app.py",  # the local control panel's HTTP server (it starts the engine through the CLI)
        "cli.py",  # argument parsing and printing
        "report.py",  # HTML reports of finished runs
        "paper_report.py",  # summaries of the paper journal
        "capture.py",  # read-only evidence snapshots
        "research/orchestrator.py",  # how the language models are asked; validation runs in worker.py
        "research/providers.py",
        "research/roles.py",
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


def simulation_digest(*, market: str = "equity") -> str:
    """Fingerprint only modules able to affect the selected research engine.

    Preserve pre-multi-market equity research digests: new crypto-only modules
    must not silently invalidate existing approved US-equity hypotheses.
    Shared agent/accounting changes still invalidate both markets. Future
    crypto deployments must explicitly request market="crypto".
    """
    if market not in {"equity", "crypto"}:
        raise ValueError(f"unsupported research market {market!r}")
    return _digest(lambda name: name not in NOT_SIMULATION and (market == "crypto" or not name.startswith("markets/")))


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
