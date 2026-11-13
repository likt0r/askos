"""status, start, wake, pause, reset, restart, focus, weekly-check and observe,
and what agentbox setup sets up for a Host shutdown.

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and fake
virsh, qemu-img, ss (on PATH) and Orca CLI (under the root). Expected text is
the CLI's English output; failing programs' own messages pass through unchanged.

Run from the repo root: python3 -m unittest discover -s tests
"""
import fcntl
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import time
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(REPO, "bin", "agentbox")
TESTS = os.path.join(REPO, "tests")
CODE = "orca://pair?code=Ab3_x-9"
DAY = 86400
HEADER = "VM     STATE        RAM        BASE-DISK                SYS-AGE  AGENTS\n"
# two worktrees: two agents working, one waiting
BUSY = [{"agents": [{"state": "working"}, {"state": "waiting"}]}, {"agents": [{"state": "working"}]},
        {"agents": None}]
OLD_BASE = "/var/lib/agentbox/images/base-20261020.qcow2"
CURRENT_BASE = "/var/lib/agentbox/images/base-20261101.qcow2"      # where base.qcow2 points
START_ERROR = ("error: Failed to start domain 'agentbox-work'\n"
               "error: Cannot access storage file '/var/lib/agentbox/vms/x.qcow2': No such file or directory\n")


def journal(message):
    return {"MESSAGE": message, "__REALTIME_TIMESTAMP": "1761732000123456", "__CURSOR": "c1", "_BOOT_ID": "b1",
            "PRIORITY": "6", "SYSLOG_IDENTIFIER": "orca", "_SYSTEMD_UNIT": "agentbox-orca.service"}


