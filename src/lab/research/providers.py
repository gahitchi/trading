"""
Language-model providers for the research swarm. Each one turns
(system prompt, user prompt, JSON schema) into a JSON object matching the schema.

- ClaudeCodeProvider: runs the Claude Code CLI headless (`claude -p`), which uses
  the Claude subscription you are logged in with. Usage counts against your plan's
  normal limits. If ANTHROPIC_API_KEY is set, Claude Code bills that key instead.
- OllamaProvider: a local model served by Ollama (free, runs on your GPU).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import jsonschema


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class CallRecord:
    role: str
    provider: str
    model: str
    seconds: float
    ok: bool
    cost_estimate_usd: float | None = None
    error: str = ""


class Provider:
    name = "base"

    def __init__(self, model: str, timeout_s: int, usage_log: Path | None = None):
        self.model = model
        self.timeout_s = timeout_s
        self.usage_log = usage_log
        self.calls: list[CallRecord] = []

    def complete_json(self, *, role: str, system: str, prompt: str, schema: dict) -> dict:
        t0 = time.monotonic()
        rec = CallRecord(role, self.name, self.model, 0.0, False)
        try:
            out, rec.cost_estimate_usd = self._complete(system, prompt, schema)
            jsonschema.validate(out, schema)
            rec.ok = True
            return out
        except jsonschema.ValidationError as e:
            rec.error = f"output did not match schema: {e.message}"
            raise LLMError(rec.error) from e
        except LLMError as e:
            rec.error = str(e)
            raise
        finally:
            rec.seconds = round(time.monotonic() - t0, 2)
            self.calls.append(rec)
            if self.usage_log:
                self.usage_log.parent.mkdir(parents=True, exist_ok=True)
                with self.usage_log.open("a") as f:
                    f.write(json.dumps({**rec.__dict__, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")

    def _complete(self, system: str, prompt: str, schema: dict) -> tuple[dict, float | None]:
        raise NotImplementedError


class ClaudeCodeProvider(Provider):
    name = "claude_code"

    def __init__(
        self, model: str, timeout_s: int, workdir: Path, usage_log: Path | None = None, binary: str | None = None
    ):
        super().__init__(model, timeout_s, usage_log)
        self.binary = binary or shutil.which("claude")
        if not self.binary:
            raise LLMError(
                "Claude Code CLI not found. Install it (https://code.claude.com/docs/en/setup) "
                "and run `claude` once to log in with your subscription."
            )
        self.workdir = workdir

    def _complete(self, system, prompt, schema):
        # An empty, dedicated working directory: no project files, CLAUDE.md or MCP servers
        # leak into the agents, and no tools are enabled, so they can only answer.
        self.workdir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--system-prompt",
            system,
            "--model",
            self.model,
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=self.workdir,
                timeout=self.timeout_s,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude timed out after {self.timeout_s}s") from e
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}") from e
        if payload.get("is_error") or proc.returncode != 0:
            raise LLMError(f"claude reported an error: {str(payload.get('result'))[:800]}")
        out = payload.get("structured_output")
        if out is None:
            raise LLMError(f"claude returned no structured output: {str(payload.get('result'))[:800]}")
        return out, payload.get("total_cost_usd")


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, model: str, timeout_s: int, url: str, usage_log: Path | None = None):
        super().__init__(model, timeout_s, usage_log)
        self.url = url.rstrip("/")

    def _complete(self, system, prompt, schema):
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "format": schema,  # Ollama constrains decoding to this JSON schema
                "stream": False,
                "options": {"temperature": 0.2},
            }
        ).encode()
        req = urllib.request.Request(f"{self.url}/api/chat", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                payload = json.loads(r.read())
        except urllib.error.URLError as e:
            raise LLMError(
                f"cannot reach Ollama at {self.url} ({e.reason}). Is `ollama serve` running "
                f"and the model pulled (`ollama pull {self.model}`)?"
            ) from e
        except TimeoutError as e:
            raise LLMError(f"Ollama timed out after {self.timeout_s}s") from e
        if "error" in payload:
            raise LLMError(f"Ollama error: {payload['error']}")
        content = payload.get("message", {}).get("content", "")
        try:
            return json.loads(content), None
        except json.JSONDecodeError as e:
            raise LLMError(f"Ollama returned invalid JSON: {content[:300]}") from e


class Budget:
    """Hard cap on model calls per research cycle."""

    def __init__(self, provider: Provider, max_calls: int):
        self.provider = provider
        self.max_calls = max_calls
        self.used = 0

    @property
    def left(self) -> int:
        return self.max_calls - self.used

    def call(self, **kw) -> dict:
        if self.used >= self.max_calls:
            raise BudgetExceeded(f"model-call budget of {self.max_calls} for this cycle is used up")
        self.used += 1
        return self.provider.complete_json(**kw)


def make_provider(settings, root: Path) -> Provider:
    usage = root / "research" / "usage.jsonl"
    if settings.provider == "claude_code":
        return ClaudeCodeProvider(
            settings.model, settings.call_timeout_s, root / "research" / ".agent_workspace", usage
        )
    if settings.provider == "ollama":
        return OllamaProvider(settings.model, settings.call_timeout_s, settings.ollama_url, usage)
    raise LLMError(f"unknown provider {settings.provider!r}")
