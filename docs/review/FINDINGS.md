# Review findings ledger

Source: *STAG: Complete Engineering Review and Claude Implementation Handoff* (October 18, 2026),
32 findings against PR #1 at `16e70b2`. This ledger tracks each finding's status on the branch.
Status values: **fixed** (repaired with a regression test on this branch), **partial** (library or
simulation path repaired; production integration still a gap), **planned** (needs an owner
decision or an integration that does not exist yet; see phase).

Review decision we are honouring: STAG remains a **local development and simulation project**. No
production mutation tools or sensitive multi-tenant data should be connected until the Phase 1–3
items below are implemented.

| ID | P | Finding | Status | Where / test | Notes |
|---|---|---|---|---|---|
| R01 | P1 | Authenticate every API and WebSocket caller | planned (Phase 1) | — | Needs the owner's identity provider and claim contract. Health now reports `mode: local-demo`; compose binds to loopback; README states the API is unauthenticated. |
| R02 | P1 | Resolve approval freshness from server state | fixed | `stag/api/routes.py` authorize/dispatch; `tests/test_techops_api.py::test_action_ledger_flow_over_api` | Approval binds to the server's current incident version; dispatch revalidates from server state with no caller version; `expected_incident_version` is a consistency check only. Operations require an existing open incident. |
| R03 | P1 | Do not retry an unknown action outcome blindly | fixed | `ActionLedger` `RETRY_READY`, `RetryBudgetExceeded`; `TestActionLedger` | OUTCOME_UNKNOWN/RECONCILING are not dispatchable; only a destination check proving absence yields RETRY_READY; retry budget enforced; same operation id. |
| R04 | P1 | Separate operation application from verified recovery | fixed | `ActionState.APPLIED`; `verify` validates before writing; reserved fields protected; `RESOLVED` only from `VERIFYING` | Reconciliation never produces VERIFIED. |
| R05 | P1 | Bind immutable operation terms and enforce preconditions | partial | operation id covers policy version + preconditions (`terms_hash`) | Live precondition evaluation, conflict locks, approval expiry and destination adapters: Phase 3. |
| R06 | P1 | Make crew verification fail closed | fixed | `Verdict` enum; runner defaults to UNKNOWN; `test_review_regressions.py` | Missing/misspelled/garbage verdicts never resolve; fixture evidence is labelled `simulated`. |
| R07 | P1 | Enforce crew policy limits in the schema and runtime | fixed | `stag/crews/schema.py` validators; runtime ceiling check | Policy ceiling ≤ medium, unique roles, human approver required, automated roles cannot mutate, gate ordering enforced. |
| R08 | P1 | Enforce scope and capability grants outside the executor | partial | `BoundedRun` snapshot, catalog check, scope check on parameters; steps cannot widen roles | A trusted tool/model gateway with credentials unavailable to executors: Phase 2. |
| R09 | P1 | Honor partial findings and escalation in crew control flow | fixed | runner holds the incident (`INVESTIGATING → HELD`) on partial findings | No proposal or dispatch from incomplete evidence. |
| R10 | P1 | Repair budget arithmetic and account for actual usage | partial | `BudgetLedger` validates finite non-negative amounts; settled refs cannot be reused; runner settles in `finally` | Real token/cost accounting through a gateway and atomic multi-worker enforcement: Phase 2. |
| R11 | P1 | Deduplicate before indexing and stateful trigger evaluation | fixed | `IncidentCoordinator.register/correlate`; orchestrator + runner order; `test_duplicates_are_not_indexed_or_counted_by_stateful_rules` | Non-triggering observations are registered too. |
| R12 | P1 | Use canonical identities and hash the meaningful envelope | fixed | `identity.py` JSON keys, `envelope_hash`, `StreamEvent.doc_key` in all stores | Existing on-disk indexes keyed by bare event id need a rebuild (documented in STATUS). |
| R13 | P1 | Separate replay and event-time recovery from live processing | partial | `is_replay` isolation; event-time recurrence; late evidence path; tz-aware timestamps | Watermarks, clock-skew policy and isolated replay outputs across adapters: Phase 2. |
| R14 | P1 | Persist control state with transactions and worker coordination | planned (Phase 1) | — | Needs the owner's operational database choice. Docstrings no longer claim durability. |
| R15 | P1 | Keep deterministic detection independent of enrichment | partial | orchestrator emits `trigger` before retrieval/generation; indexing/retrieval/generation failures isolated; WebSocket fan-out concurrent with timeout | Bounded work queues and durable admission: Phase 2. |
| R16 | P1 | Give investigation requests a real work lifecycle | planned (Phase 1) | — | `mark_investigated` documented as *request* time. |
| R17 | P1 | Align Kafka acknowledgment with durable processing | planned (Phase 2) | — | Needs a broker in tests. |
| R18 | P1 | Enforce tenant, classification and model-egress policy | planned (Phase 1–2) | — | Skill describes the design; no runtime claims it is enforced. |
| R19 | P2 | Own background tasks and bound blocking work | partial | idempotent start/stop with owned task; crew runs via `asyncio.to_thread` | Embedding offload and bounded concurrency: Phase 2. |
| R20 | P2 | Correct retrieval identity, relevance and query construction | partial | source-scoped doc keys; query builder keeps service/message/error identifiers | Sparse relevance floor, RRF score handling, self-retrieval exclusion: Phase 2. |
| R21 | P2 | Make knowledge persistence consistent and bounded | planned (Phase 2) | — | |
| R22 | P2 | Validate model output and expose degraded generation | fixed | `InsightGenerator._build_insight`; `generation_mode`/`degraded_reason`; `tests/test_generator_validation.py` | |
| R23 | P2 | Keep crew simulation distinct from a resumable live workflow | partial | reports carry `mode: simulation`; impossible live-resume text removed; API endpoint documented as simulation | Durable run ids and resumable steps: Phase 3. |
| R24 | P2 | Validate workflow control flow and fail CI on failed runs | fixed | trigger construction validated; limits bounded; gate ordering; step budget; CLI exits 1 on failed steps/unmet expectations; CI runs `run-all` | |
| R25 | P2 | Bound and validate public input at admission | partial | typed scenario enums, repeat/event caps, query ranges, 422 on invalid scenario | Rate limits and job admission: Phase 4. |
| R26 | P2 | Repair WebSocket deployment, teardown and incident visibility | partial | same-origin ws URL, disposed guard, backoff, `trigger`/`incident_update` handling, source-scoped keys, visible error, version footer | History hydration and accessibility pass: Phase 4. |
| R27 | P1 | Prevent skill installers from deleting their own source | fixed | `_is_source` guard; copy mode for ephemeral clones; installer tests | |
| R28 | P2 | Package crews, skills and required assets in installed distributions | fixed | `setup.py` build hook, `stag/_resources.py`, `scripts/check-packaging.sh`, CI `packaging` job; validators fail on empty catalogs | |
| R29 | P2 | Repair and test the container deployment path | partial | multi-stage Dockerfile, catalogs copied, non-root, no baked `.env`, `.dockerignore`, loopback compose binding | Docker build not exercised in this environment; CI container smoke: Phase 4. |
| R30 | P2 | Close dependency and test-coverage gaps | partial | +40 tests incl. regression probes, installed-package job | npm advisory triage and lock strategy: Phase 4. |
| R31 | P2 | Align observability and documentation with implemented behavior | partial | `docs/STATUS.md` capability matrix; README corrections; health readiness; "model calls avoided" relabelled | Measured cost metrics: Phase 2. |
| R32 | P2 | Do not use the winning rule as the sole incident identity | planned (Phase 4) | crews may group by service only (`symptom_from_rule: false`) | Correlation redesign needs reviewed cases. |

## Decisions required from the deployment owner (before Phase 1)

Identity provider and claim contract · tenancy and residency rules · operational database ·
paging destination · allowed model routes · budget units · the first permitted action and target.
