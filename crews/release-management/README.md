# Release Management Crew

**Use when** CI/CD and post-deploy metrics flow into STAG and canary regressions, change-freeze
conflicts and emergency changes should be handled the same way every time.

```
deploy/metric stream ─► coordinate ─► gate_triage ─► freeze_and_canary_check (bounded)
  ─► propose_rollback ─► authorize (policy ≤ medium, else release manager) ─► dispatch ─► verify
  ─► close ─► release_note (draft) ─► announce
```

The bundled scenario has one medium canary regression (policy-approved rollback) and one critical
emergency change (held for the release manager unless `--auto-approve`).

```bash
python -m stag.crews run release-management                 # critical one halts at authorize
python -m stag.crews run release-management --auto-approve
```
