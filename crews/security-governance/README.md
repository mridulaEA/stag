# Shadow AI & Data Governance Crew

STAG's original demo scenario (sensitive data to unsanctioned AI tools) expressed as a crew. Uses
the default trigger rules and groups cases by **tool × department × rule**, so a wave of DLP alerts
becomes one governance case with the users as graph entities.

```
dlp/proxy/idp ─► coordinate ─► triage ─► exposure_investigation ─► propose_control (DLP block on tool + data types)
  ─► authorize (CISO delegate) ─► dispatch ─► verify ─► resolve ─► notify_governance ─► governance_case (draft)
```

Skills: `stag-security-governance`, `stag-techops-dedup-correlation`, `stag-techops-incident-triage`,
`stag-techops-bounded-investigation`, `stag-techops-recovery-verification`.

```bash
python -m stag.crews run security-governance --auto-approve
STREAM_SOURCE=simulator TECHOPS_COORDINATOR_ENABLED=true python -m stag.main   # live version
```
