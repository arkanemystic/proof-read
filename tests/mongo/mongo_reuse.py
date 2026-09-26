"""Load existing SQLite test modules under private names so their bodies can run on Mongo."""

from __future__ import annotations

import importlib.util
from pathlib import Path

W5 = Path(__file__).resolve().parents[1] / "w5"


def load_w5(name: str):
    """Import an existing SQLite test module under a private name (the original stays untouched)."""
    path = W5 / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_mongo_reuse_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
