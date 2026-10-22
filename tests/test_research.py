"""Research layer: sandbox, providers, experiments, the holdout ledger, the CLI."""

import json
import textwrap

import pytest

from lab import experiments
from lab.cli import main as lab_main
from lab.research.providers import ClaudeCodeProvider, LLMError, OllamaProvider, Provider
from lab.research.sandbox import check_file, static_check
from lab.signals.registry import SignalSpec

GOOD = textwrap.dedent("""
    from __future__ import annotations
    import numpy as np
    from lab.signals.base import Signal, sma

    class Mom(Signal):
        name = "mom"
        description = "x"
        lookback = 60
        defaults = {"n": 50}

        def __init__(self, **params):
            super().__init__(**params)
            self.lookback = self.params["n"] + 10

        def target(self, w):
            return 1.0 if w.close[-1] > sma(w.close, self.params["n"]) else 0.0
""")


def _file(tmp_path, src, name="signal.py"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(src))
    return p


def test_sandbox_accepts_a_proper_signal(tmp_path):
    r = check_file(_file(tmp_path, GOOD))
    assert r.ok, r.text()
    assert r.info["deterministic"] and r.info["changes"] > 0


@pytest.mark.parametrize(
    "snippet,expected",
    [
        ("import os", "import os"),
        ("import subprocess", "import subprocess"),
        ("from pathlib import Path", "pathlib"),
        ("x = open('/etc/passwd')", "module level"),
        ("y = eval('1')", "module level"),
    ],
)
def test_sandbox_blocks_module_level_escapes(snippet, expected):
    r = static_check(GOOD + "\n" + snippet + "\n")
    assert not r.ok and any(expected in e for e in r.errors), r.errors


@pytest.mark.parametrize(
    "body,expected",
    [
        ("return open('/etc/passwd').read()", "'open'"),
        ("return getattr(w, 'close')[-1]", "'getattr'"),
        ("return w.__class__", "__class__"),
        ("return super().__dict__", "__dict__"),
        ("return np.load('x.npy')", "numpy.load"),
        ("return type(w)", "'type'"),
    ],
)
def test_sandbox_blocks_escapes_inside_target(body, expected):
    src = GOOD.replace('return 1.0 if w.close[-1] > sma(w.close, self.params["n"]) else 0.0', body)
    r = static_check(src)
    assert not r.ok and any(expected in e for e in r.errors), r.errors


def test_sandbox_allows_derived_constants_but_not_calls():
    ok = GOOD.replace(
        "class Mom(Signal):", "WARMUP = 22\nLOOKBACK = WARMUP + 1\nLEVELS = (1, -2, 3.5)\n\n\nclass Mom(Signal):"
    )
    assert static_check(ok).ok, static_check(ok).errors
    bad = GOOD.replace("class Mom(Signal):", "X = len('abc')\n\n\nclass Mom(Signal):")
    assert not static_check(bad).ok
    bad2 = GOOD.replace("class Mom(Signal):", "X = np.pi\n\n\nclass Mom(Signal):")
    assert not static_check(bad2).ok


def test_sandbox_catches_crash_and_constant_signal(tmp_path):
    crash = check_file(_file(tmp_path, GOOD.replace("w.close[-1] >", "w.close[10_000] >"), "c.py"))
    assert not crash.ok and "crashed" in crash.errors[0]
    const = check_file(
        _file(
            tmp_path,
            GOOD.replace('return 1.0 if w.close[-1] > sma(w.close, self.params["n"]) else 0.0', "return 1.0"),
            "k.py",
        )
    )
    assert not const.ok and "constant" in const.errors[0]


class _Fixed(Provider):
    name = "fixed"

    def __init__(self, out):
        super().__init__("m", 5)
        self.out = out

    def _complete(self, system, prompt, schema):
        return self.out, None


def test_provider_enforces_schema():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    assert _Fixed({"a": 1}).complete_json(role="r", system="s", prompt="p", schema=schema) == {"a": 1}
    with pytest.raises(LLMError, match="schema"):
        _Fixed({"a": "no"}).complete_json(role="r", system="s", prompt="p", schema=schema)


def test_provider_errors_are_explanatory(tmp_path, monkeypatch):
    import lab.research.providers as providers

    monkeypatch.setattr(providers.shutil, "which", lambda _name: None)
    with pytest.raises(LLMError, match="Claude Code CLI not found"):
        ClaudeCodeProvider("haiku", 5, tmp_path)
    with pytest.raises(LLMError, match="cannot reach Ollama"):
        OllamaProvider("m", 3, "http://127.0.0.1:9").complete_json(
            role="r", system="s", prompt="p", schema={"type": "object"}
        )


def test_holdout_ledger_allows_once(goog_lab):
    spec = SignalSpec("sma_trend")
    experiments.check_holdout_allowed(goog_lab, spec)
    comp, _ = experiments.compare(goog_lab, [spec], ["GOOG"], goog_lab.period("holdout"))
    experiments.record_holdout(goog_lab, spec, comp)
    with pytest.raises(experiments.HoldoutUsed):
        experiments.check_holdout_allowed(goog_lab, spec)
    experiments.check_holdout_allowed(goog_lab, spec, reason="fixed a data bug")
    experiments.check_holdout_allowed(goog_lab, SignalSpec("sma_trend", {"fast": 20}))  # different params


def test_null_test_reports_a_valid_p_value(goog_lab):
    nt = experiments.null_test(
        goog_lab, SignalSpec("sma_trend"), ["GOOG"], goog_lab.period("train"), samples=4, workers=1
    )
    assert 1 / 5 <= nt["p_value"] <= 1 and len(nt["null_edges"]) == 4


def test_cli_end_to_end(goog_lab, capsys):
    cfgp = str(goog_lab.root / "lab.toml")
    assert lab_main(["--config", cfgp, "backtest", "sma_trend"]) == 0
    out = capsys.readouterr().out
    assert "buy_and_hold (control)" in out and "sma_trend" in out
    assert lab_main(["--config", cfgp, "data", "list"]) == 0
    with pytest.raises(SystemExit, match="guarded"):
        lab_main(["--config", cfgp, "backtest", "--period", "holdout"])
    saved = list((goog_lab.results_dir).glob("*-backtest/summary.json"))
    assert saved and json.loads(saved[0].read_text())["data"]["GOOG"]["rows"] == 2148
    report = (saved[0].parent / "report.html").read_text()
    assert "Deflated" in report or "DSR" in report
    assert report.count("data:image/png;base64,") >= 2 and "Data quality" in report


@pytest.mark.llm
def test_real_model_roundtrip(goog_lab):
    """Calls the configured provider for real: pytest -m llm"""
    assert lab_main(["--config", str(goog_lab.root / "lab.toml"), "llm-check"]) == 0
