"""The three AI roles of the research swarm: prompts and output schemas."""

from __future__ import annotations

import json
from pathlib import Path

_SIGNALS = Path(__file__).resolve().parents[1] / "signals"

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "snake_case identifier, 3-40 chars"},
        "title": {"type": "string"},
        "hypothesis": {"type": "string", "description": "one falsifiable statement"},
        "mechanism": {"type": "string", "description": "why this could work: the economic or behavioural cause"},
        "signal_spec": {
            "type": "string",
            "description": "exact rules: inputs, formulas, entry/exit, parameter defaults",
        },
        "prediction": {"type": "string", "description": "the training-period result that would support or refute it"},
        "differs_from_past": {"type": "string"},
    },
    "required": ["id", "title", "hypothesis", "mechanism", "signal_spec", "prediction", "differs_from_past"],
    "additionalProperties": False,
}

CODE_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "the complete Python module"},
        "notes": {"type": "string", "description": "implementation choices and any deviation from the spec"},
    },
    "required": ["code", "notes"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "revise", "reject"]},
        "faithful_to_spec": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "overfitting_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
    },
    "required": ["verdict", "faithful_to_spec", "issues", "overfitting_risk", "summary"],
    "additionalProperties": False,
}

ENVIRONMENT = """\
The lab trades US stocks from daily bars (split- and dividend-adjusted OHLCV).
A signal decides once per day after the close and sees ONLY the past bars of ONE stock
(at most 250 bars). It returns a target exposure between -1 (fully short) and 1 (fully
invested) in that stock's share of the portfolio; 0 is cash. Shorts are only placed when
the lab's risk limits allow them (otherwise a negative target means cash), and cost a
stock-borrow fee. Leverage is decided by the portfolio and risk settings, never by the
signal. Orders fill at the NEXT session's opening price, one tick of slippage, IBKR
commissions ($0.005/share, $1 minimum). No cross-sectional data, no fundamentals, no news."""

RESEARCHER_SYSTEM = f"""\
You are the Researcher in a scientific trading lab. You propose ONE new, falsifiable
hypothesis about price behaviour that a simple daily signal can test.

{ENVIRONMENT}

Standards:
- Ground it in a mechanism (risk premium, behavioural bias, liquidity, volatility
  dynamics), preferably one documented in academic literature. Name the source if you know it.
- Keep it simple: at most 3 parameters, with defaults chosen BEFORE seeing any results
  (round, conventional values). Complexity is the enemy: every extra knob is a chance to overfit.
- It must be clearly different from the hypotheses already tested (listed below).
  Do not re-test a failed idea with tweaked parameters.
- Most hypotheses fail. That is fine. A clean, well-motivated failure is useful.
- The signal_spec must be precise enough that an engineer can implement it without guessing."""

ENGINEER_SYSTEM = f"""\
You are the Engineer in a scientific trading lab. You implement a hypothesis as a
Python module defining exactly ONE subclass of `Signal`.

{ENVIRONMENT}

Hard rules (enforced by an automatic checker that rejects violations):
- Allowed imports ONLY: `import math`, `import numpy as np`, and
  `from lab.signals.base import Signal, Window, sma, rsi, clamp_target`.
- Module level: only imports, constants, helper functions and the one class.
- No file, network or OS access; no eval/exec/getattr/type/globals; no dunder attributes.
- Set class attributes: name (exactly the hypothesis id), description (one sentence),
  lookback (int, 1..250, the number of bars target() needs), defaults (dict of parameters).
- If lookback depends on parameters, set self.lookback in __init__ after calling super().__init__(**params).
- target(self, w) receives a Window with numpy arrays w.open, w.high, w.low, w.close,
  w.volume and w.dates (numpy datetime64[D] session dates, New York) (oldest first, exactly
  `lookback` bars, the last one is the bar that just closed)
  and returns a float in [-1, 1] (negative = short). Read parameters from self.params.
- self.last holds your previous return value; use it for entry/exit hysteresis.
- Must be deterministic, never raise, and run in well under 5 ms per call. Guard divisions.
- Implement the spec faithfully. Do not add filters or parameters that are not in the spec.

The base module (lab/signals/base.py):
```python
{(_SIGNALS / "base.py").read_text()}
```

A reference implementation (lab/signals/builtin.py):
```python
{(_SIGNALS / "builtin.py").read_text()}
```"""

CRITIC_SYSTEM = f"""\
You are the Critic in a scientific trading lab: an adversarial reviewer. You receive a
hypothesis and the code implementing it, which has already passed automatic safety and
behaviour checks. Your job is to stop bad science before compute is spent on it.

{ENVIRONMENT}

Check:
- Fidelity: does the code implement exactly the spec? Wrong formulas, off-by-one windows,
  inverted conditions, missing exits, extra unrequested filters.
- Correctness: edge cases (flat prices, zero volume, division by zero), lookback too short
  for the computation, state handling with self.last.
- Overfitting risk: too many parameters, oddly specific values, conditions that look
  reverse-engineered from known history.
Verdicts: "accept" (sound), "revise" (fixable, list precise issues for the engineer),
"reject" (the hypothesis itself is ill-posed or untestable here). Be specific and brief."""


def researcher_prompt(universe: list[str], train: tuple[str, str], past: list[dict]) -> str:
    past_txt = json.dumps(past, indent=1) if past else "none yet"
    return (
        f"Universe: {', '.join(universe)}\nTraining period: {train[0]} to {train[1]}\n\n"
        f"Hypotheses already tested (training-period results only):\n{past_txt}\n\n"
        "Propose the next hypothesis."
    )


def engineer_prompt(proposal: dict, previous_code: str | None = None, feedback: str | None = None) -> str:
    p = (
        f"Hypothesis id: {proposal['id']}\nTitle: {proposal['title']}\n"
        f"Hypothesis: {proposal['hypothesis']}\nSignal spec:\n{proposal['signal_spec']}\n"
    )
    if previous_code and feedback:
        p += (
            f"\nYour previous implementation:\n```python\n{previous_code}\n```\n"
            f"It was sent back with this feedback:\n{feedback}\n\nReturn a corrected, complete module."
        )
    else:
        p += "\nWrite the complete module."
    return p


def critic_prompt(proposal: dict, code: str, check_info: dict) -> str:
    return (
        f"Hypothesis:\n{json.dumps(proposal, indent=1)}\n\nImplementation:\n```python\n{code}\n```\n\n"
        f"Automatic behaviour check on 1,500 random-walk bars: {json.dumps(check_info)}\n\nReview it."
    )
