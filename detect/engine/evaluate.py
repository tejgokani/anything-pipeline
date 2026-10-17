"""Evaluate rule conditions against one unit directory, collecting evidence."""
from __future__ import annotations

from typing import NamedTuple

from .matchers import RepoFS, join, lookup, path_keys
from .rules import COMBINATOR_KEYS, Rules


class Result(NamedTuple):
    ok: bool
    evidence: tuple = ()


FALSE = Result(False, ())


def describe(cond) -> str:
    """Compact human-readable form of a condition, for --explain."""
    if isinstance(cond, str):
        return cond
    parts = []
    for key, arg in cond.items():
        if key == "search_up":
            continue
        if key in COMBINATOR_KEYS:
            parts.append(f"{key}[{', '.join(describe(item) for item in arg)}]")
        elif key in ("json", "toml"):
            text = f"{key}:{arg['file']}#{'.'.join(path_keys(arg['path']))}"
            if "equals" in arg:
                text += f" == {arg['equals']!r}"
            elif arg.get("exists", True) is False:
                text += " (absent)"
            parts.append(text)
        elif key == "file_any":
            parts.append("file_any:" + "|".join(arg))
        else:
            parts.append(f"{key}:{arg}")
    return " ".join(parts)


class Evaluator:
    def __init__(self, fs: RepoFS, rules: Rules, unit: str = "."):
        self.fs = fs
        self.rules = rules
        self.unit = unit or "."
        self._signals: dict[str, Result] = {}

    def signal(self, name: str) -> Result:
        if name not in self._signals:
            self._signals[name] = self.eval(self.rules.signals[name])
        return self._signals[name]

    def eval(self, cond) -> Result:
        if isinstance(cond, str):
            return self.signal(cond)
        search_up = bool(cond.get("search_up", False))
        evidence: list[str] = []
        for key, arg in cond.items():
            if key == "search_up":
                continue
            if key == "all":
                results = [self.eval(item) for item in arg]
                ok = all(r.ok for r in results)
                found = [e for r in results if r.ok for e in r.evidence]
            elif key == "any":
                results = [self.eval(item) for item in arg]
                ok = any(r.ok for r in results)
                found = [e for r in results if r.ok for e in r.evidence]
            elif key == "none":
                ok = not any(self.eval(item).ok for item in arg)
                found = []
            else:
                leaf = self._leaf(key, arg, search_up)
                ok, found = leaf.ok, list(leaf.evidence)
            if not ok:
                return FALSE
            evidence.extend(found)
        return Result(True, tuple(dict.fromkeys(evidence)))

    def _dirs(self, search_up: bool) -> list[str]:
        if not search_up or self.unit == ".":
            return [self.unit]
        parts = self.unit.split("/")
        return ["/".join(parts[:i]) for i in range(len(parts), 0, -1)] + ["."]

    def _leaf(self, key: str, arg, search_up: bool) -> Result:
        for directory in self._dirs(search_up):
            result = self._leaf_in(key, arg, directory)
            if result.ok:
                return result
        return FALSE

    def _leaf_in(self, key: str, arg, directory: str) -> Result:
        fs = self.fs
        if key == "file":
            rel = join(directory, arg)
            return Result(True, (rel,)) if fs.is_file(rel) else FALSE
        if key == "file_any":
            for name in arg:
                rel = join(directory, name)
                if fs.is_file(rel):
                    return Result(True, (rel,))
            return FALSE
        if key == "glob":
            hits = fs.glob(directory, arg)
            return Result(True, (hits[0],)) if hits else FALSE
        # json / toml: a missing or unparsable file never matches, even for exists: false.
        rel = join(directory, arg["file"])
        data = fs.read_json(rel) if key == "json" else fs.read_toml(rel)
        if data is None:
            return FALSE
        found, value = lookup(data, arg["path"])
        label = f"{rel}#{'.'.join(path_keys(arg['path']))}"
        if "equals" in arg:
            ok = found and value == arg["equals"]
        else:
            wanted = arg.get("exists", True)
            ok = found == wanted
            if ok and not wanted:
                label += " (absent)"
        return Result(True, (label,)) if ok else FALSE
