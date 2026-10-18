# Capability status: implemented, simulated, planned

STAG is a **local development and simulation project**. This matrix states what each capability
does today so demo counters are not mistaken for measured results or tested production controls.

| Capability | Implemented (tested) | Simulated (demo only) | Planned |
|---|---|---|---|
| Deterministic detection | Rule engine; `trigger` events emitted before enrichment; failures in indexing/retrieval/generation do not suppress detection | — | Independent paging destination; bounded work queues; detection-only degraded startup |
| Identity & dedup | Canonical observation keys; envelope hash; register-before-index; source-scoped store keys | — | Transactional store with unique constraints across workers |
| Incident lifecycle | Versioned state machine; RESOLVED only via VERIFYING; recorded transitions with actor/reason | — | Durable persistence; multi-process consistency |
| Material-change investigation | Requests only on new episode / severity increase / new signature / recurrence / cooldown | — | Queued/running/succeeded/failed work lifecycle; stale-result handling |
| Bounded investigation | Assignment snapshot; capability catalog; scope checks on parameters; limits | Template findings from `DeterministicExecutor` | Trusted tool/model gateway with credentials unavailable to executors |
| Remediation ledger | Terms-bound operation id; APPLIED vs VERIFIED; no dispatch from uncertain states; retry budget; server-derived freshness in the API | Dispatch changes a record only | Destination adapters; live preconditions; conflict locks; approval expiry |
| Recovery verification | Typed verdicts; unknown never resolves; evidence validation before write | Fixture verdicts in scenarios (`simulated: true`) | Independent fresh probes over observation windows |
| Budgets | Finite non-negative arithmetic; reserve/settle; settled refs immutable | One unit per recorded model call in crews | Real token/cost accounting; atomic enforcement across workers |
| Crews | Schema-enforced gates (human approver, policy ≤ medium, no automated mutation); end-to-end runs with and without approval; CLI exit codes | Every run (`mode: simulation`) | Durable, resumable live workflow with authenticated approvals |
| Replay / late events | Replay observations isolated from live incidents; late evidence corrects the timeline; event-time recurrence | — | Watermarks, clock-skew policy, isolated replay outputs per adapter |
| API | Bounded query params; readiness in `/api/health`; 422 on invalid scenario input | — | **Authentication and authorization (none today)**; rate limits; job admission |
| Knowledge stores | In-memory NumPy cosine + BM25 + NetworkX with file persistence | — | Consistent atomic snapshots; retention; retrieval evaluation |
| Generation | Output validated at one boundary; `generation_mode` llm / fallback / degraded | Template fallback without an API key | Claim-level grounding; model-route governance |
| Packaging | Wheel bundles skills/crews/agents; validators fail on empty catalogs; CI installed-package check | — | Container smoke test in CI |
| Dashboard | Same-origin WebSocket with backoff; trigger and dedup visibility; simulation label on crews | — | History hydration; accessibility pass |

## Counters and what they mean

- **investigations suppressed / model calls avoided** = duplicate deliveries + evidence-only
  updates. It counts investigation *triggers* that did not start, not provider spend.
- Crew **resolved** means the simulated workflow reached RESOLVED with fixture verdicts.

## Migration notes

- Knowledge-store document keys changed from `<event id>` to `<source>::<event id>`. Delete
  `data/` (or rebuild the index) when upgrading from 0.2.x.
- `ActionLedger.reconcile(True)` now yields `APPLIED`; callers must `verify` to reach `VERIFIED`.
- `reconcile(False)` yields `RETRY_READY`; the retry is an explicit `dispatch`.
- Operation ids now include `policy_version` and `preconditions`.
- `IncidentState.TRIAGED → RESOLVED` was removed; use `DISMISSED` for administrative closure.
