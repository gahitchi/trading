"""
Generated strategy code runs in a locked-down worker process, never in the lab's own process.

The static check (sandbox.static_check) is a first filter, not a boundary: an allowed library
call can still have side effects, and importing a module executes its class bodies (G007-F4,
ChatGPT's audit, which wrote a file from a class body during import). So the lab never imports
AI-written code. Each research signal gets a worker that:

  1. imports only numpy and lab.signals.base (trusted code), with an empty environment
  2. locks itself down BEFORE it sees the generated source:
       - closes every inherited file descriptor except its two pipes and stderr, and sets
         RLIMIT_NOFILE to 3 as an additional resource cap
       - RLIMIT_FSIZE 0 (no file may be written), RLIMIT_NPROC 0 (no new processes),
         RLIMIT_AS and RLIMIT_CPU caps
       - drops root to nobody when the host grants that capability (Docker runs unprivileged);
         the mandatory syscall filter also forbids changing credentials or raising limits
       - installs a default-deny Linux seccomp syscall filter (lab.research.syscalls);
         file/network/process operations remain forbidden even if a descriptor is closed
  3. compiles the source, instantiates the one Signal subclass, then answers target() calls

The parent sends price windows down a pipe and reads back only a fixed-format reply (a status
byte, a float, a short UTF-8 message): no pickles or code ever travel from the worker to the
parent. Every call has a deadline; a worker that hangs, crashes or misbehaves is killed and the
signal raises, which the SignalAgent turns into "go flat".

Not a container: the worker shares the kernel and can read memory already in its own process
(nothing secret is there: it is started with an empty environment). Network namespaces would
need privileges the lab does not have; the syscall filter blocks sockets and path operations.
Linux and libseccomp are required; failure to install the filter refuses the signal.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from lab.signals.base import Signal, Window

_SRC = str(Path(__file__).resolve().parents[2])
_HDR = struct.Struct("<Iid")  # rows, has_dates, last target
_REPLY = struct.Struct("<BdI")  # status (0 ok, 1 error), value, message length

STARTUP_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 2.0


class IsolationError(RuntimeError):
    pass


_WORKER = r"""
import json, os, resource, struct, sys
sys.path.insert(0, sys.argv[1])
import math
import numpy as np
from lab.signals.base import Signal, Window, clamp_target, rsi, sma
from lab.research.syscalls import install

def _read(n):
    buf = b""
    while len(buf) < n:
        chunk = os.read(0, n - len(buf))
        if not chunk:
            os._exit(0)
        buf += chunk
    return buf

def _reply(status, value, msg=""):
    data = msg.encode("utf-8", "replace")[:2000]
    os.write(1, struct.pack("<BdI", status, value, len(data)) + data)

n = struct.unpack("<I", _read(4))[0]
setup = json.loads(_read(n))

# ---- lock down before touching the generated source -------------------------------------
os.closerange(3, 65536)
if os.geteuid() == 0:
    try:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    except PermissionError:
        # Some container runtimes expose UID 0 without SETUID/SETGID capability.
        # The mandatory syscall filter below is still the security boundary and
        # forbids privilege changes, filesystem access and process creation.
        pass
for lim, val in ((resource.RLIMIT_NOFILE, 3), (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_NPROC, 0),
                 (resource.RLIMIT_AS, 1 << 30), (resource.RLIMIT_CPU, 900)):
    resource.setrlimit(lim, (val, val))

try:
    install()
    ns = {"__name__": "candidate_signal", "__builtins__": __builtins__}
    exec(compile(setup["source"], "signal.py", "exec"), ns)
    classes = [c for c in ns.values() if isinstance(c, type) and issubclass(c, Signal) and c is not Signal]
    if len(classes) != 1:
        raise ValueError(f"expected exactly one Signal subclass, found {len(classes)}")
    sig = classes[0](**setup["params"])
    meta = {"name": str(sig.name), "description": str(sig.description), "lookback": sig.lookback,
            "defaults": sig.defaults, "params": sig.params}
    json.dumps(meta, allow_nan=False)
    _reply(0, 0.0, json.dumps(meta))
except BaseException as e:
    _reply(1, 0.0, f"{type(e).__name__}: {e}")
    os._exit(0)

hdr = struct.Struct("<Iid")
while True:
    rows, has_dates, last = hdr.unpack(_read(hdr.size))
    cols = [np.frombuffer(_read(8 * rows), dtype="<f8") for _ in range(6)]  # o, h, l, c, v, minute
    dates = np.frombuffer(_read(8 * rows), dtype="<i8").astype("datetime64[D]") if has_dates else None
    try:
        sig.last = last
        raw = sig.target(Window(*cols[:5], dates, cols[5]))
        value = float(raw)
        _reply(0, value)
    except BaseException as e:
        _reply(1, 0.0, f"{type(e).__name__}: {e}")
