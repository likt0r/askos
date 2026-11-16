"""A VM's and a unit's state in GLOSSARY terms (agentbox/states.py): each of libvirt's and
systemd's words, a Pause (a managed save) apart from libvirt's own "paused", a Build unit
that still runs while it stops, an Orca connection that no longer counts while it closes.

virsh and systemctl answer through a stand-in for subprocess.run; nothing of the Host is touched.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from agentbox import describe, states  # noqa: E402
from agentbox.errors import Fail  # noqa: E402

CONNECTION = "agentbox-gui-work@7-10.0.0.1:9000-192.168.1.20:51000.service"
WORDS = ("running", "shut off", "paused", "in shutdown", "crashed", "pmsuspended", "idle", "blocked")


class Answers:
    """subprocess.run for virsh and systemctl: answers[verb] is (exit code, stdout, stderr)
    or an exception to raise; the verb is the first argument after the program's options."""

    def __init__(self, **answers):
        self.answers = answers

    def __call__(self, argv, **_):
        args = argv[4:] if argv[0] == "virsh" else argv[1:]
        answer = self.answers.get(args[0].replace("-", "_"), (1, "", "not asked for"))
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(argv, *answer)


def asking(**answers):
    return mock.patch("subprocess.run", Answers(**answers))


def domstate(word):
    return (0, word + "\n", "") if word is not None else (1, "", "error: failed to connect to the hypervisor")


def dominfo(saved):
    return (0, f"Id:             -\nName:           agentbox-work\nState:          shut off\n"
               f"Managed save:   {'yes' if saved else 'no'}\n", "")


class VmStateTest(unittest.TestCase):
    def test_running_only_when_libvirt_says_so(self):
        for word, expected in (("running", True), ("shut off", False), ("paused", False), ("idle", False),
                               (None, False)):
            with self.subTest(word=word), asking(domstate=domstate(word)):
                self.assertIs(states.running("work"), expected)

    def test_a_pause_is_a_managed_save(self):
        for saved in (True, False):
            with self.subTest(saved=saved), asking(dominfo=dominfo(saved)):
                self.assertIs(states.has_pause("work"), saved)

    def test_status_calls_a_saved_vm_paused(self):
        with asking(dominfo=dominfo(True), domstate=domstate("shut off")):
            self.assertEqual(states.status("work"), "paused")
        for word in WORDS:
            with self.subTest(word=word), asking(dominfo=dominfo(False), domstate=domstate(word)):
                self.assertEqual(states.status("work"), word)
        with asking(dominfo=dominfo(False), domstate=domstate(None)):
            self.assertEqual(states.status("work"), "missing")

    def test_the_control_room_tells_a_pause_from_libvirts_paused(self):
        expected = {"running": "running", "shut off": "stopped", "paused": "suspended",
                    "in shutdown": "shutting_down", "crashed": "crashed", "pmsuspended": "suspended",
                    "idle": "running", "blocked": "running", "missing": "unknown", "something new": "unknown"}
        for word, state in expected.items():
            with self.subTest(word=word):
                self.assertEqual(states.control_room(word, False), state)
                self.assertEqual(states.control_room(word, True), "paused")

    def test_what_reads_its_definition_at_the_next_start(self):
        for word, expected in (("shut off", True), ("missing", True), ("running", False), ("paused", False),
                               ("crashed", False)):
            with self.subTest(word=word):
                self.assertIs(states.starts_anew(word), expected)

    def test_off(self):
        for answer, expected in ((domstate("shut off"), (True, "shut off")),
                                 (domstate("running"), (False, "running")),
                                 (domstate("paused"), (False, "paused")),
                                 ((0, "\n", ""), (False, "unknown")),
                                 ((1, "", "error: failed to get domain 'agentbox-work'"), (True, "not defined")),
                                 (domstate(None), (False, "unknown"))):
            with self.subTest(answer=answer), asking(domstate=answer):
                self.assertEqual(states.off("work"), expected)

    def test_a_qemu_process(self):
        for word, expected in (("running", True), ("paused", True), ("pmsuspended", True), ("in shutdown", True),
                               ("shut off", False), ("crashed", False), (None, False)):
            with self.subTest(word=word), asking(domstate=domstate(word)):
                self.assertIs(states.has_process("work"), expected)

    def test_a_network(self):
        info = "Name:           agentbox-work\nUUID:           1\nActive:         {}\nPersistent:     yes\n" \
               "Autostart:      {}\nBridge:         abr-work\n"
        for active, autostart in (("yes", "yes"), ("no", "yes"), ("yes", "no"), ("no", "no")):
            with self.subTest(active=active, autostart=autostart), \
                    asking(net_info=(0, info.format(active, autostart), "")):
                self.assertEqual(states.network("work"), (active == "yes", autostart == "yes"))
        with asking(net_info=(1, "", "error: failed to get network 'agentbox-work'")):
            self.assertIsNone(states.network("work"))
        with asking(net_info=(1, "", "error: failed to connect to the hypervisor")), \
                self.assertRaises(Fail) as caught:
            states.network("work")
        self.assertEqual((caught.exception.error, caught.exception.params), ("action_failed",
                                                                           {"program": "virsh", "vm": "work"}))


