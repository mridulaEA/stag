# Crews: design notes

## Why crews, not agents

Agent frameworks make it easy to spawn roles that sound like an org chart (planner, researcher,
manager). STAG crews start from the opposite end: the **backbone** decides what may happen
(identity, state, authorization, idempotency, verification, budget) and a crew only arranges roles
and steps on top of it. That is why a crew file cannot express an unsafe workflow — the schema has
no step for "agent changes production directly".

## Anatomy

| Part | Where | Enforced by |
|---|---|---|
| Roles | `crew.yaml: roles[]` → `agents/*.md`, `skills/*` | runner builds `InvestigationAssignment` from role capabilities; `human: true` roles are the only valid approvers |
| Triggers | `crew.yaml: triggers[]` | `RuleRegistry.from_specs` (deterministic rules) |
| Episode identity | `crew.yaml: signal_key` | `IncidentCoordinator` with a crew-specific `SignalKey` function |
| Workflow | `crew.yaml: workflow[]` | `CrewRunner._execute` maps each action to a backbone call and records implicit lifecycle transitions |
| Content | `RoleExecutor` | `DeterministicExecutor` (templates, offline) or your LLM-backed executor |
| Proof | `scenario.yaml` | `tests/test_crews.py` runs every crew with and without approval |

## Executors

The runner owns control flow; an executor only supplies content for `investigate`, `verify`,
`notify` and `record`. Implement `RoleExecutor` to plug in an LLM (via the STAG generator, Claude
Code subagents, Codex or Cursor agents) — the limits, permissions and ledger still apply because
they are enforced by `BoundedRun` and `ActionLedger`, not by the executor.

```python
from stag.crews import run_crew, load_crew, load_scenario
from stag.crews.runner import DeterministicExecutor

class MyExecutor(DeterministicExecutor):
    def investigate(self, crew, role, incident, run, evidence):
        run.use_capability("read_service_health", service=incident.attributes["service"])  # enforced
        run.record_model_call()                                                                # counted
        ...  # call your model, return InvestigationFindings with evidence refs
        return super().investigate(crew, role, incident, run, evidence)

crew = load_crew("techops-incident-triage")
report = run_crew(crew, load_scenario(crew), auto_approve=False, executor=MyExecutor())
print(report.to_markdown())
```

## Simulation vs production

Everything the runner does today is a **simulation** (`CrewRunReport.mode == "simulation"`):
verdicts come from scenario fixtures labelled `simulated: true`, actions change ledger records
only, approvals are flags, and a held run cannot be resumed through the live API (each API run
uses its own isolated coordinator and ledger). The eight crews are scenario templates.

A production runtime needs, in order: authenticated principals and resource authorization; a
transactional store for observations, incidents, requests and operations with an outbox; a durable
investigation and run lifecycle with resumable steps; a trusted tool/model gateway; destination
adapters with live preconditions and reconciliation; independent recovery probes. See
`docs/review/FINDINGS.md` (phases 1–3). Until then, do not connect real remediation tools.

## Mapping to Claude Code, Codex, Cursor

- Claude Code: each role's `agent` is a subagent in `agents/`; the main session orchestrates and
  holds approvals (`skills/stag-multi-agent-orchestration`).
- Codex / Cursor: one agent per role prompt using the same skills from `.agents/skills`; shared
  state is the STAG API, not the transcript.
- Any runtime: `skills/stag-crew-authoring` tells the agent how to author and check a new crew.
