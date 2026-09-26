# MongoDB schema (Atlas database `proofread_runs`)

Proofread keeps storage behind three ports (`Store`, `EventLog`, `VectorIndex`, see `proofread/contracts.py`).
`proofread/store/factory.py` selects the MongoDB backend (`proofread/store/mongo_store.py`) when `MONGODB_URI` is set
(unless `PROOFREAD_STORE_BACKEND=sqlite`), otherwise SQLite. The same documents are stored by both backends; this
file describes the Mongo layout used on Atlas for section 10c.

## Collections

| Collection | `_id` | What one document is | Written by |
|---|---|---|---|
| `harness_versions` | `<run_id>:v<n>` | One genome version of a run: `arm`, `run_id`, `version`, `parent`, `edit_id` that produced it, `generation`, `genome` (the full nested genome document), `genome_hash`, `score`, `created_at` | orchestrator (champion init, promotion) |
| `edits` | `<run_id>-g<gen>-c<i>` | One proposed genome edit and its whole lifecycle: `intent`, `rationale`, `patch` (RFC 6902 JSON Patch against the parent genome), `parent_version`, `parent_hash`, resulting `genome` and `genome_hash`, `status` (lifecycle `proposed` then `invalid`, or `evaluating` then `rejected_formal`, `rejected_empirical` or `promoted`), `screening`, `violations` (per episode), `delta_points`, `lb_points`, `n_pairs`, `reason` / `gate_reason`, `retrieved` (similar past rejections shown to the proposer), `proposer_cost_usd`, timestamps | orchestrator; status updated in place |
| `rejected_edits` | same as the edit | Copy of every rejected edit plus the Vector Search fields `embedding` (512 floats), `_vec_ns` (namespace, `rejected:<run_id>`), `_vec_meta` | orchestrator + `AtlasVectorIndex.add` |
| `episodes` | store key (`<arm>\|<model>\|<task>\|<mode>\|s<seed>\|...`) or `<arm>-<uuid>` | One agent episode (`EpisodeResult`): `arm`, `run_id`, `task_id`, `variant`, `split`, `mode` (observe or enforce), `model`, `seed`, `generation`, `candidate_id`, `genome_hash`, grades `passed_workspace` and `passed_pristine`, `mechanism` (cheat classification), `violations` and `effective_violations` (the verdicts, embedded), `n_actions`, `n_denied`, `verifier_latency_ms` (one entry per verified action), `cost_usd`, `turns`, `error`, `trace_path`, timestamps | episode runner |
| `actions` | `<store>::<episode_id>:<verify#>:<i>` | One effect-level Action that was checked by the verifier: `episode_id`, `step`, `kind` (write, delete, rename, chmod, exec, connect), `path`, `dst`, `host`, `cmd` (300 chars), `n_added_lines`, `n_removed_lines`, `attributed`, `failed` (policy ids that fired), `stage`, `round`, `ts` | `scripts/mongo_actions.py` from the trace `verify` records (the runner writes these to JSONL traces today) |
| `events` | `seq` (int) | Append-only event log: `seq`, `type` (`arm.started`, `edit.proposed`, `edit.decided`, `generation.done`, ...), idempotency `key` (unique), `payload`, `ts` | `MongoEventLog.append` (transaction on the `counters` doc) |
| `counters` | `events` | Next event seq | `MongoEventLog` |
| `cursors` | consumer name | Last seq a consumer processed | `MongoEventLog.set_cursor` |
| `stream_tokens` | `<consumer>:<collection>` | Persisted change-stream resume token (`token`, `ts`) | `EditsChangeFeed` |
| `vectors` | `<namespace>:<doc_id>` | Only for runs that used the numpy index over a MongoStore (and migrated SQLite vector rows) | `NumpyVectorIndex`, migration |
| `migrated_events`, `migrated_cursors` | `<store>::<seq or consumer>` | Events and cursors copied from the six SQLite run stores (kept out of `events` so the live seq space never collides) | `scripts/mongo_migrate_all.py` |

