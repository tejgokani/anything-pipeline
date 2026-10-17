"""Golden tests: `detect` on every fixture must equal its expected.plan.json."""
import json
import unittest
from pathlib import Path

from detect.engine import detect, load_rules

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXPECTED_FIXTURES = {
    "npm-library",
    "next-app-vercel",
    "python-uv-library",
    "go-service-container",
    "pnpm-turbo-monorepo",
    "ambiguous-polyglot",
}


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_all_six_fixtures_exist(self):
        found = {p.name for p in FIXTURES.iterdir() if (p / "expected.plan.json").is_file()}
        self.assertEqual(found, EXPECTED_FIXTURES)

    def test_each_fixture_matches_expected_plan(self):
        for name in sorted(EXPECTED_FIXTURES):
            with self.subTest(fixture=name):
                expected = json.loads((FIXTURES / name / "expected.plan.json").read_text())
                actual = detect(FIXTURES / name, rules=self.rules)
                self.assertEqual(actual, expected)

    def test_plan_is_deterministic(self):
        for name in sorted(EXPECTED_FIXTURES):
            with self.subTest(fixture=name):
                first = detect(FIXTURES / name, rules=self.rules)
                second = detect(FIXTURES / name, rules=self.rules)
                self.assertEqual(first, second)

    def test_explain_adds_evidence_without_changing_decisions(self):
        for name in sorted(EXPECTED_FIXTURES):
            with self.subTest(fixture=name):
                plain = detect(FIXTURES / name, rules=self.rules)
                explained = detect(FIXTURES / name, rules=self.rules, explain=True)
                for unit in explained["units"]:
                    self.assertIn("explain", unit)
                    unit.pop("explain")
                if explained["workspace"]:
                    explained["workspace"].pop("explain")
                self.assertEqual(explained, plain)


if __name__ == "__main__":
    unittest.main()
