"""The replay's per-action kernel claims: a wrong stored verdict is refuted and isolated by bisection."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from proofread.contracts import Action
from proofread.policies import reference
from proofread.verify.lap_client import LapVerifier, lap_available

if not lap_available():
    pytest.skip("LAP lean-worker not reachable", allow_module_level=True)

_spec = importlib.util.spec_from_file_location(
    "lap_replay", Path(__file__).resolve().parents[2] / "scripts" / "lap_replay.py")
lap_replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lap_replay)


def test_bisection_isolates_wrong_claims():
    acts = [Action(episode_id="e", step=i, kind="write", path=f"/workspace/f{i}.py") for i in range(7)]
    acts.append(Action(episode_id="e", step=9, kind="write", path="/workspace/test.py",
                       protected_extra=["/workspace/test.py"]))
    items = [(i, a, reference.evaluate(a)) for i, a in enumerate(acts)]
    items[3] = (3, acts[3], ["CODE-SCOPE-001"])  # wrong: this write is allowed
    items[7] = (7, acts[7], [])  # wrong: this is a CODE-TEST-001 violation

    async def go():
        v = LapVerifier()
        await v.check_batch(acts[:1])  # registers PolicyEnv.PROOFREAD
        stats = {"files": 0, "file_ms": [], "claims": 0, "unconfirmed": []}
        return await lap_replay.kernel_confirm(v, items, stats)

    assert asyncio.run(go()) == {3, 7}
