"""Split a monorepo into units using the workspace rules in rules.yml."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from .evaluate import Evaluator, describe
from .matchers import RepoFS, glob_to_regex, join
from .rules import Rules


@dataclass
class Workspace:
    tools: list = field(default_factory=list)
    unit_source: str | None = None
    affected: str | None = None
    units: list | None = None  # None means the repo root is the only unit
    problems: list = field(default_factory=list)
    matched: list = field(default_factory=list)  # explain data


def discover(fs: RepoFS, rules: Rules, root_eval: Evaluator) -> Workspace | None:
    matched = []
    for rule in rules.workspaces:
        result = root_eval.eval(rule["when"])
        if result.ok:
            matched.append((rule, result))
    if not matched:
        return None

    ws = Workspace(tools=[rule["tool"] for rule, _ in matched])
    ws.matched = [
        {"tool": rule["tool"], "when": describe(rule["when"]), "evidence": list(result.evidence)}
        for rule, result in matched
    ]
    for rule, _ in matched:
        if ws.affected is None and rule.get("affected"):
            ws.affected = rule["affected"]
    for rule, _ in matched:
        source = rule.get("units_from")
        if not source:
            continue
        ws.unit_source = source
        members = _members(fs, source)
        if members:
            ws.units = members
        else:
            ws.problems.append(f"no workspace members resolved from {source}")
        break
    return ws


def _members(fs: RepoFS, source: str) -> list:
    text = fs.read_text(source)
    if text is None:
        return []
    if source == "go.work":
        return _go_members(fs, parse_go_work(text))
    patterns = parse_pnpm_workspace(text) if source == "pnpm-workspace.yaml" else parse_package_workspaces(text)
    return _node_members(fs, patterns)


def parse_pnpm_workspace(text: str) -> list:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    packages = data.get("packages") if isinstance(data, dict) else None
    return [p for p in packages if isinstance(p, str)] if isinstance(packages, list) else []


def parse_package_workspaces(text: str) -> list:
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return []
    workspaces = data.get("workspaces") if isinstance(data, dict) else None
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    return [p for p in workspaces if isinstance(p, str)] if isinstance(workspaces, list) else []


def parse_go_work(text: str) -> list:
    uses: list = []
    block = None  # "use" or "other" while inside a parenthesised block
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if block:
            if line == ")":
                block = None
            elif block == "use":
                uses.append(line.strip('"'))
            continue
        if line.endswith("("):
            block = "use" if line.split()[0] == "use" else "other"
        elif line.startswith("use ") or line.startswith("use\t"):
            uses.append(line[3:].strip().strip('"'))
    return uses


def _normalise(pattern: str) -> str:
    return re.sub(r"^\./", "", pattern).rstrip("/") or "."


def _node_members(fs: RepoFS, patterns: list) -> list:
    includes = [_normalise(p) for p in patterns if not p.startswith("!")]
    excludes = [_normalise(p[1:]) for p in patterns if p.startswith("!")]
    _, dirs = fs.walk(".")
    members = []
    for directory in dirs:
        if not any(glob_to_regex(p).match(directory) for p in includes):
            continue
        if any(glob_to_regex(p).match(directory) for p in excludes):
            continue
        if fs.is_file(join(directory, "package.json")):
            members.append(directory)
    return sorted(set(members))


def _go_members(fs: RepoFS, uses: list) -> list:
    members = []
    for entry in uses:
        directory = _normalise(entry)
        if fs.is_dir(directory) and fs.is_file(join(directory, "go.mod")):
            members.append(directory)
    return sorted(set(members))
