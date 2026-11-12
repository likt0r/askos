"""Activity (#15): the actor of a call, the recording of state-changing commands in the
journal, the timeline of `agentbox activity` and its retention.

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root. Journal entries go to
a stand-in for journald's socket under that root (fake_journald.py), the timeline comes
from a fake journalctl (fake_journalctl.py), and virsh, qemu-img, ss, systemctl,
virt-install and the Orca CLI are the usual fakes.

Run from the repo root: python3 -m unittest discover -s tests
"""
import ast
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from fake_journald import Journald  # noqa: E402
from journal_writers import (CLI, CONTROL_ROOM, FORGED, GH, MEMBER, PROXY, proxy, token_service,  # noqa: E402
                             write_group, write_passwd, writer, written_by)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(REPO, "bin", "agentbox")
TESTS = os.path.join(REPO, "tests")
DAY = 86400
CODE = "orca://pair?code=Ab3_x-9"
TOKEN = "sk-ant-oat01-AbCdEf-123ghi_JKL"
FIREFOX = "Firefox/Linux (192.0.2.23)"

# agentbox-suspicious: evil.* is suspicious, everything else unremarkable
CLASSIFIER = '''#!/bin/sh
case "$1" in evil.*) echo exfil; exit 2;; esac
echo
'''
# curl standing in for Anthropic: prints the answer in curl-answer (after reading the header from stdin)
CURL = '''#!/bin/sh
cat > /dev/null
cat "{dir}/curl-answer"
'''


def fake(path, program):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nexec python3 "{os.path.join(TESTS, program)}" "$@"\n')
    os.chmod(path, 0o755)


def script(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


def vm_journal(message):
    """A record of the VM's own journal, as the guest agent hands it over (pair)."""
    return {"MESSAGE": message, "__REALTIME_TIMESTAMP": "1761732000123456", "__CURSOR": "c1", "_BOOT_ID": "b1",
            "PRIORITY": "6", "SYSLOG_IDENTIFIER": "orca", "_SYSTEMD_UNIT": "agentbox-orca.service"}


class ActivityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.root = os.path.join(self.dir, "root")
        for sub in ("etc/agentbox/secrets", "etc/agentbox/github", "etc/systemd/system", "var/lib/agentbox/vms",
                    "var/lib/agentbox/images"):
            os.makedirs(self.path(sub))
        vm_records.write(self.root)
        write_passwd(self.root)
        for name, program in (("virsh", "fake_virsh.py"), ("qemu-img", "fake_qemu_img.py"), ("ss", "fake_ss.py"),
                              ("systemctl", "fake_systemctl.py"), ("virt-install", "fake_virt_install.py"),
                              ("journalctl", "fake_journalctl.py"), ("firewall-cmd", "fake_firewall_cmd.py")):
            fake(os.path.join(self.dir, "bin", name), program)
        fake(self.path("opt/Orca/resources/bin/orca-ide"), "fake_orca.py")
        script(self.path("usr/libexec/agentbox-suspicious"), CLASSIFIER)
        script(os.path.join(self.dir, "bin", "curl"), CURL.format(dir=self.dir))
        self.journald = self.journal_at("run/systemd/journal.agentbox/socket")
        self.env = {}
        self.scenario()

    def path(self, rel):
        return os.path.join(self.root, rel)

    def journal_at(self, rel):
        journald = Journald(self.path(rel))
        self.addCleanup(journald.close)
        return journald

    def scenario(self, **s):
        with open(os.path.join(self.dir, "scenario.json"), "w") as f:
            json.dump(s, f)

    def run_agentbox(self, *args, stdin="", **env):
        e = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
             "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC", **self.env, **env}
        r = subprocess.run([AGENTBOX, *args], input=stdin, capture_output=True, text=True, env=e, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def entries(self, journald=None):
        return (journald or self.journald).entries()

    def raw(self):
        return b"".join(self.journald.raw())

    def calls(self, cmd=None):
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                calls = [json.loads(line) for line in f]
        except FileNotFoundError:
            return []
        return [c for c in calls if cmd is None or c["cmd"] == cmd]

    def allowlist_exists(self):
        return os.path.exists(self.path("etc/agentbox/allowlist.json"))

    def orca_listens(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        self.addCleanup(srv.close)
        self.env["AGENTBOX_ORCA_ADDR"] = "127.0.0.1:%d" % srv.getsockname()[1]


class ActorTest(ActivityTest):
    """--actor is a global option before the command; without it the actor is the user."""

    def test_both_forms_before_the_command(self):
        self.assertEqual(self.run_agentbox("--actor", FIREFOX, "allow", "work", "example.org")[0], 0)
        self.assertEqual(self.run_agentbox("--actor=timer", "disallow", "work", "example.org")[0], 0)
        self.assertEqual([e["AGENTBOX_ACTOR"] for e in self.entries()], [FIREFOX, "timer"])

    def test_the_command_does_not_see_it(self):
        code, out, err = self.run_agentbox("--actor=timer", "allow", "work", "example.org", "--json")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual((result["scope"], result["domain"], result["message"]),
                         ("work", "example.org", "work: example.org allowed"))
        # but it is who added the entry
        self.assertEqual(result["added_by"], "timer")

    def test_without_actor_the_user_who_called_sudo(self):
        self.run_agentbox("close-web", "work", SUDO_USER="alice")
        self.assertEqual(self.entries()[-1]["AGENTBOX_ACTOR"], "alice")

    def test_without_actor_or_sudo_the_user_running_it(self):
        e = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
        r = subprocess.run([AGENTBOX, "close-web", "work"], capture_output=True, text=True, timeout=60,
                           env={**e, "PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
                                "AGENTBOX_ROOT": self.root, "FAKE_DIR": self.dir})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.entries()[-1]["AGENTBOX_ACTOR"], pwd.getpwuid(os.getuid()).pw_name)

    def test_longest_actor(self):
        self.assertEqual(self.run_agentbox("--actor=" + "a" * 100, "close-web", "work")[0], 0)
        self.assertEqual(self.entries()[-1]["AGENTBOX_ACTOR"], "a" * 100)

    def test_invalid_actor_is_refused_before_the_command_runs(self):
        for actor in ("", " ", " timer", "timer ", "a" * 101, "two\nlines", "\x1b[31mred", "a\u202eb", "-x",
                      "a\u00a0b", "tab\there"):
            with self.subTest(actor=actor):
                code, out, err = self.run_agentbox(f"--actor={actor}", "allow", "work", "example.org")
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, "agentbox: invalid value for --actor (1 to 100 printable characters)\n")
        self.assertFalse(self.allowlist_exists())
        self.assertEqual(self.entries(), [])

    def test_actor_needs_a_value(self):
        self.assertEqual(self.run_agentbox("--actor"), (2, "", "agentbox: --actor needs a value\n"))


