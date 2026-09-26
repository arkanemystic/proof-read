"""In-container snapshot walker. Baked into the read-only image at /opt/proofread/snap.py and run as
root with `python3 -I -S` (isolated: ignores PYTHON* env, user site, .pth files, cwd on sys.path), so
nothing the agent writes can influence it.

stdin: JSON {"roots": [...], "prev": {path: statkey}, "always": [path...], "always_globs": [...],
             "content_max": int, "max_entries": int}
stdout: JSON {"entries": {path: entry}, "truncated": bool, "errors": [str]}

entry: {"t": "f"|"d"|"l"|"o", "mode": int, "ino": int, "nlink": int, "size": int, "key": [...],
        "h": sha256 hex (omitted when the stat key equals prev and the path is not protected),
        "c": base64 content (files <= content_max, only when hashed),
        "target": readlink, "rh": hash of resolved target, "via": symlink path for alias entries}

Stat prefilter: a file is rehashed only if (mode, ino, size, mtime_ns, ctime_ns, nlink, uid) changed.
ctime cannot be set from userspace, so utime tricks do not hide a content change. Protected paths
(manifest + test/grader patterns) are always rehashed.
"""

import base64
import fnmatch
import hashlib
import json
import os
import stat
import sys

SKIP_DIRS = {".pytest_cache"}  # __pycache__ is captured: a planted .pyc is a grader-tampering vector
UNSAFE_PREFIXES = ("/proc", "/sys", "/dev")


def under(p, roots):
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)


def statkey(st):
    return [st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink, st.st_uid]


def read_file(path, content_max):
    h = hashlib.sha256()
    buf = bytearray()
    keep = True
    with open(path, "rb", buffering=0) as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            if keep:
                buf += chunk
                if len(buf) > content_max:
                    keep = False
                    buf = bytearray()
    return h.hexdigest(), (bytes(buf) if keep else None)


def main():
    req = json.loads(sys.stdin.read() or "{}")
    roots = req.get("roots", ["/workspace"])
    prev = req.get("prev", {})
    always = set(req.get("always", []))
    globs = req.get("always_globs", [])
    content_max = int(req.get("content_max", 2_000_000))
    max_entries = int(req.get("max_entries", 50_000))
    entries = {}
    errors = []
    truncated = False

    def protected(p):
        if p in always:
            return True
        base = p.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatchcase(base, g) or fnmatch.fnmatchcase(p, g) for g in globs)

    def add_file(p, st, real=None, via=None):
        e = {"t": "f", "mode": stat.S_IMODE(st.st_mode), "ino": st.st_ino, "nlink": st.st_nlink,
             "size": st.st_size, "key": statkey(st)}
        if via:
            e["via"] = via
        if via or prev.get(p) != e["key"] or protected(p):
            try:
                h, c = read_file(real or p, content_max)
                e["h"] = h
                if c is not None:
                    e["c"] = base64.b64encode(c).decode("ascii")
            except OSError as ex:
                e["h"] = "unreadable:" + type(ex).__name__
        entries[p] = e

    def walk(top, alias=None, seen=None, depth=0):
        """Walk top. With alias, emit entries under the alias prefix (symlinked directory)."""
        nonlocal truncated
        seen = seen if seen is not None else set()
        if depth > 8:
            return
        try:
            real_top = os.path.realpath(top)
        except OSError:
            return
        if real_top in seen:
            return
        seen = seen | {real_top}
        try:
            names = sorted(os.listdir(top))
        except OSError as ex:
            errors.append(f"listdir {top}: {ex}")
            return
        for name in names:
            if len(entries) >= max_entries:
                truncated = True
                return
            real = os.path.join(top, name)
            p = os.path.join(alias, name) if alias else real
            try:
                st = os.lstat(real)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if name in SKIP_DIRS:
                    continue
                entries[p] = {"t": "d", "mode": stat.S_IMODE(st.st_mode), "ino": st.st_ino,
                              "nlink": 0, "size": 0, "key": []}
                if alias:
                    entries[p]["via"] = alias
                walk(real, os.path.join(alias, name) if alias else None, seen, depth + 1)
            elif stat.S_ISREG(st.st_mode):
                add_file(p, st, real=real, via=alias)
            elif stat.S_ISLNK(st.st_mode):
                try:
                    target = os.readlink(real)
                except OSError:
                    target = "?"
                resolved = os.path.realpath(real)
                e = {"t": "l", "mode": 0, "ino": st.st_ino, "nlink": st.st_nlink, "size": 0,
                     "key": statkey(st), "target": target, "resolved": resolved}
                if alias:
                    e["via"] = alias
                try:
                    rst = os.stat(real)
                    safe_file = under(resolved, roots) or not under(resolved, UNSAFE_PREFIXES)
                    if stat.S_ISREG(rst.st_mode) and not safe_file:
                        e["rh"] = "unsafe:" + resolved
                    elif stat.S_ISDIR(rst.st_mode) and not under(resolved, roots):
                        e["rh"] = "outside-dir:" + resolved
                    elif stat.S_ISREG(rst.st_mode):
                        h, c = read_file(resolved, content_max)
                        e["rh"] = h
                        if c is not None:
                            e["c"] = base64.b64encode(c).decode("ascii")
                    elif stat.S_ISDIR(rst.st_mode):
                        e["rh"] = "dir"
                        entries[p] = e
                        walk(resolved, p, seen, depth + 1)
                        continue
                    else:
                        e["rh"] = "other"
                except OSError:
                    e["rh"] = "dangling"
                entries[p] = e
            else:
                entries[p] = {"t": "o", "mode": stat.S_IMODE(st.st_mode), "ino": st.st_ino,
                              "nlink": st.st_nlink, "size": 0, "key": statkey(st), "h": "special"}
                if alias:
                    entries[p]["via"] = alias

    for root in roots:
        if os.path.isdir(root):
            walk(root)
    sys.stdout.write(json.dumps({"entries": entries, "truncated": truncated, "errors": errors[:20]}))


if __name__ == "__main__":
    main()
