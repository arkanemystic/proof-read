import asyncio, json
from proofread.agent.episode import make_episode_runner
from proofread.benchmarks.impossiblebench import list_tasks
from proofread.evolve.orchestrator import to_contract_task
from proofread.benchmarks.impossiblebench import load_task
from proofread.genome.schema import default_genome
from proofread.store.sqlite_store import SqliteStore
MODELS=["qwen/qwen3.8-27b:free","nvidia/nemotron-3-ultra-550b-a55b:free","google/gemma-4-31b-it:free"]
async def main():
    runner=make_episode_runner(); store=SqliteStore("data/proofread.sqlite"); g=default_genome()
    ids=list_tasks("selection_original")[:2]
    jobs=[(m,to_contract_task(load_task(t))) for m in MODELS for t in ids]
    res=await asyncio.gather(*[runner(t,g,mode="observe",model=m,arm="freepick",budget_key="selection") for m,t in jobs],return_exceptions=True)
    for (m,t),r in zip(jobs,res):
        if isinstance(r,Exception): print(m,t.id,"EXC",repr(r)[:200]); continue
        store.put("episodes",f"freepick|{m}|{t.id}",r.model_dump(mode="json")|{"split":"selection_original"})
        print(json.dumps({"model":m,"task":t.id,"ws":r.passed_workspace,"turns":r.turns,"viol":r.violations,"err":(r.error or "")[:150]}),flush=True)
asyncio.run(main())
