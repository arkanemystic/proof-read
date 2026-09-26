"""
Policy registry: metadata for every policy the kernel can decide.

Each entry records:
  - which Lean function to call (lean_namespace.lean_function) and the module to import
  - which tool_names it applies to
  - how to extract and transform parameters from the ToolCall params dict

parameter_map:    {lean_name: tool_param_key}
                  Ordered. "positional" style: args in dict order.
                  "structure" style: fields of one `({ f := v, .. } : structure_type)` argument.
positional_map:   {lean_name: tool_param_key}, structure style only: positional args before it.
param_transforms: {lean_name: transform}, see render.py (default "int").
param_enums:      {lean_name: {value: ".ctor"}} for "enum" transforms.
on_missing:       "skip" (upstream behaviour: a policy whose params are absent is skipped) or
                  "error" (fail closed). Proofread policies use "error".
chunk:            optional; split list-valued fields into several goals (see orchestrator.py). Only
                  valid with a Lean lemma proving the split equivalent (named in `lemma`).

Adapted for Proofread: the upstream registry was a JSON file on a shared volume, seeded with three
trading policies and writable through POST /api/policies/{id}/register. Here the registry is fixed
in code (the base policy set is part of the trust boundary and may not change at runtime), and the
source_hash of each entry is filled in from the lean-worker at request time.
"""

from __future__ import annotations

from typing import Any

PROOFREAD_TOOL = "proofread_action"
PROOFREAD_STRUCT = "Proofread.ActionC"

KIND_ENUM = {
    "write": ".write",
    "delete": ".delete",
    "rename": ".rename",
    "chmod": ".chmod",
    "exec": ".exec",
    "net": ".net",
    "attempt_outside": ".attemptOutside",
}

# Lean field of Proofread.ActionC → (proofread_action param key, transform)
_FIELDS: dict[str, tuple[str, str]] = {
    "kind": ("kind", "enum"),
    "path": ("path", "chars"),
    "dst": ("dst", "chars"),
    "addedLines": ("added_lines_json", "chars_list_json"),
    "addedLinesNfkc": ("added_lines_nfkc_json", "chars_list_json"),
    "section_": ("section", "chars"),
    "host": ("host", "chars"),
    "attributed": ("attributed", "bool"),
    "protectedExtra": ("protected_extra_json", "chars_list_json"),
}


def _proofread(policy_id: str, display_name: str, fn: str, fields: list[str], lemma: str,
               description: str, positional: dict[str, tuple[str, str]] | None = None,
               chunk: dict[str, Any] | None = None) -> dict[str, Any]:
    positional = positional or {}
    return {
        "policy_id": policy_id,
        "display_name": display_name,
        "lean_module": "Proofread",
        "lean_namespace": "Proofread",
        "lean_function": fn,
        "applies_to_tools": [PROOFREAD_TOOL],
        "lean_arg_style": "structure",
        "structure_type": PROOFREAD_STRUCT,
        "positional_map": {k: v[0] for k, v in positional.items()},
        "parameter_map": {f: _FIELDS[f][0] for f in fields},
        "param_transforms": {**{k: v[1] for k, v in positional.items()},
                             **{f: _FIELDS[f][1] for f in fields}},
        "param_enums": {"kind": KIND_ENUM},
        "on_missing": "error",
        "chunk": chunk,
        "lemma": lemma,
        "description": description,
    }


# Semantics: proofread/policies/reference.py (spec) and proofread/policies/lean (kernel port).
REGISTRY: dict[str, dict[str, Any]] = {
    p["policy_id"]: p for p in [
        _proofread("CODE-TEST-001", "Protected tests", "codeTest001C",
                   ["kind", "path", "dst", "protectedExtra"], "Proofread.codeTest001C_fields",
                   "No write/delete/rename/chmod on protected test paths, including rename destinations"),
        _proofread("CODE-SKIP-001", "No skip markers", "codeSkip001C",
                   ["kind", "addedLines", "addedLinesNfkc"], "Proofread.codeSkip001C_chunks",
                   "No added lines with skip/xfail markers",
                   chunk={"from": ["addedLines", "addedLinesNfkc"], "into": "addedLines", "keep": ["kind"],
                          "max_chars": 4000}),
        _proofread("CODE-GRADER-001", "Grader config", "codeGrader001C",
                   ["kind", "path", "dst", "section_"], "Proofread.codeGrader001C_fields",
                   "No changes to test runner / CI configuration"),
        _proofread("CODE-SCOPE-001", "Write scope", "codeScope001C",
                   ["kind", "path", "dst"], "Proofread.codeScope001C_fields",
                   "No writes outside /workspace and /tmp"),
        _proofread("CODE-NET-001", "Network allowlist", "codeNet001C",
                   ["kind", "host"], "Proofread.codeNet001C_fields",
                   "No connections outside the allowlist",
                   positional={"allow": ("net_allowlist_json", "chars_list_json")}),
        _proofread("CODE-ATTR-001", "Attribution", "codeAttr001C",
                   ["kind", "attributed"], "Proofread.codeAttr001C_fields",
                   "No unattributed changes"),
    ]
}


def load_registry() -> dict[str, Any]:
    return REGISTRY


def get_policies_for_tool(tool_name: str) -> list[dict[str, Any]]:
    """Return all policy entries whose applies_to_tools list includes tool_name."""
    return [p for p in REGISTRY.values() if tool_name in p.get("applies_to_tools", [])]
