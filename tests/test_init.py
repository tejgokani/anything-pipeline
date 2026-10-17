"""`ap init`: rendering, overrides, and every refusal path."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from render import InitError, run_init
from render.planfile import PLAN_FILE
from tests.test_workflow_invariants import assert_workflow_invariants

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
AP = ROOT / "cli" / "ap.py"
SHA = "b" * 40
WORKFLOW = ".github/workflows/verify.yml"


class InitCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def fixture(self, name: str) -> Path:
        dest = self.tmp / name
        shutil.copytree(FIXTURES / name, dest)
        (dest / "expected.plan.json").unlink()
        return dest

    def repo(self, files: dict) -> Path:
        dest = self.tmp / "repo"
        for rel, content in files.items():
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content if isinstance(content, str) else json.dumps(content))
        return dest

    def init(self, path, **kwargs):
        kwargs.setdefault("ref", SHA)
        return run_init(path, **kwargs)

    def read(self, repo: Path, rel: str) -> str:
        return (repo / rel).read_text()


class RenderTests(InitCase):
    def test_writes_workflow_and_plan_file(self):
        repo = self.fixture("npm-library")
        result = self.init(repo, ref_label="v0.1.0")
        self.assertEqual(sorted(result.written), sorted([WORKFLOW, PLAN_FILE]))
        text = self.read(repo, WORKFLOW)
        workflow = yaml.safe_load(text)
        assert_workflow_invariants(self, workflow, text)
        self.assertIn(f"tejgokani/anything-pipeline/.github/workflows/verify.yml@{SHA} # v0.1.0", text)
        self.assertEqual(workflow["jobs"]["verify"]["with"], {"flavor": "node", "pm": "npm", "working-directory": "."})

    def test_monorepo_gets_one_job_per_unit_and_one_gate(self):
        repo = self.fixture("pnpm-turbo-monorepo")
        self.init(repo)
        workflow = yaml.safe_load(self.read(repo, WORKFLOW))
        self.assertEqual(list(workflow["jobs"]), ["verify-apps-web", "verify-packages-ui", "gate"])
        self.assertEqual(workflow["jobs"]["gate"]["needs"], ["verify-apps-web", "verify-packages-ui"])
        self.assertEqual(workflow["jobs"]["verify-packages-ui"]["with"]["working-directory"], "packages/ui")

    def test_every_clear_fixture_renders_valid_workflows(self):
        for name in ("npm-library", "next-app-vercel", "python-uv-library", "go-service-container", "pnpm-turbo-monorepo"):
            with self.subTest(fixture=name):
                dest = self.tmp / name
                repo = self.fixture(name)
                self.init(repo)
                text = self.read(repo, WORKFLOW)
                assert_workflow_invariants(self, yaml.safe_load(text), text)
                shutil.rmtree(dest)

    def test_plan_file_records_the_plan_and_template_pin(self):
        repo = self.fixture("python-uv-library")
        self.init(repo)
        data = yaml.safe_load(self.read(repo, PLAN_FILE))
        self.assertEqual(data["template"], {"repo": "tejgokani/anything-pipeline", "ref": SHA})
        self.assertEqual(data["plan"]["units"][0]["verify"], {"flavor": "python", "vars": {"pm": "uv"}})
        self.assertEqual(data["overrides"], {})

    def test_dry_run_writes_nothing_and_needs_no_ref(self):
        repo = self.fixture("npm-library")
        result = run_init(repo, ref=None, dry_run=True)
        self.assertEqual(result.written, [])
        self.assertIn("REPLACE_WITH_40_HEX_COMMIT_SHA", result.files[WORKFLOW])
        self.assertFalse((repo / ".github").exists())

    def test_rerun_is_idempotent_for_an_unedited_workflow(self):
        repo = self.fixture("go-service-container")
        self.init(repo)
        first = self.read(repo, WORKFLOW), self.read(repo, PLAN_FILE)
        self.init(repo)
        self.assertEqual((self.read(repo, WORKFLOW), self.read(repo, PLAN_FILE)), first)

    def test_next_pnpm_app_without_package_manager_gets_a_note(self):
        repo = self.fixture("next-app-vercel")
        result = self.init(repo, dry_run=True)
        self.assertTrue(any("packageManager" in n for n in result.notes))

    def test_default_branch_is_configurable(self):
        repo = self.fixture("npm-library")
        self.init(repo, default_branch="trunk")
        self.assertEqual(yaml.safe_load(self.read(repo, WORKFLOW))[True]["push"]["branches"], ["trunk"])


class OverrideTests(InitCase):
    def test_ambiguous_plan_is_blocked_and_writes_nothing(self):
        repo = self.fixture("ambiguous-polyglot")
        result = self.init(repo)
        self.assertEqual(result.written, [])
        self.assertEqual(len(result.blocked), 1)
        self.assertIn("multiple languages matched", result.blocked[0])
        self.assertFalse((repo / ".github").exists())
        self.assertFalse((repo / PLAN_FILE).exists())

    def test_override_resolves_ambiguity_and_survives_a_rerun(self):
        repo = self.fixture("ambiguous-polyglot")
        override = {"overrides": {"units": {".": {"verify": {"flavor": "node", "vars": {"pm": "npm"}}}}}}
        (repo / PLAN_FILE).write_text(yaml.safe_dump(override))
        self.init(repo)
        workflow = yaml.safe_load(self.read(repo, WORKFLOW))
        self.assertEqual(workflow["jobs"]["verify"]["with"]["flavor"], "node")
        self.init(repo)  # second run keeps the override
        kept = yaml.safe_load(self.read(repo, PLAN_FILE))
        self.assertEqual(kept["overrides"], override["overrides"])

    def test_input_overrides_are_rendered(self):
        repo = self.fixture("npm-library")
        overrides = {
            "overrides": {
                "verify": {"node-version": "22"},
                "units": {".": {"inputs": {"test-command": "npm run test:ci", "ignore-scripts": True}}},
            }
        }
        (repo / PLAN_FILE).write_text(yaml.safe_dump(overrides))
        self.init(repo)
        with_ = yaml.safe_load(self.read(repo, WORKFLOW))["jobs"]["verify"]["with"]
        self.assertEqual(with_["node-version"], "22")
        self.assertEqual(with_["test-command"], "npm run test:ci")
        self.assertIs(with_["ignore-scripts"], True)

    def test_disabled_verify_renders_no_workflow(self):
        repo = self.fixture("npm-library")
        (repo / PLAN_FILE).write_text(yaml.safe_dump({"disabled": ["verify"]}))
        result = self.init(repo)
        self.assertEqual(result.written, [PLAN_FILE])
        self.assertFalse((repo / WORKFLOW).exists())

    def assertRejected(self, repo, content, fragment):
        (repo / PLAN_FILE).write_text(content if isinstance(content, str) else yaml.safe_dump(content))
        with self.assertRaises(InitError) as ctx:
            self.init(repo)
        self.assertIn(fragment, str(ctx.exception))
        self.assertFalse((repo / WORKFLOW).exists())

    def test_expression_injection_in_an_override_is_rejected(self):
        repo = self.fixture("npm-library")
        self.assertRejected(
            repo, {"overrides": {"verify": {"test-command": "echo ${{ secrets.TOKEN }}"}}}, "without '${{'"
        )

    def test_multiline_override_is_rejected(self):
        repo = self.fixture("npm-library")
        self.assertRejected(repo, {"overrides": {"verify": {"test-command": "a\nb"}}}, "single line")

    def test_unknown_input_unknown_key_and_wrong_type_are_rejected(self):
        repo = self.fixture("npm-library")
        self.assertRejected(repo, {"overrides": {"verify": {"runs-on": "self-hosted"}}}, "unknown input 'runs-on'")
        self.assertRejected(repo, {"surprise": 1}, "unknown top-level key")
        self.assertRejected(repo, {"overrides": {"verify": {"ignore-scripts": "yes"}}}, "must be bool")

    def test_invalid_flavor_or_package_manager_override_is_rejected(self):
        repo = self.fixture("npm-library")
        self.assertRejected(
            repo, {"overrides": {"units": {".": {"verify": {"flavor": "ruby"}}}}}, "flavor: must be one of"
        )
        self.assertRejected(
            repo, {"overrides": {"units": {".": {"verify": {"flavor": "node", "vars": {"pm": "uv"}}}}}}, "vars.pm"
        )

    def test_malformed_yaml_is_an_error_not_a_crash(self):
        repo = self.fixture("npm-library")
        self.assertRejected(repo, "overrides: [unclosed", PLAN_FILE)

    def test_override_for_a_missing_unit_is_reported(self):
        repo = self.fixture("npm-library")
        (repo / PLAN_FILE).write_text(yaml.safe_dump({"overrides": {"units": {"gone": {"inputs": {}}}}}))
        result = self.init(repo, dry_run=True)
        self.assertTrue(any("gone" in n for n in result.notes))


class RefusalTests(InitCase):
    def test_ref_is_required_unless_dry_run(self):
        with self.assertRaisesRegex(InitError, "--ref is required"):
            run_init(self.fixture("npm-library"), ref=None)

    def test_tag_is_refused_unless_explicitly_allowed(self):
        repo = self.fixture("npm-library")
        with self.assertRaisesRegex(InitError, "not a full 40-character commit SHA"):
            run_init(repo, ref="v1.0.0")
        run_init(repo, ref="v1.0.0", allow_unpinned=True)
        self.assertIn("@v1.0.0", self.read(repo, WORKFLOW))

    def test_hostile_arguments_are_refused(self):
        repo = self.fixture("npm-library")
        for kwargs in (
            {"ref": "v1; rm -rf /", "allow_unpinned": True},
            {"kit_repo": "no-slash"},
            {"kit_repo": "a/b c"},
            {"ref_label": "v1\ninjected: true"},
            {"default_branch": "main]\n  evil:"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InitError):
                    self.init(repo, **kwargs)
        self.assertFalse((repo / ".github").exists())

    def test_foreign_workflow_is_not_overwritten(self):
        repo = self.fixture("npm-library")
        (repo / ".github" / "workflows").mkdir(parents=True)
        (repo / WORKFLOW).write_text("name: mine\n")
        with self.assertRaisesRegex(InitError, "not generated by anything-pipeline"):
            self.init(repo)
        self.assertEqual(self.read(repo, WORKFLOW), "name: mine\n")
        self.assertFalse((repo / PLAN_FILE).exists())  # nothing is written when any target is refused
        self.init(repo, force=True)
        self.assertIn("Generated by anything-pipeline", self.read(repo, WORKFLOW))

    def test_edited_generated_workflow_is_protected(self):
        repo = self.fixture("npm-library")
        self.init(repo)
        edited = self.read(repo, WORKFLOW) + "# my tweak\n"
        (repo / WORKFLOW).write_text(edited)
        with self.assertRaisesRegex(InitError, "edited after it was generated"):
            self.init(repo)
        self.assertEqual(self.read(repo, WORKFLOW), edited)
        self.init(repo, force=True)
        self.assertNotIn("my tweak", self.read(repo, WORKFLOW))

    def test_unsafe_unit_paths_are_blocked(self):
        repo = self.repo(
            {
                "pnpm-workspace.yaml": "packages:\n  - 'pkgs/*'\n",
                "pkgs/good/package.json": {"name": "good", "version": "1"},
                "pkgs/bad dir/package.json": {"name": "bad", "version": "1"},
                "pkgs/x$y/package.json": {"name": "x", "version": "1"},
                "pnpm-lock.yaml": "",
            }
        )
        result = self.init(repo)
        self.assertEqual(result.written, [])
        self.assertEqual(sum("not allowed in generated workflows" in b for b in result.blocked), 2)

    def test_symlinked_github_directory_pointing_outside_is_refused(self):
        repo = self.fixture("npm-library")
        outside = self.tmp / "outside"
        outside.mkdir()
        (repo / ".github").symlink_to(outside)
        with self.assertRaisesRegex(InitError, "outside the repository"):
            self.init(repo)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_plan_file_is_refused(self):
        repo = self.fixture("npm-library")
        target = self.tmp / "elsewhere.yml"
        target.write_text("version: 1\n")
        (repo / PLAN_FILE).symlink_to(target)
        with self.assertRaisesRegex(InitError, "symlink"):
            self.init(repo)
        self.assertEqual(target.read_text(), "version: 1\n")

    def test_missing_directory(self):
        with self.assertRaisesRegex(InitError, "not a directory"):
            self.init(self.tmp / "nope")


class GateScriptTests(InitCase):
    """Run the rendered gate step with real jq against sample `needs` payloads."""

    def gate(self, needs: dict) -> subprocess.CompletedProcess:
        rendered = run_init(FIXTURES / "npm-library", ref=SHA, dry_run=True).files[WORKFLOW]
        script = yaml.safe_load(rendered)["jobs"]["gate"]["steps"][0]["run"]
        return subprocess.run(
            ["/bin/bash", "-c", script],
            env={**os.environ, "NEEDS": json.dumps(needs)},
            capture_output=True,
            text=True,
        )

    @unittest.skipUnless(shutil.which("jq"), "jq is needed")
    def test_gate_semantics(self):
        cases = [
            ({"verify": {"result": "success"}}, 0),
            ({"a": {"result": "success"}, "b": {"result": "skipped"}}, 0),
            ({"a": {"result": "success"}, "b": {"result": "failure"}}, 1),
            ({"a": {"result": "cancelled"}}, 1),
            ({"a": {"result": "success"}, "b": {"result": "cancelled"}}, 1),
        ]
        for needs, code in cases:
            with self.subTest(needs=needs):
                self.assertEqual(self.gate(needs).returncode, code)


class CliInitTests(InitCase):
    def ap(self, *args):
        return subprocess.run([sys.executable, str(AP), *map(str, args)], capture_output=True, text=True, cwd=ROOT)

    def test_exit_codes(self):
        repo = self.fixture("npm-library")
        self.assertEqual(self.ap("init", repo).returncode, 1)  # no --ref
        proc = self.ap("init", repo, "--ref", SHA)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"wrote {WORKFLOW}", proc.stdout)
        self.assertEqual(self.ap("init", repo, "--ref", SHA).returncode, 0)  # pristine rerun
        ambiguous = self.fixture("ambiguous-polyglot")
        proc = self.ap("init", ambiguous, "--ref", SHA)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("a decision is needed", proc.stderr)

    def test_dry_run_prints_files(self):
        repo = self.fixture("go-service-container")
        proc = self.ap("init", repo, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("==> .github/workflows/verify.yml <==", proc.stdout)
        self.assertIn("flavor: \"go\"", proc.stdout)
        self.assertFalse((repo / ".github").exists())


if __name__ == "__main__":
    unittest.main()
