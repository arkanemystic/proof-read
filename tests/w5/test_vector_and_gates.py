import pytest

from proofread.contracts import AddedPolicy, EpisodeResult, Genome, VectorIndex
from proofread.evolve.config import arm_preset
from proofread.evolve.gates import decide, screening_reject
from proofread.evolve.patching import PatchError, validate_edit
from proofread.evolve.proposer import extract_json
from proofread.evolve.stats import empirical_gate, paired_bootstrap_lower_bound
from proofread.store.sqlite_store import SqliteStore
from proofread.store.vector import DIM, NumpyVectorIndex, embed

# ------------------------------------------------------------------ vectors


def test_embed_deterministic_and_normalized():
    a, b = embed("Run the tests after every edit"), embed("Run the tests after every edit")
    assert a == b and len(a) == DIM
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9
    assert all(x == 0 for x in embed(""))


def test_vector_search_similarity_and_persistence(tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite")
    idx = NumpyVectorIndex(s, namespace="rejected:armC")
    assert isinstance(idx, VectorIndex)
    texts = {
        "e1": "run the tests more often after each edit to catch failures",
        "e2": "shorten the system prompt role section",
        "e3": "add a memory note about reading the docstring carefully",
    }
    for k, t in texts.items():
        idx.add(k, embed(t), {"intent": t})
    hits = idx.search(embed("run tests after every edit"), k=2)
    assert hits[0][0] == "e1" and hits[0][1] > hits[1][1]
    assert hits[0][2]["intent"] == texts["e1"]
    idx.add("e1", embed(texts["e1"]), {"intent": "updated"})  # upsert
    assert len(idx) == 3
    reloaded = NumpyVectorIndex(s, namespace="rejected:armC")
    assert len(reloaded) == 3 and reloaded.search(embed(texts["e3"]), k=1)[0][0] == "e3"
    assert reloaded.search(embed(texts["e1"]), k=1)[0][2]["intent"] == "updated"
    assert len(NumpyVectorIndex(s, namespace="other")) == 0
    assert NumpyVectorIndex().search(embed("x")) == []


# ------------------------------------------------------------------ bootstrap gate math


def _maps(c, h):
    return ({(f"t{i}", 0): bool(x) for i, x in enumerate(c)}, {(f"t{i}", 0): bool(x) for i, x in enumerate(h)})


def test_bootstrap_known_cases():
    assert paired_bootstrap_lower_bound([5.0] * 10) == 5.0
    assert paired_bootstrap_lower_bound([]) == float("-inf")
    # identical -> delta 0, lb 0, no promotion
    g = empirical_gate(*_maps([1, 0, 1, 0], [1, 0, 1, 0]))
    assert g.delta_points == 0 and g.lower_bound_points == 0 and not g.promote
    # all pairs improve -> lb 100
    g = empirical_gate(*_maps([1] * 10, [0] * 10))
    assert g.delta_points == 100 and g.lower_bound_points == 100 and g.promote
    # one improvement out of 50 -> delta 2 points but lb 0 -> no promotion
    g = empirical_gate(*_maps([1] + [0] * 49, [0] * 50))
    assert g.delta_points == pytest.approx(2.0) and g.lower_bound_points == 0 and not g.promote
    # 7 wins, 1 loss out of 20 -> delta 30, lb clearly positive
    c = [1] * 7 + [0] + [1] * 6 + [0] * 6
    h = [0] * 7 + [1] + [1] * 6 + [0] * 6
    g = empirical_gate(*_maps(c, h))
    assert g.delta_points == pytest.approx(30.0) and 10 < g.lower_bound_points < 30 and g.promote
    # worse candidate
    g = empirical_gate(*_maps([0] * 10, [1] * 10))
    assert g.delta_points == -100 and not g.promote
    # deterministic under the seeded RNG
    assert empirical_gate(*_maps(c, h)) == empirical_gate(*_maps(c, h))


def test_bootstrap_min_delta_threshold():
    # 100 pairs, 1 win: lb 0 anyway; 3 wins/1 loss out of 100 -> delta 2
    c = [1, 1, 1, 0] + [0] * 96
    h = [0, 0, 0, 1] + [0] * 96
    g = empirical_gate(*_maps(c, h))
    assert g.delta_points == pytest.approx(2.0)
    g2 = empirical_gate(*_maps(c, h), min_delta_points=5.0)
    assert not g2.promote


def test_pairing_only_uses_common_keys():
    cand = {("a", 0): True, ("b", 0): True, ("c", 1): True}
    champ = {("a", 0): False, ("b", 0): False}
    g = empirical_gate(cand, champ)
    assert g.n_pairs == 2


def test_screening_rule():
    assert screening_reject(0, 3) and screening_reject(0, 5) and screening_reject(1, 4)
    assert screening_reject(0, 2) is None and screening_reject(2, 4) is None and screening_reject(5, 5) is None


def _ep(task, passed, viol=()):
    return EpisodeResult(episode_id=task, task_id=task, variant="original", genome_hash="h", model="m",
                         mode="observe", passed_workspace=passed, violations=list(viol))


def test_decide_formal_vs_ungated():
    champ = [_ep(f"t{i}", i < 2) for i in range(10)]
    cand = [_ep(f"t{i}", True, ["CODE-TEST-001"] if i == 5 else []) for i in range(10)]
    c = decide(arm_preset("C"), cand, champ)
    assert c.status == "rejected_formal" and c.stats.delta_points == pytest.approx(80.0) and c.violations
    a = decide(arm_preset("A"), cand, champ)
    assert a.status == "passed_gates"
    same = decide(arm_preset("C"), champ, champ)
    assert same.status == "rejected_empirical"


# ------------------------------------------------------------------ static validation


def test_validate_edit_accepts_surface_edits():
    g = Genome()
    c = validate_edit(g, [{"op": "replace", "path": "/workflow/max_turns", "value": 40},
                          {"op": "add", "path": "/memory_notes/-", "value": "Read the docstring examples."},
                          {"op": "add", "path": "/system_prompt/edge_cases", "value": "Consider edge cases."},
                          {"op": "replace", "path": "/tools/enabled", "value": ["read_file", "write_file", "run"]},
                          {"op": "add", "path": "/added_policies/-", "value": {
                              "id": "ADD-NO-EXIT", "description": "no sys.exit", "kind": "deny_added_line_regex",
                              "pattern": r"sys\.exit"}}])
    assert c.workflow.max_turns == 40 and c.added_policies[0].id == "ADD-NO-EXIT" and c.version == g.version


@pytest.mark.parametrize("ops,msg", [
    ([], "non-empty"),
    ([{"op": "replace", "path": "/version", "value": 9}], "editable surface"),
    ([{"op": "add", "path": "/tool_impl", "value": "x"}], "editable surface"),
    ([{"op": "replace", "path": "/workflow/max_turns", "value": 999}], "schema"),
    ([{"op": "replace", "path": "/tools/run_timeout_s", "value": 30}], None),
    ([{"op": "add", "path": "/workflow/new_field", "value": 1}], "schema"),
    ([{"op": "remove", "path": "/system_prompt/zzz"}], "apply"),
    ([{"op": "replace", "path": "/workflow/max_turns", "value": 30}], "no-op"),
    ([{"op": "add", "path": "/added_policies/-", "value": {"id": "ADD-X", "description": "d",
                                                            "kind": "deny_exec_regex", "pattern": "("}}], "regex"),
    ([{"op": "remove", "path": "/added_policies"}], "cannot remove"),
])
def test_validate_edit_rejects(ops, msg):
    if msg is None:
        validate_edit(Genome(), ops)
        return
    with pytest.raises(PatchError, match=msg):
        validate_edit(Genome(), ops)


def test_tools_only_narrow_and_policies_append_only():
    g = Genome().model_copy(update={"tools": Genome().tools.model_copy(update={"enabled": ["read_file", "run"],
                                                                                "run_timeout_s": 30})})
    with pytest.raises(PatchError, match="shrink|tool_widened"):
        validate_edit(g, [{"op": "add", "path": "/tools/enabled/-", "value": "write_file"}])
    with pytest.raises(PatchError, match="decrease|tool_widened"):
        validate_edit(g, [{"op": "replace", "path": "/tools/run_timeout_s", "value": 60}])
    pol = AddedPolicy(id="ADD-A", description="d", kind="deny_path_glob", pattern="/workspace/test*.py")
    g2 = Genome(added_policies=[pol])
    with pytest.raises(PatchError, match="append-only|policy_removed"):
        validate_edit(g2, [{"op": "remove", "path": "/added_policies/0"}])
    with pytest.raises(PatchError, match="append-only|policy_modified"):
        validate_edit(g2, [{"op": "replace", "path": "/added_policies/0/pattern", "value": "/nothing"}])
    with pytest.raises(PatchError, match="duplicate"):
        validate_edit(g2, [{"op": "add", "path": "/added_policies/-", "value": pol.model_dump()}])
    with pytest.raises(PatchError, match="hides tests"):
        validate_edit(Genome(), [{"op": "replace", "path": "/workflow/max_turns", "value": 10}], hide_tests=True)


def test_extract_json():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n{"intent": "x", "patch": []}\n```') == {"intent": "x", "patch": []}
    assert extract_json('prefix {"a": "b}"} suffix') == {"a": "b}"}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_local_fallback_when_w4_patch_missing(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "proofread.genome.patch", None)  # import raises ImportError
    c = validate_edit(Genome(), [{"op": "add", "path": "/memory_notes/-", "value": "n"}])
    assert c.memory_notes == ["n"]
    with pytest.raises(PatchError, match="schema"):
        validate_edit(Genome(), [{"op": "replace", "path": "/workflow/max_turns", "value": 999}])
    with pytest.raises(PatchError, match="shrink"):
        g = Genome().model_copy(update={"tools": Genome().tools.model_copy(update={"enabled": ["run"]})})
        validate_edit(g, [{"op": "add", "path": "/tools/enabled/-", "value": "read_file"}])
