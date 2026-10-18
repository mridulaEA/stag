# Review findings ledger

Sources: *STAG: Complete Engineering Review and Claude Implementation Handoff* (October 18, 2026;
32 findings against `16e70b2`) and *STAG Repair verification* (October 18, 2026; recheck of
`060cc16`, 12 follow-up findings N01–N12). This ledger tracks each finding's status on the branch.
Status values: **fixed** (demonstrated defect closed within the stated local scope, with a
regression test), **partial** (repaired behaviour plus a remaining acceptance gap or deferred
production integration), **planned** (substantive runtime gap; documentation alone does not close
it). A "fixed" status never claims a production guarantee; see `docs/STATUS.md`.

Review decision we are honouring: STAG remains a **local development and simulation project**. No
production mutation tools or sensitive multi-tenant data should be connected until the Phase 1–3
items below are implemented.

| ID | P | Finding | Status | Where / test | Notes |
|---|---|---|---|---|---|
| R01 | P1 | Authenticate every API and WebSocket caller | planned (Phase 1) | — | Needs the owner's identity provider. Health reports `mode: local-demo`; compose and the CLI default now bind to loopback (`HOST=127.0.0.1`). |
| R02 | P1 | Resolve approval freshness from server state | fixed | `stag/api/routes.py` authorize/dispatch; `tests/test_techops_api.py::test_action_ledger_flow_over_api` | Approval binds to the server's current incident version; dispatch revalidates from server state with no caller version; `expected_incident_version` is a consistency check only. Operations require an existing open incident. |
| R03 | P1 | Do not retry an unknown action outcome blindly | partial | `ActionLedger` `RETRY_READY`, `RetryBudgetExceeded`; cumulative ceiling (N02) | OUTCOME_UNKNOWN/RECONCILING are not dispatchable; the attempt ceiling is cumulative across re-approval; audited `extend_retry_budget`. Destination-side idempotency keys: Phase 3. |
| R04 | P1 | Separate operation application from verified recovery | partial | `ActionState.APPLIED`; `VerificationResult`; RESOLVED requires a current successful `VerificationRecord` (N01/N07) | Generic transitions cannot resolve; empty or integer verdicts rejected. Independent fresh probes: Phase 3. |
| R05 | P1 | Bind immutable operation terms and enforce preconditions | partial | operation id covers policy + preconditions; caller input detached; integrity digest checked on authorize/dispatch/verify; `view()` returns detached copies (N03) | Live preconditions, conflict locks, approval expiry, destination adapters: Phase 3. |
| R06 | P1 | Make crew verification fail closed | partial | `Verdict` enum; runner defaults to UNKNOWN; executor verdicts must be real bools and evidence must validate (N07) | Fixture evidence is `simulated` and accepted only by a simulation-mode coordinator. Real health probes: Phase 3. |
| R07 | P1 | Enforce crew policy limits in the schema and runtime | fixed | `stag/crews/schema.py` validators; runtime ceiling check | Policy ceiling ≤ medium, unique roles, human approver required, automated roles cannot mutate, gate ordering enforced. |
| R08 | P1 | Enforce scope and capability grants outside the executor | partial | `BoundedRun` snapshot, catalog check, scope check on parameters; steps cannot widen roles | A trusted tool/model gateway with credentials unavailable to executors: Phase 2. |
| R09 | P1 | Honor partial findings and escalation in crew control flow | partial | runner holds on `partial` **or** `escalate` with a recorded disposition (N06) | Continuing after escalation needs a separately recorded human decision; that review service is Phase 4. |
| R10 | P1 | Repair budget arithmetic and account for actual usage | partial | `BudgetLedger` validates finite non-negative amounts; settled refs cannot be reused; runner settles in `finally` | Real token/cost accounting through a gateway and atomic multi-worker enforcement: Phase 2. |
| R11 | P1 | Deduplicate before indexing and stateful trigger evaluation | fixed | `IncidentCoordinator.register/correlate`; orchestrator + runner order; `test_duplicates_are_not_indexed_or_counted_by_stateful_rules` | Non-triggering observations are registered too. |
| R12 | P1 | Use canonical identities and hash the meaningful envelope | partial | canonical JSON machine keys for observations, signal keys (extras as mapping) and store/UI document keys (N05) | Tenant/source-instance scoping needs authenticated registration: Phase 1. Existing indexes need a rebuild (STATUS). |
| R13 | P1 | Separate replay and event-time recovery from live processing | partial | runtime `processing_mode` (live/replay/simulation); replay-tagged events rejected before any effect in live mode and isolated in replay mode; event-time recurrence; late evidence (N04) | Watermarks, clock-skew policy and replay outputs per adapter: Phase 2. |
| R14 | P1 | Persist control state with transactions and worker coordination | planned (Phase 1) | — | Needs the owner's operational database choice. Docstrings no longer claim durability. |
| R15 | P1 | Keep deterministic detection independent of enrichment | partial | deterministic detection emitted before indexing; indexing bounded by `INDEX_TIMEOUT_SECONDS`; failures isolated (N08) | Bounded work queues, durable admission, embedding offload: Phase 2. |
| R16 | P1 | Give investigation requests a real work lifecycle | planned (Phase 1) | — | `mark_investigated` documented as *request* time. |
| R17 | P1 | Align Kafka acknowledgment with durable processing | planned (Phase 2) | — | Needs a broker in tests. |
| R18 | P1 | Enforce tenant, classification and model-egress policy | planned (Phase 1–2) | — | Skill describes the design; no runtime claims it is enforced. |
| R19 | P2 | Own background tasks and bound blocking work | partial | owned task with supervision: unexpected end clears readiness and records `last_error`; restart allowed (N12) | Embedding offload and bounded concurrency: Phase 2. |
| R20 | P2 | Correct retrieval identity, relevance and query construction | partial | source-scoped doc keys; query builder keeps service/message/error identifiers | Sparse relevance floor, RRF score handling, self-retrieval exclusion: Phase 2. |
| R21 | P2 | Make knowledge persistence consistent and bounded | planned (Phase 2) | — | |
| R22 | P2 | Validate model output and expose degraded generation | fixed | `InsightGenerator._build_insight`; `generation_mode`/`degraded_reason`; `tests/test_generator_validation.py` | |
| R23 | P2 | Keep crew simulation distinct from a resumable live workflow | partial | reports carry `mode: simulation`; impossible live-resume text removed; API endpoint documented as simulation | Durable run ids and resumable steps: Phase 3. |
| R24 | P2 | Validate workflow control flow and fail CI on failed runs | fixed | trigger construction validated; limits bounded; gate ordering; step budget; CLI exits 1 on failed steps/unmet expectations; CI runs `run-all` | |
| R25 | P2 | Bound and validate public input at admission | partial | typed scenario enums, repeat/event caps, query ranges, 422 on invalid scenario | Rate limits and job admission: Phase 4. |
| R26 | P2 | Repair WebSocket deployment, teardown and incident visibility | partial | per-effect socket/timer ownership with socket-identity guards; history hydration on connect; trigger metadata retained (N11) | Browser/StrictMode harness test and accessibility pass: Phase 4. |
| R27 | P1 | Prevent skill installers from deleting their own source | fixed | `_is_source` guard; copy mode for ephemeral clones; installer tests | |
| R28 | P2 | Package crews, skills and required assets in installed distributions | fixed | `setup.py` build hook, `stag/_resources.py`, `scripts/check-packaging.sh`, CI `packaging` job; validators fail on empty catalogs | |
| R29 | P2 | Repair and test the container deployment path | partial | multi-stage Dockerfile, catalogs copied, non-root, no baked `.env`, `.dockerignore`, loopback compose binding | Docker build not exercised in this environment; CI container smoke: Phase 4. |
| R30 | P2 | Close dependency and test-coverage gaps | partial | +40 tests incl. regression probes, installed-package job | npm advisory triage and lock strategy: Phase 4. |
| R31 | P2 | Align observability and documentation with implemented behavior | partial | `docs/STATUS.md`; readiness reflects the live task; `/api/ready`; counters relabelled (N12) | Measured cost metrics: Phase 2. |
| R32 | P2 | Do not use the winning rule as the sole incident identity | planned (Phase 4) | crews may group by service only (`symptom_from_rule: false`) | Correlation redesign needs reviewed cases. |