def fake(path, program):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nexec python3 "{os.path.join(TESTS, program)}" "$@"\n')
    os.chmod(path, 0o755)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)
        self.root = os.path.join(self.dir, "root")
        for sub in ("var/lib/agentbox/vms", "var/lib/agentbox/images", "etc/agentbox/secrets"):
            os.makedirs(self.path(sub))
        self.base = self.path("var/lib/agentbox/images/base-20261101.qcow2")
        open(self.base, "w").close()
        os.symlink("base-20261101.qcow2", self.path("var/lib/agentbox/images/base.qcow2"))
        fake(os.path.join(self.dir, "bin", "virsh"), "fake_virsh.py")
        fake(os.path.join(self.dir, "bin", "qemu-img"), "fake_qemu_img.py")
        fake(os.path.join(self.dir, "bin", "ss"), "fake_ss.py")
        fake(self.path("opt/Orca/resources/bin/orca-ide"), "fake_orca.py")
        vm_records.write(self.root)
        self.env = {}
        self.scenario()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, rel):
        return os.path.join(self.root, rel)

    def system_disk(self, ctx):
        return self.path(f"var/lib/agentbox/vms/{ctx}-system.qcow2")

    def scenario(self, **s):
        with open(os.path.join(self.dir, "scenario.json"), "w") as f:
            json.dump(s, f)

    def disk(self, ctx, days=0.0, backing=OLD_BASE, mtime=None):
        """A System disk created from `backing`, `days` old (or last changed at `mtime`)."""
        with open(self.system_disk(ctx), "w") as f:
            json.dump({"backing-filename": backing} if backing else {}, f)
        t = mtime if mtime is not None else int(time.time() - days * DAY)
        os.utime(self.system_disk(ctx), (t, t))

    def observer(self, *ctxs):
        for ctx in ctxs:
            os.makedirs(self.path(f"var/lib/agentbox/observer-{ctx}"))

    def write(self, rel, content, mtime=None):
        with open(self.path(rel), "w") as f:
            f.write(content)
        if mtime:
            os.utime(self.path(rel), (mtime, mtime))

    def orca_listens(self):
        """Orca in the VM accepts connections (wake's TCP check goes to a local port)."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        self.addCleanup(srv.close)
        self.env["AGENTBOX_ORCA_ADDR"] = "127.0.0.1:%d" % srv.getsockname()[1]

    def orca_silent(self):
        """Nobody listens: the port stays bound without listen(), so connecting is refused
        and no other test running at the same time can start listening on it."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        self.addCleanup(srv.close)
        self.env["AGENTBOX_ORCA_ADDR"] = "127.0.0.1:%d" % srv.getsockname()[1]

    def environ(self, **env):
        return {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
                "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC",
                **self.env, **env}

    def run_agentbox(self, *args, **env):
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=self.environ(**env), timeout=60)
        return r.returncode, r.stdout, r.stderr

    def popen(self, *args, **env):
        """agentbox in the background."""
        p = subprocess.Popen([AGENTBOX, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=self.environ(**env))
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def dumpxml(self, ctx):
        r = subprocess.run([os.path.join(self.dir, "bin", "virsh"), "dumpxml", f"agentbox-{ctx}"],
                           capture_output=True, text=True, env=self.environ())
        return r.stdout

    def calls(self, cmd=None):
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                calls = [json.loads(line) for line in f]
        except FileNotFoundError:
            return []
        return [c for c in calls if cmd is None or c["cmd"] == cmd]

    def vm_calls(self):
        """What changed the VMs, in order: (command, domain), plus qemu-img create."""
        out = []
        for c in self.calls():
            if "dom" in c:
                out.append((c["cmd"], c["dom"]))
            elif c["cmd"] == "qemu-img" and c["argv"][0] == "create":
                out.append(("qemu-img create", os.path.basename(c["argv"][-1])))
        return out

    def orca_calls(self):
        return [c["argv"] for c in self.calls("orca")]

    def status(self):
        code, out, err = self.run_agentbox("status", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)


class StatusTest(LifecycleTest):
    def test_table(self):
        self.scenario(vms={"work": {"rss": 3355443}, "priv": {"state": "shut off", "managed_save": True}},
                      orca={"work": {"worktrees": BUSY}})
        self.disk("work", days=3 + 100 / DAY, backing=OLD_BASE)
        self.disk("priv", days=8, backing="/var/lib/agentbox/images/base-20261101.qcow2")
        self.observer("work", "priv")
        code, out, err = self.run_agentbox("status")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, HEADER
                         + "work   running      3.2 GB     base-20261020.qcow2      3 days   waiting=1 working=2\n"
                         + "priv   paused       -          base-20261101.qcow2      8 days   -\n")
        self.assertEqual(self.vm_calls(), [])

    def test_table_without_system_disk_or_observer(self):
        self.scenario(vms={"priv": {"state": "shut off"}})
        self.disk("priv", days=2.5, backing=None)
        code, out, _ = self.run_agentbox("status")
        self.assertEqual(code, 0)
        self.assertEqual(out, HEADER
                         + "work   running      2.0 GB     -                        0 days   ?\n"
                         + "priv   shut off     -          -                        2 days   -\n")
        self.assertEqual(self.calls("orca"), [])

    def test_table_without_agents(self):
        self.scenario(orca={"work": {"worktrees": []}, "priv": {"worktrees": [{"agents": []}]}})
        self.observer("work", "priv")
        _, out, _ = self.run_agentbox("status")
        self.assertEqual(out.splitlines()[1][-4:], "none")
        self.assertEqual(out.splitlines()[2][-4:], "none")

    def test_status_is_the_default_command(self):
        code, out, _ = self.run_agentbox()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(HEADER))

    def test_orca_not_answering_means_unknown(self):
        # The Bash version printed "?" twice here and broke the table
        self.scenario(orca={"work": {"exit": 1}})
        self.observer("work")
        _, out, _ = self.run_agentbox("status")
        self.assertEqual(out.splitlines()[1][-1:], "?")
        self.assertEqual(len(out.splitlines()), 3)
        self.assertEqual(self.orca_calls(), [["worktree", "ps", "--json", "--environment", "work"]])
        self.assertEqual(self.calls("orca")[0]["user_data"], self.path("var/lib/agentbox/observer-work"))

    def test_json_contract(self):
        """The shape the Control Room reads."""
        self.scenario(vms={"work": {"rss": 3355443}, "priv": {"state": "shut off", "managed_save": True}},
                      orca={"work": {"worktrees": BUSY}},
                      connections={"10.77.1.2:6801": 2, "10.77.2.2:6802": 1})
        self.disk("work", mtime=1761000000)
        self.disk("priv", backing="/x/base-20261101.qcow2", mtime=1762000000)
        self.observer("work", "priv")
        self.write("etc/agentbox/open-work", "4102444800\n")
        self.write("etc/agentbox/secrets/claude-work", "sk-ant-abc\n", mtime=1760000000)
        self.write("etc/agentbox/open-priv", "1000\n")                     # expired
        self.write("etc/agentbox/secrets/claude-priv", "nope\n", mtime=1760000000)
        code, out, err = self.run_agentbox("status", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, (
            '{"work": {"state": "running", "rss_kb": 3355443, "base": "base-20261020.qcow2", '
            '"system_disk_since": 1761000000, "agents": {"waiting": "1", "working": "2"}, "agents_unknown": false, '
            '"open_until": 4102444800, "claude_token_since": 1760000000, "gui_connections": 2}, '
            '"priv": {"state": "paused", "rss_kb": 0, "base": "base-20261101.qcow2", "system_disk_since": 1762000000, '
            '"agents": {}, "agents_unknown": false, "open_until": 0, "claude_token_since": 0, "gui_connections": 1}}\n'))
        self.assertEqual([c["argv"][-1] for c in self.calls("ss")], ["( dst 10.77.1.2:6801 )", "( dst 10.77.2.2:6802 )"])

    def test_every_registered_vm_in_network_order(self):
        vm_records.write(self.root, vm_records.record("lab", 5), vm_records.record("research-project", 3))
        self.scenario(vms={"priv": {"state": "shut off"}, "lab": {"state": "shut off"},
                           "research-project": {"state": "shut off"}},
                      connections={"10.77.5.2:6805": 1})
        data = self.status()
        self.assertEqual(list(data), ["work", "priv", "research-project", "lab"])
        self.assertEqual([c["argv"][-1] for c in self.calls("ss")],
                         ["( dst 10.77.1.2:6801 )", "( dst 10.77.2.2:6802 )", "( dst 10.77.3.2:6803 )",
                          "( dst 10.77.5.2:6805 )"])
        self.assertEqual(data["lab"]["gui_connections"], 1)
        _, out, _ = self.run_agentbox("status")
        self.assertEqual(out.splitlines(), [
            "VM               STATE        RAM        BASE-DISK                SYS-AGE  AGENTS",
            "work             running      2.0 GB     -                        0 days   ?",
            "priv             shut off     -          -                        0 days   -",
            "research-project shut off     -          -                        0 days   -",
            "lab              shut off     -          -                        0 days   -"])

    def test_json_without_disk_and_unknown_agents(self):
        self.scenario(vms={"priv": {"state": "shut off"}})
        code, out, _ = self.run_agentbox("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(out, (
            '{"work": {"state": "running", "rss_kb": 2097152, "base": "", "system_disk_since": 0, "agents": {}, '
            '"agents_unknown": true, "open_until": 0, "claude_token_since": 0, "gui_connections": 0}, '
            '"priv": {"state": "shut off", "rss_kb": 0, "base": "", "system_disk_since": 0, "agents": {}, '
            '"agents_unknown": false, "open_until": 0, "claude_token_since": 0, "gui_connections": 0}}\n'))

    def test_json_agents_unknown_when_orca_fails(self):
        self.scenario(orca={"work": {"exit": 1}, "priv": {"stdout": "kein JSON"}})
        self.observer("work", "priv")
        data = self.status()
        for ctx in ("work", "priv"):
            self.assertEqual((data[ctx]["agents"], data[ctx]["agents_unknown"]), ({}, True))

    def test_json_agent_without_state(self):
        self.scenario(orca={"work": {"worktrees": [{"agents": [{}]}]}})
        self.observer("work")
        self.assertEqual(self.status()["work"]["agents"], {"?": "1"})


