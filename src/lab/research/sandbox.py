"""
Checks for AI-written signal code, before it is allowed anywhere near a backtest.

1. static check (AST): exactly one Signal subclass; only whitelisted imports
   (math, numpy, lab.signals.base); no file/network/process access, no eval/exec,
   no dunder attribute tricks, no module-level side effects
2. behavioural check in a separate, resource-limited Python process: the signal
   runs over 1,500 random-walk bars; it must not crash, must return numbers,
   must be deterministic and fast, and its lookback must be honest

Static checks block the obvious escape routes, not a determined attacker: read
any generated code before you promote it to paper trading.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_IMPORTS = {"math", "numpy"}
ALLOWED_FROM = {
    "lab.signals.base": {"Signal", "Window", "sma", "rsi", "clamp_target"},
    "math": None,
    "__future__": {"annotations"},
}
FORBIDDEN_NAMES = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
    "input",
    "breakpoint",
    "help",
    "exit",
    "quit",
    "memoryview",
    "type",
    "object",
    "__builtins__",
    "__loader__",
    "__spec__",
    "classmethod",
    "staticmethod",
    "property",
}
FORBIDDEN_NUMPY = {
    "load",
    "save",
    "savez",
    "savez_compressed",
    "loadtxt",
    "savetxt",
    "genfromtxt",
    "fromfile",
    "tofile",
    "memmap",
    "ctypeslib",
    "lib",
    "distutils",
    "f2py",
    "testing",
    "show_config",
    "DataSource",
}
MAX_SOURCE_BYTES = 20_000
MAX_LOOKBACK = 250


@dataclass
class CheckReport:
    ok: bool
    stage: str
    errors: list = field(default_factory=list)
    info: dict = field(default_factory=dict)

    def text(self) -> str:
        head = f"{self.stage}: {'passed' if self.ok else 'FAILED'}"
        return "\n".join([head, *(f"- {e}" for e in self.errors)])


def _is_constant_expr(node) -> bool:
    """Literals, names and arithmetic on them; no calls, attributes or indexing."""
    if isinstance(node, (ast.Constant, ast.Name)):
        return True
    if isinstance(node, ast.BinOp):
        return _is_constant_expr(node.left) and _is_constant_expr(node.right)
    if isinstance(node, ast.UnaryOp):
        return _is_constant_expr(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_constant_expr(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_constant_expr(k) for k in node.keys) and all(
            _is_constant_expr(v) for v in node.values
        )
    return False


def static_check(source: str) -> CheckReport:
    errs: list[str] = []
    if len(source.encode()) > MAX_SOURCE_BYTES:
        return CheckReport(False, "static", [f"source is larger than {MAX_SOURCE_BYTES} bytes"])
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return CheckReport(False, "static", [f"syntax error line {e.lineno}: {e.msg}"])

    classes = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
            if isinstance(node, ast.ClassDef):
                classes.append(node)
            continue
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and all(isinstance(t, ast.Name) for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
            and node.value is not None
            and _is_constant_expr(node.value)
        ):
            continue  # module-level constants, e.g. LOOKBACK = WARMUP + 1
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # docstring
        errs.append(f"line {node.lineno}: only imports, constants, functions and one class are allowed at module level")

    signal_classes = [c for c in classes if any(isinstance(b, ast.Name) and b.id == "Signal" for b in c.bases)]
    if len(signal_classes) != 1:
        errs.append(f"define exactly one class that subclasses Signal (found {len(signal_classes)})")
    if len(classes) != len(signal_classes):
        errs.append("no classes other than the Signal subclass")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    errs.append(f"line {node.lineno}: import {a.name} is not allowed (allowed: math, numpy)")
        elif isinstance(node, ast.ImportFrom):
            allowed = ALLOWED_FROM.get(node.module or "", False)
            if allowed is False:
                errs.append(f"line {node.lineno}: from {node.module} import ... is not allowed")
            elif allowed is not None:
                bad = [a.name for a in node.names if a.name not in allowed]
                if bad:
                    errs.append(f"line {node.lineno}: cannot import {bad} from {node.module}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errs.append(f"line {node.lineno}: use of {node.id!r} is not allowed")
        elif isinstance(node, ast.Attribute):
            is_super_init = (
                node.attr == "__init__"
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "super"
            )
            if node.attr.startswith("__") and node.attr.endswith("__") and not is_super_init:
                errs.append(f"line {node.lineno}: dunder attribute {node.attr!r} is not allowed")
            if node.attr in FORBIDDEN_NUMPY and isinstance(node.value, ast.Name) and node.value.id in ("np", "numpy"):
                errs.append(f"line {node.lineno}: numpy.{node.attr} is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            errs.append(f"line {node.lineno}: global/nonlocal is not allowed")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await)):
            errs.append(f"line {node.lineno}: async code is not allowed")

    info = {}
    if len(signal_classes) == 1:
        cls = signal_classes[0]
        info["class_name"] = cls.name
        attrs = {
            t.id: n.value for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)
        }
        for key in ("name", "description", "lookback", "defaults"):
            if key not in attrs:
                errs.append(f"class {cls.name} must set `{key}`")
        if "name" in attrs and isinstance(attrs["name"], ast.Constant):
            info["name"] = attrs["name"].value
        if not any(isinstance(n, ast.FunctionDef) and n.name == "target" for n in cls.body):
            errs.append(f"class {cls.name} must define target(self, w)")
    return CheckReport(not errs, "static", sorted(set(errs)), info)


_HARNESS = r"""
import importlib.util, json, math, resource, sys, time
resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
import numpy as np
from lab.signals.base import Signal, Window, clamp_target

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("candidate_signal", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
classes = [c for c in vars(mod).values() if isinstance(c, type) and issubclass(c, Signal) and c is not Signal]
cls = classes[0]
out = {"name": cls.name, "lookback": cls.lookback, "defaults": cls.defaults}

def run(seed):
    rng = np.random.default_rng(seed)
    n = 1500
    close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, n)))
    opn = close * np.exp(rng.normal(0, 0.004, n))
    high = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    vol = rng.lognormal(14, 0.4, n)
    dates = np.busday_offset(np.datetime64('2015-01-02'), np.arange(n), roll='forward')
    s = cls()
    lb = s.lookback
    if not isinstance(lb, int) or not 1 <= lb <= 250:
        raise ValueError(f"lookback must be an int in 1..250, got {lb!r}")
    targets, invalid, t0 = [], 0, time.perf_counter()
    for i in range(lb, n + 1):
        w = Window(opn[i-lb:i], high[i-lb:i], low[i-lb:i], close[i-lb:i], vol[i-lb:i], dates[i-lb:i])
        raw = s.target(w)
        t = clamp_target(raw)
        try:
            if not math.isfinite(float(raw)) or float(raw) != t:
                invalid += 1
        except Exception:
            invalid += 1
        s.last = t
        targets.append(t)
    return targets, invalid, (time.perf_counter() - t0) / max(1, len(targets))

