# biject-oss: the formal gate's verifier service

Proofread's formal gate calls a biject-api compatible service (`POST /api/verify`,
`GET /api/policies`) through `proofread/verify/biject_client.py`. The proprietary biject-api cannot
evaluate the Proofread policies (see `proofread/verify/BIJECT_INTERFACE.md`). This directory is
the open-source earlier iteration of biject-api, **lean-agent-protocol**, adapted so that the Lean 4
kernel decides every Proofread policy on every action.

- Upstream: https://github.com/Akhatri98/lean-agent-protocol at commit `060b07f` (Apache-2.0, see
  `LICENSE`). Paper: Rashie & Rashi (2026), *Type-Checked Compliance: Deterministic Guardrails for
  Agentic Financial Systems Using Lean 4 Theorem Proving*, arXiv:2604.01483.
- Only the backend and lean-worker are vendored, and both are modified (list below).

## Run it

Without Docker (needs elan and uv; builds `proofread/policies/lean` on first start):

```
biject-oss/run-local.sh          # worker 127.0.0.1:19000, backend 127.0.0.1:18002
```

With Docker, from the repo root:

```
docker compose -f biject-oss/docker-compose.yml up -d --build
```

The backend listens on `127.0.0.1:18002`, which is `BijectVerifier`'s default URL. Once it is up,
`make_verifier("auto")` selects `BijectVerifier`. Set `PROOFREAD_VERIFIER=biject` to require it:
every action then fails closed while the service is down. To require an API key, put
`BIJECT_API_KEYS=<key>` in `biject-local/.env` (gitignored). `run-local.sh` and the client both
read that file; for Docker, pass `--env-file biject-local/.env`.

Checks:

```
uv run pytest -q tests/biject_oss tests/failclosed/test_biject_failclosed.py
uv run pytest -q tests/differential/test_biject_vs_reference.py   # 2,000 hypothesis actions, live
```

The `biject`-marked tests skip unless the service is running and serving the pinned policy hash.

## How a call is decided

1. `BijectVerifier` pins the service before it sends any action. `/api/policies` must list all six
   base policies for tool `proofread_action`, each with `source_hash ==
   lean_source_hash(proofread/policies/lean)`. The Lean port must also be aligned to the spec
   (`LEAN_SPEC_HASH == reference.BASE_POLICY_HASH`).
2. The client encodes the action with the same bridge the LeanVerifier uses (`encode_action`:
   NFC paths, NFKC-folded lines, lowercased host) and posts it as typed params.
3. The backend (`backend/app/orchestrator.py`) renders one Lean goal per policy from the typed
   params (`render.py`). Every character of data becomes its own char literal, so no input can
   close a literal or add Lean syntax. A goal looks like:
   `Proofread.codeTest001C ({ kind := .write, path := ['/','w',…] } : Proofread.ActionC) = true`
4. The lean-worker writes the goals into one file, one `example : <goal> := by decide +kernel` per
   line, followed by a sentinel. It runs `lean --json` against the compiled policies and attributes
   each diagnostic to its goal by line.
5. The verdict is `allowed` if every goal is proved and `blocked` with the complete
   `failed_policies` if some goals are refuted. It is `error` if anything else happens: an
   undecided goal, a missing or malformed param, a hash mismatch, an unavailable worker, or a Lean
   crash. The client treats `error` as FAILCLOSED.

### Why goals are stated on `ActionC`

In this toolchain, reducing `String.toList` on a string literal in the kernel is far slower than
the policy itself: about 0.2 s per 80 characters, and minutes for a 200-line file. The policies
were therefore refactored (by definition, so behavior is unchanged) into a `List Char` core
(`codeTest001C` … on `Proofread.ActionC`), and the `String` policies are `core ∘ Action.toC`. Goals
use Char-list literals directly.

Two kinds of kernel-checked lemmas in `Proofread/Policies.lean` justify how goals are cut down:

