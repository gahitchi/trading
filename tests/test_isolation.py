"""Generated code never runs in the lab's process (G007-F4); promotion binds the traded artifact (G007-F5)."""

import json

import numpy as np
import pytest

from lab.research.approval import ApprovalError, sha256_bytes, write_approval
from lab.research.isolation import IsolatedSignal, IsolationError
from lab.research.sandbox import static_check
from lab.signals.base import Window
from lab.signals.registry import SignalNotFound, SignalSpec, resolve

HEADER = """import numpy as np
from lab.signals.base import Signal, sma
class Probe(Signal):
    name = "probe"
    description = "audit"
    lookback = 3
    defaults = {"k": 2}
"""


def _src(body="        return float(w.close[-1] > sma(w.close, self.params['k']))", extra=""):
    return HEADER + extra + "\n    def target(self, w):\n" + body + "\n"


def _window():
    a = np.array([1.0, 2.0, 3.0])
    return Window(a, a + 1, a - 1, a, np.ones(3), np.arange(3).astype("datetime64[D]"))


def test_isolated_signal_works_and_reports_its_metadata():
    s = IsolatedSignal(_src(), {"k": 2})
    try:
        assert (s.name, s.lookback, s.params) == ("probe", 3, {"k": 2})
        assert s.target(_window()) == 1.0
    finally:
        s.close()


def test_class_body_side_effect_that_passes_the_static_check_cannot_touch_the_filesystem(tmp_path):
    """ChatGPT's reproduction: an allowed numpy method in a class body wrote a file at import."""
    marker = tmp_path / "marker.bin"
    src = _src(extra=f"    marker = np.array([7]).tofile({str(marker)!r})\n")
    assert static_check(src).ok  # the static filter alone lets it through
    (tmp_path / "signal.py").write_text(src)
    with pytest.raises(SignalNotFound, match="Operation not permitted"):
        SignalSpec("probe", {}, str(tmp_path / "signal.py")).make()
    assert not marker.exists()


@pytest.mark.parametrize(
    "extra, body",
    [
        ("    data = np.fromfile('/etc/hostname', dtype='u1')\n", None),  # read a file
        ("", "        np.array([1]).tofile('/tmp/lab-isolation-probe'); return 0.0"),  # write from target()
        ("", "        import socket\n        socket.socket(); return 0.0"),  # network
    ],
)
def test_files_and_sockets_are_out_of_reach(extra, body):
    try:
        s = IsolatedSignal(_src(**({"body": body} if body else {}), extra=extra))
    except IsolationError:
        return  # refused while loading
    try:
        with pytest.raises(IsolationError):
            s.target(_window())
    finally:
        s.close()


def test_a_hanging_signal_is_killed_at_the_deadline():
    s = IsolatedSignal(_src(body="        while True:\n            pass"))
    with pytest.raises(IsolationError, match="timed out"):
        s.target(_window())


def test_descriptor_reuse_and_path_operations_cannot_escape(tmp_path):
    """These programs pass the static filter. NOFILE/FSIZE alone stop neither one."""
    marker = tmp_path / "sentinel"
    marker.write_text("7")
    for body in (
        f"        api = np.array([0]).ctypes._ctypes._os\n        api.unlink({str(marker)!r})\n        return 0.0",
        f"        api = np.array([0]).ctypes._ctypes._os\n        api.close(2)\n"
        f"        fd = api.open({str(marker)!r}, 0)\n        return float(api.read(fd, 1))",
    ):
        src = _src(body=body)
        assert static_check(src).ok
        s = IsolatedSignal(src)
        try:
            with pytest.raises(IsolationError, match="Operation not permitted"):
                s.target(_window())
        finally:
            s.close()
        assert marker.read_text() == "7"


def test_sending_to_an_unresponsive_worker_has_a_deadline(monkeypatch):
    from lab.research import isolation

    monkeypatch.setattr(isolation, "CALL_TIMEOUT_S", 0.15)
    # After returning once, stop consuming input; the next 250-row window exceeds
    # the pipe buffer. A receive-only timeout never protected this write.
    s = IsolatedSignal(_src())
    try:
        import os
        import signal
        import time

        os.kill(s._proc.pid, signal.SIGSTOP)
        started = time.monotonic()
        with pytest.raises(IsolationError, match="write timed out"):
            s._send(b"x" * 1_000_000, timeout=0.15)
        assert time.monotonic() - started < 2
    finally:
        s.close()


# ---------------------------------------------------------------------------- promotion binding


def _promoted(research, body="        return 0.1"):
    hdir = research / "hypotheses" / "probe"
    hdir.mkdir(parents=True)
    (hdir / "signal.py").write_text(_src(body=body))
    validation = {
        "source_sha256": sha256_bytes((hdir / "signal.py").read_bytes()),
        "params": {},
        "deployment": {},
        "config_fingerprint": "test-fixture",
    }
    for name in ("train.json", "test.json"):
        (hdir / name).write_text(json.dumps({"passed": True, "survived": True, "validation": validation}))
    (hdir / "status.json").write_text(json.dumps({"status": "promoted", "history": []}))
    write_approval(hdir, (hdir / "signal.py").read_bytes(), {})
    return hdir


def test_promotion_binds_source_evidence_and_parameters(tmp_path):
    """ChatGPT's reproduction: editing a promoted signal from 0.1 to 0.999 still traded."""
    hdir = _promoted(tmp_path)
    spec = resolve("probe", tmp_path, require_tradable=True)
    s = spec.make()
    assert s.target(_window()) == pytest.approx(0.1)
    s.close()
    with pytest.raises(SignalNotFound, match="parameters"):
        resolve("probe", tmp_path, {"k": 5}, require_tradable=True)
    (hdir / "signal.py").write_text(_src(body="        return 0.999"))
    with pytest.raises(SignalNotFound, match="changed since it was promoted"):
        resolve("probe", tmp_path, require_tradable=True)
    with pytest.raises(SignalNotFound, match="differs from the approved"):
        spec.make()  # a spec resolved before the edit refuses the edited file too


def test_changed_evidence_or_missing_manifest_voids_the_approval(tmp_path):
    hdir = _promoted(tmp_path)
    (hdir / "test.json").write_text(json.dumps({"passed": True, "edited": 1}))
    with pytest.raises(SignalNotFound, match=r"test\.json changed"):
        resolve("probe", tmp_path, require_tradable=True)
    (hdir / "approval.json").unlink()
    with pytest.raises(SignalNotFound, match="approval manifest"):
        resolve("probe", tmp_path, require_tradable=True)


def test_source_changed_between_validation_and_promotion_is_rejected(tmp_path):
    hdir = _promoted(tmp_path)
    (hdir / "signal.py").write_text(_src(body="        return 0.999"))
    with pytest.raises(ApprovalError, match="re-validate"):
        write_approval(hdir, (hdir / "signal.py").read_bytes(), {})


def test_failed_or_unbound_validation_cannot_be_promoted(tmp_path):
    hdir = _promoted(tmp_path)
    raw = (hdir / "signal.py").read_bytes()
    for bad in ({"passed": True}, {"survived": False}, {"validation": {}}):
        (hdir / "test.json").write_text(json.dumps(bad))
        with pytest.raises(ApprovalError, match="re-validate"):
            write_approval(hdir, raw, {})


def test_deployment_configuration_is_bound_to_validation(tmp_path):
    from lab.research.approval import verify_approval

    hdir = _promoted(tmp_path)
    with pytest.raises(ApprovalError, match="deployment configuration"):
        verify_approval(hdir, {}, context={"risk": "changed"})
