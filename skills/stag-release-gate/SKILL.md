---
name: stag-release-gate
description: Gate releases and changes with live STAG evidence — change-freeze windows, open incidents on the service and its dependencies, canary health, emergency/hotfix flags — and prepare a policy-bounded rollback as one retry-safe operation with a recorded release decision. Use when deployment or canary signals arrive, before a CAB/change approval, or when asked "is it safe to ship/roll back?". Not for diagnosing why the canary failed (use stag-techops-bounded-investigation).
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG: Release Gate

A release decision is evidence plus policy, recorded. The agent assembles the evidence and prepares
the proposal; policy or a human commits.

## Gate checklist (read-only, bounded)

| Check | Capability | Blocks when |
|---|---|---|
| Change-freeze window for the service / region | `read_incident_history` (freeze calendar) | inside freeze and change is not emergency-approved |
| Open incidents on the service or its dependencies | `read_incident_history`, `read_dependency_graph` | any open high/critical incident touches the path |
| Canary health vs baseline | `read_metrics`, `read_service_health` | `canary_error_rate` or latency delta beyond threshold |
| Previous stable version available | `inspect_deployment_metadata` | rollback target missing |
| Emergency / hotfix flag | payload `change_type` | always needs a human |

## Procedure

1. On a deployment/canary signal, `transition → TRIAGED` with the gate verdict in the reason.
2. Run the gate checklist as a bounded investigation (`release-gate` budget profile).
3. If a regression is confirmed, prepare `rollback_deployment` (target service, `to:
   previous_stable`, `scope: canary_first`) with preconditions `canary_regression_confirmed`,
   `previous_version_available`.
4. Authorization: `policy` with `auto_if_severity_at_most: medium` for canary rollbacks; high /
   critical or any emergency change waits for the release manager. Never adjust severity to pass.
5. Revalidate before dispatch (the incident may have moved), dispatch, then verify canary health
   and error budget over the window.
6. Record the **release decision** (ship / hold / rollback) with the evidence refs; announce once.

## What a rollback is not

A code rollback is not a business rollback: payments taken, emails sent, migrations applied need
compensating actions as separate ledger operations. Extend the release manifest beyond code
(model id, prompt versions, retrieval config, feature flags) so "what changed" is answerable.

## KPIs

canary regressions caught before full rollout · rollback time to verified · policy vs human
approvals · freeze violations attempted.

Crew: `crews/release-management`. Agent: `agents/stag-release-gatekeeper.md`.
