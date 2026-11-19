"""`agentbox alert-watch` (#11, ADR 0022): a VM whose own proxy raised an Alert with a strong
Signal is paused; a weak Signal, another VM's proxy, a forged entry, alert_action=none, a VM
that is off or paused already change nothing; the Pause waits for the VM's lifecycle lock.

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and fake virsh and
journalctl on PATH; the tests write the proxies' lines into the fake journal as journald
stamps them (journal_writers.py), and read what the watcher sent to the Activity journal
(fake_journald.py), what it did to the VMs (fake_virsh.py) and what describe shows.
Run from the repo root: python3 -m unittest discover -s tests
"""
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_virsh  # noqa: E402
import vm_records  # noqa: E402
from fake_journald import Journald  # noqa: E402
from journal_writers import CLI, FORGED, proxy, write_passwd, written_by  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(REPO, "bin", "agentbox")
TESTS = os.path.join(REPO, "tests")
WAIT = 30                            # seconds a test waits for what has to come
QUIET = 1.5                          # seconds a test waits to see that nothing happens
# the Signals of the proxy's Alerts, as the classifier names them (proxy/agentbox-suspicious)
STRONG = ("exfil", "c2", "tunnel", "paste", "phishing", "blockchain", "lookalike", "raw-ip", "metadata", "threat")
WEAK = ("young", "payload", "random-label", "tld", "many-labels")


def fake(path, program):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nexec python3 "{os.path.join(TESTS, program)}" "$@"\n')
    os.chmod(path, 0o755)


class AlertWatchTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = os.path.join(self.dir, "root")
        for sub in ("var/lib/agentbox/vms", "var/lib/agentbox/images", "etc/agentbox"):
            os.makedirs(self.path(sub))
        open(self.path("var/lib/agentbox/images/base-20261101.qcow2"), "w").close()
        os.symlink("base-20261101.qcow2", self.path("var/lib/agentbox/images/base.qcow2"))
        for prog, script in (("virsh", "fake_virsh.py"), ("journalctl", "fake_journalctl.py"),
                             ("qemu-img", "fake_qemu_img.py"), ("ss", "fake_ss.py"), ("systemctl", "fake_systemctl.py")):
            fake(os.path.join(self.dir, "bin", prog), script)
        vm_records.write(self.root)
        write_passwd(self.root)
        self.journald = Journald(self.path("run/systemd/journal.agentbox/socket"))
        self.addCleanup(self.journald.close)
        self.journal = []
        self.write_journal()
        self.scenario()
        self.proc = None

    def path(self, rel):
        return os.path.join(self.root, rel)

    def scenario(self, **s):
        tmp = os.path.join(self.dir, "scenario.json.new")
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, os.path.join(self.dir, "scenario.json"))

    def settings(self, **values):
        with open(self.path("etc/agentbox/settings.json"), "w") as f:
            json.dump(values, f)

    def env(self, **extra):
        return {"PATH": os.path.join(self.dir, "bin") + ":/usr/bin:/bin", "FAKE_VIRSH_DIR": self.dir,
                "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC",
                "AGENTBOX_ALERT_WATCH_RETRY_MS": "200", "AGENTBOX_ORCA_ADDR": "127.0.0.1:9", **extra}

    def run_agentbox(self, *args):
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=self.env(), timeout=60)
        return r.returncode, r.stdout, r.stderr

    # --- the journal ------------------------------------------------------------------

    def write_journal(self):
        tmp = os.path.join(self.dir, "journal.json.new")
        with open(tmp, "w") as f:
            json.dump(self.journal, f)
        os.replace(tmp, os.path.join(self.dir, "journal.json"))

    def line(self, message, identifier, by, age=0):
        """A proxy line in the Activity namespace as journald stamps it for `by`, `age` seconds old."""
        e = {"MESSAGE": message, "SYSLOG_IDENTIFIER": identifier,
             "__REALTIME_TIMESTAMP": str(int((time.time() - age) * 1e6)), "__CURSOR": f"c{len(self.journal)}",
             "PRIORITY": "6", "_FAKE_NAMESPACE": "agentbox", **by}
        self.journal.append(e)
        self.write_journal()

    def alert(self, vm, host, *signals, age=0):
        """An Alert of the VM's own proxy."""
        self.line(f"[{vm}] ALERT {host} [{','.join(signals)}]", f"agentbox-proxy-{vm}", proxy(vm), age)

    def read_up_to(self, age):
        """As if an earlier run had read the journal up to `age` seconds ago."""
        os.makedirs(self.path("var/lib/agentbox"), exist_ok=True)
        with open(self.path("var/lib/agentbox/alert-watch.json"), "w") as f:
            json.dump({"last": int((time.time() - age) * 1e6)}, f)

    # --- the watcher ------------------------------------------------------------------

    def start(self):
        self.err = open(os.path.join(self.dir, "alert-watch.err"), "w+")
        self.addCleanup(self.err.close)
        self.out = open(os.path.join(self.dir, "alert-watch.out"), "w+")
        self.addCleanup(self.out.close)
        before = len(self.followers())
        self.proc = subprocess.Popen([AGENTBOX, "alert-watch"], stdin=subprocess.DEVNULL, stdout=self.out,
                                     stderr=self.err, env=self.env())
        self.addCleanup(self.stop)
        # its journalctl reports what is written after it started
        self.until(lambda: len(self.followers()) > before, "no journal follower")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def stderr(self):
        with open(os.path.join(self.dir, "alert-watch.err")) as f:
            return f.read()

    def until(self, found, what, timeout=WAIT):
        deadline = time.monotonic() + timeout
        while True:
            value = found()
            if value:
                return value
            if self.proc and self.proc.poll() is not None:
                self.fail(f"alert-watch ended ({self.proc.returncode}); {what}; stderr: {self.stderr()}")
            if time.monotonic() > deadline:
                self.fail(f"{what}; stderr: {self.stderr() if self.proc else ''}")
            time.sleep(0.02)

    def calls(self, cmd):
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                lines = f.read().split("\n")[:-1]
        except FileNotFoundError:
            return []
        return [c for c in map(json.loads, lines) if c.get("cmd") == cmd]

    def followers(self):
        return [c["argv"] for c in self.calls("journalctl") if "--follow" in c["argv"]]

    def saves(self):
        """The VMs managedsave was called for, in order."""
        return [c["dom"].removeprefix("agentbox-") for c in self.calls("managedsave")]

    def paused(self, vm):
        self.until(lambda: vm in self.saves() and self.entries(), f"{vm} not paused")

    def entries(self):
        """What the watcher recorded in Activity."""
        return [e for e in self.journald.entries() if e.get("SYSLOG_IDENTIFIER") == "agentbox-activity"]

    def describe(self, vm):
        code, out, err = self.run_agentbox("describe", vm, "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def hold(self, vm):
        """Hold the VM's lifecycle lock as another agentbox call would; released by os.close."""
        lock = self.path(f"var/lib/agentbox/locks/lifecycle-{vm}.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd


class PauseTest(AlertWatchTest):
    def test_a_strong_alert_pauses_only_its_vm_and_is_recorded(self):
        self.start()
        self.alert("work", "upload.exfil.example", "exfil", "young")
        self.paused("work")
        self.assertEqual(self.saves(), ["work"])
        [entry] = self.entries()
        self.assertEqual({k: entry.get(k) for k in ("AGENTBOX_KIND", "AGENTBOX_ACTION", "AGENTBOX_CODE", "AGENTBOX_VM",
                                                     "AGENTBOX_ACTOR", "AGENTBOX_OUTCOME", "AGENTBOX_DESCRIPTION",
                                                     "AGENTBOX_PARAM_DOMAIN", "AGENTBOX_PARAM_SIGNALS")},
                         {"AGENTBOX_KIND": "alert", "AGENTBOX_ACTION": "alert.paused", "AGENTBOX_CODE": "alert.paused",
                          "AGENTBOX_VM": "work", "AGENTBOX_ACTOR": "alert-watch", "AGENTBOX_OUTCOME": "ok",
                          "AGENTBOX_DESCRIPTION": "Paused for an Alert: upload.exfil.example [exfil,young]",
                          "AGENTBOX_PARAM_DOMAIN": "upload.exfil.example", "AGENTBOX_PARAM_SIGNALS": "exfil, young"})
        # an ordinary Pause, marked as made by the Alert, which describe shows
        work = self.describe("work")
        self.assertEqual(work["state"], "paused")
        self.assertEqual({k: v for k, v in work["paused_by_alert"].items() if k != "time"},
                         {"domain": "upload.exfil.example", "signals": ["exfil", "young"]})
        self.assertRegex(work["paused_by_alert"]["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.describe("priv")["state"], "running")
        self.assertIsNone(self.describe("priv")["paused_by_alert"])
        code, out, _ = self.run_agentbox("describe", "work")
        self.assertIn("work: paused by an Alert: upload.exfil.example [exfil,young] at ", out)

    def test_every_strong_signal_pauses(self):
        self.start()
        names = []
        for i, signal_ in enumerate(STRONG):
            name = f"vm{i}"
            vm_records.write(self.root, vm_records.record(name, 10 + i))
            names.append(name)
            self.alert(name, f"{signal_}.example", signal_)
        self.until(lambda: sorted(self.saves()) == sorted(names), "not every VM paused")

    def test_a_start_resumes_it_and_ends_the_mark(self):
        self.start()
        self.alert("work", "c2.example", "c2")
        self.paused("work")
        self.assertTrue(os.path.exists(self.path("var/lib/agentbox/alert-pause-work.json")))
        self.assertEqual(self.run_agentbox("start", "work"), (0, "work is running\n", ""))
        self.assertFalse(os.path.exists(self.path("var/lib/agentbox/alert-pause-work.json")))
        work = self.describe("work")
        self.assertEqual((work["state"], work["paused_by_alert"]), ("starting", None))     # Orca has not answered yet
        # paused again by hand: an ordinary Pause, not one by an Alert
        self.assertEqual(self.run_agentbox("pause", "work")[0], 0)
        self.assertIsNone(self.describe("work")["paused_by_alert"])

    def test_a_vm_that_is_off_or_paused_already_stays_as_it_is(self):
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off", "managed_save": True}})
        vm_records.write(self.root, vm_records.record("lab", 3))
        self.start()
        self.alert("work", "exfil.example", "exfil")
        self.alert("priv", "exfil.example", "exfil")
        self.alert("lab", "exfil.example", "exfil")             # the sentinel: it runs
        self.paused("lab")
        time.sleep(QUIET)
        self.assertEqual(self.saves(), ["lab"])
        self.assertEqual([e["AGENTBOX_VM"] for e in self.entries()], ["lab"])
        self.assertIsNone(self.describe("priv")["paused_by_alert"])
        self.assertEqual(self.describe("work")["state"], "stopped")

    def test_more_alerts_for_a_vm_pause_it_once(self):
        self.start()
        for _ in range(5):
            self.alert("work", "exfil.example", "exfil")
        self.paused("work")
        time.sleep(QUIET)
        self.assertEqual(self.saves(), ["work"])
        self.assertEqual(len(self.entries()), 1)

    def test_a_vm_that_is_not_registered_is_left_alone(self):
        self.start()
        self.alert("gone", "exfil.example", "exfil")
        self.alert("work", "exfil.example", "exfil")
        self.paused("work")
        time.sleep(QUIET)
        self.assertEqual(self.saves(), ["work"])

    def test_a_failed_pause_is_recorded(self):
        self.scenario(vms={"work": {"managedsave_fails": True}})
        self.start()
        self.alert("work", "exfil.example", "exfil")
        self.until(self.entries, "nothing recorded")
        [entry] = self.entries()
        self.assertEqual((entry["AGENTBOX_ACTION"], entry["AGENTBOX_OUTCOME"], entry["AGENTBOX_ERROR"]),
                         ("alert.paused", "failed", "action_failed"))
        self.assertIn("Pause for an Alert failed: exfil.example [exfil]: error: Failed to save domain",
                      entry["AGENTBOX_DESCRIPTION"])
        self.assertIsNone(self.describe("work")["paused_by_alert"])
        self.assertIn("work: the Pause for an Alert (exfil.example [exfil]) failed", self.stderr())
        # the watcher goes on
        self.alert("priv", "exfil.example", "exfil")
        self.paused("priv")


class WeakSignalTest(AlertWatchTest):
    def test_a_weak_signal_never_pauses(self):
        self.start()
        for signal_ in WEAK:
            self.alert("work", f"{signal_}.example", signal_)
        self.alert("work", "many.example", *WEAK)
        self.alert("priv", "exfil.example", "exfil")            # the sentinel, after the weak ones
        self.paused("priv")
        time.sleep(QUIET)
        self.assertEqual(self.saves(), ["priv"])
        self.assertEqual(self.describe("work")["state"], "running")

    def test_a_strong_signal_among_weak_ones_pauses(self):
        self.start()
        self.alert("work", "fresh.example", "young", "lookalike")
        self.paused("work")


class SettingTest(AlertWatchTest):
    def test_none_changes_nothing_and_is_read_at_every_alert(self):
        self.settings(alert_action="none")
        self.start()
        self.alert("work", "exfil.example", "exfil")
        time.sleep(QUIET)
        self.assertEqual((self.saves(), self.entries()), ([], []))
        self.assertEqual(self.describe("work")["state"], "running")
        # back to pause, without a restart of the watcher
        self.settings(alert_action="pause")
        self.alert("work", "exfil.example", "exfil")
        self.paused("work")

    def test_pause_is_the_default(self):
        self.assertFalse(os.path.exists(self.path("etc/agentbox/settings.json")))
        code, out, _ = self.run_agentbox("settings", "--json")
        self.assertEqual(json.loads(out)["settings"]["alert_action"]["value"], "pause")
        self.start()
        self.alert("work", "exfil.example", "exfil")
        self.paused("work")

    def test_a_broken_settings_file_pauses_as_the_default_does(self):
        with open(self.path("etc/agentbox/settings.json"), "w") as f:
            f.write('{"alert_action": "sometimes"}')
        self.start()
        self.alert("work", "exfil.example", "exfil")
        self.paused("work")
        self.assertIn("alert_action: invalid value sometimes", self.stderr())
        self.assertIn("the VM is paused as with alert_action=pause", self.stderr())


class WriterTest(AlertWatchTest):
    """Only a VM's own proxy can pause it (activity.trusted and the line's own VM)."""

    def test_no_other_writer_can_pause_a_vm(self):
        self.start()
        line = "[work] ALERT exfil.example [exfil]"
        # priv's proxy writes a line for work: under its own identifier, and under work's
        self.line(line, "agentbox-proxy-priv", proxy("priv"))
        self.line(line, "agentbox-proxy-work", proxy("priv"))
        # the proxy's user outside the proxy's unit
        self.line(line, "agentbox-proxy-work", {**proxy("work"), "_SYSTEMD_UNIT": "session-9.scope"})
        # anyone else, and root's CLI, under the proxy's identifier
        self.line(line, "agentbox-proxy-work", FORGED)
        self.line(line, "agentbox-proxy-work", CLI)
        # a line of work's own proxy that is no ALERT
        self.line("[work] BLOCKED exfil.example (suspicious: exfil)", "agentbox-proxy-work", proxy("work"))
        vm_records.write(self.root, vm_records.record("lab", 3))
        self.alert("lab", "exfil.example", "exfil")             # the sentinel
        self.paused("lab")
        time.sleep(QUIET)
        self.assertEqual(self.saves(), ["lab"])
        self.assertEqual(self.describe("work")["state"], "running")
        # those read and left out (journalctl asks only for the proxies' user) are counted, never shown
        self.assertEqual(self.stderr().count("journal entries without a known writer left out: 1"), 2)
        self.assertNotIn("exfil.example", self.stderr())

    def test_it_asks_journalctl_only_for_the_proxies_alert_lines(self):
        self.start()
        [argv] = self.followers()
        self.assertEqual(argv[:-1], ["--namespace=agentbox", "--follow", "--output=json", "--no-pager",
                                     r"--grep=^\[[a-z0-9-]+\] ALERT ", "_UID=962"])
        # from its start on, so a journalctl that ends before any line loses none written meanwhile
        self.assertTrue(argv[-1].startswith("--since=@"), argv)
        self.assertLessEqual(abs(int(argv[-1][len("--since=@"):]) - time.time()), 5)

    def test_an_old_alert_pauses_nothing(self):
        self.read_up_to(1200)       # this run catches up, but only MAX_AGE back
        self.start()
        self.alert("work", "exfil.example", "exfil", age=700)
        self.alert("priv", "exfil.example", "exfil", age=60)
        self.paused("priv")
        time.sleep(QUIET)
        self.assertEqual(self.saves(), ["priv"])


class LockTest(AlertWatchTest):
    def test_the_pause_waits_for_the_lifecycle_lock_and_holds_up_no_other_vm(self):
        fd = self.hold("work")
        try:
            self.start()
            self.alert("work", "exfil.example", "exfil")
            self.alert("priv", "exfil.example", "exfil")
            self.paused("priv")
            time.sleep(QUIET)
            self.assertEqual(self.saves(), ["priv"])
        finally:
            os.close(fd)
        self.until(lambda: "work" in self.saves(), "work not paused once its lock was free")
        self.assertEqual(self.saves(), ["priv", "work"])

    def test_a_vm_stopped_while_the_lock_was_held_stays_off(self):
        fd = self.hold("work")
        try:
            self.start()
            self.alert("work", "exfil.example", "exfil")
            time.sleep(QUIET)
            # what held the lock stopped the VM (a Reset without start, a delete)
            with fake_virsh.changing(self.dir):
                fake_virsh.save("domains.json", {"agentbox-work": {"state": "shut off", "managed_save": False,
                                                                   "defined": True}}, self.dir)
        finally:
            os.close(fd)
        self.alert("priv", "exfil.example", "exfil")            # the sentinel
        self.paused("priv")
        time.sleep(QUIET)
        self.assertEqual(self.saves(), ["priv"])
        self.assertEqual([e["AGENTBOX_VM"] for e in self.entries()], ["priv"])


    def test_an_alert_that_grew_too_old_while_the_lock_was_held_pauses_nothing(self):
        """A Reset or Restart may hold the lock for minutes: its fresh system is not paused for
        the old one's Alert."""
        self.read_up_to(1200)
        fd = self.hold("work")
        try:
            self.start()
            self.alert("work", "exfil.example", "exfil", age=598)
            time.sleep(QUIET + 1)
        finally:
            os.close(fd)
        self.alert("priv", "exfil.example", "exfil")            # the sentinel
        self.paused("priv")
        time.sleep(QUIET)
        self.assertEqual(self.saves(), ["priv"])


class RestartTest(AlertWatchTest):
    def test_a_restarted_watcher_goes_on_after_the_last_alert_it_read(self):
        self.start()
        self.alert("work", "exfil.example", "exfil")
        self.paused("work")
        self.stop()
        self.assertEqual(self.run_agentbox("start", "work")[0], 0)      # the owner resumed it
        self.alert("priv", "exfil.example", "exfil")             # while the watcher was down
        self.start()
        self.paused("priv")
        time.sleep(QUIET)
        self.assertEqual(self.saves(), ["work", "priv"])
        self.assertEqual(self.describe("work")["state"], "starting")      # resumed, not paused again
        self.assertTrue(self.followers()[-1][-1].startswith("--since=@"))

    def test_it_ends_on_sigterm(self):
        self.start()
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=10), 0)

    def test_it_takes_no_arguments(self):
        code, _, err = self.run_agentbox("alert-watch", "work")
        self.assertEqual((code, err), (1, "agentbox: alert-watch (without arguments)\n"))


if __name__ == "__main__":
    unittest.main()
