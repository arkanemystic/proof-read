import sys
from pathlib import Path

_here = Path(__file__).parent
for d in (_here, _here.parent / "differential", _here.parent / "golden_cheats"):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
