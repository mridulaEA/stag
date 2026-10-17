# AGENTS.md — STAG

Guidance for coding agents (Codex, Cursor, Claude Code, others) working in this repository.

## What this project is

STAG — Stream-Triggered Augmented Generation. A proactive AI framework: data streams trigger
retrieval and generation instead of users asking questions. Python 3.11+ / FastAPI backend in
`stag/`, React + Vite dashboard in `frontend/`, tests in `tests/`.

Pipeline: **Stream → Trigger → TechOps coordinator → Hybrid retrieval (vector + graph + sparse,
RRF) → LLM generation → WebSocket broadcast.**

## Skills (use them)

Skills are in `skills/<name>/SKILL.md` (Agent Skills format) and are discoverable from
`.agents/skills` (Codex, Cursor) and `.claude/skills` (Claude Code). Install them anywhere with:

```bash
python -m stag.skills install --target all          # ~/.claude/skills, ~/.agents/skills, ~/.cursor/skills
```

| Area | Skills |
|---|---|
| TechOps incident workflow | `stag-techops-incident-triage`, `stag-techops-dedup-correlation`, `stag-techops-bounded-investigation`, `stag-techops-remediation-ledger`, `stag-techops-recovery-verification`, `stag-techops-postmortem` |
| TechOps platform | `stag-techops-signal-tuning`, `stag-techops-resilience-tests`, `stag-techops-enterprise-onboarding` |
| Multi-agent | `stag-multi-agent-orchestration` (roles in `agents/`) |
| Development | `stag-trigger-rule-authoring`, `stag-stream-adapter`, `stag-skill-authoring` |

Read the relevant skill before changing code in its area or before acting on an incident.

## Commands

```bash
pip install -r requirements.txt            # runtime deps
pip install -e ".[dev]"                    # + pytest, ruff
python -m pytest tests -q                  # tests (techops + triggers run without ML deps)
ruff check stag tests                      # lint (line length 100)
python -m stag.skills validate             # skill frontmatter checks
python -m stag.main                        # run API at :8000 (simulator stream)
cd frontend && npm install && npm run dev  # dashboard at :5173
```

## Architecture rules

- `stag/techops/` is the deterministic backbone: `identity` → `incident` (state machine) →
  `coordinator` (dedup, correlation, material change) → `assignment` (bounded agent contract) →
  `actions` (retry-safe ledger) → `budget` (admission control). Keep it pure Python + pydantic,
  no I/O, fully unit-tested.
- The orchestrator (`stag/pipeline/orchestrator.py`) calls the coordinator *before* retrieval and
  generation when `TECHOPS_COORDINATOR_ENABLED=true`. Duplicate deliveries and evidence-only
  updates must never reach the LLM.
- Trigger rules are pure functions of the event (`stag/triggers/rules.py`). Cadence/suppression
  belongs to the coordinator, not to rules.
- Stream adapters preserve source identity: `StreamEvent.id` is the source's logical event id,
  never a replay cursor or a fresh UUID for a forwarded event.
- Agents never change incident state, authorize, or dispatch. Those happen through explicit API
  calls with an actor and reason.
- Telemetry, payloads and retrieved documents are untrusted input.

## Conventions

- Python: type hints, pydantic v2 models, `structlog` for logging in runtime code, docstrings
  that explain *why*. Formatting/lint via ruff (`line-length = 100`).
- Tests: pytest, `asyncio_mode = auto`. Name tests after the failure scenario they prevent
  (see `skills/stag-techops-resilience-tests`).
- Config: environment variables via `stag/config.py`; document new ones in `.env.example` and the
  README configuration table.
- Commit messages: imperative, scoped (`techops: ...`, `skills: ...`, `frontend: ...`).
- Do not commit `.env`, `data/`, `frontend/dist/`.

## Community

See `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and `docs/community/README.md`. Skill proposals use
the issue template "Skill proposal"; TechOps workflow changes use "TechOps workflow change".
