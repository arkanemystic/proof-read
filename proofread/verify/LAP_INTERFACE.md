# Lean-Agent Protocol (LAP) lean-worker: real interface (as observed)

Source: https://github.com/Akhatri98/lean-agent-protocol, cloned read-only at
vendor/lean-agent-protocol (commit 060b07f753147a0c9b42e41b79a154b8024ee045,
2026-04-23). Only `lean-worker/` is run. The backend (FastAPI, needs ANTHROPIC_API_KEY and
ARISTOTLE_API_KEY for natural-language policy formalization) is not run; we call the worker directly,
exactly as the backend's `app/orchestrator.py` does.

## Running it

    docker build -t lap-lean-worker:local vendor/lean-agent-protocol/lean-worker
    docker run -d --name lap-lean-worker -p 127.0.0.1:9100:9000 --restart unless-stopped lap-lean-worker:local

Image: ubuntu 22.04, elan with Lean `leanprover/lean4:v4.28.0`, a Lake project `/app/PolicyEnv`
(library `PolicyEnv`, modules `PolicyEnv.Basic`, `CAP001`, `PRC001`, `POS001`: trading demo policies),
pre-built with `lake build`. `worker.py` is a stdlib `http.server.HTTPServer` on port 9000. It is
single threaded: requests are served one at a time. Bound to 127.0.0.1:9100 on this host. No volume is
mounted, so registered policies live in the container filesystem; the client re-registers on first use.
Proofread's client URL: env `PROOFREAD_LAP_URL`, default `http://127.0.0.1:9100`.

## Endpoints

| Method, path | Request | Response |
|---|---|---|
| GET /health | none | `{"status":"ok","policies_loaded":N}` (N = number of `PolicyEnv/*.lean` except Basic) |
| GET /modules | none | `{"modules":["PolicyEnv.Basic",...]}` (the `import` lines of `PolicyEnv.lean`) |
| POST /compile-policy | `{"policy_id": str, "lean_code": str}` | `{"success": bool, "error": str or null, "module_name": str or null}` |
| POST /verify | `{"conjecture": str}` | `{"result": "proved"\|"refuted"\|"error", "trace": str, "latency_us": int, "elab_us": int or null}` |

### How policies compile and register

`/compile-policy` sanitizes `policy_id` to `[A-Za-z0-9_]`, writes `lean_code` to
`/app/PolicyEnv/PolicyEnv/<id>.lean`, appends `import PolicyEnv.<id>` to `PolicyEnv.lean`, and runs
`lake build` (timeout 120 s). On failure it rolls back both files, rebuilds, and returns the Lake log in
`error`. Re-registering the same id overwrites the file (idempotent). Proofread registers
`proofread/policies/lap/PROOFREAD.lean` with `policy_id = "PROOFREAD"`, giving module
`PolicyEnv.PROOFREAD`, namespace `PolicyEnv.Proofread`. A successful registration takes about 3 to 10 s.

### How conjectures are submitted and decided

`/verify` writes the conjecture to a temp file, inserts `set_option profiler true` after the imports,
and runs `lean <file>` with `LEAN_PATH` pointing at the Lake build output (timeout 30 s). The
conjecture is a complete Lean file. LAP's own backend builds, per policy,
`import PolicyEnv.X` + `example : pred args = true := by decide`. Proofread uses the same shape:

    import PolicyEnv.PROOFREAD
    example : PolicyEnv.Proofread.allowed { kind := 0, path_present := true, ..., attributed := true } = true := by decide

Result mapping by the worker: exit code 0 gives `proved`. Nonzero exit with any of the substrings
`decide tactic failed`, `native_decide tactic failed`, `is false`, `type mismatch`,
`application type mismatch` gives `refuted`; anything else gives `error`; a 30 s timeout gives
`error` with trace `lean timed out after 30s`. Because `type mismatch` also maps to `refuted`,
Proofread's client only treats `refuted` as a policy refutation when the trace contains
``Tactic `decide` proved that the proposition`` and `is false` (the Lean 4.28 message for a
decidably false proposition). Every other outcome fails closed.

Note: a file with several `example`s is `proved` only if every one of them is proved; the replay uses
this to check one claim per recorded action in files of 64 examples, bisecting on failure.

## Latency (this host, measured)

- Per `/verify` call: about 180 to 270 ms wall inside the worker (process start plus `.olean`
  import dominates), of which Lean elaboration is about 1 to 2 ms (`elab_us`). Worker warmup probes:
  271 ms and 270 ms.
- One `allowed` conjecture per action (proved) costs one call; a refuted one costs 1 + 6 calls (one per
  base policy) to name the violated policies.
- A file with 64 per-action examples: about 1.5 s.
- The server is single threaded, so client concurrency above 1 only overlaps HTTP latency.

## Proofread client

`proofread/verify/lap_client.py` (`LapVerifier`, implements `contracts.Verifier`), facts in
`proofread/verify/facts.py`, policies in `proofread/policies/lap/PROOFREAD.lean`, axioms in
`proofread/policies/lap/AXIOMS.txt`. Fail closed on: worker unreachable, HTTP status != 200, timeout,
non-JSON or non-object body, unknown `result`, missing `trace`, `error`, `refuted` without the decide
marker, registration failure, an `allowed` refutation that no single policy explains, and any fact
extraction exception. Fail-closed verdicts carry `failed_policies=["FAILCLOSED"]`, `source="failclosed"`,
`detail` prefixed `LAP:`.
