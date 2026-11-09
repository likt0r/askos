"""How the CLI reports a failure (agentbox/errors.py, cli.py): "agentbox: <message>" on
stderr, or with --json one JSON object {"error", "message", "params"} instead, which the
Control Room reads (docs/control-room-api.md); the exit code is the same either way.

The real CLI runs with AGENTBOX_ROOT and fake virsh, as in test_lifecycle.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_lifecycle import REPO, START_ERROR, LifecycleTest  # noqa: E402

sys.path.insert(0, REPO)
from agentbox import cli  # noqa: E402


class JsonFailureTest(LifecycleTest):
    def failure(self, *args, exit_code=1):
        """The --json failure of agentbox <args>, checked against the same call in text mode."""
        code, out, err = self.run_agentbox(*args, "--json")
        self.assertEqual((code, out), (exit_code, ""), err)
        self.assertEqual(err.count("\n"), 1, err)
        said = json.loads(err)
        self.assertEqual(set(said), {"error", "message", "params"})
        self.assertIsInstance(said["params"], dict)
        text = self.run_agentbox(*args)
        self.assertEqual(text, (exit_code, "", f"agentbox: {said['message']}\n"))
        return said

    def test_a_failure_names_its_code_message_and_parameters(self):
        said = self.failure("start", "nope")
        self.assertEqual(said["error"], "vm_not_found")
        self.assertEqual(said["params"], {"vm": "nope"})
        self.assertIn("nope", said["message"])
        self.assertEqual(self.calls(), [])

    def test_exit_codes_stay(self):
        # those of `agentbox logs`, which the Control Room falls back on without a code
        said = self.failure("logs", "priv", "--lines=many", exit_code=2)
        self.assertEqual(said["error"], "invalid_argument")
        self.assertEqual(said["params"].get("option"), "--lines")
        self.scenario(state="shut off")
        self.assertEqual(self.failure("logs", "work", exit_code=3)["error"], "vm_not_running")

    def test_the_global_option_fails_the_same_way(self):
        said = self.failure("--actor=", "status", exit_code=2)
        self.assertEqual(said["error"], "invalid_argument")

    def test_an_option_s_value_is_no_json_flag(self):
        # the note "--json" asks for text, as the command reads it; a later --json counts
        code, out, err = self.run_agentbox("set-note", "nope", "x.org", "--note", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("agentbox: "), err)
        said = self.failure("set-note", "nope", "x.org", "--note", "--json")
        self.assertEqual((said["error"], said["params"]), ("vm_not_found", {"vm": "nope"}))
        # every option USAGE shows with a value, none of the flags
        self.assertLessEqual({"--actor", "--note", "--for", "--confirm", "--since", "--reset", "--mode"}, cli.VALUED)
        self.assertEqual(cli.VALUED & {"--json", "--force", "--temporary", "--all", "--no-start", "--no-prompt"},
                         set())

    def test_another_program_s_own_error_output(self):
        # virsh's own message, unchanged in text mode; with --json it is the message
        self.scenario(vms={"work": {"state": "shut off", "start_fails": True}})
        self.assertEqual(self.run_agentbox("start", "work"), (1, "", START_ERROR))
        code, out, err = self.run_agentbox("start", "work", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err), {"error": "action_failed", "message": START_ERROR.strip(), "params": {}})


if __name__ == "__main__":
    unittest.main()
