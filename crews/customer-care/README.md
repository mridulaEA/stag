# Customer Care Response Crew

**Use when** support tickets, chat and feedback stream into STAG and a wave of customers should be
answered once, correctly, with proactive communication.

```
tickets/feedback ─► coordinate ─► triage ─► impact_assessment ─► proactive_update (status page)
  ─► propose_known_issue_response ─► authorize (care lead) ─► dispatch ─► verify (ticket rate, CSAT)
  ─► resolve ─► followup ─► care_review (draft)
```

Signals: ticket surges per product area, CSAT drops, blocked critical journeys. Skills:
`stag-customer-care-comms`, `stag-techops-bounded-investigation`, `stag-techops-recovery-verification`.

```bash
python -m stag.crews run customer-care --auto-approve
```
