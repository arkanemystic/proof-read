"""Throughput benchmark: python -m proofread.verify.bench [n]. Prints actions/sec and p50/p95."""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
import warnings

from proofread.contracts import Action


def _actions(n: int) -> list[Action]:
    kinds = ["write", "delete", "rename", "exec", "net", "write", "write"]
    out = []
    for i in range(n):
        out.append(Action(episode_id="bench", step=i, kind=kinds[i % len(kinds)],
                          path=f"/workspace/pkg{i % 13}/mod{i}.py" if i % 5 else f"/workspace/tests/test_{i}.py",
                          dst="" if i % 7 else f"/workspace/other{i}.py",
                          added_lines=[f"def f{i}(x):", "    return x + 1", "# pytest.mark.skip" if i % 11 == 0 else "pass"],
                          host="example.com" if i % 7 == 4 else "", attributed=bool(i % 9)))
    return out


async def _run(v, acts: list[Action], batch: int) -> dict:
    lat = []
    t0 = time.perf_counter()
    for k in range(0, len(acts), batch):
        b = acts[k:k + batch]
        s = time.perf_counter()
        await v.check_batch(b)
        lat.append((time.perf_counter() - s) * 1000.0)
    dt = time.perf_counter() - t0
    qs = statistics.quantiles(lat, n=20) if len(lat) >= 2 else [lat[0]] * 19
    return {"actions_per_s": round(len(acts) / dt, 1), "batch": batch,
            "p50_batch_ms": round(statistics.median(lat), 3), "p95_batch_ms": round(qs[18], 3)}


async def main(n: int) -> None:
    from proofread.verify.lean_verifier import LeanVerifier
    from proofread.verify.reference_verifier import ReferenceVerifier

    acts = _actions(n)
    for batch in (1, 20):
        print("reference", await _run(ReferenceVerifier(), acts, batch))
        lv = LeanVerifier()
        print("lean     ", await _run(lv, acts, batch))
        await lv.aclose()


if __name__ == "__main__":
    warnings.simplefilter("ignore")
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 4000))