- `codeXxx001C_fields` (`rfl`): each policy reads only the fields it names, so a goal for
  CODE-TEST-001 does not carry the file's added lines.
- `codeSkip001C_chunks`: CODE-SKIP-001 equals the conjunction of the policy over any list of chunks
  that covers exactly the set of added lines (raw and NFKC). The backend deduplicates the lines and
  cuts them into chunks of at most 4,000 characters. Chunks go to the worker as separate batches,
  so a large write is checked by several Lean processes in parallel.
  `tests/biject_oss/test_render_and_goals.py` checks the coverage condition.

Measured locally (M-series Mac, 8 Lean processes): median 170 ms per action, p90 450 ms, and about
3 s for a 400-line write with non-ASCII content.

## Changes from upstream

Kept: the orchestrator/lean-worker split; conjectures rendered from registry metadata
(`parameter_map`, `param_transforms`); `decide` in the kernel as the only decision procedure;
`/api/verify`, `/api/policies`, `/api/health`; and the append-only JSONL audit log.

Changed:
- `lean-worker/worker.py`
  - Replaced `/verify` (raw conjecture text) with `/verify-batch`. The worker owns the imports and
    the tactic (`decide +kernel`), and rejects goals containing comment, command, or proof tokens.
  - Replaced marker-grepping of stdout with `lean --json` and per-line attribution, plus an
    end-of-file sentinel (a crash can no longer leave later goals looking proved).
  - Added a startup self-test (one goal it must prove, one it must refute; otherwise it reports
    `degraded` and refuses to serve).
  - Reports `source_hash` from `/health`; uses a threaded server with a bounded process pool;
    serves the Proofread Lake project (toolchain from its `lean-toolchain`, v4.34.1) instead of
    `PolicyEnv` (v4.28.0).
- `backend/app/orchestrator.py`
  - Checks all policies instead of stopping at the first refutation.
  - Adds structure-style arguments, chunked goals, and batching across worker processes.
  - Missing or malformed params are an `error` for `on_missing="error"` policies. Upstream skipped
    such a policy, which fails open.
  - Adds request `policy_hash` pinning.
- `backend/app/render.py` (new): typed `bool`, `enum`, `chars`, and `chars_list_json` transforms
  beside upstream's `int`/`bps`.
- `backend/app/policy_registry.py`: a fixed in-code registry of the six Proofread base policies
  (the base policy set is part of the trust boundary), replacing the JSON file seeded with trading
  policies.
- `backend/app/models.py`: biject-api identity constraints on requests. Responses carry
  `failed_policies`, `policy_hash`, `reject_code`, and `conjecture_sha256`. `PolicyMetadata` gains
  `lean_namespace`, `lean_arg_style`, `structure_type`, `positional_map`, `param_enums`,
  `on_missing`, `chunk`, `lemma`, and `source_hash`.
- `backend/app/main.py`
  - Optional bearer-key auth (`BIJECT_API_KEYS`) and a request body limit.
  - A kernel error returns verdict `error`; upstream returned `blocked` with a policy id, which
    would misattribute it as a policy violation.
  - The audit log stores digests of params and conjectures (params can hold whole files).
- `docker-compose.yml`: publishes the backend on 127.0.0.1 only; no Traefik/Coolify; the worker
  image compiles `proofread/policies/lean`, passed as an additional build context.

Removed: the React frontend; NL→Lean formalization (Claude + Aristotle) and PDF upload; Claude
back-translation of refutations (an Anthropic API call carrying action content on every block);
`/api/compile-policy` and `/api/policies/{id}/register` (runtime Lean compilation and registry
writes, which would let a caller change the policy set); the sandbox parser, mock trading agent,
log SSE, and audit WebSocket; and the trading `PolicyEnv` and deploy scripts.

Not verified here: the Docker images were not built on the development machine (Docker was not
running). The native path (`run-local.sh`) is what the tests above ran against.
