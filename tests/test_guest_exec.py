"""Programs in a VM through its guest agent (guest.execute, guest.numbers): starting a
wrapper, waiting for it with backoff, decoding its output, and what fails how.

virsh's answer to guest-exec and the guest agent's guest-exec-status answers are mocked,
and so is the clock; tests/fake_virsh.py covers the same through the CLI.

Run from the repo root: python3 -m unittest discover -s tests
"""
import base64
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentbox import guest  # noqa: E402
from agentbox.errors import Fail  # noqa: E402

WRAPPER = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-x"
MISSING = ("error: internal error: unable to execute QEMU agent command 'guest-exec': Failed to execute child "
           f"process “{WRAPPER}” (No such file or directory)\n")


def b64(text):
    return base64.b64encode(text.encode()).decode()


def ended(out="", err="", **status):
    return {"exited": True, "exitcode": 0, "out-data": b64(out), "err-data": b64(err), **status}


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        self.started = subprocess.CompletedProcess([], 0, json.dumps({"return": {"pid": 4001}}), "")
        self.statuses = []
        self.asked = []
        self.now = 1000.0
        self.slept = []

        def agent(ctx, cmd):
            self.asked.append(cmd)
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

        def sleep(seconds):
            self.slept.append(seconds)
            self.now += seconds

        for patch in (mock.patch.object(guest.virsh, "run", lambda *a, **kw: self.started),
                      mock.patch.object(guest, "agent", agent),
                      mock.patch.object(guest.time, "monotonic", lambda: self.now),
                      mock.patch.object(guest.time, "sleep", sleep)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_it(self, wait=60, **kw):
        return guest.execute("work", WRAPPER, ["a", "b"], wait, **kw)

    def fails(self, **kw):
        with self.assertRaises(Fail) as caught:
            self.run_it(**kw)
        return caught.exception

    def test_output_once_the_wrapper_ended(self):
        self.statuses = [{"exited": False}, ended("out\xe9\n", "err", exitcode=3, **{"err-truncated": True})]
        self.assertEqual(self.run_it(), (3, "out\xe9\n", "err", True))
        self.assertEqual(self.asked, [{"execute": "guest-exec-status", "arguments": {"pid": 4001}}] * 2)

    def test_it_asks_ever_less_often_up_to_every_two_seconds(self):
        self.statuses = [{"exited": False}] * 7 + [ended()]
        self.run_it()
        self.assertEqual(self.slept, [0.2, 0.4, 0.8, 1.6, 2, 2, 2])

    def test_a_wrapper_that_does_not_end_in_time_or_is_stopped_gives_124(self):
        self.statuses = [{"exited": False}]
        self.assertEqual(self.run_it(wait=10)[0], 124)
        self.assertGreater(self.now, 1010)
        self.assertLess(self.now, 1013)
        self.statuses = [ended("x", signal=9, exitcode=None)]
        self.assertEqual(self.run_it()[0], 124)

    def test_a_missing_wrapper(self):
        self.started = subprocess.CompletedProcess([], 1, "", MISSING)
        called = []

        def missing():
            called.append(True)
            raise Fail("no wrapper", error="inbox_missing")

        self.assertEqual(self.fails(missing=missing).error, "inbox_missing")
        self.assertEqual(called, [True])
        # without a callback it is a guest agent that does not answer
        f = self.fails()
        self.assertEqual((f.error, f.exit, f.params), ("guest_agent_unavailable", 4, {"vm": "work"}))
        self.assertTrue(f.message.startswith("work: the guest agent does not answer (error: internal error"))

    def test_a_guest_agent_that_does_not_answer(self):
        self.started = subprocess.CompletedProcess([], 1, "", "error: Guest agent is not responding\n")
        f = self.fails(missing=lambda: self.fail("not a missing wrapper"))
        self.assertEqual((f.message, f.error, f.exit),
                         ("work: the guest agent does not answer (error: Guest agent is not responding)",
                          "guest_agent_unavailable", 4))

    def test_a_pid_that_is_no_whole_number(self):
        for answer in ('{"return": {"pid": true}}', '{"return": {"pid": "4"}}', '{"return": {}}', "[]", "no"):
            with self.subTest(answer=answer):
                self.started = subprocess.CompletedProcess([], 0, answer, "")
                f = self.fails()
                self.assertEqual((f.message, f.error), ("work: unexpected answer from the guest agent",
                                                        "guest_agent_unavailable"))

    def test_lenient_decoding_replaces_bad_utf8_but_needs_base64(self):
        self.statuses = [{"exited": True, "exitcode": 0, "out-data": base64.b64encode(b"a\xff").decode()}]
        self.assertEqual(self.run_it(), (0, "a�", "", False))
        self.statuses = [{"exited": True, "exitcode": 0, "out-data": "abc"}]
        self.assertEqual(self.fails().message, "work: unexpected answer from the guest agent")

    def test_an_answer_is_decoded_strictly_with_a_size_cap(self):
        self.statuses = [ended('{"n": 1}', "ignored", exitcode=2)]
        self.assertEqual(self.run_it(answer=16), (2, '{"n": 1}', None, False))
        for status in (ended("x" * 17), ended("x", **{"out-truncated": True}),
                       {"exited": True, "exitcode": 0, "out-data": base64.b64encode(b"\xff").decode()},
                       {"exited": True, "exitcode": 0, "out-data": b64("ok") + "!"},
                       {"exited": True, "exitcode": 0, "out-data": 5}):
            with self.subTest(status=status):
                self.statuses = [status]
                self.assertIsNone(self.run_it(answer=16)[1])


class NumbersTest(unittest.TestCase):
    def test_only_small_whole_numbers_under_the_keys(self):
        keys = ("files", "bytes")
        self.assertEqual(guest.numbers('{"files": 2, "bytes": 0, "x": 1}', keys, 10), [2, 0])
        for out in ('{"files": 2}', '{"files": 2, "bytes": 10}', '{"files": -1, "bytes": 1}',
                    '{"files": true, "bytes": 1}', '{"files": 1.0, "bytes": 1}', "[1, 2]", '"x"', "no", "",
                    "[" * 100000):
            with self.subTest(out=out[:40]):
                self.assertIsNone(guest.numbers(out, keys, 10))


if __name__ == "__main__":
    unittest.main()
