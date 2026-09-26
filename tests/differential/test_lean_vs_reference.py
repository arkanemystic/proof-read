"""Differential: Lean policies (compiled policycheck) vs reference.py, 2,000 hypothesis actions."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import strategies
from strategies import actions, allowlists

from proofread.policies import reference
from proofread.verify.lean_verifier import LEAN_EXE, LeanVerifier, build_lean

pytestmark = pytest.mark.timeout(900)

_state: dict = {}


def _verifier(allow: tuple[str, ...]) -> LeanVerifier:
    if "loop" not in _state:
        if not LEAN_EXE.exists() and not build_lean():
            pytest.skip("lean toolchain unavailable")
        _state["loop"] = asyncio.new_event_loop()
        _state["v"] = {}
    vs = _state["v"]
    if allow not in vs:
        vs[allow] = LeanVerifier(net_allowlist=allow, workers=1)
    return vs[allow]


def _lean(a, allow=()):
    v = _verifier(allow)
    return _state["loop"].run_until_complete(v.check_batch([a]))[0]


@settings(max_examples=2000, deadline=None, suppress_health_check=list(HealthCheck), derandomize=False)
@given(a=actions(), allow=allowlists)
def test_lean_matches_reference(a, allow):
    ref = sorted(reference.evaluate(a, net_allowlist=allow))
    v = _lean(a, allow)
    assert v.source == "lean", v.detail
    assert sorted(v.failed_policies) == ref, (a.model_dump(), allow)


@settings(max_examples=8, deadline=None, suppress_health_check=list(HealthCheck))
@given(batch=st.lists(actions(), min_size=50, max_size=150))
def test_lean_batch_matches_reference(batch):
    v = _verifier(())
    out = _state["loop"].run_until_complete(v.check_batch(batch))
    assert len(out) == len(batch)
    for a, verdict in zip(batch, out):
        assert sorted(verdict.failed_policies) == sorted(reference.evaluate(a)), a.model_dump()


@pytest.mark.parametrize("line,expect", [
    ("@pytest.mark.skip", True), ("pytest.skip('x')", True), ("x = pytest . exit", True),
    ("from pytest import skip", True), ("from _pytest.outcomes import fail", True),
    ("import pytest as pt", True), ("raise unittest.SkipTest()", True), ("ｐytest.ｓkip()", True),
    ("skipper = 1", False), ("markskip", False), ("éxfail", False), ("xfailé", False),
    ("from pytest import fixture", False), ("self._skip = 1", False),
])
def test_marker_cases(line, expect):
    from proofread.contracts import Action

    a = Action(episode_id="m", step=1, kind="write", path="/workspace/f.py", added_lines=[line])
    assert reference.line_has_skip(line) == expect
    assert ("CODE-SKIP-001" in _lean(a).failed_policies) == expect


@settings(max_examples=1000, deadline=None, suppress_health_check=list(HealthCheck))
@given(lines=st.lists(strategies._line(), min_size=1, max_size=3))
def test_skip_markers_lean_matches_reference(lines):
    from proofread.contracts import Action

    a = Action(episode_id="m", step=1, kind="write", path="/workspace/f.py", added_lines=lines)
    assert _lean(a).failed_policies == reference.evaluate(a), lines
