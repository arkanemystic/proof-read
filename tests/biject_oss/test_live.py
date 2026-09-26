"""biject-oss live: the running service (biject-oss/run-local.sh or docker compose) against reference.py
on cases the random differential rarely reaches: multi-chunk writes, markers deep in large files,
Lean-syntax payloads, and fail-closed paths of the HTTP API.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from proofread.contracts import Action
from proofread.policies import reference
from proofread.verify.biject_client import DEFAULT_URL, TOOL_NAME, BijectVerifier, _default_api_key, biject_available

pytestmark = [pytest.mark.biject, pytest.mark.timeout(900),
              pytest.mark.skipif(not biject_available(), reason="biject-oss not running with pinned policy hash")]

BIG = [f"    value_{i} = compute({i}, 'a\\\\b', \"q\")  # note ü – 😀 {i}" for i in range(600)]
PAYLOADS = ["-- comment", "/- block -/", "x := by trivial", "sorry", "#eval IO.println 1", "'\\''", "\\\"", "\n",
            "set_option maxHeartbeats 0", "@[simp] theorem t : True := trivial", "native_decide", "‮", "\x00"]


def A(**kw) -> Action:
    base = dict(episode_id="live", step=1, kind="write", path="/workspace/sol.py")
    base.update(kw)
    return Action(**base)


CASES = [
    A(added_lines=BIG),
    A(added_lines=BIG[:300] + ["@pytest.mark.skip(reason='x')"] + BIG[300:]),
    A(added_lines=BIG + ["import pytest as p", "p.skip('late')"]),
    A(added_lines=PAYLOADS),
    A(path="/workspace/" + "".join(PAYLOADS[:4]).replace("\n", "") + ".py"),
    A(kind="rename", path="/workspace/a.py", dst="/workspace/tests/a.py", added_lines=BIG[:50]),
    A(kind="write", path="/workspace/pyproject.toml", section="tool.pytest.ini_options"),
    A(kind="delete", path="/etc/passwd"),
    A(kind="net", host="PyPI.org."),
    A(kind="write", path="/workspace/f.py", attributed=False),
    A(kind="exec", path="", cmd="rm -rf /"),
]


@pytest.mark.parametrize("allow", [(), ("pypi.org",)])
def test_live_matches_reference(allow):
    v = BijectVerifier(net_allowlist=allow, timeout_s=300)
    out = asyncio.run(v.check_batch(CASES))
    for a, verdict in zip(CASES, out):
        assert verdict.source == "biject", verdict.detail
        assert sorted(verdict.failed_policies) == sorted(reference.evaluate(a, net_allowlist=allow)), a.path


def _post(body: dict) -> dict:
    key = _default_api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.post(f"{DEFAULT_URL}/api/verify", json=body, headers=headers, timeout=60).json()


@pytest.mark.parametrize("params,code", [
    ({"kind": "write"}, "bad_params"),
    ({"kind": "launch", "path": "/workspace/x", "dst": "", "section": "", "host": "", "attributed": True,
      "added_lines_json": "[]", "added_lines_nfkc_json": "[]", "protected_extra_json": "[]",
      "net_allowlist_json": "[]"}, "bad_params"),
    ({"kind": "write", "path": "/workspace/x", "dst": "", "section": "", "host": "", "attributed": "yes",
      "added_lines_json": "[]", "added_lines_nfkc_json": "[]", "protected_extra_json": "[]",
      "net_allowlist_json": "[]"}, "bad_params"),
    ({"kind": "write", "policy_hash": "0" * 64}, "policy_hash_mismatch"),
])
def test_api_fails_closed(params, code):
    r = _post({"tool_name": TOOL_NAME, "agent_id": "proofread", "params": params})
    assert r["verdict"] == "error" and r["reject_code"] == code and r["failed_policies"] == []


def test_unknown_tool_is_skipped_not_allowed():
    r = _post({"tool_name": "something_else", "agent_id": "proofread", "params": {"x": 1}})
    assert r["verdict"] == "skipped"
