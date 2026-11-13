"""Programs of the Host (agentbox/programs.py): one way to run them, one way they fail.

Runs real small programs (this Python with -c) in a temporary directory; nothing of the
Host is touched.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentbox import programs  # noqa: E402
from agentbox.errors import Fail  # noqa: E402


def py(code):
    return [sys.executable, "-c", code]


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def fails(self, argv, timeout=30, **kw):
        with self.assertRaises(Fail) as caught:
            programs.run(argv, timeout, **kw)
        return caught.exception

    def test_success_gives_the_output(self):
        r = programs.run(py("import sys; print('out'); print('err', file=sys.stderr)"), 30)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "out\n", "err\n"))

    def test_input_and_bytes_that_are_not_utf8(self):
        r = programs.run(py("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read() + b'\\xff')"), 30,
                         input="hello")
        self.assertEqual(r.stdout, "hello�")

    def test_a_missing_program(self):
        missing = os.path.join(self.dir, "nothing-here")
        f = self.fails([missing, "--x"])
        self.assertEqual((f.message, f.exit, f.error, f.params), (f"{missing} is missing", 127, "action_failed",
                                                                    {"program": "nothing-here"}))

    def test_a_program_that_cannot_be_started(self):
        path = os.path.join(self.dir, "not-executable")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        f = self.fails([path], what="the thing")
        self.assertEqual((f.exit, f.error, f.params), (1, "action_failed", {"program": "not-executable"}))
        self.assertTrue(f.message.startswith("the thing cannot be started: "), f.message)

    def test_the_time_limit(self):
        f = self.fails(py("import time; time.sleep(30)"), timeout=0.3, what="sleeper")
        self.assertEqual((f.message, f.exit, f.error), ("sleeper does not answer within 0.3 s", 1, "action_failed"))

    def test_a_non_zero_exit_names_the_call_and_what_the_program_said(self):
        f = self.fails(py("import sys; print('first line\\n  second\\x1b[2J line', file=sys.stderr); sys.exit(3)"),
                       what="the thing", error="other_code", vm="work")
        self.assertEqual((f.message, f.exit, f.error, f.params),
                         ("the thing failed: first line second[2J line", 1, "other_code",
                          {"program": os.path.basename(sys.executable), "vm": "work"}))

    def test_the_call_is_named_by_its_command_line_by_default(self):
        f = self.fails(["sh", "-c", "exit 4", "x"])
        self.assertEqual(f.message, "sh -c exit 4 x failed: exit 4")

    def test_only_the_last_characters_of_the_error_output(self):
        f = self.fails(py("import sys; sys.stderr.write('a' * 1000 + 'b' * 300); sys.exit(1)"), what="p")
        self.assertEqual(f.message, "p failed: " + "b" * 300)
        self.assertEqual(programs.STDERR, 300)

    def test_exit_codes_that_count_as_an_answer(self):
        self.assertEqual(programs.run(["sh", "-c", "exit 1"], 30, ok=(0, 1)).returncode, 1)
        self.assertEqual(programs.run(["sh", "-c", "exit 7"], 30, ok=None).returncode, 7)
        self.assertEqual(self.fails(["sh", "-c", "exit 2"], ok=(0, 1)).message, "sh -c exit 2 failed: exit 2")

    def test_the_c_locale_and_another_environment(self):
        argv = py("import os; print(os.environ.get('LC_ALL'), os.environ.get('X'))")
        with mock.patch.dict(os.environ, {"LC_ALL": "de_DE.UTF-8", "X": "1"}):
            self.assertEqual(programs.run(argv, 30).stdout, "de_DE.UTF-8 1\n")
            self.assertEqual(programs.run(argv, 30, c_locale=True).stdout, "C 1\n")
            self.assertEqual(programs.run(argv, 30, env={"X": "2"}, c_locale=True).stdout, "C 2\n")

    def test_systemctl_runs_in_the_c_locale_with_its_time_limit(self):
        with mock.patch.object(programs.subprocess, "run") as run:
            run.return_value = programs.subprocess.CompletedProcess([], 0, "", "")
            programs.systemctl("start", "x.service")
            programs.systemctl("is-active", "x.service", timeout=60, ok=None)
        (first, kw1), (second, kw2) = run.call_args_list
        self.assertEqual((first[0], kw1["timeout"], kw1["env"]["LC_ALL"]),
                         (["systemctl", "start", "x.service"], 120, "C"))
        self.assertEqual((second[0], kw2["timeout"]), (["systemctl", "is-active", "x.service"], 60))

    def test_summary_without_error_output_is_the_exit_code(self):
        r = programs.subprocess.CompletedProcess([], 5, "", None)
        self.assertEqual(programs.summary(r), "exit 5")
        with self.assertRaises(Fail) as caught:
            programs.fail(r, "virsh start", program="virsh")
        self.assertEqual((caught.exception.message, caught.exception.params), ("virsh start failed: exit 5",
                                                                               {"program": "virsh"}))


class QuietlyTest(unittest.TestCase):
    def test_whatever_goes_wrong_is_ignored(self):
        programs.quietly(["sh", "-c", "echo out; echo err >&2; exit 3"], 30)
        programs.quietly(["/nonexistent/program"], 30)
        programs.quietly(py("import time; time.sleep(30)"), 0.2)


if __name__ == "__main__":
    unittest.main()
