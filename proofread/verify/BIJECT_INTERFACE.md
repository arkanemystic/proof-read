# biject-api interface (as read from /home/dev/work/biject-api, read-only)

Sources: biject-api CLAUDE.md (Invariants, Request identity, Conjecture builder S4-A-02, File-to-role
map), AGENTS.md, docker-compose*.yml, backend/Dockerfile(.local), backend/app/models.py,
backend/app/main.py routes, backend/lean_worker/worker.py.

## Service

- FastAPI backend (uvicorn :8000 in the container) + redis. The Lean kernel pool runs in-process
  (AF_UNIX workers), PolicyEnv (Lake project, toolchain v4.28.0) is baked into the image at
  /app/PolicyEnv and built with `lake build` during `docker build`.
- Routes used by a verifier client:
  - `GET /api/health` (open scope)
  - `POST /api/verify` body `ToolCallRequest`:
    `{tool_name: ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$, params: {<=32 keys, str/int/float/bool},
      agent_id: ^[a-zA-Z0-9_-]{1,64}$ (required), call_id: ^[a-zA-Z0-9_.-]{1,64}$, session_id?}`
    returns `GuardrailResultResponse`: `{call_id, verdict: "allowed"|"blocked"|"skipped",
    explanation, lean_trace, latency_us, policy_id, conjecture, elab_us, reject_code, ...}`.
  - `GET /api/policies` -> `{policies: {id: PolicyMetadata}, count}`; PolicyMetadata carries
    `lean_module`, `lean_function`, `applies_to_tools`, `parameter_map`, `param_transforms`,
    `lean_arg_style` ("positional" | "structure"), `source_hash` (sha256 of the Lean source), ...
  - `POST /api/policies/{id}/register` (admin scope; needs BIJECT_ADMIN_KEYS).
  - `GET /api/audit/head`, `GET /api/audit/verify` (Ed25519-signed hash-chained audit ledger;
    signature is over `entry_hash`, key = AUDIT_SIGNING_KEY).
- Auth: `Authorization: Bearer <key>`; api scope keys from BIJECT_API_KEYS, admin scope from
  BIJECT_ADMIN_KEYS. Keys never in URLs.
- Decision path: for each applicable policy the orchestrator renders a conjecture
  `example : PolicyEnv.<f> <args> ... := by decide` from typed params and checks it in the Lean
  kernel (~150 to 200 ms per policy per call per their docs). Any failure to decide is a denial.

## Why biject-api cannot run the Proofread policies today (hard block)

1. Typed-input invariant (AGENTS.md "Verification inputs", CLAUDE.md S4-A-02): kernel parameters
   may only be enum/int/bool or shape-typed strings (`hex64`, `oid_path`, `ident`, at most 104
   UTF-8 bytes). There is deliberately no generic `str` transform. Proofread policies must classify
   raw paths (`/workspace/pkg/tests/test_x.py`) and raw added source lines (skip markers), which are
   exactly the free-form strings biject refuses to pass into the kernel. Encoding them would require
   pre-classifying in Python, which would move the decision out of Lean.
2. .olean signature gate: the backend verifies every compiled PolicyEnv .olean at import against
   an Ed25519 public key hardcoded in backend/lean_worker/worker.py. A locally built image signs
   with a local seed and exits at startup (documented KNOWN LIMITATION in
   docker-compose.override.example.yml; reproduced here, log line
   `SECURITY: olean signature check failed: /app/PolicyEnv/.lake/build/lib/lean/PolicyEnv/AttestedValue.olean`).
   Mounting our own Lean modules would hit the same gate. The CI-signed image used by
   docker-compose.demo.yml (ghcr.io/bijectai/biject-api:<sha>) returns `unauthorized` on pull.
3. Registering new policies needs admin keys plus `ENABLE_POLICY_COMPILE=true` for caller Lean,
   which the project documents as unsafe; and we may not edit biject-api.

## Reproduce the local start attempt

```
cd /home/dev/work/proofread
docker compose -p proofread-biject --env-file biject-local/.env \
  -f /home/dev/work/biject-api/docker-compose.yml -f biject-local/docker-compose.local.yml up -d --build
docker logs proofread-biject-backend-1 | tail     # olean signature check failed, restart loop
docker compose -p proofread-biject --env-file biject-local/.env \
  -f /home/dev/work/biject-api/docker-compose.yml -f biject-local/docker-compose.local.yml down -v
```

biject-local/.env holds fresh local keys (AUDIT_SIGNING_KEY, LEAN_SIGNING_KEY, BIJECT_API_KEYS,
BIJECT_ADMIN_KEYS, ORACLE_SIGNING_KEY); it is gitignored. The override binds 127.0.0.1:18002 and
uses backend/Dockerfile.local (slim). The prod compose is never used.

## What Proofread ships instead

- `proofread/policies/lean`: the six policies in Lean 4 (Bool functions, Prop wrappers, Decidable
  instances, path canonicalization and marker matching over raw strings), kernel `by decide`
  examples, `policycheck` JSON-lines executable.
- `LeanVerifier` (verify/lean_verifier.py): runs the compiled Lean policies; differential-tested
  against reference.py (zero disagreements on 2,000+ hypothesis actions).
- `BijectVerifier` (verify/biject_client.py): transport-complete client for `/api/verify`
  (tool_name `proofread_action`, params = bridge-encoded action fields plus `policy_hash`), pinned
  to a registry policy whose `source_hash` equals reference.BASE_POLICY_HASH. It is selected by
  `make_verifier("auto")` only when that pin is satisfied; otherwise ReferenceVerifier is used.
