---
name: stag-ai-automation-governance
description: Govern production automations (runbook bots, agents, scheduled remediations) through STAG — detect failure spikes, retry storms, unknown outcomes and cost runaways per automation; pause safely as one approved operation; reconcile unknown outcomes from target state; measure cost per correct completed outcome; and draft automation reviews. Use when automations misbehave, when deciding what to automate next, or when asked "is this agent safe to leave running?".
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG: AI Automation Governance

Automations get the same backbone as incidents: counted, bounded, approved, verified. Determinism
is not correctness — an automation that is wrong consistently is still wrong, so measure outcomes,
not runs.

## Signals (per `automation_id`)

| Signal | Rule | Meaning |
|---|---|---|
| failure rate > 20% | `numeric_threshold` on `failure_rate` | the automation's assumptions broke |
| outcome `unknown` | `pattern` on `outcome` | side effect may or may not have happened → reconcile |
| > 5 runs / hour | `frequency` grouped by `automation_id` | retry storm; check idempotency |
| cost > budget per run | `numeric_threshold` on `cost_usd` | runaway loop or oversized context |

## Procedure

1. Triage the episode per automation; link to the incident it was trying to fix.
2. Bounded investigation (`read_metrics`, `read_incident_history`, `read_dependency_graph`,
   `query_scoped_logs`): what changed in the target, in the automation's inputs, or in policy?
3. Reconcile every `unknown` outcome from the target's state (`ActionLedger.reconcile`); never
   count it as free or as failed.
4. Prepare `pause_automation` (safe stop, drain in-flight) with preconditions
   `no_in_flight_mutations_lost`, `owner_notified`. Policy may approve ≤ medium; the platform
   owner approves above.
5. Verify: no new runs, in-flight work drained, dependent incident not worsened.
6. Automation review (draft): root cause, candidate fixes, guardrail changes (limits, capability
   scope, budget), and whether this step should stay automated, become AI-assisted, or return to
   a human. Fully loaded cost across all attempts ÷ verified correct completions.

## Choosing what to automate (candidate discovery)

From the action ledger: plan steps executed often, with high verified-success rate, low parameter
variance and a reliable idempotent target → deterministic automation candidates. Steps whose
evidence varies → AI-assisted proposals, human commit. Steps with irreversible side effects stay human.

## Guardrails

- An automation never pauses, resumes or re-scopes itself; the ledger holds the operation.
- Budget exhaustion degrades to deterministic diagnostics, not to more model calls.
- Review drafts change nothing until a human promotes them.

Crew: `crews/ai-automation`. Agent: `agents/stag-automation-engineer.md`.
