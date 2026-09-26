"""Typed facts for the Lean-Agent Protocol (LAP) verifier.

Trust boundary: every string judgement (path canonicalization, protected-test and grader-config
classification, scope roots, skip-marker regexes with NFKC folding, host allowlist normalization) is
done here by trusted Python, by calling the classification functions in proofread/policies/reference.py.
The resulting facts are Bool and Nat only. The policy decision over those facts is made by the Lean
kernel (`decide`) in proofread/policies/lap/PROOFREAD.lean, served by LAP's lean-worker.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass, fields

from proofread.actions.canonicalize import canon_path
from proofread.contracts import Action
from proofread.policies import reference as ref

KIND_CODES: dict[str, int] = {
    "write": 0,
    "delete": 1,
    "rename": 2,
    "chmod": 3,
    "exec": 4,
    "net": 5,
    "attempt_outside": 6,
}

LEAN_NAMESPACE = "PolicyEnv.Proofread"
LEAN_MODULE = "PolicyEnv.PROOFREAD"

# Lean predicate name per base policy id (true = allowed).
POLICY_PREDICATES: dict[str, str] = {
    "CODE-TEST-001": "testOk",
    "CODE-SKIP-001": "skipOk",
    "CODE-GRADER-001": "graderOk",
    "CODE-SCOPE-001": "scopeOk",
    "CODE-NET-001": "netOk",
    "CODE-ATTR-001": "attrOk",
}


@dataclass(frozen=True)
class Facts:
    """Field order and names mirror `structure Facts` in PROOFREAD.lean."""

    kind: int
    path_present: bool
    dst_present: bool
    is_protected_test_path: bool
    dst_is_protected_test_path: bool
    added_skip_markers: int
    touches_grader_config: bool
    dst_touches_grader_config: bool
    write_in_scope: bool
    dst_in_scope: bool
    host_allowlisted: bool
    attributed: bool

    def key(self) -> tuple:
        return astuple(self)

    def lean_literal(self) -> str:
        parts = []
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, bool):
                lit = "true" if v else "false"
            elif isinstance(v, int) and v >= 0:
                lit = str(v)
            else:  # pragma: no cover - construction guarantees Bool/Nat
                raise ValueError(f"fact {f.name} is not Bool/Nat: {v!r}")
            parts.append(f"{f.name} := {lit}")
        return "{ " + ", ".join(parts) + " }"


def _host_allowlisted(a: Action, net_allowlist: tuple[str, ...]) -> bool:
    # Reuse reference.check_net's normalization by judging a net-kind copy of the action.
    probe = a if a.kind == "net" else a.model_copy(update={"kind": "net"})
    return not ref.check_net(probe, tuple(net_allowlist))


def extract_facts(a: Action, net_allowlist: tuple[str, ...] = ()) -> Facts:
    path = canon_path(a.path) if a.path else ""
    dst = canon_path(a.dst) if a.dst else ""
    return Facts(
        kind=KIND_CODES[a.kind],
        path_present=bool(a.path),
        dst_present=bool(a.dst),
        is_protected_test_path=bool(a.path) and ref.is_protected_test_path(path, a.protected_extra),
        dst_is_protected_test_path=bool(a.dst) and ref.is_protected_test_path(dst, a.protected_extra),
        added_skip_markers=sum(1 for line in a.added_lines if ref.line_has_skip(line)),
        touches_grader_config=bool(a.path) and ref.is_grader_path(path, a.section),
        dst_touches_grader_config=bool(a.dst) and ref.is_grader_path(dst, a.section),
        write_in_scope=bool(a.path) and ref.in_scope(path),
        dst_in_scope=bool(a.dst) and ref.in_scope(dst),
        host_allowlisted=_host_allowlisted(a, net_allowlist),
        attributed=bool(a.attributed),
    )


def conjecture(facts: Facts, predicate: str = "allowed") -> str:
    """The exact Lean source submitted to the lean-worker's POST /verify."""
    return (
        f"import {LEAN_MODULE}\n"
        f"example : {LEAN_NAMESPACE}.{predicate} {facts.lean_literal()} = true := by decide\n"
    )


__all__ = ["Facts", "KIND_CODES", "POLICY_PREDICATES", "LEAN_MODULE", "extract_facts", "conjecture"]
