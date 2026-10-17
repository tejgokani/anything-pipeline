"""End-to-end tests of `ap detect` as a subprocess."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AP = ROOT / "cli" / "ap.py"
FIXTURES = ROOT / "tests" / "fixtures"


def run_ap(*args):
    return subprocess.run([sys.executable, str(AP), *map(str, args)], capture_output=True, text=True, cwd=ROOT)


class CliTests(unittest.TestCase):
    def test_json_output_matches_expected_plan(self):
        proc = run_ap("detect", FIXTURES / "npm-library", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = json.loads((FIXTURES / "npm-library" / "expected.plan.json").read_text())
        self.assertEqual(json.loads(proc.stdout), expected)

    def test_strict_exits_2_on_ambiguous_plan(self):
        proc = run_ap("detect", FIXTURES / "ambiguous-polyglot", "--strict")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("multiple languages matched: node, python", proc.stdout)

    def test_ambiguous_without_strict_exits_0(self):
        self.assertEqual(run_ap("detect", FIXTURES / "ambiguous-polyglot").returncode, 0)

    def test_strict_exits_0_on_clear_plan(self):
        self.assertEqual(run_ap("detect", FIXTURES / "go-service-container", "--strict").returncode, 0)

    def test_explain_shows_signals_and_evidence(self):
        proc = run_ap("detect", FIXTURES / "pnpm-turbo-monorepo", "--explain")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for fragment in (
            "workspace: turbo + pnpm",
            "unit apps/web",
            "unit packages/ui",
            "signals:",
            "pm_pnpm",
            "pnpm-lock.yaml",
            "flavors.verify[1]",
            "result: clear",
        ):
            self.assertIn(fragment, proc.stdout)

    def test_missing_path_exits_1(self):
        proc = run_ap("detect", ROOT / "no-such-dir")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not a directory", proc.stderr)

    def test_invalid_rules_file_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "rules.yml"
            bad.write_text("version: 1\nsignals: {a: nope}\nkinds: {k: a}\npipelines: {}\n")
            proc = run_ap("detect", FIXTURES / "npm-library", "--rules", bad)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("invalid rules", proc.stderr)


if __name__ == "__main__":
    unittest.main()
