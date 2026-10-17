"""Execute the shell inside verify.yml with stub tools on PATH.

GitHub Actions cannot run here, so this checks the logic that decides *which* commands
run, in what order, and when the job must fail. Real tool behaviour (npm, uv, go...) is
covered only by running the workflow on GitHub.
"""
import shutil
import unittest

from tests.workflow_helpers import StepRunner

INSTALL = "Install dependencies"
TASKS = "Run check, test and build"


class NodeInstall(unittest.TestCase):
    def run_install(self, pm, files=None, cwd=".", workspace_files=None, **inputs):
        runner = StepRunner(self)
        runner.write(files or {})
        proc = runner.run("node", INSTALL, {"pm": pm, **inputs}, cwd=cwd)
        return runner, proc

    def test_npm_ci_with_lockfile(self):
        runner, proc = self.run_install("npm", {"package-lock.json": "{}"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(runner.calls(), ["npm ci"])

    def test_npm_without_a_lockfile_fails_closed(self):
        runner, proc = self.run_install("npm", {"package.json": "{}"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no package-lock.json", proc.stdout)
        self.assertEqual(runner.calls(), [])

    def test_npm_without_a_lockfile_installs_only_when_opted_in(self):
        runner, proc = self.run_install("npm", {"package.json": "{}"}, **{"allow-unlocked": True})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(runner.calls(), ["npm install --no-audit --no-fund"])

    def test_npm_finds_lockfile_in_parent_directory(self):
        runner, _ = self.run_install("npm", {"package-lock.json": "{}", "pkgs/a/package.json": "{}"}, cwd="pkgs/a")
        self.assertEqual(runner.calls(), ["npm ci"])

    def test_ignore_scripts_flag(self):
        runner, _ = self.run_install("npm", {"package-lock.json": "{}"}, **{"ignore-scripts": True})
        self.assertEqual(runner.calls(), ["npm ci --ignore-scripts"])

    def test_pnpm_frozen_lockfile(self):
        runner, _ = self.run_install("pnpm", {"pnpm-lock.yaml": ""})
        self.assertEqual(runner.calls(), ["pnpm install --frozen-lockfile"])

    def test_bun_frozen_lockfile(self):
        runner, _ = self.run_install("bun", {"bun.lock": ""})
        self.assertEqual(runner.calls(), ["bun install --frozen-lockfile"])

    def test_yarn_classic_and_berry(self):
        runner = StepRunner(self)
        runner.write({"yarn.lock": ""})
        self.assertEqual(runner.run("node", INSTALL, {"pm": "yarn"}).returncode, 0)
        self.assertIn("yarn install --frozen-lockfile", runner.calls())
        runner = StepRunner(self)
        runner.write({"yarn.lock": ""})
        proc = runner.run("node", INSTALL, {"pm": "yarn"}, extra_env={"STUB_YARN_VERSION": "4.1.0"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("yarn install --immutable", runner.calls())

    def test_unknown_package_manager_fails_and_never_executes_the_value(self):
        runner = StepRunner(self)
        proc = runner.run("node", INSTALL, {"pm": "npm; touch pwned"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unsupported package manager", proc.stdout)
        self.assertFalse((runner.root / "pwned").exists())

    def test_install_failure_fails_the_step(self):
        runner = StepRunner(self)
        runner.write({"package-lock.json": "{}"})
        proc = runner.run("node", INSTALL, {"pm": "npm"}, extra_env={"STUB_FAIL": "npm"})
        self.assertNotEqual(proc.returncode, 0)


@unittest.skipUnless(shutil.which("node"), "node is needed to read package.json scripts")
class NodeTasks(unittest.TestCase):
    def run_tasks(self, scripts, **inputs):
        runner = StepRunner(self)
        import json

        runner.write({"package.json": json.dumps({"scripts": scripts})})
        proc = runner.run("node", TASKS, {"pm": "pnpm", **inputs})
        return runner, proc

    def test_runs_lint_typecheck_test_and_build_in_order(self):
        runner, proc = self.run_tasks({"lint": "x", "typecheck": "x", "test": "x", "build": "x"})
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(runner.calls(), ["pnpm run lint", "pnpm run typecheck", "pnpm run test", "pnpm run build"])

    def test_missing_tasks_are_skipped(self):
        runner, proc = self.run_tasks({"test": "vitest"})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(runner.calls(), ["pnpm run test"])
        self.assertIn("no check task found", proc.stdout)

    def test_fails_closed_when_nothing_ran(self):
        runner, proc = self.run_tasks({"start": "node ."})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("verify ran nothing", proc.stdout)
        self.assertEqual(runner.calls(), [])

    def test_allow_empty_opts_out(self):
        _, proc = self.run_tasks({}, **{"allow-empty": True})
        self.assertEqual(proc.returncode, 0)

    def test_npm_placeholder_test_script_is_not_a_test(self):
        runner, proc = self.run_tasks({"test": 'echo "Error: no test specified" && exit 1'})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(runner.calls(), [])

    def test_command_override_replaces_the_default(self):
        runner, proc = self.run_tasks({"test": "x", "build": "x"}, **{"test-command": "echo custom-test > custom.txt"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(runner.calls(), ["pnpm run build"])
        self.assertTrue((runner.root / "custom.txt").exists())

    def test_failing_task_fails_the_step_and_stops(self):
        runner = StepRunner(self)
        runner.write({"package.json": '{"scripts": {"lint": "x", "test": "x"}}'})
        proc = runner.run("node", TASKS, {"pm": "pnpm"}, extra_env={"STUB_FAIL": "pnpm"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(runner.calls(), ["pnpm run lint"])


class PythonSteps(unittest.TestCase):
    def install(self, pm, files):
        runner = StepRunner(self)
        runner.write(files)
        proc = runner.run("python", INSTALL, {"pm": pm})
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return runner.calls()

    def test_uv_sync_frozen(self):
        self.assertEqual(self.install("uv", {"uv.lock": ""}), ["uv sync --frozen"])

    def test_poetry_is_pinned(self):
        self.assertEqual(self.install("poetry", {}), ["pipx install poetry==2.1.3", "poetry install --no-interaction"])

    def test_pip_requirements_files(self):
        calls = self.install("pip", {"requirements.txt": "", "requirements-dev.txt": ""})
        self.assertEqual(calls, ["python -m pip install -r requirements.txt", "python -m pip install -r requirements-dev.txt"])

    def test_pip_editable_install_with_and_without_dev_extra(self):
        self.assertEqual(self.install("pip", {"pyproject.toml": "[project]\nname='x'\n"}), ["python -m pip install -e ."])
        with_dev = "[project]\nname='x'\n[project.optional-dependencies]\ndev = ['pytest']\n"
        self.assertEqual(self.install("pip", {"pyproject.toml": with_dev}), ["python -m pip install -e .[dev]"])

    def test_unknown_package_manager_fails(self):
        runner = StepRunner(self)
        self.assertNotEqual(runner.run("python", INSTALL, {"pm": "conda"}).returncode, 0)

    def tasks(self, pm, files, **inputs):
        runner = StepRunner(self)
        runner.write(files)
        proc = runner.run("python", TASKS, {"pm": pm, **inputs})
        return runner.calls(), proc

    def test_uv_runs_configured_tools_then_builds(self):
        pyproject = "[project]\nname='x'\n[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n"
        calls, proc = self.tasks("uv", {"pyproject.toml": pyproject, "tests/test_x.py": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(calls, ["uv run ruff check .", "uv run mypy .", "uv run python -m pytest -q", "uv build"])

    def test_library_with_no_config_still_builds(self):
        calls, proc = self.tasks("uv", {"pyproject.toml": "[project]\nname='x'\n"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(calls, ["uv build"])

    def test_pip_with_no_tests_or_config_fails_closed(self):
        calls, proc = self.tasks("pip", {"pyproject.toml": "[project]\nname='x'\n"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("verify ran nothing", proc.stdout)
        self.assertEqual(calls, [])

    def test_pip_runs_pytest_when_tests_exist(self):
        calls, proc = self.tasks("pip", {"tests/test_x.py": ""})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(calls, ["python -m pytest -q"])

    def test_poetry_prefix(self):
        calls, _ = self.tasks("poetry", {"tests/test_x.py": ""})
        self.assertEqual(calls, ["poetry run python -m pytest -q", "poetry build"])


class GoSteps(unittest.TestCase):
    def run_tasks(self, extra_env=None, **inputs):
        runner = StepRunner(self)
        proc = runner.run("go", TASKS, inputs, extra_env=extra_env)
        return runner.calls(), proc

    def test_default_sequence(self):
        calls, proc = self.run_tasks()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(calls, ["gofmt -l .", "go vet ./...", "go test ./...", "go build ./..."])

    def test_unformatted_code_fails_before_vet(self):
        calls, proc = self.run_tasks(extra_env={"STUB_GOFMT_OUTPUT": "main.go"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("gofmt needed", proc.stdout)
        self.assertEqual(calls, ["gofmt -l ."])

    def test_override_replaces_test(self):
        calls, proc = self.run_tasks(**{"test-command": "echo custom"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(calls, ["gofmt -l .", "go vet ./...", "go build ./..."])


@unittest.skipUnless(shutil.which("python3"), "python3 is needed to read mise.toml")
class GenericSteps(unittest.TestCase):
    def run_tasks(self, files, cwd=".", **inputs):
        runner = StepRunner(self)
        runner.write(files)
        proc = runner.run("generic", TASKS, inputs, cwd=cwd)
        return runner.calls(), proc

    def test_runs_only_the_defined_tasks(self):
        calls, proc = self.run_tasks({"mise.toml": "[tasks.check]\nrun='x'\n[tasks.build]\nrun='x'\n"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(calls, ["mise run check", "mise run build"])

    def test_no_tasks_fails_closed(self):
        calls, proc = self.run_tasks({"mise.toml": "[tools]\nnode='22'\n"})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(calls, [])

    def test_finds_mise_toml_in_a_parent_directory(self):
        calls, proc = self.run_tasks({"mise.toml": "[tasks.test]\nrun='x'\n", "pkgs/a/readme": ""}, cwd="pkgs/a")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(calls, ["mise run test"])


if __name__ == "__main__":
    unittest.main()
