"""
Builds Lean 4 conjectures dynamically from the policy registry and calls the lean-worker.

Conjecture shape for a policy entry (one goal; the worker adds `example :` and `:= by decide +kernel`):
  positional:  {ns}.{lean_function} {arg0} {arg1} ... = true
  structure:   {ns}.{lean_function} {pos0} ... ({ f0 := v0, f1 := v1, ... } : {structure_type}) = true

Args are extracted from the ToolCall params dict using parameter_map (ordered) and rendered by the
typed transforms in render.py.

A policy with a `chunk` spec is split into several goals: the union of the list fields named in
`from` is deduplicated and cut into chunks of at most `max_chars` characters, and each chunk becomes
one goal `{ <keep fields>, <into> := chunk }`. That split is only used where a Lean lemma proves it
equivalent to the whole (for CODE-SKIP-001: Proofread.codeSkip001C_chunks). Chunks are sent to the
worker in separate batches, so a large write is checked by several Lean processes in parallel.

Verdict for a tool call:
  proved   every goal of every applicable policy proved
  refuted  some goals refuted, none errored; failed_policies = policies with a refuted goal
  error    any goal errored, a param was missing or malformed, the worker was unreachable or served
           a different source hash (callers treat this as a violation: fail closed)
  skipped  no policy covers the tool (or every applicable policy is on_missing="skip" and skipped)

Upstream checked policies one at a time (one `lean` process per policy) and stopped at the first
refutation; here all goals are checked so that failed_policies is complete.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import httpx

from .policy_registry import get_policies_for_tool
from .render import MAX_CHARS, RenderError, lean_chars_list, render, size_of, str_list

BATCH_RENDERED_CHARS = 20_000  # goals per worker request, by rendered size
HEALTH_TTL_S = 10.0
MAX_CONJECTURE_ECHO = 4000  # the full text can be megabytes; responses carry a prefix and a sha256


class _Skip(Exception):
    pass


def _arg(policy: dict[str, Any], name: str, key: str, params: dict[str, Any]) -> str:
    if key not in params:
        if policy.get("on_missing", "skip") == "error":
            raise RenderError(f"{policy['policy_id']}: missing param {key!r}")
        raise _Skip()
    return render(params[key], policy.get("param_transforms", {}).get(name, "int"),
                  enum=policy.get("param_enums", {}).get(name), what=f"{policy['policy_id']}.{name}")


def _fn(policy: dict[str, Any]) -> str:
    return f"{policy.get('lean_namespace', 'PolicyEnv')}.{policy['lean_function']}"


def _struct(policy: dict[str, Any], fields: dict[str, str]) -> str:
    body = ", ".join(f"{k} := {v}" for k, v in fields.items())
    return f"({{ {body} }} : {policy['structure_type']})"


def build_goals(policy: dict[str, Any], params: dict[str, Any]) -> list[str]:
    """Lean propositions for one policy. Raises RenderError (fail closed) or _Skip."""
    if policy.get("lean_arg_style", "positional") == "positional":
        args = [_arg(policy, n, k, params) for n, k in policy["parameter_map"].items()]
        return [f"{_fn(policy)} {' '.join(args)} = true"]

    head = " ".join([_fn(policy)] + [_arg(policy, n, k, params) for n, k in policy.get("positional_map", {}).items()])
    chunk = policy.get("chunk")
    if not chunk:
        fields = {n: _arg(policy, n, k, params) for n, k in policy["parameter_map"].items()}
        return [f"{head} {_struct(policy, fields)} = true"]

    keep = {n: _arg(policy, n, policy["parameter_map"][n], params) for n in chunk["keep"]}
    lines: dict[str, None] = {}  # ordered set: union of the chunked fields, deduplicated
    for n in chunk["from"]:
        key = policy["parameter_map"][n]
        if key not in params:
            if policy.get("on_missing", "skip") == "error":
                raise RenderError(f"{policy['policy_id']}: missing param {key!r}")
            raise _Skip()
        for x in str_list(params[key], f"{policy['policy_id']}.{n}"):
            lines.setdefault(x, None)
    chunks: list[list[str]] = [[]]
    size = 0
    for x in lines:
        if chunks[-1] and size + len(x) > chunk["max_chars"]:
            chunks.append([])
            size = 0
        chunks[-1].append(x)
        size += len(x) + 1
    return [f"{head} {_struct(policy, {**keep, chunk['into']: lean_chars_list(c)})} = true" for c in chunks]


def _batches(goals: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    out: list[list[dict[str, str]]] = []
    size = 0
    for g in sorted(goals, key=lambda g: len(g["prop"])):
        if not out or size + len(g["prop"]) > BATCH_RENDERED_CHARS or len(out[-1]) >= 256:
            out.append([])
            size = 0
        out[-1].append(g)
        size += len(g["prop"])
    return out


class WorkerHealth:
    """Cached lean-worker health; source_hash identifies the compiled policy sources."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.at = 0.0

    async def get(self, client: httpx.AsyncClient, lean_worker_url: str, fresh: bool = False) -> dict[str, Any]:
        if fresh or time.monotonic() - self.at > HEALTH_TTL_S:
            try:
                r = await client.get(f"{lean_worker_url}/health", timeout=5.0)
                self.data = r.json() if r.status_code == 200 else {"status": f"HTTP {r.status_code}"}
            except Exception as exc:  # noqa: BLE001
                self.data = {"status": "unreachable", "error": str(exc)}
            self.at = time.monotonic()
        return self.data

    def source_hash(self) -> str:
        return self.data.get("source_hash", "") if self.data.get("status") == "ok" else ""