a, inv, per_call = run(7)
b, _, _ = run(7)
out.update(
    calls=len(a), invalid=inv, ms_per_call=round(per_call * 1000, 3), deterministic=(a == b),
    mean_target=float(np.mean(a)), changes=int(sum(1 for x, y in zip(a, a[1:]) if x != y)),
    distinct=sorted(set(round(x, 4) for x in a))[:10],
)
print(json.dumps(out))
"""


def behaviour_check(path: Path, timeout_s: int = 90) -> CheckReport:
    proc = subprocess.run(
        [sys.executable, "-s", "-c", _HARNESS, str(path)],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        cwd=path.parent,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
        return CheckReport(False, "behaviour", ["signal crashed:", *tail])
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    errs = []
    if not info["deterministic"]:
        errs.append("not deterministic: same bars gave different targets")
    if info["invalid"]:
        errs.append(f"{info['invalid']} targets were not finite numbers in [-1, 1]")
    if info["ms_per_call"] > 5:
        errs.append(f"too slow: {info['ms_per_call']} ms per call (limit 5 ms)")
    if info["changes"] == 0:
        errs.append(
            "target never changes on 1,500 random bars: the signal is constant (equivalent to cash or buy-and-hold)"
        )
    return CheckReport(not errs, "behaviour", errs, info)


def check_file(path: Path) -> CheckReport:
    report = static_check(path.read_text())
    if not report.ok:
        return report
    b = behaviour_check(path)
    b.info = {**report.info, **b.info}
    return b
