# STAG Crews

A **crew** is a ready-made team for one operational use case: roles (agents + skills + least
permissions, with a human approver), a workflow written only in backbone operations, and a scenario
that proves it runs end-to-end. Crews are declarative (`crews/<name>/crew.yaml`) and run on the
TechOps backbone, so every crew inherits dedup, incident lifecycle, bounded investigation, the
action ledger and budget control for free.

```bash
python -m stag.crews list                                  # catalog
python -m stag.crews show techops-incident-triage          # roles + workflow
python -m stag.crews run techops-incident-triage           # halts at the human gate (HELD)
python -m stag.crews run techops-incident-triage --auto-approve   # simulate the approver → RESOLVED
python -m stag.crews run-all --auto-approve                # end-to-end check of every crew
curl -X POST localhost:8000/api/crews/data-ops/run -H 'content-type: application/json' -d '{"auto_approve": true}'
```

## Catalog

| Crew | Domain | Signals it watches | Human gate | Agents |
|---|---|---|---|---|
| [`techops-incident-triage`](techops-incident-triage/) | techops | error rate, latency, error signatures, high-severity alerts | on-call approves restart / rollback | coordinator, investigator, remediation reviewer, recovery verifier |
| [`problem-management`](problem-management/) | techops | recurring incidents, known-error tickets | change owner / CAB approves permanent fix | problem analyst, investigator, verifier |
| [`release-management`](release-management/) | techops | canary regressions, emergency changes, latency deltas | policy ≤ medium, else release manager | release gatekeeper, remediation reviewer, verifier |
| [`ai-automation`](ai-automation/) | techops | automation failure spikes, unknown outcomes, retry storms, cost runaways | policy ≤ medium, else platform owner | automation engineer, investigator, verifier |
| [`customer-care`](customer-care/) | customer-care | ticket surges, CSAT drops, blocked journeys | care lead approves known-issue response | customer communicator, investigator, verifier |
| [`log-management`](log-management/) | techops | error log signatures, volume bursts, collector lag | policy ≤ medium, else platform on-call | log analyst, verifier |
| [`data-ops`](data-ops/) | data | pipeline failures, quality drift, freshness SLA, row-count collapse | dataset owner approves backfill | data steward, investigator, verifier |
| [`security-governance`](security-governance/) | security | sensitive data to unsanctioned AI tools, policy violations (STAG defaults) | CISO delegate approves DLP control | security analyst, investigator, verifier |

## How a crew runs

```
events ─► coordinate (dedup · correlate · material change?) ─► [no] evidence-only update, no model call
                                                            └► [yes] workflow for that incident episode:
   transition ─► investigate (bounded) ─► propose_action (ledger) ─► authorize (human | policy ≤ medium → else HELD)
   ─► dispatch (revalidate staleness) ─► verify (independent; unknown ≠ healthy) ─► resolve ─► notify ─► record (draft)
```

What every crew guarantees, by construction:

- Redelivered events are counted once; repeated symptoms update one episode.
- Only material changes (new episode, severity increase, new signature, recurrence, cooldown) start an investigation.
- Investigations run under an assignment contract (capabilities, call/time limits); automated roles never mutate production.
- Exactly one ledger operation per mitigation path; retries reuse the operation id; stale approvals block dispatch.
- Recovery is verified independently; missing telemetry halts instead of resolving.
- Communications carry incident/operation ids; artifacts are drafts until a human reviews them.

## Build your own

Read [`skills/stag-crew-authoring/SKILL.md`](../skills/stag-crew-authoring/SKILL.md) (or ask your
coding agent for a crew; the skill is discoverable in Claude Code, Codex and Cursor). Copy the
closest crew, change the signal key and triggers, keep the human gate, add a scenario, run
`python -m stag.crews validate && python -m pytest tests/test_crews.py`.

Ideas we would like to see: on-call handoff, change-freeze calendar, FinOps anomaly, SaaS
integration health, access-review, IoT fleet health, fraud-ops.