"""


class IsolatedSignal(Signal):
    """A research Signal whose code runs only inside its locked-down worker (see module doc)."""

    def __init__(self, source: str, params: dict | None = None, *, label_path: str = ""):
        self._proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _WORKER, _SRC],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
            cwd="/",
            close_fds=True,
            bufsize=0,
        )
        os.set_blocking(self._proc.stdin.fileno(), False)
        self._path = label_path
        payload = json.dumps({"source": source, "params": params or {}}).encode()
        self._send(struct.pack("<I", len(payload)) + payload)
        status, _, msg = self._recv(STARTUP_TIMEOUT_S)
        if status != 0:
            self.close()
            raise IsolationError(f"{label_path or 'research signal'}: {msg}")
        meta = json.loads(msg)
        lookback = meta.get("lookback")
        if type(lookback) is not int or not 1 <= lookback <= 250:
            self.close()
            raise IsolationError(f"lookback must be an int in 1..250, got {lookback!r}")
        if not isinstance(meta.get("params"), dict) or not isinstance(meta.get("defaults"), dict):
            self.close()
            raise IsolationError("signal parameters must be a dict")
        self.name = str(meta["name"])
        self.description = str(meta["description"])
        self.lookback = lookback
        self.defaults = dict(meta["defaults"])
        self.params = dict(meta["params"])
        self.last = 0.0
        self.calls = 0

    # ------------------------------------------------------------------ pipe

    def _send(self, data: bytes, timeout: float = STARTUP_TIMEOUT_S) -> None:
        try:
            fd = self._proc.stdin.fileno()
            deadline = time.monotonic() + timeout
            remaining = memoryview(data)
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([], [fd], [], left)[1]:
                    self.close()
                    raise IsolationError(f"{self._path or 'research signal'}: worker write timed out")
                try:
                    count = os.write(fd, remaining)
                    remaining = remaining[count:]
                except BlockingIOError:
                    continue
        except (BrokenPipeError, ValueError, OSError) as e:
            self.close()
            raise IsolationError(f"{self._path or 'research signal'}: worker is gone") from e

    def _read_exact(self, n: int, deadline: float) -> bytes:
        out = self._proc.stdout
        buf = b""
        fd = out.fileno()
        while len(buf) < n:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                self.close()
                raise IsolationError(f"{self._path or 'research signal'}: worker timed out")
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                self.close()
                raise IsolationError(f"{self._path or 'research signal'}: worker exited")
            buf += chunk
        return buf

    def _recv(self, timeout: float) -> tuple[int, float, str]:
        deadline = time.monotonic() + timeout
        status, value, length = _REPLY.unpack(self._read_exact(_REPLY.size, deadline))
        if length > 2000:
            self.close()
            raise IsolationError("malformed reply from worker")
        msg = self._read_exact(length, deadline).decode("utf-8", "replace") if length else ""
        return status, value, msg

    # ------------------------------------------------------------------ Signal

    def target(self, w: Window) -> float:
        if self._proc is None:
            raise IsolationError(f"{self._path or 'research signal'}: worker is closed")
        rows = len(w.close)
        if rows > 250 or any(len(a) != rows for a in (w.open, w.high, w.low, w.volume, w.minute, w.dates)):
            raise IsolationError("window must contain at most 250 aligned rows")
        has_dates = not np.isnat(w.dates).all() if rows else False
        parts = [_HDR.pack(rows, int(has_dates), float(self.last))]
        parts += [
            np.ascontiguousarray(a, dtype="<f8").tobytes() for a in (w.open, w.high, w.low, w.close, w.volume, w.minute)
        ]
        if has_dates:
            parts.append(np.ascontiguousarray(w.dates.astype("datetime64[D]").astype("<i8")).tobytes())
        self._send(b"".join(parts), CALL_TIMEOUT_S)
        status, value, msg = self._recv(CALL_TIMEOUT_S)
        self.calls += 1
        if status != 0:
            raise IsolationError(msg or "signal raised")
        return value

    def close(self) -> None:
        proc, self._proc = getattr(self, "_proc", None), None
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def __del__(self):
        with contextlib.suppress(Exception):  # interpreter shutdown
            self.close()


def load_isolated(path: Path, params: dict | None = None, *, expect_sha256: str | None = None) -> IsolatedSignal:
    """Statically check, then start the generated signal at `path` in its own locked-down worker.

    expect_sha256: for promoted signals, the approved source digest. The bytes are read ONCE, and those
    same bytes are hashed, checked and run, so a file swapped after approval is refused."""
    import hashlib

    from lab.research.sandbox import static_check
    from lab.signals.registry import SignalNotFound

    raw = path.read_bytes()
    if expect_sha256 is not None and hashlib.sha256(raw).hexdigest() != expect_sha256:
        raise SignalNotFound(f"{path}: source differs from the approved version; re-validate and promote again")
    source = raw.decode("utf-8")
    report = static_check(source)
    if not report.ok:
        raise SignalNotFound(f"{path}: fails static checks:\n{report.text()}")
    try:
        return IsolatedSignal(source, params, label_path=str(path))
    except IsolationError as e:
        raise SignalNotFound(str(e)) from e
