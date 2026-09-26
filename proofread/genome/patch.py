"""RFC 6902 JSON Patch over the genome, restricted to the editable surface."""

from __future__ import annotations

import copy
from typing import Any

import jsonpatch

from proofread.contracts import Genome

from .editable_surface import EDITABLE_TOP_LEVEL, EditRejected, validate_candidate

ALLOWED_OPS = ("add", "remove", "replace", "move", "copy", "test")


def _check_pointer(ptr: Any) -> None:
    if not isinstance(ptr, str) or not ptr.startswith("/"):
        raise EditRejected("patch_bad_path", repr(ptr))
    top = ptr.split("/")[1].replace("~1", "/").replace("~0", "~")
    if top not in EDITABLE_TOP_LEVEL:
        raise EditRejected("patch_outside_surface", ptr)


def check_ops(ops: list[dict[str, Any]]) -> None:
    if not isinstance(ops, list) or not ops:
        raise EditRejected("patch_empty")
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in ALLOWED_OPS:
            raise EditRejected("patch_bad_op", repr(op)[:200])
        _check_pointer(op.get("path"))
        if op["op"] in ("move", "copy"):
            _check_pointer(op.get("from"))


def apply_patch(champion: Genome, ops: list[dict[str, Any]]) -> Genome:
    """Apply a JSON Patch to the champion and validate the result against the editable surface."""
    check_ops(ops)
    doc = copy.deepcopy(champion.model_dump(mode="json"))
    try:
        out = jsonpatch.apply_patch(doc, ops, in_place=False)
    except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException, KeyError, IndexError, TypeError) as e:
        raise EditRejected("patch_apply_failed", str(e)[:500]) from e
    return validate_candidate(out, champion)


def diff(champion: Genome, candidate: Genome) -> list[dict[str, Any]]:
    """JSON Patch that turns champion into candidate (for logging edits)."""
    return jsonpatch.make_patch(champion.model_dump(mode="json"), candidate.model_dump(mode="json")).patch
