# DataOps Reliability Crew

**Use when** orchestrator runs, data-quality checks and freshness metrics stream into STAG and data
owners want fewer, better-handled data incidents.

```
pipeline/quality/freshness ─► coordinate ─► triage ─► lineage_investigation ─► propose_backfill (idempotent)
  ─► authorize (dataset owner) ─► dispatch ─► verify (freshness + quality) ─► resolve ─► notify_consumers ─► review (draft)
```

Skills: `stag-dataops-pipeline-triage`, `stag-techops-bounded-investigation`, `stag-techops-recovery-verification`.

```bash
python -m stag.crews run data-ops --auto-approve
```
