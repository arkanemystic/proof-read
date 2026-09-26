"""LapVerifier: base policies decided by the Lean kernel inside the Lean-Agent Protocol lean-worker.

Pipeline per action: trusted Python extracts Bool/Nat facts (proofread/verify/facts.py, reusing
reference.py classification), then the worker is asked to elaborate
    import PolicyEnv.PROOFREAD
    example : PolicyEnv.Proofread.allowed { ...facts... } = true := by decide
"proved" means allowed. "refuted" (decide proved the proposition false) triggers one conjecture per
base policy to name the violated ones. Anything else fails closed (FAILCLOSED): worker down, HTTP
error, timeout, malformed JSON, "error" result, refuted without the decide marker, or an `allowed`
refutation that no single policy explains.

Decisions are cached per distinct fact tuple (the predicate is a pure function of the facts, so an
identical tuple always gets the identical kernel decision); only successful decisions are cached.
The worker's HTTPServer is single threaded, so concurrency is bounded (default 4).
See proofread/verify/LAP_INTERFACE.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from proofread.contracts import BASE_POLICY_IDS, FAILCLOSED_POLICY, Action, Verdict
from proofread.policies import reference as ref

from .facts import LEAN_MODULE, POLICY_PREDICATES, Facts, conjecture, extract_facts

LAP_URL = os.environ.get("PROOFREAD_LAP_URL", "http://127.0.0.1:9100")
POLICY_SOURCE = Path(__file__).resolve().parents[1] / "policies" / "lap" / "PROOFREAD.lean"
POLICY_ID = "PROOFREAD"  # LAP registers it as module PolicyEnv.PROOFREAD
REFUTED_MARKER = "Tactic `decide` proved that the proposition"


def lap_policy_hash(src: Path = POLICY_SOURCE) -> str:
    h = hashlib.sha256()
    h.update(src.read_bytes() + b"\0" + ref.BASE_POLICY_HASH.encode())
    return h.hexdigest()


class LapError(Exception):
    """Any condition under which the verifier cannot decide (maps to FAILCLOSED)."""


@dataclass
class Decision:
    failed: list[str]
    conjectures: list[dict] = field(default_factory=list)  # {conjecture, result, latency_us, rtt_ms}
    rtt_ms: float = 0.0
    cached: bool = False


class LapVerifier:
    provisional = True

    def __init__(self, url: str = LAP_URL, net_allowlist: tuple[str, ...] = (), timeout_s: float = 30.0,
                 concurrency: int = 4, cache: bool = True, register: bool = True) -> None:
        self.url = url.rstrip("/")
        self.net_allowlist = tuple(net_allowlist)
        self.timeout_s = timeout_s
        self.concurrency = max(1, concurrency)
        self.use_cache = cache
        self.register = register
        self.policy_hash = lap_policy_hash()
        self._cache: dict[tuple, list[str]] = {}
        self._registered = False
        self._reg_lock: asyncio.Lock | None = None
        self.last_decisions: list[Decision] = []  # per action of the last check_batch (for replay)

    # ------------------------------------------------------------------ worker I/O

    async def _post(self, client: httpx.AsyncClient, path: str, payload: dict, timeout: float) -> dict:
        try:
            r = await client.post(self.url + path, json=payload, timeout=timeout)
        except (httpx.HTTPError, OSError) as e:
            raise LapError(f"lap worker unreachable or timed out on {path}: {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise LapError(f"lap worker HTTP {r.status_code} on {path}")
        try:
            data = r.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise LapError(f"lap worker malformed JSON on {path}") from e
        if not isinstance(data, dict):
            raise LapError(f"lap worker malformed response on {path}: not an object")
        return data

    async def ensure_registered(self, client: httpx.AsyncClient) -> None:
        """Compile PROOFREAD.lean into the worker's PolicyEnv via POST /compile-policy (LAP's path)."""
        if self._registered or not self.register:
            return
        if self._reg_lock is None:
            self._reg_lock = asyncio.Lock()
        async with self._reg_lock:
            if self._registered:
                return
            data = await self._post(client, "/compile-policy",
                                    {"policy_id": POLICY_ID, "lean_code": POLICY_SOURCE.read_text()},
                                    timeout=max(self.timeout_s, 240.0))
            if data.get("success") is not True or data.get("module_name") != POLICY_ID:
                raise LapError(f"lap policy registration failed: {str(data.get('error'))[:300]}")
            self._registered = True

    async def _verify(self, client: httpx.AsyncClient, sem: asyncio.Semaphore, conj: str) -> tuple[bool, dict]:
        """True = proved, False = refuted by decide. Raises LapError otherwise."""
        async with sem:
            t0 = time.perf_counter()
            data = await self._post(client, "/verify", {"conjecture": conj}, timeout=self.timeout_s)
            rtt = (time.perf_counter() - t0) * 1000.0
        result, trace = data.get("result"), data.get("trace")
        rec = {"conjecture": conj, "result": result, "latency_us": data.get("latency_us"), "rtt_ms": rtt}
        if not isinstance(trace, str) or result not in ("proved", "refuted", "error"):
            raise LapError(f"lap worker malformed verify response: result={result!r}")
        if result == "proved":
            return True, rec
        if result == "refuted" and REFUTED_MARKER in trace and "is false" in trace:
            return False, rec
        raise LapError(f"lap worker could not decide ({result}): {trace[:300]}")

    async def decide(self, client: httpx.AsyncClient, sem: asyncio.Semaphore, facts: Facts) -> Decision:
        if self.use_cache and facts.key() in self._cache:
            return Decision(failed=list(self._cache[facts.key()]), cached=True)
        ok, rec = await self._verify(client, sem, conjecture(facts, "allowed"))
        recs = [rec]
        failed: list[str] = []
        if not ok:
            pids = list(BASE_POLICY_IDS)
            outs = await asyncio.gather(*[self._verify(client, sem, conjecture(facts, POLICY_PREDICATES[p]))
                                          for p in pids])
            for pid, (pok, prec) in zip(pids, outs):
                recs.append(prec)
                if not pok:
                    failed.append(pid)
            if not failed:
                raise LapError("allowed refuted but no base policy refuted (kind out of range?)")
        failed = sorted(failed)
        if self.use_cache:
            self._cache[facts.key()] = failed
        return Decision(failed=failed, conjectures=recs, rtt_ms=sum(r["rtt_ms"] for r in recs))

    # ------------------------------------------------------------------ Verifier protocol

    def _failclosed(self, detail: str, latency_ms: float = 0.0) -> Verdict:
        return Verdict(ok=False, failed_policies=[FAILCLOSED_POLICY], policy_hash=self.policy_hash,
                       provisional=True, source="failclosed", detail=("LAP: " + detail)[:500],
                       latency_ms=latency_ms)

    async def check_batch(self, actions: list[Action]) -> list[Verdict]:
        self.last_decisions = []
        if not actions:
            return []
        t0 = time.perf_counter()
        try:
            facts = [extract_facts(a, self.net_allowlist) for a in actions]
        except Exception as e:  # classification error: fail closed for the whole batch
            return [self._failclosed(f"fact extraction error: {e}") for _ in actions]
        sem = asyncio.Semaphore(self.concurrency)
        try:
            async with httpx.AsyncClient() as client:
                try:
                    await self.ensure_registered(client)
                except LapError as e:
                    ms = (time.perf_counter() - t0) * 1000.0
                    self.last_decisions = [Decision(failed=[FAILCLOSED_POLICY]) for _ in actions]
                    return [self._failclosed(str(e), ms) for _ in actions]
                uniq: dict[tuple, Facts] = {}
                for f in facts:
                    uniq.setdefault(f.key(), f)
                keys = list(uniq)
                outs = await asyncio.gather(*[self.decide(client, sem, uniq[k]) for k in keys],
                                            return_exceptions=True)
        except Exception as e:  # pragma: no cover - defensive
            return [self._failclosed(f"unexpected: {type(e).__name__}: {e}") for _ in actions]
        by_key = dict(zip(keys, outs))
        verdicts: list[Verdict] = []
        seen: set[tuple] = set()
        for f in facts:
            d = by_key[f.key()]
            if isinstance(d, BaseException):
                self.last_decisions.append(Decision(failed=[FAILCLOSED_POLICY]))
                verdicts.append(self._failclosed(str(d) if isinstance(d, LapError) else f"{type(d).__name__}: {d}",
                                                 0.0))
                continue
            first = f.key() not in seen
            seen.add(f.key())
            dd = d if first else Decision(failed=d.failed, cached=True)
            self.last_decisions.append(dd)
            verdicts.append(Verdict(ok=not d.failed, failed_policies=list(d.failed), policy_hash=self.policy_hash,
                                    provisional=True, source="lap", latency_ms=dd.rtt_ms,
                                    detail="cached" if dd.cached else ""))
        return verdicts


def lap_available(url: str = LAP_URL, timeout_s: float = 3.0) -> bool:
    try:
        r = httpx.get(url.rstrip("/") + "/health", timeout=timeout_s)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        return False


__all__ = ["LapVerifier", "LapError", "lap_available", "lap_policy_hash", "LAP_URL", "LEAN_MODULE"]
