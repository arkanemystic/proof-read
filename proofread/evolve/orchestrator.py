"""Evolution loop for one arm.

Edit lifecycle: proposed -> invalid | evaluating -> rejected_formal | rejected_empirical | promoted.

Resumability: every step is an idempotent stage keyed f"{run_id}:{edit_id}:{stage}" (or
f"{run_id}:g{gen}:{stage}" for generation steps) in the event log; each episode result is stored in
the "episodes" collection under a deterministic id, so a restarted run reuses finished proposals,
episodes and decisions and only does the remaining work. Store writes that follow an event are
idempotent puts and are simply redone on resume.

CLI:
  python -m proofread.evolve.orchestrator --arm A|C|B|C-noret --db data/proofread.sqlite \
      --generations 3 --candidates 3 --model SLUG [--tasks N] [--deadline ISO] [--seeds 0,1]
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import time
from datetime import datetime
from typing import Any, Callable

from proofread.contracts import (
    BudgetExceeded, EpisodeResult, EpisodeRunner, EventLog, Genome, ModelClient, Mode, Store, Task,
)
from proofread.evolve.config import ArmConfig, arm_preset
from proofread.evolve.gates import decide, screening_reject
from proofread.evolve.patching import PatchError, validate_edit
from proofread.evolve.proposer import Proposer
from proofread.store.vector import NumpyVectorIndex, embed


class DeadlineReached(RuntimeError):
    pass


TaskLoader = Callable[[str], list[Task]]


class ArmRun:
    def __init__(self, cfg: ArmConfig, runner: EpisodeRunner, proposer_client: ModelClient, store: Store,
                 eventlog: EventLog, tasks: list[Task], deadline_ts: float | None = None,
                 initial_genome: Genome | None = None) -> None:
        self.cfg, self.runner, self.store, self.log = cfg, runner, store, eventlog
        self.tasks = tasks
        self.screen_tasks = tasks[: cfg.screening_tasks]
        self.deadline_ts = deadline_ts
        self.rid = cfg.rid
        self.sem = asyncio.Semaphore(max(1, cfg.concurrency))
        self.index = NumpyVectorIndex(store, namespace=f"rejected:{self.rid}")
        self.proposer = Proposer(proposer_client, self.index, budget_key=cfg.budget_key, retrieval=cfg.retrieval,
                                 k=cfg.retrieval_k, max_tokens=cfg.proposer_max_tokens,
                                 temperature=cfg.proposer_temperature, trace_chars=cfg.trace_chars,
                                 max_traces=cfg.max_failure_traces)
        g = initial_genome or Genome()
        if cfg.hide_tests:
            g = g.model_copy(update={"context": g.context.model_copy(update={"include_test_file_in_prompt": False})})
        self.initial_genome = g
        self.events: dict[str, dict[str, Any]] = {}
        self._load_events()

    # ---------------------------------------------------------------- events
    def _load_events(self) -> None:
        get_by_key = getattr(self.log, "get_by_key", None)
        self._get_by_key = get_by_key
        if get_by_key is not None:
            return  # lazy lookups
        after = 0
        prefix = self.rid + ":"
        while True:
            batch = self.log.read(after_seq=after, limit=1000)
            if not batch:
                break
            for e in batch:
                if e.key.startswith(prefix):
                    self.events[e.key] = e.payload
            after = batch[-1].seq

    def _ev(self, *parts: str) -> str:
        return ":".join((self.rid, *parts))

    def done(self, key: str) -> dict[str, Any] | None:
        if key in self.events:
            return self.events[key]
        if self._get_by_key is not None:
            e = self._get_by_key(key)
            if e is not None:
                self.events[key] = e.payload
                return e.payload
        return None

    def emit(self, type: str, key: str, payload: dict[str, Any]) -> None:
        payload = {"run_id": self.rid, "arm": self.cfg.name, **payload}
        self.log.append(type, key, payload)
        self.events[key] = payload

    # ---------------------------------------------------------------- episodes
    def _deadline_passed(self) -> bool:
        return self.deadline_ts is not None and time.time() >= self.deadline_ts

    def _episode_doc_id(self, gen: int, cand: str, task_id: str, seed: int) -> str:
        return f"{self.rid}:g{gen}:{cand}:{task_id}:s{seed}"

    async def _episode(self, task: Task, genome: Genome, gen: int, cand: str, seed: int,
                       mode: Mode) -> EpisodeResult | None:
        doc_id = self._episode_doc_id(gen, cand, task.id, seed)
        d = self.store.get("episodes", doc_id)
        if d is not None:
            return EpisodeResult.model_validate(d)
        if self._deadline_passed():
            return None
        async with self.sem:
            if self._deadline_passed():
                return None
            try:
                r = await self.runner(task, genome, mode=mode, model=self.cfg.agent_model, seed=seed, arm=self.cfg.name,
                                      budget_key=self.cfg.budget_key, pristine=self.cfg.pristine, generation=gen,
                                      candidate_id=cand)
            except BudgetExceeded:
                raise
            except Exception as e:  # infrastructure error: counts as a failed episode, not persisted (retried on resume)
                return EpisodeResult(episode_id=f"err:{doc_id}", task_id=task.id, variant=task.variant,
                                     genome_hash=genome.content_hash(), model=self.cfg.agent_model, mode=mode, seed=seed,
                                     arm=self.cfg.name, generation=gen, candidate_id=cand, error=repr(e)[:500])
        r = r.model_copy(update={"arm": self.cfg.name, "generation": gen, "candidate_id": cand, "seed": seed})
        self.store.put("episodes", doc_id, {**r.model_dump(mode="json"), "_id": doc_id, "run_id": self.rid})
        return r

    async def _eval(self, tasks: list[Task], seeds: list[int], genome: Genome, gen: int, cand: str,
                    mode: Mode) -> list[EpisodeResult]:
        jobs = [self._episode(t, genome, gen, cand, s, mode) for t in tasks for s in seeds]
        res = await asyncio.gather(*jobs)
        if any(r is None for r in res):
            raise DeadlineReached(f"deadline reached during {cand} gen {gen}")
        return list(res)  # type: ignore[arg-type]

    # ---------------------------------------------------------------- champions
    def _put_version(self, genome: Genome, gen: int, edit_id: str | None, parent: str | None,
                     score: dict[str, Any] | None = None) -> str:
        vid = f"{self.rid}:v{genome.version}"
        self.store.put("harness_versions", vid, {
            "_id": vid, "run_id": self.rid, "arm": self.cfg.name, "version": genome.version,
            "genome": genome.model_dump(mode="json"), "genome_hash": genome.content_hash(), "generation": gen,
            "edit_id": edit_id, "parent": parent, "score": score or {}, "created_at": time.time()})
        return vid

    def _load_version(self, vid: str) -> Genome:
        d = self.store.get("harness_versions", vid)
        if d is None:
            raise RuntimeError(f"missing harness version {vid}")
        return Genome.model_validate(d["genome"])

    # ---------------------------------------------------------------- edits
    def _history(self) -> list[dict[str, Any]]:
        eds = self.store.find("edits", {"run_id": self.rid})
        return sorted(eds, key=lambda e: (e.get("generation", 0), e.get("candidate_index", 0)))

    def _reject(self, edit: dict[str, Any], status: str, reason: str) -> None:
        edit.update(status=status, reason=reason, decided_at=time.time())
        self.store.put("edits", edit["id"], edit)
        self.store.put("rejected_edits", edit["id"], edit)
        self.index.add(edit["id"], embed(edit.get("intent", "")), {
            "intent": edit.get("intent", ""), "reason": reason, "status": status, "patch": edit.get("patch", []),
            "delta_points": edit.get("delta_points"), "generation": edit.get("generation")})

    async def _propose(self, gen: int, k: int, champion: Genome, champ_res: list[EpisodeResult]) -> dict[str, Any]:
        edit_id = f"{self.rid}-g{gen}-c{k}"
        existing = self.store.get("edits", edit_id)
        if existing is not None and existing.get("proposed_at"):
            self.emit("edit.proposed", self._ev(edit_id, "proposed"), {"edit_id": edit_id})
            if existing.get("status") == "invalid":
                self._reject(existing, "invalid", existing.get("reason", ""))  # idempotent redo
                self.emit("edit.decided", self._ev(edit_id, "decided"), {"edit_id": edit_id, "status": "invalid",
                                                                            "reason": existing.get("reason", "")})
            return existing
        if self._deadline_passed():
            raise DeadlineReached("deadline reached before proposal")
        p = await self.proposer.propose(champion, champ_res, self._history())
        edit: dict[str, Any] = {
            "_id": edit_id, "id": edit_id, "run_id": self.rid, "arm": self.cfg.name, "generation": gen,
            "candidate_index": k, "parent_hash": champion.content_hash(), "parent_version": champion.version,
            "intent": p.intent, "patch": p.patch, "rationale": p.rationale,
            "retrieved": [{"id": r["id"], "similarity": r["similarity"]} for r in p.retrieved],
            "proposer_cost_usd": p.cost_usd, "status": "proposed", "reason": "", "proposed_at": time.time(),
        }
        try:
            if p.parse_error:
                raise PatchError(p.parse_error)
            cand = validate_edit(champion, p.patch, hide_tests=self.cfg.hide_tests)
            edit["genome"] = cand.model_dump(mode="json")
            edit["genome_hash"] = cand.content_hash()
            edit["status"] = "evaluating"
            self.store.put("edits", edit_id, edit)
            self.emit("edit.proposed", self._ev(edit_id, "proposed"), {"edit_id": edit_id, "status": "evaluating"})
        except PatchError as e:
            edit["raw"] = p.raw[:4000]
            self._reject(edit, "invalid", f"static: {e}"[:1000])  # single put: doc exists only when final
            self.emit("edit.proposed", self._ev(edit_id, "proposed"), {"edit_id": edit_id, "status": "invalid"})
            self.emit("edit.decided", self._ev(edit_id, "decided"), {"edit_id": edit_id, "status": "invalid",
                                                                        "reason": edit["reason"]})
        return edit

    async def _evaluate(self, edit: dict[str, Any], gen: int, champ_res: list[EpisodeResult]) -> dict[str, Any]:
        """Screening, full eval, formal and empirical gates. Returns the gates payload."""
        edit_id = edit["id"]
        prev = self.done(self._ev(edit_id, "decided"))
        if prev is not None:
            return prev
        cand = Genome.model_validate(edit["genome"])
        mode = self.cfg.mode_for_candidates
        seed0 = self.cfg.seeds[0]
        champ_by = {(r.task_id, r.seed): r for r in champ_res}

        # screening
        scr = self.done(self._ev(edit_id, "screened"))
        if scr is None:
            sres = await self._eval(self.screen_tasks, [seed0], cand, gen, edit_id, mode)
            cp = sum(r.passed_workspace for r in sres)
            hp = sum(champ_by[(t.id, seed0)].passed_workspace for t in self.screen_tasks if (t.id, seed0) in champ_by)
            reason = screening_reject(cp, hp)
            scr = {"edit_id": edit_id, "candidate_passes": cp, "champion_passes": hp, "reject_reason": reason}
            self.emit("edit.screened", self._ev(edit_id, "screened"), scr)
        if scr.get("reject_reason"):
            out = {"edit_id": edit_id, "status": "rejected_empirical", "reason": scr["reject_reason"],
                   "stats": {}, "violations": [], "screened_out": True}
            edit.update(screening=scr)
            self._reject(edit, "rejected_empirical", scr["reject_reason"])
            self.emit("edit.decided", self._ev(edit_id, "decided"), out)
            return out

        # full evaluation in observe mode, to completion (screening episodes are reused)
        full = await self._eval(self.tasks, self.cfg.seeds, cand, gen, edit_id, mode)
        self.emit("edit.evaluated", self._ev(edit_id, "evaluated"), {
            "edit_id": edit_id, "n": len(full), "passes": sum(r.passed_workspace for r in full),
            "violating": sum(bool(r.violations) for r in full), "errors": sum(bool(r.error) for r in full)})
        dec = decide(self.cfg, full, champ_res)
        out = {"edit_id": edit_id, **dec.as_dict()}
        edit.update(screening=scr, delta_points=dec.stats.delta_points, lb_points=dec.stats.lower_bound_points,
                    candidate_rate=dec.stats.candidate_rate, champion_rate=dec.stats.champion_rate,
                    n_pairs=dec.stats.n_pairs, violations=dec.violations,
                    n_violating_episodes=len(dec.violations), counterfactual_delta_points=dec.stats.delta_points,
                    gate_reason=dec.reason)
        if dec.status == "passed_gates":
            edit.update(status="evaluating", reason=dec.reason)
            self.store.put("edits", edit_id, edit)
        else:
            self._reject(edit, dec.status, dec.reason)
        self.emit("edit.decided", self._ev(edit_id, "decided"), out)
        return out

    # ---------------------------------------------------------------- generations
    async def _generation(self, gen: int, champion_vid: str) -> str:
        champion = self._load_version(champion_vid)
        champ_res = await self._eval(self.tasks, self.cfg.seeds, champion, gen, "champion", self.cfg.champion_mode)
        passes = sum(r.passed_workspace for r in champ_res)
        self.emit("gen.champion_evaluated", self._ev(f"g{gen}", "champion_evaluated"), {
            "generation": gen, "champion": champion_vid, "passes": passes, "n": len(champ_res),
            "violating": sum(bool(r.violations) for r in champ_res)})

        edits = []
        for k in range(self.cfg.candidates_per_generation):
            edits.append(await self._propose(gen, k, champion, champ_res))
        live = [e for e in edits if e.get("status") != "invalid"]
        decisions = await asyncio.gather(*[self._evaluate(e, gen, champ_res) for e in live])

        sel = self.done(self._ev(f"g{gen}", "selection"))
        if sel is None:
            passed = [(d["stats"]["delta_points"], -i, d["edit_id"]) for i, d in enumerate(decisions)
                      if d["status"] == "passed_gates"]
            winner = max(passed)[2] if passed else None
            sel = {"generation": gen, "promoted": winner, "passed": [p[2] for p in passed]}
            self.emit("gen.selection", self._ev(f"g{gen}", "selection"), sel)
        winner = sel["promoted"]
        new_vid = champion_vid
        for e, d in zip(live, decisions):
            if d["status"] != "passed_gates":
                continue
            if e["id"] == winner:
                g = Genome.model_validate(e["genome"]).model_copy(update={"version": champion.version + 1})
                new_vid = self._put_version(g, gen, e["id"], champion_vid,
                                            {"delta_points": d["stats"]["delta_points"],
                                             "lb_points": d["stats"]["lower_bound_points"],
                                             "candidate_rate": d["stats"]["candidate_rate"]})
                e.update(status="promoted", promoted_version=new_vid, decided_at=time.time())
                self.store.put("edits", e["id"], e)
                self.emit("edit.promoted", self._ev(e["id"], "promoted"), {"edit_id": e["id"], "version": new_vid})
            else:
                self._reject(e, "rejected_empirical", "passed gates but a sibling candidate had a larger delta")
        self.emit("gen.done", self._ev(f"g{gen}", "done"), {"generation": gen, "champion": new_vid,
                                                             "promoted": winner})
        return new_vid

    async def run(self) -> dict[str, Any]:
        init = self.done(self._ev("init"))
        if init is None:
            vid = self._put_version(self.initial_genome, -1, None, None)
            self.emit("arm.started", self._ev("init"), {"champion": vid, "config": self.cfg.model_dump(),
                                                        "tasks": [t.id for t in self.tasks]})
            init = self.events[self._ev("init")]
        champion_vid = init["champion"]
        stopped = ""
        gens_done = 0
        try:
            for gen in range(self.cfg.generations):
                d = self.done(self._ev(f"g{gen}", "done"))
                if d is not None:
                    champion_vid = d["champion"]
                    gens_done += 1
                    continue
                if self._deadline_passed():
                    raise DeadlineReached("deadline reached before generation start")
                champion_vid = await self._generation(gen, champion_vid)
                gens_done += 1
        except DeadlineReached as e:
            stopped = f"deadline: {e}"
        except BudgetExceeded as e:
            stopped = f"budget: {e}"
        if stopped:
            self.log.append("arm.stopped", self._ev("stopped", f"{time.time():.6f}"),
                            {"run_id": self.rid, "reason": stopped})
        else:
            self.emit("arm.finished", self._ev("finished"), {"champion": champion_vid})
        edits = self._history()
        return {"run_id": self.rid, "arm": self.cfg.name, "champion": champion_vid,
                "champion_hash": self._load_version(champion_vid).content_hash(),
                "generations_completed": gens_done, "stopped": stopped,
                "edits": {e["id"]: e.get("status") for e in edits}}


async def run_arm(cfg: ArmConfig, runner: EpisodeRunner, proposer_client: ModelClient, store: Store,
                  eventlog: EventLog, task_loader: TaskLoader | list[Task], deadline_ts: float | None = None,
                  initial_genome: Genome | None = None) -> dict[str, Any]:
    tasks = list(task_loader) if isinstance(task_loader, list) else list(task_loader(cfg.split))
    if cfg.max_tasks:
        tasks = tasks[: cfg.max_tasks]
    if not tasks:
        raise ValueError("no tasks")
    return await ArmRun(cfg, runner, proposer_client, store, eventlog, tasks, deadline_ts, initial_genome).run()


# -------------------------------------------------------------------- CLI wiring (lazy, real components)


def _call_with_supported(fn: Callable, **kw: Any) -> Any:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(**kw)
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return fn(**kw)
    return fn(**{k: v for k, v in kw.items() if k in params})


def to_contract_task(t: Any) -> Task:
    if isinstance(t, Task):
        return t
    d = t.model_dump() if hasattr(t, "model_dump") else dict(t)
    if "files" in d and "variant" in d:
        return Task.model_validate(d)
    meta = d.get("metadata") or d.get("meta") or {}
    variant = meta.get("variant") or d["id"].split("/")[1]
    return Task(id=d["id"], variant=variant, files=d.get("workspace_files") or d.get("files"),
                test_paths=d["test_paths"], spec=d["spec"], test_command=d.get("test_command") or ["python", "test.py"],
                meta=meta)


def real_task_loader(split: str) -> list[Task]:
    from proofread.benchmarks import impossiblebench as ib  # W2

    return [to_contract_task(ib.load_task(i)) for i in ib.list_tasks(split)]


def build_real_components(args: argparse.Namespace, store: Store) -> tuple[Any, Any]:
    from proofread.agent.episode import make_episode_runner  # W4
    from proofread.models.client import make_client  # W4

    proposer_model = args.proposer_model
    if not proposer_model:
        try:
            from proofread.models import config as mcfg  # type: ignore

            proposer_model = getattr(mcfg, "PROPOSER_MODEL", "") or "claude-opus-5-5"
        except Exception:
            proposer_model = "claude-opus-5-5"
    proposer = _call_with_supported(make_client, role="proposer", model=proposer_model)
    # W4 resolves sandbox (W1), verifier (W3), graders and agent client lazily when omitted.
    extra: dict[str, Any] = {"trace_dir": args.trace_dir}
    runner = _call_with_supported(make_episode_runner, **extra)
    return runner, proposer


def _parse_deadline(s: str | None) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


async def _amain(args: argparse.Namespace) -> dict[str, Any]:
    from proofread.store.sqlite_store import SqliteEventLog, SqliteStore

    store, log = SqliteStore(args.db), SqliteEventLog(args.db)
    cfg = arm_preset(args.arm, generations=args.generations, candidates_per_generation=args.candidates,
                     agent_model=args.model, max_tasks=args.tasks, concurrency=args.concurrency,
                     seeds=[int(x) for x in args.seeds.split(",")], split=args.split,
                     run_id=args.run_id, proposer_model=args.proposer_model)
    runner, proposer = build_real_components(args, store)
    return await run_arm(cfg, runner, proposer, store, log, real_task_loader, _parse_deadline(args.deadline))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m proofread.evolve.orchestrator")
    ap.add_argument("--arm", required=True, choices=["A", "C", "B", "C-noret"])
    ap.add_argument("--db", default="data/proofread.sqlite")
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--candidates", type=int, default=3)
    ap.add_argument("--model", required=True, help="inner agent model slug")
    ap.add_argument("--proposer-model", default="")
    ap.add_argument("--tasks", type=int, default=0)
    ap.add_argument("--seeds", default="0")
    ap.add_argument("--split", default="training")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--trace-dir", default="traces")
    ap.add_argument("--deadline", default=None, help="ISO UTC time; no new episodes start after it")
    args = ap.parse_args(argv)
    print(json.dumps(asyncio.run(_amain(args)), indent=1))


if __name__ == "__main__":
    main()
