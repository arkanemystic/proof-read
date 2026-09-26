"""Normalized Action records: the unit every policy judges.

Actions describe effects observed on the workspace (write, delete, rename, chmod), commands
executed (exec), network attempts (net) and attempts to touch paths outside the sandbox
(attempt_outside). Policies judge effects, not the command text.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ActionKind = Literal["write", "delete", "rename", "chmod", "exec", "net", "attempt_outside"]
ACTION_KINDS: tuple[str, ...] = ("write", "delete", "rename", "chmod", "exec", "net", "attempt_outside")


class Action(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: str
    step: int = Field(ge=-1)  # -1 marks the pre-grade final diff
    kind: ActionKind
    path: str = ""  # absolute, canonical (e.g. /workspace/src/x.py)
    dst: str = ""  # rename destination, canonical
    added_lines: list[str] = Field(default_factory=list)
    removed_lines: list[str] = Field(default_factory=list)
    section: str = ""  # config section touched, e.g. "tool:pytest" or "tool.pytest.ini_options"
    host: str = ""  # net destination host
    cmd: str = ""  # exec command line (informational; policies judge effects)
    attributed: bool = True
    protected_extra: list[str] = Field(default_factory=list)  # manifest test paths for this task

    def canonical_json(self) -> str:
        """Deterministic JSON used for hashing and for the verifier wire format."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    violations: list[str] = Field(default_factory=list)  # policy ids
    source: str = "reference"  # "biject" | "reference" | "failclosed"
    detail: str = ""
    latency_ms: float = 0.0
