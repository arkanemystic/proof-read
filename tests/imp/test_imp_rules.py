"""Section 9 IMP additions: cost promotion rule (D-F02), OpenRouter proposer routing, IMP splits."""

from proofread.benchmarks import impossiblebench as ib
from proofread.contracts import EpisodeResult
from proofread.evolve.config import arm_preset
from proofread.evolve.gates import decide
from proofread.evolve.stats import cost_gate
from proofread.models import config as mcfg


def _eps(passes: list[bool], costs: list[float], viol: bool = False) -> list[EpisodeResult]:
    return [EpisodeResult(episode_id=f"e{i}", task_id=f"t{i}", variant="original", genome_hash="h", model="m",
                          mode="observe", seed=0, passed_workspace=p, cost_usd=c,
                          violations=["CODE-TEST-001"] if viol and i == 0 else [])
            for i, (p, c) in enumerate(zip(passes, costs))]


def test_cost_gate_promotes_cheaper_equal_pass():
    champ = {(f"t{i}", 0): (i % 2 == 0, 0.020) for i in range(20)}
    cand = {(f"t{i}", 0): (i % 2 == 0, 0.012) for i in range(20)}
    s = cost_gate(cand, champ)
    assert s.promote and abs(s.saving_fraction - 0.4) < 1e-9 and s.lower_bound_saving_usd > 0


def test_cost_gate_rejects_lower_pass_rate_or_small_saving():
    champ = {(f"t{i}", 0): (True, 0.020) for i in range(20)}
    cand = {(f"t{i}", 0): (i != 0, 0.010) for i in range(20)}
    assert not cost_gate(cand, champ).promote  # pass rate lower
    cand2 = {(f"t{i}", 0): (True, 0.018) for i in range(20)}
    assert not cost_gate(cand2, champ).promote  # 10% < 15%


def test_decide_cost_rule_only_when_enabled_and_formal_first():
    champ = _eps([True] * 10 + [False] * 10, [0.02] * 20)
    cand = _eps([True] * 10 + [False] * 10, [0.01] * 20)
    off = decide(arm_preset("C"), cand, champ)
    assert off.status == "rejected_empirical"
    on = decide(arm_preset("C", cost_rule=True), cand, champ)
    assert on.status == "passed_gates" and on.rule == "cost" and on.as_dict()["cost"]["promote"]
    bad = _eps([True] * 10 + [False] * 10, [0.01] * 20, viol=True)
    assert decide(arm_preset("C", cost_rule=True), bad, champ).status == "rejected_formal"
    assert decide(arm_preset("A", cost_rule=True), bad, champ).status == "passed_gates"


def test_proposer_routing():
    assert mcfg.provider_for("proposer", "deepseek/deepseek-v4-pro") == ("openrouter", "OPENROUTER_API_KEY")
    assert mcfg.provider_for("proposer", "claude-opus-5-5") == ("anthropic", "PROPOSER_API_KEY")


def test_imp_splits_disjoint_and_locked():
    t, h = ib.list_tasks("imp_training"), ib.list_tasks("imp_capability_holdout")
    assert len(t) == 24 and len(h) == 40 and not set(t) & set(h)
    assert h == ib.list_tasks("capability_holdout")
    assert all(x.startswith("lcb/original/") for x in t + h)


def test_agent_only_reasoning_override(monkeypatch):
    monkeypatch.delenv("PROOFREAD_REASONING_EFFORT_FILE", raising=False)
    monkeypatch.delenv("PROOFREAD_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("PROOFREAD_AGENT_REASONING_EFFORT", "none")
    assert mcfg.reasoning_effort("agent", "qwen/qwen3.7-flash") == "none"
    assert mcfg.reasoning_effort("proposer", "deepseek/deepseek-v4-pro") == ""
