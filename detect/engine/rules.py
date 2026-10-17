"""Load and validate detect/rules.yml."""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "rules.yml"

LEAF_KEYS = frozenset({"file", "file_any", "glob", "json", "toml"})
COMBINATOR_KEYS = frozenset({"all", "any", "none"})
MODIFIER_KEYS = frozenset({"search_up"})
UNIT_SOURCES = frozenset({"pnpm-workspace.yaml", "package.json", "go.work"})
TOP_KEYS = frozenset({"version", "ignore_dirs", "signals", "kinds", "pipelines", "flavors", "workspaces"})
DEFAULT_IGNORE = (".git", "node_modules", "vendor", ".venv", "venv", "__pycache__")


class RulesError(Exception):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("invalid rules:\n  " + "\n  ".join(self.errors))


class Rules:
    def __init__(self, data: dict):
        self.version = data["version"]
        self.ignore_dirs = list(data.get("ignore_dirs", DEFAULT_IGNORE))
        self.signals = data["signals"]
        self.kinds = data["kinds"]
        pipelines = data["pipelines"]
        self.always = list(pipelines.get("always", []))
        self.ship = list(pipelines.get("ship", []))
        self.verify_flavors = list((data.get("flavors") or {}).get("verify", []))
        self.workspaces = list(data.get("workspaces") or [])


def load_rules(path: str | Path | None = None) -> Rules:
    path = Path(path) if path else DEFAULT_RULES_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise RulesError([f"{path}: {exc or 'nested too deeply'}"]) from exc
    errors = validate(data)
    if errors:
        raise RulesError(errors)
    return Rules(data)


def validate(data) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["top level must be a mapping"]
    for key in data:
        if key not in TOP_KEYS:
            errors.append(f"unknown top-level key {key!r}")
    if data.get("version") != 1:
        errors.append("version must be 1")
    for required in ("signals", "kinds", "pipelines"):
        if not isinstance(data.get(required), dict) or not data.get(required):
            errors.append(f"{required!r} must be a non-empty mapping")
    if errors:
        return errors

    ignore = data.get("ignore_dirs", [])
    if not isinstance(ignore, list) or not all(isinstance(i, str) for i in ignore):
        errors.append("ignore_dirs must be a list of strings")

    signals, kinds = data["signals"], data["kinds"]
    names = set(signals)
    for name, cond in signals.items():
        _check_cond(cond, f"signals.{name}", names, errors)
    for name, cond in kinds.items():
        _check_cond(cond, f"kinds.{name}", names, errors)
    _check_pipelines(data["pipelines"], set(kinds), errors)
    _check_flavors(data.get("flavors") or {}, names, errors)
    _check_workspaces(data.get("workspaces") or [], names, errors)
    if not errors:
        errors.extend(_find_cycles(signals))
    return errors


def _check_cond(cond, where: str, names: set[str], errors: list[str]) -> None:
    if isinstance(cond, str):
        if cond not in names:
            errors.append(f"{where}: unknown signal {cond!r}")
        return
    if not isinstance(cond, dict) or not cond:
        errors.append(f"{where}: condition must be a signal name or a non-empty mapping")
        return
    ops = [k for k in cond if k not in MODIFIER_KEYS]
    for op in ops:
        if op not in LEAF_KEYS and op not in COMBINATOR_KEYS:
            errors.append(f"{where}: unknown key {op!r}")
            return
    leaves = [op for op in ops if op in LEAF_KEYS]
    combos = [op for op in ops if op in COMBINATOR_KEYS]
    if not ops:
        errors.append(f"{where}: needs a leaf or combinator")
        return
    if leaves and (len(leaves) > 1 or combos):
        errors.append(f"{where}: a leaf cannot be mixed with other operators")
        return
    if "search_up" in cond:
        if not isinstance(cond["search_up"], bool):
            errors.append(f"{where}.search_up: must be true or false")
        if combos:
            errors.append(f"{where}: search_up applies to leaves only")
    for op in ops:
        arg = cond[op]
        here = f"{where}.{op}"
        if op in COMBINATOR_KEYS:
            if not isinstance(arg, list) or not arg:
                errors.append(f"{here}: must be a non-empty list")
                continue
            for i, item in enumerate(arg):
                _check_cond(item, f"{here}[{i}]", names, errors)
        elif op in ("file", "glob"):
            if not isinstance(arg, str) or not arg:
                errors.append(f"{here}: must be a non-empty string")
        elif op == "file_any":
            if not isinstance(arg, list) or not arg or not all(isinstance(a, str) for a in arg):
                errors.append(f"{here}: must be a non-empty list of strings")
        else:  # json / toml
            _check_doc_matcher(arg, here, errors)


