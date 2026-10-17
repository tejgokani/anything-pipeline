"""`ap init`: detect, apply overrides, render the verify caller and the plan file."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from detect.engine import detect
from detect.engine.matchers import RepoFS

from . import callers, planfile

# Placeholder until the kit is published; override with --kit-repo.
DEFAULT_KIT_REPO = "tejgokani/anything-pipeline"
WORKFLOW_PATH = ".github/workflows/verify.yml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
LABEL_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
DRY_RUN_PLACEHOLDER = "REPLACE_WITH_40_HEX_COMMIT_SHA"


class InitError(Exception):
    """A problem the user must fix; nothing was written."""


@dataclass
class InitResult:
    files: dict = field(default_factory=dict)  # relative path -> text that is (or would be) written
    written: list = field(default_factory=list)
    blocked: list = field(default_factory=list)  # ambiguity that needs a decision; nothing written
    notes: list = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    dry_run: bool = False


def run_init(
    path,
    *,
    ref: str | None,
    kit_repo: str = DEFAULT_KIT_REPO,
    ref_label: str = "",
    default_branch: str = "main",
    dry_run: bool = False,
    force: bool = False,
    allow_unpinned: bool = False,
    rules=None,
) -> InitResult:
    root = Path(path).resolve()
    if not root.is_dir():
        raise InitError(f"not a directory: {path}")
    _check_args(ref, kit_repo, ref_label, default_branch, dry_run, allow_unpinned)
    ref = ref or DRY_RUN_PLACEHOLDER

    try:
        existing = planfile.load_existing(root)
    except planfile.PlanFileError as exc:
        raise InitError(str(exc)) from exc

    plan = detect(root, rules=rules)
    result = InitResult(plan=plan, dry_run=dry_run)
    overrides = existing.get("overrides") or {}
    unit_overrides = overrides.get("units") or {}
    global_inputs = overrides.get("verify") or {}

    units = []
    detected_paths = {u["path"] for u in plan["units"]}
    for stale in sorted(set(unit_overrides) - detected_paths):
        result.notes.append(f"override for unit {stale!r} matches no detected unit and was ignored")
    for problem in plan["problems"]:
        result.blocked.append(problem)

    for unit in plan["units"]:
        unit_path = unit["path"]
        if not planfile.safe_unit_path(unit_path):
            result.blocked.append(
                f"unit path {unit_path!r} has characters that are not allowed in generated workflows "
                "(allowed: letters, digits and . _ @ + - /)"
            )
            continue
        override = unit_overrides.get(unit_path) or {}
        verify = override.get("verify") or unit["verify"]
        if verify is None:
            result.blocked.append(_ambiguity_message(unit))
            continue
        units.append(
            {
                "path": unit_path,
                "flavor": verify["flavor"],
                "pm": (verify.get("vars") or {}).get("pm", ""),
                "inputs": {**global_inputs, **(override.get("inputs") or {})},
            }
        )
    if result.blocked:
        return result

    disabled = existing.get("disabled") or []
    if "verify" in disabled:
        result.notes.append("verify is listed under `disabled:`; no workflow rendered")
    else:
        result.files[WORKFLOW_PATH] = callers.render_verify(kit_repo, ref, ref_label, default_branch, units)
    result.files[planfile.PLAN_FILE] = planfile.dump(kit_repo, ref, plan, existing)
    result.notes += _scope_notes(plan)
    if _needs_pnpm_version(root, units):
        result.notes.append(
            "pnpm is used but package.json has no `packageManager` field; set it, or add "
            "`pnpm-version` under overrides.verify, otherwise the pnpm setup step will fail."
        )

    refusals = _plan_writes(root, result.files, force)
    if refusals:
        raise InitError("\n".join(refusals))
    if not dry_run:
        for rel, text in result.files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            result.written.append(rel)
    return result


def _check_args(ref, kit_repo, ref_label, default_branch, dry_run, allow_unpinned) -> None:
    if not REPO_RE.match(kit_repo):
        raise InitError(f"--kit-repo must look like owner/name, got {kit_repo!r}")
    if ref_label and not LABEL_RE.match(ref_label):
        raise InitError(f"--ref-label may only contain letters, digits and . _ + -, got {ref_label!r}")
    if not BRANCH_RE.match(default_branch):
        raise InitError(f"--default-branch has unsupported characters: {default_branch!r}")
    if ref is None:
        if not dry_run:
            raise InitError(
                "--ref is required: pass the full 40-character commit SHA of the anything-pipeline release "
                "to pin (use --dry-run to preview without one)"
            )
        return
    if SHA_RE.match(ref):
        return
    if not allow_unpinned:
        raise InitError(
            f"--ref {ref!r} is not a full 40-character commit SHA. Tags and branches can be moved, which is "
            "how tj-actions/changed-files was compromised. Pass a SHA, or --allow-unpinned to accept the risk."
        )
    if not REF_RE.match(ref):
        raise InitError(f"--ref has unsupported characters: {ref!r}")


def _ambiguity_message(unit: dict) -> str:
    reasons = "; ".join(unit["reasons"]) or "no verify flavor"
    return (
        f"unit {unit['path']!r} is ambiguous ({reasons}). Decide in .anything-pipeline.yml, for example:\n"
        f"    overrides:\n      units:\n        {planfile_quote(unit['path'])}:\n"
        "          verify: { flavor: node, vars: { pm: npm } }"
    )


def planfile_quote(text: str) -> str:
    return '"' + text.replace('"', '\\"') + '"'


def _needs_pnpm_version(root: Path, units: list) -> bool:
    uses_pnpm = [u for u in units if u["flavor"] == "node" and u["pm"] == "pnpm" and "pnpm-version" not in u["inputs"]]
    if not uses_pnpm:
        return False
    package = RepoFS(root, ()).read_json("package.json")
    return not (isinstance(package, dict) and package.get("packageManager"))


def _scope_notes(plan: dict) -> list:
    notes = ["Phase 1 renders the verify pipeline only; agent-gate and harden are not available yet."]
    shipping = sorted(
        {f"{u['ship']['pipeline']}" for u in plan["units"] if u["ship"]}
    )
    if shipping:
        notes.append(f"Detected ship pipeline(s) not rendered yet: {', '.join(shipping)}.")
    return notes


def _plan_writes(root: Path, files: dict, force: bool) -> list:
    """Decide up front whether every target is safe to write; returns refusal messages."""
    refusals = []
    for rel, text in files.items():
        target = root / rel
        # The parent directories and the file must stay inside the repository.
        probe = target
        while not probe.exists() and probe != root:
            probe = probe.parent
        resolved = probe.resolve()
        if resolved != root and root not in resolved.parents:
            refusals.append(f"{rel}: resolves outside the repository; refusing to write")
            continue
        if target.is_symlink():
            refusals.append(f"{rel}: is a symlink; refusing to write")
            continue
        if rel == WORKFLOW_PATH and target.is_file() and not force:
            state = callers.generated_state(target.read_text(encoding="utf-8"))
            if state == "foreign":
                refusals.append(f"{rel} already exists and was not generated by anything-pipeline; use --force to replace it")
            elif state == "modified":
                refusals.append(
                    f"{rel} was edited after it was generated; use --force to overwrite your edits "
                    "(or move them into overrides in .anything-pipeline.yml)"
                )
    return refusals