health = WorkerHealth()


def _result(result: str, **kw: Any) -> dict[str, Any]:
    base = {"result": result, "trace": "", "latency_us": 0, "policy_id": "NONE", "conjecture": "",
            "explanation": "", "elab_us": None, "failed_policies": [], "policy_hash": "", "reject_code": "",
            "goals": 0, "conjecture_sha256": ""}
    base.update(kw)
    return base


async def verify(tool_name: str, params: dict[str, Any], lean_worker_url: str,
                 client: httpx.AsyncClient) -> dict[str, Any]:
    """Check ALL applicable policies for a tool call (see module docstring for the verdicts)."""
    t0 = time.perf_counter()
    policies = get_policies_for_tool(tool_name)
    if not policies:
        return _result("skipped", explanation=f"No policies registered for tool: {tool_name}")

    h = await health.get(client, lean_worker_url)
    served = health.source_hash()
    if not served:
        return _result("error", reject_code="worker_unavailable",
                       explanation=f"lean-worker not ready: {h.get('status')} {h.get('detail') or h.get('error') or ''}")
    if "policy_hash" in params and params["policy_hash"] != served:
        return _result("error", reject_code="policy_hash_mismatch", policy_hash=served,
                       explanation="request pinned a different policy source hash than the kernel serves")
    if size_of(params) > MAX_CHARS:
        return _result("error", reject_code="too_large", policy_hash=served, explanation="params too large")

    goals: list[dict[str, str]] = []
    owner: dict[str, str] = {}
    for policy in policies:
        try:
            props = build_goals(policy, params)
        except _Skip:
            continue
        except (RenderError, KeyError, ValueError, TypeError) as exc:
            return _result("error", reject_code="bad_params", policy_hash=served, policy_id=policy["policy_id"],
                           explanation=str(exc)[:500])
        for j, prop in enumerate(props):
            gid = f"{policy['policy_id']}#{j}"
            goals.append({"id": gid, "prop": prop})
            owner[gid] = policy["policy_id"]
    if not goals:
        return _result("skipped", explanation=f"No applicable policy parameters found for tool: {tool_name}")

    async def run(batch: list[dict[str, str]]) -> dict[str, Any]:
        r = await client.post(f"{lean_worker_url}/verify-batch", json={"goals": batch}, timeout=180.0)
        if r.status_code != 200:
            raise RuntimeError(f"lean-worker HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    try:
        replies = await asyncio.gather(*(run(b) for b in _batches(goals)))
    except Exception as exc:  # noqa: BLE001
        return _result("error", reject_code="worker_error", policy_hash=served, explanation=str(exc)[:500])

    by_id: dict[str, dict[str, Any]] = {}
    worker_us = 0
    for rep in replies:
        if rep.get("source_hash") != served:
            return _result("error", reject_code="policy_hash_mismatch", policy_hash=served,
                           explanation="lean-worker source hash changed during the request")
        worker_us += int(rep.get("latency_us", 0))
        for res in rep.get("results", []):
            by_id[res.get("id")] = res
    rank = {"proved": 0, "refuted": 1, "error": 2}
    status: dict[str, str] = {}  # policy → worst goal result
    first_trace: dict[str, str] = {}
    for g in goals:
        res = by_id.get(g["id"], {"result": "error", "trace": "no result for goal"})
        pid, r = owner[g["id"]], res.get("result")
        if r not in rank:
            r = "error"
        if pid not in status or rank[r] > rank[status[pid]]:
            status[pid] = r
            first_trace[pid] = str(res.get("trace", ""))

    latency_us = int((time.perf_counter() - t0) * 1e6)
    conjecture = "\n".join(g["prop"] for g in goals)
    conjecture_sha256 = hashlib.sha256(conjecture.encode("utf-8")).hexdigest()
    errored = sorted(p for p, s in status.items() if s == "error")
    refuted = sorted(p for p, s in status.items() if s == "refuted")
    common = dict(latency_us=latency_us, elab_us=worker_us, policy_hash=served, goals=len(goals),
                  conjecture=conjecture[:MAX_CONJECTURE_ECHO], conjecture_sha256=conjecture_sha256)
    if errored:
        return _result("error", reject_code="kernel_error", policy_id=", ".join(errored),
                       trace=first_trace[errored[0]], explanation="Lean could not decide: " + ", ".join(errored),
                       **common)
    if refuted:
        return _result("refuted", policy_id=", ".join(refuted), failed_policies=refuted,
                       trace=first_trace[refuted[0]], explanation="Violates " + ", ".join(refuted), **common)
    return _result("proved", policy_id=", ".join(sorted(status)), **common)
