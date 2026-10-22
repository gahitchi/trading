"""
The research swarm's cycle. AI agents propose and build; deterministic code judges.

    Researcher (AI) ─proposal─▶ Engineer (AI) ─code─▶ Sandbox checks ─┐
          ▲                         ▲   └──────── errors ◀────────────┘ (retry)
          │                         └─ issues ◀─ Critic (AI) ◀─ passing code
          │                                         │ accept
          │                                         ▼
          │                         Validator (train + noise floor, separate process)
          │                                         │ pass
          │                                         ▼
          │                         Test period, once ─▶ candidate / failed_test
          └───────── training results only ─────────┘  (NOTEBOOK.md + status.json)

Everything each agent receives and returns is written to the hypothesis folder.
The Researcher never sees test-period results, so the test set stays out-of-sample.
Promotion to paper trading is a human decision: `lab research promote <id>`.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from lab.config import LabConfig
from lab.research import roles
from lab.research.providers import Budget, BudgetExceeded, LLMError, Provider
from lab.research.sandbox import check_file
from lab.signals.builtin import BUILTIN

FINAL = {"rejected_by_critic", "broken", "failed_train", "failed_test", "candidate", "promoted", "error"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")
    s = re.sub(r"_+", "_", s)[:40].strip("_")
    if not s or not s[0].isalpha():
        s = "h_" + s
    return s[:40]


class Hypothesis:
    def __init__(self, root: Path, hid: str):
        self.id = hid
        self.dir = root / "hypotheses" / hid

    def write(self, name: str, obj) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / name
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str))

    def read(self, name: str):
        p = self.dir / name
        if not p.exists():
            return None
        return json.loads(p.read_text()) if name.endswith(".json") else p.read_text()

    @property
    def status(self) -> str | None:
        s = self.read("status.json")
        return s["status"] if s else None

    def set_status(self, status: str, note: str = "") -> None:
        s = self.read("status.json") or {"history": []}
        s["status"] = status
        s["history"].append({"at": _now(), "status": status, "note": note})
        self.write("status.json", s)

    def log_call(self, role: str, prompt: str, output) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "transcript.jsonl").open("a") as f:
            f.write(json.dumps({"at": _now(), "role": role, "prompt": prompt, "output": output}) + "\n")


class Orchestrator:
    def __init__(self, cfg: LabConfig, provider: Provider, *, log=print):
        self.cfg = cfg
        self.provider = provider
        self.root = cfg.research_dir
        self.log = log

    # ------------------------------------------------------------------ the shared record

    def all_hypotheses(self) -> list[Hypothesis]:
        d = self.root / "hypotheses"
        return [Hypothesis(self.root, p.name) for p in sorted(d.iterdir())] if d.exists() else []

    def past_for_researcher(self) -> list[dict]:
        """What the Researcher may see: proposals and TRAINING outcomes, never test results."""
        out = []
        for h in self.all_hypotheses():
            p = h.read("proposal.json") or {}
            t = h.read("train.json")
            entry = {
                "id": h.id,
                "title": p.get("title"),
                "hypothesis": p.get("hypothesis"),
                "outcome": {
                    "candidate": "passed training",
                    "failed_test": "passed training",
                    "promoted": "passed training",
                }.get(h.status, h.status),
            }
            if t:
                entry["train_sharpe_edge_vs_buy_and_hold"] = round(t["rows"][1]["sharpe_edge"], 3)
                entry["noise_floor_p_value"] = round(t["null"]["p_value"], 3)
            review = h.read("review.json")
            if review:
                entry["critic"] = review.get("summary")
            out.append(entry)
        builtin = [{"id": n, "title": c.description, "outcome": "built-in"} for n, c in BUILTIN.items()]
        return builtin + out

    def notebook(self, h: Hypothesis) -> None:
        p = h.read("proposal.json") or {}
        lines = [
            f"\n## {h.id}: {p.get('title', '')}",
            f"_{_now()} · status: **{h.status}**_",
            "",
            f"**Hypothesis.** {p.get('hypothesis', '')}",
            "",
            f"**Mechanism.** {p.get('mechanism', '')}",
            "",
            f"**Prediction.** {p.get('prediction', '')}",
            "",
        ]
        review = h.read("review.json")
        if review:
            lines += [
                f"**Critic** ({review['verdict']}, overfitting risk {review['overfitting_risk']}). {review['summary']}",
                "",
            ]
        t = h.read("train.json")
        if t:
            c, b = t["rows"][1], t["rows"][0]
            lines += [
                f"**Train {t['rows'][0].get('start')}..{t['rows'][0].get('end')}** on {', '.join(t['tickers'])}: "
                f"Sharpe {c.get('sharpe', 0):.2f} vs buy-and-hold {b.get('sharpe', 0):.2f} "
                f"(edge {c['sharpe_edge']:+.2f}); return {c.get('total_return', 0):+.1%} vs {b.get('total_return', 0):+.1%}; "
                f"max drawdown {c.get('max_drawdown', 0):.1%} vs {b.get('max_drawdown', 0):.1%}; "
                f"noise-floor p = {t['null']['p_value']:.3f} over {t['null']['samples']} bootstrap samples; "
                f"deflated Sharpe {c.get('dsr', float('nan')):.2f} after {c.get('trials', 1)} trial(s). "
                f"Checks: {', '.join(k + (' ✓' if v else ' ✗') for k, v in t['checks'].items())}.",
                "",
            ]
        te = h.read("test.json")
        if te:
            c, b = te["rows"][1], te["rows"][0]
            lines += [
                f"**Test (out-of-sample)**: Sharpe {c.get('sharpe', 0):.2f} vs {b.get('sharpe', 0):.2f} "
                f"(edge {te['sharpe_edge']:+.2f}) → {'survived' if te['survived'] else 'did not survive'}.",
                "",
            ]
        n = sum(1 for x in self.all_hypotheses() if x.read("train.json"))
        lines += [
            f"_Hypotheses validated so far: {n}. At p ≤ {self.cfg.research.max_p_value}, about "
            f"{n * self.cfg.research.max_p_value:.1f} would pass training by luck alone; the test period is the real filter._"
        ]
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "NOTEBOOK.md").open("a") as f:
            f.write("\n".join(lines) + "\n")

    # ------------------------------------------------------------------ the cycle

    def cycle(self) -> Hypothesis | None:
        rs = self.cfg.research
        budget = Budget(self.provider, rs.max_llm_calls_per_cycle)
        h = None
        try:
            h = self._propose(budget)
            if not self._implement(h, budget):
                return h
            if not self._validate(h):
                return h
            return h
        except BudgetExceeded as e:
            self.log(f"stopped: {e}")
            if h and h.status not in FINAL:
                h.set_status("error", str(e))
            return h
        except LLMError as e:
            self.log(f"model call failed: {e}")
            if h and h.status not in FINAL:
                h.set_status("error", str(e))
            return h
        finally:
            if h and h.status:
                self.notebook(h)

    def retry(self, hid: str) -> Hypothesis:
        """Implement and validate an existing proposal again (e.g. after an infrastructure fix).
        Only hypotheses that never reached validation can be retried: re-running one that
        failed on data would be p-hacking."""
        h = Hypothesis(self.root, hid)
        if not h.dir.exists():
            raise ValueError(f"no hypothesis {hid}")
        if h.status not in ("broken", "error", "rejected_by_critic", "proposed", "implemented"):
            raise ValueError(f"{hid} is {h.status!r}: it already met the data, so retrying would be p-hacking")
        h.set_status("proposed", "retry requested")
        budget = Budget(self.provider, self.cfg.research.max_llm_calls_per_cycle)
        try:
            if self._implement(h, budget):
                self._validate(h)
        except LLMError as e:
            self.log(f"model call failed: {e}")
            if h.status not in FINAL:
                h.set_status("error", str(e))
        finally:
            self.notebook(h)
        return h

    def _propose(self, budget: Budget) -> Hypothesis:
        train = self.cfg.period("train")
        prompt = roles.researcher_prompt(
            list(self.cfg.universe), (str(train.start), str(train.end)), self.past_for_researcher()
        )
        self.log("Researcher: proposing a hypothesis...")
        proposal = budget.call(
            role="researcher", system=roles.RESEARCHER_SYSTEM, prompt=prompt, schema=roles.PROPOSAL_SCHEMA
        )
        hid = slug(proposal["id"])
        taken = {x.id for x in self.all_hypotheses()} | set(BUILTIN)
        base, k = hid, 2
        while hid in taken:
            hid = f"{base[:36]}_{k}"
            k += 1
        proposal["id"] = hid
        h = Hypothesis(self.root, hid)
        h.log_call("researcher", prompt, proposal)
        h.write("proposal.json", proposal)
        h.set_status("proposed")
        self.log(f"  {hid}: {proposal['title']}")
        return h

    def _engineer(self, h: Hypothesis, budget: Budget, previous: str | None, feedback: str | None) -> str:
        proposal = h.read("proposal.json")
        prompt = roles.engineer_prompt(proposal, previous, feedback)
        out = budget.call(role="engineer", system=roles.ENGINEER_SYSTEM, prompt=prompt, schema=roles.CODE_SCHEMA)
        h.log_call("engineer", prompt, out)
        code = out["code"].strip() + "\n"
        code = re.sub(r"^\s*name\s*=\s*['\"][^'\"]*['\"]", f'    name = "{h.id}"', code, count=1, flags=re.M)
        h.write("signal.py", code)
        return code

    def _implement(self, h: Hypothesis, budget: Budget) -> bool:
        rs = self.cfg.research
        code, feedback, report = None, None, None
        last_passing = None
        reviews = 0
        for attempt in range(1 + rs.max_fix_attempts):
            self.log(f"Engineer: {'writing' if code is None else 'fixing'} {h.id} (attempt {attempt + 1})")
            code = self._engineer(h, budget, code, feedback)
            report = check_file(h.dir / "signal.py")
            h.write(
                "checks.json", {"ok": report.ok, "stage": report.stage, "errors": report.errors, "info": report.info}
            )
            if not report.ok:
                self.log(f"  sandbox: {report.stage} check failed ({len(report.errors)} issue(s))")
                feedback = "Automatic checks failed:\n" + report.text()
                continue
            self.log("  sandbox: passed")
            last_passing = (code, report)
            if reviews >= 2:
                break
            self.log("Critic: reviewing...")
            prompt = roles.critic_prompt(h.read("proposal.json"), code, report.info)
            review = budget.call(role="critic", system=roles.CRITIC_SYSTEM, prompt=prompt, schema=roles.REVIEW_SCHEMA)
            reviews += 1
            h.log_call("critic", prompt, review)
            h.write("review.json", review)
            self.log(f"  verdict: {review['verdict']} ({review['summary'][:100]})")
            if review["verdict"] == "accept":
                h.set_status("implemented", "sandbox passed, critic accepted")
                return True
            if review["verdict"] == "reject":
                h.set_status("rejected_by_critic", review["summary"])
                return False
            feedback = "The Critic asked for revisions:\n" + "\n".join(f"- {i}" for i in review["issues"])
        if last_passing is not None:
            # Static checks do not override a human/AI critic's unresolved defects.
            # Never promote code which was not explicitly accepted.
            h.set_status("rejected_by_critic", "review did not accept the final checked implementation")
            return False
        h.set_status("broken", "could not produce code that passes the sandbox")
        return False

    def _run_worker(self, mode: str, h: Hypothesis) -> bool:
        cmd = [sys.executable, "-m", "lab.research.worker", mode, str(h.dir), str(self.cfg.root / "lab.toml")]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            h.set_status("error", f"validator ({mode}) failed: {proc.stderr[-1500:]}")
            self.log(
                f"  validator failed: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.returncode}"
            )
            return False
        return True

    def _validate(self, h: Hypothesis) -> bool:
        self.log(f"Validator: train period + {self.cfg.research.null_samples}-sample noise floor...")
        if not self._run_worker("train", h):
            return False
        t = h.read("train.json")
        c = t["rows"][1]
        self.log(
            f"  Sharpe edge {c['sharpe_edge']:+.2f}, p = {t['null']['p_value']:.3f} -> {'PASS' if t['passed'] else 'fail'}"
        )
        if not t["passed"]:
            h.set_status("failed_train", "; ".join(k for k, v in t["checks"].items() if not v))
            return False
        h.set_status("validated", "passed training")
        self.log("Gate: one run on the test period...")
        if not self._run_worker("test", h):
            return False
        te = h.read("test.json")
        h.set_status("candidate" if te["survived"] else "failed_test", f"test Sharpe edge {te['sharpe_edge']:+.2f}")
        self.log(f"  test Sharpe edge {te['sharpe_edge']:+.2f} -> {h.status}")
        return te["survived"]
