"""Regression coverage for Arch dashboard disconnect and port collision findings."""

from __future__ import annotations

import errno
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lab import app


@pytest.mark.parametrize(
    "disconnect",
    [
        BrokenPipeError(32, "Broken pipe"),
        ConnectionResetError(104, "Reset"),
        ConnectionAbortedError(103, "Aborted"),
        TimeoutError("timeout"),
    ],
)
def test_browser_disconnections_do_not_print_handler_tracebacks(disconnect):
    handler = object.__new__(app.Handler)
    handler.close_connection = False
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = Mock()
    handler.wfile.write.side_effect = disconnect
    handler.reply(200, {"ok": True})
    assert handler.close_connection is True
    handler.send_response.assert_called_once_with(200)


def test_unrelated_output_failure_is_not_silently_swallowed():
    handler = object.__new__(app.Handler)
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = Mock()
    handler.wfile.write.side_effect = RuntimeError("disk or handler programming fault")
    with pytest.raises(RuntimeError, match="programming fault"):
        handler.reply(200, {"ok": True})


def test_occupied_loopback_port_explains_port_override_without_killing_listener(tmp_path, monkeypatch):
    class ControllerStub:
        def __init__(self, _config):
            self.directory = tmp_path

    def busy(_address, _controller):
        raise OSError(errno.EADDRINUSE, "already in use")

    monkeypatch.setattr(app, "Controller", ControllerStub)
    monkeypatch.setattr(app, "AppServer", busy)
    args = SimpleNamespace(directory=str(tmp_path), host="127.0.0.1", port=8765, config=None, no_browser=True)
    with pytest.raises(OSError, match=r"port in use.*--port"):
        app.serve(args)
    assert (tmp_path / "LOCK").exists()


def test_public_cli_reports_occupied_port_without_traceback(tmp_path, monkeypatch, capsys):
    from lab import cli

    class ControllerStub:
        def __init__(self, _config):
            self.directory = tmp_path

    def busy(_address, _controller):
        raise OSError(errno.EADDRINUSE, "already in use")

    monkeypatch.setattr(app, "Controller", ControllerStub)
    monkeypatch.setattr(app, "AppServer", busy)
    status = cli.main(["app", "--directory", str(tmp_path), "--port", "8765", "--no-browser"])
    err = capsys.readouterr().err
    assert status == 2
    assert "port in use" in err
    assert "--port" in err
    assert "Traceback" not in err


def test_cli_does_not_hide_unrelated_bind_failures(tmp_path, monkeypatch):
    from lab import cli

    class ControllerStub:
        def __init__(self, _config):
            self.directory = tmp_path

    def denied(_address, _controller):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(app, "Controller", ControllerStub)
    monkeypatch.setattr(app, "AppServer", denied)
    with pytest.raises(OSError, match="permission denied"):
        cli.main(["app", "--directory", str(tmp_path), "--no-browser"])
