"""EpisodeRunner implementation (contracts.EpisodeRunner).

Flow: acquire sandbox slot -> start sandbox -> agent loop (observe|enforce) -> pre-grade final diff
verified -> workspace grade -> export files, stop sandbox, release slot -> pristine grade (own slot)
-> mechanism -> trace. On BudgetExceeded the episode ends (still graded) and EpisodeResult.error
starts with "BudgetExceeded" so the caller can stop that workload.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from proofread.contracts import (
    BudgetExceeded, EpisodeResult, Genome, GradeResult, Grader, MAX_CONCURRENT_SANDBOXES, Mode, Sandbox, Task,
    Verifier,
)

from .loop import AgentLoop, EpisodeCostAbort, Tracer
from .mechanism import classify

ARM_BUDGET_KEYS = {"A": "arm_A", "B": "arm_B", "C": "arm_C", "C_noret": "arm_C_noret", "baseline": "baselines",
                   "baselines": "baselines", "selection": "selection", "smoke": "smoke"}


def _effort(role: str, model: str) -> str:
    try:
        from proofread.models import config
        return config.reasoning_effort(role, config.openrouter_slug(model))
    except Exception:
        return ""


def role_for(budget_key: str, arm: str = "") -> str:
    """Client role from budget_key (arm as fallback): selection, baselines -> baseline, else agent."""
    k = budget_key or ARM_BUDGET_KEYS.get(arm, "")
    if k == "selection":
        return "selection"
    if k == "baselines":
        return "baseline"
    return "agent"


def default_slot_factory() -> Callable[[], Any]:
    try:
        from proofread.sandbox.runner import sandbox_slot  # W1

        return sandbox_slot
    except Exception:
        sem = asyncio.Semaphore(MAX_CONCURRENT_SANDBOXES)

        @asynccontextmanager
        async def _slot():
            async with sem:
                yield

        return _slot


async def _grade(grader: Any, task: Task, sandbox: Sandbox, final_output: str) -> GradeResult:
    try:
        params = inspect.signature(grader.grade).parameters
        if "final_output" in params:
            return await grader.grade(task, sandbox, final_output=final_output)
        return await grader.grade(task, sandbox)
    except Exception as e:  # fail closed
        return GradeResult(grader=getattr(grader, "name", "stub") if getattr(grader, "name", "") in
                           ("workspace", "pristine", "stub") else "stub", passed=False,
                           error=f"grader error: {type(e).__name__}: {e}"[:500])


class EpisodeRunnerImpl:
    def __init__(self, *, verifier_factory: Callable[[], Verifier], sandbox_factory: Callable[[str], Sandbox],
                 client_factory: Callable[[str, str], Any], workspace_grader: Grader,
                 pristine_grader: Any | None = None, trace_dir: str | Path = "traces",
                 slot: Callable[[], Any] | None = None, max_tokens: int = 16000) -> None:
        self.verifier_factory = verifier_factory
        self.sandbox_factory = sandbox_factory
        self.client_factory = client_factory
        self.workspace_grader = workspace_grader
        self.pristine_grader = pristine_grader
        self.trace_dir = Path(trace_dir).resolve()
        self.slot = slot or default_slot_factory()
        self.max_tokens = max_tokens

    async def __call__(self, task: Task, genome: Genome, *, mode: Mode, model: str, seed: int = 0, arm: str = "",
                       budget_key: str = "", pristine: bool = False, generation: int = -1,
                       candidate_id: str = "") -> EpisodeResult:
        if mode not in ("observe", "enforce"):
            raise ValueError(f"bad mode {mode!r}")
        episode_id = f"{arm or 'ep'}-{uuid.uuid4().hex[:12]}"
        budget_key = budget_key or ARM_BUDGET_KEYS.get(arm, "")
        trace_path = self.trace_dir / f"{episode_id}.jsonl"
        tracer = Tracer(trace_path)
        res = EpisodeResult(episode_id=episode_id, task_id=task.id, variant=task.variant,
                            genome_hash=genome.content_hash(), model=model, mode=mode, seed=seed, arm=arm,
                            generation=generation, candidate_id=candidate_id, trace_path=str(trace_path),
                            started_at=time.time())
        tracer.write("meta", episode_id=episode_id, task_id=task.id, variant=task.variant, mode=mode, model=model,
                     seed=seed, arm=arm, budget_key=budget_key, generation=generation, candidate_id=candidate_id,
                     genome=genome.model_dump(mode="json"), pristine=pristine,
                     reasoning_effort=_effort(role_for(budget_key, arm), model),
                     temperature=os.environ.get("PROOFREAD_TEMPERATURE") or
                     ("provider default" if os.environ.get("PROOFREAD_NO_TEMPERATURE") else "0.0"))
        errors: list[str] = []
        loop: AgentLoop | None = None
        files: dict[str, str] | None = None
        final_text = ""
        passed_ws = False

        async with self.slot():
            sandbox = self.sandbox_factory(episode_id)
            verifier = None
            try:
                await sandbox.start(task)
                verifier = self.verifier_factory()
                client = self.client_factory(role_for(budget_key, arm), model)
                loop = AgentLoop(task=task, genome=genome, mode=mode, sandbox=sandbox, verifier=verifier,
                                 client=client, episode_id=episode_id, budget_key=budget_key, tracer=tracer,
                                 max_tokens=self.max_tokens)
                try:
                    await loop.run()
                except BudgetExceeded as e:
                    errors.insert(0, f"BudgetExceeded: {e}")
                except EpisodeCostAbort as e:
                    errors.insert(0, f"aborted_cost: {e}")
                except Exception as e:
                    errors.append(f"agent error: {type(e).__name__}: {e}"[:500])
                final_text = next((m.content for m in reversed(loop.history) if m.role == "assistant"), "")
                await loop.final_check()
                gr = await _grade(self.workspace_grader, task, sandbox, final_text)
                tracer.write("grade", **gr.model_dump())
                passed_ws = gr.passed and not gr.error
                if mode == "enforce" and loop.state.final_violations:
                    passed_ws = False  # unattributed/violating residue cannot be rolled back: fail closed
                if pristine:
                    if self.pristine_grader is None:
                        errors.append("pristine requested but no pristine grader configured")
                    elif hasattr(self.pristine_grader, "grade_files"):
                        files = await sandbox.export_files()
                    else:
                        pr = await _grade(self.pristine_grader, task, sandbox, final_text)
                        tracer.write("grade", **pr.model_dump())
                        res.passed_pristine = pr.passed and not pr.error
            except Exception as e:
                errors.append(f"episode error: {type(e).__name__}: {e}"[:500])
            finally:
                if verifier is not None and hasattr(verifier, "aclose"):
                    try:
                        await verifier.aclose()  # e.g. LeanVerifier worker process
                    except Exception:
                        pass
                try:
                    await sandbox.stop()
                except Exception as e:
                    errors.append(f"sandbox stop error: {type(e).__name__}: {e}"[:300])

        # Pristine grading takes its own slot (after ours is released) to avoid deadlock at the cap.
        if pristine and files is not None:
            try:
                from proofread.graders._common import flagged
            except Exception:
                def flagged(s: str) -> bool:
                    return "[flag_for_human_intervention]" in (s or "").lower()
            if flagged(final_text):
                pr = GradeResult(grader="pristine", passed=False, error="flag_for_human_intervention")
            else:
                try:
                    pr = await self.pristine_grader.grade_files(task, files, episode_id)
                except Exception as e:
                    pr = GradeResult(grader="pristine", passed=False, error=f"grader error: {type(e).__name__}: {e}")
            tracer.write("grade", **pr.model_dump())
            res.passed_pristine = pr.passed and not pr.error
        elif pristine and res.passed_pristine is None:
            res.passed_pristine = False

        st = loop.state if loop else None
        res.passed_workspace = passed_ws
        if st:
            res.violations = list(st.attempted)
            res.n_actions, res.n_denied, res.turns = st.n_actions, st.n_denied, st.turns
            res.cost_usd, res.verifier_latency_ms, res.provisional = st.cost_usd, st.latencies_ms, st.provisional
            res.mechanism = classify(task, passed_ws, st.effective)
        res.error = "; ".join(errors)[:1000]
        res.ended_at = time.time()
        tracer.write("result", **res.model_dump(mode="json"),
                     effective_violations=list(st.effective) if st else [])
        return res


def make_episode_runner(*, verifier_factory: Callable[[], Verifier] | None = None,
                        sandbox_factory: Callable[[str], Sandbox] | None = None,
                        client_factory: Callable[[str, str], Any] | None = None,
                        workspace_grader: Grader | None = None, pristine_grader: Any | None = None,
                        trace_dir: str | Path = "traces", slot: Callable[[], Any] | None = None,
                        max_tokens: int = 16000) -> EpisodeRunnerImpl:
    """Build an EpisodeRunner. Any omitted component resolves to the real implementation lazily."""
    if sandbox_factory is None:
        from .wiring import real_sandbox_factory

        sandbox_factory = real_sandbox_factory()
    if verifier_factory is None:
        from .wiring import real_verifier_factory

        verifier_factory = real_verifier_factory()
    if client_factory is None:
        from proofread.models.client import make_client

        client_factory = make_client
    if workspace_grader is None:
        from proofread.graders import WorkspaceGrader

        workspace_grader = WorkspaceGrader()
    if pristine_grader is None:
        try:
            from proofread.graders import PristineGrader

            pristine_grader = PristineGrader(sandbox_factory)
        except Exception:
            pristine_grader = None
    return EpisodeRunnerImpl(verifier_factory=verifier_factory, sandbox_factory=sandbox_factory,
                             client_factory=client_factory, workspace_grader=workspace_grader,
                             pristine_grader=pristine_grader, trace_dir=trace_dir, slot=slot, max_tokens=max_tokens)
