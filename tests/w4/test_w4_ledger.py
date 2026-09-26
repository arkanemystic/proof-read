import pytest

from proofread.contracts import BudgetExceeded
from proofread.models.ledger import SpendLedger


def test_caps_across_two_instances(tmp_path):
    db = tmp_path / "spend.sqlite"
    a = SpendLedger(db, total_cap=10.0, caps={"arm_A": 1.0, "arm_C": 5.0})
    b = SpendLedger(db, total_cap=10.0, caps={"arm_A": 1.0, "arm_C": 5.0})
    a.check("arm_A")
    a.record(role="agent", model="m", budget_key="arm_A", input_tokens=10, output_tokens=5, cost_usd=0.6)
    b.check("arm_A")  # 0.6 < 1.0
    b.record(role="agent", model="m", budget_key="arm_A", input_tokens=10, output_tokens=5, cost_usd=0.5)
    with pytest.raises(BudgetExceeded):
        a.check("arm_A")
    with pytest.raises(BudgetExceeded):
        b.check("arm_A")
    b.check("arm_C")  # other key unaffected
    assert abs(a.spent("arm_A") - 1.1) < 1e-9 and abs(b.total() - 1.1) < 1e-9
    # total cap applies to every key, including unknown ones
    a.record(role="proposer", model="p", budget_key="misc", input_tokens=1, output_tokens=1, cost_usd=9.0)
    with pytest.raises(BudgetExceeded):
        b.check("arm_C")
    with pytest.raises(BudgetExceeded):
        b.check("")
    assert b.by_key()["misc"] == 9.0


def test_default_caps_from_contracts(tmp_path):
    from proofread.contracts import BUDGET_CAPS_USD, BUDGET_TOTAL_USD

    led = SpendLedger(tmp_path / "s.sqlite")
    assert led.total_cap == BUDGET_TOTAL_USD and led.caps == BUDGET_CAPS_USD
