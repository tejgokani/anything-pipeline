"""Read-only view of a repository plus path helpers.

Nothing here executes repo code, follows symlinks out of the repo root, or touches
the network. Files are only parsed as JSON, TOML or YAML text.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from functools import lru_cache
from pathlib import Path

MAX_READ_BYTES = 5 * 1024 * 1024


def join(directory: str, name: str) -> str:
    """Join a repo-relative directory ('.' for the root) and a relative name."""
    return name if directory in ("", ".") else f"{directory}/{name}"


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a glob (`*`, `?`, `**`, `**/`) to an anchored regex over posix paths."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def path_keys(path) -> list[str]:
    if isinstance(path, list):
        return list(path)
    return [k for k in path.removeprefix("$.").split(".") if k]


def lookup(data, path) -> tuple[bool, object]:
    """Resolve a dotted path (or list of keys) in parsed JSON/TOML. Returns (found, value)."""
    cur = data
    for key in path_keys(path):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return False, None
    return True, cur


class RepoFS:
    def __init__(self, root: str | Path, ignore_dirs):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"not a directory: {root}")
        self.ignore = frozenset(ignore_dirs)
        self._tree: dict[str, tuple[list[str], list[str]]] = {}
        self._json: dict[str, object] = {}
        self._toml: dict[str, object] = {}

    def _safe(self, rel: str) -> Path | None:
        """Resolve a repo-relative path; None if it escapes the repo root."""
        try:
            resolved = (self.root / rel).resolve()
        except (OSError, RuntimeError):
            return None
        if resolved == self.root or self.root in resolved.parents:
            return resolved
        return None

    def is_file(self, rel: str) -> bool:
        path = self._safe(rel)
        return bool(path and path.is_file())

    def is_dir(self, rel: str) -> bool:
        path = self._safe(rel)
        return bool(path and path.is_dir())

    def read_text(self, rel: str) -> str | None:
        path = self._safe(rel)
        if not path or not path.is_file():
            return None
        try:
            if path.stat().st_size > MAX_READ_BYTES:
                return None
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def read_json(self, rel: str):
        if rel not in self._json:
            text = self.read_text(rel)
            try:
                self._json[rel] = None if text is None else json.loads(text)
            except (ValueError, RecursionError):  # RecursionError: absurdly nested input
                self._json[rel] = None
        return self._json[rel]

    def read_toml(self, rel: str):
        if rel not in self._toml:
            text = self.read_text(rel)
            try:
                self._toml[rel] = None if text is None else tomllib.loads(text)
            except (tomllib.TOMLDecodeError, RecursionError):
                self._toml[rel] = None
        return self._toml[rel]

    def walk(self, directory: str) -> tuple[list[str], list[str]]:
        """Files and directories under `directory`, as posix paths relative to it.

        Ignored directories are pruned and symlinked directories are not followed.
        """
        if directory not in self._tree:
            files: list[str] = []
            dirs: list[str] = []
            base = self._safe(directory)
            if base and base.is_dir():
                for dirpath, dirnames, filenames in os.walk(base):
                    dirnames[:] = sorted(d for d in dirnames if d not in self.ignore)
                    rel = Path(dirpath).relative_to(base).as_posix()
                    prefix = "" if rel == "." else rel + "/"
                    dirs.extend(prefix + d for d in dirnames)
                    files.extend(prefix + f for f in sorted(filenames))
            self._tree[directory] = (files, dirs)
        return self._tree[directory]

    def glob(self, directory: str, pattern: str) -> list[str]:
        regex = glob_to_regex(pattern)
        files, _ = self.walk(directory)
        return [join(directory, f) for f in files if regex.match(f)]
