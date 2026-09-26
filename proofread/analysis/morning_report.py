"""Generate MORNING_REPORT.md at the repo root from MORNING_REPORT_TEMPLATE.md.

    python -m proofread.analysis.morning_report --root . --results results/ --spend data/spend.sqlite

Inputs are read defensively: every missing file is reported as missing, never papered over.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from proofread.analysis.load import load_spend, read_text, spend_summary

TEMPLATE = Path(__file__).with_name("MORNING_REPORT_TEMPLATE.md")
DECISION_KEYWORDS = ("CUT", "select", "fallback", "provisional", "PROVISIONAL", "biject", "gate", "arm",
                     "budget", "cap", "model", "policy", "Lean", "compress")


def what_finished(progress: str | None) -> str:
    if progress is None:
        return "PROGRESS.md missing."
    done, other = [], []
    for line in progress.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.strip().startswith("|") or len(cells) < 2:
            continue
        item, status = cells[0], cells[1].lower()
        if item.lower() in ("item", "boundary") or set(item) <= set("-: ") or re.match(r"^\d{4}-", status):
            continue
        rest = " | ".join(cells[2:])
        (done if status.startswith("done") else other).append(f"- {item}: {cells[1]}" + (f" ({rest})" if rest else ""))
    out = ["Finished:", *(done or ["- (none recorded)"]), "", "Not finished / other status:",
           *(other or ["- (none)"])]
    return "\n".join(out)


def headline(summary: dict[str, Any] | None) -> str:
    if summary is None:
        return "results/summary.json missing: the analysis was not run, so there are no headline numbers."
    lines = []
    if summary.get("n_episodes", 0) == 0:
        lines.append("**No episode data.**")
    if "SYNTHETIC" in str(summary.get("source", "")):
        lines.append("**WARNING: these numbers come from SYNTHETIC data and are not results.**")
    lines += [f"- {h}" for h in summary.get("headline", [])]
    caps = summary.get("capability", [])
    if caps:
        lines.append("")
        lines.append("Capability (pristine pass rate on original tasks):")
        for c in caps:
            if c.get("n"):
                lines.append(f"- {c['label']}: {100 * c['rate']:.1f}% [{100 * c['lo']:.1f}, {100 * c['hi']:.1f}] "
                             f"({c['k']}/{c['n']})")
            else:
                lines.append(f"- {c['label']}: no data")
    lines.append("")
    lines.append(f"Rejected edits with positive counterfactual delta: {summary.get('rejected_positive', 0)}.")
    return "\n".join(lines)


def provisional(summary: dict[str, Any] | None, decisions: str | None, blocked: str | None) -> str:
    lines = []
    if summary is not None:
        p = summary.get("provisional", {})
        n, k = p.get("n", 0), p.get("provisional", 0)
        if k:
            lines.append(f"- {k} of {n} episodes are PROVISIONAL-NO-BIJECT: they were checked locally instead of "
                         "by biject-api (B-002, D-013). Experiment runs used the compiled Lean 4 policies "
                         "(PROOFREAD_VERIFIER=lean), with the Python reference as differential oracle. Every "
                         "formal-gate decision and every violation count that depends on them is provisional "
                         "until replayed against biject-api.")
        else:
            lines.append(f"- No provisional episodes among {n}: all verdicts came from biject-api.")
    else:
        lines.append("- results/summary.json missing; provisional status unknown.")
    lines.append("- The Lean policies themselves are DRAFT: PENDING HUMAN REVIEW, so every verdict (biject or "
                 "reference) is provisional on that review.")
    for src, text in (("DECISIONS.md", decisions), ("BLOCKED.md", blocked)):
        for line in (text or "").splitlines():
            if "PROVISIONAL" in line.upper() and line.strip():
                lines.append(f"- ({src}) {line.strip()}")
    return "\n".join(lines)


def cuts(decisions: str | None) -> str:
    if decisions is None:
        return "DECISIONS.md missing."
    hits = [line.strip() for line in decisions.splitlines() if "CUT" in line]
    return "\n".join(f"- {h}" for h in hits) if hits else "No cuts recorded (no DECISIONS.md line contains 'CUT')."


def key_decisions(decisions: str | None, limit: int = 400) -> str:
    if decisions is None:
        return "DECISIONS.md missing."
    entries: list[str] = []
    for line in decisions.splitlines():
        if re.match(r"^\s*-?\s*D-\d+", line):
            entries.append(line.strip())
        elif entries and line.strip() and not line.startswith("#"):
            entries[-1] += " " + line.strip()
    if len(entries) <= 30:
        keyed = entries
    else:
        keyed = [e for e in entries if any(k.lower() in e.lower() for k in DECISION_KEYWORDS)] or entries
    if not keyed:
        return "No D-numbered entries in DECISIONS.md."
    out = [f"- {e[:limit]}{'...' if len(e) > limit else ''}" for e in keyed]
    note = "all entries" if len(keyed) == len(entries) else "filtered by keywords; see DECISIONS.md for all"
    return f"{len(keyed)} of {len(entries)} entries ({note}; long entries truncated):\n\n" + "\n".join(out)


def spend(spend_rows: list[dict[str, Any]], summary: dict[str, Any] | None) -> str:
    s = spend_summary(spend_rows)
    by_role = s["by_role"] or (summary or {}).get("spend_by_role") or {}
    if not by_role:
        ec = (summary or {}).get("episode_cost") or {}
        if ec:
            rows = "\n".join(f"| {k} | {v:.2f} |" for k, v in ec.items())
            return ("Spend ledger unavailable. Episode-reported cost only (excludes proposer):\n\n"
                    "| group | USD |\n|---|---|\n" + rows)
        return "Spend ledger unavailable and no episode costs: spend unknown."
    rows = "\n".join(f"| {k} | {v:.2f} |" for k, v in sorted(by_role.items()))
    total = sum(by_role.values())
    return f"| role | USD |\n|---|---|\n{rows}\n| **total** | **{total:.2f}** |\n\nCaps: total 150, per arm 35, baselines 25, selection 10."


def review(root: Path) -> str:
    lean_dir = root / "proofread" / "policies" / "lean"
    lean = sorted(p.relative_to(root).as_posix() for p in lean_dir.rglob("*.lean")
                  if ".lake" not in p.parts) if lean_dir.exists() else []
    axioms = root / "proofread" / "policies" / "AXIOMS.txt"
    lines = ["1. **Lean policies (first).** Every file is marked DRAFT: PENDING HUMAN REVIEW. Check each policy "
             "says what its ID claims, and that path and marker classification is right:"]
    lines += [f"   - {p}" for p in lean] or ["   - (no .lean files found under proofread/policies/lean)"]
    if axioms.exists():
        txt = axioms.read_text(encoding="utf-8", errors="replace").strip()
        lines.append("   - proofread/policies/AXIOMS.txt (`#print axioms` output; expect only standard axioms):")
        lines.append("\n```\n" + (txt[:3000] + ("\n..." if len(txt) > 3000 else "")) + "\n```")
    else:
        lines.append("   - proofread/policies/AXIOMS.txt MISSING")
    lines += [
        "2. Reference verifier vs biject-api differential results (tests/differential) and fail-closed tests "
        "(tests/failclosed), especially if any number above is PROVISIONAL.",
        "3. The key figure and its caveats in results/results.md (enforce-mode rows are low by construction; "
        "compare observe rows and violation-attempt rates).",
        "4. Rejected edits with positive counterfactual delta (results.md section 4): are formal rejections real cheats?",
        "5. Model selection table and the pre-registered rule outcome (results/model_selection.md).",
        "6. BLOCKED.md items and every CUT above.",
    ]
    return "\n".join(lines)


def build_morning_report(root: str | Path = ".", results_dir: str | Path | None = None,
                         spend_path: str | Path | None = None, out_path: str | Path | None = None) -> Path:
    root = Path(root)
    results = Path(results_dir) if results_dir else root / "results"
    summary = None
    sp = results / "summary.json"
    if sp.exists():
        try:
            summary = json.loads(sp.read_text(encoding="utf-8"))
        except ValueError:
            summary = None
    progress = read_text(root / "PROGRESS.md")
    decisions = read_text(root / "DECISIONS.md")
    blocked = read_text(root / "BLOCKED.md")
    spend_rows = load_spend(spend_path if spend_path else root / "data" / "spend.sqlite")
    fill = {
        "GENERATED": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "WHAT_FINISHED": what_finished(progress),
        "HEADLINE": headline(summary),
        "PROVISIONAL": provisional(summary, decisions, blocked),
        "CUTS": cuts(decisions),
        "BLOCKED": blocked.strip() if blocked is not None else "BLOCKED.md missing.",
        "DECISIONS": key_decisions(decisions),
        "SPEND": spend(spend_rows, summary),
        "REVIEW": review(root),
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    for k, v in fill.items():
        text = text.replace("{{" + k + "}}", v)
    out = Path(out_path) if out_path else root / "MORNING_REPORT.md"
    out.write_text(text, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate MORNING_REPORT.md")
    ap.add_argument("--root", default=".")
    ap.add_argument("--results", default=None)
    ap.add_argument("--spend", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    print(build_morning_report(a.root, a.results, a.spend, a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