class RecordTest(ActivityTest):
    """Every state-changing command is recorded centrally, with actor and outcome."""

    def test_state_changing_command(self):
        self.assertEqual(self.run_agentbox("--actor", FIREFOX, "allow", "work", "example.org"),
                         (0, "work: example.org allowed\n", ""))
        [e] = self.entries()
        self.assertEqual(e, {
            "MESSAGE": f"work allow ok ({FIREFOX}): Allow example.org", "PRIORITY": "5",
            "SYSLOG_IDENTIFIER": "agentbox-activity", "AGENTBOX_KIND": "allowlist", "AGENTBOX_ACTION": "allow",
            "AGENTBOX_CODE": "allow", "AGENTBOX_VM": "work", "AGENTBOX_ACTOR": FIREFOX, "AGENTBOX_OUTCOME": "ok",
            "AGENTBOX_DESCRIPTION": "Allow example.org", "AGENTBOX_PARAM_DOMAIN": "example.org",
            "AGENTBOX_PARAM_SCOPE": "work"})

    def test_create_and_delete_name_the_vm(self):
        images = self.path("var/lib/agentbox/images")
        open(os.path.join(images, "base-20261101.qcow2"), "w").close()
        os.symlink("base-20261101.qcow2", os.path.join(images, "base.qcow2"))
        self.scenario(vms={"lab": {"defined": False}})
        for args in (["create", "lab", "--memory-mib", "8192"], ["delete", "lab", "--confirm", "lab"]):
            code, _, err = self.run_agentbox("--actor", FIREFOX, *args)
            self.assertEqual(code, 0, err)
        self.assertEqual([(e["AGENTBOX_ACTION"], e.get("AGENTBOX_VM"), e["AGENTBOX_ACTOR"], e["AGENTBOX_OUTCOME"])
                          for e in self.entries()],
                         [("create", "lab", FIREFOX, "ok"), ("delete", "lab", FIREFOX, "ok")])

    def test_failure_with_its_reason(self):
        code, _, err = self.run_agentbox("--actor=timer", "allow", "work", "evil.example")
        self.assertEqual(code, 1)
        [e] = self.entries()
        self.assertEqual((e["AGENTBOX_OUTCOME"], e["AGENTBOX_EXIT"], e["PRIORITY"], e["AGENTBOX_DESCRIPTION"]),
                         ("failed", "1", "4", "Allow evil.example: evil.example is suspicious (exfil). "
                                              "Allowing it only with --force"))

    def test_code_parameters_and_error_code(self):
        """#69: the action is the entry's code, its parameters are fields of their own, and a
        failure adds its error code and that error's parameters; the English text stays."""
        self.assertEqual(self.run_agentbox("allow", "all", "pypi.org", "--for", "1h", "--note", "x", "--json")[0], 0)
        self.assertEqual(self.run_agentbox("allow", "work", "evil.example", "--temporary")[0], 1)
        ok, failed = [{k: v for k, v in e.items() if k.startswith("AGENTBOX_") and k != "AGENTBOX_ACTOR"}
                      for e in self.entries()]
        self.assertEqual(ok, {
            "AGENTBOX_KIND": "allowlist", "AGENTBOX_ACTION": "allow", "AGENTBOX_CODE": "allow",
            "AGENTBOX_OUTCOME": "ok", "AGENTBOX_DESCRIPTION": "Allow pypi.org for all VMs for 1 hour",
            "AGENTBOX_PARAM_DOMAIN": "pypi.org", "AGENTBOX_PARAM_SCOPE": "all", "AGENTBOX_PARAM_DURATION": "1h"})
        self.assertEqual(failed, {
            "AGENTBOX_KIND": "allowlist", "AGENTBOX_ACTION": "allow", "AGENTBOX_CODE": "allow", "AGENTBOX_VM": "work",
            "AGENTBOX_OUTCOME": "failed", "AGENTBOX_EXIT": "1",
            "AGENTBOX_DESCRIPTION": "Allow evil.example temporarily: evil.example is suspicious (exfil). "
                                    "Allowing it only with --force",
            "AGENTBOX_PARAM_DOMAIN": "evil.example", "AGENTBOX_PARAM_SCOPE": "work",
            "AGENTBOX_PARAM_TEMPORARY": "true", "AGENTBOX_ERROR": "domain_suspicious",
            "AGENTBOX_ERROR_PARAM_DOMAIN": "evil.example"})

    def test_parameters_per_command(self):
        images = self.path("var/lib/agentbox/images")
        open(os.path.join(images, "base-20261101.qcow2"), "w").close()
        os.symlink("base-20261101.qcow2", os.path.join(images, "base.qcow2"))
        self.scenario(vms={"lab": {"defined": False}, "work": {"state": "shut off"}})
        for args in (["reset", "work", "--no-start"], ["extend", "all", "a.example", "--for", "8h", "--force"],
                     ["set-settings", "reset_time=04:00", "ghp_notakey", "--json"],
                     ["set-github-app", "-----BEGIN RSA PRIVATE KEY-----", "--no-prompt"],
                     ["create", "lab", "--image", "base", "--memory-mib", "8192"],
                     ["delete", "lab", "--confirm", "lab", "--delete-data-disk"], ["open-web", "work", "x30"]):
            self.run_agentbox(*args)
        self.assertEqual([(e["AGENTBOX_ACTION"], {k[len("AGENTBOX_PARAM_"):]: v for k, v in e.items()
                                                  if k.startswith("AGENTBOX_PARAM_")}) for e in self.entries()], [
            ("reset", {"NO_START": "true"}),
            ("extend", {"DOMAIN": "a.example", "SCOPE": "all", "DURATION": "8h", "FORCE": "true"}),
            # Settings by their keys only, the App ID only when it is one
            ("set-settings", {"SETTINGS": "reset_time"}),
            ("set-github-app", {}),
            ("create", {"IMAGE": "base"}),
            ("delete", {"DELETE_DATA_DISK": "true"}),
            ("open-web", {}),
        ])

    def test_error_parameters_have_the_care_of_the_commands(self):
        """A failure's parameters keep a Setting only when it is a known one, as the command's
        own do: unknown_setting names what was passed, which could be a pasted secret."""
        for args in (["set-settings", "my-pasted-secret=x"], ["set-settings", f"{TOKEN}=x"],
                     ["set-settings", "reset_time=99:99"]):
            self.assertNotEqual(self.run_agentbox(*args)[0], 0, args)
        entries = [{k: v for k, v in e.items() if k.startswith(("AGENTBOX_ERROR", "AGENTBOX_PARAM_"))}
                   for e in self.entries()]
        self.assertEqual(entries, [
            {"AGENTBOX_ERROR": "unknown_setting"},
            {"AGENTBOX_ERROR": "unknown_setting"},
            {"AGENTBOX_ERROR": "invalid_setting", "AGENTBOX_ERROR_PARAM_SETTING": "reset_time",
             "AGENTBOX_PARAM_SETTINGS": "reset_time"}])
        # the description says what failed, cleaned like every value
        self.assertNotIn(b"sk-ant", self.raw())

    def test_a_parameter_the_timeline_would_drop_is_refused(self):
        sys.path.insert(0, REPO)
        from agentbox import activity
        with mock.patch.object(activity, "_send") as send, mock.patch.object(activity, "_actor", "timer"):
            for name in ("a" * 29, "Domain", "9x", "-x"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    activity.record("action", "x", "y", params={name: "v"})
            activity.record("action", "x", "y", params={"a" * 28: "v"})
        self.assertEqual(send.call_count, 1)

    def test_interrupt_and_internal_errors_have_codes(self):
        # in this process: neither comes from a command on purpose (focus off names no VM, so
        # nothing on the Host is looked at)
        sys.path.insert(0, REPO)
        from agentbox import activity
        sent = []
        with mock.patch.object(activity, "_send", sent.append), mock.patch.object(activity, "_actor", "timer"):
            for exc in (KeyboardInterrupt, KeyError):
                with self.subTest(exc=exc), self.assertRaises(exc):
                    with activity.command("focus", ["off"]):
                        raise exc("x")
        fields = [dict(line.split("=", 1) for line in data.decode().splitlines()) for data in sent]
        self.assertEqual([(f["AGENTBOX_ERROR"], f["AGENTBOX_DESCRIPTION"], f["AGENTBOX_PARAM_OFF"],
                           f.get("AGENTBOX_ERROR_PARAM_TYPE")) for f in fields],
                         [("interrupted", "Focus off: interrupted", "true", None),
                          ("unexpected_error", "Focus off: internal error (KeyError)", "true", "KeyError")])

    def test_a_reset_whose_start_fails_is_a_failure(self):
        # #52: it used to exit 0 with virsh's message on stderr
        images = self.path("var/lib/agentbox/images")
        open(os.path.join(images, "base-20261101.qcow2"), "w").close()
        os.symlink("base-20261101.qcow2", os.path.join(images, "base.qcow2"))
        self.scenario(vms={"work": {"start_fails": "active"}})
        code, _, _ = self.run_agentbox("--actor", FIREFOX, "reset", "work")
        self.assertEqual(code, 1)
        [e] = self.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_VM"], e["AGENTBOX_OUTCOME"]), ("reset", "work", "failed"))
        self.assertIn("fresh System disk, but the start fails", e["AGENTBOX_DESCRIPTION"])

    def test_kind_and_description_per_command(self):
        self.write_conf("open-work", "4102444800\n")
        self.write_conf("watchlist.json", '{"entries": []}\n')       # Open web needs a readable Watchlist
        for args in (["open-web", "work", "30"], ["close-web", "work"], ["allow", "priv", "evil.example", "--force"],
                     ["disallow", "priv", "evil.example"], ["set-owners", "priv", "alice", "my-org", "--json"],
                     ["pause", "work"], ["focus", "priv"], ["focus", "off"]):
            self.assertEqual(self.run_agentbox(*args)[0], 0, args)
        self.assertEqual([(e["AGENTBOX_KIND"], e["AGENTBOX_ACTION"], e.get("AGENTBOX_VM"), e["AGENTBOX_DESCRIPTION"])
                          for e in self.entries()], [
            ("allowlist", "open-web", "work", "Open web for 30 minutes"),
            ("allowlist", "close-web", "work", "Close Open web"),
            ("allowlist", "allow", "priv", "Allow evil.example with --force"),
            ("allowlist", "disallow", "priv", "Remove the Allowlist entry evil.example"),
            ("credentials", "set-owners", "priv", "Set GitHub owners: alice, my-org"),
            ("action", "pause", "work", "Pause"),
            ("action", "focus", "priv", "Focus on priv"),
            ("action", "focus", None, "Focus off"),
        ])

    def test_memory_limit(self):
        """A change is recorded with its Actor; the same limit again changes nothing."""
        for mib, code in (("16384", 0), ("16384", 0), ("512", 1)):
            self.assertEqual(self.run_agentbox("--actor", FIREFOX, "set-memory", "work", mib)[0], code, mib)
        self.assertEqual([(e["AGENTBOX_KIND"], e["AGENTBOX_ACTION"], e["AGENTBOX_VM"], e["AGENTBOX_ACTOR"],
                           e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"]) for e in self.entries()], [
            ("action", "set-memory", "work", FIREFOX, "ok", "Memory limit 16384 MiB"),
            ("action", "set-memory", "work", FIREFOX, "failed",
             "Memory limit 512 MiB: memory limit (MiB) must be a whole number from 1024 to 1048576"),
        ])

    def test_container_image_cleanup(self):
        """A change is recorded with the values that apply; a call that changes nothing, or
        fails, as the others."""
        for args, code in ((("work", "--days", "14"), 0), (("work", "--days", "14"), 0), (("work", "off"), 0),
                           (("work", "on", "--days", "30"), 0), (("work", "--days", "0"), 1)):
            self.assertEqual(self.run_agentbox("--actor", FIREFOX, "set-container-image-cleanup", *args)[0], code, args)
        self.assertEqual([(e["AGENTBOX_ACTION"], e["AGENTBOX_VM"], e["AGENTBOX_ACTOR"], e["AGENTBOX_OUTCOME"],
                           e.get("AGENTBOX_PARAM_ENABLED"), e.get("AGENTBOX_PARAM_DAYS"))
                          for e in self.entries()], [
            ("set-container-image-cleanup", "work", FIREFOX, "ok", "true", "14"),
            ("set-container-image-cleanup", "work", FIREFOX, "ok", "false", "14"),
            ("set-container-image-cleanup", "work", FIREFOX, "ok", "true", "30"),
            ("set-container-image-cleanup", "work", FIREFOX, "failed", None, None),
        ])

    def test_vm_variables(self):
        """set-env with names and values (not secret), --replace as a flag, unset-env with the
        names; the same values again change nothing; a refused name is a failed entry."""
        for args, code in ((["set-env", "work", "A=1", "B=x y"], 0), (["set-env", "work", "A=1"], 0),
                           (["set-env", "work", "NODE_OPTIONS="], 1), (["set-env", "work", "C=3", "--replace"], 0),
                           (["unset-env", "work", "C", "--json"], 0), (["env", "work"], 0)):
            self.assertEqual(self.run_agentbox("--actor", FIREFOX, *args)[0], code, args)
        entries = self.entries()
        self.assertEqual([(e["AGENTBOX_ACTION"], e["AGENTBOX_VM"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"])
                          for e in entries], [
            ("set-env", "work", "ok", "Set VM variables: A=1, B=x y"),
            ("set-env", "work", "failed", "Set VM variables: NODE_OPTIONS=: NODE_OPTIONS cannot be a VM variable: "
                                          "a Safeguard or Agent Box itself sets or reads it"),
            ("set-env", "work", "ok", "Replace the VM variables: C=3"),
            ("unset-env", "work", "ok", "Remove VM variables: C"),
        ])
        self.assertEqual(entries[0]["AGENTBOX_PARAM_ENV"], "A=1, B=x y")
        self.assertEqual((entries[1]["AGENTBOX_ERROR"], entries[1]["AGENTBOX_ERROR_PARAM_NAME"]),
                         ("env_name_reserved", "NODE_OPTIONS"))
        self.assertEqual((entries[2]["AGENTBOX_PARAM_ENV"], entries[2]["AGENTBOX_PARAM_REPLACE"]), ("C=3", "true"))
        self.assertEqual(entries[3]["AGENTBOX_PARAM_NAMES"], "C")

    def test_many_vm_variables_show_that_the_entry_is_cut(self):
        """Every parameter and the description keep 500 characters: with 64 variables the names
        come first, and a cut list says how many it leaves out."""
        values = [f"V{i}=" + "x" * 40 for i in range(64)]
        names = [f"NAME_{i}_" + "N" * 50 for i in range(64)]
        for args in (["set-env", "work", *(f"{n}=1" for n in names)], ["unset-env", "work", *names],
                     ["set-env", "work", *values]):
            self.assertEqual(self.run_agentbox("--actor", FIREFOX, *args)[0], 0, args)
        _, unset_env, set_env = self.entries()
        # all names of set-env fit, the values do not
        self.assertEqual(set_env["AGENTBOX_DESCRIPTION"],
                         "Set VM variables: " + ", ".join(f"V{i}" for i in range(64)) + " (values left out, too long)")
        shown = set_env["AGENTBOX_PARAM_ENV"]
        self.assertLessEqual(len(shown), 500)
        pairs, more = shown.rsplit(", … +", 1)
        self.assertEqual(pairs.split(", "), values[:len(pairs.split(", "))])
        self.assertEqual(int(more), 64 - len(pairs.split(", ")))
        # names that do not fit either
        text = unset_env["AGENTBOX_DESCRIPTION"]
        listed, more = re.fullmatch(r"Remove VM variables: (.*) and (\d+) more", text).groups()
        self.assertEqual(listed.split(", "), names[:len(listed.split(", "))])
        self.assertEqual(int(more), 64 - len(listed.split(", ")))
        listed, more = unset_env["AGENTBOX_PARAM_NAMES"].rsplit(", … +", 1)
        self.assertEqual(listed.split(", "), names[:len(listed.split(", "))])
        self.assertEqual(int(more), 64 - len(listed.split(", ")))

    def write_conf(self, name, text):
        with open(self.path(f"etc/agentbox/{name}"), "w") as f:
            f.write(text)

    def test_read_only_commands_are_not_recorded(self):
        for args in (["status"], ["status", "--json"], ["allowlist", "work"], ["owners", "work", "--json"],
                     ["blocked", "work"], ["alerts", "priv", "--json"], ["activity"], ["activity", "--json"],
                     ["harmless"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_agentbox("--actor=timer", *args)[0], 0)
        self.run_agentbox("--actor=timer", "check-domain", "example.org")     # classifier is a stand-in here
        self.assertEqual(self.entries(), [])

    def test_unknown_command_is_not_recorded(self):
        self.assertEqual(self.run_agentbox("--actor=timer", "nonsense")[0], 1)
        self.assertEqual(self.entries(), [])

    def test_unknown_vm_is_a_failed_attempt_without_vm(self):
        self.assertEqual(self.run_agentbox("allow", "dev", "example.org")[0], 1)
        [e] = self.entries()
        self.assertNotIn("AGENTBOX_VM", e)
        self.assertEqual((e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"]),
                         ("failed", "Allow example.org: No such VM: dev"))

    def test_hostile_arguments_stay_one_clean_line(self):
        self.run_agentbox("allow", "work", "x\x1b[31m\nAGENTBOX_KIND=signin\u202e")
        [e] = self.entries()
        self.assertEqual(e["AGENTBOX_KIND"], "allowlist")
        self.assertEqual(e["AGENTBOX_DESCRIPTION"],
                         "Allow x AGENTBOX_KIND=signin: invalid domain: x AGENTBOX_KIND=signin")

    def test_wake_is_recorded_only_when_it_starts_the_vm(self):
        """Every Orca connection wakes the VM; a running VM is no Activity."""
        self.orca_listens()
        self.assertEqual(self.run_agentbox("--actor=Orca (192.0.2.23)", "wake", "work")[0], 0)
        self.assertEqual(self.entries(), [])
        self.scenario(vms={"work": {"state": "shut off"}})
        self.assertEqual(self.run_agentbox("--actor=Orca (192.0.2.23)", "wake", "work")[0], 0)
        [e] = self.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_VM"], e["AGENTBOX_ACTOR"], e["AGENTBOX_DESCRIPTION"]),
                         ("wake", "work", "Orca (192.0.2.23)", "Wake"))

    def test_without_the_namespace_into_the_default_journal(self):
        self.journald.close()
        os.remove(self.journald.path)
        default = self.journal_at("run/systemd/journal/socket")
        self.assertEqual(self.run_agentbox("close-web", "work"), (0, "work: Open web closed\n", ""))
        self.assertEqual([e["AGENTBOX_ACTION"] for e in self.entries(default)], ["close-web"])

    def test_without_any_journal_the_command_works(self):
        self.journald.close()
        os.remove(self.journald.path)
        self.assertEqual(self.run_agentbox("close-web", "work"), (0, "work: Open web closed\n", ""))

    def test_journal_not_listening_is_reported_but_the_command_stands(self):
        self.journald.close()          # the socket file stays, nobody reads it
        code, out, err = self.run_agentbox("close-web", "work")
        self.assertEqual((code, out), (0, "work: Open web closed\n"))
        self.assertTrue(err.startswith("agentbox: Activity not recorded in the journal"), err)

    @unittest.skipIf(os.geteuid() == 0, "root may write anyway")
    def test_without_the_right_to_write_a_warning_and_nowhere_else(self):
        # only root and the group agentbox-journal may write into the namespace
        os.chmod(self.journald.path, 0)
        default = self.journal_at("run/systemd/journal/socket")
        code, out, err = self.run_agentbox("close-web", "work")
        self.assertEqual((code, out), (0, "work: Open web closed\n"))
        self.assertEqual(err, "agentbox: Activity not recorded: only root and the group agentbox-journal write into "
                              "the journal (the members of libvirt, after agentbox setup and logging in again)\n")
        self.assertEqual((self.entries(), self.entries(default)), ([], []))


class SecretTest(ActivityTest):
    """Tokens, keys and Pairing codes never reach the journal."""

    def setUp(self):
        super().setUp()
        with open(self.path("etc/agentbox/secrets/claude-priv"), "w") as f:
            f.write("none\n")

    def answer(self, text):
        with open(os.path.join(self.dir, "curl-answer"), "w") as f:
            f.write(text)

    def test_claude_token(self):
        self.answer('{"id":"msg_01","type":"message"}')
        code, _, err = self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=TOKEN + "\n")
        self.assertEqual(code, 0, err)
        [e] = self.entries()
        self.assertEqual((e["AGENTBOX_KIND"], e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"]),
                         ("credentials", "set-claude-token", "ok", "Set the Claude token"))
        self.assertNotIn(b"sk-ant", self.raw())

    def test_refused_claude_token_echoed_back(self):
        self.answer('{"type":"error","error":{"type":"authentication_error","message":"bad key '
                    + TOKEN[:20] + '"}}')
        code, _, _ = self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=TOKEN + "\n")
        self.assertEqual(code, 1)
        [e] = self.entries()
        self.assertEqual(e["AGENTBOX_OUTCOME"], "failed")
        self.assertIn("Anthropic rejects the token", e["AGENTBOX_DESCRIPTION"])
        self.assertNotIn(b"sk-ant", self.raw())
        self.assertNotIn(TOKEN[7:20].encode(), self.raw())

    def test_token_passed_as_argument_by_mistake(self):
        self.answer('{"id":"msg_01","type":"message"}')
        self.run_agentbox("set-claude-token", "priv", TOKEN, "--no-prompt", stdin=TOKEN + "\n")
        self.assertEqual(len(self.entries()), 1)
        self.assertNotIn(b"sk-ant", self.raw())

    def test_pairing_code(self):
        self.scenario(responses=[{"entries": [vm_journal(f"Pairing: {CODE}")]}])
        self.assertEqual(self.run_agentbox("--actor", FIREFOX, "pair", "priv"), (0, CODE + "\n", ""))
        [e] = self.entries()
        self.assertEqual((e["AGENTBOX_KIND"], e["AGENTBOX_ACTION"], e["AGENTBOX_VM"], e["AGENTBOX_ACTOR"],
                          e["AGENTBOX_DESCRIPTION"]),
                         ("pairing", "pair", "priv", FIREFOX, "Show the Pairing code"))
        self.assertNotIn(b"pair?code", self.raw())
        self.assertNotIn(b"Ab3_x-9", self.raw())

    def test_actor_carrying_secrets_is_hidden(self):
        self.run_agentbox(f"--actor=x {CODE} {TOKEN} ghs_abcdefghijklmnop", "close-web", "work")
        self.assertEqual(self.entries()[-1]["AGENTBOX_ACTOR"],
                         "x orca://pair?[hidden] [hidden] [hidden]")
        for secret in (b"Ab3_x-9", b"sk-ant", b"ghs_"):
            self.assertNotIn(secret, self.raw())


class WeeklyCheckTest(ActivityTest):
    """The timer's automatic Resets, one entry per VM, with the timer as actor."""

    def setUp(self):
        super().setUp()
        base = self.path("var/lib/agentbox/images/base-20261101.qcow2")
        open(base, "w").close()
        os.symlink("base-20261101.qcow2", self.path("var/lib/agentbox/images/base.qcow2"))

    def disk(self, vm, days, build="20261101"):
        """A System disk of the VM, `days` old, backed by that Build (by default the current one,
        so only its age makes a Reset due)."""
        disk = self.path(f"var/lib/agentbox/vms/{vm}-system.qcow2")
        with open(disk, "w") as f:
            json.dump({"backing-filename": f"/var/lib/agentbox/images/base-{build}.qcow2"}, f)
        t = time.time() - days * DAY
        os.utime(disk, (t, t))

    def test_reset_and_postponed_with_reason(self):
        self.scenario(vms={"work": {"state": "shut off"}}, connections={"10.77.2.2:6802": 1})
        self.disk("work", 8)
        self.disk("priv", 8)
        self.assertEqual(self.run_agentbox("--actor=timer", "weekly-check")[0], 0)
        self.assertEqual([{k: v for k, v in e.items() if k.startswith("AGENTBOX_")} for e in self.entries()], [
            {"AGENTBOX_KIND": "reset", "AGENTBOX_ACTION": "reset.automatic", "AGENTBOX_CODE": "reset.automatic",
             "AGENTBOX_VM": "work", "AGENTBOX_ACTOR": "timer", "AGENTBOX_OUTCOME": "ok",
             "AGENTBOX_DESCRIPTION": "Automatic Reset (the VM was off)", "AGENTBOX_PARAM_REASON": "stopped"},
            {"AGENTBOX_KIND": "reset", "AGENTBOX_ACTION": "reset.postponed", "AGENTBOX_CODE": "reset.postponed",
             "AGENTBOX_VM": "priv", "AGENTBOX_ACTOR": "timer", "AGENTBOX_OUTCOME": "ok",
             "AGENTBOX_DESCRIPTION": "Automatic Reset postponed (a device is connected)",
             "AGENTBOX_PARAM_REASON": "gui-connected"}])

    def test_resets_onto_a_newer_build_have_the_reason_update(self):
        """auto_update (#114): a VM whose Image has a newer Build is reset by the same rules; a
        postponed one keeps why it was postponed, both name the Build."""
        self.scenario(vms={"work": {"state": "shut off"}}, connections={"10.77.2.2:6802": 1})
        self.disk("work", 1, build="20261020")
        self.disk("priv", 1, build="20261020")
        self.assertEqual(self.run_agentbox("--actor=timer", "weekly-check")[0], 0)
        self.assertEqual([{k: v for k, v in e.items() if k.startswith("AGENTBOX_")} for e in self.entries()], [
            {"AGENTBOX_KIND": "reset", "AGENTBOX_ACTION": "reset.automatic", "AGENTBOX_CODE": "reset.automatic",
             "AGENTBOX_VM": "work", "AGENTBOX_ACTOR": "timer", "AGENTBOX_OUTCOME": "ok",
             "AGENTBOX_DESCRIPTION": "Automatic Reset onto Build base-20261101 (the VM was off)",
             "AGENTBOX_PARAM_REASON": "update", "AGENTBOX_PARAM_IMAGE": "base", "AGENTBOX_PARAM_BUILD": "20261101"},
            {"AGENTBOX_KIND": "reset", "AGENTBOX_ACTION": "reset.postponed", "AGENTBOX_CODE": "reset.postponed",
             "AGENTBOX_VM": "priv", "AGENTBOX_ACTOR": "timer", "AGENTBOX_OUTCOME": "ok",
             "AGENTBOX_DESCRIPTION": "Automatic Reset onto Build base-20261101 postponed (a device is connected)",
             "AGENTBOX_PARAM_REASON": "gui-connected", "AGENTBOX_PARAM_IMAGE": "base",
             "AGENTBOX_PARAM_BUILD": "20261101"}])

    def test_vms_left_alone_are_not_recorded(self):
        self.disk("work", 1)
        self.assertEqual(self.run_agentbox("--actor=timer", "weekly-check"), (0, "", ""))
        self.assertEqual(self.entries(), [])

    def test_failure(self):
        self.scenario(vms={"work": {"state": "shut off"}})
        self.disk("work", 8)
        os.remove(self.path("var/lib/agentbox/images/base.qcow2"))
        self.assertEqual(self.run_agentbox("--actor=timer", "weekly-check")[0], 1)
        [e] = self.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"], e["AGENTBOX_ERROR"],
                          e["AGENTBOX_ERROR_PARAM_IMAGE"], e["AGENTBOX_PARAM_REASON"]),
                         ("reset.failed", "failed",
                          "Automatic Reset failed: no Build of Image base, run agentbox build base first",
                          "image_has_no_build", "base", "error"))

    def test_the_installed_timer_passes_its_actor(self):
        self.assertEqual(self.run_agentbox("regenerate")[0], 0)
        with open(self.path("etc/systemd/system/agentbox-weekly.service")) as f:
            self.assertIn("ExecStart=/usr/bin/agentbox --actor=timer weekly-check\n", f.read())


def journal_entry(ident, message, age, **fields):
    """A record of the Host's journal (journalctl -o json), `age` seconds old, by the
    writer agentbox knows for the identifier unless `fields` say otherwise."""
    return {"__REALTIME_TIMESTAMP": str(int((time.time() - age) * 10**6)), "__CURSOR": f"c-{ident}-{age}",
            "_BOOT_ID": "b1", "PRIORITY": "5", "SYSLOG_IDENTIFIER": ident, "MESSAGE": message,
            **writer(ident, fields.get("AGENTBOX_KIND")), **fields}


def recorded(age, kind, action, description, vm=None, actor=FIREFOX, outcome="ok", **fields):
    fields = {"AGENTBOX_KIND": kind, "AGENTBOX_ACTION": action, "AGENTBOX_ACTOR": actor, "AGENTBOX_OUTCOME": outcome,
              "AGENTBOX_DESCRIPTION": description, **({"AGENTBOX_VM": vm} if vm else {}), **fields}
    return journal_entry("agentbox-activity", f"{vm or '-'} {action} {outcome}: {description}", age, **fields)


HOUR = 3600
# The recorded entries here were written before #69: German text, no code, no parameters.
JOURNAL = [
    recorded(10 * HOUR, "signin", "signin", "angemeldet"),
    recorded(9 * HOUR, "allowlist", "allow", "Freigabe example.org", vm="work"),
    journal_entry("agentbox-proxy-work", "[work] ALERT evil.example [exfil,young]", 8 * HOUR),
    journal_entry("agentbox-proxy-work", "[work] BLOCKED evil.example (not on the Allowlist)", 8 * HOUR - 1),
    journal_entry("agentbox-proxy-work", "[work] ALLOWED example.org (allowlist example.org work)", 7 * HOUR),
    journal_entry("agentbox-gh-priv", "[priv] token for likt0r/askos until 2026-11-03T12:00:00Z", 6 * HOUR),
    journal_entry("agentbox-gh-priv", "[priv] refused: 'evil/repo': owner-not-allowed: evil is not a GitHub owner "
                                      "of the VM priv", 5 * HOUR),
    recorded(4 * HOUR, "action", "reset", "Reset: qemu-img create scheitert", vm="priv", outcome="failed"),
    recorded(3 * HOUR, "reset", "reset.automatic", "automatischer Reset (untaetig)", vm="work", actor="timer"),
    journal_entry("sshd", "Accepted publickey for bewr", 2 * HOUR),
    # the older German keywords are ignored entirely
    journal_entry("agentbox-proxy-work", "[work] VERDACHT old.example [exfil]", 2 * HOUR - 1),
    journal_entry("agentbox-gh-priv", "[priv] Token fuer old/repo bis 2026-11-03T12:00:00Z", 2 * HOUR - 2),
    journal_entry("agentbox-gh-priv", "[priv] abgelehnt: 'old/repo': nein", 2 * HOUR - 3),
    # a VM deleted since: its Alerts stay part of the timeline
    journal_entry("agentbox-proxy-lab", "[lab] ALERT paste.example [paste]", HOUR),
]
TIMELINE = [
    {"vm": "lab", "actor": "VM lab", "kind": "alert", "action": "alert", "outcome": "ok",
     "description": "Alert: paste.example (paste)", "code": "alert",
     "params": {"domain": "paste.example", "signals": "paste"}},
    {"vm": "work", "actor": "timer", "kind": "reset", "action": "reset.automatic", "outcome": "ok",
     "description": "automatischer Reset (untaetig)"},
    {"vm": "priv", "actor": FIREFOX, "kind": "action", "action": "reset", "outcome": "failed",
     "description": "Reset: qemu-img create scheitert"},
    {"vm": "priv", "actor": "VM priv", "kind": "github", "action": "github.refused", "outcome": "failed",
     "description": "GitHub token for evil/repo refused: evil is not a GitHub owner of the VM priv",
     "code": "github.refused", "params": {"request": "evil/repo", "repository": "evil/repo",
                                          "reason": "owner-not-allowed"}, "reason": "owner-not-allowed"},
    {"vm": "priv", "actor": "VM priv", "kind": "github", "action": "github.token", "outcome": "ok",
     "description": "GitHub token for likt0r/askos until 2026-11-03T12:00:00Z", "code": "github.token",
     "params": {"repository": "likt0r/askos", "expires": "2026-11-03T12:00:00Z"}},
    {"vm": "work", "actor": "VM work", "kind": "alert", "action": "alert", "outcome": "ok",
     "description": "Alert: evil.example (exfil,young)", "code": "alert",
     "params": {"domain": "evil.example", "signals": "exfil, young"}},
    {"vm": "work", "actor": FIREFOX, "kind": "allowlist", "action": "allow", "outcome": "ok",
     "description": "Freigabe example.org"},
    {"vm": None, "actor": FIREFOX, "kind": "signin", "action": "signin", "outcome": "ok", "description": "angemeldet"},
]


class JournalTest(ActivityTest):
    """The Host's journal as the fake journalctl shows it."""

    def journal(self, entries):
        with open(os.path.join(self.dir, "journal.json"), "w") as f:
            json.dump(entries, f)

    def timeline(self, *args):
        code, out, err = self.run_agentbox("activity", *args, "--json")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        for e in result["entries"]:
            self.assertRegex(e.pop("time"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
        return result

    def descriptions(self, *args):
        return [e["description"] for e in self.timeline(*args)["entries"]]

    def journalctl(self):
        return [c["argv"] for c in self.calls("journalctl")]


class TimelineTest(JournalTest):
    """`agentbox activity`: one timeline from the recorded entries, Alerts and GitHub tokens."""

    def test_merges_every_source_newest_first(self):
        self.journal(JOURNAL)
        self.assertEqual(self.timeline(), {"entries": TIMELINE, "truncated": False, "retention_days": 90})

    def test_github_token_entries_have_their_action_as_code_and_parameters(self):
        # github-app/agentbox-gh-tokend writes these lines; a refusal names its reason's code
        self.journal([
            journal_entry("agentbox-gh-priv", "[priv] error: 'likt0r/x': TimeoutError('timed out')", HOUR),
            journal_entry("agentbox-gh-priv", "[priv] refused: 'a/b': something-new: a reason from later", 2 * HOUR),
            journal_entry("agentbox-gh-priv", "[priv] refused: 'not a repo': without a code", 3 * HOUR),
            journal_entry("agentbox-gh-priv", "[priv] refused: 'a/b': github-status status=404: GitHub answered 404",
                          4 * HOUR),
        ])
        entries = self.timeline("--kind=github")["entries"]
        self.assertEqual([(e["code"], e["outcome"], e["description"], e["params"], e.get("reason")) for e in entries], [
            ("github.error", "failed", "GitHub token for likt0r/x failed: TimeoutError('timed out')",
             {"request": "likt0r/x", "repository": "likt0r/x", "message": "TimeoutError('timed out')"}, None),
            ("github.refused", "failed", "GitHub token for a/b refused: a reason from later",
             {"request": "a/b", "repository": "a/b", "reason": "something-new"}, "something-new"),
            ("github.refused", "failed", "GitHub token for not a repo refused: without a code",
             {"request": "not a repo"}, None),
            ("github.refused", "failed", "GitHub token for a/b refused: GitHub answered 404",
             {"request": "a/b", "repository": "a/b", "reason": "github-status", "status": "404"}, "github-status")])

    def test_text(self):
        self.journal(JOURNAL[:3])
        code, out, err = self.run_agentbox("activity")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  work +alert +Alert: evil.example "
                                   r"\(exfil,young\)  by VM work$")
        self.assertTrue(lines[2].endswith(f"-                signin     angemeldet  by {FIREFOX}"), lines[2])

    def test_failed_entries_are_marked_in_text(self):
        self.journal([JOURNAL[7]])
        _, out, _ = self.run_agentbox("activity")
        self.assertTrue(out.endswith("Reset: qemu-img create scheitert  by Firefox/Linux (192.0.2.23)"
                                     "  [failed]\n"), out)

    def test_reads_the_journal_of_activity(self):
        self.journal(JOURNAL)
        self.timeline()
        recorded_call, proxy_call, token_call = self.journalctl()
        # recorded entries only from the namespace, by whoever its sockets let write
        self.assertIn("--namespace=agentbox", recorded_call)
        self.assertEqual([a for a in recorded_call if a.startswith(("--identifier=", "_UID="))],
                         ["--identifier=agentbox-activity"])
        # the services' logs also from the default journal (anyone can write it), by their
        # users, of every VM that ever logged (lab is deleted)
        for call, uid in ((proxy_call, PROXY), (token_call, GH)):
            self.assertIn("--namespace=+agentbox", call)
            self.assertEqual([a for a in call if a.startswith(("--identifier=", "_UID="))], [f"_UID={uid}"])
        self.assertIn("lab", {e["vm"] for e in self.timeline()["entries"]})

    def test_filter_by_kind(self):
        self.journal(JOURNAL)
        self.assertEqual([e["kind"] for e in self.timeline("--kind", "alert")["entries"]], ["alert", "alert"])
        self.assertEqual([e["action"] for e in self.timeline("--kind=github,signin")["entries"]],
                         ["github.refused", "github.token", "signin"])
        self.assertEqual(self.descriptions("--kind=reset"), ["automatischer Reset (untaetig)"])

    def test_reason_of_a_postponed_reset(self):
        postponed = recorded(HOUR, "reset", "reset.postponed", "automatischer Reset verschoben (Agenten arbeiten)",
                             vm="work", actor="timer")
        postponed["AGENTBOX_REASON"] = "agents-working"
        odd = recorded(HOUR - 1, "reset", "reset.postponed", "x", vm="work", actor="timer")
        odd["AGENTBOX_REASON"] = "Not A Code\x1b[31m"
        self.journal([postponed, odd])
        entries = self.timeline("--kind=reset", "--vm=work")["entries"]
        # only a code the Control Room can translate; entries without one have no field
        self.assertEqual([e.get("reason") for e in entries], [None, "agents-working"])
        self.assertNotIn("reason", entries[0])

    def test_code_parameters_and_error_code(self):
        """#69: an entry recorded with a code shows it with its parameters, a failure also its
        error code and that error's parameters; an older one only its description."""
        ok = recorded(3 * HOUR, "allowlist", "allow", "Allow pypi.org for all VMs for 1 hour", AGENTBOX_CODE="allow",
                      AGENTBOX_PARAM_DOMAIN="pypi.org", AGENTBOX_PARAM_SCOPE="all", AGENTBOX_PARAM_DURATION="1h",
                      AGENTBOX_EXIT="0", AGENTBOX_ERROR="ignored_when_ok")
        failed = recorded(2 * HOUR, "allowlist", "allow", "Allow evil.example: evil.example is suspicious (exfil)",
                          vm="work", outcome="failed", AGENTBOX_CODE="allow", AGENTBOX_PARAM_DOMAIN="evil.example",
                          AGENTBOX_PARAM_SCOPE="work", AGENTBOX_ERROR="domain_suspicious",
                          AGENTBOX_ERROR_PARAM_DOMAIN="evil.example", AGENTBOX_EXIT="1",
                          # odd names and values: left out, or cleaned like any text
                          AGENTBOX_PARAM_9X="no", AGENTBOX_PARAM_NOTE=f"a\x1b[31mb {TOKEN}")
        postponed = recorded(HOUR, "reset", "reset.postponed", "Automatic Reset postponed (agents are working)",
                             vm="work", actor="timer", AGENTBOX_CODE="reset.postponed",
                             AGENTBOX_PARAM_REASON="agents-working")
        # a code that is not the action is none
        other = recorded(HOUR - 1, "action", "pause", "Pause", vm="work", AGENTBOX_CODE="start")
        self.journal([ok, failed, postponed, other, JOURNAL[1]])
        entries = self.timeline()["entries"]
        self.assertEqual(entries, [
            {"vm": "work", "actor": FIREFOX, "kind": "action", "action": "pause", "outcome": "ok",
             "description": "Pause"},
            {"vm": "work", "actor": "timer", "kind": "reset", "action": "reset.postponed", "outcome": "ok",
             "description": "Automatic Reset postponed (agents are working)", "code": "reset.postponed",
             "params": {"reason": "agents-working"}, "reason": "agents-working"},
            {"vm": "work", "actor": FIREFOX, "kind": "allowlist", "action": "allow", "outcome": "failed",
             "description": "Allow evil.example: evil.example is suspicious (exfil)", "code": "allow",
             "params": {"domain": "evil.example", "note": "ab [hidden]", "scope": "work"},
             "error": "domain_suspicious", "error_params": {"domain": "evil.example"}},
            {"vm": None, "actor": FIREFOX, "kind": "allowlist", "action": "allow", "outcome": "ok",
             "description": "Allow pypi.org for all VMs for 1 hour", "code": "allow",
             "params": {"domain": "pypi.org", "duration": "1h", "scope": "all"}},
            # before #69
            {"vm": "work", "actor": FIREFOX, "kind": "allowlist", "action": "allow", "outcome": "ok",
             "description": "Freigabe example.org"},
        ])

    def test_what_the_cli_records_reads_back(self):
        """What record() writes is what the timeline shows: the CLI's entries through the
        stand-in journald, read back as journalctl hands them over."""
        self.assertEqual(self.run_agentbox("--actor", FIREFOX, "extend", "all", "a.example", "--for", "8h")[0], 1)
        written = [{**e, "__REALTIME_TIMESTAMP": str(int(time.time() * 10**6)), "__CURSOR": "c1", "_BOOT_ID": "b1",
                    **writer("agentbox-activity", e["AGENTBOX_KIND"])} for e in self.entries()]
        self.journal(written)
        [entry] = self.timeline()["entries"]
        self.assertEqual({k: entry[k] for k in ("code", "params", "error", "error_params")}, {
            "code": "extend", "params": {"domain": "a.example", "scope": "all", "duration": "8h"},
            "error": "allowlist_entry_not_found", "error_params": {"domain": "a.example", "scope": "all"}})
        self.assertTrue(entry["description"].startswith("Extend the Allowlist entry a.example for all VMs by 8 hours: "))

    def test_filter_by_kind_asks_only_the_sources_that_have_it(self):
        self.journal(JOURNAL)
        self.timeline("--kind=allowlist")
        self.assertEqual(len(self.journalctl()), 1)
        self.assertIn("AGENTBOX_KIND=allowlist", self.journalctl()[0])

    def test_filter_by_vm(self):
        self.journal(JOURNAL)
        self.assertEqual({e["vm"] for e in self.timeline("--vm", "priv")["entries"]}, {"priv"})
        self.assertEqual(len(self.timeline("--vm=priv")["entries"]), 3)
        self.assertEqual(self.descriptions("--vm=lab"), ["Alert: paste.example (paste)"])
        self.assertEqual(self.timeline("--vm=nope")["entries"], [])

    def test_filter_by_time(self):
        self.journal(JOURNAL)
        self.assertEqual([e["action"] for e in self.timeline("--since=-330min", "--until=-150min")["entries"]],
                         ["reset.automatic", "reset", "github.refused"])
        self.assertEqual(len(self.timeline("--since=today")["entries"])
                         + len(self.timeline("--until=today")["entries"]), len(TIMELINE))

    def test_search_in_any_text_ignoring_case(self):
        self.journal(JOURNAL)
        self.assertEqual(self.descriptions("--search=EVIL"), [
            "GitHub token for evil/repo refused: evil is not a GitHub owner of the VM priv",
            "Alert: evil.example (exfil,young)"])
        self.assertEqual(len(self.timeline("--search", "firefox")["entries"]), 3)
        self.assertEqual(self.descriptions("--search=VM lab"), ["Alert: paste.example (paste)"])
        self.assertEqual(self.timeline("--search=nothing like this")["entries"], [])

    def test_filters_combine(self):
        self.journal(JOURNAL)
        self.assertEqual(self.descriptions("--kind=alert,allowlist", "--vm=work", "--search=example"),
                         ["Alert: evil.example (exfil,young)", "Freigabe example.org"])

    def test_number_of_entries(self):
        self.journal(JOURNAL)
        result = self.timeline("--lines=3")
        self.assertEqual([e["description"] for e in result["entries"]], [e["description"] for e in TIMELINE[:3]])
        self.assertTrue(result["truncated"])
        self.assertFalse(self.timeline("--lines=8")["truncated"])
        code, _, err = self.run_agentbox("activity", "--lines=2")
        self.assertEqual(code, 0)
        self.assertIn("there are more entries", err)

    def test_invalid_filters(self):
        for args in (["--kind=nope"], ["--kind=alert,"], ["--vm=WORK"], ["--vm=../x"], ["--since=gestern"],
                     ["--until=2026-13-45"], ["--lines=0"], ["--lines=5001"], ["--search="], ["--search=\x1b[31m"],
                     ["--grep=x"], ["work"], ["--kind"]):
            with self.subTest(args=args):
                code, out, err = self.run_agentbox("activity", *args)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("agentbox: "), err)
        self.assertEqual(self.journalctl(), [])

    def test_untrusted_text_is_cleaned(self):
        self.journal([
            recorded(HOUR, "action", "x", f"a\x1b[31mred\x07 {CODE} {TOKEN}\u202e", actor="b\nc"),
            journal_entry("agentbox-gh-priv", "[priv] refused: 'ghs_abcdefghijklmnop': invalid-request: no", HOUR),
            # GitHub's current installation tokens: long, with "-", "." and "_"
            journal_entry("agentbox-gh-priv", "[priv] refused: 'ghs_" + "Q3-x.Y_z9" * 40 + "': invalid-request: no",
                          HOUR),
            journal_entry("agentbox-gh-priv", "[priv] error: 'a/b': OSError('sk-ant-oat01-abcdefghijkl')", HOUR),
        ])
        out = json.dumps(self.timeline())
        for bad in ("\\u001b", "\\u0007", "\\u202e", "Ab3_x-9", "sk-ant", "ghs_", "\\n", "Q3-x", "Y_z9"):
            self.assertNotIn(bad, out)
        self.assertIn("ared orca://pair?[hidden] [hidden]", out)

    def test_nothing_logged_yet(self):
        self.assertEqual(self.timeline(), {"entries": [], "truncated": False, "retention_days": 90})
        # recorded entries, and the services' logs by their users
        self.assertEqual(len(self.journalctl()), 3)

    def test_failing_journalctl(self):
        self.scenario(journalctl_exit=4)
        code, out, err = self.run_agentbox("activity", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err), {"error": "journal_failed", "params": {},
                                           "message": "journalctl failed: Failed to open journal: Permission denied"})
        self.assertEqual(self.run_agentbox("activity")[2],
                         "agentbox: journalctl failed: Failed to open journal: Permission denied\n")


# Entries their writer may not write: each writer only its own kinds, journald stamps
# who wrote it (_UID, _SYSTEMD_UNIT). Each says "forged" somewhere.
# Not the services' users: for the services' logs (also read from the default journal,
# which anyone can write) journalctl never even returns these.
OTHER_USERS = [
    # a local user's process
    written_by(journal_entry("agentbox-proxy-work", "[work] ALERT forged.example [exfil]", HOUR), FORGED),
    written_by(journal_entry("agentbox-gh-priv", "[priv] token for forged/repo until 2026-11-03T12:00:00Z", HOUR),
               FORGED),
    # root never logs as the proxy; the right unit but not its user
    written_by(journal_entry("agentbox-proxy-work", "[work] ALERT forged-by-root.example [exfil]", HOUR), CLI),
    written_by(journal_entry("agentbox-proxy-work", "[work] ALERT forged-uid.example [exfil]", HOUR),
               {**proxy("work"), "_UID": "1000"}),
    # the proxies' user as a token service
    written_by(journal_entry("agentbox-gh-priv", "[priv] token for forged/proxy until 2026-11-03T12:00:00Z", HOUR),
               proxy("priv")),
]
# Read, left out and counted.
WRONG_WRITERS = [
    # sign-ins come from the Control Room only; no trusted fields at all
    written_by(recorded(HOUR, "signin", "signin", "forged: angemeldet"), FORGED),
    written_by(recorded(HOUR, "action", "reset", "forged: ohne Absender", vm="work"), {}),
    # the services never record commands, the CLI never Alerts or GitHub tokens
    written_by(recorded(HOUR, "action", "delete", "forged: vom Proxy", vm="work"), proxy("work")),
    written_by(recorded(HOUR, "allowlist", "allow", "forged: vom Token-Dienst", vm="priv"), token_service("priv")),
    written_by(recorded(HOUR, "alert", "alert", "forged: Verdacht", vm="work"), CLI),
    # the proxy or token service of another VM, also one whose name starts the same
    written_by(journal_entry("agentbox-proxy-work", "[work] ALERT forged-by-lab.example [exfil]", HOUR),
               proxy("lab")),
    written_by(journal_entry("agentbox-proxy-work", "[work] ALERT forged-by-work-2.example [exfil]", HOUR),
               proxy("work-2")),
    written_by(journal_entry("agentbox-gh-priv", "[priv] refused: 'forged/x': invalid-request: no", HOUR), token_service("work")),
    written_by(journal_entry("agentbox-gh-priv", "[priv] refused: 'forged/y': invalid-request: no", HOUR),
               token_service("priv-2")),
    written_by(journal_entry("agentbox-gh-priv", "[priv] refused: 'forged/z': invalid-request: no", HOUR),
               {**token_service("priv"), "_SYSTEMD_UNIT": "agentbox-ghvm-priv@7.scope"}),
    # a service's user in the other service's unit
    written_by(journal_entry("agentbox-proxy-work", "[work] ALERT forged-gh-unit.example [exfil]", HOUR),
               {**proxy("work"), "_SYSTEMD_UNIT": token_service("work")["_SYSTEMD_UNIT"]}),
    written_by(journal_entry("agentbox-gh-priv", "[priv] refused: 'forged/w': invalid-request: no", HOUR),
               {**token_service("priv"), "_SYSTEMD_UNIT": "agentbox-proxy-priv.service"}),
    # an identifier given twice
    {**journal_entry("agentbox-proxy-work", "[work] ALERT forged-twice.example [exfil]", HOUR),
     "SYSLOG_IDENTIFIER": ["agentbox-proxy-work", "agentbox-proxy-work"]},
    # the Control Room records its sign-ins and sign-outs only, without a VM, from its unit
    written_by(recorded(HOUR, "signin", "signin", "forged: andere Unit"),
               {**CONTROL_ROOM, "_SYSTEMD_UNIT": "user@959.service"}),
    written_by(recorded(HOUR, "allowlist", "allow", "forged: Freigabe", vm="work"), CONTROL_ROOM),
    written_by(recorded(HOUR, "signin", "delete", "forged: geloescht"), CONTROL_ROOM),
    written_by(recorded(HOUR, "signin", "signin", "forged: VM", vm="work"), CONTROL_ROOM),
    written_by(recorded(HOUR, "signin", "signin", "forged: doppelt", AGENTBOX_KIND=["signin", "action"]),
               CONTROL_ROOM),
]
FORGED_ENTRIES = OTHER_USERS + WRONG_WRITERS
UNTRUSTED = "agentbox: journal entries without a known writer left out: {}\n"


class ForgedTest(JournalTest):
    """Only entries their writer may write count, in Activity, the Alerts and the GitHub
    view; those read but left out are counted, never shown."""

    def test_activity_shows_only_known_writers(self):
        self.journal(JOURNAL + FORGED_ENTRIES)
        code, out, err = self.run_agentbox("activity", "--json")
        self.assertEqual((code, err), (0, UNTRUSTED.format(len(WRONG_WRITERS))))
        entries = json.loads(out)["entries"]
        for e in entries:
            del e["time"]
        self.assertEqual(entries, TIMELINE)

    def test_alerts_show_only_the_vms_proxy(self):
        self.journal(JOURNAL + FORGED_ENTRIES)
        code, out, err = self.run_agentbox("alerts", "work", "--json")
        self.assertEqual((code, err), (0, UNTRUSTED.format(3)))
        self.assertEqual([a["domain"] for a in json.loads(out)], ["evil.example"])

    def test_github_view_shows_only_the_vms_token_service(self):
        self.journal(JOURNAL + FORGED_ENTRIES)
        code, out, err = self.run_agentbox("github-tokens", "priv", "--json")
        self.assertEqual((code, err), (0, UNTRUSTED.format(4)))
        self.assertEqual([r["request"] for r in json.loads(out)["requests"]], ["evil/repo", "likt0r/askos"])

    def test_root_counts_without_unit_or_command(self):
        # journald reads them from /proc and misses them when the CLI has ended
        self.journal([written_by(recorded(HOUR, "action", "regenerate", "Konfiguration erzeugen"),
                                 {"_UID": "0", "_GID": "0", "_TRANSPORT": "journal"})])
        self.assertEqual(self.descriptions(), ["Konfiguration erzeugen"])

    def test_the_cli_without_root_counts(self):
        # `agentbox pair` needs only libvirt access; the socket let the member write
        self.journal([written_by(recorded(HOUR, "pairing", "pair", "Pairing-Code anzeigen", vm="priv",
                                          actor="bewr"), MEMBER)])
        self.assertEqual(self.descriptions(), ["Pairing-Code anzeigen"])

    def test_forged_entries_cannot_push_real_ones_out(self):
        newer = [written_by(recorded(60 - i, "signin", "signin", f"forged {i}"), FORGED) for i in range(5)] \
            + [written_by(recorded(60 - i, "action", "reset", f"forged {i}", vm="work"), CONTROL_ROOM)
               for i in range(5)]
        self.journal(JOURNAL + newer)
        code, out, err = self.run_agentbox("activity", "--lines=2", "--json")
        self.assertEqual((code, err), (0, UNTRUSTED.format(10)))
        self.assertEqual([e["description"] for e in json.loads(out)["entries"]],
                         [e["description"] for e in TIMELINE[:2]])

    def test_the_warning_tells_nothing_of_their_content(self):
        self.journal(FORGED_ENTRIES)
        code, out, err = self.run_agentbox("activity")
        self.assertEqual((code, out, err), (0, "", UNTRUSTED.format(len(WRONG_WRITERS))))

    def test_without_the_service_users_their_entries_do_not_count(self):
        os.remove(self.path("etc/passwd"))
        self.journal(JOURNAL + [written_by(recorded(HOUR, "action", "delete", "forged: vom Proxy", vm="work"),
                                           proxy("work"))])
        # what the CLI recorded stays; the sign-in has no Control Room to come from, a
        # service's unit still records no command, and nobody's entries are read for the services
        code, out, err = self.run_agentbox("activity", "--json")
        self.assertEqual((code, err), (0, UNTRUSTED.format(2)))
        self.assertEqual([e["action"] for e in json.loads(out)["entries"]], ["reset.automatic", "reset", "allow"])
        self.assertEqual([[a for a in c if a.startswith("_UID=")] for c in self.journalctl()], [[]])


class RetentionTest(JournalTest):
    """Activity is kept for 90 days by default, or as long as settings.json says."""

    def settings(self, text):
        with open(self.path("etc/agentbox/settings.json"), "w") as f:
            f.write(text)

    def test_older_entries_are_not_shown(self):
        self.journal([recorded(91 * DAY, "signin", "signin", "zu alt"),
                      recorded(89 * DAY, "signin", "signin", "noch da"),
                      journal_entry("agentbox-proxy-work", "[work] ALERT old.example [exfil]", 91 * DAY)])
        self.assertEqual(self.descriptions(), ["noch da"])

    def test_an_earlier_since_is_cut_at_the_retention(self):
        self.journal([recorded(91 * DAY, "signin", "signin", "zu alt"), recorded(DAY, "signin", "signin", "neu")])
        before = time.time()
        self.assertEqual(self.descriptions("--since=-200d"), ["neu"])
        since = int([a for a in self.journalctl()[0] if a.startswith("--since=@")][0][len("--since=@"):])
        self.assertAlmostEqual(since, before - 90 * DAY, delta=60)

    def test_retention_from_the_settings(self):
        self.settings('{"activity_retention_days": 7}\n')
        self.journal([recorded(8 * DAY, "signin", "signin", "zu alt"), recorded(6 * DAY, "signin", "signin", "da")])
        result = self.timeline()
        self.assertEqual(([e["description"] for e in result["entries"]], result["retention_days"]), (["da"], 7))

    def test_invalid_settings(self):
        for text in ('{"activity_retention_days": 0}', '{"activity_retention_days": "90"}',
                     '{"activity_retention_days": true}', '{"activity_retention_days": 3651}', "[]", "kaputt"):
            with self.subTest(settings=text):
                self.settings(text)
                code, out, err = self.run_agentbox("activity")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("settings.json", err)

    def regenerate(self):
        code, out, err = self.run_agentbox("regenerate")
        self.assertEqual((code, err), (0, ""))
        return out

    def journald_conf(self):
        with open(self.path("etc/systemd/journald@agentbox.conf")) as f:
            return f.read()

    def systemctl(self):
        return [c["argv"] for c in self.calls("systemctl") if "journald" in " ".join(c["argv"])]

    def test_regenerate_sets_up_the_namespace_with_its_retention(self):
        out = self.regenerate()
        self.assertIn("Activity is kept for 90 days\n", out)
        conf = self.journald_conf()
        for line in ("[Journal]", "Storage=persistent", "MaxRetentionSec=90day", "MaxFileSec=1day"):
            self.assertIn(line + "\n", conf)
        self.assertEqual(self.systemctl(), [["try-restart", "systemd-journald@agentbox.service"],
                                            ["enable", "--now", "systemd-journald@agentbox.socket"]])
        os.remove(os.path.join(self.dir, "calls.jsonl"))
        self.assertNotIn("Activity is kept", self.regenerate())
        self.assertEqual(self.systemctl(), [["enable", "--now", "systemd-journald@agentbox.socket"]])

    def test_changed_retention_restarts_the_namespaces_journald(self):
        self.regenerate()
        os.remove(os.path.join(self.dir, "calls.jsonl"))
        self.settings('{"activity_retention_days": 30}')
        self.assertIn("Activity is kept for 30 days\n", self.regenerate())
        self.assertIn("MaxRetentionSec=30day\n", self.journald_conf())
        self.assertIn(["try-restart", "systemd-journald@agentbox.service"], self.systemctl())

    def test_invalid_settings_stop_regenerate_before_any_change(self):
        self.settings('{"activity_retention_days": -1}')
        code, _, err = self.run_agentbox("regenerate")
        self.assertEqual(code, 1)
        self.assertIn("activity_retention_days", err)
        self.assertEqual([c for c in self.calls() if c["cmd"] != "journalctl"], [])
        self.assertFalse(os.path.exists(self.path("etc/systemd/journald@agentbox.conf")))

    def test_proxy_and_token_service_log_into_the_namespace(self):
        self.regenerate()
        for unit in ("agentbox-proxy-priv.service", "agentbox-ghvm-priv@.service"):
            with open(self.path(f"etc/systemd/system/{unit}")) as f:
                self.assertIn("\nLogNamespace=agentbox\n", f.read())

    def test_services_write_into_the_namespace_through_its_group(self):
        # systemd connects stdout and stderr with the unit's user and primary group,
        # before SupplementaryGroups=; the services' own groups stay, libvirt never
        write_group(self.root)
        self.regenerate()
        for unit, user in (("agentbox-proxy-priv.service", "agentbox-proxy"),
                           ("agentbox-ghvm-priv@.service", "agentbox-gh")):
            with open(self.path(f"etc/systemd/system/{unit}")) as f:
                text = f.read()
            self.assertIn(f"\nUser={user}\nGroup=agentbox-journal\nSupplementaryGroups={user}\n", text)
            self.assertNotIn("libvirt", text)

    def test_without_the_group_the_units_name_none(self):
        # a unit naming a group that does not exist would not start
        self.regenerate()
        with open(self.path("etc/systemd/system/agentbox-proxy-priv.service")) as f:
            self.assertNotIn("Group", f.read())


class ControlRoomWordsTest(unittest.TestCase):
    """#69: the Control Room words every action that is recorded (web/ui/app/utils/activity.ts
    SENTENCES); only older entries and actions added later show their stored text."""

    def ui(self, *parts):
        with open(os.path.join(REPO, "web", "ui", *parts)) as f:
            return f.read()

    def recorded_actions(self):
        """The commands DESCRIBE words, the sign-ins, Alerts, and the action of every
        activity.record() call in agentbox/."""
        sys.path.insert(0, REPO)
        from agentbox import activity
        actions = set(activity.DESCRIBE) | activity.SIGN_INS | {"alert"} | {a for a, _ in activity.GITHUB.values()}
        folder = os.path.join(REPO, "agentbox")
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(folder, name)) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "record" and getattr(node.func.value, "id", None) == "activity"):
                    continue
                action = node.args[1]
                if isinstance(action, ast.Constant):
                    actions.add(action.value)
                else:       # build.<status> of a Build that failed or was rejected (images._Failed)
                    self.assertEqual(ast.unparse(action), "f'build.{f.status}'")
                    actions |= {"build.failed", "build.rejected"}
        return actions

    def test_every_recorded_action_has_a_sentence(self):
        code = self.ui("app", "utils", "activity.ts")
        block = code[code.index("export const SENTENCES"):]
        sentences = set(re.findall(r"^  '([a-z.-]+)': ", block[:block.index("\n}\n")], re.M))
        actions = self.recorded_actions()
        self.assertIn("reset.update", actions)
        self.assertEqual(actions - sentences, set())

    def test_every_reason_of_the_token_service_is_translated(self):
        with open(os.path.join(REPO, "github-app", "agentbox-gh-tokend")) as f:
            source = f.read()
        reasons = set(ast.literal_eval(re.search(r"^REASONS = (\(.*?\))$", source, re.M | re.S).group(1)))
        self.assertIn("owner-not-allowed", reasons)
        for language in ("en", "de"):
            text = self.ui("i18n", "locales", language, "access.ts")
            block = text[text.index("    tokens: {"):]
            block = block[block.index("      reasons: {"):]
            translated = set(re.findall(r"^        '([a-z-]+)': ", block[:block.index("\n      },")], re.M))
            with self.subTest(language=language):
                self.assertEqual(translated, reasons)

    def test_every_reason_of_the_reset_check_is_translated(self):
        sys.path.insert(0, REPO)
        from agentbox import lifecycle
        for language in ("en", "de"):
            text = self.ui("i18n", "locales", language, "activity.ts")
            block = text[text.index("    reasons: {"):]
            translated = set(re.findall(r"^      '([a-z-]+)': ", block[:block.index("\n    },")], re.M))
            with self.subTest(language=language):
                self.assertEqual(set(lifecycle.CHECK_REASONS) - translated, set())


class JournalAccessTest(unittest.TestCase):
    """The package lets only root and the group agentbox-journal write into the namespace and
    gives the Control Room the group; agentbox setup gives the group the members of libvirt and
    the running sockets its group and mode (test_setup.py). Only a real Host shows the effect;
    these pin what it relies on."""

    def rpm(self, *parts):
        with open(os.path.join(REPO, "rpm", *parts)) as f:
            return f.read()

    def test_the_namespace_sockets_belong_to_root_and_the_group(self):
        self.assertIn("[Socket]\nSocketMode=0660\nSocketGroup=agentbox-journal\n",
                      self.rpm("units", "agentbox-journal.conf"))

    def test_the_package_creates_the_group(self):
        self.assertRegex(self.rpm("agentbox.sysusers"), r"(?m)^g agentbox-journal -$")

    def test_the_control_room_writes_through_the_group(self):
        unit = self.rpm("units", "agentbox-web.service")
        self.assertIn("[Service]\nUser=agentbox-web\nSupplementaryGroups=agentbox-journal\n", unit)
        self.assertNotIn("libvirt", unit)


if __name__ == "__main__":
    unittest.main()
