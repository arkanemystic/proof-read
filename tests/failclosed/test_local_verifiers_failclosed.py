"""Fail-closed behaviour of LeanVerifier (worker timeout, crash, kill, malformed output) and
ReferenceVerifier (evaluation error), plus make_verifier fallback."""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from proofread.contracts import FAILCLOSED_POLICY, Action, Verifier
from proofread.policies import reference
from proofread.verify import ReferenceVerifier, make_verifier
from proofread.verify.lean_verifier import LEAN_EXE, LeanVerifier, build_lean


def act(i: int = 0, **kw) -> Action:
    base = dict(episode_id="ep-1", step=i, kind="write", path=f"/workspace/f{i}.py")
    base.update(kw)
    return Action(**base)


def assert_fc(vs):
    assert vs and all((not v.ok) and v.failed_policies == [FAILCLOSED_POLICY] for v in vs), vs


def fake_exe(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fake_policycheck"
    p.write_text(f"#!{sys.executable}\nimport sys, json, time, os\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.fixture(scope="module")
def lean_ready():
    if not LEAN_EXE.exists() and not build_lean():
        pytest.skip("lean toolchain unavailable")


async def test_lean_timeout_fails_closed(tmp_path):
    exe = fake_exe(tmp_path, "time.sleep(30)")
    v = LeanVerifier(exe=exe, timeout_s=0.3, retries=1, workers=1)
    assert_fc(await v.check_batch([act(), act(1)]))
    await v.aclose()


async def test_lean_malformed_output_fails_closed(tmp_path):
    exe = fake_exe(tmp_path, "for line in sys.stdin:\n    print('not json', flush=True)")
    v = LeanVerifier(exe=exe, timeout_s=2, retries=0, workers=1)
    assert_fc(await v.check_batch([act()]))
    await v.aclose()


async def test_lean_unknown_policy_id_fails_closed(tmp_path):
    exe = fake_exe(tmp_path, "for line in sys.stdin:\n    print(json.dumps({'id': json.loads(line)['id'], 'failed': ['NOPE']}), flush=True)")
    v = LeanVerifier(exe=exe, timeout_s=2, retries=0, workers=1)
    assert_fc(await v.check_batch([act()]))
    await v.aclose()


async def test_lean_error_response_fails_closed(tmp_path):
    exe = fake_exe(tmp_path, "for line in sys.stdin:\n    print(json.dumps({'id': json.loads(line)['id'], 'error': 'x'}), flush=True)")
    v = LeanVerifier(exe=exe, timeout_s=2, retries=0, workers=1)
    assert_fc(await v.check_batch([act()]))
    await v.aclose()


async def test_lean_worker_dies_midway_fails_closed(tmp_path):
    exe = fake_exe(tmp_path, "sys.stdin.readline(); os._exit(1)")
    v = LeanVerifier(exe=exe, timeout_s=2, retries=1, workers=1)
    assert_fc(await v.check_batch([act(), act(1)]))
    await v.aclose()


async def test_lean_missing_exe_fails_closed(tmp_path):
    assert_fc(await LeanVerifier(exe=tmp_path / "nope", build=False).check_batch([act()]))


async def test_lean_real_worker_killed_recovers_correctly(lean_ready):
    v = LeanVerifier(workers=1, timeout_s=5)
    first = await v.check_batch([act(0, path="/workspace/test_x.py")])
    assert first[0].failed_policies == ["CODE-TEST-001"]
    proc = v._workers[0].proc
    os.kill(proc.pid, signal.SIGKILL)
    await asyncio.sleep(0.1)
    out = await v.check_batch([act(1, path="/workspace/tests/a.py"), act(2)])
    # either a fresh worker answered correctly, or it failed closed; never fail open
    for a, verdict in zip([act(1, path="/workspace/tests/a.py"), act(2)], out):
        assert verdict.failed_policies in (reference.evaluate(a), [FAILCLOSED_POLICY])
    again = await v.check_batch([act(3, path="/workspace/tests/b.py")])
    assert again[0].failed_policies == ["CODE-TEST-001"] and again[0].source == "lean"
    await v.aclose()


async def test_lean_worker_killed_during_batch(lean_ready):
    v = LeanVerifier(workers=1, timeout_s=5, chunk_size=10_000, retries=0)
    await v.check_batch([act(0)])
    proc = v._workers[0].proc
    batch = [act(i, added_lines=["x = 1"] * 200) for i in range(1, 3000)]
    task = asyncio.create_task(v.check_batch(batch))
    await asyncio.sleep(0.005)
    os.kill(proc.pid, signal.SIGKILL)
    out = await task
    for a, verdict in zip(batch, out):
        assert verdict.failed_policies in (reference.evaluate(a), [FAILCLOSED_POLICY])
    await v.aclose()


async def test_reference_error_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad")

    v = ReferenceVerifier()
    monkeypatch.setattr(reference, "evaluate", boom)
    out = await v.check_batch([act()])
    assert_fc(out)
    assert out[0].source == "failclosed" and out[0].provisional


async def test_make_verifier_auto_falls_back(monkeypatch):
    monkeypatch.setenv("BIJECT_URL", "http://127.0.0.1:9")
    v = make_verifier("auto")
    assert isinstance(v, ReferenceVerifier) and isinstance(v, Verifier)
    assert v.provisional and v.policy_hash == reference.BASE_POLICY_HASH
    out = await v.check_batch([act(0, path="/workspace/test_a.py"), act(1)])
    assert out[0].failed_policies == ["CODE-TEST-001"] and out[1].ok and out[1].source == "reference"


async def test_make_verifier_biject_down_fails_closed(monkeypatch):
    monkeypatch.setenv("BIJECT_URL", "http://127.0.0.1:9")
    v = make_verifier("biject")
    v.timeout_s, v.retries = 0.3, 0
    assert_fc(await v.check_batch([act()]))


def test_make_verifier_rejects_unknown_kind():
    with pytest.raises(ValueError):
        make_verifier("nope")


async def test_lean_spec_hash_mismatch_fails_closed(lean_ready):
    v = LeanVerifier(workers=1)
    v.spec_hash = "0" * 64
    assert_fc(await v.check_batch([act()]))
    await v.aclose()


def test_lean_port_aligned_to_current_reference():
    from proofread.verify.lean_verifier import LEAN_SPEC_HASH

    assert reference.BASE_POLICY_HASH == LEAN_SPEC_HASH, "reference.py changed: re-align the Lean port"
