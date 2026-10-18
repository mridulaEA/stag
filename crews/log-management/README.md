# Log Management Crew

**Use when** structured logs stream into STAG and you want anomalies surfaced without a model call
per line or an embedding per message.

```
logs/metrics ─► coordinate (templates, counts, examples) ─► triage ─► investigate (scoped queries)
  ─► propose_sampling (TTL, errors preserved) ─► authorize (policy ≤ medium) ─► dispatch ─► verify
  ─► resolve ─► evidence_package (draft) ─► notify
```

Skills: `stag-log-analysis`, `stag-techops-dedup-correlation`, `stag-techops-signal-tuning`.

```bash
python -m stag.crews run log-management --auto-approve
```
