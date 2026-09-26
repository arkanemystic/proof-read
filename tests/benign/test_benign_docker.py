"""Benign episodes through the real gVisor sandbox: zero flags, empty final diff."""

from __future__ import annotations

import asyncio

import pytest

from proofread.sandbox.scenarios import BENIGN, run_all

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def benign_results():
    return asyncio.run(run_all(BENIGN))


def test_benign_count():
    assert len(BENIGN) >= 15


@pytest.mark.parametrize("sc", BENIGN, ids=[s.name for s in BENIGN])
def test_benign_zero_flags(benign_results, sc):
    r = benign_results[sc.name]
    assert not r.error, r.error
    assert r.flags == [], [(a.kind, a.path, a.host, a.added_lines[:3]) for a in r.actions]
    assert not any(a.step == -1 for a in r.actions), "final diff must be empty"


def test_benign_semantics(benign_results):
    assert "AssertionError" in benign_results["solve_and_run"].runs[-1].stderr  # oneoff task: 2 -> 4 is impossible
    assert benign_results["timeout_command"].runs[0].timed_out
    assert "raise NotImplementedError" in benign_results["checkpoint_restore"].runs[-1].stdout
    assert "b.py" in benign_results["rename_own_file"].files
    kinds = {a.kind for a in benign_results["rename_own_file"].actions}
    assert "rename" in kinds
    assert any(a.kind == "chmod" for a in benign_results["chmod_own_script"].actions)
