---
name: stag-skill-authoring
description: Create, validate and contribute a STAG skill that works in Claude Code, Codex and Cursor from one SKILL.md source. Use when adding a new TechOps workflow, a role skill, or a development skill to the STAG skill pack, or when reviewing a community skill PR. Covers the Agent Skills frontmatter rules, folder layout, cross-tool discovery, validation (`stag-skills validate`) and the contribution checklist.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: community
---

# STAG: Skill Authoring and Contribution

STAG skills follow the Agent Skills format (`SKILL.md` with YAML frontmatter) so one source works
in Claude Code (`.claude/skills`, plugins), Codex (`.agents/skills`) and Cursor
(`.cursor/skills`, `.agents/skills`, `.claude/skills`). The canonical source is `skills/<name>/`
in this repository; the other locations are links or installed copies.

## Layout

```text
skills/<skill-name>/
├── SKILL.md              # required: frontmatter + procedure
├── references/           # optional: contracts, templates, longer docs the skill cites
├── scripts/              # optional: helpers the skill tells the agent to run
└── agents/openai.yaml    # optional: Codex UI metadata / invocation policy
```

## Frontmatter rules (validated by `python -m stag.skills validate`)

- `name`: lowercase letters, digits, hyphens; ≤ 64 chars; **must equal the folder name**;
  prefix with `stag-` (TechOps skills: `stag-techops-<workflow>`).
- `description`: ≤ 1024 chars, non-empty. Front-load *what* it does and *when* to use it, name
  trigger phrases, and say when **not** to use it (point to the sibling skill). Agents choose
  skills from this text alone.
- Optional: `license`, `metadata` (author, version, domain: `techops | development | community`),
  Cursor `paths`/`disable-model-invocation`, Claude `allowed-tools`.

## Writing the body

1. One-paragraph principle: what the skill guarantees and what it never does.
2. **When this skill applies** (and hand-offs to siblings).
3. **Inputs** — which STAG objects/APIs to read.
4. **Procedure** — numbered, each step with the concrete call or file.
5. **Output** — a schema or example block the agent should produce.
6. **Guardrails** — the "never" list (untrusted telemetry, no state change by claim, no new op id
   for a retry, missing telemetry ≠ healthy …).
7. **Related code** — file paths and tests.

Keep it under ~250 lines; move long material to `references/`. Prefer tables for decisions.
Write for an agent that has the repo open: name files and functions, not vague advice.

## Validate and test

```bash
python -m stag.skills validate            # frontmatter, name/folder match, description length
python -m stag.skills list                # catalog as the agents see it
python -m stag.skills install --target all --scope project   # refresh local links
```

Try the skill in at least one tool: Claude Code (`/stag-…` or let it auto-select), Codex (`$` or
`/skills`), Cursor (`/` in Agent chat). Check that the description alone makes the agent pick it
for the intended request and *not* for its sibling's request.

## Contribution checklist (PR template mirrors this)

- [ ] Folder name == `name`; validation passes
- [ ] Description says what, when, and when-not
- [ ] Procedure references real STAG code paths / API routes
- [ ] Guardrails present; no step executes a production change without the ledger
- [ ] Added to `skills/README.md` catalog with domain and one-line summary
- [ ] If TechOps: which resilience-test rows it relies on (`stag-techops-resilience-tests`)
- [ ] Tried in at least one agent runtime; note which in the PR

See `CONTRIBUTING.md` and `docs/community/README.md` for review flow and the skill proposal
issue template.
