"""`.anything-pipeline.yml`: the plan, the user's overrides, and their validation."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

PLAN_FILE = ".anything-pipeline.yml"

FLAVOR_PMS = {
    "node": ("npm", "pnpm", "yarn", "bun"),
    "python": ("uv", "poetry", "pip"),
    "go": (),
    "generic": (),
}

# Inputs of verify.yml that overrides may set, with their YAML type.
INPUT_TYPES = {
    "node-version": str,
    "python-version": str,
    "pnpm-version": str,
    "poetry-version": str,
    "ignore-scripts": bool,
    "check-command": str,
    "test-command": str,
    "build-command": str,
    "allow-empty": bool,
    "allow-unlocked": bool,
}

TOP_KEYS = {"version", "template", "plan", "overrides", "disabled", "ejected"}
UNIT_PATH = re.compile(r"^(\.|[A-Za-z0-9._@+-]+(/[A-Za-z0-9._@+-]+)*)$")


class PlanFileError(Exception):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def safe_unit_path(path: str) -> bool:
    """Unit paths end up in generated YAML, so only plain path characters are allowed."""
    return bool(UNIT_PATH.match(path)) and ".." not in path.split("/")


def load_existing(root: Path) -> dict:
    """Read and validate an existing plan file; returns {} when there is none."""
    path = root / PLAN_FILE
    if path.is_symlink():
        raise PlanFileError([f"{PLAN_FILE} is a symlink; refusing to read it"])
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise PlanFileError([f"{PLAN_FILE}: {exc or 'nested too deeply'}"]) from exc
    errors = validate(data)
    if errors:
        raise PlanFileError([f"{PLAN_FILE}: {e}" for e in errors])
    return data


def validate(data) -> list[str]:
    if data is None:
        return []
    if not isinstance(data, dict):
        return ["top level must be a mapping"]
    errors = [f"unknown top-level key {k!r}" for k in data if k not in TOP_KEYS]
    if "version" in data and data["version"] != 1:
        errors.append("version must be 1")
    for key in ("disabled", "ejected"):
        if key in data and not (isinstance(data[key], list) and all(isinstance(i, str) for i in data[key])):
            errors.append(f"{key} must be a list of strings")
    overrides = data.get("overrides", {})
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        return errors + ["overrides must be a mapping"]
    for key in overrides:
        if key not in {"units", "verify"}:
            errors.append(f"overrides: unknown key {key!r}")
    errors += _check_inputs(overrides.get("verify", {}), "overrides.verify")
    units = overrides.get("units", {}) or {}
    if not isinstance(units, dict):
        return errors + ["overrides.units must be a mapping"]
    for path, unit in units.items():
        where = f"overrides.units.{path}"
        if not isinstance(path, str) or not safe_unit_path(path):
            errors.append(f"{where}: unsafe or invalid unit path")
            continue
        if not isinstance(unit, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for key in unit:
            if key not in {"verify", "inputs"}:
                errors.append(f"{where}: unknown key {key!r}")
        if "verify" in unit:
            errors += _check_verify(unit["verify"], f"{where}.verify")
        errors += _check_inputs(unit.get("inputs", {}), f"{where}.inputs")
    return errors


def _check_verify(verify, where: str) -> list[str]:
    if not isinstance(verify, dict):
        return [f"{where}: must be a mapping"]
    errors = [f"{where}: unknown key {k!r}" for k in verify if k not in {"flavor", "vars"}]
    flavor = verify.get("flavor")
    if flavor not in FLAVOR_PMS:
        return errors + [f"{where}.flavor: must be one of {sorted(FLAVOR_PMS)}"]
    vars_ = verify.get("vars", {}) or {}
    if not isinstance(vars_, dict) or set(vars_) - {"pm"}:
        errors.append(f"{where}.vars: only 'pm' is supported")
    else:
        pm = vars_.get("pm")
        if FLAVOR_PMS[flavor] and pm not in FLAVOR_PMS[flavor]:
            errors.append(f"{where}.vars.pm: for {flavor} must be one of {list(FLAVOR_PMS[flavor])}")
        if not FLAVOR_PMS[flavor] and pm:
            errors.append(f"{where}.vars.pm: {flavor} takes no package manager")
    return errors


def _check_inputs(inputs, where: str) -> list[str]:
    if inputs is None:
        return []
    if not isinstance(inputs, dict):
        return [f"{where}: must be a mapping"]
    errors = []
    for name, value in inputs.items():
        expected = INPUT_TYPES.get(name)
        if expected is None:
            errors.append(f"{where}: unknown input {name!r} (allowed: {sorted(INPUT_TYPES)})")
        elif not isinstance(value, expected) or (expected is str and isinstance(value, bool)):
            errors.append(f"{where}.{name}: must be {expected.__name__}")
        elif expected is str and ("${{" in value or any(c in value for c in "\n\r\0")):
            errors.append(f"{where}.{name}: must be a single line without '${{{{'")
    return errors


def dump(kit_repo: str, ref: str, plan: dict, existing: dict) -> str:
    """Serialise the plan file, preserving the user's overrides, disabled and ejected lists."""
    document = {
        "version": 1,
        "template": {"repo": kit_repo, "ref": ref},
        "plan": {
            "units": [
                {k: unit[k] for k in ("path", "kinds", "verify", "ship")} for unit in plan["units"]
            ],
            "always": plan["always"],
        },
        "overrides": existing.get("overrides") or {},
        "disabled": existing.get("disabled") or [],
        "ejected": existing.get("ejected") or [],
    }
    header = (
        "# anything-pipeline plan. `plan:` is rewritten by `ap init`; `overrides:` is yours and is preserved.\n"
        "# Overrides: units.<path>.verify {flavor, vars.pm} replaces detection; verify / units.<path>.inputs set\n"
        "# verify.yml inputs (node-version, test-command, ...).\n"
    )
    return header + yaml.safe_dump(document, sort_keys=False, default_flow_style=False)
