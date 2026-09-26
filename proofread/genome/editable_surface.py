"""Editable-surface enforcement for proposed genomes.

The proposer may only edit Genome fields (schema extra=forbid rejects anything else, so tool
implementations, the interceptor, verifier client, graders, sandbox config and base policies are
not even representable). On top of the schema this module enforces, relative to the champion:
- tools may only narrow: enabled is a subset, run_timeout_s and max_file_bytes do not grow;
- added_policies are append-only: every champion policy is still present and unchanged;
- new policy ids are unique and never shadow a base policy id; regex patterns compile;
- prompt and memory sizes are bounded.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from proofread.contracts import BASE_POLICY_IDS, Genome

MAX_PROMPT_CHARS = 20_000
MAX_PROMPT_SECTIONS = 20
MAX_MEMORY_NOTES = 50
MAX_MEMORY_CHARS = 10_000
MAX_ADDED_POLICIES = 50
EDITABLE_TOP_LEVEL = tuple(Genome.model_fields)  # version, system_prompt, workflow, context, tools, ...


class EditRejected(ValueError):
    """A proposed genome violates the editable surface. `reason` is a short machine-friendly code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _as_genome(candidate: Genome | dict[str, Any]) -> Genome:
    if isinstance(candidate, Genome):
        # Re-validate from a dump so a model_construct()ed object cannot bypass the schema.
        candidate = candidate.model_dump(mode="json", warnings=False)
    if not isinstance(candidate, dict):
        raise EditRejected("schema", "genome must be a JSON object")
    try:
        return Genome.model_validate(candidate)
    except ValidationError as e:
        raise EditRejected("schema", str(e)[:2000]) from e


def validate_candidate(candidate: Genome | dict[str, Any], champion: Genome) -> Genome:
    """Return the validated candidate Genome or raise EditRejected."""
    g = _as_genome(candidate)

    # Prompt and memory bounds.
    if len(g.system_prompt) > MAX_PROMPT_SECTIONS:
        raise EditRejected("prompt_too_large", f"{len(g.system_prompt)} sections")
    total = sum(len(k) + len(v) for k, v in g.system_prompt.items())
    if total > MAX_PROMPT_CHARS:
        raise EditRejected("prompt_too_large", f"{total} chars")
    if len(g.memory_notes) > MAX_MEMORY_NOTES or sum(map(len, g.memory_notes)) > MAX_MEMORY_CHARS:
        raise EditRejected("memory_too_large")

    # Tools may only narrow.
    extra_tools = set(g.tools.enabled) - set(champion.tools.enabled)
    if extra_tools:
        raise EditRejected("tool_widened", f"enables {sorted(extra_tools)}")
    if len(set(g.tools.enabled)) != len(g.tools.enabled):
        raise EditRejected("tool_duplicate")
    if g.tools.run_timeout_s > champion.tools.run_timeout_s:
        raise EditRejected("tool_widened", "run_timeout_s grows")
    if g.tools.max_file_bytes > champion.tools.max_file_bytes:
        raise EditRejected("tool_widened", "max_file_bytes grows")

    # Guardrails are monotone: champion's added policies remain, unchanged.
    by_id = {}
    for p in g.added_policies:
        if p.id in by_id:
            raise EditRejected("policy_duplicate_id", p.id)
        if p.id in BASE_POLICY_IDS:
            raise EditRejected("policy_shadows_base", p.id)
        by_id[p.id] = p
    for old in champion.added_policies:
        new = by_id.get(old.id)
        if new is None:
            raise EditRejected("policy_removed", old.id)
        if new != old:
            raise EditRejected("policy_modified", old.id)
    if len(g.added_policies) > MAX_ADDED_POLICIES:
        raise EditRejected("too_many_policies")
    for p in g.added_policies:
        if not p.pattern:
            raise EditRejected("policy_empty_pattern", p.id)
        if p.kind in ("deny_added_line_regex", "deny_exec_regex"):
            try:
                re.compile(p.pattern)
            except re.error as e:
                raise EditRejected("policy_bad_regex", f"{p.id}: {e}") from e
    return g
