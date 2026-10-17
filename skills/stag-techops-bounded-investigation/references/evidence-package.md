# Evidence package template

Assemble this *before* the first model call. Everything below is read-only.

```yaml
incident_id: INC-4821
evidence_snapshot: incident-evidence-v17
scope:
  services: [checkout-api, identity-adapter]
  environment: production
  window: 2026-10-17T13:55Z .. 2026-10-17T14:10Z

live_operational_evidence:
  - ref: metric:db.pool.wait_ms      # exact identifiers stay searchable
    summary: p99 wait 40ms → 3600ms at 14:01Z
  - ref: trace:7f3a…                 # representative, not every trace
    summary: checkout → pool acquire timeout 5s
  - ref: deploy:checkout-api@v42
    summary: rolled out 13:58Z by pipeline run 9912
  - ref: health:checkout-api
    summary: 3/6 instances unhealthy

approved_operational_knowledge:
  - ref: runbook:RB-17 (v3, applies_to: checkout-api, condition: pool exhaustion)
  - ref: owner:team-checkout (on-call route pd-checkout)
  - ref: architecture:checkout-depends-on:[postgres-main, identity-adapter]

historical_incident_knowledge:
  - ref: incident:INC-3310 (reviewed) — similar symptom, cause: pool size regression
    note: similarity is a clue, not proof

missing:
  - config diff for v42 (not retrieved yet)
  - identity-adapter error budget status
```

Rules:
- Preserve query scope and timestamps on every ref.
- Keep raw telemetry in its operational store; cite it, do not paste it wholesale.
- Filter knowledge by applicability, version and access before it reaches the model.
- Mark anything inferred as `inferred:` so it cannot be mistaken for an observation.
