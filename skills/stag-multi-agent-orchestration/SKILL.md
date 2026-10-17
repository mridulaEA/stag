---
name: stag-multi-agent-orchestration
description: Design or run STAG's multi-agent incident workflow — coordinator, investigator, remediation reviewer and recovery verifier — with handoff schemas, separate permissions, termination rules and conflict resolution, built on the deterministic backbone (incident state machine, assignment contracts, action ledger). Use when splitting work across agents or subagents (Claude Code agents, Codex, Cursor), when asked "should this be multi-agent?", or when agents disagree. Starts from a single-agent baseline; adds agents only where parallelism, separate permissions or independent expertise is measurable.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG Multi-Agent Orchestration

**STAG is not an enterprise-wide agent with guardrails. It is an operations system that grants
agents bounded assignments.** Agents supply investigative flexibility. The architecture supplies
continuity (incident state), authority (authorization, ledger) and accounting (budget).

Before adding agents: establish a single-agent baseline on the same tasks, tools and budget.
Split only where parallelism, separate permissions or independent expertise gives a measurable
benefit. Role names that sound like an org chart (planner, manager, reviewer) are not a reason.

## Roles and their permissions

| Role | May do | May NOT do | Backbone object |
|---|---|---|---|
| **Coordinator** (deterministic code first, agent assist second) | dedup, correlate, open/attach incidents, decide material change, create `InvestigationRequest`, record transitions | call the LLM for every event; change state on an agent's say-so | `IncidentCoordinator` |
| **Investigator** | read-only diagnostics within an `InvestigationAssignment`; produce `InvestigationFindings` | mutate anything; expand its own capabilities; decide the incident is resolved | `BoundedRun` |
| **Remediation reviewer** | turn a proposal into one `PREPARED` operation; check preconditions, policy version, staleness; present evidence to the approver | authorize or dispatch; mint a new op id for a retry | `ActionLedger.prepare/revalidate` |
| **Recovery verifier** | read independent health signals over a window; record `verify()` | pick or run the mitigation; declare RESOLVED without the coordinator | `ActionLedger.verify` |
| **Human approver / on-call** | `authorize`, `reject`, final `RESOLVED` | — | API `/techops/actions/*/authorize`, `/incidents/*/transition` |

Permission to investigate ≠ permission to recommend ≠ permission to commit. Enforce it in the
executor (capabilities, ledger, API), never only in prompts.

## Handoff schemas (shared state lives in the backbone, not in chat)

```yaml
# Coordinator → Investigator
investigation_request:
  id: INV-…
  incident_id: INC-…
  evidence_snapshot_version: 17
  reason: severity_increase
  assignment: <InvestigationAssignment yaml>        # scope, capabilities, limits, required output

# Investigator → Reviewer (findings are versioned against the snapshot)
investigation_findings:
  evidence_snapshot: incident-evidence-v17
  observed_facts_with_references: [...]
  hypotheses: [...]
  contradictory_or_missing_evidence: [...]
  proposed_next_step: "propose restart_component(checkout-api, replicas=2) per RB-17"
  partial: false

# Reviewer → Approver
action_proposal:
  operation_id: OP-…           # deterministic; same step → same id
  plan_step / target / parameters / policy_version / preconditions
  evidence_for: [...]          # refs
  evidence_against: [...]
  incident_version_at_proposal: 17

# Executor → Verifier
dispatch_record: {operation_id, dispatched_at, attempts, outcome: ok|unknown}

# Verifier → Coordinator
verification: {operation_id, verdict: healthy|not_recovered|unknown, window, signals: [...]}
```

Every message carries `incident_id`, `incident_version`/`evidence_snapshot`, and (for actions)
`operation_id`. If a downstream role receives a message whose version is behind the incident's
current version, it **re-reads the incident** before acting.

## Orchestration loop (one controlled workflow per incident episode)

```text
event+trigger → Coordinator.ingest
  ├─ duplicate / evidence-only → update counts; done (no agent)
  └─ material change → InvestigationRequest
        → Investigator (bounded) → findings (versioned)
        → Coordinator: INVESTIGATING → ACTION_PROPOSED (or back to TRIAGED / DISMISSED)
        → Reviewer: ledger.prepare → PREPARED
        → Human/policy: authorize → AUTHORIZED   (HELD / REJECTED otherwise)
        → Executor: revalidate → dispatch → (unknown → reconcile)
        → Verifier: verify over window → Coordinator: VERIFYING → RESOLVED | INVESTIGATING again
```

New signals during the episode **update this workflow**; they do not spawn unrelated agents.

## Termination and conflict rules

- **Termination**: an investigation ends when `BoundedRun` is exhausted, findings are complete,
  or the incident version it was given is superseded. Partial findings are returned, never
  silently discarded, never "one more call".
- **Conflicting proposals** (two investigators propose different mitigations): both become
  `PREPARED` records; the incident sits in `ACTION_PROPOSED`; the approver picks at most one;
  the other is `REJECTED` with a reason. No concurrent dispatch for the same incident/target.
- **Disagreement on cause**: record both hypotheses with evidence for/against; choose the next
  *safe check* that discriminates, not the more confident narrative.
- **Shared-platform incidents**: keep local incidents (own impact, owner, recovery) and link a
  parent; the platform team owns the platform mitigation; domain recovery is not complete because
  the parent closed.
- **Budget**: each role reserves from the incident's `BudgetLedger` before running; exhaustion
  degrades to approved cheaper route → deterministic diagnostics → partial findings → human.
- **Model outage**: coordinator, paging and ledger keep working; agents report `unavailable`.

## Mapping to tools

- **Claude Code**: subagent definitions in `agents/` (`stag-incident-coordinator`,
  `stag-investigator`, `stag-remediation-reviewer`, `stag-recovery-verifier`). Each lists only the
  tools its role needs. The main session acts as orchestrator and holds approvals.
- **Codex / Cursor**: one agent per role prompt; use the handoff schemas above as the message
  format and the STAG API (`/api/techops/*`) as shared state. Skills are discovered from
  `.agents/skills`.
- **Any runtime**: shared state is the backbone (incidents, requests, ledger), not the
  transcript. A durable workflow engine (e.g. Temporal-style) may host the outer loop; LLM calls,
  DB and external APIs go in activities with their own retry and side-effect controls.

## Evaluate the decomposition

Compare single-agent vs multi-agent on the same tasks: grounding quality, false merges/splits,
duplicate side effects (must be 0), time to useful triage, cost per correct completed outcome.
If multi-agent does not win on a measured dimension, remove agents.

## Related files

- `agents/*.md` — Claude Code subagent definitions
- `skills/stag-techops-*` — per-role procedures
- `stag/techops/` — the backbone the roles share