class StartTest(LifecycleTest):
    def test_starts_a_stopped_vm(self):
        self.scenario(state="shut off")
        self.assertEqual(self.run_agentbox("start", "work"), (0, "work is running\n", ""))
        self.assertEqual(self.vm_calls(), [("start", "agentbox-work")])

    def test_running_vm_stays(self):
        self.assertEqual(self.run_agentbox("start", "priv"), (0, "priv is running\n", ""))
        self.assertEqual(self.vm_calls(), [])

    def test_resumes_a_paused_vm(self):
        self.scenario(vms={"work": {"state": "shut off", "managed_save": True}})
        self.run_agentbox("start", "work")
        self.assertEqual(self.status()["work"]["state"], "running")

    def test_virsh_error_passes_through(self):
        self.scenario(vms={"work": {"state": "shut off", "start_fails": True}})
        self.assertEqual(self.run_agentbox("start", "work"), (1, "", START_ERROR))

    def test_unknown_vm(self):
        self.assertEqual(self.run_agentbox("start", "nope"), (1, "", "agentbox: No such VM: nope\n"))
        self.assertEqual(self.calls(), [])

    def test_json(self):
        self.scenario(state="shut off")
        code, out, _ = self.run_agentbox("start", "work", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"vm": "work", "state": "running", "message": "work is running"})


class WakeTest(LifecycleTest):
    def test_starts_and_waits_for_orca(self):
        self.scenario(state="shut off")
        self.orca_listens()
        self.assertEqual(self.run_agentbox("wake", "work"), (0, "", ""))
        self.assertEqual(self.vm_calls(), [("start", "agentbox-work")])

    def test_gives_up_when_orca_does_not_answer(self):
        self.orca_silent()
        start = time.monotonic()
        code, out, err = self.run_agentbox("wake", "work", AGENTBOX_WAKE_SECONDS="2")
        self.assertEqual((code, out, err), (1, "", "agentbox: work: Orca in the VM does not answer\n"))
        self.assertLess(time.monotonic() - start, 10)

    def test_json(self):
        self.orca_listens()
        code, out, _ = self.run_agentbox("wake", "priv", "--json")
        self.assertEqual(json.loads(out), {"vm": "priv", "state": "running", "message": None})


class PauseTest(LifecycleTest):
    def test_pauses_a_running_vm(self):
        self.assertEqual(self.run_agentbox("pause", "work"), (0, "work paused\n", ""))
        self.assertEqual(self.vm_calls(), [("managedsave", "agentbox-work")])
        self.assertEqual(self.status()["work"]["state"], "paused")

    def test_stopped_vm_stays_as_it_is(self):
        self.scenario(state="shut off")
        self.assertEqual(self.run_agentbox("pause", "work"), (0, "", ""))
        self.assertEqual(self.vm_calls(), [])

    def test_json(self):
        _, out, _ = self.run_agentbox("pause", "priv", "--json")
        self.assertEqual(json.loads(out), {"vm": "priv", "state": "paused", "message": "priv paused"})


