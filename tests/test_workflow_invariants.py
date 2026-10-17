"""Static security and consistency checks over verify.yml and the rendered callers.

These are the seeds of `ap audit`: cheap, deterministic rules that every workflow this
kit ships must satisfy.
"""
import re
import subprocess
import unittest
from pathlib import Path

import yaml

from detect.engine import load_rules
from render import callers
from render.planfile import FLAVOR_PMS
from tests.workflow_helpers import ROOT, SHA_PIN, VERIFY_WORKFLOW, all_steps, find_step, load_workflow, triggers

SHA = "a" * 40


def assert_workflow_invariants(test, workflow: dict, text: str, allow_local_uses: bool = False):
    test.assertEqual(workflow.get("permissions"), {}, "top-level permissions must be {}")
    test.assertNotIn("pull_request_target", text)
    test.assertNotIn("workflow_run", text.split("jobs:")[0], "verify must not use privileged triggers")
    test.assertNotRegex(text, r"secrets\s*:\s*inherit")
    test.assertNotRegex(text, r"\$\{\{\s*secrets\.")
    for job_id, job in workflow["jobs"].items():
        test.assertTrue(job.get("name"), f"{job_id}: jobs need a name (stable check names, zizmor anonymous-definition)")
        test.assertIn("permissions", job, f"{job_id}: permissions must be explicit")
        test.assertIn(job["permissions"], ({}, {"contents": "read"}), f"{job_id}: least privilege")
        if "uses" not in job:
            test.assertIn("timeout-minutes", job, f"{job_id}: needs a timeout")
        uses = job.get("uses")
        if uses and not allow_local_uses:
            test.assertRegex(uses, SHA_PIN, f"{job_id}: reusable workflow must be pinned to a SHA")
    for job_id, step in all_steps(workflow):
        if "uses" in step:
            test.assertRegex(step["uses"], SHA_PIN, f"{job_id}: {step['uses']} must be pinned to a full SHA")
            if step["uses"].startswith("actions/checkout@"):
                test.assertIs(step.get("with", {}).get("persist-credentials"), False, f"{job_id}: checkout must not persist credentials")
        script = step.get("run", "")
        test.assertNotRegex(script, r"\$\{\{", f"{job_id}: expressions must not be interpolated into run blocks")


class VerifyWorkflowInvariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = VERIFY_WORKFLOW.read_text()
        cls.workflow = load_workflow()

    def test_security_invariants(self):
        assert_workflow_invariants(self, self.workflow, self.text)

    def test_only_workflow_call_trigger(self):
        self.assertEqual(list(triggers(self.workflow)), ["workflow_call"])

    def test_every_pin_has_a_version_comment(self):
        for line in self.text.splitlines():
            if re.search(r"\buses:\s*\S+@[0-9a-f]{40}", line):
                self.assertRegex(line, r"# v\d+(\.\d+)*$", line)

    def test_flavors_in_workflow_match_planfile_and_rules(self):
        guard = self.workflow["jobs"]["unsupported-flavor"]["if"]
        listed = set(re.findall(r'"([a-z]+)"', guard))
        self.assertEqual(listed, set(FLAVOR_PMS))
        for job in listed:
            self.assertIn(job, self.workflow["jobs"], f"missing job for flavor {job}")
        rules = load_rules()
        for rule in rules.verify_flavors:
            self.assertIn(rule["use"], FLAVOR_PMS, f"rules.yml emits unsupported flavor {rule['use']}")
            pm = (rule.get("vars") or {}).get("pm")
            if pm:
                self.assertIn(pm, FLAVOR_PMS[rule["use"]])
                script = find_step(self.workflow, rule["use"], "Install dependencies")["run"]
                self.assertRegex(script, rf"(^|\n)\s*{pm}\)", f"install step has no branch for {pm}")

    def test_caller_inputs_are_declared_by_the_workflow(self):
        declared = set(triggers(self.workflow)["workflow_call"]["inputs"])
        from render.planfile import INPUT_TYPES

        self.assertTrue(set(INPUT_TYPES) <= declared, set(INPUT_TYPES) - declared)
        self.assertTrue({"flavor", "pm", "working-directory"} <= declared)

    def test_every_run_block_is_valid_bash(self):
        checked = 0
        for job_id, step in all_steps(self.workflow):
            if "run" in step:
                proc = subprocess.run(["/bin/bash", "-n"], input=step["run"], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"{job_id}/{step.get('name')}: {proc.stderr}")
                checked += 1
        self.assertEqual(checked, 7)

    def test_every_run_block_sets_strict_mode(self):
        for job_id, step in all_steps(self.workflow):
            if "run" in step and job_id != "unsupported-flavor":
                self.assertIn("set -euo pipefail", step["run"], f"{job_id}/{step.get('name')}")

    def test_fails_closed_on_unknown_flavor(self):
        job = self.workflow["jobs"]["unsupported-flavor"]
        self.assertIn("exit 1", job["steps"][0]["run"])


class RenderedCallerInvariants(unittest.TestCase):
    def render(self, units, ref=SHA, label="v0.1.0"):
        text = callers.render_verify("owner/anything-pipeline", ref, label, "main", units)
        return text, yaml.safe_load(text)

    def unit(self, path=".", flavor="node", pm="npm", inputs=None):
        return {"path": path, "flavor": flavor, "pm": pm, "inputs": inputs or {}}

    def test_single_unit_caller(self):
        text, workflow = self.render([self.unit()])
        assert_workflow_invariants(self, workflow, text)
        self.assertEqual(list(workflow["jobs"]), ["verify", "gate"])
        self.assertEqual(workflow["jobs"]["gate"]["needs"], ["verify"])
        self.assertEqual(workflow["jobs"]["gate"]["if"], "${{ always() }}")

    def test_gate_needs_every_unit_job(self):
        units = [self.unit("apps/web"), self.unit("packages/ui"), self.unit("services/api", "go", "")]
        text, workflow = self.render(units)
        assert_workflow_invariants(self, workflow, text)
        jobs = [j for j in workflow["jobs"] if j != "gate"]
        self.assertEqual(len(jobs), 3)
        self.assertEqual(workflow["jobs"]["gate"]["needs"], jobs)

    def test_job_ids_are_valid_and_unique(self):
        ids = callers.job_ids([".", "a/b", "a-b", "A/B", "x" * 80])
        self.assertEqual(len(set(ids.values())), len(ids))
        for job_id in ids.values():
            self.assertRegex(job_id, r"^[a-z_][a-z0-9_-]*$")

    def test_generated_header_and_hash_detect_edits(self):
        text, _ = self.render([self.unit()])
        self.assertEqual(callers.generated_state(text), "pristine")
        self.assertEqual(callers.generated_state(text + "# edited\n"), "modified")
        self.assertEqual(callers.generated_state("name: other\n"), "foreign")

    def test_values_are_quoted_so_they_cannot_break_out_of_yaml(self):
        text, workflow = self.render([self.unit(inputs={"test-command": 'echo "a": b # c\n'.strip()})])
        with_ = workflow["jobs"]["verify"]["with"]
        self.assertEqual(with_["test-command"], 'echo "a": b # c')


class ShippedWorkflowDirectory(unittest.TestCase):
    def test_every_shipped_workflow_passes_the_invariants(self):
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            with self.subTest(workflow=path.name):
                assert_workflow_invariants(self, yaml.safe_load(path.read_text()), path.read_text())


if __name__ == "__main__":
    unittest.main()
