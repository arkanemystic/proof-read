"""Differential: LAP kernel decision over typed facts vs reference.evaluate on 2,000 hypothesis actions.

Actions are generated in chunks and sent to the live lean-worker in batches. The decision cache is keyed by the full fact tuple, so every distinct tuple is kernel-decided once."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from strategies import actions, allowlists

from proofread.policies import reference
from proofread.verify.lap_client import LapVerifier, lap_available

pytestmark = pytest.mark.timeout(1800)

if not lap_available():
    pytest.skip("LAP lean-worker not reachable", allow_module_level=True)

N_CHUNKS = 100
CHUNK = 20  # 100 x 20 = 2,000 actions
_state = {"n": 0, "loop": asyncio.new_event_loop(), "v": {}}


def _verifier(allow):
    if allow not in _state["v"]:
        _state["v"][allow] = LapVerifier(net_allowlist=allow, cache=True, concurrency=4)
    return _state["v"][allow]


@settings(max_examples=N_CHUNKS, deadline=None, suppress_health_check=list(HealthCheck), derandomize=True)
@given(batch=st.lists(actions(), min_size=CHUNK, max_size=CHUNK), allow=allowlists)
def test_lap_matches_reference(batch, allow):
    vs = _state["loop"].run_until_complete(_verifier(allow).check_batch(batch))
    for a, v in zip(batch, vs):
        assert v.source == "lap", v
        assert v.failed_policies == reference.evaluate(a, allow), (a, v)
    _state["n"] += len(batch)


def test_count():
    assert _state["n"] >= N_CHUNKS * CHUNK
