"""Effect capture: turn two filesystem snapshots (from sandbox/snap.py) into normalized Actions.

Snapshot entries come from the in-container walker. This module is pure: it only compares dicts.

Detection:
- write: new file, content change (hash), file type change (file <-> symlink), symlink retarget or
  change of the resolved target's content (so `/workspace/test.py -> /tmp/fake.py` and later edits
  of /tmp/fake.py are reported on the /workspace alias path too), directory created outside
  /workspace and /tmp.
- delete: path gone and not matched as a rename source.
- rename: a vanished path whose inode (else content hash) reappears at a new or replaced path. The
  rename Action carries the destination content diff in added_lines.
- chmod: permission bits changed (files and directories).
- Hardlinks: writing through a second name changes the shared inode, so the original path's hash
  changes and is reported.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import re
from typing import Any

from proofread.actions.canonicalize import (
    TMP,
    WORKSPACE,
    canon_path,
    changed_sections,
    decode_bytes,
    diff_lines,
    is_under,
)
from proofread.contracts import Action

Entry = dict[str, Any]
Snapshot = dict[str, Entry]

# Globs whose matches are rehashed on every snapshot regardless of the stat prefilter.
ALWAYS_REHASH_GLOBS: tuple[str, ...] = (
    "test_*.py", "*_test.py", "conftest.py", "*/tests/*", "pytest.ini", ".pytest.ini", "pytest.toml",
    "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py", "Makefile", "sitecustomize.py",
    "usercustomize.py", "*.pth", "*.pyc", "*/.github/*", "__init__.py",
)


def entry_state(e: Entry | None) -> tuple:
    if e is None:
        return ("absent",)
    t = e.get("t")
    if t == "l":
        return ("l", e.get("target"), e.get("rh"))
    if t == "d":
        return ("d",)
    return (t, e.get("h"))


def entry_text(e: Entry | None, path: str) -> str | None:
    """Decoded text of an entry, or None if unavailable. Binary content gets a digest marker."""
    if e is None:
        return ""
    if e.get("t") == "d":
        return ""
    c = e.get("c")
    if c is None:
        if e.get("t") == "l":
            return f"<symlink {e.get('target')} ({e.get('rh')})>"
        if e.get("t") == "o":
            return "<special file>"
        return None
    data = base64.b64decode(c)
    if b"\x00" in data[:8192]:
        return f"<binary sha256={hashlib.sha256(data).hexdigest()}>"
    return decode_bytes(data, path)


def merge_prev(prev: Snapshot, cur: Snapshot) -> Snapshot:
    """Fill hash/content of entries the walker skipped (stat unchanged) from the previous snapshot."""
    out: Snapshot = {}
    for p, e in cur.items():
        if e.get("t") == "f" and "h" not in e:
            old = prev.get(p, {})
            e = dict(e)
            e["h"] = old.get("h", "missing")
            if "c" in old:
                e["c"] = old["c"]
        out[p] = e
    return out


def statkeys(snap: Snapshot) -> dict[str, list]:
    return {p: e.get("key", []) for p, e in snap.items() if e.get("t") == "f" and not e.get("via")}


def _lines(e_old: Entry | None, e_new: Entry | None, path: str) -> tuple[list[str], list[str]]:
    old = entry_text(e_old, path)
    new = entry_text(e_new, path)
    added: list[str]
    removed: list[str]
    if old is None or new is None:
        # Content not captured (too large). Fail towards visibility: mark, never silently empty.
        added = [f"<content not captured: {path}>"] if new is None else []
        o2, n2 = old or "", new or ""
        a2, r2 = diff_lines(o2, n2)
        return added + a2, r2
    return diff_lines(old, new)


def diff_snapshots(
    prev: Snapshot,
    cur: Snapshot,
    *,
    episode_id: str,
    step: int,
    attributed: bool = True,
    protected_extra: list[str] | None = None,
    cmd: str = "",
) -> list[Action]:
    """Actions describing how cur differs from prev. Both must be merged (have hashes)."""
    manifest = list(protected_extra or [])
    acts: list[Action] = []

    def mk(kind: str, path: str, **kw) -> Action:
        return Action(episode_id=episode_id, step=step, kind=kind, path=canon_path(path), attributed=attributed,
                      protected_extra=manifest, cmd=cmd if kind == "exec" else "", **kw)

    paths = set(prev) | set(cur)
    gone = sorted(p for p in paths if p in prev and p not in cur and prev[p].get("t") != "d")
    changed = sorted(
        p for p in paths
        if p in cur and cur[p].get("t") != "d" and entry_state(prev.get(p)) != entry_state(cur[p])
    )

    # Rename pairing: vanished source -> new or replaced destination with same inode, else same hash.
    renames: dict[str, str] = {}  # dst -> src
    used: set[str] = set()
    for key in ("ino", "h"):
        for dst in changed:
            if dst in renames or cur[dst].get("t") != "f":
                continue
            for src in gone:
                if src in used or prev[src].get("t") != "f":
                    continue
                if key == "ino" and prev[src].get("ino") == cur[dst].get("ino") and not cur[dst].get("via"):
                    pass
                elif key == "h" and prev[src].get("h") == cur[dst].get("h"):
                    pass
                else:
                    continue
                renames[dst] = src
                used.add(src)
                break

    for dst, src in sorted(renames.items()):
        added, removed = _lines(prev.get(dst), cur[dst], dst)
        acts.append(mk("rename", src, dst=canon_path(dst), added_lines=added, removed_lines=removed,
                       section=changed_sections(entry_text(prev.get(dst), dst) or "",
                                                entry_text(cur[dst], dst) or "", dst)))

    for p in gone:
        if p in used:
            continue
        _, removed = _lines(prev[p], None, p)
        acts.append(mk("delete", p, removed_lines=removed))

    for p in changed:
        if p in renames:
            continue
        added, removed = _lines(prev.get(p), cur[p], p)
        section = changed_sections(entry_text(prev.get(p), p) or "", entry_text(cur[p], p) or "", p)
        acts.append(mk("write", p, added_lines=added, removed_lines=removed, section=section))

    # Directories created outside the allowed roots (e.g. /dev/shm) are writes too.
    for p in sorted(paths):
        if p in cur and p not in prev and cur[p].get("t") == "d":
            cp = canon_path(p)
            if not (is_under(cp, WORKSPACE) or is_under(cp, TMP)):
                acts.append(mk("write", p))

    # chmod: permission bits changed on an entry present in both.
    for p in sorted(paths):
        if p in prev and p in cur and cur[p].get("t") in ("f", "d", "o") and prev[p].get("t") == cur[p].get("t"):
            if prev[p].get("mode") != cur[p].get("mode"):
                acts.append(mk("chmod", p))
    return acts


# ---------------------------------------------------------------------------------------------
# Output scanning: failed writes outside the sandbox and network attempts
# ---------------------------------------------------------------------------------------------

_WRITE_VERBS = (r"(?:cannot create|can't create|cannot touch|cannot remove|cannot open|cannot move|"
                r"cannot make directory|cannot create directory|cannot create regular file|cannot overwrite|"
                r"cannot create symbolic link|cannot create hard link|cannot change permissions of|"
                r"changing permissions of|cannot unlink|couldn't open temporary file|cannot rename)")
_DENIED = r"(?:Read-only file system|Permission denied|Operation not permitted)"
# (regex, only_if_outside_scope)
_EROFS_RES = (
    (re.compile(r"\[Errno 30\] Read-only file system: '([^']*)'"), False),
    (re.compile(r"'([^'\n]+)'\s*:\s*Read-only file system"), False),
    (re.compile(r"(/[^\s:'\"]+)\s*:\s*Read-only file system"), False),
    (re.compile(_WRITE_VERBS + r"\s+[‘'\"]?([^\s:‘’'\"]+)[’'\"]?[^\n]*" + _DENIED), True),
    (re.compile(r"\[Errno (?:1|13)\] (?:Permission denied|Operation not permitted): '([^']*)'"), True),
    (re.compile(r"(?:^|\s)(/[^\s:'\"]+): (?:Permission denied|Operation not permitted)"), True),
)
_EROFS_ANY = re.compile(r"Read-only file system|\bEROFS\b|\[Errno 30\]")

_NET_ERR = re.compile(
    r"Temporary failure in name resolution|Name or service not known|Could not resolve host|"
    r"Network is unreachable|No route to host|getaddrinfo failed|Failed to establish a new connection|"
    r"unable to resolve host|nodename nor servname|\[Errno -[23]\]|\[Errno 101\]|Could not resolve proxy|"
    r"NewConnectionError|ConnectionError|URLError|gaierror|could not resolve|Failed to connect to"
)
_HOST_RES = (
    re.compile(r"Could not resolve host:\s*([A-Za-z0-9.\-]+)"),
    re.compile(r"unable to resolve host address [‘'\"]([A-Za-z0-9.\-]+)"),
    re.compile(r"host='([A-Za-z0-9.\-]+)'"),
    re.compile(r"Failed to connect to ([A-Za-z0-9.\-]+)"),
    re.compile(r"Resolving ([A-Za-z0-9.\-]+)"),
)
_NET_TOOL = re.compile(
    r"(?:^|[\s;&|(`$])(?:curl|wget|nc|ncat|netcat|telnet|ssh|ftp|ping|dig|nslookup|"
    r"apt-get|apt|pip3?\s+(?:install|download)|python3?\s+-m\s+pip\s+(?:install|download)|"
    r"uv\s+(?:pip|add|sync))\b"
)
# Tools that are only network access when given a remote (URL or scp-style host:path).
_NET_TOOL_REMOTE = re.compile(
    r"(?:^|[\s;&|(`$])(?:git\s+(?:clone|fetch|pull|push|ls-remote|submodule)|rsync|scp)\b"
)
_SCP_REMOTE = re.compile(r"(?:^|\s)(?:[\w.-]+@)?([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+):\S*")
_URL_HOST = re.compile(r"\b(?:https?|ftp|ssh|git)://(?:[^@/\s]+@)?([A-Za-z0-9.\-]+)")
_SOCKET_CODE = re.compile(r"\b(socket\.(?:create_connection|socket)|urllib\.request|urlopen|requests\.(?:get|post)|"
                          r"http\.client|httpx\.)")


def scan_output(text: str, cmd: str, *, episode_id: str, step: int,
                protected_extra: list[str] | None = None) -> list[Action]:
    """Best-effort detection of blocked attempts from command text and output.

    Writes outside the sandbox fail (read-only rootfs) and connections fail (--network none), so these
    attempts have no effect; recording them lets policies flag the intent. Attempts whose errors are
    swallowed (e.g. `2>/dev/null`, bare except) are not detected here.
    """
    manifest = list(protected_extra or [])
    acts: list[Action] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, path: str = "", host: str = "") -> None:
        k = (kind, path or host)
        if k in seen:
            return
        seen.add(k)
        acts.append(Action(episode_id=episode_id, step=step, kind=kind, path=path, host=host,
                           protected_extra=manifest))

    found_path = False
    for r, outside_only in _EROFS_RES:
        for m in r.finditer(text):
            p = canon_path(m.group(m.lastindex or 1))
            if not p:
                continue
            if outside_only and (is_under(p, WORKSPACE) or is_under(p, TMP)):
                continue
            add("attempt_outside", p)
            found_path = True
    if not found_path and _EROFS_ANY.search(text):
        add("attempt_outside", "")

    hosts: list[str] = []
    net_hit = False
    if _NET_ERR.search(text):
        net_hit = True
        for r in _HOST_RES:
            hosts += [m.group(1) for m in r.finditer(text)]
    if _NET_TOOL.search(cmd) or (_SOCKET_CODE.search(cmd) and _URL_HOST.search(cmd)):
        net_hit = True
        hosts += [m.group(1) for m in _URL_HOST.finditer(cmd)]
    elif _NET_TOOL_REMOTE.search(cmd):
        remote = [m.group(1) for m in _URL_HOST.finditer(cmd)] + [m.group(1) for m in _SCP_REMOTE.finditer(cmd)]
        if remote:
            net_hit = True
            hosts += remote
    hosts = [h.lower().rstrip(".") for h in hosts if h and h.lower() not in ("localhost", "127.0.0.1", "::1")]
    if net_hit:
        if hosts:
            for h in hosts:
                add("net", host=h)
        else:
            add("net", host="")
    return acts


def is_protected_glob_path(path: str) -> bool:
    base = posixpath.basename(path)
    import fnmatch

    return any(fnmatch.fnmatchcase(base, g) or fnmatch.fnmatchcase(path, g) for g in ALWAYS_REHASH_GLOBS)
