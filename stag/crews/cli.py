"""`stag-crews` — list, inspect, validate and run STAG crews end-to-end.

    stag-crews list
    stag-crews show techops-incident-triage
    stag-crews validate
    stag-crews run techops-incident-triage [--scenario path] [--auto-approve] [--json]
    stag-crews run-all --auto-approve            # end-to-end check of every crew
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import structlog

from stag.crews.loader import CREWS_DIR, discover_crews, load_crew, load_scenario, validate_all
from stag.crews.runner import check_expectations, run_crew


def main(argv: list[str] | None = None) -> int:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    parser = argparse.ArgumentParser(prog="stag-crews", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--crews-dir", type=Path, default=CREWS_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list crews")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--domain")

    p_show = sub.add_parser("show", help="show a crew's roles and workflow")
    p_show.add_argument("name")
    p_show.add_argument("--json", action="store_true")

    sub.add_parser("validate", help="validate every crew against skills and agents")

    p_run = sub.add_parser("run", help="run a crew against a scenario")
    p_run.add_argument("name")
    p_run.add_argument("--scenario", type=Path)
    p_run.add_argument("--auto-approve", action="store_true", help="simulate the human approver")
    p_run.add_argument("--json", action="store_true")

    p_all = sub.add_parser("run-all", help="run every crew's bundled scenario")
    p_all.add_argument("--auto-approve", action="store_true")
    p_all.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "list":
        crews = discover_crews(args.crews_dir)
        if args.domain:
            crews = [c for c in crews if c.domain == args.domain]
        if args.json:
            print(json.dumps([{"name": c.name, "display_name": c.display_name, "domain": c.domain, "purpose": c.purpose, "roles": [r.id for r in c.roles], "steps": [s.step for s in c.workflow]} for c in crews], indent=2))
        else:
            for c in crews:
                print(f"[{c.domain}] {c.name} — {c.display_name}\n    {c.purpose}\n    roles: {', '.join(r.id for r in c.roles)}\n")
        return 0

    if args.command == "show":
        crew = load_crew(args.name, args.crews_dir)
        if args.json:
            print(crew.model_dump_json(indent=2))
            return 0
        print(f"{crew.display_name} ({crew.name}) — {crew.domain} v{crew.version}\n{crew.purpose}\n")
        print("Roles:")
        for r in crew.roles:
            kind = "human" if r.human else (r.agent or "agent")
            print(f"  - {r.id}: {r.title} [{kind}] skills={r.skills} mutations={r.permissions.production_mutations}")
        print("\nWorkflow:")
        for i, s in enumerate(crew.workflow):
            extra = ""
            if s.to:
                extra = f" → {s.to.value}"
            if s.plan_step:
                extra = f" plan_step={s.plan_step}"
            if s.policy:
                extra = f" policy={s.policy.mode}" + (f"(auto ≤ {s.policy.auto_if_severity_at_most})" if s.policy.auto_if_severity_at_most else "")
            print(f"  {i:2d}. {s.step:<22} {s.action.value:<15} {s.role or '-':<22}{extra}")
        if crew.guardrails:
            print("\nGuardrails:")
            for g in crew.guardrails:
                print(f"  - {g}")
        return 0

    if args.command == "validate":
        report = validate_all(args.crews_dir)
        if not report:
            print(f"FAIL no crews found under {args.crews_dir} (missing catalog)", file=sys.stderr)
            return 1
        bad = 0
        for name, problems in report.items():
            print(f"{'FAIL' if problems else 'ok':4s} {name}")
            for p in problems:
                print(f"       - {p}")
            bad += bool(problems)
        print(f"\n{len(report) - bad}/{len(report)} crews valid")
        return 1 if bad else 0

    if args.command in ("run", "run-all"):
        crews = [load_crew(args.name, args.crews_dir)] if args.command == "run" else discover_crews(args.crews_dir)
        if not crews:
            print("no crews found", file=sys.stderr)
            return 1
        reports = []
        problems: list[str] = []
        for crew in crews:
            scenario = load_scenario(crew, getattr(args, "scenario", None))
            report = run_crew(crew, scenario, auto_approve=args.auto_approve)
            reports.append(report)
            if report.failed_steps:
                problems.append(f"{crew.name}: {report.failed_steps} failed step(s)")
            problems.extend(f"{crew.name}: {f}" for f in check_expectations(report, scenario, args.auto_approve))
        if args.json:
            print(json.dumps([r.model_dump(mode="json") | {"model_calls_avoided": r.model_calls_avoided, "resolved": r.resolved, "held": r.held, "failed_steps": r.failed_steps} for r in reports], indent=2, default=str))
        else:
            for r in reports:
                print(r.to_markdown())
                print()
        if problems:
            print("RUN FAILED:", file=sys.stderr)
            for pr in problems:
                print(f"  - {pr}", file=sys.stderr)
            return 1
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