Every migrated document also carries `source_store` (imp3, rerun, proofread, imp, imp2, smoke), `run_label`
(the doc's `run_id`, else `<store>:<arm>`), `orig_id` and `migrated: true`; its `_id` is `<store>::<orig id>` because
baseline episode ids repeat across stores. Documents written live by the orchestrator have no prefix.

## Why documents fit this data

- **The genome is a nested document that evolves by JSON Patch.** A genome is a tree (system prompt sections,
  workflow knobs, context policy, tool configuration, added guardrail policies). An edit is literally an RFC 6902
  patch against that tree, so storing the genome as a nested document means the stored form is the form the
  proposer edits, the schema validator checks (the trust boundary forbids patch paths into tools, interceptor,
  verifier, graders, sandbox and base policies) and the paired gate evaluates. No shredding into tables, and a new
  genome field (a new prompt section, a new guardrail) needs no migration.
- **Lineage is a chain of edit documents.** Each `harness_versions` doc points to its `parent` and the `edit_id`
  that produced it; each `edits` doc holds the patch, parent hash, gate evidence and final status. Walking lineage is
  following `parent` links; auditing a promotion is reading one edit document (patch, violations per episode,
  paired delta, lower bound, reason). Rejected edits stay as documents and become the retrieval memory for the
  proposer through Atlas Vector Search on the same collection, so no separate vector store is needed.
- **Verdicts are embedded per episode.** The formal gate needs, per candidate, which episodes had which
  violations; the report needs pass flags, mechanism, violations and latency per episode. Embedding the verdict
  summary (`violations`, `effective_violations`, `n_denied`, `verifier_latency_ms`) in the episode makes every
  gate and report query a single-collection `$match`/`$group` (see `scripts/mongo_numbers.py`), while the raw
  per-action records live in `actions` and the full traces on disk.
- **Change streams drive the event side.** Edit status transitions are observed with a change stream on `edits`
  (`EditsChangeFeed`, resume token persisted in `stream_tokens`, at-least-once, idempotent handlers) instead of
  polling, and the append-only `events` log keeps a total order for replay.

## Indexes

B-tree (created by `scripts/mongo_indexes.py`, idempotent):

| Collection | Keys | Serves |
|---|---|---|
| `actions` | `{episode_id: 1}` | all actions of an episode (trace view, LAP replay, attribution checks) |
| `episodes` | `{arm: 1, task_id: 1}` | per-arm pass rates, paired per-task comparisons (gate and holdouts) |
| `edits` | `{status: 1, arm: 1}` | gate ledger by outcome, pending edits for resume |
| `harness_versions` | `{arm: 1, version: 1}` | current champion of an arm, lineage walk |
| `events` | `{key: 1}` unique, `{seq: 1}` unique | idempotent append, ordered replay (from `MongoEventLog`) |

Atlas Vector Search (one search index, the free tier allows few): `rejected_edits_embedding` on
`rejected_edits`, `{"type": "vector", "path": "embedding", "numDimensions": 512, "similarity": "cosine"}` plus
filter field `_vec_ns`. Dimensions and similarity come from `proofread/store/vector.py` (`DIM = 512`, L2-normalised
hashed bag of words, cosine). Queries use `exact: true` (ENN; identical top-k to numpy at our sizes, checked in
`results/final/mongo_vector_demo.json`), `exact: false` with `numCandidates` for ANN at scale.

## Scaling

- **Hot write path: `actions`.** Every tool call produces one or more Action documents plus a verifier call, so
  `actions` grows about 10 times faster than `episodes` (6,804 actions for 879 migrated episodes here) and is written
  concurrently by every sandbox. Writes are small, insert-only and keyed by episode.
- **Sharding.** Shard `actions` on `{episode_id: "hashed"}`: inserts spread evenly across shards (episode ids are
  random), and the dominant read (all actions of one episode) still targets a single shard. `episodes` can shard on
  hashed `_id` or on `{arm: 1, task_id: 1}` if per-arm scans dominate; `edits`, `harness_versions` and `events` are
  tiny (a few documents per generation) and stay unsharded.
- **TTL on raw traces.** Raw per-turn trace documents (model text, tool output, added line contents) are large and
  only needed for debugging and red-team review. If they move from JSONL into Mongo, put them in their own
  collection (`traces`) with a TTL index on a `created_at` date (for example 14 days), keeping `actions`,
  `episodes` and `edits` (the audit record) forever. Archive to object storage before expiry if needed.
- **Expected bottleneck at 10x load.** Not Mongo storage but the serialised event log: every `EventLog.append`
  is a transaction that increments the single `counters` document, so appends are serialised cluster-wide
  (fine at hundreds per run, a hot spot at tens of thousands of concurrent episodes). Second: action inserts
  if written one at a time. Handling: (1) batch action inserts per verify round (`insert_many`, `ordered=false`,
  `w: majority` only at episode end); (2) keep the global seq only for low-rate control events (edit and generation
  transitions) and use per-run or per-episode sequences (compound `_id: {run_id, seq}`) for high-rate events, or
  let change streams on `edits`/`episodes` replace high-rate events altogether; (3) move the per-action verifier
  latency array out of the episode document if it exceeds a few thousand entries (16 MB document limit is far
  away, but large arrays make updates expensive); (4) on Atlas, scale from the shared tier to a dedicated tier
  before sharding, and use search nodes for Vector Search once `rejected_edits` reaches the tens of thousands.
