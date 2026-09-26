"""Canonicalization of paths, text and diffs. Pure functions, no I/O.

Everything that policies judge goes through here first so the reference policies (and biject-api)
see one normal form:
- paths: absolute, relative paths resolved against /workspace, `.`/`..`/`//` collapsed, NFC.
- text: decoded (honouring a PEP 263 coding cookie for Python sources), NFC, CRLF and lone CR -> LF.
- lines: added/removed computed as a multiset difference, so duplicating an existing line counts as
  an addition and reordering is not an addition.
"""

from __future__ import annotations

import codecs
import io
import posixpath
import re
import tokenize
import unicodedata
from collections import Counter

WORKSPACE = "/workspace"
TMP = "/tmp"


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def canon_path(path: str, cwd: str = WORKSPACE) -> str:
    """Absolute, normalized, NFC path. Relative paths are resolved against cwd (default /workspace).

    Pure string operation: symlinks are resolved by the capture layer (effects.py), not here.
    """
    p = nfc(path or "").replace("\x00", "")
    if not p:
        return ""
    if not p.startswith("/"):
        p = posixpath.join(cwd, p)
    p = posixpath.normpath(p)
    # POSIX normpath keeps a leading "//"; collapse it so "//workspace/x" == "/workspace/x".
    p = "/" + p.lstrip("/")
    return p


def is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def canon_text(s: str) -> str:
    return normalize_newlines(nfc(s))


_COOKIE_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def decode_bytes(data: bytes, path: str = "") -> str:
    """Decode file bytes the way Python would read the source.

    For .py/.pth files a PEP 263 coding cookie is honoured (so an exotic encoding cannot hide an
    added skip marker from the line matcher). Everything else is UTF-8 with replacement.
    """
    if path.endswith((".py", ".pth", ".pyw")):
        try:
            enc, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            return data.decode(enc, errors="replace")
        except (SyntaxError, LookupError, UnicodeDecodeError):
            m = None
            for line in data.splitlines()[:2]:
                m = _COOKIE_RE.match(line)
                if m:
                    break
            if m:
                try:
                    return data.decode(codecs.lookup(m.group(1).decode("ascii", "replace")).name, errors="replace")
                except LookupError:
                    pass
    return data.decode("utf-8", errors="replace")


def split_lines(s: str) -> list[str]:
    """Canonical lines. str.splitlines also splits on \\v, \\f, \\x1c-\\x1e, \\x85, \\u2028, \\u2029,
    which over-splits relative to the Python tokenizer; that is safe for detection."""
    return canon_text(s).splitlines()


def diff_lines(old: str | None, new: str | None) -> tuple[list[str], list[str]]:
    """(added, removed) as multiset differences of canonical lines, in file order."""
    old_l = split_lines(old or "")
    new_l = split_lines(new or "")
    oc = Counter(old_l)
    nc = Counter(new_l)
    added: list[str] = []
    budget = nc - oc
    for line in new_l:
        if budget.get(line, 0) > 0:
            added.append(line)
            budget[line] -= 1
    removed: list[str] = []
    budget = oc - nc
    for line in old_l:
        if budget.get(line, 0) > 0:
            removed.append(line)
            budget[line] -= 1
    return added, removed


# ---------------------------------------------------------------------------------------------
# Config section detection (setup.cfg / tox.ini / pyproject.toml)
# ---------------------------------------------------------------------------------------------

_TOML_HDR = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
_INI_HDR = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*([#;].*)?$")


def _sections(text: str, toml: bool) -> dict[str, list[str]]:
    """Map section name -> lines (header line excluded). Top-level lines go to ""."""
    out: dict[str, list[str]] = {"": []}
    cur = ""
    hdr = _TOML_HDR if toml else _INI_HDR
    for line in split_lines(text):
        m = hdr.match(line)
        if m:
            cur = m.group(1).strip().replace('"', "").replace("'", "")
            cur = re.sub(r"\s*\.\s*", ".", cur) if toml else cur
            out.setdefault(cur, [])
            continue
        out.setdefault(cur, []).append(line)
    return out


def changed_sections(old: str | None, new: str | None, path: str) -> str:
    """Comma-joined sorted names of config sections whose content changed ("" if not a config file).

    Top-level changes in pyproject.toml are reported as "<top>" (TOML dotted keys like
    `tool.pytest.ini_options.addopts = ...` can live at top level), so the policy fails closed.
    """
    base = posixpath.basename(path)
    if base == "pyproject.toml":
        toml = True
    elif base in ("setup.cfg", "tox.ini", "pytest.ini", ".pytest.ini"):
        toml = False
    else:
        return ""
    a = _sections(old or "", toml)
    b = _sections(new or "", toml)
    changed = []
    for name in set(a) | set(b):
        if Counter(a.get(name, [])) != Counter(b.get(name, [])) or ((name in a) != (name in b)):
            changed.append(name or "<top>")
    return ",".join(sorted(changed))


__all__ = [
    "WORKSPACE",
    "TMP",
    "nfc",
    "canon_path",
    "is_under",
    "normalize_newlines",
    "canon_text",
    "decode_bytes",
    "split_lines",
    "diff_lines",
    "changed_sections",
]
