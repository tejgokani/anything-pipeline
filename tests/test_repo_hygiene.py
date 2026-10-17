"""Publish-hygiene guards: licence, security policy, packaging metadata, CI and dependency pins."""
import re
import tomllib
import unittest
from pathlib import Path

import yaml

from tests.workflow_helpers import SHA_PIN, all_steps, load_workflow, triggers

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class RequiredFiles(unittest.TestCase):
    def test_files_a_public_repo_needs_exist(self):
        for rel in (
            "README.md", "LICENSE", "SECURITY.md", "pyproject.toml", "DESIGN.md", ".gitignore",
            ".github/CODEOWNERS", ".github/dependabot.yml", ".github/workflows/self-ci.yml",
            ".github/requirements-test.txt", ".github/requirements-lint.txt",
        ):
            self.assertTrue((ROOT / rel).is_file(), f"missing {rel}")

    def test_license_is_the_unmodified_apache_2_text(self):
        body = text("LICENSE")
        self.assertIn("Apache License", body)
        self.assertIn("Version 2.0, January 2004", body)
        self.assertIn("limitations under the License.", body)
        self.assertGreater(len(body), 11_000)

    def test_security_policy_names_a_private_channel(self):
        body = text("SECURITY.md")
        self.assertIn("Report a vulnerability", body)
        self.assertRegex(body, r"(?i)do not open a public issue")

    def test_readme_does_not_overclaim(self):
        body = text("README.md")
        self.assertIn("Pre-alpha", body)
        self.assertIn("not built", body.lower())
        self.assertIn("has **not yet been run on GitHub Actions**", body)
        self.assertIn("Threat model", body)
        self.assertIn("does not protect against", body.lower())
        self.assertNotRegex(body.lower(), r"production[- ]ready|enterprise[- ]grade|fully secure|bulletproof")


class PackagingMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.meta = tomllib.loads(text("pyproject.toml"))

    def test_python_floor_and_dependencies(self):
        project = self.meta["project"]
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertTrue(any(d.lower().startswith("pyyaml") for d in project["dependencies"]))

    def test_licence_and_entry_point(self):
        project = self.meta["project"]
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(project["scripts"]["ap"], "cli.ap:main")
        self.assertEqual(project["readme"], "README.md")

    def test_rules_yml_is_packaged(self):
        self.assertIn("rules.yml", self.meta["tool"]["setuptools"]["package-data"]["detect"])

    def test_floor_matches_what_ci_tests(self):
        matrix = load_workflow(WORKFLOWS / "self-ci.yml")["jobs"]["tests"]["strategy"]["matrix"]["include"]
        self.assertIn("3.11", {entry["python"] for entry in matrix})

    def test_every_package_directory_is_importable(self):
        for pkg in ("cli", "detect", "detect/engine", "render"):
            self.assertTrue((ROOT / pkg / "__init__.py").is_file(), pkg)


class DependencyPins(unittest.TestCase):
    def test_ci_requirements_are_exact_and_hash_pinned(self):
        for rel in (".github/requirements-test.txt", ".github/requirements-lint.txt"):
            body = text(rel)
            requirement_lines = [l for l in body.splitlines() if l and not l.startswith(("#", " "))]
            self.assertTrue(requirement_lines, rel)
            for line in requirement_lines:
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==\d+(\.\d+)*\s*\\?$", f"{rel}: {line}")
            # every requirement block must carry its own hashes, and every hash must be well formed
            blocks = re.split(r"(?m)^(?=[A-Za-z0-9_.-]+==)", re.sub(r"(?m)^#.*\n", "", body))
            blocks = [b for b in blocks if b.strip()]
            self.assertEqual(len(blocks), len(requirement_lines), rel)
            for block in blocks:
                hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})\b", block)
                self.assertGreaterEqual(len(hashes), 1, f"{rel}: no hashes for {block.splitlines()[0]}")
                self.assertEqual(len(hashes), block.count("--hash="), f"{rel}: malformed hash in {block.splitlines()[0]}")

    def test_dependabot_covers_actions_and_pip_with_a_cooldown(self):
        updates = yaml.safe_load(text(".github/dependabot.yml"))["updates"]
        by_eco = {u["package-ecosystem"]: u for u in updates}
        self.assertEqual(set(by_eco), {"github-actions", "pip"})
        for eco, entry in by_eco.items():
            self.assertGreaterEqual(entry["cooldown"]["default-days"], 3, eco)
        self.assertEqual(by_eco["pip"]["directory"], "/.github")

    def test_codeowners_protects_ci_and_the_workflow_renderer(self):
        owners = text(".github/CODEOWNERS")
        for path in ("/.github/", "/render/", "/detect/rules.yml"):
            self.assertRegex(owners, rf"(?m)^{re.escape(path)}\s+@\S+")


class SelfCi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = load_workflow(WORKFLOWS / "self-ci.yml")
        cls.raw = (WORKFLOWS / "self-ci.yml").read_text()

    def test_triggers_are_unprivileged(self):
        self.assertEqual(sorted(triggers(self.workflow)), ["pull_request", "push"])

    def test_runs_tests_zizmor_and_actionlint(self):
        scripts = " ".join(step.get("run", "") for _, step in all_steps(self.workflow))
        self.assertIn("unittest discover", scripts)
        self.assertIn("zizmor", scripts)
        self.assertIn("actionlint", scripts)

    def test_gate_depends_on_every_other_job(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(sorted(jobs["gate"]["needs"]), sorted(j for j in jobs if j != "gate"))

    def test_python_deps_are_installed_with_required_hashes(self):
        installs = [s["run"] for _, s in all_steps(self.workflow) if "pip install" in s.get("run", "")]
        self.assertTrue(installs)
        for command in installs:
            self.assertIn("--require-hashes", command)

    def test_go_tool_is_pinned_to_an_exact_version(self):
        self.assertRegex(self.raw, r"go install github\.com/rhysd/actionlint/cmd/actionlint@v\d+\.\d+\.\d+")

    def test_every_action_is_sha_pinned(self):
        for _, step in all_steps(self.workflow):
            if "uses" in step:
                self.assertRegex(step["uses"], SHA_PIN)


class PrivacyGuard(unittest.TestCase):
    def test_no_personal_paths_or_emails_are_committed(self):
        needles = ["/Users/" + "tejgokani", "tejm" + "gokani@", "/private/" + "tmp"]
        skip_dirs = {".git", "__pycache__"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or skip_dirs & set(path.relative_to(ROOT).parts):
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for needle in needles:
                self.assertNotIn(needle, body, f"{path.relative_to(ROOT)} contains {needle!r}")


if __name__ == "__main__":
    unittest.main()
