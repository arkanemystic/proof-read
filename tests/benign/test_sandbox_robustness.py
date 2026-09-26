"""Sandbox robustness: concurrency cap (pure), plus docker checks for fork bombs, huge output,
background jobs, timeouts, fail-closed capture, checkpoint/restore and export."""

from __future__ import annotations

import asyncio

import pytest

from proofread.contracts import Sandbox
from proofread.policies.reference import evaluate
from proofread.sandbox import runner
from proofread.sandbox.runner import DockerSandbox, SandboxError, ensure_image, sandbox_slot
from proofread.sandbox.scenarios import GOOD_FUNC, make_task


def test_sandbox_slot_caps_concurrency():
    async def go():
        live = 0
        peak = 0

        async def one():
            nonlocal live, peak
            async with sandbox_slot():
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.01)
                live -= 1

        await asyncio.gather(*(one() for _ in range(40)))
        return peak

    assert asyncio.run(go()) == 12
    assert asyncio.run(go()) == 12  # works across event loops


def test_protocol_conformance():
    assert isinstance(DockerSandbox("x"), Sandbox)


async def _with_sandbox(fn, cls=DockerSandbox):
    await ensure_image()
    async with sandbox_slot():
        sb = cls("w1-robust", build=False)
        await sb.start(make_task())
        try:
            return await fn(sb)
        finally:
            await sb.stop()


@pytest.mark.docker
def test_fork_bomb_huge_output_background():
    async def fn(sb):
        r1 = await sb.run(":(){ :|:& };:", 5)
        r2 = await sb.run("echo alive")
        r3 = await sb.run("yes | head -c 5000000")
        r4 = await sb.run("sleep 100 & echo quick")
        r5 = await sb.run("sleep 30", 2)
        r6 = await sb.run("ln -s / rootlink && ln -s /proc/kmsg kmsg && ln -s /usr/lib lib && ln -s . self")
        return r1, r2, r3, r4, r5, r6

    r1, r2, r3, r4, r5, r6 = asyncio.run(_with_sandbox(fn))
    assert r6.duration_s < 20
    assert not any(a.kind == "attempt_outside" for a in r6.actions), r6.actions
    assert {a.path for a in r6.actions if a.kind == "write"} >= {"/workspace/rootlink", "/workspace/kmsg"}
    assert r2.stdout == "alive\n"
    assert len(r3.stdout) <= runner.OUTPUT_CAP
    assert r4.stdout == "quick\n" and r4.duration_s < 10
    assert r5.timed_out and r5.duration_s < 10


class _BrokenSnap(DockerSandbox):
    fail = False

    async def _snap_raw(self, prev):
        if self.fail:
            raise SandboxError("boom")
        return await super()._snap_raw(prev)


@pytest.mark.docker
def test_capture_failure_fails_closed():
    async def fn(sb):
        sb.fail = True
        r = await sb.run("echo hi")
        acts = await sb.write("func.py", GOOD_FUNC)
        final = await sb.final_diff()
        return r, acts, final

    r, acts, final = asyncio.run(_with_sandbox(fn, _BrokenSnap))
    for group in (r.actions, acts, final):
        flags = {f for a in group for f in evaluate(a)}
        assert {"CODE-ATTR-001", "CODE-SCOPE-001"} <= flags


@pytest.mark.docker
def test_checkpoint_restore_export_read_snapshot():
    async def fn(sb):
        tok = await sb.checkpoint()
        await sb.write("func.py", GOOD_FUNC)
        await sb.run("mkdir -p pkg && echo 'X=1' > pkg/mod.py && chmod 000 pkg/mod.py")
        snap1 = await sb.snapshot()
        exp1 = await sb.export_files()
        await sb.restore(tok)
        exp2 = await sb.export_files()
        final = await sb.final_diff()
        text = await sb.read("test.py")
        return snap1, exp1, exp2, final, text

    snap1, exp1, exp2, final, text = asyncio.run(_with_sandbox(fn))
    assert "/workspace/pkg/mod.py" in snap1 and "/workspace/func.py" in snap1
    assert exp1["func.py"] == GOOD_FUNC and exp1["pkg/mod.py"] == "X=1\n"
    assert set(exp2) == {"func.py", "test.py"} and "NotImplementedError" in exp2["func.py"]
    assert final == []
    assert "def test_func" in text
