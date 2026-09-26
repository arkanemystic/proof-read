"""Docker + gVisor episode sandbox implementing contracts.Sandbox.

One container per episode:
  docker run -d --runtime=runsc --read-only --network none
    --tmpfs /workspace (rw, exec, 256 MiB, owned by uid 1000)  --tmpfs /tmp (rw, exec, 256 MiB)
    --cpus 1 --memory 1g --pids-limit 4096 --ulimit nproc=256 --cap-drop ALL (+ the few caps the root-side capture needs)
    --security-opt no-new-privileges  image  sleep infinity
Agent commands run as uid 1000 (root wrapper + setpriv), under `timeout -s KILL`. After every command all
uid-1000 processes are SIGKILLed (root `pkill -STOP/-KILL -u 1000`, tini as PID 1 reaps), which also reaps detached background
jobs (setsid/nohup), then the capture walker (root, `python3 -I -S`, read-only image path) snapshots
/workspace, /tmp and /dev/shm and the diff against the previous snapshot becomes the step's Actions.
`write` goes through the same path (content streamed into the container, then the same capture).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import re
import tarfile
import time
import uuid
import weakref
from pathlib import Path
from typing import Any

from proofread.actions.canonicalize import TMP, WORKSPACE, canon_path, decode_bytes, is_under
from proofread.actions.effects import (
    ALWAYS_REHASH_GLOBS,
    Snapshot,
    diff_snapshots,
    merge_prev,
    scan_output,
    statkeys,
)
from proofread.contracts import MAX_CONCURRENT_SANDBOXES, Action, RunResult, Task

DEFAULT_IMAGE = "proofread-sandbox:py312"
SANDBOX_DIR = Path(__file__).resolve().parent
CAPTURE_ROOTS = (WORKSPACE, TMP, "/dev/shm")
AGENT_UID = "1000:1000"
OUTPUT_CAP = 200_000
_KILL_ALL = "pkill -STOP -u 1000; pkill -KILL -u 1000; pkill -KILL -u 1000"
_RUN_WRAPPER = (
    'timeout -s KILL "$1" setpriv --reuid=1000 --regid=1000 --clear-groups /bin/bash -c "$2"; rc=$?; '
    + _KILL_ALL + "; exit $rc"
)
AGENT_ENV = {
    "HOME": "/tmp",
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "LANG": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
}

# ---------------------------------------------------------------------------------------------
# Global concurrency cap
# ---------------------------------------------------------------------------------------------

SANDBOX_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_SANDBOXES)
_loop_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _semaphore() -> asyncio.Semaphore:
    """SANDBOX_SEMAPHORE for the loop it is bound to; a fresh per-loop semaphore for other loops
    (tests call asyncio.run repeatedly, and an asyncio.Semaphore cannot cross loops)."""
    loop = asyncio.get_running_loop()
    bound = getattr(SANDBOX_SEMAPHORE, "_loop", None)
    if bound is None or bound is loop:
        return SANDBOX_SEMAPHORE
    sem = _loop_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_SANDBOXES)
        _loop_semaphores[loop] = sem
    return sem


@contextlib.asynccontextmanager
async def sandbox_slot():
    """`async with sandbox_slot():` around the whole lifetime of a sandbox (start .. stop)."""
    sem = _semaphore()
    async with sem:
        yield


# ---------------------------------------------------------------------------------------------
# docker CLI helpers
# ---------------------------------------------------------------------------------------------


class SandboxError(RuntimeError):
    pass


async def _read_capped(stream: asyncio.StreamReader | None, cap: int) -> bytes:
    """Read a stream to EOF, keeping at most cap bytes (the rest is drained and dropped)."""
    if stream is None:
        return b""
    kept = bytearray()
    dropped = 0
    while True:
        chunk = await stream.read(1 << 16)
        if not chunk:
            break
        room = cap - len(kept)
        if room > 0:
            kept += chunk[:room]
        dropped += max(0, len(chunk) - max(room, 0))
    if dropped:
        kept += f"\n[... {dropped} bytes truncated]".encode()
    return bytes(kept)


async def _docker(*args: str, stdin: bytes | None = None, timeout: float = 120.0,
                  cap: int = 256 * 1024 * 1024) -> tuple[int, bytes, bytes, bool]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    async def feed() -> None:
        if stdin is not None and proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(stdin)
                await proc.stdin.drain()
            with contextlib.suppress(Exception):
                proc.stdin.close()

    async def body() -> tuple[bytes, bytes]:
        _, out, err = await asyncio.gather(feed(), _read_capped(proc.stdout, cap), _read_capped(proc.stderr, cap))
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(body(), timeout=timeout)
        return proc.returncode or 0, out, err, False
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return 124, b"", b"", True


def image_hash() -> str:
    h = hashlib.sha256()
    for name in ("Dockerfile", "snap.py"):
        h.update((SANDBOX_DIR / name).read_bytes())
    return h.hexdigest()[:16]


_build_lock: asyncio.Lock | None = None


async def build_image(image: str = DEFAULT_IMAGE, force: bool = False) -> str:
    """Build the sandbox image if missing or stale (label proofread.hash != sources). Returns tag."""
    want = image_hash()
    if not force:
        rc, out, _, _ = await _docker("image", "inspect", "-f", '{{index .Config.Labels "proofread.hash"}}', image)
        if rc == 0 and out.decode().strip() == want:
            return image
    rc, out, err, _ = await _docker("build", "-q", "--label", f"proofread.hash={want}", "-t", image,
                                    str(SANDBOX_DIR), timeout=900)
    if rc != 0:
        raise SandboxError(f"docker build failed: {err.decode(errors='replace')[-2000:]}")
    return image


async def ensure_image(image: str = DEFAULT_IMAGE) -> str:
    global _build_lock
    if _build_lock is None:
        _build_lock = asyncio.Lock()
    try:
        async with _build_lock:
            return await build_image(image)
    except RuntimeError as e:  # lock bound to a different loop
        if "different event loop" not in str(e) and "attached to a different loop" not in str(e):
            raise
        _build_lock = asyncio.Lock()
        async with _build_lock:
            return await build_image(image)


def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", s)[:40]


def _tar_files(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        dirs: set[str] = set()
        for rel in sorted(files):
            parts = rel.strip("/").split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    ti = tarfile.TarInfo(d)
                    ti.type = tarfile.DIRTYPE
                    ti.mode, ti.uid, ti.gid, ti.mtime = 0o755, 1000, 1000, int(time.time())
                    tf.addfile(ti)
            data = files[rel].encode("utf-8")
            ti = tarfile.TarInfo(rel.strip("/"))
            ti.size, ti.mode, ti.uid, ti.gid, ti.mtime = len(data), 0o644, 1000, 1000, int(time.time())
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------------------------


class DockerSandbox:
    def __init__(
        self,
        episode_id: str,
        *,
        image: str = DEFAULT_IMAGE,
        runtime: str = "runsc",
        cpus: float = 1.0,
        memory: str = "1g",
        pids_limit: int = 4096,
        nproc: int = 256,
        workspace_size: str = "256m",
        tmp_size: str = "256m",
        build: bool = True,
    ) -> None:
        self.episode_id = episode_id
        self.image = image
        self.runtime = runtime
        self.cpus = cpus
        self.memory = memory
        self.pids_limit = pids_limit
        self.nproc = nproc
        self.workspace_size = workspace_size
        self.tmp_size = tmp_size
        self.build = build
        self.name = f"pf-{_sanitize(episode_id)}-{uuid.uuid4().hex[:8]}"
        self.started = False
        self._step = 0
        self._manifest: list[str] = []
        self._prev: Snapshot = {}
        self._checkpoints: dict[str, bytes] = {}
        self.capture_errors: list[str] = []

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self, task: Task) -> None:
        if self.build:
            await ensure_image(self.image)
        args = [
            "run", "-d", "--name", self.name, "--label", "proofread=1",
            f"--runtime={self.runtime}", "--read-only", "--network", "none",
            "--tmpfs", f"{WORKSPACE}:rw,exec,size={self.workspace_size},uid=1000,gid=1000,mode=0755",
            "--tmpfs", f"{TMP}:rw,exec,size={self.tmp_size},mode=1777",
            "--cpus", str(self.cpus), "--memory", self.memory, "--memory-swap", self.memory,
            # Under runsc the host pids cgroup also counts sentry threads; a low value crashes the
            # sandbox. The real per-agent process cap is RLIMIT_NPROC (enforced by gVisor).
            "--pids-limit", str(self.pids_limit), "--ulimit", f"nproc={self.nproc}:{self.nproc}",
            "--cap-drop", "ALL",
            "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "DAC_READ_SEARCH",
            "--cap-add", "FOWNER", "--cap-add", "KILL", "--cap-add", "SETUID", "--cap-add", "SETGID",
            "--security-opt", "no-new-privileges",
            "--init", "--user", "0:0", "--workdir", WORKSPACE,
            self.image, "sleep", "infinity",
        ]
        rc, _, err, _ = await _docker(*args, timeout=180)
        if rc != 0:
            raise SandboxError(f"docker run failed: {err.decode(errors='replace')[-2000:]}")
        self.started = True
        self._manifest = task.manifest_abs()
        if task.files:
            rc, _, err, _ = await _docker("exec", "-i", "-u", "0:0", self.name, "tar", "-C", WORKSPACE,
                                          "--same-owner", "-xpf", "-", stdin=_tar_files(task.files))
            if rc != 0:
                raise SandboxError(f"populate failed: {err.decode(errors='replace')[-2000:]}")
        self._prev = await self._snap()

    async def stop(self) -> None:
        if self.started:
            await _docker("rm", "-f", self.name, timeout=60)
            self.started = False

    async def __aenter__(self) -> "DockerSandbox":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # -- low-level ---------------------------------------------------------------------------

    def _env_args(self) -> list[str]:
        out: list[str] = []
        for k, v in AGENT_ENV.items():
            out += ["-e", f"{k}={v}"]
        return out

    async def _kill_agent(self) -> None:
        """SIGKILL every uid-1000 process (detached background jobs included)."""
        for _ in range(3):
            # Root-side pkill: under gVisor a uid-1000 `kill -9 -1` does not reach processes started by
            # other `docker exec` sessions. --init (tini) reaps the zombies so pgrep sees them go.
            await _docker("exec", "-u", "0:0", self.name, "/bin/sh", "-c", _KILL_ALL, timeout=30)
            rc, out, _, _ = await _docker("exec", "-u", "0:0", self.name, "pgrep", "-u", "1000", timeout=30)
            if rc != 0 or not out.strip():
                return
            await asyncio.sleep(0.1)

    async def _snap_raw(self, prev: Snapshot) -> Snapshot:
        req = {
            "roots": list(CAPTURE_ROOTS),
            "prev": statkeys(prev),
            "always": list(self._manifest),
            "always_globs": list(ALWAYS_REHASH_GLOBS),
            "content_max": 2_000_000,
            "max_entries": 50_000,
        }
        rc, out, err, to = await _docker(
            "exec", "-i", "-u", "0:0", self.name, "python3", "-I", "-S", "/opt/proofread/snap.py",
            stdin=json.dumps(req).encode(), timeout=120,
        )
        if rc != 0 or to:
            raise SandboxError(f"snapshot failed rc={rc} timeout={to}: {err.decode(errors='replace')[-500:]}")
        data = json.loads(out)
        if data.get("truncated"):
            raise SandboxError("snapshot truncated (too many entries)")
        return merge_prev(prev, data["entries"])

    async def _snap(self) -> Snapshot:
        return await self._snap_raw(self._prev)

    def _capture_failed(self, step: int, why: str) -> list[Action]:
        """Fail closed: an unknown effect is reported as an unattributed out-of-scope attempt."""
        self.capture_errors.append(why)
        return [Action(episode_id=self.episode_id, step=step, kind="attempt_outside", path="",
                       attributed=False, cmd=f"capture failed: {why}"[:500], protected_extra=self._manifest)]

    async def _capture(self, step: int, cmd: str = "") -> list[Action]:
        await self._kill_agent()
        try:
            cur = await self._snap()
        except (SandboxError, ValueError, KeyError) as e:
            await self._kill_agent()
            try:
                cur = await self._snap()
            except (SandboxError, ValueError, KeyError) as e2:
                return self._capture_failed(step, f"{e}; {e2}")
        acts = diff_snapshots(self._prev, cur, episode_id=self.episode_id, step=step, attributed=True,
                              protected_extra=self._manifest, cmd=cmd)
        self._prev = cur
        return acts

    # -- Sandbox protocol --------------------------------------------------------------------

    async def run(self, cmd: str, timeout_s: float = 60.0) -> RunResult:
        self._step += 1
        step = self._step
        t0 = time.monotonic()
        t = max(1, int(timeout_s))
        # Root wrapper: run the command as uid 1000 (setpriv), then STOP+KILL every uid-1000 process
        # so background jobs cannot hold the output pipes open or outlive the step.
        rc, out, err, host_to = await _docker(
            "exec", "-u", "0:0", "-w", WORKSPACE, *self._env_args(), self.name,
            "/bin/sh", "-c", _RUN_WRAPPER, "sh", str(t), cmd,
            timeout=t + 20, cap=OUTPUT_CAP,
        )
        timed_out = host_to or rc in (124, 137) and (time.monotonic() - t0) >= t - 0.5
        stdout = out.decode("utf-8", errors="replace")
        stderr = err.decode("utf-8", errors="replace")
        acts = [Action(episode_id=self.episode_id, step=step, kind="exec", cmd=cmd, protected_extra=self._manifest)]
        acts += scan_output(stdout + "\n" + stderr, cmd, episode_id=self.episode_id, step=step,
                            protected_extra=self._manifest)
        acts += await self._capture(step)
        return RunResult(exit_code=rc, stdout=stdout[:OUTPUT_CAP], stderr=stderr[:OUTPUT_CAP],
                         timed_out=bool(timed_out), duration_s=time.monotonic() - t0, actions=acts)

    async def write(self, path: str, content: str) -> list[Action]:
        self._step += 1
        step = self._step
        p = canon_path(path)
        rc, out, err, _ = await _docker(
            "exec", "-i", "-u", AGENT_UID, "-w", WORKSPACE, *self._env_args(), self.name,
            "/bin/sh", "-c", 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"', "sh", p,
            stdin=content.encode("utf-8"), timeout=60,
        )
        acts: list[Action] = []
        if rc != 0:
            text = err.decode("utf-8", errors="replace")
            acts += scan_output(text, "", episode_id=self.episode_id, step=step, protected_extra=self._manifest)
            if not (is_under(p, WORKSPACE) or is_under(p, TMP)) and not any(a.kind == "attempt_outside" for a in acts):
                acts.append(Action(episode_id=self.episode_id, step=step, kind="attempt_outside", path=p,
                                   protected_extra=self._manifest))
        acts += await self._capture(step)
        return acts

    async def read(self, path: str) -> str:
        p = canon_path(path)
        rc, out, err, _ = await _docker("exec", "-u", AGENT_UID, self.name, "cat", "--", p, timeout=60)
        if rc != 0:
            raise FileNotFoundError(f"{p}: {err.decode(errors='replace').strip()}")
        return decode_bytes(out, p)

    async def snapshot(self) -> dict[str, str]:
        cur = await self._snap_raw(self._prev)
        out: dict[str, str] = {}
        for p, e in cur.items():
            if e.get("t") == "f":
                out[p] = e.get("h", "")
            elif e.get("t") == "l":
                out[p] = f"symlink:{e.get('target')}:{e.get('rh')}"
            elif e.get("t") == "o":
                out[p] = "special"
        return out

    async def _tar_workspace(self) -> bytes:
        rc, out, err, _ = await _docker("exec", "-u", "0:0", self.name, "tar", "-C", WORKSPACE, "-cf", "-", ".",
                                        timeout=120)
        if rc != 0:
            raise SandboxError(f"tar failed: {err.decode(errors='replace')[-500:]}")
        return out

    async def checkpoint(self) -> str:
        await self._kill_agent()
        tok = f"cp{len(self._checkpoints)}-{uuid.uuid4().hex[:6]}"
        self._checkpoints[tok] = await self._tar_workspace()
        return tok

    async def restore(self, token: str) -> None:
        data = self._checkpoints[token]
        await self._kill_agent()
        rc, _, err, _ = await _docker(
            "exec", "-i", "-u", "0:0", self.name, "/bin/sh", "-c",
            f"find {WORKSPACE} -mindepth 1 -delete && tar -C {WORKSPACE} --same-owner -xpf -",
            stdin=data, timeout=120,
        )
        if rc != 0:
            raise SandboxError(f"restore failed: {err.decode(errors='replace')[-500:]}")
        self._prev = await self._snap()

    async def final_diff(self) -> list[Action]:
        await self._kill_agent()
        try:
            cur = await self._snap_raw(self._prev)
        except (SandboxError, ValueError, KeyError) as e:
            return self._capture_failed(-1, str(e))
        return diff_snapshots(self._prev, cur, episode_id=self.episode_id, step=-1, attributed=False,
                              protected_extra=self._manifest)

    async def export_files(self) -> dict[str, str]:
        data = await self._tar_workspace()
        out: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
            for m in tf.getmembers():
                if not m.isreg():
                    continue
                rel = canon_path(m.name, cwd=WORKSPACE)[len(WORKSPACE) + 1:]
                if not rel or any(part in ("__pycache__", ".pytest_cache") for part in rel.split("/")):
                    continue
                f = tf.extractfile(m)
                if f is not None:
                    out[rel] = decode_bytes(f.read(), rel)
        return out

    # -- extras (not in the protocol) --------------------------------------------------------

    async def exec_root(self, cmd: str, timeout_s: float = 60.0) -> tuple[int, str]:
        """Out-of-band root command, bypassing capture. For graders/tests only, never the agent."""
        rc, out, err, _ = await _docker("exec", "-u", "0:0", self.name, "/bin/sh", "-c", cmd, timeout=timeout_s)
        return rc, (out + err).decode("utf-8", errors="replace")


async def cleanup_stale(prefix: str = "pf-") -> int:
    """Remove leftover proofread containers (label proofread=1)."""
    rc, out, _, _ = await _docker("ps", "-aq", "--filter", "label=proofread=1")
    ids = out.decode().split()
    if ids:
        await _docker("rm", "-f", *ids, timeout=120)
    return len(ids)


__all__ = ["DockerSandbox", "SANDBOX_SEMAPHORE", "sandbox_slot", "build_image", "ensure_image", "SandboxError",
           "cleanup_stale", "DEFAULT_IMAGE"]
