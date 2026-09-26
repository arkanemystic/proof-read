import json

import pytest

from proofread.benchmarks import impossiblebench as ib
from proofread.contracts import Task

SPLIT_SIZES = {"training": 20, "capability_holdout": 40, "cheat_holdout": 40,
               "selection_original": 10, "selection_cheat": 10}


def base(tid: str) -> str:
    return tid.split("/")[2]


def test_snapshot_has_103_problems_with_all_variants():
    assert len(ib.problem_ids()) == 103
    for b in ib.problem_ids():
        p = ib._problems()[b]
        assert set(p["tests"]) == {"oneoff", "conflicting"}
        for v in ("oneoff", "conflicting"):
            assert p["tests"][v] != p["original_test"]
    assert len(ib.all_task_ids()) == 309


@pytest.mark.parametrize("variant", ib.VARIANTS)
def test_load_task_returns_contract_task(variant):
    t = ib.load_task(f"lcb/{variant}/lcbhard_91")
    assert isinstance(t, Task)
    assert t.variant == variant and t.impossible == (variant != "original")
    assert set(t.files) == {"func.py", "test.py"} and t.test_paths == ["test.py"]
    assert t.test_command == ["python", "test.py"]
    assert t.files["func.py"].endswith("\n    raise NotImplementedError")
    assert t.files["test.py"].startswith("from func import shortest_palindrome_with_prefix\n")
    assert "def test_func(candidate)" in t.files["test.py"]
    assert "**DO NOT MODIFY THE TESTS.**" in t.spec and "func.py" in t.spec
    orig = t.meta["original_test_files"]["test.py"]
    assert (orig == t.files["test.py"]) == (variant == "original")
    assert t.meta["verify_timeout_s"] == 30.0


def test_bad_ids_raise():
    for bad in ("lcb/foo/lcbhard_1", "x/original/lcbhard_1", "lcb/original/lcbhard_999"):
        with pytest.raises(KeyError):
            ib.load_task(bad)


def test_splits_lock_sizes_hash_and_determinism():
    lock = json.loads(ib.SPLITS_LOCK.read_text())
    assert lock["snapshot_sha256"] == ib.snapshot_sha256()
    assert lock["sha256"] == ib.splits_digest(lock["splits"], lock["snapshot_sha256"])
    assert lock["splits"] == ib.build_splits(lock["seed"])
    for s, n in SPLIT_SIZES.items():
        ids = ib.list_tasks(s)
        assert len(ids) == n == len(set(ids)), s
        for i in ids:
            ib.load_task(i)


def test_splits_variants():
    assert all(i.startswith("lcb/original/") for s in ("training", "capability_holdout", "selection_original")
               for i in ib.list_tasks(s))
    assert len(ib.list_tasks("cheat_holdout", "oneoff")) == 20
    assert len(ib.list_tasks("cheat_holdout", "conflicting")) == 20
    assert len(ib.list_tasks("selection_cheat", "oneoff")) == 5
    assert len(ib.list_tasks("selection_cheat", "original")) == 0


def test_splits_disjoint_at_problem_level():
    P = {s: {base(i) for i in ib.list_tasks(s)} for s in SPLIT_SIZES}
    assert len(P["cheat_holdout"]) == 40  # each problem once
    holdouts = P["capability_holdout"] | P["cheat_holdout"]
    for s in ("training", "selection_original", "selection_cheat"):
        assert not (P[s] & holdouts), s
    assert not (P["training"] & (P["selection_original"] | P["selection_cheat"]))
    assert not (P["selection_original"] & P["selection_cheat"])
    # cheat holdout may reuse capability-holdout problems (mutated), documented in the notes
    assert len(P["cheat_holdout"] - P["capability_holdout"]) == 23


def test_training_weighted_toward_hard():
    ranked = sorted(ib.problem_ids(), key=lambda b: -ib.hardness_proxy(b))
    top = set(ranked[: len(ranked) // 3])
    train = {base(i) for i in ib.list_tasks("training")}
    assert len(train & top) == 14


def test_tampered_lock_rejected(tmp_path, monkeypatch):
    lock = json.loads(ib.SPLITS_LOCK.read_text())
    lock["splits"]["training"][0] = "lcb/original/lcbhard_0"
    p = tmp_path / "splits.lock"
    p.write_text(json.dumps(lock))
    monkeypatch.setattr(ib, "SPLITS_LOCK", p)
    ib._lock.cache_clear()
    try:
        with pytest.raises(ValueError):
            ib.list_tasks("training")
    finally:
        monkeypatch.undo()
        ib._lock.cache_clear()


def test_unknown_split():
    with pytest.raises(ValueError):
        ib.list_tasks("nope")


@pytest.mark.network
def test_snapshot_matches_hf():
    from datasets import load_dataset

    probs = ib._problems()
    for v in ib.VARIANTS:
        for r in load_dataset(ib.HF_DATASET, split=v):
            p = probs[r["task_id"]]
            assert r["prompt"] == p["prompt"] and r["original_test"] == p["original_test"]
            assert r["test"] == (p["original_test"] if v == "original" else p["tests"][v])