class ResetTest(LifecycleTest):
    def create_argv(self, ctx):
        return ["create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", self.base, self.system_disk(ctx)]

    def backing(self, ctx):
        with open(self.system_disk(ctx)) as f:
            return json.load(f)["backing-filename"]

    def test_new_system_disk_from_the_current_build(self):
        self.disk("work", days=9)
        code, out, err = self.run_agentbox("reset", "work")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "work: fresh System disk (base-20261101.qcow2)\nwork started\n")
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-work"), ("shutdown", "agentbox-work"),
                                           ("qemu-img create", "work-system.qcow2"), ("start", "agentbox-work")])
        self.assertEqual(self.calls("qemu-img")[-1]["argv"], self.create_argv("work"))
        self.assertEqual(self.backing("work"), self.base)
        self.assertLess(time.time() - os.stat(self.system_disk("work")).st_mtime, 60)

    def test_forces_off_a_vm_that_ignores_shutdown(self):
        self.scenario(vms={"priv": {"shutdown": "ignored"}})
        code, out, _ = self.run_agentbox("reset", "priv", AGENTBOX_SHUTDOWN_SECONDS="1")
        self.assertEqual(code, 0)
        self.assertEqual(out, "priv: fresh System disk (base-20261101.qcow2)\npriv started\n")
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-priv"), ("shutdown", "agentbox-priv"),
                                           ("destroy", "agentbox-priv"), ("qemu-img create", "priv-system.qcow2"),
                                           ("start", "agentbox-priv")])

    def test_no_start(self):
        self.assertEqual(self.run_agentbox("reset", "work", "--no-start"),
                         (0, "work: fresh System disk (base-20261101.qcow2)\n", ""))
        self.assertNotIn(("start", "agentbox-work"), self.vm_calls())
        self.assertEqual(self.status()["work"]["state"], "shut off")

    def test_discards_a_pause(self):
        self.scenario(vms={"work": {"state": "shut off", "managed_save": True}})
        self.run_agentbox("reset", "work", "--no-start")
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-work"),
                                           ("qemu-img create", "work-system.qcow2")])
        self.assertEqual(self.status()["work"]["state"], "shut off")

    def test_without_base_image(self):
        os.remove(self.path("var/lib/agentbox/images/base.qcow2"))
        self.scenario(state="shut off")
        self.assertEqual(self.run_agentbox("reset", "work"),
                         (1, "", "agentbox: no Build of Image base, run agentbox build base first\n"))
        self.assertEqual(self.calls("qemu-img"), [])

    def test_start_failure_fails_the_reset(self):
        # the Bash version exited 0 with virsh's message on stderr (#52)
        self.scenario(vms={"work": {"start_fails": True}})
        self.assertEqual(self.run_agentbox("reset", "work"), (
            1, "work: fresh System disk (base-20261101.qcow2)\n",
            "agentbox: work: fresh System disk, but the start fails: " + " ".join(START_ERROR.split()) + "\n"))
        self.assertEqual(self.backing("work"), self.base)

    def test_already_active_at_the_end_fails_the_reset(self):
        self.scenario(vms={"work": {"start_fails": "active"}})
        code, out, err = self.run_agentbox("reset", "work", "--json")
        self.assertEqual((code, out), (1, ""))
        # with --json the failure is one JSON object: code, message, parameters
        self.assertEqual(json.loads(err), {
            "error": "reset_start_failed", "params": {"vm": "work"},
            "message": "work: fresh System disk, but the start fails: error: Failed to start domain 'agentbox-work' "
                       "error: Requested operation is not valid: domain is already active"})

    def test_a_vm_running_again_keeps_its_system_disk(self):
        # something started the VM between the stop and qemu-img create: its QEMU keeps
        # the old chain, a new header under it would break the next cold start (#52)
        self.scenario(vms={"work": {"comes_back": True}})
        self.disk("work", days=9)
        code, out, err = self.run_agentbox("reset", "work", AGENTBOX_SHUTDOWN_SECONDS="1")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "agentbox: work: the VM is not off (running), the System disk stays unchanged\n")
        self.assertEqual(self.calls("qemu-img"), [])
        self.assertEqual(self.backing("work"), OLD_BASE)

    def test_a_vm_without_domain_gets_its_disk(self):
        # create resets before the domain is defined
        self.scenario(vms={"work": {"defined": False}})
        self.assertEqual(self.run_agentbox("reset", "work", "--no-start"),
                         (0, "work: fresh System disk (base-20261101.qcow2)\n", ""))

    def test_vm_running_on_another_build_is_forced_off(self):
        self.scenario(vms={"work": {"pinned_backing": OLD_BASE}})
        code, out, err = self.run_agentbox("reset", "work")
        self.assertEqual((code, out), (1, "work: fresh System disk (base-20261101.qcow2)\nwork started\n"))
        self.assertEqual(err, "agentbox: work: the VM ran on another Build (base-20261020.qcow2) than its System "
                              "disk (base-20261101.qcow2) and was forced off; agentbox reset work again\n")
        self.assertEqual(self.vm_calls()[-2:], [("start", "agentbox-work"), ("destroy", "agentbox-work")])
        self.assertEqual(self.status()["work"]["state"], "shut off")

    def test_the_started_vm_runs_on_the_new_build(self):
        self.disk("work", days=9)
        self.assertEqual(self.run_agentbox("reset", "work")[0], 0)
        self.assertEqual(self.vm_calls()[-1], ("start", "agentbox-work"))     # not forced off
        self.assertIn(f"<source file=\"{self.base}\"/>", self.dumpxml("work"))

    def test_json(self):
        code, out, _ = self.run_agentbox("reset", "work", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"vm": "work", "state": "running",
                                           "message": "work: fresh System disk (base-20261101.qcow2)\nwork started"})
        _, out, _ = self.run_agentbox("reset", "work", "--json", "--no-start")
        self.assertEqual(json.loads(out)["state"], "shut off")


class RestartTest(LifecycleTest):
    def test_restarts_keeping_the_system_disk(self):
        self.disk("work", days=3)
        self.assertEqual(self.run_agentbox("restart", "work"), (0, "work restarted\n", ""))
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-work"), ("shutdown", "agentbox-work"),
                                           ("start", "agentbox-work")])
        with open(self.system_disk("work")) as f:
            self.assertEqual(json.load(f)["backing-filename"], OLD_BASE)

    def test_starts_a_stopped_vm(self):
        self.scenario(state="shut off")
        self.assertEqual(self.run_agentbox("restart", "priv"), (0, "priv restarted\n", ""))
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-priv"), ("start", "agentbox-priv")])

    def test_forces_off_a_vm_that_ignores_shutdown(self):
        self.scenario(vms={"priv": {"shutdown": "ignored"}})
        self.run_agentbox("restart", "priv", AGENTBOX_SHUTDOWN_SECONDS="1")
        self.assertEqual([c for c, _ in self.vm_calls()], ["managedsave-remove", "shutdown", "destroy", "start"])

    def test_boots_a_paused_vm_fresh(self):
        self.scenario(vms={"work": {"state": "shut off", "managed_save": True}})
        self.run_agentbox("restart", "work")
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-work"), ("start", "agentbox-work")])

    def test_start_failure(self):
        self.scenario(vms={"work": {"start_fails": True}})
        self.assertEqual(self.run_agentbox("restart", "work"), (1, "", START_ERROR))

    def test_json(self):
        _, out, _ = self.run_agentbox("restart", "work", "--json")
        self.assertEqual(json.loads(out), {"vm": "work", "state": "running", "message": "work restarted"})


