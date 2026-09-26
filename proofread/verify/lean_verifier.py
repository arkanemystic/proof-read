"""LeanVerifier: evaluates the Lean 4 policies (proofread/policies/lean) via the compiled
`policycheck` JSON-lines executable.

The bridge applies only Unicode table lookups that Lean core lacks: NFC on paths, NFKC fold of added
lines, str.lower() on hosts, and mapping non-ASCII non-word characters in lines to U+0001 (every
policy literal is ASCII, so this preserves all matches). All classification happens in Lean.

Pool of persistent worker processes, bounded concurrency, per-chunk timeout, restart on worker
death, fail closed on any error, cache keyed by canonical action JSON + policy hash + allowlist.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path

from proofread.contracts import BASE_POLICY_IDS, FAILCLOSED_POLICY, Action, AddedPolicy, Verdict

LEAN_DIR = Path(__file__).resolve().parents[1] / "policies" / "lean"
LEAN_EXE = LEAN_DIR / ".lake" / "build" / "bin" / "policycheck"

# reference.BASE_POLICY_HASH of the spec the Lean policies were aligned to. If reference.py changes,
# LeanVerifier fails closed (policy hash mismatch) until the Lean port is re-aligned and re-tested.
LEAN_SPEC_HASH = "43854fa7e8ca27bf8c3155f0987e138e0da747a54308714f51c937c11ff06ba7"


def lean_source_hash(root: Path = LEAN_DIR) -> str:
    h = hashlib.sha256()
    for p in sorted(list(root.glob("*.lean")) + list(root.glob("Proofread/*.lean")) + [root / "lakefile.toml"]):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def build_lean(timeout_s: float = 600.0) -> bool:
    try:
        r = subprocess.run(["lake", "build"], cwd=LEAN_DIR, capture_output=True, timeout=timeout_s)
        return r.returncode == 0 and LEAN_EXE.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def _project_line(s: str) -> str:
    out = []
    for ch in s:
        if ord(ch) < 128 or ch.isspace() or ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("\x01")
    return "".join(out)


def encode_action(a: Action, allow: tuple[str, ...] = ()) -> dict:
    nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
    return {
        "kind": a.kind,
        "path": nfc(a.path),
        "dst": nfc(a.dst),
        "added_lines": [_project_line(x) for x in a.added_lines],
        "added_lines_nfkc": [_project_line(unicodedata.normalize("NFKC", x)) for x in a.added_lines],
        "section": a.section,
        "host": (a.host or "").lower(),
        "attributed": a.attributed,
        "protected_extra": [nfc(x) for x in a.protected_extra],
    }


def _failclosed(policy_hash: str, detail: str, latency_ms: float = 0.0) -> Verdict:
    return Verdict(ok=False, failed_policies=[FAILCLOSED_POLICY], policy_hash=policy_hash, provisional=True,
                   source="failclosed", detail=detail[:500], latency_ms=latency_ms)


class _Worker:
    def __init__(self, exe: Path) -> None:
        self.exe = exe
        self.proc: asyncio.subprocess.Process | None = None
        self.reaping: list[asyncio.Task] = []

    async def ensure(self) -> asyncio.subprocess.Process:
        if self.proc is None or self.proc.returncode is not None:
            self.proc = await asyncio.create_subprocess_exec(
                str(self.exe), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=64 * 1024 * 1024)
        return self.proc

    def kill(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, RuntimeError):
                pass
        try:  # reap in the background so the transport is closed on this loop
            self.reaping.append(asyncio.get_running_loop().create_task(proc.wait()))
        except RuntimeError:
            pass

    async def run(self, payloads: list[dict], timeout_s: float) -> list[list[str]]:
        """Returns one failed-policy list per payload; raises on any protocol problem."""
        proc = await self.ensure()
        assert proc.stdin is not None and proc.stdout is not None
        data = "".join(json.dumps({"id": i, "allow": p.pop("_allow"), "action": p}, ensure_ascii=False) + "\n"
                       for i, p in enumerate(payloads)).encode("utf-8")

        async def _io() -> list[list[str]]:
            proc.stdin.write(data)
            await proc.stdin.drain()
            out: list[list[str]] = []
            for i in range(len(payloads)):
                line = await proc.stdout.readline()
                if not line:
                    raise RuntimeError("worker exited")
                msg = json.loads(line)
                if not isinstance(msg, dict) or msg.get("id") != i:
                    raise RuntimeError("worker response id mismatch")
                if "error" in msg:
                    raise RuntimeError(f"worker error: {msg['error']}")
                failed = msg.get("failed")
                if not isinstance(failed, list) or not all(f in BASE_POLICY_IDS for f in failed):
                    raise RuntimeError("malformed worker response")
                out.append(sorted(failed))
            return out

        return await asyncio.wait_for(_io(), timeout_s)


class LeanVerifier:
    source = "lean"

    def __init__(self, added_policies: list[AddedPolicy] | None = None, net_allowlist: tuple[str, ...] = (),
                 workers: int = 4, chunk_size: int = 64, timeout_s: float = 10.0, retries: int = 1,
                 cache_size: int = 100_000, exe: Path | None = None, build: bool = True) -> None:
        from proofread.policies import reference

        self.added_policies = list(added_policies or [])
        self.net_allowlist = tuple(net_allowlist)
        self.policy_hash: str = reference.BASE_POLICY_HASH
        self.spec_hash: str = LEAN_SPEC_HASH
        self.provisional = True  # not biject-api; see notes/W3.md
        self.exe = Path(exe) if exe else LEAN_EXE
        if exe is None and build and not self.exe.exists():
            build_lean()
        self.lean_source_hash = lean_source_hash() if exe is None else ""
        self.timeout_s = timeout_s
        self.retries = retries
        self.chunk_size = chunk_size
        self._workers = [_Worker(self.exe) for _ in range(max(1, workers))]
        self._free: asyncio.Queue[_Worker] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cache: OrderedDict[str, list[str]] = OrderedDict()
        self._cache_size = cache_size
        self._ref = reference

    def _key(self, a: Action) -> str:
        return hashlib.sha256((self.policy_hash + "\0" + json.dumps(self.net_allowlist) + "\0" +
                               a.canonical_json()).encode("utf-8")).hexdigest()

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._free is None or self._loop is not loop:
            for w in self._workers:
                w.kill()
            self._loop = loop
            self._free = asyncio.Queue()
            for w in self._workers:
                self._free.put_nowait(w)

    async def _chunk(self, actions: list[Action]) -> list[list[str]] | str:
        assert self._free is not None
        last = ""
        for _ in range(self.retries + 1):
            w = await self._free.get()
            try:
                payloads = []
                for a in actions:
                    p = encode_action(a)
                    p["_allow"] = [h.lower() for h in self.net_allowlist]
                    payloads.append(p)
                return await w.run(payloads, self.timeout_s)
            except asyncio.TimeoutError:
                last = "lean worker timeout"
                w.kill()
            except Exception as e:  # noqa: BLE001 - every failure fails closed
                last = f"lean worker failure: {type(e).__name__}: {e}"
                w.kill()
            finally:
                self._free.put_nowait(w)
        return last

    async def check_batch(self, actions: list[Action]) -> list[Verdict]:
        t0 = time.perf_counter()
        if not self.exe.exists():
            return [_failclosed(self.policy_hash, "policycheck executable missing") for _ in actions]
        if self.policy_hash != self.spec_hash:
            return [_failclosed(self.policy_hash, "policy hash mismatch: Lean port not aligned to reference spec")
                    for _ in actions]
        self._bind_loop()
        results: list[list[str] | str | None] = [None] * len(actions)
        todo: list[int] = []
        for i, a in enumerate(actions):
            hit = self._cache.get(self._key(a))
            if hit is not None:
                results[i] = hit
            else:
                todo.append(i)
        chunks = [todo[k:k + self.chunk_size] for k in range(0, len(todo), self.chunk_size)]
        outs = await asyncio.gather(*(self._chunk([actions[i] for i in c]) for c in chunks))
        for c, out in zip(chunks, outs):
            for j, i in enumerate(c):
                if isinstance(out, str):
                    results[i] = out
                else:
                    results[i] = out[j]
                    self._cache[self._key(actions[i])] = out[j]
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        lat = (time.perf_counter() - t0) * 1000.0 / max(1, len(actions))
        verdicts: list[Verdict] = []
        for a, r in zip(actions, results):
            if isinstance(r, str) or r is None:
                verdicts.append(_failclosed(self.policy_hash, r or "no result", lat))
                continue
            failed = list(r)
            if self.added_policies:
                try:
                    failed += [p for p in self._ref.evaluate_added(a, self.added_policies) if p not in failed]
                except Exception as e:  # noqa: BLE001
                    verdicts.append(_failclosed(self.policy_hash, f"added policy error: {e}", lat))
                    continue
            verdicts.append(Verdict(ok=not failed, failed_policies=failed, policy_hash=self.policy_hash,
                                    provisional=True, source="lean", latency_ms=lat))
        return verdicts

    async def aclose(self) -> None:
        for w in self._workers:
            w.kill()
            tasks, w.reaping = w.reaping, []
            if tasks:
                await asyncio.wait(tasks, timeout=2.0)

    def __del__(self) -> None:  # best effort
        for w in getattr(self, "_workers", []):
            try:
                w.kill()
            except Exception:  # noqa: BLE001
                pass
