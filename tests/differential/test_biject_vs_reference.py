"""Differential: biject-api vs reference.py over 2,000 hypothesis actions.

Skips cleanly unless a local biject-api is healthy AND serves the pinned policy hash (see
proofread/verify/BIJECT_INTERFACE.md for why that is currently not the case).
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from strategies import actions, allowlists

from proofread.policies import reference
from proofread.verify.biject_client import BijectVerifier, biject_available

pytestmark = [pytest.mark.biject, pytest.mark.timeout(3600),
              pytest.mark.skipif(not biject_available(), reason="local biject-api not running with pinned policy hash")]

_state: dict = {}


@settings(max_examples=2000, deadline=None, suppress_health_check=list(HealthCheck))
@given(a=actions(), allow=allowlists)
def test_biject_matches_reference(a, allow):
    loop = _state.setdefault("loop", asyncio.new_event_loop())
    v = _state.setdefault(allow, BijectVerifier(net_allowlist=allow, timeout_s=30))
    verdict = loop.run_until_complete(v.check_batch([a]))[0]
    assert verdict.source == "biject", verdict.detail
    assert sorted(verdict.failed_policies) == sorted(reference.evaluate(a, net_allowlist=allow))
