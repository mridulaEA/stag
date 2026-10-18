# Problem Management Crew

**Use when** incidents recur on the same service or workarounds keep being re-applied. The crew
feeds an ITIL-style problem process from STAG incident history.

```
incident/ticket stream ─► coordinate ─► open_problem ─► root_cause_analysis (bounded)
  ─► known_error (draft) ─► propose_permanent_fix ─► cab_approval (human) ─► raise_change
  ─► verify_no_recurrence ─► close_problem ─► problem_review (draft)
```

Roles: `stag-problem-analyst`, `stag-investigator` (read-only), human change owner / CAB,
`stag-recovery-verifier`. Skills: `stag-problem-management`, `stag-techops-bounded-investigation`,
`stag-release-gate`, `stag-techops-recovery-verification`.

```bash
python -m stag.crews run problem-management --auto-approve
```
