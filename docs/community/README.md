# STAG Developer Community

STAG is built in the open for people who run AI on live data streams: TechOps / SRE engineers,
platform teams, RAG and agent builders, and enterprise architects. This page is the map.

## Where things happen

| Channel | Use it for |
|---|---|
| [GitHub Discussions](https://github.com/mridulaEA/stag/discussions) | questions, "show and tell" (your STAG deployment, skills, dashboards), RFCs, TechOps workflow debates |
| [Issues](https://github.com/mridulaEA/stag/issues) | bugs, feature requests, **skill proposals**, **TechOps workflow changes** (templates provided) |
| Pull requests | code, skills, adapters, docs — see `CONTRIBUTING.md` |
| [enterpriseaitech.com](https://enterpriseaitech.com) | the article series that drives the roadmap |

Discussion categories we suggest enabling on the repository: *Announcements*, *Q&A*,
*Show and tell*, *Ideas*, *TechOps workflows*, *Skills*.

## The skill ecosystem

Skills are the community's unit of contribution. One `SKILL.md` works in Claude Code, Codex and
Cursor, so a workflow written once helps every team regardless of their coding agent.

- Catalog: [`skills/README.md`](../../skills/README.md)
- Author guide: [`skills/stag-skill-authoring/SKILL.md`](../../skills/stag-skill-authoring/SKILL.md)
- Install anywhere: `python -m stag.skills install --target all` or
  `claude plugin marketplace add mridulaEA/stag && claude plugin install stag-techops@stag-skills`

Skills we would love to see next (open a *Skill proposal* issue to claim one):

- `stag-techops-change-freeze-review` — evaluate a proposed deploy against open incidents
- `stag-techops-oncall-handoff` — summarise open episodes, pending approvals and stale actions for the next shift
- `stag-techops-knowledge-curation` — review investigation findings before promoting them into runbooks / the index
- `stag-adapter-opentelemetry`, `stag-adapter-salesforce-audit`, `stag-adapter-github-events`
- `stag-techops-finops-review` — reconcile budget ledgers against provider bills

## Roadmap themes

1. **TechOps backbone hardening** — persistence for incidents and the action ledger (PostgreSQL /
   Redis), outbox dispatcher, replay mode, late-event policy.
2. **Production connectors** — Kafka exactly-once boundaries, OpenTelemetry logs/traces,
   PagerDuty / Jira / ServiceNow record mapping with verified dedup semantics.
3. **Evaluation** — a labelled incident replay set for false-merge / false-split metrics,
   grounding checks for investigation findings, cost per correct completed outcome.
4. **Enterprise** — processing cells, policy engine integration (OPA), classification propagation
   to derived data, hierarchical budgets.
5. **Dashboard** — incident lifecycle view, action ledger view, "model calls avoided" counters.

## Governance

- Maintainers: Enterprise AI Tech. We aim to respond to issues and PRs within a week.
- Decisions that change the backbone's guarantees go through an RFC discussion first.
- Blameless by default: incident reviews, code reviews and discussions talk about systems.
- `CODE_OF_CONDUCT.md` applies in every channel.

## Recognition

Contributors are credited in release notes and in `skills/README.md` for skills they author.
Real-world TechOps stories (anonymised) are welcome in *Show and tell*; they shape the roadmap more
than anything else.
