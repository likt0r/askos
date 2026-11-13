"""Which systemd states count as running: a Build unit while it stops still runs
(images._active), an Orca connection while it closes no longer counts (describe.devices).

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentbox import describe, images  # noqa: E402

CONNECTION = "agentbox-gui-work@7-10.0.0.1:9000-192.168.1.20:51000.service"


class UnitStateTest(unittest.TestCase):
    def build_active(self, state):
        answer = subprocess.CompletedProcess([], 0 if state == "active" else 3, stdout=state + "\n", stderr="")
        with mock.patch.object(images.subprocess, "run", return_value=answer):
            return images._active("agentbox-build@base.service")

    def test_a_build_stopping_still_runs(self):
        for state in ("active", "activating", "deactivating", "reloading"):
            with self.subTest(state=state):
                self.assertTrue(self.build_active(state))
        for state in ("inactive", "failed", ""):
            with self.subTest(state=state):
                self.assertFalse(self.build_active(state))

    def test_a_connection_being_closed_is_no_connection(self):
        for state in ("active", "activating", "reloading"):
            with self.subTest(state=state):
                self.assertEqual(describe.devices([(CONNECTION, "loaded", state, "running", "")]),
                                 {"work": [{"address": "192.168.1.20", "connections": 1}]})
        for state in ("deactivating", "inactive", "failed"):
            with self.subTest(state=state):
                self.assertEqual(describe.devices([(CONNECTION, "loaded", state, "dead", "")]), {})


if __name__ == "__main__":
    unittest.main()
