# Contributing to STAG

Thanks for helping build Stream-Triggered Augmented Generation. STAG is MIT-licensed and
community-driven; contributions of code, skills, adapters, docs and real-world TechOps feedback are
all welcome.

## Ways to contribute

| Contribution | Start with |
|---|---|
| A new **skill** (TechOps workflow, role, development helper) | `skills/stag-skill-authoring/SKILL.md`, then the *Skill proposal* issue template |
| A **stream adapter** (Kafka variant, OpenTelemetry, SaaS audit log, webhook format) | `skills/stag-stream-adapter/SKILL.md` |
| A **trigger rule** | `skills/stag-trigger-rule-authoring/SKILL.md` |
| A change to the **TechOps backbone** (`stag/techops/`) | *TechOps workflow change* issue template; add rows from `stag-techops-resilience-tests` |
| Knowledge store / retrieval improvements | open a discussion first; include a retrieval-quality measurement |
| Docs, examples, dashboards | just open a PR |

Good first issues are labelled `good first issue`; TechOps-specific ones `techops`.

## Development setup

```bash
git clone https://github.com/mridulaEA/stag.git && cd stag
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests -q                 # core tests run without GPU / ML downloads
ruff check stag tests
python -m stag.skills validate
```

Frontend: `cd frontend && npm install && npm run dev`.

Agents: this repo ships `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/` and the `skills/` pack, so
Codex, Claude Code and Cursor pick up the project conventions automatically. Using an agent to
draft a contribution is fine; you remain responsible for reviewing and testing it.

## Pull request expectations

1. One concern per PR; describe the failure scenario or workflow it addresses.
2. Tests: every rule, adapter, coordinator or ledger change needs a test named after the scenario
   it prevents (`test_lost_response_does_not_repeat_side_effect`). Skills need
   `python -m stag.skills validate` to pass.
3. Docs: update `README.md` configuration tables, `.env.example`, and the relevant skill when
   behaviour changes.
4. Keep the deterministic backbone deterministic: no I/O, no model calls in `stag/techops/`.
5. CI (`.github/workflows/ci.yml`) must be green: tests, ruff, skill validation.
6. Fill in the PR template; link the issue or discussion.

## Design principles reviewers apply

- **Identity before meaning.** Observation, signal, incident and action identities stay separate.
- **Deterministic backbone, bounded agents.** Agents choose what to investigate, never what they
  may access or change. State changes, authorization and dispatch are explicit and recorded.
- **Continuous monitoring ≠ continuous LLM calls.** Repeated evidence updates counts; only
  material change triggers investigation.
- **Verified outcomes.** A command succeeding, a transcript saying "done", or missing telemetry is
  not recovery.
- **Untrusted input.** Telemetry and retrieved content never carry instruction authority.

## Releasing skills

Skill versions live in each `SKILL.md` `metadata.version`; the plugin version lives in
`.claude-plugin/plugin.json`. Bump the plugin version when any skill changes behaviour so Claude
Code users receive the update; Codex/Cursor users re-run the installer.

## Community

Questions and proposals go to GitHub Discussions (see `docs/community/README.md`). Be kind; the
`CODE_OF_CONDUCT.md` applies everywhere the project meets.
