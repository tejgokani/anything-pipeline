"""Hostile-input hardening: terminal safety, pathological nesting, runtime guard."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cli import ap
from detect.engine import detect, render_text
from detect.engine.text import escape_controls
from render import InitError, run_init

ROOT = Path(__file__).resolve().parent.parent
HOSTILE_DIR = "evil\x1b[2J\x1b[31mresult: clear\nresult: clear\u202e"


class EscapeControlsTests(unittest.TestCase):
    def test_escapes_terminal_and_layout_attacks(self):
        self.assertEqual(escape_controls("a\x1b[31mb"), "a\\x1b[31mb")
        self.assertEqual(escape_controls("a\nb"), "a\\x0ab")
        self.assertEqual(escape_controls("a\rb"), "a\\x0db")
        self.assertEqual(escape_controls("x\u202ey"), "x\\u202ey")  # right-to-left override
        self.assertEqual(escape_controls("x\u2028y"), "x\\u2028y")  # line separator
        self.assertEqual(escape_controls("\x00\x7f\x85"), "\\x00\\x7f\\x85")

    def test_lone_surrogates_from_undecodable_file_names_are_escaped(self):
        name = os.fsdecode(b"bad-\xff-name")
        self.assertEqual(escape_controls(name), "bad-\\udcff-name")
        escape_controls(name).encode("utf-8")  # must not raise

    def test_ordinary_text_is_untouched(self):
        for text in ("packages/ui", "café", "日本語/パッケージ", "emoji-🙂", "a b"):
            self.assertEqual(escape_controls(text), text)

    def test_keep_newlines_keeps_only_newline_and_tab(self):
        self.assertEqual(escape_controls("a\n\tb\x1bc", keep_newlines=True), "a\n\tb\\x1bc")


class TerminalSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()

    def make_hostile_workspace(self):
        (self.repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'pk/*'\n")
        member = self.repo / "pk" / HOSTILE_DIR
        try:
            member.mkdir(parents=True)
        except OSError:
            self.skipTest("filesystem rejects this directory name")
        (member / "package.json").write_text('{"name": "x", "version": "1"}')

    def test_render_text_neutralises_hostile_names(self):
        self.make_hostile_workspace()
        text = render_text(detect(self.repo, explain=True), str(self.repo))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\u202e", text)
        forged = [line for line in text.splitlines() if line.startswith("result:")]
        self.assertEqual(len(forged), 1, "only the tool's own verdict line may start with 'result:'")

    def test_cli_output_is_clean_in_every_mode(self):
        self.make_hostile_workspace()
        for args in (["detect", str(self.repo)], ["detect", str(self.repo), "--explain"], ["detect", str(self.repo), "--json"]):
            with self.subTest(args=args):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = ap.main(args)
                self.assertIn(code, (0, 2))
                combined = out.getvalue() + err.getvalue()
                self.assertNotIn("\x1b", combined)
                self.assertNotIn("\u202e", combined)
                self.assertEqual(sum(1 for l in combined.splitlines() if l.startswith("result:")), 0 if "--json" in args else 1)

    def test_init_blocks_hostile_unit_path_without_printing_escapes(self):
        self.make_hostile_workspace()
        (self.repo / "pnpm-lock.yaml").write_text("")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ap.main(["init", str(self.repo), "--ref", "a" * 40])
        self.assertEqual(code, 2)
        self.assertNotIn("\x1b", err.getvalue())
        self.assertFalse((self.repo / ".github").exists())

    def test_undecodable_file_name_does_not_crash_the_cli(self):
        (self.repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'pk/*'\n")
        raw = os.fsencode(self.repo / "pk" / "bad-") + b"\xff"
        try:
            os.makedirs(raw)
        except OSError:
            self.skipTest("filesystem rejects non-UTF-8 names")
        with open(os.path.join(raw, b"package.json"), "w") as handle:
            handle.write('{"name": "x", "version": "1"}')
        proc = subprocess.run(
            [sys.executable, str(ROOT / "cli" / "ap.py"), "detect", str(self.repo)], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class EmitTests(unittest.TestCase):
    def test_emit_strips_escapes_from_stdout_and_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            ap.emit("hi \x1b[31mred\x1b[0m")
            ap.emit("warn \x07bell", err=True)
        self.assertEqual(out.getvalue(), "hi \\x1b[31mred\\x1b[0m\n")
        self.assertEqual(err.getvalue(), "warn \\x07bell\n")


class PathologicalNestingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()

    def test_deeply_nested_package_json_is_treated_as_unreadable(self):
        (self.repo / "package.json").write_text("[" * 200_000 + "]" * 200_000)
        plan = detect(self.repo)
        unit = plan["units"][0]
        self.assertEqual(unit["kinds"], [])
        self.assertIn("no kind matched", unit["reasons"])

    def test_deeply_nested_pyproject_is_treated_as_unreadable(self):
        (self.repo / "pyproject.toml").write_text("x = " + "[" * 100_000 + "]" * 100_000)
        plan = detect(self.repo)
        self.assertEqual(plan["units"][0]["kinds"], [])

    def test_deeply_nested_pnpm_workspace_is_a_reported_problem(self):
        (self.repo / "pnpm-workspace.yaml").write_text("packages: " + "[" * 100_000 + "]" * 100_000)
        plan = detect(self.repo)
        self.assertTrue(plan["ambiguous"])
        self.assertTrue(any("no workspace members" in p for p in plan["problems"]))

    def test_deeply_nested_plan_file_is_an_init_error(self):
        (self.repo / "package.json").write_text('{"name": "x", "version": "1"}')
        (self.repo / ".anything-pipeline.yml").write_text("overrides: " + "[" * 100_000 + "]" * 100_000)
        with self.assertRaises(InitError):
            run_init(self.repo, ref="a" * 40)

    def test_cli_has_a_safety_net_for_recursion_anywhere_else(self):
        err = io.StringIO()
        with mock.patch.object(ap, "detect", side_effect=RecursionError), redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = ap.main(["detect", str(self.repo)])
        self.assertEqual(code, 1)
        self.assertIn("nested too deeply", err.getvalue())

    def test_cli_end_to_end_on_deep_json_exits_cleanly(self):
        (self.repo / "package.json").write_text("[" * 200_000 + "]" * 200_000)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "cli" / "ap.py"), "detect", str(self.repo), "--json"], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        json.loads(proc.stdout)


class RuntimeGuardTests(unittest.TestCase):
    def test_old_python_gets_a_friendly_message(self):
        message = ap.check_runtime((3, 9, 6))
        self.assertIn("Python 3.11 or newer is required", message)
        self.assertIn("3.9.6", message)

    def test_supported_python_passes(self):
        self.assertIsNone(ap.check_runtime((3, 11, 0)))
        self.assertIsNone(ap.check_runtime())

    def test_missing_pyyaml_gets_a_friendly_message(self):
        with mock.patch.dict(sys.modules, {"yaml": None}):
            message = ap.check_runtime((3, 12, 0))
        self.assertIn("PyYAML is required", message)
        self.assertIn("pip install", message)

    def test_the_cli_file_still_parses_on_old_pythons(self):
        # The guard runs before other imports, so the file must be parseable by Python 3.9 syntax rules.
        import ast

        tree = ast.parse((ROOT / "cli" / "ap.py").read_text(), feature_version=(3, 9))
        self.assertTrue(tree.body)


if __name__ == "__main__":
    unittest.main()
