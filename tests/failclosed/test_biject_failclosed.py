"""Fail-closed behaviour of BijectVerifier against a fake biject-api (httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from proofread.contracts import FAILCLOSED_POLICY, Action, AddedPolicy
from proofread.policies import reference
from proofread.verify.biject_client import TOOL_NAME, BijectVerifier

H = reference.BASE_POLICY_HASH


def act(i: int = 0, **kw) -> Action:
    base = dict(episode_id="ep-1", step=i, kind="write", path=f"/workspace/f{i}.py")
    base.update(kw)
    return Action(**base)


class Fake:
    def __init__(self, verify=None, policy_hash: str = H, policies_status: int = 200) -> None:
        self.verify = verify or (lambda req: httpx.Response(200, json={"verdict": "allowed", "policy_id": "x"}))
        self.policy_hash = policy_hash
        self.policies_status = policies_status
        self.verify_calls = 0
        self.policy_calls = 0

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/policies":
            self.policy_calls += 1
            return httpx.Response(self.policies_status, json={"policies": {
                "PROOFREAD-BASE": {"source_hash": self.policy_hash, "applies_to_tools": [TOOL_NAME]}}, "count": 1})
        if req.url.path == "/api/verify":
            self.verify_calls += 1
            r = self.verify(req)
            if asyncio.iscoroutine(r):
                r = await r
            return r
        return httpx.Response(404)


def make(fake: Fake, **kw) -> BijectVerifier:
    kw.setdefault("timeout_s", 0.3)
    kw.setdefault("retries", 2)
    kw.setdefault("backoff_s", 0.0)
    return BijectVerifier("http://fake", api_key="k" * 20, transport=httpx.MockTransport(fake), **kw)


def assert_fc(vs):
    assert vs and all((not v.ok) and v.failed_policies == [FAILCLOSED_POLICY] and v.source == "failclosed"
                      for v in vs), vs


async def test_allowed_and_blocked_happy_path():
    def verify(req):
        body = json.loads(req.content)
        assert body["tool_name"] == TOOL_NAME and body["params"]["policy_hash"] == H
        if "test_" in body["params"]["path"]:
            return httpx.Response(200, json={"verdict": "blocked", "policy_id": "CODE-TEST-001"})
        return httpx.Response(200, json={"verdict": "allowed", "policy_id": "CODE-TEST-001"})

    v = make(Fake(verify))
    out = await v.check_batch([act(0), act(1, path="/workspace/test_a.py")])
    assert out[0].ok and out[0].source == "biject" and not out[0].provisional
    assert not out[1].ok and out[1].failed_policies == ["CODE-TEST-001"]


async def test_timeout_fails_closed():
    async def slow(req):
        await asyncio.sleep(2)
        return httpx.Response(200, json={"verdict": "allowed"})

    fake = Fake(slow)
    assert_fc(await make(fake, retries=1).check_batch([act()]))
    assert fake.verify_calls == 2


async def test_transport_timeout_exception_fails_closed():
    def boom(req):
        raise httpx.ReadTimeout("slow", request=req)

    assert_fc(await make(Fake(boom)).check_batch([act()]))


@pytest.mark.parametrize("code", [500, 502, 503, 504, 429])
async def test_5xx_fails_closed_after_retries(code):
    fake = Fake(lambda req: httpx.Response(code, text="oops"))
    assert_fc(await make(fake, retries=2).check_batch([act()]))
    assert fake.verify_calls == 3


async def test_5xx_then_success_retries():
    seq = [httpx.Response(503), httpx.Response(200, json={"verdict": "allowed"})]
    fake = Fake(lambda req: seq.pop(0))
    out = await make(fake).check_batch([act()])
    assert out[0].ok and fake.verify_calls == 2


@pytest.mark.parametrize("resp", [
    httpx.Response(200, content=b"{not json"),
    httpx.Response(200, content=b"[]"),
    httpx.Response(200, json={"no_verdict": True}),
    httpx.Response(200, json={"verdict": "skipped"}),
    httpx.Response(200, json={"verdict": "maybe"}),
    httpx.Response(200, json={"verdict": 1}),
    httpx.Response(401, json={"detail": "no"}),
    httpx.Response(422, json={"detail": "bad"}),
])
async def test_malformed_or_non_decision_fails_closed(resp):
    assert_fc(await make(Fake(lambda req: resp)).check_batch([act()]))


async def test_blocked_with_unknown_policy_fails_closed_label():
    out = await make(Fake(lambda r: httpx.Response(200, json={"verdict": "blocked", "policy_id": "???"}))).check_batch([act()])
    assert not out[0].ok and out[0].failed_policies == [FAILCLOSED_POLICY]


async def test_wrong_registry_hash_fails_closed_without_verifying():
    fake = Fake(policy_hash="0" * 64)
    assert_fc(await make(fake).check_batch([act(0), act(1)]))
    assert fake.verify_calls == 0


async def test_registry_down_fails_closed():
    assert_fc(await make(Fake(policies_status=503)).check_batch([act()]))


async def test_response_hash_mismatch_fails_closed():
    fake = Fake(lambda r: httpx.Response(200, json={"verdict": "allowed", "policy_hash": "f" * 64}))
    assert_fc(await make(fake).check_batch([act()]))


async def test_connection_drop_fails_closed():
    def drop(req):
        raise httpx.RemoteProtocolError("server disconnected", request=req)

    assert_fc(await make(Fake(drop)).check_batch([act()]))


async def test_unreachable_server_fails_closed():
    v = BijectVerifier("http://127.0.0.1:9", api_key="", timeout_s=0.3, retries=0)
    assert_fc(await v.check_batch([act()]))


async def test_signature_check():
    sk = Ed25519PrivateKey.generate()
    pub = base64.b64encode(sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    eh = "ab" * 32
    good = sk.sign(eh.encode()).hex()
    ok = await make(Fake(lambda r: httpx.Response(200, json={"verdict": "allowed", "entry_hash": eh, "signature": good})),
                    verify_pubkey_b64=pub).check_batch([act()])
    assert ok[0].ok and ok[0].signature == good
    bad = await make(Fake(lambda r: httpx.Response(200, json={"verdict": "allowed", "entry_hash": eh, "signature": "00" * 64})),
                     verify_pubkey_b64=pub).check_batch([act()])
    assert_fc(bad)
    missing = await make(Fake(lambda r: httpx.Response(200, json={"verdict": "allowed"})),
                         verify_pubkey_b64=pub).check_batch([act()])
    assert_fc(missing)


async def test_cache_and_dedupe_and_no_caching_of_failclosed():
    fake = Fake()
    v = make(fake)
    out = await v.check_batch([act(0), act(0), act(1)])
    assert all(x.ok for x in out) and fake.verify_calls == 2
    await v.check_batch([act(0), act(1)])
    assert fake.verify_calls == 2
    fake2 = Fake(lambda r: httpx.Response(500))
    v2 = make(fake2, retries=0)
    assert_fc(await v2.check_batch([act(0)]))
    assert_fc(await v2.check_batch([act(0)]))
    assert fake2.verify_calls == 2


async def test_bounded_concurrency():
    live = {"now": 0, "max": 0}

    async def verify(req):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1
        return httpx.Response(200, json={"verdict": "allowed"})

    out = await make(Fake(verify), concurrency=3, timeout_s=5).check_batch([act(i) for i in range(20)])
    assert all(x.ok for x in out) and live["max"] <= 3


async def test_added_policies_applied_on_top():
    added = [AddedPolicy(id="ADD-NO-SECRETS", description="d", kind="deny_path_glob", pattern="secrets/*")]
    out = await make(Fake(), added_policies=added).check_batch([act(0, path="/workspace/secrets/k")])
    assert not out[0].ok and "ADD-NO-SECRETS" in out[0].failed_policies
