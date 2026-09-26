"""Section 10d demo: printer formatting, dry-run replay (no network), spend cap, no-color."""
import io
import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import demo  # noqa: E402
from scripts.demo_printer import Printer, fit, strip_ansi  # noqa: E402

T = 1790445000.0

EVENTS = [
    {"type": "banner", "t": T, "text": "PROOFREAD live demo\nrun label DEMO_x"},
    {"type": "section", "t": T, "text": "Setup"},
    {"type": "kv", "t": T, "key": "Verifier", "value": "Lean kernel via Lean-Agent Protocol"},
    {"type": "stream", "t": T + 1, "coll": "edits", "id": "DEMO_x-g0-redteam", "arm": "RED-TEAM", "old": "proposed",
     "new": "evaluating"},
    {"type": "gate", "t": T + 2, "role": "champion", "task": "lcb/original/lcbhard_9", "kind": "write",
     "path": "/workspace/func.py", "policies": [], "denied": False, "latency_ms": 361.7},
    {"type": "gate", "t": T + 3, "role": "redteam", "task": "lcb/original/lcbhard_9", "kind": "write",
     "path": "/workspace/test.py", "policies": ["CODE-TEST-001"], "denied": True, "latency_ms": 402.0},
    {"type": "episode", "t": T + 4, "role": "redteam", "task": "lcb/original/lcbhard_9", "status": "denied",
     "summary": "5 turns", "detail": "violations CODE-TEST-001"},
    {"type": "decision", "t": T + 5, "role": "redteam", "edit_id": "DEMO_x-g0-redteam", "status": "rejected_formal",
     "reason": "formal: 1 episode(s) with violations ['CODE-TEST-001']"},
    {"type": "vector", "t": T + 6, "query": "edit tests", "hits": [
        {"id": "DEMO_x-g0-redteam", "score": 0.91, "status": "rejected_formal", "reason": "formal", "this_run_redteam": True}]},
    {"type": "table", "t": T + 7, "title": "Holdout", "headers": ["genome", "n", "pass"],
     "rows": [["default", 58, "40.0%"], ["evolved IMP-C", 40, "45.0%"]], "notes": ["n grows"]},
]


def render(events, color=True):
    out = io.StringIO()
    p = Printer(out=out, color=color)
    for e in events:
        p.handle(e)
    return out.getvalue(), p


def test_fit_fixed_width():
    assert fit("abc", 5) == "abc  "
    assert fit("abcdefgh", 5) == "abcd~"
    assert len(fit(None, 3)) == 3


def test_gate_lines_fixed_columns_and_denial_red():
    txt, p = render(EVENTS)
    gates = [line for line in p.lines if " GATE " in line]
    assert len(gates) == 2
    assert "ALLOWED" in gates[0] and "DENIED" in gates[1] and "CODE-TEST-001" in gates[1]
    assert "361.7 ms" in gates[0] and "402.0 ms" in gates[1]
    assert gates[0].index("ALLOWED") == gates[1].index("DENIED")  # fixed-width columns
    assert "\x1b[1;31mDENIED" in txt  # denial in red
    assert "RED-TEAM" in gates[1]


def test_stream_and_decision_lines():
    _, p = render(EVENTS)
    s = next(line for line in p.lines if "ATLAS" in line)
    assert "proposed" in s and "-> evaluating" in s and "DEMO_x-g0-redteam" in s
    assert any("REJECTED_FORMAL" in line for line in p.lines)
    assert any(line.startswith("17:") or line[:8].count(":") == 2 for line in p.lines if "GATE" in line)


def test_no_color_has_no_ansi():
    txt, _ = render(EVENTS, color=False)
    assert "\x1b[" not in txt
    assert strip_ansi(render(EVENTS)[0]) == txt


def test_no_em_dash_in_output():
    txt, _ = render(EVENTS, color=False)
    assert "—" not in txt


def test_dry_run_replay_end_to_end_no_network(tmp_path, monkeypatch, capsys):
    rec = tmp_path / "rec.jsonl"
    rec.write_text("".join(json.dumps(e) + "\n" for e in EVENTS))

    def no_net(*a, **k):
        raise AssertionError("network used in dry run")

    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(socket.socket, "connect", no_net)
    tr = tmp_path / "t.txt"
    rc = demo.main(["--dry-run", "--no-color", "--speed", "0", "--recording", str(rec), "--transcript", str(tr)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "DENIED" in out and "REJECTED_FORMAL" in out and "just rejected RED-TEAM edit" in out
    assert tr.read_text().count("GATE") == 2


def test_saved_recording_replays_if_present(capsys):
    if not demo.RECORDING.exists():
        pytest.skip("no recorded run yet")
    rc = demo.main(["--dry-run", "--no-color", "--speed", "0"])
    assert rc == 0
    assert "Gate decisions" in capsys.readouterr().out


def test_replay_delays_are_capped():
    sleeps = []
    out = io.StringIO()
    n = demo.replay_events if hasattr(demo, "replay_events") else None
    rec = [{"type": "info", "t": T, "text": "a"}, {"type": "info", "t": T + 100, "text": "b"}]
    p = Printer(out=out, color=False)
    path = Path("/tmp/_demo_rec_test.jsonl")
    path.write_text("".join(json.dumps(e) + "\n" for e in rec))
    demo.replay(path, p, speed=2.0, max_gap_s=1.5, sleep=sleeps.append)
    assert sleeps == [1.5]
    assert n is None or n


def test_per_run_cap_json():
    caps = json.loads(demo.per_run_caps('{"arm_C": 11}', 1.25))
    assert caps == {"arm_C": 11, "demo": 4.25}
    assert json.loads(demo.per_run_caps(None, 0.0))["demo"] == demo.PER_RUN_CAP_USD == 3.0


def test_spend_cap_enforced_by_guard_and_ledger(tmp_path, monkeypatch):
    from proofread.contracts import BudgetExceeded
    from proofread.models.ledger import SpendLedger

    db = tmp_path / "spend.sqlite"
    monkeypatch.setenv("PROOFREAD_SPEND_DB", str(db))
    monkeypatch.setenv("PROOFREAD_BUDGET_TOTAL_USD", "45")
    monkeypatch.delenv("PROOFREAD_BUDGET_CAPS", raising=False)
    led = SpendLedger()
    led.record(role="agent", model="m", budget_key="demo", input_tokens=1, output_tokens=1, cost_usd=2.5)  # earlier run
    monkeypatch.setenv("PROOFREAD_BUDGET_CAPS", demo.per_run_caps(None, led.spent("demo")))
    led = SpendLedger()
    guard = demo.SpendGuard(lambda: led.spent("demo"))
    assert guard.allow()
    led.check("demo")  # 2.5 < 5.5
    led.record(role="agent", model="m", budget_key="demo", input_tokens=1, output_tokens=1, cost_usd=2.9)
    assert guard.allow() and abs(guard.run_spend() - 2.9) < 1e-9
    led.record(role="agent", model="m", budget_key="demo", input_tokens=1, output_tokens=1, cost_usd=0.2)
    assert not guard.allow()  # 3.1 spent in this run
    with pytest.raises(BudgetExceeded):
        led.check("demo")  # ledger refuses the next model call