class UnitStateTest(unittest.TestCase):
    def build_running(self, answer):
        with asking(is_active=answer):
            return states.build_running("agentbox-build@base.service")

    def test_a_build_stopping_still_runs(self):
        for word in ("active", "activating", "deactivating", "reloading"):
            with self.subTest(word=word):
                self.assertTrue(self.build_running((0 if word == "active" else 3, word + "\n", "")))
        for word in ("inactive", "failed", ""):
            with self.subTest(word=word):
                self.assertFalse(self.build_running((3, word + "\n", "")))
        self.assertFalse(self.build_running(FileNotFoundError()))
        self.assertFalse(self.build_running(subprocess.TimeoutExpired("systemctl", 30)))

    def test_the_update_runs_while_its_unit_stops_as_a_build_does(self):
        update = "agentbox-build@update.service"
        for word in ("active", "activating", "deactivating", "reloading", "inactive", "failed"):
            with self.subTest(word=word):
                expected = word not in ("inactive", "failed")
                self.assertIs(states.build_active(word), expected)
                self.assertIs(describe.updating([(update, "loaded", word, "running", "")]), expected)
                # another Build's unit is no update
                self.assertFalse(describe.updating([("agentbox-build@base.service", "loaded", word, "running", "")]))
        self.assertFalse(describe.updating([]))

    def test_the_control_room_gets_a_unit_s_state_in_its_own_words(self):
        for load, active, expected in (("loaded", "active", "running"), ("loaded", "activating", "running"),
                                       ("loaded", "reloading", "running"), ("loaded", "deactivating", "stopping"),
                                       ("loaded", "inactive", "stopped"), ("loaded", "failed", "failed"),
                                       ("not-found", "inactive", "failed"), ("error", "inactive", "failed"),
                                       ("bad-setting", "inactive", "failed"),
                                       ("masked", "inactive", "stopped")):
            with self.subTest(load=load, active=active):
                self.assertEqual(states.unit_state(load, active), expected)
                [row] = describe.services([("agentbox-feeds.service", load, active, "dead", "")], lambda names: {})
                self.assertEqual(row["state"], expected)
        for active, sub, text in (("active", "running", "active (running)"), ("failed", "failed", "failed"),
                                  ("inactive", "", "inactive"), ("deactivating", "stop-sigterm", "deactivating (stop-sigterm)")):
            with self.subTest(active=active, sub=sub):
                self.assertEqual(states.unit_text(active, sub), text)
                [row] = describe.services([("agentbox-feeds.service", "loaded", active, sub, "")], lambda names: {})
                self.assertEqual(row["text"], text)

    def test_a_connection_being_closed_is_no_connection(self):
        for word in ("active", "activating", "reloading"):
            with self.subTest(word=word):
                self.assertTrue(states.connection_open(word))
                self.assertEqual(describe.devices([(CONNECTION, "loaded", word, "running", "")]),
                                 {"work": [{"address": "192.168.1.20", "connections": 1}]})
        for word in ("deactivating", "inactive", "failed"):
            with self.subTest(word=word):
                self.assertFalse(states.connection_open(word))
                self.assertEqual(describe.devices([(CONNECTION, "loaded", word, "dead", "")]), {})

    def test_started_and_enabled(self):
        with asking(is_active=(0, "active\n", "")):
            self.assertTrue(states.started("agentbox-vmnet.service", 60))
        with asking(is_active=(3, "inactive\n", "")):
            self.assertFalse(states.started("agentbox-vmnet.service", 60))
        with asking(is_enabled=(0, "enabled\n", "")):
            self.assertTrue(states.enabled("agentbox-vmnet.service", 60))
        for word in ("disabled", "static", "masked", "enabled-runtime"):
            with self.subTest(word=word), asking(is_enabled=(1, word + "\n", "")):
                self.assertFalse(states.enabled("agentbox-vmnet.service", 60))

    def test_a_unit_that_failed_since(self):
        def show(result, began):
            return (0, f"Result={result}\nInactiveExitTimestamp={began}\n", "")
        for answer, expected in ((show("exit-code", "@1000"), True), (show("timeout", "@1500"), True),
                                 (show("exit-code", "@999"), False), (show("success", "@1000"), False),
                                 (show("", "@1000"), False), (show("exit-code", ""), False),
                                 ((1, "", "Failed to connect to bus"), False), (FileNotFoundError(), False)):
            with self.subTest(answer=answer), asking(show=answer):
                self.assertIs(states.failed_since("agentbox-build@base.service", 1000), expected)


# states.py, and virsh.py, which runs virsh for it and hands over domstate's word
OWN = {os.path.join(REPO, "agentbox", name) for name in ("states.py", "virsh.py")}


class OneModuleTest(unittest.TestCase):
    # libvirt's and systemd's state words and questions, and a comparison of virsh.state()
    WORDS = re.compile(r'"(shut off|crashed|pmsuspended|in shutdown|activating|deactivating|reloading|domstate|'
                       r'inactive|is-active|is-enabled|Active|Autostart)"|\b(Active|Autostart):|Managed save|'
                       r'virsh\.state\([^)]*\)\s*[!=]=')

    def test_no_other_module_reads_the_words(self):
        for directory in ("agentbox", "proxy", "bin", "web"):
            for root, _, files in os.walk(os.path.join(REPO, directory)):
                for name in files:
                    path = os.path.join(root, name)
                    if path in OWN or "node_modules" in root:
                        continue
                    if not (name.endswith(".py") or directory in ("bin", "proxy") or name == "agentbox-web"):
                        continue
                    with open(path, errors="replace") as f:
                        text = f.read()
                    with self.subTest(path=os.path.relpath(path, REPO)):
                        self.assertIsNone(self.WORDS.search(text))
                        if path != os.path.join(REPO, "agentbox", "programs.py"):
                            self.assertNotRegex(text, r'[\[(]\s*["\']systemctl["\']')


if __name__ == "__main__":
    unittest.main()
