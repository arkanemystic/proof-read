"""
Typed parameter transforms: tool-call params → Lean 4 terms.

Added for Proofread (upstream only had "int" and "bps"). Every transform validates its input type
and raises RenderError on anything unexpected; the orchestrator turns that into an "error" verdict
(fail closed). No caller-controlled text reaches Lean except inside char literals: each character
of a string is rendered as its own `'c'` literal (a fixed safe ASCII set) or `(Char.ofNat n)`, so
data can never close a literal, open a comment, or add a command.

Transforms:
  "int"             int(value)                          → 42
  "bps"             int(float(value) * 10000)           → 2200
  "bool"            JSON bool only                      → true | false
  "enum"            value looked up in param_enums      → .write
  "chars"           str                                 → ['a', 'b'] : List Char
  "chars_list_json" JSON-encoded list[str]              → [['a'], ['b', 'c']] : List (List Char)
"""

from __future__ import annotations

import json
import re
import string
from typing import Any

# Printable ASCII except the two characters that are special inside a char literal.
_SAFE = frozenset(string.ascii_letters + string.digits + " !\"#$%&()*+,-./:;<=>?@[]^_`{|}~")
_ENUM_CTOR = re.compile(r"^\.[A-Za-z][A-Za-z0-9_]*$")
MAX_CHARS = 1_000_000  # per action, across all string params


class RenderError(ValueError):
    pass


def lean_char(c: str) -> str:
    if c in _SAFE:
        return f"'{c}'"
    n = ord(c)
    if 0xD800 <= n <= 0xDFFF:
        raise RenderError("lone surrogate in string")  # Char.ofNat would silently map it to '\0'
    return f"(Char.ofNat {n})"


def lean_chars(s: str) -> str:
    return "[" + ",".join(lean_char(c) for c in s) + "]"


def _as_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise RenderError(f"{what}: expected string, got {type(value).__name__}")
    return value


def str_list(value: Any, what: str) -> list[str]:
    """Decode a "chars_list_json" param to list[str] (validated)."""
    raw = _as_str(value, what)
    try:
        xs = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RenderError(f"{what}: bad JSON: {e}") from e
    if not isinstance(xs, list) or not all(isinstance(x, str) for x in xs):
        raise RenderError(f"{what}: expected a JSON array of strings")
    return xs


def render(value: Any, transform: str, *, enum: dict[str, str] | None = None, what: str = "") -> str:
    if transform == "int":
        if isinstance(value, bool):
            raise RenderError(f"{what}: expected int, got bool")
        return str(int(value))
    if transform == "bps":
        return str(int(float(value) * 10000))
    if transform == "bool":
        if not isinstance(value, bool):
            raise RenderError(f"{what}: expected bool, got {type(value).__name__}")
        return "true" if value else "false"
    if transform == "enum":
        v = _as_str(value, what)
        ctor = (enum or {}).get(v)
        if ctor is None or not _ENUM_CTOR.match(ctor):
            raise RenderError(f"{what}: {v!r} is not an allowed value")
        return ctor
    if transform == "chars":
        return lean_chars(_as_str(value, what))
    if transform == "chars_list_json":
        return lean_chars_list(str_list(value, what))
    raise RenderError(f"{what}: unknown transform {transform!r}")


def lean_chars_list(xs: list[str]) -> str:
    return "[" + ",".join(lean_chars(x) for x in xs) + "]"


def size_of(params: dict[str, Any]) -> int:
    return sum(len(v) for v in params.values() if isinstance(v, str))
