"""ClaudeCliClient: proposer calls through the local `claude` CLI (decision D-010, BLOCKED B-001).

Used when no proposer workspace ID is configured. Runs
  claude -p --model <model> --output-format json --strict-mcp-config --tools ""
with the flattened conversation on STDIN, cwd /tmp/proposer_cwd, and the parent environment minus
ANTHROPIC_API_KEY (and minus any proofread key variables). No keys from .env are passed. Tools are
not supported. Spend is checked and recorded in the SpendLedger like the HTTP clients.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from proofread.contracts import ChatMessage, ModelResponse, Role, ToolSpec

from .ledger import SpendLedger, default_ledger

PROPOSER_CWD = Path("/tmp/proposer_cwd")
_STRIP_ENV = ("ANTHROPIC_API_KEY", "PROPOSER_API_KEY", "AGENT_API_KEY", "BASELINE_API_KEY", "OPENROUTER_API_KEY")


class ClaudeCliError(RuntimeError):
    pass


def flatten(messages: list[ChatMessage]) -> str:
    parts: list[str] = []
    for m in messages:
        if m.role == "system":
            parts.append(f"<system>\n{m.content}\n</system>")
        elif m.role == "user":
            parts.append(f"<user>\n{m.content}\n</user>")
        elif m.role == "assistant":
            parts.append(f"<assistant>\n{m.content}\n</assistant>")
        else:
            parts.append(f"<tool_result id={m.tool_call_id!r}>\n{m.content}\n</tool_result>")
    if len(messages) == 1 and messages[0].role == "user":
        return messages[0].content
    return "\n\n".join(parts) + "\n\nRespond as the assistant to the last user message."


class ClaudeCliClient:
    def __init__(self, role: Role, model: str, *, ledger: SpendLedger | None = None, timeout_s: float = 600.0,
                 retries: int = 2, command: list[str] | None = None, cwd: str | Path = PROPOSER_CWD,
                 episode_id: str = "") -> None:
        self.role = role
        self.model = model
        self.ledger = ledger or default_ledger()
        self.timeout_s = timeout_s
        self.retries = retries
        self.command = command or ["claude", "-p", "--model", model, "--output-format", "json",
                                   "--strict-mcp-config", "--tools", ""]
        self.cwd = Path(cwd)
        self.episode_id = episode_id
        self.spent_usd = 0.0

    def __repr__(self) -> str:
        return f"ClaudeCliClient(role={self.role!r}, model={self.model!r})"

    @staticmethod
    def child_env() -> dict[str, str]:
        env = dict(os.environ)
        for k in _STRIP_ENV:
            env.pop(k, None)
        return env

    async def _once(self, prompt: str) -> dict[str, Any]:
        self.cwd.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(self.cwd), env=self.child_env())
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), self.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ClaudeCliError(f"claude CLI timed out after {self.timeout_s}s")
        if proc.returncode != 0:
            raise ClaudeCliError(f"claude CLI exit {proc.returncode}: {err.decode(errors='replace')[-500:]}")
        try:
            data = json.loads(out.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ClaudeCliError(f"claude CLI returned non-JSON: {out[:300]!r}") from e
        if not isinstance(data, dict) or data.get("is_error") or "result" not in data:
            raise ClaudeCliError(f"claude CLI error result: {str(data)[:500]}")
        return data

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None, max_tokens: int = 4096,
                       temperature: float = 0.0, budget_key: str = "") -> ModelResponse:
        if tools:
            raise ClaudeCliError("ClaudeCliClient does not support tools")
        self.ledger.check(budget_key)
        prompt = flatten(messages)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                data = await self._once(prompt)
                break
            except ClaudeCliError as e:
                last = e
                if attempt < self.retries:
                    await asyncio.sleep(2 ** attempt)
        else:
            raise last  # type: ignore[misc]
        u = data.get("usage") or {}
        itok = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(
            u.get("cache_creation_input_tokens") or 0)
        resp = ModelResponse(text=str(data.get("result") or ""), input_tokens=itok,
                             output_tokens=int(u.get("output_tokens") or 0),
                             cost_usd=float(data.get("total_cost_usd") or 0.0), model=self.model,
                             stop_reason=str(data.get("stop_reason") or ""))
        self.spent_usd += resp.cost_usd
        self.ledger.record(role=self.role, model=self.model, budget_key=budget_key, input_tokens=resp.input_tokens,
                           output_tokens=resp.output_tokens, cost_usd=resp.cost_usd, episode_id=self.episode_id)
        return resp
