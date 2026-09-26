"""Build results/final/ (figures, dashboard.html, NUMBERS.md, numbers.json) from stored data.

usage: uv run python scripts/build_final.py [--imp-db data/imp.sqlite] [--rerun-db data/rerun.sqlite]
       [--spend-db data/final_spend.sqlite] [--rt-gate results/rerun/rt_gate.json] [--out results/final]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proofread.analysis.final.build import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
