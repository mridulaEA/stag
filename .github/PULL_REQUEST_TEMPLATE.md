## Summary

<!-- What does this change and which operational scenario or workflow does it address? -->

## Type

- [ ] TechOps backbone (`stag/techops/`)
- [ ] Trigger rule / stream adapter / retrieval / generation
- [ ] Skill or agent definition (`skills/`, `agents/`)
- [ ] API / frontend
- [ ] Docs / community

## Checklist

- [ ] Tests added or updated, named after the scenario they prevent
- [ ] `python -m pytest tests -q`, `ruff check stag tests`, `python -m stag.skills validate` pass locally
- [ ] Config documented in `.env.example` and README (if applicable)
- [ ] Relevant skill(s) updated to match behaviour
- [ ] For backbone changes: resilience-test rows covered: <!-- e.g. 1, 4, 7 -->
- [ ] For skills: folder name == `name`; description says what / when / when-not; tried in <!-- Claude Code / Codex / Cursor -->

## Linked issue / discussion

<!-- Closes #… -->
