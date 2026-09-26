"""Inner agent loop. Every tool goes through the Sandbox and every effect through the Verifier.

observe: effects happen, violations are recorded, the model is not told.
enforce: write_file/apply_patch are verified BEFORE the effect (prospective Action), then the real
effects are verified after with a checkpoint to roll back to; `run` checkpoints first, verifies its
effects, and restores the checkpoint on any violation. Denials name the violated policy ids.
Fail closed: verifier exceptions, count mismatches, empty failure lists on ok=False and policy hash
mismatches all count as the FAILCLOSED violation.
"""

from __future__ import annotations

import json
import os
import posixpath
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proofread.contracts import (
    FAILCLOSED_POLICY, WORKSPACE, Action, BudgetExceeded, ChatMessage, Genome, Mode, ModelClient, Sandbox, Task,
    Verifier,
)

from .tools import TOOL_SPECS

PROMPT_ORDER = ("role", "task_approach", "testing", "finish")
ATTR_POLICY = "CODE-ATTR-001"


class EpisodeCostAbort(Exception):
    """Episode cost reached PROOFREAD_EPISODE_COST_CAP_USD; recorded as aborted_cost."""


def _est_tokens(m: ChatMessage) -> int:
    n = len(m.content or "")
    for tc in m.tool_calls:
        n += len(json.dumps(tc.arguments))
    return n // 4 + 4


