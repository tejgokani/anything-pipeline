"""Turn rules + a repository into a plan: units, kinds, verify flavor, ship pipeline."""
from __future__ import annotations

from pathlib import Path

from .evaluate import Evaluator, describe
from .matchers import RepoFS
from .rules import Rules, load_rules
from .workspaces import discover

PLAN_VERSION = 1


def detect(repo: str | Path, rules: Rules | None = None, explain: bool = False) -> dict:
    """Detect pipelines for `repo`. Pure function of the file tree.

    With explain=True each unit also carries the evidence behind every decision.
    """
    rules = rules or load_rules()
    fs = RepoFS(repo, rules.ignore_dirs)
    root_eval = Evaluator(fs, rules, ".")
    workspace = discover(fs, rules, root_eval)
    paths = workspace.units if workspace and workspace.units else ["."]

    units = [_detect_unit(fs, rules, path, explain) for path in paths]
    problems = [f"workspace: {p}" for p in workspace.problems] if workspace else []

    workspace_out = None
    if workspace:
        workspace_out = {
            "tools": workspace.tools,
            "unit_source": workspace.unit_source,
            "affected": workspace.affected,
        }
        if explain:
            workspace_out["explain"] = workspace.matched

    return {
        "version": PLAN_VERSION,
        "ambiguous": bool(problems) or any(u["ambiguous"] for u in units),
        "problems": problems,
        "always": list(rules.always),
        "workspace": workspace_out,
        "units": units,
    }


def _detect_unit(fs: RepoFS, rules: Rules, path: str, explain: bool) -> dict:
    ev = Evaluator(fs, rules, path)

    kinds: list[str] = []
    kinds_why: dict = {}
    for name, cond in rules.kinds.items():
        result = ev.eval(cond)
        if result.ok:
            kinds.append(name)
            kinds_why[name] = {"rule": describe(cond), "evidence": list(result.evidence)}

    verify, verify_why, verify_reasons = _pick_verify(ev, rules)
    ship, ship_why, ship_reasons = _pick_ship(rules, kinds)

    reasons: list[str] = []
    if not kinds:
        reasons.append("no kind matched")
    reasons += verify_reasons + ship_reasons
    if (
        "mise_file" in rules.signals
        and "mise_tasks" in rules.signals
        and ev.signal("mise_file").ok
        and not ev.signal("mise_tasks").ok
    ):
        reasons.append("mise.toml defines none of check, test, build")

    unit = {
        "path": path,
        "kinds": kinds,
        "verify": verify,
        "ship": ship,
        "ambiguous": bool(reasons),
        "reasons": reasons,
    }
    if explain:
        unit["explain"] = {
            "signals": {n: list(ev.signal(n).evidence) for n in rules.signals if ev.signal(n).ok},
            "kinds": kinds_why,
            "verify": verify_why,
            "ship": ship_why,
        }
    return unit


def _pick_verify(ev: Evaluator, rules: Rules):
    chosen: dict = {}  # language -> (index, rule, result); first match per language wins
    for index, rule in enumerate(rules.verify_flavors):
        result = ev.eval(rule["when"])
        if not result.ok:
            continue
        if rule.get("exclusive"):
            return _verify_entry(rule), _verify_why(index, rule, result), []
        chosen.setdefault(rule["use"], (index, rule, result))
    if not chosen:
        return None, None, ["no verify flavor matched"]
    if len(chosen) > 1:
        return None, None, ["multiple languages matched: " + ", ".join(sorted(chosen))]
    index, rule, result = next(iter(chosen.values()))
    return _verify_entry(rule), _verify_why(index, rule, result), []


def _verify_entry(rule: dict) -> dict:
    return {"flavor": rule["use"], "vars": dict(rule.get("vars", {}))}


def _verify_why(index: int, rule: dict, result) -> dict:
    return {"rule": f"flavors.verify[{index}]", "when": describe(rule["when"]), "evidence": list(result.evidence)}


def _pick_ship(rules: Rules, kinds: list):
    candidates: dict = {}  # (pipeline, flavor) -> (rule index, kinds)
    for index, rule in enumerate(rules.ship):
        wanted = [k for k in kinds if k in rule["when"]["kind_any"]]
        for kind in wanted:
            flavor = rule.get("flavor_by_kind", {}).get(kind)
            entry = candidates.setdefault((rule["add"], flavor), (index, []))
            entry[1].append(kind)
    if not candidates:
        return None, None, []
    if len(candidates) > 1:
        names = sorted(f"{p}/{f}" if f else p for p, f in candidates)
        return None, None, ["conflicting ship pipelines: " + ", ".join(names)]
    (pipeline, flavor), (index, via) = next(iter(candidates.items()))
    return (
        {"pipeline": pipeline, "flavor": flavor},
        {"rule": f"pipelines.ship[{index}]", "via_kinds": via},
        [],
    )


def render_text(plan: dict, root: str = ".") -> str:
    """Human-readable plan. Evidence is shown when the plan was built with explain=True."""
    lines = [f"anything-pipeline detect: {root}"]
    ws = plan["workspace"]
    if ws:
        lines.append(f"workspace: {' + '.join(ws['tools'])}" + (f" (units from {ws['unit_source']})" if ws["unit_source"] else ""))
        if ws["affected"]:
            lines.append(f"  affected: {ws['affected']}")
        for item in ws.get("explain", []):
            lines.append(f"  {item['tool']}: {item['when']}  <- {', '.join(item['evidence'])}")
    for problem in plan["problems"]:
        lines.append(f"PROBLEM: {problem}")

    for unit in plan["units"]:
        lines.append("")
        lines.append(f"unit {unit['path']}")
        why = unit.get("explain")
        if why:
            lines.append("  signals:")
            for name, evidence in why["signals"].items():
                lines.append(f"    {name:<16} {', '.join(evidence)}")
        lines.append("  kinds: " + (", ".join(unit["kinds"]) or "(none)"))
        if why:
            for name, info in why["kinds"].items():
                lines.append(f"    {name:<16} {info['rule']}")
        verify = unit["verify"]
        if verify:
            vars_text = ", ".join(f"{k}={v}" for k, v in verify["vars"].items())
            lines.append(f"  verify: {verify['flavor']}" + (f" ({vars_text})" if vars_text else ""))
            if why and why["verify"]:
                lines.append(f"    {why['verify']['rule']}: {why['verify']['when']}")
        else:
            lines.append("  verify: (undecided)")
        ship = unit["ship"]
        if ship:
            lines.append(f"  ship: {ship['pipeline']}" + (f"/{ship['flavor']}" if ship["flavor"] else ""))
            if why and why["ship"]:
                lines.append(f"    {why['ship']['rule']} via {', '.join(why['ship']['via_kinds'])}")
        else:
            lines.append("  ship: (none)")
        if unit["ambiguous"]:
            lines.append("  AMBIGUOUS:")
            lines.extend(f"    - {reason}" for reason in unit["reasons"])

    lines.append("")
    lines.append("always: " + ", ".join(plan["always"]))
    lines.append("result: " + ("AMBIGUOUS (needs a decision)" if plan["ambiguous"] else "clear"))
    return "\n".join(lines)
