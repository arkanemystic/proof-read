"""One demo episode in its own process (section 10d). Launched by scripts/demo.py, never by hand.

Each episode runs in a separate process so that the red-team episodes (GPT-5, RT sampling settings) and the
IMP episodes (qwen, reasoning off) can use different process-wide environment settings at the same time.

Output: JSON lines on stdout, one per event:
  {"type": "gate", ...}           every verifier decision on an action the inner agent took (live)
  {"type": "episode_done", ...}   the final EpisodeResult (also written to Atlas)
Logs and tracebacks go to stderr (the parent redirects stderr to logs/demo_<ts>.log).

The episode document is written to the Atlas `episodes` collection twice: status "running" at start, then
its final status, so the change stream in the parent shows the transition.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_OUT_LOCK = None


def emit(ev: dict[str, Any]) -> None:
    ev.setdefault("t", time.time())
    sys.stdout.write(json.dumps(ev, default=str) + "\n")
    sys.stdout.flush()


def install_gate_hook(meta: dict[str, Any]) -> None:
    """Wrap AgentLoop.verify (in this process only) to emit one gate event per verified action."""
    from proofread.agent.loop import AgentLoop

    orig = AgentLoop.verify

    async def verify(self, actions, stage):  # type: ignore[no-untyped-def]
        captured: dict[str, Any] = {}
        tw = self.tracer.write

        def w(type_: str, **payload: Any) -> None:
            if type_ == "verify":
                captured.update(payload)
            return tw(type_, **payload)

        self.tracer.write = w
        n_lat = len(self.state.latencies_ms)
        try:
            failed = await orig(self, actions, stage)
        finally:
            self.tracer.write = tw
        lat = self.state.latencies_ms[-1] if len(self.state.latencies_ms) > n_lat else None
        per = captured.get("failed") or [[] for _ in actions]
        n = max(1, len(actions))
        for a, pa in zip(actions, per):
            emit({"type": "gate", **meta, "stage": stage, "mode": self.mode, "kind": a.kind,
                  "path": a.path or (a.cmd or "")[:60], "dst": a.dst,
                  "policies": list(pa), "allowed": not pa,
                  "denied": bool(pa) and self.mode == "enforce" and stage in ("pre", "post", "feedback_test"),
                  "latency_ms": round(lat / n, 1) if lat is not None else None,
                  "batch": len(actions)})
        return failed

    AgentLoop.verify = verify  # type: ignore[assignment]


SOFT = {"hit": False, "turns": 0}


def install_soft_deadline(seconds: float) -> None:
    """Demo turn deadline: after `seconds` of agent time the loop finishes its current turn and stops, and the
    episode is graded on the workspace as it stands (same graders, same gates). Wraps AgentLoop.run in this
    process only; loop.py is unchanged. Reported on screen as "stopped at demo turn deadline"."""
    from proofread.agent.loop import AgentLoop

    orig = AgentLoop.run

    async def run(self):  # type: ignore[no-untyped-def]
        async def fire() -> None:
            await asyncio.sleep(seconds)
            SOFT["hit"], SOFT["turns"] = True, self.state.turns
            self.state.turns = 10**6  # the while condition fails before the next model call

        task = asyncio.create_task(fire())
        try:
            state = await orig(self)
        finally:
            task.cancel()
        if SOFT["hit"]:
            state.turns = SOFT["turns"]
            state.finished = False
        return state

    AgentLoop.run = run  # type: ignore[assignment]


async def run(a: argparse.Namespace) -> int:
    from proofread.agent.episode import make_episode_runner
    from proofread.contracts import EpisodeResult, Genome
    from proofread.evolve.orchestrator import real_task_loader
    from proofread.store.factory import backend, make_store
    from proofread.verify.lap_client import LapVerifier

    meta = {"cand": a.cand, "task": a.task, "arm": a.arm, "role": a.role}
    install_gate_hook(meta)
    if a.soft_deadline and a.soft_deadline > 0:
        install_soft_deadline(a.soft_deadline)
    if backend() != "mongo":
        emit({"type": "episode_done", **meta, "error": "store backend is not mongo", "passed": False,
              "violations": [], "cost_usd": 0.0})
        return 2
    store = make_store()
    genome = Genome.model_validate_json(Path(a.genome).read_text())
    task = next(t for t in real_task_loader(a.split) if t.id == a.task)
    doc_id = f"{a.run_id}:g0:{a.cand}:{a.task}:s0"
    base = {"_id": doc_id, "run_id": a.run_id, "arm": a.arm, "candidate_id": a.cand, "task_id": a.task,
            "model": a.model, "mode": a.mode, "generation": 0, "seed": 0, "budget_key": a.budget_key,
            "demo": True, "demo_role": a.role}
    store.put("episodes", doc_id, {**base, "status": "running", "started_at": time.time()})
    if a.verifier == "lap":
        vf = lambda: LapVerifier()  # noqa: E731
    else:
        from proofread.verify import make_verifier

        vf = lambda: make_verifier("lean")  # noqa: E731
    runner = make_episode_runner(verifier_factory=vf, trace_dir=a.trace_dir)
    t0 = time.time()
    err = ""
    res: EpisodeResult | None = None
    try:
        res = await asyncio.wait_for(
            runner(task, genome, mode=a.mode, model=a.model, seed=0, arm=a.arm, budget_key=a.budget_key,
                   generation=0, candidate_id=a.cand),
            timeout=a.timeout)
    except asyncio.TimeoutError:
        err = f"demo timeout after {a.timeout:.0f}s"
    except Exception as e:  # logged, counted as a failed episode
        traceback.print_exc(file=sys.stderr)
        err = f"episode error: {type(e).__name__}"
    if res is None:
        res = EpisodeResult(episode_id=f"err:{doc_id}", task_id=task.id, variant=task.variant,
                            genome_hash=genome.content_hash(), model=a.model, mode=a.mode, seed=0, arm=a.arm,
                            generation=0, candidate_id=a.cand, error=err)
    d = res.model_dump(mode="json")
    if res.violations:
        status = "denied" if a.mode == "enforce" else "violating"
    elif res.error:
        status = "timeout" if "timeout" in res.error else "error"
    else:
        status = "passed" if res.passed_workspace else "failed"
    store.put("episodes", doc_id, {**d, **base, "status": status, "wall_s": round(time.time() - t0, 1),
                                   "demo_soft_deadline": SOFT["hit"]})
    emit({"type": "episode_done", **meta, "status": status, "result": d, "wall_s": round(time.time() - t0, 1),
          "soft_deadline": SOFT["hit"]})
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    for k in ("run-id", "cand", "arm", "role", "task", "genome", "model", "mode", "budget-key", "trace-dir"):
        ap.add_argument(f"--{k}", required=True)
    ap.add_argument("--split", default="imp_training")
    ap.add_argument("--timeout", type=float, default=150.0)
    ap.add_argument("--soft-deadline", type=float, default=0.0)
    ap.add_argument("--verifier", default="lap", choices=["lap", "lean"])
    a = ap.parse_args(argv)
    try:
        return asyncio.run(run(a))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        emit({"type": "episode_done", "cand": a.cand, "task": a.task, "arm": a.arm, "role": a.role,
              "status": "error", "result": None, "error": "worker crashed"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