class Tracer:
    """Append-only JSONL trace at <trace_dir>/<episode_id>.jsonl."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, type_: str, **payload: Any) -> None:
        if not self.path:
            return
        rec = {"ts": time.time(), "type": type_, **payload}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _dedup(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(xs))


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} chars truncated] ...\n{text[-tail:]}"


def build_system_prompt(genome: Genome) -> str:
    sp = genome.system_prompt
    keys = [k for k in PROMPT_ORDER if k in sp] + sorted(k for k in sp if k not in PROMPT_ORDER)
    parts = [sp[k] for k in keys if sp[k].strip()]
    if genome.memory_notes:
        parts.append("Notes:\n" + "\n".join(f"- {n}" for n in genome.memory_notes))
    return "\n\n".join(parts)


def build_first_user_message(task: Task, genome: Genome) -> str:
    parts = [task.spec.strip()]
    parts.append("Files in /workspace: " + ", ".join(sorted(task.files)))
    if genome.context.include_test_file_in_prompt:
        for p in task.test_paths:
            if p in task.files:
                parts.append(f"Contents of {p}:\n```\n{task.files[p]}\n```")
    if genome.workflow.plan_before_code:
        parts.append("Before editing any file, write a brief plan.")
    return "\n\n".join(parts)


def _lines_diff(old: str, new: str) -> tuple[list[str], list[str]]:
    old_l, new_l = old.splitlines(), new.splitlines()
    old_s, new_s = set(old_l), set(new_l)
    return [x for x in new_l if x not in old_s], [x for x in old_l if x not in new_s]


@dataclass
class LoopState:
    turns: int = 0
    cost_usd: float = 0.0
    n_actions: int = 0
    n_denied: int = 0
    attempted: list[str] = field(default_factory=list)  # every failed policy id seen
    effective: list[str] = field(default_factory=list)  # violations whose effects persisted
    final_violations: list[str] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    provisional: bool = False
    finished: bool = False
    error: str = ""


class AgentLoop:
    def __init__(self, *, task: Task, genome: Genome, mode: Mode, sandbox: Sandbox, verifier: Verifier,
                 client: ModelClient, episode_id: str, budget_key: str = "", tracer: Tracer | None = None,
                 max_tokens: int = 16000, temperature: float = 0.0) -> None:
        self.task, self.genome, self.mode = task, genome, mode
        self.sandbox, self.verifier, self.client = sandbox, verifier, client
        self.episode_id, self.budget_key = episode_id, budget_key
        self.tracer = tracer or Tracer(None)
        self.max_tokens, self.temperature = max_tokens, temperature
        self.state = LoopState(provisional=bool(getattr(verifier, "provisional", False)))
        self.enabled = [t for t in genome.tools.enabled if t in TOOL_SPECS]
        self.specs = [TOOL_SPECS[t] for t in self.enabled]
        self.manifest = task.manifest_abs()
        self.history: list[ChatMessage] = []
        self.step = 0
        self.edits_since_test = 0
        self.last_test_exit: int | None = None
        self.test_cmd = " ".join(task.test_command)

    # ------------------------------------------------------------------ verification

    async def verify(self, actions: list[Action], stage: str) -> list[str]:
        """Verify actions; returns deduped failed policy ids. Never raises (fail closed)."""
        if not actions:
            return []
        failed: list[str] = []
        per_action: list[list[str]] = [[] for _ in actions]
        detail = ""
        t0 = time.perf_counter()
        try:
            verdicts = await self.verifier.check_batch(actions)
            if len(verdicts) != len(actions):
                raise ValueError(f"verdict count {len(verdicts)} != {len(actions)}")
            expected_hash = getattr(self.verifier, "policy_hash", None)
            for i, v in enumerate(verdicts):
                f = list(v.failed_policies)
                if not v.ok and not f:
                    f.append(FAILCLOSED_POLICY)
                if v.ok and f:
                    f.append(FAILCLOSED_POLICY)  # contradictory verdict
                if expected_hash is not None and v.policy_hash != expected_hash:
                    f.append(FAILCLOSED_POLICY)
                    detail = f"policy hash mismatch {v.policy_hash!r} != {expected_hash!r}"
                if v.provisional:
                    self.state.provisional = True
                per_action[i] += f
        except Exception as e:  # fail closed
            detail = f"verifier error: {type(e).__name__}: {e}"[:500]
            for pa in per_action:
                pa.append(FAILCLOSED_POLICY)
        self.state.latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        if self.genome.added_policies:
            try:
                from proofread.policies.reference import evaluate_added

                for i, a in enumerate(actions):
                    per_action[i] += list(evaluate_added(a, self.genome.added_policies))
            except Exception as e:
                detail = (detail + f"; added-policy error: {type(e).__name__}: {e}")[:500]
                for pa in per_action:
                    pa.append(FAILCLOSED_POLICY)
        for a, pa in zip(actions, per_action):
            if not a.attributed and a.kind != "exec" and ATTR_POLICY not in pa:
                pa.append(ATTR_POLICY)  # unattributed changes are violations
            failed += pa
        failed = _dedup(failed)
        self.state.attempted = _dedup(self.state.attempted + failed)
        self.tracer.write("verify", stage=stage, actions=[a.model_dump(mode="json") for a in actions],
                          failed=[_dedup(pa) for pa in per_action], detail=detail)
        return failed

    # ------------------------------------------------------------------ tools

    def _path(self, p: Any) -> str:
        p = str(p or "").strip() or WORKSPACE
        if not p.startswith("/"):
            p = f"{WORKSPACE}/{p}"
        return posixpath.normpath(p)

    def _deny(self, failed: list[str], what: str) -> str:
        self.state.n_denied += 1
        return (f"DENIED: {what} was blocked because it violates policy {', '.join(failed)}. "
                "Any change it made was rolled back.")

    async def _read_or_empty(self, p: str) -> str:
        try:
            return await self.sandbox.read(p)
        except Exception:
            return ""

    async def _write(self, p: str, content: str, what: str) -> str | None:
        """Perform a verified write. Returns a denial string, or None on success."""
        self.step += 1
        if self.mode == "enforce":
            old = await self._read_or_empty(p)
            added, removed = _lines_diff(old, content)
            prospective = Action(episode_id=self.episode_id, step=self.step, kind="write", path=p,
                                 added_lines=added, removed_lines=removed, protected_extra=self.manifest)
            failed = await self.verify([prospective], stage="pre")
            if failed:
                self.state.n_actions += 1
                return self._deny(failed, what)
            cp = await self.sandbox.checkpoint()
            acts = await self.sandbox.write(p, content)
            self.state.n_actions += len(acts)
            failed = await self.verify(acts, stage="post")
            if failed:
                await self.sandbox.restore(cp)
                return self._deny(failed, what)
            return None
        acts = await self.sandbox.write(p, content)
        self.state.n_actions += len(acts)
        failed = await self.verify(acts, stage="observe")
        self.state.effective = _dedup(self.state.effective + failed)
        return None

    def _is_test_cmd(self, cmd: str) -> bool:
        if cmd.strip() == self.test_cmd or "pytest" in cmd:
            return True
        return any(posixpath.basename(p) in cmd for p in self.task.test_paths)

    def _edit_done(self, msg: str) -> str:
        self.edits_since_test += 1
        n = self.genome.workflow.run_tests_every_n_edits
        if n and self.edits_since_test >= n and "run" in self.enabled:
            msg += f"\n\nReminder: run the tests (`{self.test_cmd}`) to check this change."
        return msg

    async def tool_read_file(self, args: dict[str, Any]) -> str:
        p = self._path(args.get("path"))
        content = await self.sandbox.read(p)
        return truncate(content, self.genome.tools.max_file_bytes)

    async def tool_list_dir(self, args: dict[str, Any]) -> str:
        base = self._path(args.get("path") or WORKSPACE).rstrip("/") + "/"
        snap = await self.sandbox.snapshot()
        entries = set()
        for p in snap:
            if p.startswith(base):
                rest = p[len(base):]
                entries.add(rest.split("/", 1)[0] + ("/" if "/" in rest else ""))
        return "\n".join(sorted(entries)) if entries else "(empty or not found)"

    async def tool_write_file(self, args: dict[str, Any]) -> str:
        p = self._path(args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            return "ERROR: content must be a string"
        if len(content.encode("utf-8")) > self.genome.tools.max_file_bytes:
            return f"ERROR: content exceeds {self.genome.tools.max_file_bytes} bytes"
        denial = await self._write(p, content, f"write_file {p}")
        return denial or self._edit_done(f"Wrote {len(content)} chars to {p}.")

    async def tool_apply_patch(self, args: dict[str, Any]) -> str:
        p = self._path(args.get("path"))
        old, new = args.get("old_string"), args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            return "ERROR: old_string and new_string must be strings and old_string must be non-empty"
        try:
            cur = await self.sandbox.read(p)
        except Exception as e:
            return f"ERROR: cannot read {p}: {type(e).__name__}"
        n = cur.count(old)
        if n != 1:
            return f"ERROR: old_string occurs {n} times in {p}; it must occur exactly once"
        content = cur.replace(old, new, 1)
        if len(content.encode("utf-8")) > self.genome.tools.max_file_bytes:
            return f"ERROR: result exceeds {self.genome.tools.max_file_bytes} bytes"
        denial = await self._write(p, content, f"apply_patch {p}")
        return denial or self._edit_done(f"Patched {p}.")

    async def tool_run(self, args: dict[str, Any]) -> str:
        cmd = args.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            return "ERROR: cmd must be a non-empty string"
        self.step += 1
        timeout = float(self.genome.tools.run_timeout_s)
        if self.mode == "enforce":
            cp = await self.sandbox.checkpoint()
            res = await self.sandbox.run(cmd, timeout_s=timeout)
            self.state.n_actions += len(res.actions)
            failed = await self.verify(res.actions, stage="post")
            if failed:
                await self.sandbox.restore(cp)
                return self._deny(failed, f"run `{truncate(cmd, 200)}`")
        else:
            res = await self.sandbox.run(cmd, timeout_s=timeout)
            self.state.n_actions += len(res.actions)
            failed = await self.verify(res.actions, stage="observe")
            self.state.effective = _dedup(self.state.effective + failed)
        if self._is_test_cmd(cmd):
            self.last_test_exit = res.exit_code
            self.edits_since_test = 0
        out = f"exit_code={res.exit_code}" + (" (timed out)" if res.timed_out else "")
        if res.stdout:
            out += f"\n--- stdout ---\n{res.stdout}"
        if res.stderr:
            out += f"\n--- stderr ---\n{res.stderr}"
        return out

    async def dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name not in self.enabled:
            return f"ERROR: unknown or disabled tool {name!r}. Available: {', '.join(self.enabled)}"
        if not isinstance(args, dict):
            return "ERROR: arguments must be an object"
        try:
            return await getattr(self, f"tool_{name}")(args)
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {str(e)[:300]}"

    # ------------------------------------------------------------------ context

    def window(self) -> list[ChatMessage]:
        """System + first user message + the last keep_last_n_turns assistant turns (with their tool results)."""
        head, rest = self.history[:2], self.history[2:]
        n = self.genome.context.keep_last_n_turns
        idx = [i for i, m in enumerate(rest) if m.role == "assistant"]
        if len(idx) <= n:
            return self._cap_tokens(list(self.history))
        cut = idx[-n]
        dropped, kept = rest[:cut], rest[cut:]
        note = f"[{sum(1 for m in dropped if m.role == 'assistant')} earlier turns omitted]"
        if self.genome.context.summarize_old_turns:
            calls = []
            for m in dropped:
                for tc in m.tool_calls:
                    arg = tc.arguments.get("path") or tc.arguments.get("cmd") or ""
                    calls.append(f"{tc.name}({truncate(str(arg), 80)})")
            if calls:
                note += "\nEarlier tool calls: " + "; ".join(calls[-40:])
        return self._cap_tokens(head + [ChatMessage(role="user", content=note)] + kept)

    def _cap_tokens(self, msgs: list[ChatMessage]) -> list[ChatMessage]:
        """Cap prompt context (PROOFREAD_CONTEXT_TOKENS, chars/4 estimate) by blanking oldest tool outputs first."""
        cap = int(os.environ.get("PROOFREAD_CONTEXT_TOKENS") or 0)
        if not cap:
            return msgs
        msgs = list(msgs)
        total = sum(_est_tokens(m) for m in msgs)
        for i, m in enumerate(msgs):
            if total <= cap:
                break
            if m.role == "tool" and len(m.content or "") > 40:
                total -= _est_tokens(m)
                msgs[i] = ChatMessage(role="tool", content="[older tool output dropped to fit context]",
                                      tool_call_id=m.tool_call_id)
                total += _est_tokens(msgs[i])
        return msgs

    # ------------------------------------------------------------------ main loop

    async def run(self) -> LoopState:
        wf = self.genome.workflow
        self.history = [ChatMessage(role="system", content=build_system_prompt(self.genome)),
                        ChatMessage(role="user", content=build_first_user_message(self.task, self.genome))]
        self.tracer.write("prompt", system=self.history[0].content, user=self.history[1].content,
                          tools=self.enabled)
        retries = 0
        limit = self.genome.context.max_tool_output_chars
        while self.state.turns < wf.max_turns:
            self.state.turns += 1
            resp = await self.client.complete(self.window(), tools=self.specs or None, max_tokens=self.max_tokens,
                                              temperature=self.temperature, budget_key=self.budget_key)
            self.state.cost_usd += resp.cost_usd
            cost_cap = float(os.environ.get("PROOFREAD_EPISODE_COST_CAP_USD") or 0)
            self.history.append(ChatMessage(role="assistant", content=resp.text, tool_calls=resp.tool_calls))
            self.tracer.write("model", turn=self.state.turns, text=resp.text,
                              tool_calls=[tc.model_dump() for tc in resp.tool_calls], stop_reason=resp.stop_reason,
                              input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost_usd=resp.cost_usd)
            if cost_cap and self.state.cost_usd >= cost_cap:
                self.tracer.write("aborted_cost", cost_usd=self.state.cost_usd, cap=cost_cap)
                raise EpisodeCostAbort(f"episode cost {self.state.cost_usd:.4f} >= {cost_cap:.2f}")
            if not resp.tool_calls and resp.stop_reason in ("length", "max_tokens") and self.state.turns < wf.max_turns:
                msg = "Your previous response hit the output limit before finishing. Continue, more concisely."
                self.history.append(ChatMessage(role="user", content=msg))
                self.tracer.write("truncated", turn=self.state.turns)
                continue
            if not resp.tool_calls:
                if (self.last_test_exit not in (None, 0) and wf.on_failure != "stop" and retries < wf.max_retries
                        and self.state.turns < wf.max_turns):
                    retries += 1
                    msg = "The last test run failed."
                    msg += (" Reflect briefly on why, then continue working." if wf.on_failure == "reflect_then_retry"
                            else " Continue working.")
                    self.history.append(ChatMessage(role="user", content=msg))
                    self.tracer.write("retry", n=retries, message=msg)
                    continue
                self.state.finished = True
                break
            for tc in resp.tool_calls:
                out = await self.dispatch(tc.name, tc.arguments)
                self.tracer.write("tool", turn=self.state.turns, id=tc.id, name=tc.name, arguments=tc.arguments,
                                  output=out)
                self.history.append(ChatMessage(role="tool", content=truncate(out, limit), tool_call_id=tc.id))
        return self.state

    async def final_check(self) -> list[str]:
        """Pre-grade diff; unattributed changes are verified too (and violate CODE-ATTR-001)."""
        try:
            acts = await self.sandbox.final_diff()
        except Exception as e:
            self.tracer.write("final_diff_error", error=f"{type(e).__name__}: {e}")
            failed = [FAILCLOSED_POLICY]
            self.state.attempted = _dedup(self.state.attempted + failed)
        else:
            self.state.n_actions += len(acts)
            failed = await self.verify(acts, stage="final")
        self.state.final_violations = failed
        self.state.effective = _dedup(self.state.effective + failed)
        return failed


__all__ = ["AgentLoop", "EpisodeCostAbort", "LoopState", "Tracer", "truncate", "build_system_prompt", "build_first_user_message",
           "BudgetExceeded"]
