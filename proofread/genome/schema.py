"""Genome schema (re-exported from contracts, decision D-005) plus load/save helpers."""

from __future__ import annotations

import json
from pathlib import Path

from proofread.contracts import AddedPolicy, ContextPolicy, Genome, ToolConfig, Workflow

DEFAULT_GENOME_PATH = Path(__file__).resolve().parent / "default_genome.json"

__all__ = ["AddedPolicy", "ContextPolicy", "Genome", "ToolConfig", "Workflow", "DEFAULT_GENOME_PATH",
           "load_genome", "save_genome", "default_genome", "genome_from_dict"]


def genome_from_dict(d: dict) -> Genome:
    return Genome.model_validate(d)


def load_genome(path: str | Path) -> Genome:
    return Genome.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_genome(genome: Genome, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(genome.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def default_genome() -> Genome:
    """The initial champion. Neutral prompt: no cheating instructions and no policy hints."""
    return load_genome(DEFAULT_GENOME_PATH)
