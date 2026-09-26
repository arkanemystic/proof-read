"""Print concrete violating actions (and denials) from rerun traces, for RERUN_REPORT.md."""
import json, sqlite3, sys

c = sqlite3.connect("data/rerun.sqlite")
eps = [json.loads(d) for (d,) in c.execute("SELECT doc FROM docs WHERE collection='episodes'")]
limit = int(sys.argv[1]) if len(sys.argv) > 1 else 6
shown = 0
for e in sorted(eps, key=lambda e: (e["arm"], e["task_id"])):
    if not (e.get("violations") or e.get("passed_workspace") or e.get("n_denied")):
        continue
    print(f"## {e['arm']} {e['task_id']} {e['episode_id']} mech={e.get('mechanism')} "
          f"passed_ws={e.get('passed_workspace')} violations={e.get('violations')} denied={e.get('n_denied')}")
    for line in open(e["trace_path"]):
        r = json.loads(line)
        k = r.get("type")
        if k == "verify":
            for a, f in zip(r.get("actions", []), r.get("failed", [])):
                if f:
                    print(f"  VIOLATION {f} stage={r.get('stage')} kind={a.get('kind')} path={a.get('path')} "
                          f"dst={a.get('dst')} cmd={(a.get('cmd') or '')[:160]!r} added={(a.get('added_lines') or [])[:3]}")
        elif k == "tool" and str(r.get("output", "")).startswith("DENIED"):
            print(f"  DENIED turn={r.get('turn')} {r.get('name')} {json.dumps(r.get('arguments'))[:200]}")
    shown += 1
    if shown >= limit:
        break
