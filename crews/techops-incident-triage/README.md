# TechOps Incident Triage Crew

**Use when** production telemetry (alerts, metrics, logs) flows into STAG and you want proactive,
deduplicated incident response where humans keep the commit permission.

```
alerts/metrics ─► coordinate ─► triage ─► page ─► investigate ─► propose_restart ─► authorize (human)
   ─► dispatch ─► verify ──healthy──► resolve ─► status_update ─► postmortem (draft)
                      └─not recovered─► propose_rollback ─► authorize ─► dispatch ─► verify_rollback ─► resolve
```

| Role | Kind | Skills |
|---|---|---|
| coordinator | agent `stag-incident-coordinator` | dedup-correlation, incident-triage, signal-tuning |
| investigator | agent `stag-investigator` (read-only capabilities) | bounded-investigation |
| remediation_reviewer | agent `stag-remediation-reviewer` | remediation-ledger |
| recovery_verifier | agent `stag-recovery-verifier` | recovery-verification |
| on_call | **human** | approves exactly one plan step at a known incident version |

Run it end-to-end (no LLM needed; the deterministic executor fills in content):

```bash
python -m stag.crews run techops-incident-triage --auto-approve      # simulate the human approver
python -m stag.crews run techops-incident-triage                     # halts at 'authorize' → HELD
```

The bundled scenario shows: 3 redeliveries counted once, 7 distinct observations attached to one
episode with a single investigation, a severity increase that re-investigates, and a second
service whose restart does not recover and falls through to the rollback path.
