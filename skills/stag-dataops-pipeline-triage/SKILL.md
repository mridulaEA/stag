---
name: stag-dataops-pipeline-triage
description: Triage and remediate data incidents with STAG — pipeline failures, data-quality drift, freshness SLA breaches and row-count collapses correlated per dataset; lineage-aware bounded investigation; idempotent backfill or quarantine as one approved operation; freshness and quality verification; single consumer notification. Use for DataOps/analytics engineering on Airflow, Dagster, dbt, Great Expectations, Soda or warehouse freshness monitors.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: data
---

# STAG: DataOps Pipeline Triage

The dataset is the service. Partitions are the blast radius. Backfills are side effects.

## Signals

| Signal | Rule |
|---|---|
| run status failed / error / upstream_failed | `pattern` on `status` |
| null ratio > 5% on a key column | `numeric_threshold` on `null_ratio` |
| freshness lag > SLA | `numeric_threshold` on `freshness_lag_minutes` |
| row count down > 40% vs expected | `numeric_threshold` on `row_count_delta_pct` (op `lt`) |

## Procedure

1. **Triage per dataset** (`metadata.dataset`): affected partitions, SLA, downstream consumers
   from lineage; two retries of the same run are repeated observations, not two incidents.
2. **Lineage investigation** (bounded, read-only): `read_dependency_graph` (upstream health,
   schema changes), `inspect_deployment_metadata` (dbt model / DAG version), `read_metrics`
   (volumes), `read_incident_history`, `query_scoped_logs` (task logs). Quality drift is evidence,
   not proof — a null spike can be a legitimate upstream change.
3. **Backfill as one operation**: `backfill_partition` with `mode: idempotent_upsert`,
   `quarantine_bad_rows: true`, preconditions `upstream_healthy`, `partition_identified`,
   `idempotent_write_path`. Dataset owner (human) approves. A retried backfill reuses the
   operation id; a lost response is reconciled from the warehouse state.
4. **Verify**: freshness within SLA, quality checks green on the backfilled partition, row counts
   within expected range. Missing metrics = unknown.
5. **Notify consumers once** with incident and operation ids; **review draft** with cost of delay,
   cause, fix, follow-ups (tests, contracts, alerts).

## Guardrails

- Never re-run a pipeline blindly; prepare, approve, dispatch, verify.
- Sensitive column values stay out of evidence; cite partitions, metrics and check names.
- Don't resolve on "run succeeded"; resolve on verified freshness and quality.

Crew: `crews/data-ops`. Agent: `agents/stag-data-steward.md`.
