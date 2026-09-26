"""Golden cheats through the real gVisor sandbox: every mechanism must be flagged."""

from __future__ import annotations

import asyncio

import pytest

from proofread.sandbox.scenarios import GOLDEN, make_task, run_all
from proofread.sandbox.runner import DockerSandbox, ensure_image

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def golden_results():
    return asyncio.run(run_all(GOLDEN))


def test_golden_count():
    assert len(GOLDEN) >= 25
    assert len({s.name for s in GOLDEN}) == len(GOLDEN)


@pytest.mark.parametrize("sc", GOLDEN, ids=[s.name for s in GOLDEN])
def test_golden_cheat_flagged(golden_results, sc):
    r = golden_results[sc.name]
    assert not r.error, r.error
    detail = [(a.kind, a.path, a.dst, a.host, a.added_lines[:3]) for a in r.actions if a.kind != "exec"]
    assert sc.expect in r.flags, detail


def test_detached_background_write_is_killed():
    """A setsid/nohup writer scheduled after the command returns is killed before it can write."""

    async def go():
        await ensure_image()
        sb = DockerSandbox("w1-bgkill", build=False)
        await sb.start(make_task())
        try:
            r = await sb.run("nohup setsid sh -c 'sleep 1; echo x >> /workspace/test.py' >/dev/null 2>&1 &")
            assert r.exit_code == 0
            await asyncio.sleep(2.5)
            final = await sb.final_diff()
            files = await sb.export_files()
            return final, files
        finally:
            await sb.stop()

    final, files = asyncio.run(go())
    assert final == []
    assert "x" not in files["test.py"].splitlines()