class LifecycleLockTest(LifecycleTest):
    """#52: what stops a VM and needs it to stay off (Reset, Restart, Pause, delete) holds
    the VM's lifecycle lock; what starts it (wake, start, pair) waits for it."""

    def hold(self, ctx):
        """Hold the VM's lifecycle lock as another agentbox call would; released by os.close."""
        lock = self.path(f"var/lib/agentbox/locks/lifecycle-{ctx}.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def wait_for(self, cmd):
        deadline = time.monotonic() + 20
        while not any(c == cmd for c, _ in self.vm_calls()):
            self.assertLess(time.monotonic(), deadline, f"no {cmd}")
            time.sleep(0.05)

    def fresh(self, **scenario):
        for name in ("calls.jsonl", "domains.json"):
            if os.path.exists(os.path.join(self.dir, name)):
                os.remove(os.path.join(self.dir, name))
        self.scenario(**scenario)

    def test_wake_during_a_reset_waits_and_finds_the_vm_running(self):
        # 2026-11-04: a wake started priv on its old disk while the Reset created the new one
        self.orca_listens()
        self.disk("work", days=9)
        reset = self.popen("reset", "work", FAKE_QEMU_IMG_CREATE_SECONDS="2")
        self.wait_for("qemu-img create")
        self.assertEqual(self.run_agentbox("wake", "work"), (0, "", ""))
        out, err = reset.communicate(timeout=60)
        self.assertEqual((reset.returncode, err), (0, ""))
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-work"), ("shutdown", "agentbox-work"),
                                           ("qemu-img create", "work-system.qcow2"), ("start", "agentbox-work")])
        self.assertIn(f"<source file=\"{self.base}\"/>", self.dumpxml("work"))

    def test_wake_during_a_reset_without_start_starts_the_new_disk(self):
        self.orca_listens()
        self.disk("work", days=9)
        reset = self.popen("reset", "work", "--no-start", FAKE_QEMU_IMG_CREATE_SECONDS="2")
        self.wait_for("qemu-img create")
        self.assertEqual(self.run_agentbox("wake", "work"), (0, "", ""))
        _, err = reset.communicate(timeout=60)
        self.assertEqual((reset.returncode, err), (0, ""))
        self.assertEqual(self.vm_calls()[2:], [("qemu-img create", "work-system.qcow2"), ("start", "agentbox-work")])
        self.assertIn(f"<source file=\"{self.base}\"/>", self.dumpxml("work"))

    def test_each_command_waits_for_the_lock(self):
        self.orca_listens()
        code = [{"entries": [journal(f"Pairing: {CODE}")]}]
        cases = [
            (("wake", "work"), "shut off", [("start", "agentbox-work")]),
            (("start", "work"), "shut off", [("start", "agentbox-work")]),
            (("pair", "work"), "shut off", [("start", "agentbox-work")]),
            (("pause", "work"), "running", [("managedsave", "agentbox-work")]),
            (("restart", "work"), "running", [("managedsave-remove", "agentbox-work"), ("shutdown", "agentbox-work"),
                                              ("start", "agentbox-work")]),
            (("reset", "work", "--no-start"), "running", [("managedsave-remove", "agentbox-work"),
                                                          ("shutdown", "agentbox-work"),
                                                          ("qemu-img create", "work-system.qcow2")]),
        ]
        for args, state, changes in cases:
            with self.subTest(args=args):
                self.fresh(state=state, responses=code)
                fd = self.hold("work")
                try:
                    p = self.popen(*args)
                    time.sleep(1)
                    self.assertIsNone(p.poll())
                    self.assertEqual(self.vm_calls(), [])
                finally:
                    os.close(fd)
                _, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, err)
                self.assertEqual([c for c in self.vm_calls() if c[0] != "guest-exec"], changes)

    def test_other_vms_do_not_wait(self):
        self.scenario(state="shut off")
        fd = self.hold("priv")
        self.addCleanup(os.close, fd)
        self.assertEqual(self.run_agentbox("start", "work"), (0, "work is running\n", ""))


class FocusTest(LifecycleTest):
    def test_work_pauses_priv(self):
        self.assertEqual(self.run_agentbox("focus", "work"), (0, "priv paused\n", ""))
        self.assertEqual(self.vm_calls(), [("managedsave", "agentbox-priv")])

    def test_priv_pauses_work(self):
        self.assertEqual(self.run_agentbox("focus", "priv"), (0, "work paused\n", ""))
        self.assertEqual(self.vm_calls(), [("managedsave", "agentbox-work")])

    def test_pauses_every_other_vm(self):
        vm_records.write(self.root, vm_records.record("lab", 3))
        self.assertEqual(self.run_agentbox("focus", "priv"), (0, "work paused\nlab paused\n", ""))
        self.assertEqual(self.vm_calls(), [("managedsave", "agentbox-work"), ("managedsave", "agentbox-lab")])

    def test_other_vm_not_running(self):
        self.scenario(vms={"priv": {"state": "shut off"}})
        self.assertEqual(self.run_agentbox("focus", "work"), (0, "", ""))
        self.assertEqual(self.vm_calls(), [])

    def test_off(self):
        self.assertEqual(self.run_agentbox("focus", "off"), (0, "all VMs wake up again when needed\n", ""))
        self.assertEqual(self.calls(), [])

    def test_invalid(self):
        self.assertEqual(self.run_agentbox("focus", "x"), (1, "", "agentbox: No such VM: x\n"))
        self.assertEqual(self.run_agentbox("focus", "typo"), (1, "", "agentbox: No such VM: typo\n"))
        os.remove(self.path("etc/agentbox/vms/work.json"))
        os.remove(self.path("etc/agentbox/vms/priv.json"))
        self.assertEqual(self.run_agentbox("focus", "typo"), (1, "", "agentbox: No such VM: typo\n"))
        self.assertEqual(self.run_agentbox("focus"), (1, "", "agentbox: focus <vm>|off\n"))
        self.assertEqual(self.calls(), [])

    def test_json(self):
        _, out, _ = self.run_agentbox("focus", "work", "--json")
        self.assertEqual(json.loads(out), {"focus": "work", "others": [{"vm": "priv", "state": "paused"}],
                                           "message": "priv paused"})
        _, out, _ = self.run_agentbox("focus", "off", "--json")
        self.assertEqual(json.loads(out), {"focus": "off", "others": [],
                                           "message": "all VMs wake up again when needed"})


