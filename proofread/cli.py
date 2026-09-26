"""proofread CLI.

  proofread run-episode --task ID --genome PATH --mode observe|enforce --model SLUG [--pristine]
                        [--arm smoke] [--budget-key smoke] [--seed 0] [--trace-dir traces] [--stub]

--stub runs the full loop against in-memory fakes (FakeSandbox, StubVerifier, StubGrader and a
scripted model) with no Docker, network or spend; useful as a wiring check.

  proofread atlas status                     collections, counts, vector index, latest events
  proofread atlas watch [--actions] [--consumer NAME] [--types t1,t2]
                                             live tail of events (or verified actions) via change streams
  proofread atlas migrate --db data/proofread.sqlite
                                             copy a SQLite store into MongoDB (idempotent)

atlas commands read MONGODB_URI / MONGODB_DB_NAME / MONGODB_*_COLLECTION from the env or .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _stub_runner(trace_dir: str):
    from proofread.contracts import FakeSandbox, ModelResponse, ScriptedModelClient, StubGrader, StubVerifier, ToolCall

    from proofread.agent.episode import make_episode_runner

    def client_factory(role, model):
        return ScriptedModelClient(model=model, role=role, responses=[
            ModelResponse(text="Plan: read func.py.", tool_calls=[ToolCall(id="c1", name="read_file",
                                                                          arguments={"path": "func.py"})]),
            ModelResponse(text="DONE"),
        ])

    return make_episode_runner(verifier_factory=StubVerifier, sandbox_factory=lambda eid: FakeSandbox(eid),
                               client_factory=client_factory, workspace_grader=StubGrader(True),
                               pristine_grader=StubGrader(True), trace_dir=trace_dir)


def _load_task(task_id: str, stub: bool):
    from proofread.contracts import Task

    if stub and task_id.startswith("stub"):
        return Task(id=task_id, variant="original", files={"func.py": "def f():\n    pass\n", "test.py": "assert True\n"},
                    test_paths=["test.py"], spec="Implement f.", test_command=["python", "test.py"])
    from proofread.benchmarks.impossiblebench import load_task

    return load_task(task_id)


async def _run_episode(args: argparse.Namespace) -> int:
    from proofread.genome.schema import load_genome

    genome = load_genome(args.genome)
    task = _load_task(args.task, args.stub)
    if args.stub:
        runner = _stub_runner(args.trace_dir)
    else:
        from proofread.agent.episode import make_episode_runner
        from proofread.store.backend import action_sink

        runner = make_episode_runner(trace_dir=args.trace_dir, action_sink=action_sink())
    res = await runner(task, genome, mode=args.mode, model=args.model, seed=args.seed, arm=args.arm,
                       budget_key=args.budget_key, pristine=args.pristine)
    print(json.dumps(res.model_dump(mode="json"), indent=2))
    return 0 if not res.error else 2


def _mask(uri: str) -> str:
    import re

    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:****@", uri)


def _atlas_status(args: argparse.Namespace) -> int:
    from proofread.store.mongo_store import AtlasVectorIndex, MongoEventLog, MongoStore

    store, log = MongoStore(), MongoEventLog()
    print(f"cluster   {_mask(store.uri).split('@')[-1].split('/')[0]}")
    print(f"database  {store.db_name}   (collections: {', '.join(f'{k}->{v}' for k, v in store.names.items())})")
    for name in sorted(store.db.list_collection_names()):
        print(f"  {name:<18} {store.db[name].estimated_document_count():>8}")
    idx = AtlasVectorIndex(store, create_index=False)
    print(f"vector search index '{idx.index_name}' on vectors: {idx.search_index_status() or 'missing'}")
    actions = store.coll("actions")
    n_viol = actions.count_documents({"ok": False})
    print(f"actions   {actions.estimated_document_count()} verified, {n_viol} violating")
    evs = list(log.events.find().sort("_id", -1).limit(args.last))[::-1]
    if evs:
        print(f"latest {len(evs)} events:")
    for d in evs:
        e = MongoEventLog._event(d)
        print(f"  #{e.seq:<6} {e.type:<24} {e.key}")
    return 0


def _atlas_watch(args: argparse.Namespace) -> int:
    import time as _t

    from proofread.store.mongo_store import MongoEventLog, MongoStore

    types = [t for t in (args.types or "").split(",") if t]
    try:
        if args.actions:
            coll = MongoStore().coll("actions")
            print(f"watching {coll.full_name} (Ctrl+C to stop)", flush=True)
            with coll.watch([{"$match": {"operationType": "insert"}}]) as stream:
                for ch in stream:
                    d = ch["fullDocument"]
                    mark = "OK  " if d.get("ok") else "VIOL"
                    pol = ",".join(d.get("failed_policies") or [])
                    print(f"{_t.strftime('%H:%M:%S')} {mark} {d.get('arm') or '-':<6} {d.get('episode_id', '')[:28]:<28} "
                          f"{d.get('kind', ''):<8} {d.get('path', '')} {pol}", flush=True)
        else:
            log = MongoEventLog()
            print(f"watching {log.events.full_name} (Ctrl+C to stop)", flush=True)
            for e in log.watch(consumer=args.consumer or None, types=types or None):
                brief = {k: v for k, v in e.payload.items() if k in ("edit_id", "status", "reason", "champion",
                                                                      "generation", "version", "delta_points")}
                print(f"{_t.strftime('%H:%M:%S')} #{e.seq:<6} {e.type:<24} {json.dumps(brief)[:160]}", flush=True)
    except KeyboardInterrupt:
        pass
    return 0


def _atlas_migrate(args: argparse.Namespace) -> int:
    from proofread.store.migrate import migrate_sqlite_to_mongo
    from proofread.store.mongo_store import MongoEventLog, MongoStore

    out = migrate_sqlite_to_mongo(args.db, MongoStore(), MongoEventLog(), progress=lambda s: print(s, flush=True))
    print(json.dumps(out, indent=1, default=str))
    return 0 if not out["events"]["conflicts"] else 3


def main(argv: list[str] | None = None) -> int:
    from proofread.genome.schema import DEFAULT_GENOME_PATH

    ap = argparse.ArgumentParser(prog="proofread")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ep = sub.add_parser("run-episode", help="run one agent episode and print the EpisodeResult")
    ep.add_argument("--task", required=True)
    ep.add_argument("--genome", default=str(DEFAULT_GENOME_PATH))
    ep.add_argument("--mode", choices=("observe", "enforce"), default="observe")
    ep.add_argument("--model", default="")
    ep.add_argument("--pristine", action="store_true")
    ep.add_argument("--arm", default="smoke")
    ep.add_argument("--budget-key", default="")
    ep.add_argument("--seed", type=int, default=0)
    ep.add_argument("--trace-dir", default="traces")
    ep.add_argument("--stub", action="store_true", help="use in-memory fakes (no docker, network or spend)")
    at = sub.add_parser("atlas", help="MongoDB Atlas backend: status, live watch, migration")
    atsub = at.add_subparsers(dest="atlas_cmd", required=True)
    st = atsub.add_parser("status", help="collections, counts, vector index status, latest events")
    st.add_argument("--last", type=int, default=8)
    st.set_defaults(fn=_atlas_status)
    wa = atsub.add_parser("watch", help="live tail of events (change stream)")
    wa.add_argument("--actions", action="store_true", help="tail verified actions instead of events")
    wa.add_argument("--consumer", default="", help="persist the resume token under this name")
    wa.add_argument("--types", default="", help="comma-separated event types to show")
    wa.set_defaults(fn=_atlas_watch)
    mi = atsub.add_parser("migrate", help="copy a SQLite store into MongoDB (idempotent)")
    mi.add_argument("--db", default="data/proofread.sqlite")
    mi.set_defaults(fn=_atlas_migrate)
    args = ap.parse_args(argv)
    if args.cmd == "atlas":
        return args.fn(args)
    if args.cmd == "run-episode":
        if not args.model:
            from proofread.models.config import agent_model

            args.model = agent_model()
        return asyncio.run(_run_episode(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
