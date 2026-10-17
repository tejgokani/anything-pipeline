"""Shared helpers for tests that inspect or execute workflow YAML."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
VERIFY_WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"
SHA_PIN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
INPUT_EXPR = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}")


def load_workflow(path: Path = VERIFY_WORKFLOW) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(workflow: dict) -> dict:
    """PyYAML reads the bare key `on` as boolean True."""
    return workflow.get(True) or workflow.get("on") or {}


def all_steps(workflow: dict):
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield job_id, step


def find_step(workflow: dict, job: str, name: str) -> dict:
    for step in workflow["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise KeyError(f"{job}/{name}")


def workflow_input_defaults(workflow: dict) -> dict:
    inputs = triggers(workflow)["workflow_call"]["inputs"]
    return {name: spec.get("default", "") for name, spec in inputs.items()}


def expand_inputs(text: str, inputs: dict) -> str:
    def replace(match):
        value = inputs[match.group(1)]
        return str(value).lower() if isinstance(value, bool) else str(value)

    return INPUT_EXPR.sub(replace, str(text))


STUB = """#!/bin/bash
# Records its invocation. Controls: STUB_FAIL=<name>, STUB_YARN_VERSION, STUB_GOFMT_OUTPUT.
name=$(basename "$0")
echo "$name $*" >> "$STUB_LOG"
if [ "${STUB_FAIL:-}" = "$name" ]; then exit 1; fi
if [ "$name" = yarn ] && [ "${1:-}" = "--version" ]; then echo "${STUB_YARN_VERSION:-1.22.22}"; fi
if [ "$name" = gofmt ] && [ -n "${STUB_GOFMT_OUTPUT:-}" ]; then echo "$STUB_GOFMT_OUTPUT"; fi
exit 0
"""
STUBBED = [
    "npm", "pnpm", "yarn", "bun", "corepack", "uv", "poetry", "pipx", "python",
    "go", "gofmt", "mise", "ruff", "mypy",
]


class StepRunner:
    """Run a `run:` block from verify.yml in a temp directory with stub tools on PATH."""

    def __init__(self, test_case, workflow: dict | None = None):
        self.workflow = workflow or load_workflow()
        self.tmp = Path(tempfile.mkdtemp(prefix="ap-step-")).resolve()
        test_case.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "workspace"
        self.root.mkdir()
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        for name in STUBBED:
            path = self.stubs / name
            path.write_text(STUB)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "calls.log"
        self.log.write_text("")

    def write(self, files: dict) -> None:
        for rel, content in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line]

    def run(self, job: str, step: str, inputs: dict | None = None, cwd: str = ".", extra_env: dict | None = None):
        values = {**workflow_input_defaults(self.workflow), **(inputs or {})}
        spec = find_step(self.workflow, job, step)
        env = {
            "PATH": f"{self.stubs}:{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "GITHUB_WORKSPACE": str(self.root),
            "STUB_LOG": str(self.log),
            **{k: expand_inputs(v, values) for k, v in (spec.get("env") or {}).items()},
            **(extra_env or {}),
        }
        return subprocess.run(
            [os.environ.get("AP_TEST_BASH", "/bin/bash"), "-c", spec["run"]],
            cwd=self.root / cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