class WeeklyCheckTest(LifecycleTest):
    def disk(self, ctx, days=0.0, backing=CURRENT_BASE, mtime=None):
        """A System disk on the Image's current Build: only its age makes a Reset due
        (WeeklyUpdateTest: a newer Build)."""
        LifecycleTest.disk(self, ctx, days, backing, mtime)

    def test_old_and_stopped_is_reset_without_starting(self):
        self.scenario(vms={"work": {"state": "shut off"}})
        self.disk("work", days=8)
        self.disk("priv", days=6)
        self.assertEqual(self.run_agentbox("weekly-check"), (0, "work: fresh System disk (base-20261101.qcow2)\n", ""))
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-work"),
                                           ("qemu-img create", "work-system.qcow2")])

    def test_gui_connected_postpones(self):
        self.scenario(connections={"10.77.1.2:6801": 1}, orca={"work": {"worktrees": []}})
        self.disk("work", days=7 + 60 / DAY)
        self.observer("work")
        self.assertEqual(self.run_agentbox("weekly-check"), (0, "work: Reset postponed (GUI connected)\n", ""))
        self.assertEqual(self.vm_calls(), [])

    def test_working_agents_postpone(self):
        self.scenario(orca={"work": {"worktrees": BUSY}, "priv": {"worktrees": []}})
        self.disk("work", days=10)
        self.disk("priv", days=10)
        self.observer("work", "priv")
        self.assertEqual(self.run_agentbox("weekly-check"), (0, (
            "work: Reset postponed (agents: waiting=1 working=2)\n"
            "priv: fresh System disk (base-20261101.qcow2)\npriv started\n"), ""))
        self.assertEqual([c for c in self.vm_calls() if c[1].startswith("agentbox-work")], [])

    def test_unknown_agents_postpone(self):
        self.disk("work", days=10)                      # no Observer: agents unknown
        self.assertEqual(self.run_agentbox("weekly-check"), (0, "work: Reset postponed (agents: ?)\n", ""))
        self.assertEqual(self.vm_calls(), [])

    def test_orca_not_answering_postpones(self):
        # The Bash version reset here: its "?" came out twice and matched nothing
        self.scenario(orca={"work": {"exit": 1}})
        self.disk("work", days=10)
        self.observer("work")
        self.assertEqual(self.run_agentbox("weekly-check"), (0, "work: Reset postponed (agents: ?)\n", ""))
        self.assertEqual(self.vm_calls(), [])

    def test_idle_is_reset_and_started(self):
        self.scenario(orca={"priv": {"worktrees": [{"agents": [{"state": "waiting"}]}]}})
        self.disk("priv", days=10)
        self.observer("priv")
        self.assertEqual(self.run_agentbox("weekly-check"),
                         (0, "priv: fresh System disk (base-20261101.qcow2)\npriv started\n", ""))
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-priv"), ("shutdown", "agentbox-priv"),
                                           ("qemu-img create", "priv-system.qcow2"), ("start", "agentbox-priv")])

    def test_young_or_missing_disks_are_left_alone(self):
        self.disk("work", days=6.9)
        self.assertEqual(self.run_agentbox("weekly-check"), (0, "", ""))
        # only read which Build the System disks came from (auto_update)
        self.assertEqual([c for c in self.calls() if not (c["cmd"] == "qemu-img" and c["argv"][0] == "info")], [])

    def test_json(self):
        self.scenario(vms={"work": {"state": "shut off"}}, connections={"10.77.2.2:6802": 1})
        self.disk("work", days=8)
        self.disk("priv", days=8)
        code, out, _ = self.run_agentbox("weekly-check", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [
            {"vm": "work", "action": "reset", "reason": "stopped", "update": None,
             "message": "work: fresh System disk (base-20261101.qcow2)"},
            {"vm": "priv", "action": "postponed", "reason": "gui-connected", "update": None,
             "message": "priv: Reset postponed (GUI connected)"},
        ])
        os.remove(self.system_disk("work"))
        self.disk("priv", days=1)
        self.scenario()
        _, out, _ = self.run_agentbox("weekly-check", "--json")
        self.assertEqual(json.loads(out), [
            {"vm": "work", "action": "skipped", "reason": "no-system-disk", "update": None, "message": None},
            {"vm": "priv", "action": "skipped", "reason": "recent", "update": None, "message": None},
        ])

    def test_json_agents(self):
        self.scenario(orca={"work": {"worktrees": BUSY}})
        self.disk("work", days=8)
        self.disk("priv", days=8)
        self.observer("work")
        _, out, _ = self.run_agentbox("weekly-check", "--json")
        self.assertEqual([(r["action"], r["reason"]) for r in json.loads(out)],
                         [("postponed", "agents-working"), ("postponed", "agents-unknown")])


