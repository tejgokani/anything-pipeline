#!/usr/bin/env python3
"""ap: anything-pipeline command line.

Exit codes: 0 ok, 1 error, 2 plan is ambiguous (`detect --strict`) or `init` needs a decision.
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 11)


def check_runtime(version_info=None) -> str | None:
    """Explain why this interpreter cannot run ap, or return None. Runs before any other import."""
    version_info = version_info or sys.version_info
    if tuple(version_info[:2]) < MIN_PYTHON:
        found = ".".join(str(part) for part in version_info[:3])
        return (
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required, but this is Python {found}. "
            "Run it with a newer interpreter, for example: "
            "uv run --python 3.11 --with 'PyYAML>=6' python cli/ap.py ..."
        )
    try:
        import yaml  # noqa: F401
    except ImportError:
        return "PyYAML is required. Install it with: python -m pip install 'PyYAML>=6'"
    return None


_problem = check_runtime()
if _problem:
    sys.stderr.write(f"ap: {_problem}\n")
    raise SystemExit(1)

import argparse  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

if __package__ in (None, ""):  # run as a script: python cli/ap.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detect.engine import RulesError, detect, load_rules, render_text  # noqa: E402
from detect.engine.text import escape_controls  # noqa: E402
from render import InitError, run_init  # noqa: E402
from render.init import DEFAULT_KIT_REPO  # noqa: E402


def emit(text: str = "", *, err: bool = False, end: str = "\n") -> None:
    """Print, neutralising terminal escapes and other control characters from untrusted repos."""
    stream = sys.stderr if err else sys.stdout
    stream.write(escape_controls(text, keep_newlines=True) + end)


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
        emit(f"ap: {exc}", err=True)
        return 1

    if result.blocked:
        emit("ap init: nothing written; a decision is needed:", err=True)
        for item in result.blocked:
            emit(f"  - {item}", err=True)
        return 2
    for note in result.notes:
        emit(f"note: {note}", err=True)
    if result.dry_run:
        for rel, text in result.files.items():
            emit(f"==> {rel} <==")
            emit(text, end="" if text.endswith("\n") else "\n")
        emit("(dry run: nothing written)", err=True)
    else:
        for rel in result.written:
            emit(f"wrote {rel}")
        emit("Next: commit these files, then require the `gate` check in branch protection.")
    return 0


def run_detect_command(args) -> int:
    try:
        rules = load_rules(args.rules)
        plan = detect(args.path, rules=rules, explain=args.explain)
    except (RulesError, OSError) as exc:
        emit(f"ap: {exc}", err=True)
        return 1

    if args.json:
        # json.dumps escapes every non-ASCII and control character, so this is terminal-safe as is.
        emit(json.dumps(plan, indent=2, sort_keys=True))
    else:
        emit(render_text(plan, args.path))
    return 2 if args.strict and plan["ambiguous"] else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        return run_init_command(args) if args.command == "init" else run_detect_command(args)
    except RecursionError:
        emit("ap: an input file is nested too deeply to process", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
