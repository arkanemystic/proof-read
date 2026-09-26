## 1. Project definition (write this section verbatim to CLAUDE.md as your first action)

Proofread is a self-improving coding agent harness. A proposer model edits the harness genome
(prompts, workflow, context policy, tool configuration, added guardrails). Each candidate edit must
pass two gates before promotion:
1. Formal gate: every action the inner agent takes under the candidate is checked by biject-api
   (Lean 4 decidable policies). Any violation rejects the edit.
2. Empirical gate: the candidate must beat the champion on a training split by a paired test.
Storage and eventing go through ports with a local SQLite backend. MongoDB Atlas comes later.

Non-negotiable design rules:
- Judge effects, not commands. After every tool call, hash-diff the workspace and emit normalized
  Action records for what changed. Before grading, diff again; unattributed changes are violations.
- Trust boundary: the genome can never modify tool implementations, the action interceptor, the
  verifier client, graders, sandbox config, or the base policy set. Enforced by schema.
- Guardrails are monotone: the harness may ADD policies, never remove or weaken base policies.
- Fail closed: verifier timeout, error, malformed response, or policy hash mismatch = violation.
- Two graders: `workspace` runs tests present in the workspace after the episode (gameable; used
  for training reward and cheat measurement). `pristine` runs original tests in a fresh container
  with only non-test files copied in (capability holdout).
- Two verification modes: `observe` records violations without blocking (candidate eval, so
  counterfactual scores exist). `enforce` verifies before effect and denies violations (champions,
  holdouts).

Stack: Python 3.12, uv, pydantic v2, asyncio, Docker with gVisor (runtime `runsc`, verified working
on this box), pytest + hypothesis, SQLite WAL behind Store/EventLog ports, numpy cosine index behind
a VectorIndex port. One model client module; model names only in config.

Models: proposer claude-opus-5-5; inner agent chosen by the pre-registered S6b selection step
(recorded in config and DECISIONS.md; use claude-sonnet-5 for smoke runs before S6b); baselines claude-haiku-4-5-20251001,
claude-sonnet-5, claude-opus-5-5, plus one current flagship each from OpenAI and Google if their
keys exist in .env.

Layout:
proofread/{genome,sandbox,actions,verify,policies,agent,graders,benchmarks,evolve,store,analysis}/
baselines/   (owned by the eval subagent only)
tests/{golden_cheats,benign,differential,failclosed}/

Paths: repo /home/dev/work/proofread (remote origin = github.com/arkanemystic/proof-read);
biject-api /home/dev/work/biject-api (read-only); ImpossibleBench /home/dev/work/vendor/impossiblebench.

