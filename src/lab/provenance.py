"""Content identity for strategy evidence and reproducible research artifacts."""

import hashlib
import importlib.metadata
import platform
from pathlib import Path

import pandas as pd


def source_digest():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        digest.update(str(p.relative_to(root)).encode())
        digest.update(p.read_bytes())
    return digest.hexdigest()


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
        "source_sha256": source_digest(),
    }
