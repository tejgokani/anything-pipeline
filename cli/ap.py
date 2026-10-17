#!/usr/bin/env python3
"""ap: anything-pipeline command line.

Exit codes: 0 ok, 1 error, 2 plan is ambiguous (only with --strict).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from detect.engine import RulesError, detect, load_rules, render_text  # noqa: E402
from render import InitError, run_init  # noqa: E402
from render.init import DEFAULT_KIT_REPO  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ap", description="Detect which pipelines a repository needs.")
    sub = parser.add_subparsers(dest="command", required=True)

    det = sub.add_parser("detect", help="run the detection manifest against a repository (read-only)")
    det.add_argument("path", nargs="?", default=".", help="repository root (default: current directory)")
    det.add_argument("--explain", action="store_true", help="show the evidence behind every decision")
    det.add_argument("--json", action="store_true", help="print the plan as JSON")
    det.add_argument("--strict", action="store_true", help="exit 2 if the plan is ambiguous")
    det.add_argument("--rules", type=Path, help="use a different rules.yml")

    ini = sub.add_parser("init", help="render the verify caller workflow and .anything-pipeline.yml")
    ini.add_argument("path", nargs="?", default=".", help="repository root (default: current directory)")
    ini.add_argument("--ref", help="full 40-character commit SHA of the anything-pipeline release to pin")
    ini.add_argument("--ref-label", default="", help="human label for the pin, e.g. v0.1.0 (written as a comment)")
    ini.add_argument("--kit-repo", default=DEFAULT_KIT_REPO, help=f"owner/name of the kit (default: {DEFAULT_KIT_REPO})")
    ini.add_argument("--default-branch", default="main", help="branch that triggers push runs (default: main)")
    ini.add_argument("--dry-run", action="store_true", help="print the files instead of writing them")
    ini.add_argument("--force", action="store_true", help="overwrite an existing or edited verify.yml")
    ini.add_argument("--allow-unpinned", action="store_true", help="accept a tag or branch for --ref (not recommended)")
    ini.add_argument("--rules", type=Path, help="use a different rules.yml")
    return parser


def run_init_command(args) -> int:
    try:
        rules = load_rules(args.rules)
        result = run_init(
            args.path,
            ref=args.ref,
            kit_repo=args.kit_repo,
            ref_label=args.ref_label,
            default_branch=args.default_branch,
            dry_run=args.dry_run,
            force=args.force,
            allow_unpinned=args.allow_unpinned,
            rules=rules,
        )
    except (RulesError, InitError, OSError) as exc:
        print(f"ap: {exc}", file=sys.stderr)
        return 1

    if result.blocked:
        print("ap init: nothing written; a decision is needed:", file=sys.stderr)
        for item in result.blocked:
            print(f"  - {item}", file=sys.stderr)
        return 2
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    if result.dry_run:
        for rel, text in result.files.items():
            print(f"==> {rel} <==")
            print(text, end="" if text.endswith("\n") else "\n")
        print("(dry run: nothing written)", file=sys.stderr)
    else:
        for rel in result.written:
            print(f"wrote {rel}")
        print("Next: commit these files, then require the `gate` check in branch protection.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return run_init_command(args)
    try:
        rules = load_rules(args.rules)
        plan = detect(args.path, rules=rules, explain=args.explain)
    except RulesError as exc:
        print(f"ap: {exc}", file=sys.stderr)
        return 1
    except (OSError, NotADirectoryError) as exc:
        print(f"ap: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(render_text(plan, args.path))
    return 2 if args.strict and plan["ambiguous"] else 0


if __name__ == "__main__":
    sys.exit(main())