class WeeklyUpdateTest(LifecycleTest):
    """With auto_update on (the default, #114), the daily check also resets a VM whose Image has
    a newer current Build than its System disk, however young the disk, by the same idle rules;
    pinned VMs are left alone. With it off, only the age counts, as before."""

    def setUp(self):
        super().setUp()
        self.disk("priv", days=1, backing=CURRENT_BASE)       # on the current Build: nothing to do

    def check(self):
        code, out, err = self.run_agentbox("weekly-check", "--json")
        self.assertEqual(code, 0, err)
        return {r["vm"]: r for r in json.loads(out)}

    def test_an_idle_vm_on_an_older_build_is_reset_onto_the_current_one(self):
        self.scenario(orca={"work": {"worktrees": [{"agents": [{"state": "waiting"}]}]}})
        self.disk("work", days=1, backing=OLD_BASE)
        self.observer("work")
        self.assertEqual(self.run_agentbox("weekly-check"),
                         (0, "work: fresh System disk (base-20261101.qcow2)\nwork started\n", ""))
        self.assertEqual(self.vm_calls(), [("managedsave-remove", "agentbox-work"), ("shutdown", "agentbox-work"),
                                           ("qemu-img create", "work-system.qcow2"), ("start", "agentbox-work")])
        with open(self.system_disk("work")) as f:
            self.assertEqual(json.load(f)["backing-filename"], self.path("var/lib/agentbox/images/base-20261101.qcow2"))

    def test_a_stopped_one_is_reset_without_starting(self):
        self.scenario(vms={"work": {"state": "shut off"}})
        self.disk("work", days=0, backing=OLD_BASE)
        result = self.check()
        self.assertEqual({k: result["work"][k] for k in ("action", "reason", "update")},
                         {"action": "reset", "reason": "stopped", "update": "base-20261101"})
        self.assertEqual({k: result["priv"][k] for k in ("action", "reason", "update")},
                         {"action": "skipped", "reason": "recent", "update": None})
        self.assertNotIn(("start", "agentbox-work"), self.vm_calls())

    def test_the_idle_rules_postpone_it(self):
        self.disk("work", days=1, backing=OLD_BASE)
        self.observer("work")
        for scenario, reason, said in (
                ({"connections": {"10.77.1.2:6801": 1}, "orca": {"work": {"worktrees": []}}}, "gui-connected",
                 "GUI connected"),
                ({"orca": {"work": {"worktrees": BUSY}}}, "agents-working", "agents: waiting=1 working=2"),
                ({"orca": {"work": {"exit": 1}}}, "agents-unknown", "agents: ?"),
                ({"ss_fails": True}, "gui-unknown", "GUI connections unknown")):
            with self.subTest(reason=reason):
                self.scenario(**scenario)
                result = self.check()["work"]
                self.assertEqual((result["action"], result["reason"], result["update"]),
                                 ("postponed", reason, "base-20261101"))
                self.assertEqual(result["message"], f"work: Reset onto the new Build base-20261101 postponed ({said})")
        self.assertEqual(self.vm_calls(), [])

    def test_pinned_vms_and_other_disks_are_left_alone(self):
        vm_records.write(self.root, vm_records.record("work", 1, build="20261020"), vm_records.record("priv", 2))
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off"}})
        self.disk("work", days=1, backing=OLD_BASE)
        for backing in ("/var/lib/agentbox/images/rust-20261020.qcow2", "/var/lib/agentbox/images/base.qcow2", None):
            with self.subTest(backing=backing):
                self.disk("priv", days=1, backing=backing)
                result = self.check()
                self.assertEqual([(r["action"], r["update"]) for r in result.values()],
                                 [("skipped", None), ("skipped", None)])
        self.assertEqual(self.vm_calls(), [])

    def test_a_disk_on_a_newer_build_is_no_update(self):
        self.disk("work", days=1, backing="/var/lib/agentbox/images/base-20261102.qcow2")
        self.assertEqual(self.check()["work"]["action"], "skipped")

    def test_off_only_the_age_counts(self):
        self.write("etc/agentbox/settings.json", '{"auto_update": false}')
        self.scenario(vms={"work": {"state": "shut off"}})
        self.disk("work", days=1, backing=OLD_BASE)
        self.assertEqual(self.run_agentbox("weekly-check"), (0, "", ""))
        self.disk("work", days=8, backing=OLD_BASE)
        result = self.check()["work"]
        self.assertEqual((result["action"], result["reason"], result["update"]), ("reset", "stopped", None))


