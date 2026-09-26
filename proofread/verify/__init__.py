"""Verifier factory.

make_verifier(kind):
  "auto"      -> BijectVerifier if the local biject-api is healthy and serves our pinned policy hash,
                 else ReferenceVerifier (provisional).
  "biject"    -> BijectVerifier (fails closed on every call if biject is down or the hash mismatches).
  "reference" -> ReferenceVerifier (pure Python reference.py, provisional).
  "lean"      -> LeanVerifier (compiled Lean policies via policycheck, provisional: not biject-api).
"""

from __future__ import annotations

import os

import logging

from proofread.contracts import AddedPolicy, Verifier

from .reference_verifier import ReferenceVerifier

log = logging.getLogger(__name__)

__all__ = ["make_verifier", "ReferenceVerifier", "VERIFIER_KINDS"]

VERIFIER_KINDS = ("auto", "biject", "reference", "lean")


def make_verifier(kind: str = "auto", added_policies: list[AddedPolicy] | None = None,
                  net_allowlist: tuple[str, ...] = ()) -> Verifier:
    if kind == "auto" and os.environ.get("PROOFREAD_VERIFIER"):
        kind = os.environ["PROOFREAD_VERIFIER"]  # integrator override (D-013)
    if kind not in VERIFIER_KINDS:
        raise ValueError(f"unknown verifier kind {kind!r}")
    if kind == "reference":
        return ReferenceVerifier(added_policies, net_allowlist)
    if kind == "lean":
        from .lean_verifier import LeanVerifier

        return LeanVerifier(added_policies=added_policies, net_allowlist=net_allowlist)
    from .biject_client import BijectVerifier, biject_available

    if kind == "biject":
        return BijectVerifier(added_policies=added_policies, net_allowlist=net_allowlist)
    try:
        if biject_available():
            return BijectVerifier(added_policies=added_policies, net_allowlist=net_allowlist)
    except Exception as e:  # pragma: no cover - biject_available never raises
        log.warning("biject availability check failed: %s", e)
    log.info("biject-api unavailable or policy hash mismatch; using provisional ReferenceVerifier")
    return ReferenceVerifier(added_policies, net_allowlist)
