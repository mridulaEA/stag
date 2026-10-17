# STAG Skill Pack

Agent skills for STAG in the open [Agent Skills](https://agentskills.io) format: one `SKILL.md`
per folder, usable unchanged from **Claude Code**, **OpenAI Codex** and **Cursor**.

## Use them from anywhere

| Runtime | One-time setup | Invoke |
|---|---|---|
| Claude Code (plugin) | `claude plugin marketplace add mridulaEA/stag` → `claude plugin install stag-techops@stag-skills` | `/stag-techops-incident-triage …` or let Claude pick |
| Claude Code (user skills) | `python -m stag.skills install --target claude` → `~/.claude/skills/` | same |
| Codex CLI / IDE / ChatGPT | `python -m stag.skills install --target codex` → `~/.agents/skills/` | `$stag-techops-…`, `/skills` |
| Cursor | `python -m stag.skills install --target cursor` → `~/.cursor/skills/` | `/stag-techops-…` in Agent chat |
| Any clone of this repo | nothing: `.claude/skills` and `.agents/skills` link to `skills/` | project skills auto-discovered |
| No Python environment | `curl -fsSL https://raw.githubusercontent.com/mridulaEA/stag/main/scripts/install-skills.sh \| bash` | |

`python -m stag.skills list` prints the catalog; `python -m stag.skills validate` checks it.

## Catalog

### TechOps incident workflow (start here)

| Skill | Use when | Produces |
|---|---|---|
| `stag-techops-incident-triage` | a trigger fired / an alert paged / "is this real, how bad, who owns it?" | triage record; `DETECTED → TRIAGED` |
| `stag-techops-dedup-correlation` | counts look inflated, tickets multiply, "same alert keeps firing" | classification: redelivery / repeated symptom / new episode |
| `stag-techops-bounded-investigation` | an investigation request exists; "why is this happening?" | facts with refs, hypotheses, gaps, next safe check |
| `stag-techops-remediation-ledger` | a mitigation is proposed; "restart/rollback X"; lost response on an action | prepared/authorized/reconciled operation record |
| `stag-techops-recovery-verification` | after dispatch; "looks fixed, close it?" | verdict healthy / not_recovered / unknown |
| `stag-techops-postmortem` | after RESOLVED; incident review | blameless draft + metrics (pending review) |

### TechOps platform

| Skill | Use when |
|---|---|
| `stag-techops-signal-tuning` | noisy/flapping alerts, investigation cadence, debounce/cooldown/hysteresis/inhibition, SLO burn rate |
| `stag-techops-resilience-tests` | adding a source, rule, action tool or coordinator change; "is this production-ready?" |
| `stag-techops-enterprise-onboarding` | connecting a new app/SaaS; identity, normalization envelope, classification, eligible routes, budgets, cells |

### Multi-agent

| Skill | Use when |
|---|---|
| `stag-multi-agent-orchestration` | splitting incident work across agents (coordinator, investigator, remediation reviewer, recovery verifier); handoff schemas; conflict rules. Claude Code subagents in [`../agents/`](../agents). |

### STAG development

| Skill | Use when |
|---|---|
| `stag-trigger-rule-authoring` | adding/changing a trigger rule in `stag/triggers/rules.py` |
| `stag-stream-adapter` | building a new stream source with stable identity, replay and drift handling |
| `stag-skill-authoring` | contributing a new skill to this pack |

## Where the workflows come from

The TechOps skills implement the controls from the STAG article series — *STAG in Production:
When One Failure Becomes a Thousand AI Triggers* and *Enterprise STAG: Deterministic Operations,
Bounded Agentic Investigation* — and are backed by code in [`../stag/techops/`](../stag/techops):
identity, incident state machine, coordinator, assignment contract, action ledger, budget ledger.
Design summary: [`../docs/techops/README.md`](../docs/techops/README.md).

## Contributing a skill

Read `stag-skill-authoring/SKILL.md`, then open a PR using the "Skill proposal" issue template
first if the scope is new. Folder name must equal `name`; `python -m stag.skills validate` must pass.