def _check_doc_matcher(arg, where: str, errors: list[str]) -> None:
    if not isinstance(arg, dict):
        errors.append(f"{where}: must be a mapping")
        return
    for key in arg:
        if key not in {"file", "path", "exists", "equals"}:
            errors.append(f"{where}: unknown key {key!r}")
    if not isinstance(arg.get("file"), str):
        errors.append(f"{where}.file: required string")
    path = arg.get("path")
    if not (isinstance(path, str) and path) and not (
        isinstance(path, list) and path and all(isinstance(p, str) for p in path)
    ):
        errors.append(f"{where}.path: required string or list of strings")
    if "exists" in arg and "equals" in arg:
        errors.append(f"{where}: use either exists or equals")
    if "exists" in arg and not isinstance(arg["exists"], bool):
        errors.append(f"{where}.exists: must be true or false")


def _check_pipelines(pipelines: dict, kind_names: set[str], errors: list[str]) -> None:
    for key in pipelines:
        if key not in {"always", "ship"}:
            errors.append(f"pipelines: unknown key {key!r}")
    always = pipelines.get("always", [])
    if not isinstance(always, list) or not all(isinstance(a, str) for a in always):
        errors.append("pipelines.always: must be a list of strings")
    ship = pipelines.get("ship", [])
    if not isinstance(ship, list):
        errors.append("pipelines.ship: must be a list")
        return
    for i, rule in enumerate(ship):
        where = f"pipelines.ship[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for key in rule:
            if key not in {"when", "add", "flavor_by_kind"}:
                errors.append(f"{where}: unknown key {key!r}")
        when = rule.get("when")
        kinds = when.get("kind_any") if isinstance(when, dict) and set(when) == {"kind_any"} else None
        if not isinstance(kinds, list) or not kinds:
            errors.append(f"{where}.when: must be {{kind_any: [..]}}")
            kinds = []
        for kind in kinds:
            if kind not in kind_names:
                errors.append(f"{where}.when: unknown kind {kind!r}")
        if not isinstance(rule.get("add"), str):
            errors.append(f"{where}.add: required string")
        fbk = rule.get("flavor_by_kind", {})
        if not isinstance(fbk, dict):
            errors.append(f"{where}.flavor_by_kind: must be a mapping")
        else:
            for kind in fbk:
                if kind not in kind_names:
                    errors.append(f"{where}.flavor_by_kind: unknown kind {kind!r}")


def _check_flavors(flavors: dict, names: set[str], errors: list[str]) -> None:
    for key in flavors:
        if key != "verify":
            errors.append(f"flavors: unknown pipeline {key!r}")
    verify = flavors.get("verify", [])
    if not isinstance(verify, list):
        errors.append("flavors.verify: must be a list")
        return
    for i, rule in enumerate(verify):
        where = f"flavors.verify[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for key in rule:
            if key not in {"when", "use", "vars", "exclusive"}:
                errors.append(f"{where}: unknown key {key!r}")
        if "when" not in rule:
            errors.append(f"{where}.when: required")
        else:
            _check_cond(rule["when"], f"{where}.when", names, errors)
        if not isinstance(rule.get("use"), str):
            errors.append(f"{where}.use: required string")
        if "vars" in rule and not isinstance(rule["vars"], dict):
            errors.append(f"{where}.vars: must be a mapping")
        if "exclusive" in rule and not isinstance(rule["exclusive"], bool):
            errors.append(f"{where}.exclusive: must be true or false")


def _check_workspaces(workspaces: list, names: set[str], errors: list[str]) -> None:
    if not isinstance(workspaces, list):
        errors.append("workspaces: must be a list")
        return
    for i, rule in enumerate(workspaces):
        where = f"workspaces[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for key in rule:
            if key not in {"when", "tool", "affected", "units_from"}:
                errors.append(f"{where}: unknown key {key!r}")
        if "when" not in rule:
            errors.append(f"{where}.when: required")
        else:
            _check_cond(rule["when"], f"{where}.when", names, errors)
        if not isinstance(rule.get("tool"), str):
            errors.append(f"{where}.tool: required string")
        if "affected" in rule and not isinstance(rule["affected"], str):
            errors.append(f"{where}.affected: must be a string")
        if "units_from" in rule and rule["units_from"] not in UNIT_SOURCES:
            errors.append(f"{where}.units_from: must be one of {sorted(UNIT_SOURCES)}")


def _refs(cond):
    if isinstance(cond, str):
        yield cond
    elif isinstance(cond, dict):
        for op in COMBINATOR_KEYS:
            for item in cond.get(op, []):
                yield from _refs(item)


def _find_cycles(signals: dict) -> list[str]:
    errors: list[str] = []
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(name: str, trail: list[str]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            errors.append("signal cycle: " + " -> ".join([*trail, name]))
            return
        state[name] = 1
        for ref in _refs(signals[name]):
            visit(ref, [*trail, name])
        state[name] = 2

    for name in signals:
        visit(name, [])
    return errors
