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
| Use-case workflows | `stag-problem-management`, `stag-release-gate`, `stag-ai-automation-governance`, `stag-customer-care-comms`, `stag-log-analysis`, `stag-dataops-pipeline-triage`, `stag-security-governance` |
| Multi-agent & crews | `stag-multi-agent-orchestration` (roles in `agents/`), `stag-crew-authoring` (crews in `crews/`) |
| Development | `stag-trigger-rule-authoring`, `stag-stream-adapter`, `stag-skill-authoring` |

Crews (`crews/<name>/crew.yaml`) are ready-made teams per use case; run and check them with
`python -m stag.crews run-all --auto-approve` and `python -m pytest tests/test_crews.py`.

Read the relevant skill before changing code in its area or before acting on an incident.

## Commands

```bash
pip install -r requirements.txt            # runtime deps
pip install -e ".[dev]"                    # + pytest, ruff
python -m pytest tests -q                  # tests (techops + triggers run without ML deps)
ruff check stag tests                      # lint (line length 100)
python -m stag.skills validate             # skill frontmatter checks
python -m stag.crews validate              # crew definitions vs skills/agents
python -m stag.crews run-all --auto-approve   # every crew end-to-end (offline, deterministic executor)
python -m stag.main                        # run API at :8000 (simulator stream)
cd frontend && npm install && npm run dev  # dashboard at :5173
```

## Architecture rules

- `stag/techops/` is the deterministic backbone: `identity` → `incident` (state machine) →
  `coordinator` (dedup, correlation, material change) → `assignment` (bounded agent contract) →
  `actions` (retry-safe ledger) → `budget` (admission control). The *logic* stays pure Python +
  pydantic and fully unit-tested; the *state* lives behind `store.ControlStore` (in-memory, SQLite,
  PostgreSQL via `CONTROL_STORE_URL`). Every mutation is load → mutate → compare-and-swap inside
  `store.transaction()`; races are settled by `insert_unique`, never by checking then writing.
- Investigation requests are work items: queued → running (leased to one worker id) → succeeded |
  failed. Claim before model work, complete with the outcome; never generate for a request you do
  not hold.
- The orchestrator (`stag/pipeline/orchestrator.py`) calls the coordinator *before* retrieval and
  generation when `TECHOPS_COORDINATOR_ENABLED=true`. Duplicate deliveries and evidence-only
  updates must never reach the LLM.
- Trigger rules are pure functions of the event (`stag/triggers/rules.py`). Cadence/suppression
  belongs to the coordinator, not to rules. Crews declare rules via `RuleRegistry.from_specs`.
- `stag/crews/` runs declarative crews on the backbone. The schema has no step for direct
  production mutation; `authorize` needs a `human: true` role or a severity-bounded policy (≤ medium).
- Stream adapters preserve source identity: `StreamEvent.id` is the source's logical event id,
  never a replay cursor or a fresh UUID for a forwarded event. Adapters with delivery semantics
  commit only in `ack()` (called by the orchestrator after a durable outcome) and never let the
  commit position pass an in-flight delivery; undecodable records are dead-lettered by
  coordinates and digest, never logged with their content.
- Agents never change incident state, authorize, or dispatch. Those happen through explicit API
  calls with an actor and reason.
- Destinations and channels are reached only through `stag/connectors/` (`docs/connectors.md`).
  An action connector is idempotent on the operation id and reconciles by *observing*; the
  dispatcher, not the destination, decides what a lost response means. Notifications go through
  the outbox (one delivery per channel and dedup key). No credentials in the repository: every
  connector has a recording fake and a config template.
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

## Status and review ledger

STAG is a local development and simulation project: no API authentication, in-memory control
state, simulated crew runs. `docs/STATUS.md` says what is implemented vs simulated vs planned;
`docs/review/FINDINGS.md` tracks the engineering-review findings (R01–R32) and their status.
When you change the ledger, coordinator, runner or installer, add a regression test to
`tests/test_review_regressions.py` or the module's test file and update the ledger row.

## Community

See `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and `docs/community/README.md`. Skill proposals use
the issue template "Skill proposal"; TechOps workflow changes use "TechOps workflow change".
