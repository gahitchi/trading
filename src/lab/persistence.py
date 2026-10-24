"""Durable local state: exclusive writers, finite JSON and atomic fsynced replacement."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import tempfile
from pathlib import Path


def read_json(path):
    def invalid(value):
        raise ValueError(f"nonfinite JSON number: {value}")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("JSON number overflows finite float")
        return result

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        Path(path).read_text(), parse_constant=invalid, parse_float=finite_float, object_pairs_hook=unique_object
    )


def atomic_json(path, value):
    p = Path(path)
    raw = json.dumps(value, allow_nan=False, indent=1)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=p.name + ".", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, p)
        directory = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


@contextlib.contextmanager
def file_lock(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def nullable_numbers(value):
    """Reports may have unavailable estimates; state must instead reject them."""
    import math

    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: nullable_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [nullable_numbers(v) for v in value]
    return value


def append_json(path, value):
    p = Path(path)
    raw = json.dumps(value, allow_nan=False) + "\n"
    with file_lock(p.with_name(p.name + ".lock")), p.open("a") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def durable_unlink(path):
    p = Path(path)
    p.unlink()
    fd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
