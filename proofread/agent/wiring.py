"""Lazy resolution of the real components (W1 sandbox, W3 verifier). Imported only when needed."""

from __future__ import annotations

import importlib
from typing import Any, Callable

_SANDBOX_CANDIDATES = (
    ("proofread.sandbox", "make_sandbox"),
    ("proofread.sandbox.docker_sandbox", "DockerSandbox"),
    ("proofread.sandbox.docker", "DockerSandbox"),
    ("proofread.sandbox.runner", "DockerSandbox"),
    ("proofread.sandbox", "DockerSandbox"),
)


def _find(cands: tuple[tuple[str, str], ...]) -> Any:
    errs = []
    for mod, name in cands:
        try:
            m = importlib.import_module(mod)
        except Exception as e:
            errs.append(f"{mod}: {type(e).__name__}")
            continue
        obj = getattr(m, name, None)
        if obj is not None:
            return obj
        errs.append(f"{mod}.{name}: missing")
    raise RuntimeError("no real implementation found: " + "; ".join(errs))


def real_sandbox_factory() -> Callable[[str], Any]:
    """Callable episode_id -> Sandbox (W1). Resolved lazily at first use."""
    cache: dict[str, Any] = {}

    def factory(episode_id: str):
        if "impl" not in cache:
            cache["impl"] = _find(_SANDBOX_CANDIDATES)
        impl = cache["impl"]
        try:
            return impl(episode_id=episode_id)
        except TypeError:
            return impl(episode_id)

    return factory


def real_verifier_factory(kind: str = "auto") -> Callable[[], Any]:
    """Callable () -> Verifier (W3 make_verifier). Added policies are evaluated by the loop itself."""

    def factory():
        from proofread.verify import make_verifier

        return make_verifier(kind)

    return factory
