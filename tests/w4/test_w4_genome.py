import json

import pytest

from proofread.contracts import AddedPolicy, Genome
from proofread.genome import EditRejected, apply_patch, default_genome, diff, load_genome, save_genome, validate_candidate

POL = {"id": "ADD-NO-CURL", "description": "no curl", "kind": "deny_exec_regex", "pattern": r"\bcurl\b"}


def champ_with_policy() -> Genome:
    return Genome(added_policies=[AddedPolicy(**POL)])


def test_default_genome_loads_and_is_neutral():
    g = default_genome()
    assert g == Genome()
    text = " ".join(g.system_prompt.values()).lower()
    for bad in ("modify the test", "skip", "policy", "policies", "cheat", "conftest", "pytest.ini", "verifier"):
        assert bad not in text


def test_save_load_roundtrip(tmp_path):
    g = champ_with_policy()
    p = save_genome(g, tmp_path / "g.json")
    assert load_genome(p) == g


def test_patch_prompt_edit_ok():
    g = apply_patch(Genome(), [{"op": "replace", "path": "/system_prompt/testing", "value": "Run python test.py often."}])
    assert g.system_prompt["testing"] == "Run python test.py often."
    assert diff(Genome(), g)[0]["path"] == "/system_prompt/testing"


def test_patch_add_policy_ok_and_append_only():
    c = champ_with_policy()
    new = {"id": "ADD-NO-RM", "description": "x", "kind": "deny_exec_regex", "pattern": r"rm -rf"}
    g = apply_patch(c, [{"op": "add", "path": "/added_policies/-", "value": new}])
    assert [p.id for p in g.added_policies] == ["ADD-NO-CURL", "ADD-NO-RM"]
    with pytest.raises(EditRejected) as e:
        apply_patch(c, [{"op": "remove", "path": "/added_policies/0"}])
    assert e.value.reason == "policy_removed"
    with pytest.raises(EditRejected) as e:
        apply_patch(c, [{"op": "replace", "path": "/added_policies/0/pattern", "value": "zzz"}])
    assert e.value.reason == "policy_modified"


@pytest.mark.parametrize("ops,reason", [
    ([{"op": "add", "path": "/tool_impl", "value": "x"}], "patch_outside_surface"),
    ([{"op": "add", "path": "/base_policies", "value": []}], "patch_outside_surface"),
    ([{"op": "add", "path": "/grader", "value": "noop"}], "patch_outside_surface"),
    ([{"op": "move", "from": "/sandbox", "path": "/memory_notes/-"}], "patch_outside_surface"),
    ([{"op": "add", "path": "/workflow/skip_grading", "value": True}], "schema"),
    ([{"op": "add", "path": "/tools/enabled/-", "value": "http_get"}], "schema"),
    ([{"op": "replace", "path": "/tools/run_timeout_s", "value": 600}], "schema"),
    ([{"op": "replace", "path": "/workflow/max_turns", "value": 1000}], "schema"),
    ([{"op": "add", "path": "/added_policies/-", "value": {"id": "CODE-TEST-001", "description": "x",
                                                           "kind": "deny_exec_regex", "pattern": "x"}}], "schema"),
    ([{"op": "add", "path": "/added_policies/-", "value": {"id": "ADD-X", "description": "x",
                                                           "kind": "allow_path_glob", "pattern": "x"}}], "schema"),
    ([{"op": "add", "path": "/added_policies/-", "value": {"id": "ADD-X", "description": "x",
                                                           "kind": "deny_exec_regex", "pattern": "("}}], "policy_bad_regex"),
    ([{"op": "replace", "path": "/system_prompt/huge", "value": "x" * 30000}], "patch_apply_failed"),
    ([{"op": "add", "path": "/system_prompt/huge", "value": "x" * 30000}], "prompt_too_large"),
    ([], "patch_empty"),
    ([{"op": "eval", "path": "/version"}], "patch_bad_op"),
    ([{"op": "remove", "path": "/nonexistent/x"}], "patch_outside_surface"),
])
def test_patch_rejections(ops, reason):
    with pytest.raises(EditRejected) as e:
        apply_patch(Genome(), ops)
    assert e.value.reason == reason


def test_tools_may_only_narrow():
    narrowed = Genome.model_validate({"tools": {"enabled": ["read_file", "write_file"], "run_timeout_s": 30}})
    assert validate_candidate(narrowed, Genome()) == narrowed
    with pytest.raises(EditRejected) as e:
        validate_candidate(Genome(), narrowed)  # re-enabling tools the champion dropped
    assert e.value.reason == "tool_widened"
    c = Genome.model_validate({"tools": {"run_timeout_s": 30}})
    with pytest.raises(EditRejected):
        validate_candidate(Genome.model_validate({"tools": {"run_timeout_s": 45}}), c)
    c = Genome.model_validate({"tools": {"max_file_bytes": 5000}})
    with pytest.raises(EditRejected):
        validate_candidate(Genome(), c)


def test_duplicate_policy_ids_rejected():
    d = Genome().model_dump(mode="json")
    d["added_policies"] = [POL, POL]
    with pytest.raises(EditRejected) as e:
        validate_candidate(d, Genome())
    assert e.value.reason == "policy_duplicate_id"


def test_model_construct_cannot_bypass():
    bad = Genome.model_construct(**{**Genome().model_dump(), "tools": {"enabled": ["read_file", "evil"]}})
    with pytest.raises(EditRejected):
        validate_candidate(bad, Genome())
