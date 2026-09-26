"""Static validation of proposed genome edits (the first gate).

1. The patch is a list of RFC 6902 ops touching only the editable surface.
2. It applies (W4's proofread.genome.patch.apply_patch if importable, else a local jsonpatch fallback)
   and the result validates against contracts.Genome (extra fields forbidden, so tool implementations,
   interceptor, verifier, graders, sandbox config and base policies are unreachable by schema).
3. Trust-boundary rules the schema cannot express: tools only narrow; added_policies append-only with
   fresh ids and compilable patterns; version is owned by the orchestrator.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import jsonpatch

from proofread.contracts import BASE_POLICY_IDS, Genome

EDITABLE_ROOTS = ("system_prompt", "workflow", "context", "tools", "memory_notes", "added_policies")
MAX_PROMPT_SECTION_CHARS = 4000
MAX_PROMPT_SECTIONS = 12
MAX_MEMORY_NOTES = 20
MAX_NOTE_CHARS = 1000
MAX_ADDED_POLICIES = 20
MAX_OPS = 30

SURFACE_DESCRIPTION = f"""Editable surface of the harness genome (JSON; edit it with RFC 6902 JSON Patch ops):
- /system_prompt/<section>: strings, the inner agent's system prompt sections (add, replace, or remove
  sections; at most {MAX_PROMPT_SECTIONS} sections of at most {MAX_PROMPT_SECTION_CHARS} chars each).
- /workflow: plan_before_code (bool), run_tests_every_n_edits (0..20), max_turns (1..60),
  on_failure ("retry" | "reflect_then_retry" | "stop"), max_retries (0..5).
- /context: max_tool_output_chars (200..20000), keep_last_n_turns (2..100),
  include_test_file_in_prompt (bool), summarize_old_turns (bool).
- /tools: tools can only be disabled or narrowed, never added or widened. enabled is a subset of the
  current list; run_timeout_s (5..60) and max_file_bytes (1000..200000) may only decrease.
- /memory_notes: list of short strings shown to the agent (at most {MAX_MEMORY_NOTES}, {MAX_NOTE_CHARS} chars each).
- /added_policies: append-only list of extra guardrails {{id: "ADD-...", description, kind, pattern}},
  kind in deny_path_glob | deny_added_line_regex | deny_exec_regex. Existing entries cannot be
  changed or removed; ids must be new and not base policy ids.
- /version is managed by the system and must not be edited. No other fields exist."""


class PatchError(ValueError):
    pass


def _local_apply(genome: Genome, ops: list[dict[str, Any]]) -> Genome:
    doc = genome.model_dump(mode="json")
    try:
        new = jsonpatch.JsonPatch(copy.deepcopy(ops)).apply(doc)
    except Exception as e:  # jsonpatch raises several exception types
        raise PatchError(f"patch does not apply: {e}") from e
    try:
        return Genome.model_validate(new)
    except Exception as e:
        raise PatchError(f"schema invalid: {e}") from e


def apply_ops(genome: Genome, ops: list[dict[str, Any]]) -> Genome:
    try:
        from proofread.genome.patch import apply_patch as w4_apply  # type: ignore
    except Exception:
        w4_apply = None
    if w4_apply is None:
        return _local_apply(genome, ops)
    try:
        g = w4_apply(genome, ops)
    except Exception as e:
        raise PatchError(f"patch rejected: {e}") from e
    return g if isinstance(g, Genome) else Genome.model_validate(g if isinstance(g, dict) else g.model_dump())


def _check_ops_shape(ops: Any) -> None:
    if not isinstance(ops, list) or not ops:
        raise PatchError("patch must be a non-empty list of ops")
    if len(ops) > MAX_OPS:
        raise PatchError(f"too many ops ({len(ops)} > {MAX_OPS})")
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in ("add", "remove", "replace", "move", "copy", "test"):
            raise PatchError(f"bad op: {op!r}")
        for key in ("path", "from"):
            if key not in op:
                continue
            p = op[key]
            if not isinstance(p, str) or not p.startswith("/"):
                raise PatchError(f"bad pointer {p!r}")
            root = p.split("/")[1]
            if root not in EDITABLE_ROOTS:
                raise PatchError(f"path {p!r} is outside the editable surface")
            if p == f"/{root}" and op["op"] == "remove":
                raise PatchError(f"cannot remove {p}")


def check_surface(old: Genome, new: Genome) -> None:
    # tools only narrow
    if not set(new.tools.enabled) <= set(old.tools.enabled):
        raise PatchError("tools.enabled may only shrink")
    if not new.tools.enabled:
        raise PatchError("tools.enabled cannot be empty")
    if new.tools.run_timeout_s > old.tools.run_timeout_s:
        raise PatchError("tools.run_timeout_s may only decrease")
    if new.tools.max_file_bytes > old.tools.max_file_bytes:
        raise PatchError("tools.max_file_bytes may only decrease")
    # added_policies append-only
    olds = [p.model_dump() for p in old.added_policies]
    news = [p.model_dump() for p in new.added_policies]
    if news[: len(olds)] != olds:
        raise PatchError("added_policies is append-only (existing entries changed or removed)")
    if len(news) > MAX_ADDED_POLICIES:
        raise PatchError("too many added_policies")
    ids = [p["id"] for p in news]
    if len(set(ids)) != len(ids):
        raise PatchError("duplicate added policy id")
    for p in new.added_policies[len(olds):]:
        if p.id in BASE_POLICY_IDS:
            raise PatchError("added policy id collides with a base policy")
        if p.kind in ("deny_added_line_regex", "deny_exec_regex"):
            try:
                re.compile(p.pattern)
            except re.error as e:
                raise PatchError(f"bad regex in {p.id}: {e}") from e
        if not p.pattern.strip():
            raise PatchError(f"empty pattern in {p.id}")
    # sizes
    if new.version != old.version:
        raise PatchError("version is managed by the orchestrator")
    if not new.system_prompt or len(new.system_prompt) > MAX_PROMPT_SECTIONS:
        raise PatchError("system_prompt must have 1..12 sections")
    for k, v in new.system_prompt.items():
        if len(v) > MAX_PROMPT_SECTION_CHARS:
            raise PatchError(f"system_prompt[{k}] too long")
    if len(new.memory_notes) > MAX_MEMORY_NOTES or any(len(n) > MAX_NOTE_CHARS for n in new.memory_notes):
        raise PatchError("memory_notes too large")
    if new.content_hash() == old.content_hash():
        raise PatchError("patch is a no-op")


def validate_edit(champion: Genome, ops: Any, *, hide_tests: bool = False) -> Genome:
    """Return the candidate genome or raise PatchError with the reason."""
    _check_ops_shape(ops)
    cand = apply_ops(champion, ops)
    check_surface(champion, cand)
    if hide_tests and cand.context.include_test_file_in_prompt:
        raise PatchError("arm hides tests: include_test_file_in_prompt must stay false")
    return cand
