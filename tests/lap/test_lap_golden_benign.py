"""Golden cheats and benign actions (fixtures from tests/golden_cheats) through the live LAP worker."""

from __future__ import annotations

import asyncio

import pytest
from test_reference_policies import BENIGN, CHEATS

from proofread.policies import reference
from proofread.verify.lap_client import LapVerifier, lap_available

pytestmark = [pytest.mark.timeout(900)]

if not lap_available():
    pytest.skip("LAP lean-worker not reachable", allow_module_level=True)

_V = LapVerifier(cache=False)


def _run(actions):
    return asyncio.run(_V.check_batch(actions))


def test_golden_cheats_flagged():
    acts = [c[1] for c in CHEATS]
    vs = _run(acts)
    for (name, a, policy), v in zip(CHEATS, vs):
        assert v.source == "lap" and v.provisional, name
        assert not v.ok and policy in v.failed_policies, (name, v)
        assert v.failed_policies == reference.evaluate(a), name


def test_benign_allowed():
    vs = _run(list(BENIGN))
    for a, v in zip(BENIGN, vs):
        assert v.ok and v.failed_policies == [] and v.source == "lap", (a, v)


def test_net_allowlist():
    from proofread.contracts import Action

    a = Action(episode_id="e", step=1, kind="net", host="A.com.")
    v1 = asyncio.run(LapVerifier(net_allowlist=("a.com",), cache=False).check_batch([a]))[0]
    v2 = _run([a])[0]
    assert v1.ok and not v2.ok and v2.failed_policies == ["CODE-NET-001"]
