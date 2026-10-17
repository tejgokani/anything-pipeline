"""Unit tests: matchers, rule validation, workspaces, ambiguity, safety."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from detect.engine import detect, load_rules
from detect.engine.evaluate import Evaluator
from detect.engine.matchers import RepoFS, glob_to_regex, lookup
from detect.engine.rules import DEFAULT_RULES_PATH, validate
from detect.engine.workspaces import parse_go_work


def make_repo(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    return root


def default_data() -> dict:
    return yaml.safe_load(DEFAULT_RULES_PATH.read_text())


class TempRepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def repo(self, files: dict) -> Path:
        return make_repo(self.root / "repo", files)

    def plan(self, files: dict, **kwargs) -> dict:
        return detect(self.repo(files), **kwargs)


class GlobAndLookupTests(unittest.TestCase):
    def test_double_star_prefix_matches_root_and_nested(self):
        rx = glob_to_regex("**/Dockerfile")
        for path in ("Dockerfile", "a/Dockerfile", "a/b/Dockerfile"):
            self.assertTrue(rx.match(path), path)
        self.assertIsNone(rx.match("Dockerfile.dev"))
        self.assertIsNone(rx.match("a/MyDockerfile"))

    def test_single_star_stays_in_one_segment(self):
        rx = glob_to_regex("packages/*")
        self.assertTrue(rx.match("packages/a"))
        self.assertIsNone(rx.match("packages/a/b"))
        self.assertTrue(glob_to_regex("packages/**").match("packages/a/b"))

    def test_lookup_dotted_and_list_paths(self):
        data = {"project": {"name": "x"}, "tasks": {"test:unit": 1}}
        self.assertEqual(lookup(data, "project.name"), (True, "x"))
        self.assertEqual(lookup(data, "$.project.name"), (True, "x"))
        self.assertEqual(lookup(data, ["tasks", "test:unit"]), (True, 1))
        self.assertEqual(lookup(data, "project.missing"), (False, None))


class EvaluatorTests(TempRepoCase):
    def evaluator(self, files, unit="."):
        rules = load_rules()
        return Evaluator(RepoFS(self.repo(files), rules.ignore_dirs), rules, unit)

    def test_search_up_finds_lockfile_at_workspace_root(self):
        ev = self.evaluator({"pnpm-lock.yaml": "x", "apps/web/package.json": "{}"}, unit="apps/web")
        self.assertTrue(ev.signal("pm_pnpm").ok)
        self.assertEqual(ev.signal("pm_pnpm").evidence, ("pnpm-lock.yaml",))

    def test_signals_without_search_up_do_not_look_upward(self):
        ev = self.evaluator({"package.json": "{}", "apps/web/index.js": ""}, unit="apps/web")
        self.assertFalse(ev.signal("node").ok)

    def test_private_true_is_not_publishable_but_private_false_is(self):
        self.assertFalse(self.evaluator({"package.json": {"name": "a", "private": True}}).signal("npm_publishable").ok)
        self.assertTrue(
            self.evaluator({"package.json": {"name": "a", "private": False}}, unit=".").signal("npm_publishable").ok
        )

    def test_ignored_directories_are_not_searched(self):
        ev = self.evaluator({"node_modules/dep/Dockerfile": "FROM x", "package.json": "{}"})
        self.assertFalse(ev.signal("dockerfile").ok)

    def test_nested_dockerfile_is_found(self):
        ev = self.evaluator({"deploy/Dockerfile.prod": "FROM x"})
        self.assertTrue(ev.signal("dockerfile").ok)

    def test_invalid_json_never_matches(self):
        ev = self.evaluator({"package.json": "{not json"})
        self.assertTrue(ev.signal("node").ok)  # the file exists
        self.assertFalse(ev.signal("npm_publishable").ok)  # but cannot be read


class RuleValidationTests(unittest.TestCase):
    def test_default_rules_are_valid(self):
        self.assertEqual(validate(default_data()), [])

    def assertRejects(self, mutate, fragment):
        data = copy.deepcopy(default_data())
        mutate(data)
        errors = validate(data)
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def test_unknown_signal_reference(self):
        self.assertRejects(lambda d: d["kinds"].update({"x": {"all": ["nope"]}}), "unknown signal 'nope'")

    def test_unknown_top_level_key(self):
        self.assertRejects(lambda d: d.update({"surprise": 1}), "unknown top-level key")

    def test_signal_cycle(self):
        def mutate(d):
            d["signals"]["a"] = {"all": ["b"]}
            d["signals"]["b"] = {"all": ["a"]}

        self.assertRejects(mutate, "signal cycle")

    def test_leaf_cannot_mix_with_combinator(self):
        self.assertRejects(lambda d: d["signals"].update({"x": {"file": "a", "all": ["node"]}}), "cannot be mixed")

    def test_search_up_only_on_leaves(self):
        self.assertRejects(lambda d: d["signals"].update({"x": {"all": ["node"], "search_up": True}}), "leaves only")

    def test_unknown_kind_in_ship_rule(self):
        self.assertRejects(lambda d: d["pipelines"]["ship"][0]["when"]["kind_any"].append("ghost"), "unknown kind 'ghost'")

    def test_bad_units_from(self):
        self.assertRejects(lambda d: d["workspaces"][0].update({"units_from": "Makefile"}), "units_from")

    def test_exists_and_equals_conflict(self):
        bad = {"json": {"file": "package.json", "path": "a", "exists": True, "equals": 1}}
        self.assertRejects(lambda d: d["signals"].update({"x": bad}), "either exists or equals")

    def test_version_must_be_one(self):
        self.assertRejects(lambda d: d.update({"version": 2}), "version must be 1")


class WorkspaceTests(TempRepoCase):
    def test_parse_go_work_handles_blocks_comments_and_other_directives(self):
        text = (
            "go 1.22\n"
            "use ./single // trailing comment\n"
            "use (\n  ./a\n  // skipped\n  ./b\n)\n"
            "replace (\n  example.com/x => ./x\n)\n"
        )
        self.assertEqual(parse_go_work(text), ["./single", "./a", "./b"])

    def test_pnpm_exclusions(self):
        plan = self.plan(
            {
                "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n  - '!packages/skip'\n",
                "packages/a/package.json": {"name": "a", "version": "1.0.0"},
                "packages/skip/package.json": {"name": "skip", "version": "1.0.0"},
                "packages/notapkg/readme.md": "",
            }
        )
        self.assertEqual([u["path"] for u in plan["units"]], ["packages/a"])

    def test_npm_workspaces_field(self):
        plan = self.plan(
            {
                "package.json": {"name": "root", "private": True, "workspaces": ["libs/*"]},
                "libs/x/package.json": {"name": "x", "version": "1.0.0"},
            }
        )
        self.assertEqual(plan["workspace"]["tools"], ["node-workspaces"])
        self.assertEqual([u["path"] for u in plan["units"]], ["libs/x"])

    def test_go_work_members(self):
        plan = self.plan(
            {
                "go.work": "go 1.22\nuse (\n\t./svc\n\t./lib\n)\n",
                "svc/go.mod": "module a\n",
                "svc/Dockerfile": "FROM x",
                "lib/go.mod": "module b\n",
            }
        )
        by_path = {u["path"]: u for u in plan["units"]}
        self.assertEqual(sorted(by_path), ["lib", "svc"])
        self.assertEqual(by_path["svc"]["ship"], {"pipeline": "ship-app", "flavor": "container"})
        self.assertEqual(by_path["lib"]["ship"], {"pipeline": "ship-package", "flavor": "go-module"})

    def test_workspace_with_no_members_is_a_problem_not_a_guess(self):
        plan = self.plan({"pnpm-workspace.yaml": "packages:\n  - 'nope/*'\n", "package.json": {"name": "r", "private": True}})
        self.assertTrue(plan["ambiguous"])
        self.assertEqual(plan["problems"], ["workspace: no workspace members resolved from pnpm-workspace.yaml"])
        self.assertEqual([u["path"] for u in plan["units"]], ["."])

    def test_turbo_alone_does_not_split_units(self):
        plan = self.plan({"turbo.json": "{}", "package.json": {"name": "p", "version": "1.0.0"}})
        self.assertEqual(plan["workspace"]["tools"], ["turbo"])
        self.assertIsNone(plan["workspace"]["unit_source"])
        self.assertEqual([u["path"] for u in plan["units"]], ["."])
        self.assertEqual(plan["problems"], [])


class AmbiguityTests(TempRepoCase):
    def test_empty_repo_has_no_kind(self):
        plan = self.plan({"README.md": "hi"})
        unit = plan["units"][0]
        self.assertTrue(plan["ambiguous"])
        self.assertEqual(unit["reasons"], ["no kind matched", "no verify flavor matched"])

    def test_python_app_that_is_not_publishable_has_no_kind(self):
        plan = self.plan({"pyproject.toml": "[tool.x]\na = 1\n", "uv.lock": ""})
        unit = plan["units"][0]
        self.assertEqual(unit["verify"], {"flavor": "python", "vars": {"pm": "uv"}})
        self.assertEqual(unit["reasons"], ["no kind matched"])

    def test_electron_app_is_not_mistaken_for_a_library(self):
        # Found on a real local repo: an Electron app with no `private` flag looked publishable.
        plan = self.plan(
            {"package.json": {"name": "ide", "version": "1.0.0", "main": "main.js", "devDependencies": {"electron": "30"}}}
        )
        unit = plan["units"][0]
        self.assertEqual(unit["kinds"], [])
        self.assertEqual(unit["reasons"], ["no kind matched"])

    def test_next_app_with_dockerfile_conflicts(self):
        plan = self.plan(
            {
                "package.json": {"name": "w", "private": True, "dependencies": {"next": "15"}},
                "Dockerfile": "FROM x",
            }
        )
        unit = plan["units"][0]
        self.assertEqual(unit["kinds"], ["web-app", "container-app"])
        self.assertIsNone(unit["ship"])
        self.assertEqual(unit["reasons"], ["conflicting ship pipelines: ship-app/container, ship-app/platform"])

    def test_mise_tasks_win_and_are_exclusive(self):
        plan = self.plan(
            {
                "package.json": {"name": "p", "version": "1"},
                "pyproject.toml": "[project]\nname='p'\n",
                "mise.toml": "[tasks.test]\nrun = 'make test'\n",
            }
        )
        unit = plan["units"][0]
        self.assertEqual(unit["verify"], {"flavor": "generic", "vars": {}})
        self.assertNotIn("multiple languages matched: node, python", unit["reasons"])

    def test_mise_file_without_tasks_is_flagged(self):
        plan = self.plan({"package.json": {"name": "p", "version": "1"}, "mise.toml": "[tools]\nnode = '22'\n"})
        self.assertIn("mise.toml defines none of check, test, build", plan["units"][0]["reasons"])

    def test_member_unit_inherits_root_mise_tasks(self):
        plan = self.plan(
            {
                "pnpm-workspace.yaml": "packages:\n  - 'pkgs/*'\n",
                "mise.toml": "[tasks.check]\nrun = 'x'\n",
                "pkgs/a/package.json": {"name": "a", "version": "1"},
            }
        )
        self.assertEqual(plan["units"][0]["verify"], {"flavor": "generic", "vars": {}})


class SafetyTests(TempRepoCase):
    def test_symlink_pointing_outside_repo_is_not_read(self):
        outside = self.root / "outside"
        make_repo(outside, {"package.json": {"name": "stolen", "version": "1"}})
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "package.json").symlink_to(outside / "package.json")
        plan = detect(repo)
        self.assertEqual(plan["units"][0]["kinds"], [])
        self.assertIn("no kind matched", plan["units"][0]["reasons"])

    def test_detect_never_modifies_the_repo(self):
        repo = self.repo({"package.json": {"name": "p", "version": "1"}, "package-lock.json": "{}"})
        before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))
        detect(repo, explain=True)
        after = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))
        self.assertEqual(before, after)

    def test_not_a_directory_raises(self):
        with self.assertRaises(NotADirectoryError):
            detect(self.root / "does-not-exist")


if __name__ == "__main__":
    unittest.main()
