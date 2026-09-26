"""Verifier factory. make_verifier("auto") prefers biject-api when healthy and hash-matching."""

from __future__ import annotations

import logging

from proofread.contracts import AddedPolicy, Verifier

from .reference_verifier import ReferenceVerifier

log = logging.getLogger(__name__)

__all__ = ["make_verifier", "ReferenceVerifier"]


def make_verifier(kind: str = "auto", added_policies: list[AddedPolicy] | None = None,
                  net_allowlist: tuple[str, ...] = ()) -> Verifier:
    if kind not in ("auto", "biject", "reference"):
        raise ValueError(f"unknown verifier kind {kind!r}")
    if kind == "reference":
        return ReferenceVerifier(added_policies, net_allowlist)
    try:
        from .biject_client import BijectVerifier, biject_available
    except ImportError:
        BijectVerifier = None  # type: ignore[assignment]
        biject_available = None  # type: ignore[assignment]
    if kind == "biject":
        if BijectVerifier is None:
            raise RuntimeError("biject client not available")
        return BijectVerifier(added_policies=added_policies, net_allowlist=net_allowlist)
    # auto
    if BijectVerifier is not None and biject_available is not None:
        try:
            if biject_available():
                return BijectVerifier(added_policies=added_policies, net_allowlist=net_allowlist)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("biject availability check failed: %s", e)
    return ReferenceVerifier(added_policies, net_allowlist)
