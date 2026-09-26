"""biject-oss backend: typed rendering is injection-proof and goal building matches the Lean lemmas.

Pure Python (no Lean, no server). The live kernel is covered by test_live.py and
tests/differential/test_biject_vs_reference.py.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "biject-oss" / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "biject-oss" / "lean-worker"))

from app import orchestrator, render  # noqa: E402
from app.policy_registry import PROOFREAD_TOOL, REGISTRY, get_policies_for_tool  # noqa: E402

from proofread.contracts import BASE_POLICY_IDS  # noqa: E402

import worker  # noqa: E402

_CHAR = re.compile(r"'[^'\\]'|\(Char\.ofNat \d+\)")


def parse_chars(term: str) -> str:
    """Inverse of render.lean_chars, strict: fails on anything but char literals."""
    assert term.startswith("[") and term.endswith("]")
    inner = term[1:-1]
    out, pos = [], 0
    while pos < len(inner):
        m = _CHAR.match(inner, pos)
        assert m, inner[pos:pos + 20]
        tok = m.group(0)
        out.append(tok[1] if tok.startswith("'") else chr(int(tok[len("(Char.ofNat "):-1])))
        pos = m.end()
        if pos < len(inner):
            assert inner[pos] == ",", inner[pos:pos + 20]
            pos += 1
    return "".join(out)


@settings(max_examples=500)
@given(st.text())
def test_chars_roundtrip_and_never_trip_the_worker_filter(s):
    term = render.lean_chars(s)
    assert parse_chars(term) == s
    assert worker.FORBIDDEN_IN_PROP.search(term) is None


@pytest.mark.parametrize("s", ['"', "'", "\\", "\\'", "'--", "-- x", "/- c -/", "\n#eval 1", "x := by trivial",
                               "sorry", "@[simp]", "\x00", "\U0001F600", "é", "\u202e"])
def test_adversarial_strings_stay_data(s):
    term = render.lean_chars(s)
    assert parse_chars(term) == s
    assert worker.FORBIDDEN_IN_PROP.search(term) is None


def test_lone_surrogate_rejected():
    with pytest.raises(render.RenderError):
        render.lean_chars("a\ud800")


@pytest.mark.parametrize("value,transform,err", [
    (1, "bool", True), ("true", "bool", True), (True, "bool", False),
    ("write", "enum", False), ("launch", "enum", True), (".write", "enum", True), (3, "enum", True),
    (5, "chars", True), ("[1]", "chars_list_json", True), ("{}", "chars_list_json", True),
    ("not json", "chars_list_json", True), ('["a"]', "chars_list_json", False), (True, "int", True),
    ("x", "nope", True),
])
def test_transforms_validate_types(value, transform, err):
    enum = {"write": ".write"}
    if err:
        with pytest.raises((render.RenderError, ValueError)):
            render.render(value, transform, enum=enum)
    else:
        render.render(value, transform, enum=enum)


def params(**kw):
    base = {"kind": "write", "path": "/workspace/f.py", "dst": "", "section": "", "host": "", "attributed": True,
            "added_lines_json": "[]", "added_lines_nfkc_json": "[]", "protected_extra_json": "[]",
            "net_allowlist_json": "[]"}
    base.update(kw)
    return base


def test_registry_covers_every_base_policy_with_a_lemma():
    pols = get_policies_for_tool(PROOFREAD_TOOL)
    assert sorted(p["policy_id"] for p in pols) == sorted(BASE_POLICY_IDS)
    for p in pols:
        assert p["on_missing"] == "error" and p["lemma"].startswith("Proofread.")
        lean = (Path(__file__).resolve().parents[2] / "proofread/policies/lean/Proofread/Policies.lean").read_text()
        assert f"theorem {p['lemma'].removeprefix('Proofread.')} " in lean


def test_one_goal_per_policy_and_fields_match_lemmas():
    for p in REGISTRY.values():
        goals = orchestrator.build_goals(p, params())
        assert len(goals) == 1 and goals[0].endswith(" = true")
        assert goals[0].startswith(f"Proofread.{p['lean_function']} ")
    net = orchestrator.build_goals(REGISTRY["CODE-NET-001"], params(net_allowlist_json='["a.com"]'))[0]
    assert net.startswith("Proofread.codeNet001C [['a','.','c','o','m']] ({ kind := .write, host := [] }")


def test_missing_or_bad_params_fail_closed():
    for p in REGISTRY.values():
        for key in set(p["parameter_map"].values()) | set(p["positional_map"].values()):
            bad = params()
            del bad[key]
            with pytest.raises(render.RenderError):
                orchestrator.build_goals(p, bad)
    with pytest.raises(render.RenderError):
        orchestrator.build_goals(REGISTRY["CODE-SKIP-001"], params(added_lines_json='"x"'))


@settings(max_examples=200)
@given(raw=st.lists(st.text(max_size=300), max_size=60), nfkc=st.lists(st.text(max_size=300), max_size=60))
def test_skip_chunks_cover_exactly_the_set_of_lines(raw, nfkc):
    """The hypothesis of Proofread.codeSkip001C_chunks: chunks cover exactly raw ∪ nfkc."""
    pol = REGISTRY["CODE-SKIP-001"]
    goals = orchestrator.build_goals(pol, params(added_lines_json=json.dumps(raw), added_lines_nfkc_json=json.dumps(nfkc)))
    seen: list[str] = []
    for g in goals:
        m = re.fullmatch(r"Proofread\.codeSkip001C \(\{ kind := \.write, addedLines := \[(.*)\] \} : Proofread\.ActionC\) = true", g)
        assert m, g[:200]
        chunk_terms = re.findall(r"\[(?:'[^'\\]'|\(Char\.ofNat \d+\)|,)*\]", m.group(1))
        chunk = [parse_chars(t) for t in chunk_terms]
        assert ",".join(chunk_terms) == m.group(1)
        assert sum(len(x) for x in chunk) <= pol["chunk"]["max_chars"] or len(chunk) == 1
        seen += chunk
    assert set(seen) == set(raw) | set(nfkc)
    assert len(seen) == len(set(seen))


def test_batches_respect_limits():
    goals = [{"id": str(i), "prop": "x" * n} for i, n in enumerate([10] * 300 + [15_000, 30_000])]
    batches = orchestrator._batches(goals)
    assert sorted(g["id"] for b in batches for g in b) == sorted(g["id"] for g in goals)
    assert all(len(b) <= 256 for b in batches)
    assert all(sum(len(g["prop"]) for g in b) <= orchestrator.BATCH_RENDERED_CHARS or len(b) == 1 for b in batches)


def test_worker_source_hash_matches_client_algorithm():
    from proofread.verify.lean_verifier import LEAN_DIR, lean_source_hash

    assert worker.source_hash(LEAN_DIR) == lean_source_hash()
