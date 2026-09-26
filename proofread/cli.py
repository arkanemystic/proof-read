"""proofread CLI.

  proofread run-episode --task ID --genome PATH --mode observe|enforce --model SLUG [--pristine]
                        [--arm smoke] [--budget-key smoke] [--seed 0] [--trace-dir traces] [--stub]

--stub runs the full loop against in-memory fakes (FakeSandbox, StubVerifier, StubGrader and a
scripted model) with no Docker, network or spend; useful as a wiring check.
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

        runner = make_episode_runner(trace_dir=args.trace_dir)
    res = await runner(task, genome, mode=args.mode, model=args.model, seed=args.seed, arm=args.arm,
                       budget_key=args.budget_key, pristine=args.pristine)
    print(json.dumps(res.model_dump(mode="json"), indent=2))
    return 0 if not res.error else 2


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
    args = ap.parse_args(argv)
    if args.cmd == "run-episode":
        if not args.model:
            from proofread.models.config import agent_model

            args.model = agent_model()
        return asyncio.run(_run_episode(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