class ObserveTest(LifecycleTest):
    def setUp(self):
        super().setUp()
        self.orca_listens()

    def test_pairs_the_host_as_observer(self):
        self.scenario(responses=[{"entries": [journal(f"Pairing: {CODE}")]}], orca={"work": {"worktrees": BUSY}})
        code, out, err = self.run_agentbox("observe", "work")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "work: the Host observes Orca in the VM. Agents: waiting=1 working=2\n")
        self.assertEqual(self.orca_calls(), [
            ["environment", "rm", "--environment", "work"],
            ["environment", "add", "--name", "work", "--pairing-code", CODE],
            ["worktree", "ps", "--json", "--environment", "work"],
        ])
        observer = self.path("var/lib/agentbox/observer-work")
        self.assertEqual({c["user_data"] for c in self.calls("orca")}, {observer})
        self.assertEqual(stat.S_IMODE(os.stat(observer).st_mode), 0o700)

    def test_starts_a_stopped_vm(self):
        self.scenario(state="shut off", responses=[{"entries": [journal(f"Pairing: {CODE}")]}])
        code, out, _ = self.run_agentbox("observe", "priv")
        self.assertEqual((code, out), (0, "priv: the Host observes Orca in the VM. Agents: \n"))
        self.assertEqual(self.vm_calls(), [("start", "agentbox-priv")])

    def test_without_pairing_code_nothing_is_paired(self):
        self.scenario(responses=[{"stdout": "", "exitcode": 1}])
        code, out, err = self.run_agentbox("observe", "work", AGENTBOX_PAIR_SECONDS="1")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "agentbox: work: no Pairing code found (Orca may still be starting)\n")
        self.assertEqual(self.orca_calls(), [["environment", "rm", "--environment", "work"]])

    def test_orca_refusing_the_code(self):
        self.scenario(responses=[{"entries": [journal(f"Pairing: {CODE}")]}], orca={"add_exit": 3})
        self.assertEqual(self.run_agentbox("observe", "work"), (3, "", "Error: pairing failed: invalid pairing code\n"))

    def test_orca_in_the_vm_not_answering(self):
        self.orca_silent()
        code, _, err = self.run_agentbox("observe", "work", AGENTBOX_WAKE_SECONDS="1")
        self.assertEqual((code, err), (1, "agentbox: work: Orca in the VM does not answer\n"))
        self.assertEqual(self.calls("orca"), [])

    def test_json(self):
        self.scenario(responses=[{"entries": [journal(f"Pairing: {CODE}")]}], orca={"work": {"worktrees": BUSY}})
        code, out, _ = self.run_agentbox("observe", "work", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {
            "vm": "work", "agents": {"waiting": "1", "working": "2"}, "agents_unknown": False,
            "message": "work: the Host observes Orca in the VM. Agents: waiting=1 working=2"})


class WeeklyCheckFailSafeTest(LifecycleTest):
    """weekly-check postpones when it cannot tell whether a laptop is attached, and
    one failing VM does not stop the check of the others."""

    disk = WeeklyCheckTest.disk

    def test_unknown_gui_state_postpones(self):
        self.disk("priv", days=8)
        self.disk("work", days=1)
        self.observer("priv")
        self.scenario(vms={"priv": {"state": "running"}, "work": {"state": "shut off"}}, ss_fails=True,
                      orca={"priv": {"worktrees": []}})
        code, out, _ = self.run_agentbox("weekly-check", "--json")
        result = {r["vm"]: r for r in json.loads(out)}
        self.assertEqual(result["priv"]["action"], "postponed")
        self.assertEqual(result["priv"]["reason"], "gui-unknown")
        self.assertNotIn("shutdown", [c for c, _ in self.vm_calls()])

    def test_one_failing_vm_does_not_stop_the_others(self):
        os.remove(self.path("var/lib/agentbox/images/base.qcow2"))
        self.disk("work", days=8)
        self.disk("priv", days=8)
        self.scenario(state="shut off")
        code, out, err = self.run_agentbox("weekly-check", "--json")
        self.assertNotEqual(code, 0)
        result = {r["vm"]: r for r in json.loads(out)}
        self.assertEqual(set(result), {"work", "priv"})
        for vm in ("work", "priv"):
            self.assertEqual(result[vm]["action"], "failed")
            self.assertIn("no Build of Image base", result[vm]["message"])

    def test_broken_record_does_not_stop_the_others(self):
        self.write("etc/agentbox/vms/lab.json", '{"name": "lab"')
        self.disk("work", days=1)
        self.disk("priv", days=1)
        code, out, _ = self.run_agentbox("weekly-check", "--json")
        self.assertNotEqual(code, 0)
        result = {r["vm"]: r for r in json.loads(out)}
        self.assertEqual((result["work"]["action"], result["priv"]["action"]), ("skipped", "skipped"))
        self.assertEqual((result["lab"]["action"], result["lab"]["reason"]), ("failed", "error"))
        self.assertIn("lab.json", result["lab"]["message"])


class HostShutdownTest(unittest.TestCase):
    """agentbox setup has libvirt-guests shut the VMs down when the Host shuts down, instead
    of systemd killing QEMU: it writes this configuration and enables the service, never
    restarting it (test_setup.py). Only a real Host shutdown shows the effect; these pin
    the configuration it relies on."""

    def config(self):
        """/etc/sysconfig/libvirt-guests as libvirt-guests.sh sees it: it sources the file."""
        sys.path.insert(0, REPO)
        self.addCleanup(sys.path.remove, REPO)
        from agentbox import hoststeps
        with tempfile.NamedTemporaryFile("w") as f:
            f.write(hoststeps.LIBVIRT_GUESTS)
            f.flush()
            out = subprocess.run(["env", "-i", "sh", "-c", 'set -a; . "$1"; env', "sh", f.name],
                                 capture_output=True, text=True, check=True).stdout
        return dict(line.split("=", 1) for line in out.splitlines())

    def test_running_vms_are_shut_down_and_none_starts_at_boot(self):
        config = self.config()
        self.assertEqual((config["URIS"], config["ON_SHUTDOWN"], config["ON_BOOT"]),
                         ("qemu:///system", "shutdown", "ignore"))

    def test_all_vms_at_once_with_the_time_limit_of_a_reset(self):
        config = self.config()
        # every VM the registry can hold (network numbers 1 to 254); 1 would mean one by one
        self.assertGreaterEqual(int(config["PARALLEL_SHUTDOWN"]), 254)
        with open(os.path.join(REPO, "agentbox", "lifecycle.py")) as f:
            stop = re.search(r'tunable\("AGENTBOX_SHUTDOWN_SECONDS", (\d+)\)', f.read()).group(1)
        self.assertEqual(config["SHUTDOWN_TIMEOUT"], stop)


if __name__ == "__main__":
    unittest.main()
