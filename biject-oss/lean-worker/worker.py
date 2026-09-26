#!/usr/bin/env python3
"""
lean-worker: HTTP server wrapping the Lean 4 kernel.

Vendored from lean-agent-protocol (github.com/Akhatri98/lean-agent-protocol @ 060b07f) and adapted
for Proofread; see ../README.md for the list of changes.

Endpoints:
  GET  /health        → {"status": "ok"|"degraded", "source_hash": "...", "imports": [...], ...}
  POST /verify-batch  → {"results": [{"id", "result": "proved"|"refuted"|"error", "trace"}],
                         "latency_us": N}

/verify-batch takes {"goals": [{"id": str, "prop": str}]}. The worker owns the imports and the
tactic: every goal becomes one line `example : <prop> := by decide +kernel` in a single file that
ends with a sentinel `#print`, and diagnostics from `lean --json` are attributed to goals by line.
A goal is "proved" only when Lean reached the sentinel and the goal's line carries no warning or
error. A refutation message on its line makes it "refuted"; anything else (unknown identifier,
kernel timeout, a diagnostic outside the goal lines, a crash, a missing sentinel) is "error", which
callers treat as a violation (fail closed).

The upstream /verify (raw conjecture text) and /compile-policy (caller-supplied Lean compiled with
`lake build`) are removed: policies are compiled into the image, never at request time.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

POLICY_ENV_DIR = Path(os.environ.get("POLICY_ENV_DIR", "/app/PolicyEnv")).resolve()
LEAN_IMPORTS = [m for m in os.environ.get("LEAN_IMPORTS", "Proofread").split(",") if m]
LEAN_LIB_DIRS = [d for d in os.environ.get("LEAN_LIB_DIRS", "Proofread").split(",") if d]
TACTIC = "decide +kernel"  # kernel reduction only: no native code, no extra axioms
LEAN_TIMEOUT_S = float(os.environ.get("LEAN_TIMEOUT_S", "60"))
MAX_PROCS = int(os.environ.get("LEAN_MAX_PROCS", str(os.cpu_count() or 2)))
MAX_GOALS = 256
MAX_PROP_CHARS = 2_000_000
MAX_BODY_BYTES = 16 * 1024 * 1024
BUILD_ON_START = os.environ.get("LEAN_BUILD_ON_START", "1") == "1"
ELAN_BIN = os.environ.get("ELAN_BIN", str(Path.home() / ".elan" / "bin"))

_env = {**os.environ, "PATH": f"{ELAN_BIN}:{os.environ.get('PATH', '')}"}

LAKE_BUILD_DIR = POLICY_ENV_DIR / ".lake" / "build" / "lib" / "lean"

REFUTED_RE = re.compile(r"^Tactic `decide` proved that the proposition\n.*\nis false$", re.S)
SENTINEL = "proofread:end"
# Goals are rendered by the backend from typed values only, with every character of data inside
# its own char literal, so none of these can occur in a rendered goal. Each would let a goal escape
# its line (comments, a second command, its own proof).
FORBIDDEN_IN_PROP = re.compile(r"[\n\r]|--|/-|-/|\bby\b|\bsorry\b|\badmit\b|#[A-Za-z]|@\[|\bset_option\b|\bnative_decide\b")

_procs = threading.BoundedSemaphore(MAX_PROCS)
_state: dict = {"status": "starting", "source_hash": "", "lean": "lean", "detail": ""}


def source_hash(root: Path = POLICY_ENV_DIR) -> str:
    """Same algorithm as proofread.verify.lean_verifier.lean_source_hash."""
    h = hashlib.sha256()
    files = list(root.glob("*.lean"))
    for d in LEAN_LIB_DIRS:
        files += list(root.glob(f"{d}/*.lean"))
    for p in sorted(files + [root / "lakefile.toml"]):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def _lean_env() -> dict:
    existing = _env.get("LEAN_PATH", "")
    lean_path = f"{LAKE_BUILD_DIR}:{existing}" if existing else str(LAKE_BUILD_DIR)
    return {**_env, "LEAN_PATH": lean_path}


def _resolve_lean() -> str:
    """The toolchain binary named by lean-toolchain (skips the elan proxy on every call)."""
    try:
        r = subprocess.run(["elan", "which", "lean"], capture_output=True, text=True, timeout=60,
                           cwd=str(POLICY_ENV_DIR), env=_env)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "lean"


def lake_build() -> tuple[bool, str]:
    """Run `lake build <libs>` in POLICY_ENV_DIR. Returns (success, output)."""
    proc = subprocess.run(
        ["lake", "build", *LEAN_IMPORTS],
        capture_output=True,
        text=True,
        timeout=1800,
        cwd=str(POLICY_ENV_DIR),
        env=_env,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _header() -> str:
    return "".join(f"import {m}\n" for m in LEAN_IMPORTS)


def _check_goal(g: object) -> str | None:
    if not isinstance(g, dict) or not isinstance(g.get("id"), str) or not isinstance(g.get("prop"), str):
        return "goal must be {id: str, prop: str}"
    prop = g["prop"]
    if not prop or len(prop) > MAX_PROP_CHARS:
        return "prop empty or too long"
    bad = FORBIDDEN_IN_PROP.search(prop)
    if bad:
        return f"prop contains forbidden token {bad.group(0)!r}"
    return None


def run_goals(goals: list[dict]) -> tuple[list[dict], int]:
    """Elaborate all goals in one `lean` process. Returns (per-goal results, latency_us)."""
    header = _header()
    first_line = header.count("\n") + 1
    body = (header + "".join(f"example : {g['prop']} := by {TACTIC}\n" for g in goals)
            + f'#print "{SENTINEL}"\n')
    line_of = {first_line + i: i for i in range(len(goals))}
    sentinel_line = first_line + len(goals)
    reached_end = False

    start_ns = time.monotonic_ns()
    with tempfile.NamedTemporaryFile(suffix=".lean", mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write(body)
        tmp_path = tmp.name
    try:
        with _procs:
            proc = subprocess.run(
                [_state["lean"], "--json", tmp_path],
                capture_output=True,
                text=True,
                timeout=LEAN_TIMEOUT_S,
                cwd=str(POLICY_ENV_DIR),
                env=_lean_env(),
            )
    except subprocess.TimeoutExpired:
        lat = (time.monotonic_ns() - start_ns) // 1000
        return [{"id": g["id"], "result": "error", "trace": f"lean timed out after {LEAN_TIMEOUT_S}s"}
                for g in goals], lat
    finally:
        os.unlink(tmp_path)
    latency_us = (time.monotonic_ns() - start_ns) // 1000

    msgs: dict[int, list[tuple[str, str]]] = {}
    stray: list[str] = []
    for raw in proc.stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            m = json.loads(raw)
            line = int(m["pos"]["line"])
            sev = str(m["severity"])
            data = str(m.get("data", ""))
        except (ValueError, KeyError, TypeError):
            stray.append(raw[:500])
            continue
        if sev == "information":
            reached_end = reached_end or (line == sentinel_line and data == SENTINEL)
            continue
        if line in line_of:
            msgs.setdefault(line_of[line], []).append((sev, data))
        else:
            stray.append(f"line {line}: {sev}: {data[:500]}")
    if proc.stderr.strip():
        stray.append(proc.stderr.strip()[:500])
    if not reached_end:
        stray.append(f"lean did not reach the end of the file (exit {proc.returncode})")
    elif proc.returncode not in (0, 1) or (proc.returncode == 1 and not msgs):
        stray.append(f"lean exit {proc.returncode} inconsistent with diagnostics")

    results = []
    for i, g in enumerate(goals):
        mine = msgs.get(i, [])
        if stray:
            res, trace = "error", "; ".join(stray)[:2000]
        elif not mine:
            res, trace = "proved", ""
        elif all(sev == "error" and REFUTED_RE.match(data) for sev, data in mine):
            res, trace = "refuted", mine[0][1][:2000]
        else:
            res, trace = "error", "; ".join(f"{s}: {d}" for s, d in mine)[:2000]
        results.append({"id": g["id"], "result": res, "trace": trace})
    return results, latency_us


class WorkerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default CLF access log
        pass

    # ------------------------------------------------------------------ helpers

    def send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY_BYTES:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw)

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {
                "status": _state["status"],
                "source_hash": _state["source_hash"],
                "imports": LEAN_IMPORTS,
                "tactic": TACTIC,
                "max_procs": MAX_PROCS,
                "detail": _state["detail"],
            })
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/verify-batch":
            self._handle_verify_batch()
        else:
            self.send_json(404, {"error": "not found"})

    def _handle_verify_batch(self) -> None:
        if _state["status"] != "ok":
            self.send_json(503, {"error": f"worker {_state['status']}: {_state['detail']}"})
            return
        try:
            body = self.read_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        goals = body.get("goals")
        if not isinstance(goals, list) or not goals or len(goals) > MAX_GOALS:
            self.send_json(400, {"error": f"goals must be a list of 1..{MAX_GOALS}"})
            return
        for g in goals:
            err = _check_goal(g)
            if err:
                self.send_json(400, {"error": err})
                return
        try:
            results, latency_us = run_goals(goals)
        except Exception as exc:  # noqa: BLE001 - reported as error, callers fail closed
            self.send_json(200, {
                "results": [{"id": g["id"], "result": "error", "trace": str(exc)[:500]} for g in goals],
                "latency_us": 0,
            })
            return
        self.send_json(200, {"results": results, "latency_us": latency_us,
                             "source_hash": _state["source_hash"]})


# ---------------------------------------------------------------------------- main

# Self-test: one goal the kernel must prove, one it must refute. If the parsing of Lean's output
# drifts (new toolchain, new message text), the worker refuses to serve instead of misreporting.
PROBES = [
    {"id": "probe-true", "prop": "Proofread.codeAttr001C ({ kind := .write } : Proofread.ActionC) = true",
     "expect": "proved"},
    {"id": "probe-false",
     "prop": "Proofread.codeAttr001C ({ kind := .write, attributed := false } : Proofread.ActionC) = true",
     "expect": "refuted"},
]


def startup() -> None:
    try:
        if BUILD_ON_START:
            ok, out = lake_build()
            if not ok:
                _state.update(status="degraded", detail=f"lake build failed: {out[-500:]}")
                print(f"lean-worker: lake build failed:\n{out}", flush=True)
                return
        _state["lean"] = _resolve_lean()
        _state["source_hash"] = source_hash()
        if os.environ.get("LEAN_PROBES", "1") == "1":
            results, lat = run_goals([{"id": p["id"], "prop": p["prop"]} for p in PROBES])
            for p, r in zip(PROBES, results):
                if r["result"] != p["expect"]:
                    _state.update(status="degraded",
                                  detail=f"self-test {p['id']}: expected {p['expect']}, got {r['result']}: {r['trace'][:300]}")
                    print(f"lean-worker: {_state['detail']}", flush=True)
                    return
            print(f"lean-worker: self-test ok ({lat / 1000:.1f}ms wall)", flush=True)
        _state.update(status="ok", detail="")
        print(f"lean-worker: serving {LEAN_IMPORTS} source_hash={_state['source_hash'][:16]} "
              f"lean={_state['lean']} max_procs={MAX_PROCS}", flush=True)
    except Exception as exc:  # noqa: BLE001
        _state.update(status="degraded", detail=f"startup failed: {exc}")
        print(f"lean-worker: {_state['detail']}", flush=True)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 9000))
    server = ThreadingHTTPServer((host, port), WorkerHandler)
    server.daemon_threads = True
    print(f"lean-worker listening on {host}:{port}", flush=True)
    threading.Thread(target=startup, daemon=True).start()
    server.serve_forever()
