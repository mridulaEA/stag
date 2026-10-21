# Capability status: implemented, simulated, planned

STAG is a **local development and simulation project**. This matrix states what each capability
does today so demo counters are not mistaken for measured results or tested production controls.

| Capability | Implemented (tested) | Simulated (demo only) | Planned |
|---|---|---|---|
| Deterministic detection | Rule engine; `trigger` events emitted before enrichment; failures in indexing/retrieval/generation do not suppress detection; paging on material change goes through the outbox before any model work | — | Bounded work queues; detection-only degraded startup |
| Identity & dedup | Canonical observation keys; envelope hash; register-before-index; source-scoped store keys; unique-insert registration in the control store (one winner across processes, tested with four OS processes on one SQLite file) | — | Retention policy by event time (purge is available, not scheduled) |
| Incident lifecycle | Versioned state machine; RESOLVED requires a current successful verification record (generic transitions cannot resolve); recorded transitions with actor/reason; every mutation is load → mutate → compare-and-swap in a transaction; state survives restart (`CONTROL_STORE_URL`: SQLite or PostgreSQL) | — | Schema migrations |
| Material-change investigation | Requests only on new episode / severity increase / new signature / recurrence / cooldown; request lifecycle queued → running (leased to one worker) → succeeded \| failed; expired leases reclaimable; staleness query | — | Automatic re-drive of orphaned requests after restart (today they are listed as pending and claimable) |
| Bounded investigation | Assignment snapshot; capability catalog; scope checks on parameters; limits | Template findings from `DeterministicExecutor` | Trusted tool/model gateway with credentials unavailable to executors |
| Remediation ledger | Terms-bound operation id with integrity digest checked on every command; APPLIED vs VERIFIED; no dispatch from uncertain states; cumulative retry ceiling with audited extension; server-derived freshness in the API | Dispatch changes a record only | Destination adapters; live preconditions; conflict locks; approval expiry |
| Recovery verification | `VerificationResult` (source, window, signals) required; real-bool verdicts only; unknown never resolves; result bound to the incident's evidence version | Fixture verdicts in scenarios (`simulated: true`), accepted only by a simulation-mode coordinator | Independent fresh probes over observation windows |
| Budgets | Finite non-negative arithmetic; reserve/settle; settled refs immutable | One unit per recorded model call in crews | Real token/cost accounting; atomic enforcement across workers |
| Crews | Schema-enforced gates (human approver, policy ≤ medium, no automated mutation); end-to-end runs with and without approval; CLI exit codes | Every run (`mode: simulation`) | Durable, resumable live workflow with authenticated approvals |
| Stream ingestion (Kafka) | Manual commit after the pipeline acknowledges a durable outcome; commit position never passes an in-flight delivery; raw bytes decoded inside an error boundary; bad UTF-8 / JSON / shape / schema / size and missing stable ids are dead-lettered with coordinates and digest only; schema-drift keys counted; consumer rebuilt with bounded backoff; per-partition lag sampled; `halt` policy available | — | Dead-letter topic (today an in-memory bounded buffer); rebalance-aware commit on partition revocation; schema registry |
| Connectors | Action connector contract (dispatch idempotent on operation id, reconcile by observation) bound to the ledger by `ActionDispatcher`; Jira and ServiceNow adapters over an injected transport; notification contract with delivery receipts; dashboard, PagerDuty, Slack, email adapters; durable deduplicated outbox in the control store with leases and bounded retries; detector-driven paging (`PAGE_ON_SEVERITY`) independent of the model path; registry builds only what has credentials | Fake ticketing destination and fake channels (crews, tests); receipts say `simulated: true` | Kubernetes and webhook-executor action adapters; dead-letter topic for notifications; signed approval callbacks |
| Replay / late events | Runtime `TECHOPS_PROCESSING_MODE`: live rejects replay-tagged events before any effect; replay mode isolates them; late evidence corrects the timeline; event-time recurrence | — | Watermarks, clock-skew policy, isolated replay outputs per adapter |
| API | Bounded query params; `/api/health` readiness from the live task; `/api/ready` 503 when the stream task is dead; 422 on invalid scenario or empty verification evidence; loopback bind by default | — | **Authentication and authorization (none today)**; rate limits; job admission |
| Knowledge stores | In-memory NumPy cosine + BM25 + NetworkX with file persistence | — | Consistent atomic snapshots; retention; retrieval evaluation |
| Generation | Output validated at one boundary; `generation_mode` llm / fallback / degraded | Template fallback without an API key | Claim-level grounding; model-route governance |
| Packaging | Wheel bundles skills/crews/agents; validators fail on empty catalogs; CI installed-package check | — | Container smoke test in CI |
| Dashboard | Same-origin WebSocket with backoff; trigger and dedup visibility; simulation label on crews | — | History hydration; accessibility pass |

## Counters and what they mean

- **investigations suppressed / model calls avoided** = duplicate deliveries + evidence-only
  updates. It counts investigation *triggers* that did not start, not provider spend.
- Crew **resolved** means the simulated workflow reached RESOLVED with fixture verdicts.

## Migration notes

- Knowledge-store document keys changed to a canonical JSON pair `["<source>","<event id>"]`
  (0.3.0 briefly used `source::id`). Delete `data/` (or rebuild the index) when upgrading.
- `Incident.signal_key` is now a canonical JSON key; the readable form is
  `attributes["signal_display"]`, which is what scenario verification hints match.
- `ActionLedger.verify` takes a `VerificationResult` (or a dict with `source`, `window`, `signals`).
- `IncidentCoordinator(processing_mode=...)`: replay-tagged events are rejected in live mode.
- Default `HOST` is now `127.0.0.1`; set `HOST=0.0.0.0` explicitly (the Dockerfile does).
- `ActionLedger.reconcile(True)` now yields `APPLIED`; callers must `verify` to reach `VERIFIED`.
- `reconcile(False)` yields `RETRY_READY`; the retry is an explicit `dispatch`.
- Operation ids now include `policy_version` and `preconditions`.
- `IncidentState.TRIAGED → RESOLVED` was removed; use `DISMISSED` for administrative closure.
