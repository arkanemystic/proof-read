"""ReferenceVerifier: wraps proofread.policies.reference (pure Python). provisional=True.

Used as the default when biject-api is not available or its policy hash does not match. Any
exception while evaluating an action yields a fail-closed verdict for that action.
"""

from __future__ import annotations

import time
from typing import Any

from proofread.contracts import FAILCLOSED_POLICY, Action, AddedPolicy, Verdict


def _reference():
    from proofread.policies import reference  # W1-owned module, imported lazily

    return reference


class ReferenceVerifier:
    source = "reference"

    def __init__(self, added_policies: list[AddedPolicy] | None = None, net_allowlist: tuple[str, ...] = ()) -> None:
        self.added_policies = list(added_policies or [])
        self.net_allowlist = tuple(net_allowlist)
        self.provisional = True
        ref = _reference()
        self.policy_hash: str = ref.BASE_POLICY_HASH

    def _eval(self, action: Action) -> list[str]:
        ref = _reference()
        failed: list[str] = list(ref.evaluate(action, net_allowlist=self.net_allowlist))
        if self.added_policies:
            failed += [p for p in ref.evaluate_added(action, self.added_policies) if p not in failed]
        return failed

    async def check_batch(self, actions: list[Action]) -> list[Verdict]:
        out: list[Verdict] = []
        for a in actions:
            t0 = time.perf_counter()
            try:
                failed = self._eval(a)
                v: dict[str, Any] = dict(ok=not failed, failed_policies=failed, source="reference")
            except Exception as e:  # fail closed
                v = dict(ok=False, failed_policies=[FAILCLOSED_POLICY], source="failclosed",
                         detail=f"reference error: {type(e).__name__}: {e}"[:500])
            out.append(Verdict(policy_hash=self.policy_hash, provisional=True,
                               latency_ms=(time.perf_counter() - t0) * 1000.0, **v))
        return out