## Recheck follow-up findings (N01–N12, from the `060cc16` verification)

| ID | P | Finding | Status | Where / test |
|---|---|---|---|---|
| N01 | P1 | A failed recovery verdict could still resolve via a workflow jump | fixed | `Incident.record_verification` / `resolution_blocker`; coordinator `record_verification`; `test_n01_*` |
| N02 | P1 | Re-approval reset the effective retry limit | fixed | cumulative `_attempt_ceiling`, `extend_retry_budget`; ctor validation; `test_n02_*` |
| N03 | P1 | Approved terms hashed but mutable | fixed (in-process) | deep-copied input; `_check_integrity` on authorize/dispatch/mark_applied/verify; `view()`; `test_n03_*` |
| N04 | P1 | Replay isolated too late; shared dedup state | fixed | `processing_mode` on the coordinator; replay rejected before rules/index in live mode; isolated namespace in replay mode; orchestrator drops replay-tagged events; `test_n04_*` |
| N05 | P1 | Canonical identity inconsistent across signal and store keys | fixed | `SignalKey.key` JSON with extras mapping, `.display` separate; `doc_key` JSON; UI `keyOf` JSON; `test_n05_*` |
| N06 | P1 | Complete result requesting escalation ignored | fixed | runner holds on `escalate`; disposition recorded; `test_n06_*` |
| N07 | P1 | Verification accepted unsupported success claims | fixed | `VerificationResult`; real-bool verdicts only; API `VerificationEvidence` (422 on empty); simulated evidence refused by live coordinator; `test_n07_*` |
| N08 | P1 | Index latency blocked deterministic detection | fixed | detection emitted before indexing; `INDEX_TIMEOUT_SECONDS`; `test_n08_*` |
| N09 | P2 | Source protection blocked normal symlink uninstall | fixed | `_is_source` distinguishes leaf links from discovery parents; `test_n09_*` |
| N10 | P2 | Malformed model objects labelled as LLM analysis | fixed | required-field check in `_build_insight`; UI badge; `test_n10_*` |
| N11 | P2 | Old WebSocket callbacks corrupted replacement state | fixed (no browser harness yet) | per-effect `gen` ownership, socket-identity guards, handler detach on cleanup, hydration |
| N12 | P2 | Readiness stayed green after the stream task died | fixed | task supervision, `healthy`/`status`/`last_error`, `/api/ready` 503, Docker healthcheck on `/api/ready`; `test_n12_*` |

## Decisions required from the deployment owner (before Phase 1)

Identity provider and claim contract · tenancy and residency rules · operational database ·
paging destination · allowed model routes · budget units · the first permitted action and target.
