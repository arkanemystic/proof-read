"""BijectVerifier: client for a local biject-api (/api/verify), implementing contracts.Verifier.

Status (see verify/BIJECT_INTERFACE.md and notes/W3.md): biject-api cannot currently evaluate the
Proofread policies, so make_verifier("auto") falls back. This client is complete on the transport
side: bounded concurrency, in-batch dedupe, retries with backoff, timeouts, policy-hash pinning via
the registry, optional Ed25519 signature check, cache keyed by canonical action JSON + policy hash,
and fail closed on every error path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx

from proofread.contracts import BASE_POLICY_IDS, FAILCLOSED_POLICY, Action, AddedPolicy, Verdict

from .lean_verifier import encode_action

DEFAULT_URL = "http://127.0.0.1:18002"
TOOL_NAME = "proofread_action"
LOCAL_ENV = Path(__file__).resolve().parents[2] / "biject-local" / ".env"
_KEY_SAFE = re.compile(r"[^a-zA-Z0-9_\-]")


def _local_env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in LOCAL_ENV.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _default_api_key() -> str:
    key = os.environ.get("BIJECT_API_KEY") or _local_env().get("BIJECT_API_KEYS", "")
    return key.split(",")[0].strip()


def _expected_hash() -> str:
    from proofread.policies import reference

    return reference.BASE_POLICY_HASH


class BijectVerifier:
    source = "biject"

    def __init__(self, base_url: str | None = None, *, api_key: str | None = None,
                 expected_policy_hash: str | None = None, added_policies: list[AddedPolicy] | None = None,
                 net_allowlist: tuple[str, ...] = (), concurrency: int = 8, timeout_s: float = 5.0,
                 retries: int = 2, backoff_s: float = 0.05, verify_pubkey_b64: str | None = None,
                 agent_id: str = "proofread", transport: httpx.AsyncBaseTransport | None = None,
                 cache_size: int = 100_000, hash_ttl_s: float = 60.0) -> None:
        self.base_url = (base_url or os.environ.get("BIJECT_URL") or DEFAULT_URL).rstrip("/")
        self.api_key = _default_api_key() if api_key is None else api_key
        self.policy_hash: str = expected_policy_hash or _expected_hash()
        self.provisional = False
        self.added_policies = list(added_policies or [])
        self.net_allowlist = tuple(net_allowlist)
        self.concurrency = concurrency
        self.timeout_s = timeout_s
        self.retries = retries
        self.backoff_s = backoff_s
        self.verify_pubkey_b64 = verify_pubkey_b64 or os.environ.get("BIJECT_VERIFY_PUBKEY") or None
        self.agent_id = agent_id
        self._transport = transport
        self._cache: OrderedDict[str, Verdict] = OrderedDict()
        self._cache_size = cache_size
        self._hash_ok_until = 0.0
        self._hash_ttl_s = hash_ttl_s
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---------------------------------------------------------------- plumbing
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _get_client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._loop = loop
            self._client = httpx.AsyncClient(base_url=self.base_url, headers=self._headers(),
                                             timeout=httpx.Timeout(self.timeout_s), transport=self._transport)
        return self._client

    def _key(self, a: Action) -> str:
        return hashlib.sha256((self.policy_hash + "\0" + json.dumps(self.net_allowlist) + "\0" +
                               a.canonical_json()).encode("utf-8")).hexdigest()

    def _fc(self, detail: str, latency_ms: float = 0.0) -> Verdict:
        return Verdict(ok=False, failed_policies=[FAILCLOSED_POLICY], policy_hash=self.policy_hash,
                       source="failclosed", detail=detail[:500], latency_ms=latency_ms)

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        """Retries transport errors, 429 and 5xx; raises the last error."""
        client = self._get_client()
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = await asyncio.wait_for(client.request(method, url, **kw), self.timeout_s)
                if r.status_code == 429 or r.status_code >= 500:
                    last = RuntimeError(f"HTTP {r.status_code}")
                else:
                    return r
            except (httpx.HTTPError, asyncio.TimeoutError, OSError) as e:
                last = e
            if attempt < self.retries:
                await asyncio.sleep(self.backoff_s * (2 ** attempt))
        raise RuntimeError(f"biject request failed: {type(last).__name__}: {last}")

    # ---------------------------------------------------------------- policy hash pinning
    async def check_policy_hash(self) -> bool:
        """The registry must hold a policy for TOOL_NAME whose source_hash equals our pinned hash."""
        if time.monotonic() < self._hash_ok_until:
            return True
        r = await self._request("GET", "/api/policies")
        if r.status_code != 200:
            return False
        body = r.json()
        pols = body.get("policies") if isinstance(body, dict) else None
        if not isinstance(pols, dict):
            return False
        ok = any(isinstance(m, dict) and m.get("source_hash") == self.policy_hash and
                 TOOL_NAME in (m.get("applies_to_tools") or [TOOL_NAME]) for m in pols.values())
        if ok:
            self._hash_ok_until = time.monotonic() + self._hash_ttl_s
        return ok

    # ---------------------------------------------------------------- signatures
    def _check_signature(self, body: dict[str, Any]) -> str | None:
        """Returns an error string, or None when OK / not configured."""
        if not self.verify_pubkey_b64:
            return None
        sig, entry_hash = body.get("signature"), body.get("entry_hash")
        if not isinstance(sig, str) or not isinstance(entry_hash, str):
            return "signature missing"
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(self.verify_pubkey_b64))
            raw = bytes.fromhex(sig) if re.fullmatch(r"[0-9a-fA-F]+", sig) else base64.b64decode(sig)
            pub.verify(raw, entry_hash.encode())
        except Exception:  # noqa: BLE001
            return "signature invalid"
        return None

    # ---------------------------------------------------------------- verify one
    def _payload(self, a: Action) -> dict[str, Any]:
        enc = encode_action(a)
        params: dict[str, Any] = {
            "kind": enc["kind"], "path": enc["path"], "dst": enc["dst"], "section": enc["section"],
            "host": enc["host"], "attributed": enc["attributed"],
            "added_lines_json": json.dumps(enc["added_lines"], ensure_ascii=False),
            "added_lines_nfkc_json": json.dumps(enc["added_lines_nfkc"], ensure_ascii=False),
            "protected_extra_json": json.dumps(enc["protected_extra"], ensure_ascii=False),
            "net_allowlist_json": json.dumps([h.lower() for h in self.net_allowlist], ensure_ascii=False),
            "policy_hash": self.policy_hash,
        }
        return {"tool_name": TOOL_NAME, "agent_id": self.agent_id, "call_id": a.digest()[:48],
                "session_id": (_KEY_SAFE.sub("-", a.episode_id) or "default")[:64], "params": params}

    def _parse(self, r: httpx.Response, latency_ms: float) -> Verdict:
        if r.status_code != 200:
            return self._fc(f"biject HTTP {r.status_code}", latency_ms)
        try:
            body = r.json()
        except (ValueError, json.JSONDecodeError):
            return self._fc("malformed JSON from biject", latency_ms)
        if not isinstance(body, dict) or not isinstance(body.get("verdict"), str):
            return self._fc("malformed verdict from biject", latency_ms)
        if "policy_hash" in body and body["policy_hash"] != self.policy_hash:
            return self._fc("policy hash mismatch", latency_ms)
        err = self._check_signature(body)
        if err:
            return self._fc(err, latency_ms)
        verdict = body["verdict"]
        sig = body.get("signature") if isinstance(body.get("signature"), str) else ""
        if verdict == "allowed":
            return Verdict(ok=True, policy_hash=self.policy_hash, signature=sig, latency_ms=latency_ms,
                           source="biject")
        if verdict == "blocked":
            failed = body.get("failed_policies")
            if not (isinstance(failed, list) and failed and all(f in BASE_POLICY_IDS for f in failed)):
                pid = body.get("policy_id")
                failed = [pid] if pid in BASE_POLICY_IDS else [FAILCLOSED_POLICY]
            return Verdict(ok=False, failed_policies=sorted(failed), policy_hash=self.policy_hash, signature=sig,
                           latency_ms=latency_ms, source="biject", detail=str(body.get("reject_code") or "")[:200])
        return self._fc(f"non-decision verdict {verdict!r}", latency_ms)

    async def _one(self, a: Action, sem: asyncio.Semaphore) -> Verdict:
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await self._request("POST", "/api/verify", json=self._payload(a))
                return self._parse(r, (time.perf_counter() - t0) * 1000.0)
            except Exception as e:  # noqa: BLE001 - fail closed
                return self._fc(str(e), (time.perf_counter() - t0) * 1000.0)

    # ---------------------------------------------------------------- Verifier protocol
    async def check_batch(self, actions: list[Action]) -> list[Verdict]:
        try:
            hash_ok = await self.check_policy_hash()
        except Exception as e:  # noqa: BLE001
            return [self._fc(f"policy hash check failed: {e}") for _ in actions]
        if not hash_ok:
            return [self._fc("policy hash mismatch") for _ in actions]
        keys = [self._key(a) for a in actions]
        todo: dict[str, Action] = {}
        for k, a in zip(keys, actions):
            if k not in self._cache and k not in todo:
                todo[k] = a
        sem = asyncio.Semaphore(self.concurrency)
        got = await asyncio.gather(*(self._one(a, sem) for a in todo.values()))
        fresh = dict(zip(todo.keys(), got))
        for k, v in fresh.items():
            if v.source == "biject":  # never cache fail-closed verdicts
                self._cache[k] = v
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        out: list[Verdict] = []
        for k, a in zip(keys, actions):
            v = fresh.get(k) or self._cache[k]
            if v.source == "biject" and self.added_policies:
                from proofread.policies import reference

                try:
                    extra = reference.evaluate_added(a, self.added_policies)
                except Exception as e:  # noqa: BLE001
                    v = self._fc(f"added policy error: {e}")
                    extra = []
                if extra:
                    v = v.model_copy(update={"ok": False, "failed_policies": sorted(set(v.failed_policies) | set(extra))})
            out.append(v)
        return out

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def biject_available(base_url: str | None = None, timeout_s: float = 1.0) -> bool:
    """Healthy AND serving our pinned policy hash. Synchronous; never raises."""
    url = (base_url or os.environ.get("BIJECT_URL") or DEFAULT_URL).rstrip("/")
    key = _default_api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with httpx.Client(base_url=url, timeout=timeout_s, headers=headers) as c:
            if c.get("/api/health").status_code != 200:
                return False
            r = c.get("/api/policies")
            if r.status_code != 200:
                return False
            pols = r.json().get("policies", {})
            h = _expected_hash()
            return any(isinstance(m, dict) and m.get("source_hash") == h for m in pols.values())
    except Exception:  # noqa: BLE001
        return False
