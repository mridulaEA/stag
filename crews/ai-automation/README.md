# AI Automation Governance Crew

**Use when** scripted or AI-driven automations run in production and need the same discipline as
incidents: counted, bounded, approved, verified.

```
automation_run stream ─► coordinate ─► triage ─► investigate ─► propose_pause
  ─► authorize (policy ≤ medium, else platform owner) ─► dispatch ─► verify ─► resolve ─► review (draft) ─► notify_owner
```

Signals: failure-rate spikes, unknown outcomes, retry storms, cost runaways (`payload.automation_id`
is the service key). Skills: `stag-ai-automation-governance`, `stag-techops-bounded-investigation`.

```bash
python -m stag.crews run ai-automation --auto-approve
```
