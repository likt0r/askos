"""`agentbox watch` (the Control Room's event source) and `agentbox describe` (#12, #13, #21).

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and fake virsh,
qemu-img, systemctl, journalctl and dnf on PATH. watch is started like the backend
starts it (stdin and stdout pipes); the tests read its JSON lines, change the fakes'
world (VM state, journal, Build records) and close stdin to end it.

Run from the repo root: python3 -m unittest discover -s tests
"""
import datetime
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_proc  # noqa: E402
import fake_virsh  # noqa: E402
import vm_records  # noqa: E402
from fake_journald import Journald  # noqa: E402
from journal_writers import CONTROL_ROOM, FORGED, proxy, token_service, write_passwd, writer  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(REPO, "bin", "agentbox")
TESTS = os.path.join(REPO, "tests")
DAY = 86400
WAIT = 60                            # seconds a test waits for what has to come: reached only when it does not
TOKEN = "ghs_" + "A" * 36
CLAUDE = "sk-ant-oat01-" + "b" * 40
CODE = "orca://pair?code=Zz9_secret-code"
UNITS = [
    ["agentbox-gui-work@2-12289-192.0.2.8:6801-192.0.2.36:36576.service", "loaded", "active", "running",
     "Orca-Verbindung zur VM work"],
    ["agentbox-gui-work@3-12290-192.0.2.8:6801-192.0.2.36:36590.service", "loaded", "active", "running",
     "Orca-Verbindung zur VM work"],
    ["agentbox-gui-work@4-12291-192.0.2.8:6801-192.0.2.41:40000.service", "loaded", "activating", "start-pre",
     "Orca-Verbindung zur VM work"],
    ["agentbox-gui-priv@5-12292-192.0.2.8:6802-192.0.2.50:1000.service", "loaded", "inactive", "dead",
     "Orca-Verbindung zur VM priv"],
    ["agentbox-ghvm-work@0-1-10.77.1.1:9100-10.77.1.2:4000.service", "loaded", "active", "running", "Token"],
    ["agentbox-proxy-work.service", "loaded", "active", "running", "Allowlist-Proxy fuer VM work"],
    ["agentbox-feeds.timer", "loaded", "active", "waiting", "HaGeZi-Listen"],
    ["ollama.service", "loaded", "failed", "failed", "Ollama Service"],
]


def fake(path, program):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nexec python3 "{os.path.join(TESTS, program)}" "$@"\n')
    os.chmod(path, 0o755)


def usec(t):
    return str(int(t * 1e6))


class WatchTest(unittest.TestCase):
    ENV = {}

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(self.remove_dir)
        self.root = os.path.join(self.dir, "root")
        for sub in ("var/lib/agentbox/vms", "var/lib/agentbox/images", "var/lib/agentbox/builds",
                    "etc/agentbox/secrets", "etc/systemd/system"):
            os.makedirs(self.path(sub))
        for build in ("20261020", "20261101-120000"):
            open(self.path(f"var/lib/agentbox/images/base-{build}.qcow2"), "w").close()
        os.symlink("base-20261101-120000.qcow2", self.path("var/lib/agentbox/images/base.qcow2"))
        for prog, script in (("virsh", "fake_virsh.py"), ("qemu-img", "fake_qemu_img.py"),
                             ("systemctl", "fake_systemctl.py"), ("journalctl", "fake_journalctl.py"),
                             ("dnf", "fake_dnf.py"), ("ss", "fake_ss.py")):
            fake(os.path.join(self.dir, "bin", prog), script)
        vm_records.write(self.root)
        write_passwd(self.root)
        self.journald = Journald(self.path("run/systemd/journal.agentbox/socket"))
        self.addCleanup(self.journald.close)
        self.journal = []
        self.write_journal()
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}}, systemctl_units=UNITS)
        self.disk("work", days=3, backing="/var/lib/agentbox/images/base-20261020.qcow2")
        self.proc = None
        # Orca in the VMs: a local port that refuses connections until orca_answers(), so
        # no check reaches a real VM of the Host
        self.extra = {}
        self.orca_silent()

    def orca_silent(self, port=0):
        """Orca refuses connections (bound, not listening)."""
        if getattr(self, "orca", None):
            port = self.orca.getsockname()[1]
            self.orca_ends()
            self.orca.close()
        self.orca = socket.socket()
        self.orca.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.orca.bind(("127.0.0.1", port))
        self.addCleanup(self.orca.close)
        self.addCleanup(self.orca_ends)
        self.extra["AGENTBOX_ORCA_ADDR"] = "127.0.0.1:%d" % self.orca.getsockname()[1]

    def orca_answers(self):
        """Orca accepts connections and closes them, however many looks and wakes come."""
        orca, ended = self.orca, threading.Event()
        orca.listen(16)
        orca.settimeout(0.05)

        def serve():
            while not ended.is_set():
                try:
                    orca.accept()[0].close()
                except TimeoutError:
                    continue
                except OSError:
                    return
        server = threading.Thread(target=serve, daemon=True)
        server.start()
        self.server = (ended, server)

    def orca_ends(self):
        """Stop accepting (before the socket closes, so the port can be bound again)."""
        ended, server = getattr(self, "server", None) or (None, None)
        if server:
            ended.set()
            server.join()
            self.server = None

    def path(self, rel):
        return os.path.join(self.root, rel)

    def remove_dir(self):
        # watch leaves at once when stdin closes; a fake virsh, systemctl or ss it started
        # just before may still write its call record here (more likely on a busy Host)
        for _ in range(50):
            try:
                shutil.rmtree(self.dir)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.1)
        shutil.rmtree(self.dir)

    def scenario(self, **s):
        """Replaced whole: the fakes that watch starts read it at any moment."""
        tmp = os.path.join(self.dir, "scenario.json.new")
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, os.path.join(self.dir, "scenario.json"))

    def domains(self, change):
        """change(domains) edits fake_virsh's VM states, as a virsh call does: one at a time
        with the calls of watch, a wake or a Restart, the file replaced whole."""
        with fake_virsh.changing(self.dir):
            try:
                with open(os.path.join(self.dir, "domains.json")) as f:
                    domains = json.load(f)
            except FileNotFoundError:
                domains = {}
            change(domains)
            fake_virsh.save("domains.json", domains, self.dir)

    def disk(self, ctx, days, backing):
        p = self.path(f"var/lib/agentbox/vms/{ctx}-system.qcow2")
        with open(p, "w") as f:
            json.dump({"backing-filename": backing}, f)
        t = time.time() - days * DAY
        os.utime(p, (t, t))

    def write_journal(self):
        tmp = os.path.join(self.dir, "journal.json.new")
        with open(tmp, "w") as f:
            json.dump(self.journal, f)
        os.replace(tmp, os.path.join(self.dir, "journal.json"))

    def log(self, message, identifier, namespace="agentbox", by=None, **fields):
        """An entry by the writer agentbox knows for the identifier, or `by` (trusted fields)."""
        e = {"MESSAGE": message, "SYSLOG_IDENTIFIER": identifier, "__REALTIME_TIMESTAMP": usec(time.time()),
             "__CURSOR": f"c{len(self.journal)}", "PRIORITY": "6", "_FAKE_NAMESPACE": namespace,
             **(writer(identifier, fields.get("AGENTBOX_KIND")) if by is None else by), **fields}
        self.journal.append(e)
        self.write_journal()

    def env(self):
        return {"PATH": os.path.join(self.dir, "bin") + ":/usr/bin:/bin", "FAKE_VIRSH_DIR": self.dir,
                "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC",
                "AGENTBOX_WATCH_POLL_MS": "150", "AGENTBOX_WATCH_AGENTS_MS": "150",
                "AGENTBOX_WATCH_RETRY_MS": "200", "AGENTBOX_WATCH_BATCH_MS": "100", **self.ENV, **self.extra}

    def run_agentbox(self, *args):
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=self.env(), timeout=60)
        return r.returncode, r.stdout, r.stderr

    # --- watch -----------------------------------------------------------------

    def start(self, **env):
        self.err = open(os.path.join(self.dir, "watch.err"), "w")
        self.addCleanup(self.err.close)
        proc = self.proc = subprocess.Popen([AGENTBOX, "watch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=self.err, env={**self.env(), **env}, text=True)
        self.addCleanup(self.stop)
        lines = self.lines = queue.Queue()
        raw = self.raw = []
        self.kept = []               # events read while event() waited for another name, oldest first

        def read():
            # into this watch's own queue: the test may have started the next watch by the
            # time the last lines and the end of this one are read
            while True:
                try:
                    line = proc.stdout.readline()
                except ValueError:   # closed by stop()
                    break
                if not line:
                    break
                raw.append(line)
                lines.put(json.loads(line))
            lines.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.proc:
            self.reader.join(timeout=10)             # it has read to the end of watch's output
            self.proc.stdout.close()

    def event(self, name, check=lambda d: True, timeout=WAIT):
        """The next event of this name whose data passes check. Events of other names stay for
        a later event() or events(): watch sends from several threads (the poll, each journal
        follower, the Build lines), so the order between names is not fixed."""
        deadline = time.monotonic() + timeout
        while True:
            e = self.next(name, deadline)
            if check(e["data"]):
                return e["data"]

    def next(self, name, deadline):
        """The oldest event of this name that no event() or events() gave out or passed."""
        for i, e in enumerate(self.kept):
            if e["event"] == name:
                return self.kept.pop(i)
        while True:
            try:
                e = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                self.fail(f"no {name} event; stderr: {self.stderr()}")
            if e is None:
                self.fail(f"watch ended; stderr: {self.stderr()}")
            self.assertEqual(set(e), {"event", "time", "data"})
            if e["event"] == name:
                return e
            self.kept.append(e)

    def events(self, seconds=0):
        """Every event no event() gave out or passed, those read by now and those of the next
        seconds, in the order watch sent them."""
        out, self.kept, deadline = self.kept, [], time.monotonic() + seconds
        while True:
            try:
                e = self.lines.get(timeout=max(0, min(0.05, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    return out
                continue
            if e is not None:
                out.append(e)

    def until(self, found, what):
        """Wait until found() is true and give its value; fails with `what` after WAIT seconds."""
        deadline = time.monotonic() + WAIT
        while True:
            value = found()
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail(f"{what}; stderr: {self.stderr()}")
            time.sleep(0.01)

    def followers(self):
        """Wait for the journal followers, which start after the first poll and report only
        what is written after them (--lines=0)."""
        self.until(lambda: len([c for c in self.calls("journalctl") if "--follow" in c["argv"]]) >= 2,
                   "no journal followers")

    def stderr(self):
        try:
            with open(os.path.join(self.dir, "watch.err")) as f:
                return f.read()
        except FileNotFoundError:    # no watch was started
            return ""

    def calls(self, cmd):
        """The calls of `cmd` the fakes recorded by now (a line still being written is none yet)."""
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                lines = f.read().split("\n")[:-1]
        except FileNotFoundError:
            return []
        return [c for c in map(json.loads, lines) if c.get("cmd") == cmd]


class SnapshotTest(WatchTest):
    def test_first_the_vms_then_the_services(self):
        self.start()
        snap = self.event("vms")
        self.assertEqual([v["name"] for v in snap["vms"]], ["work", "priv"])
        work, priv = snap["vms"]
        self.assertEqual((work["state"], work["libvirt_state"]), ("running", "running"))
        self.assertEqual((priv["state"], priv["libvirt_state"]), ("paused", "shut off"))
        self.assertEqual(work["image"], "base")
        self.assertEqual((work["build_pin"], work["image_build"], work["next_build"]),
                         (None, "20261101-120000", "20261101-120000"))
        self.assertEqual({k: work["system_disk"][k] for k in ("age_days", "file", "image", "build")},
                         {"age_days": 3, "file": "base-20261020.qcow2", "image": "base", "build": "20261020"})
        self.assertTrue(work["reset_changes_build"])
        self.assertEqual((work["memory_mib"], work["orca_port"], work["address"]), (26624, 6801, "10.77.1.2"))
        self.assertEqual(work["devices"], [{"address": "192.0.2.36", "connections": 2},
                                           {"address": "192.0.2.41", "connections": 1}])
        self.assertEqual(priv["devices"], [])
        self.assertEqual(priv["agents"], {})
        self.assertEqual(snap["problems"], [])
        event = self.event("services")
        sent = [json.loads(line)["event"] for line in self.raw]
        self.assertLess(sent.index("vms"), sent.index("services"))
        self.assertEqual(event["warnings"], [])
        services = event["units"]
        self.assertEqual([s["unit"] for s in services],
                         ["agentbox-feeds.timer", "agentbox-proxy-work.service", "ollama.service"])
        self.assertEqual(services[2], {"unit": "ollama.service", "load": "loaded", "active": "failed",
                                       "sub": "failed", "description": "Ollama Service", "triggered_by": []})

    def test_services_name_the_timer_or_socket_that_starts_them(self):
        feeds = ["agentbox-feeds.service", "loaded", "inactive", "dead", "HaGeZi-Listen holen"]
        socket = ["agentbox-proxy-work.socket", "loaded", "active", "running", "Allowlist-Proxy fuer VM work"]
        triggered = {"agentbox-feeds.service": ["agentbox-feeds.timer"],
                     "agentbox-proxy-work.service": ["agentbox-proxy-work.socket"]}
        vms = {"priv": {"state": "shut off", "managed_save": True}}
        self.scenario(vms=vms, systemctl_units=[*UNITS, feeds, socket], systemctl_triggered_by=triggered)
        self.start()
        services = {s["unit"]: s["triggered_by"] for s in self.event("services")["units"]}
        self.assertEqual(services, {"agentbox-feeds.service": ["agentbox-feeds.timer"], "agentbox-feeds.timer": [],
                                    "agentbox-proxy-work.service": ["agentbox-proxy-work.socket"],
                                    "agentbox-proxy-work.socket": [], "ollama.service": []})
        self.events(0.6)
        # asked once about the units shown (not the per-connection instances), not on every poll
        shows = [c["argv"] for c in self.calls("systemctl") if c["argv"][0] == "show"]
        self.assertEqual(shows, [["show", "--no-pager", "--property=Id,TriggeredBy", "--", *sorted(services)]])
        # a timer that comes is a change of the units: asked again
        rebuild = [["agentbox-maintenance.service", "loaded", "inactive", "dead", "Maintenance window"],
                   ["agentbox-maintenance.timer", "loaded", "active", "waiting", "Maintenance window daily"]]
        self.scenario(vms=vms, systemctl_units=[*UNITS, feeds, socket, *rebuild],
                      systemctl_triggered_by={**triggered,
                                              "agentbox-maintenance.service": ["agentbox-maintenance.timer"]})
        units = self.event("services", lambda d: any(s["triggered_by"] for s in d["units"]
                                                     if s["unit"] == "agentbox-maintenance.service"))["units"]
        self.assertEqual([s["triggered_by"] for s in units if s["unit"] == "agentbox-maintenance.timer"], [[]])

    def test_services_say_when_the_maintenance_window_s_timer_is_missing(self):
        # an update from before the Maintenance window, before regenerate: the old Reset timer is
        # there, the window's is not
        for name in ("agentbox-weekly.timer", "agentbox-feeds.timer"):
            open(self.path("etc/systemd/system/" + name), "w").close()
        self.start()
        [warning] = self.event("services")["warnings"]
        self.assertEqual(warning["code"], "maintenance_timer_missing")
        self.assertIn("sudo agentbox regenerate creates it", warning["message"])
        # regenerate wrote it: the warning goes, though no unit listed changed
        open(self.path("etc/systemd/system/agentbox-maintenance.timer"), "w").close()
        self.assertEqual(self.event("services", lambda d: not d["warnings"])["warnings"], [])

    def test_triggers_unknown_when_systemctl_show_fails(self):
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}}, systemctl_units=UNITS,
                      systemctl_fails=["show"])
        self.start()
        units = self.event("services")["units"]
        self.assertEqual([(s["unit"], s["triggered_by"]) for s in units],
                         [("agentbox-feeds.timer", None), ("agentbox-proxy-work.service", None),
                          ("ollama.service", None)])

    def test_state_changes_become_vm_events(self):
        self.start()
        self.event("vms")
        self.domains(lambda d: d.update({"agentbox-work": {"state": "shut off"}}))
        work = self.event("vm", lambda v: v["name"] == "work" and v["state"] == "stopped")
        self.assertEqual(work["agents"], {})
        # nothing changes: no more events about it
        later = [e for e in self.events(0.8) if e["event"] in ("vm", "vms")]
        self.assertEqual(later, [])

    def test_a_new_vm_sends_the_whole_list_again(self):
        self.start()
        self.event("vms")
        vm_records.write(self.root, vm_records.record("lab", 3, image="base", build="20261020"))
        snap = self.event("vms", lambda d: len(d["vms"]) == 3)
        lab = snap["vms"][2]
        self.assertEqual((lab["name"], lab["build_pin"], lab["next_build"]), ("lab", "20261020", "20261020"))

    def test_broken_records_are_problems(self):
        with open(self.path("etc/agentbox/vms/bad.json"), "w") as f:
            f.write("{")
        self.start()
        snap = self.event("vms")
        self.assertEqual([p["vm"] for p in snap["problems"]], ["bad"])

    def test_devices_unknown_when_systemctl_fails(self):
        self.scenario(systemctl_fails=["list-units"])
        self.start()
        self.assertEqual([v["devices"] for v in self.event("vms")["vms"]], [None, None])


class JournalTest(WatchTest):
    def setUp(self):
        super().setUp()
        self.start()
        self.event("vms")
        self.followers()

    def test_followers(self):
        argv = sorted(c["argv"] for c in self.calls("journalctl"))
        self.assertEqual(argv, [
            ["--follow", "--output=json", "--no-pager", "--unit=agentbox-build@*.service", "--lines=0"],
            ["--namespace=agentbox", "--follow", "--output=json", "--no-pager", "--lines=0"]])

    def test_activity(self):
        self.log("work reset ok (Firefox/Linux (192.0.2.20)): Reset", "agentbox-activity", AGENTBOX_KIND="action",
                 AGENTBOX_ACTION="reset", AGENTBOX_VM="work", AGENTBOX_OUTCOME="ok",
                 AGENTBOX_ACTOR="Firefox/Linux (192.0.2.20)", AGENTBOX_DESCRIPTION="Reset")
        a = self.event("activity")
        self.assertEqual({k: a[k] for k in ("vm", "actor", "kind", "action", "outcome", "description")},
                         {"vm": "work", "actor": "Firefox/Linux (192.0.2.20)", "kind": "action", "action": "reset",
                          "outcome": "ok", "description": "Reset"})
        self.assertRegex(a["time"], r"^\d{4}-\d\d-\d\dT")

    def test_alert(self):
        self.log("[work] ALLOWED github.com:443", "agentbox-proxy-work")
        self.log("[work] ALERT evil-paste.example:443 [paste,young]", "agentbox-proxy-work")
        alert = self.event("alert")
        self.assertEqual({k: alert[k] for k in ("vm", "domain", "signals")},
                         {"vm": "work", "domain": "evil-paste.example:443", "signals": ["paste", "young"]})
        self.assertEqual(self.event("activity")["kind"], "alert")

    def test_github_tokens(self):
        self.log("[work] token for likt0r/askos until 2026-11-03T13:00:00Z", "agentbox-gh-work")
        self.log("[work] refused: 'evil/repo': owner-not-allowed: evil is not a GitHub owner of the VM work",
                 "agentbox-gh-work")
        issued = self.event("github-token")
        self.assertEqual((issued["action"], issued["repository"]), ("issued", "likt0r/askos"))
        refused = self.event("github-token")
        self.assertEqual({k: refused[k] for k in ("vm", "action", "outcome", "request", "reason", "reason_code")},
                         {"vm": "work", "action": "refused", "outcome": "failed", "request": "evil/repo",
                          "reason": "evil is not a GitHub owner of the VM work", "reason_code": "owner-not-allowed"})
        # and as Activity entries, with the code and parameters the Control Room words
        entry = self.event("activity", lambda d: d.get("action") == "github.refused")
        self.assertEqual((entry["code"], entry["params"], entry["reason"]),
                         ("github.refused", {"request": "evil/repo", "repository": "evil/repo",
                                             "reason": "owner-not-allowed"}, "owner-not-allowed"))

    def test_only_known_writers_become_events(self):
        # a sign-in not from the Control Room, the proxy and token service of another VM,
        # the Control Room beyond sign-ins: counted, never sent
        self.log("x", "agentbox-activity", by=FORGED, AGENTBOX_KIND="signin", AGENTBOX_ACTION="forged")
        self.log("[work] ALERT forged.example [exfil]", "agentbox-proxy-work", by=proxy("priv"))
        self.log("[work] token for forged/repo until 2026-11-03T13:00:00Z", "agentbox-gh-work", by=FORGED)
        self.log("[work] refused: 'forged/x': invalid-request: no", "agentbox-gh-work", by=token_service("priv"))
        self.log("x", "agentbox-activity", by=CONTROL_ROOM, AGENTBOX_KIND="allowlist", AGENTBOX_ACTION="allow")
        self.log("y", "agentbox-activity", AGENTBOX_KIND="signin", AGENTBOX_ACTION="signin")
        self.log("z", "agentbox-activity", AGENTBOX_KIND="action", AGENTBOX_ACTION="real")
        events = []
        while not events or events[-1]["data"].get("action") != "real":
            events.append({"event": "activity", "data": self.event("activity")})
        self.assertEqual([e["data"]["action"] for e in events], ["signin", "real"])
        self.assertEqual([e["event"] for e in self.events(0.5) if e["event"] in ("alert", "github-token")], [])
        # one warning per poll, with the count only
        deadline = time.monotonic() + 10
        while sum(int(n) for n in re.findall(r"left out: (\d+)", self.stderr())) < 5:
            self.assertLess(time.monotonic(), deadline, self.stderr())
            time.sleep(0.05)
        self.assertRegex(self.stderr(), r"\A(agentbox watch: journal entries without a known writer "
                                        r"left out: \d\n)+\Z")
        self.assertNotIn("forged", "".join(self.raw))

    def test_service_users_are_read_again_when_they_change(self):
        with open(self.path("etc/passwd"), "w") as f:
            f.write("root:x:0:0:root:/root:/bin/bash\n")          # the proxies' user does not exist yet
        self.log("[work] ALERT early.example [exfil]", "agentbox-proxy-work")
        deadline = time.monotonic() + 10
        while "left out: 1\n" not in self.stderr():
            self.assertLess(time.monotonic(), deadline, self.stderr())
            time.sleep(0.05)
        write_passwd(self.root)
        self.log("[work] ALERT later.example [exfil]", "agentbox-proxy-work")
        self.assertEqual(self.event("alert")["domain"], "later.example")

    def test_entries_of_the_default_journal_are_not_activity(self):
        self.log("x", "agentbox-activity", namespace=None, AGENTBOX_KIND="action", AGENTBOX_ACTION="old")
        self.log("y", "agentbox-activity", AGENTBOX_KIND="action", AGENTBOX_ACTION="new")
        self.assertEqual(self.event("activity")["action"], "new")

    def test_secrets_never_leave(self):
        self.log("x", "agentbox-activity", AGENTBOX_KIND="credentials", AGENTBOX_ACTION="set-claude-token",
                 AGENTBOX_DESCRIPTION=f"Token {CLAUDE} und {TOKEN} und sk-Oc12AbCdEf_34-ghiJKLmn")
        self.log(f"[work] refused: '{TOKEN}': invalid-request: {CODE}", "agentbox-gh-work")
        self.log(f"Pairing: {CODE}", "agentbox", namespace=None, _SYSTEMD_UNIT="agentbox-build@base.service")
        self.event("activity")
        self.event("github-token")
        self.event("build-log")
        out = "".join(self.raw)
        for secret in (CLAUDE, TOKEN, "Zz9_secret-code", "Oc12AbCdEf"):
            self.assertNotIn(secret, out)
        self.assertIn("[hidden]", out)

    def test_follower_restarts_after_the_last_entry(self):
        self.log("a", "agentbox-activity", AGENTBOX_KIND="action", AGENTBOX_ACTION="first")
        self.event("activity")
        # the follower ends (journald restarted) and comes back after its last entry
        followers = [c for c in self.calls("journalctl") if "--namespace=agentbox" in c["argv"]]
        self.assertEqual(len(followers), 1)
        subprocess.run(["pkill", "-f", "-P", str(self.proc.pid), "fake_journalctl.py.*--namespace"])
        self.log("b", "agentbox-activity", AGENTBOX_KIND="action", AGENTBOX_ACTION="second")
        self.assertEqual(self.event("activity")["action"], "second")
        again = [c for c in self.calls("journalctl") if "--namespace=agentbox" in c["argv"]]
        self.assertEqual(again[-1]["argv"][-1], "--after-cursor=c0")

    def test_watch_records_no_activity(self):
        self.log("a", "agentbox-activity", AGENTBOX_KIND="action", AGENTBOX_ACTION="first")
        self.event("activity")
        self.assertEqual(self.journald.entries(), [])


class BuildTest(WatchTest):
    def record(self, **fields):
        rec = {"image": "base", "build": "20261103-120000", "status": "running", "step": "build",
               "started": int(time.time()), "finished": None, "release": "0.1.0",
               "base_build": None, "package_min_age_days": 7, "unit": "agentbox-build@all.service",
               "actor": "Firefox/Linux (192.0.2.20)", "agents": None, "problems": [], "error": None,
               "safeguards": None, "container": False, **fields}
        path = self.path(f"var/lib/agentbox/builds/{rec['image']}-{rec['build']}.json")
        with open(path + ".new", "w") as f:
            json.dump(rec, f)
        os.replace(path + ".new", path)

    def test_progress_lines_and_outcome(self):
        self.record(build="20261020", status="ok", step="done")       # before watch: not an event
        self.start()
        self.event("vms")
        self.record()
        b = self.event("build")
        self.assertEqual((b["image"], b["build"], b["status"], b["step"]), ("base", "20261103-120000", "running",
                                                                           "build"))
        self.assertEqual((b["release"], b["commit"]), ("0.1.0", None))      # a commit only from before #96
        self.followers()            # the event may come from the first poll, before them
        for i in range(3):
            self.log(f"STEP {i}/9: RUN dnf install", "agentbox", namespace=None,
                     _SYSTEMD_UNIT="agentbox-build@all.service")
        lines = []
        while len(lines) < 3:
            batch = self.event("build-log")
            self.assertEqual((batch["image"], batch["build"], batch["target"]), ("base", "20261103-120000", "all"))
            lines += batch["entries"]
        self.assertEqual([l["message"] for l in lines], [f"STEP {i}/9: RUN dnf install" for i in range(3)])
        self.assertEqual(batch["cursor"], lines[-1]["cursor"])
        self.record(status="rejected", step="check", finished=int(time.time()), error="Schutzmassnahme fehlt",
                    problems=["npm: ignore-scripts fehlt"])
        b = self.event("build", lambda b: b["status"] != "running")
        self.assertEqual((b["status"], b["problems"]), ("rejected", ["npm: ignore-scripts fehlt"]))
        self.assertNotIn("20261020", [e["data"].get("build") for e in self.events(0.5) if e["event"] == "build"])


class RebootTest(WatchTest):
    def test_reboot_needed(self):
        self.scenario(needs_restarting={"exit": 1, "stdout": "Core libraries or services have been updated "
                                                             "since boot-up:\n  * kernel-core\n  * glibc\n\n"
                                                             "Reboot is required to fully utilize these updates."})
        self.start()
        r = self.event("host-reboot")
        self.assertEqual((r["needed"], r["packages"]), (True, ["kernel-core", "glibc"]))
        self.assertEqual(self.calls("dnf")[0]["argv"], ["-q", "--disablerepo=*", "needs-restarting"])

    def test_not_needed_and_unknown(self):
        self.start()
        self.assertEqual(self.event("host-reboot")["needed"], False)
        self.stop()
        self.scenario(needs_restarting={"exit": 3, "stdout": "Error"})
        self.start()
        self.assertEqual(self.event("host-reboot")["needed"], None)
        self.stop()
        # dnf's own errors also end with 1 (on titan: no metadata cache for -C)
        self.scenario(needs_restarting={"exit": 1, "stdout": "Cache-only enabled but no cache for 'fedora'"})
        self.start()
        self.assertEqual(self.event("host-reboot")["needed"], None)


class LifetimeTest(WatchTest):
    def test_ends_when_stdin_closes_and_takes_its_followers(self):
        self.start()
        self.event("vms")
        time.sleep(0.5)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)
        deadline = time.monotonic() + 10
        while subprocess.run(["pgrep", "-f", f"fake_journalctl.py.*{self.dir}"], capture_output=True).stdout:
            self.assertLess(time.monotonic(), deadline, "journal followers left behind")
            time.sleep(0.1)

    def test_no_arguments(self):
        code, _, err = self.run_agentbox("watch", "extra")
        self.assertEqual((code, err), (1, "agentbox: watch (without arguments)\n"))
        # with --json the same failure as one JSON object
        code, _, err = self.run_agentbox("watch", "--json")
        self.assertEqual((code, json.loads(err)), (1, {"error": "invalid_argument", "params": {},
                                                       "message": "watch (without arguments)"}))


class DescribeTest(WatchTest):
    def test_when_the_claude_token_and_the_opencode_key_were_stored(self):
        """null without one (a placeholder or no file), the file's time with one."""
        secrets = self.path("etc/agentbox/secrets")
        os.makedirs(secrets, exist_ok=True)
        for name, text in (("claude-work", "none\n"), ("opencode-work", "sk-Oc12AbCdEf_34-ghiJKLmn\n")):
            with open(os.path.join(secrets, name), "w") as f:
                f.write(text)
        os.utime(os.path.join(secrets, "opencode-work"), (1760000000, 1760000000))
        code, out, err = self.run_agentbox("describe", "--json")
        self.assertEqual(code, 0, err)
        found = {v["name"]: (v["claude_token_since"], v["opencode_key_since"]) for v in json.loads(out)["vms"]}
        self.assertEqual(found, {"work": (None, "2025-10-09T08:53:20Z"), "priv": (None, None)})
        self.assertNotIn("Oc12AbCdEf", out)

    def test_one_vm_as_json(self):
        code, out, err = self.run_agentbox("describe", "work", "--json")
        self.assertEqual((code, err), (0, ""))
        work = json.loads(out)
        self.assertEqual((work["name"], work["state"], work["devices"][0]["address"]), ("work", "running",
                                                                                        "192.0.2.36"))
        # running without an Observer: agents unknown
        self.assertIsNone(work["agents"])

    def test_the_vm_variables_of_the_record(self):
        vm_records.write(self.root, vm_records.record("work", 1, env={"DATABASE_URL": "postgres://db/x", "A": ""}),
                         vm_records.record("priv", 2))
        code, out, err = self.run_agentbox("describe", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual({v["name"]: v["env"] for v in json.loads(out)["vms"]},
                         {"work": {"DATABASE_URL": "postgres://db/x", "A": ""}, "priv": {}})
        self.assertIn("  VM variables: DATABASE_URL, A (agentbox env work)\n", self.run_agentbox("describe")[1])

    def test_a_newer_build_makes_a_reset_due_with_auto_update(self):
        """work's System disk is 3 days old, from an earlier Build of its Image: with auto_update
        (#114) the daily Reset check would move it, so a Reset is due; without, only its age counts."""
        def due():
            code, out, err = self.run_agentbox("describe", "--json")
            self.assertEqual(code, 0, err)
            return {v["name"]: v["reset_due"] for v in json.loads(out)["vms"]}
        self.assertTrue(due()["work"])
        os.makedirs(self.path("etc/agentbox"), exist_ok=True)
        with open(self.path("etc/agentbox/settings.json"), "w") as f:
            f.write('{"auto_update": false}')
        self.assertFalse(due()["work"])
        with open(self.path("etc/agentbox/settings.json"), "w") as f:
            f.write('{"auto_update": false, "reset_age_days": 3}')
        self.assertTrue(due()["work"])

    def test_every_vm_as_json_and_text(self):
        code, out, _ = self.run_agentbox("describe", "--json")
        self.assertEqual([v["name"] for v in json.loads(out)["vms"]], ["work", "priv"])
        code, out, _ = self.run_agentbox("describe")
        self.assertEqual(code, 0)
        self.assertIn("work: running\n  Image:        base (current Build: 20261101-120000)\n"
                      "  System disk:  base-20261020.qcow2, 3 days old, the next Reset brings 20261101-120000\n",
                      out)

    def test_the_container_image_cleanup(self):
        with open(self.path("etc/agentbox/vms/priv.json")) as f:
            rec = json.load(f)
        rec.update(container_image_cleanup=False, container_image_cleanup_days=30)
        with open(self.path("etc/agentbox/vms/priv.json"), "w") as f:
            json.dump(rec, f)
        code, out, err = self.run_agentbox("describe", "--json")
        self.assertEqual(code, 0, err)
        vms = {v["name"]: v for v in json.loads(out)["vms"]}
        self.assertEqual([(vms[n]["container_image_cleanup"], vms[n]["container_image_cleanup_days"])
                          for n in ("work", "priv")], [(True, 7), (False, 30)])
        code, out, err = self.run_agentbox("describe")
        self.assertIn("work: running", out)
        self.assertIn("  Container image cleanup: after 7 days\n", out)
        self.assertIn("  Container image cleanup: off (30 days)\n", out)

    def test_unknown_vm(self):
        self.assertEqual(self.run_agentbox("describe", "nope"), (1, "", "agentbox: No such VM: nope\n"))
        code, out, err = self.run_agentbox("describe", "nope", "--json")
        self.assertEqual((code, out, json.loads(err)), (1, "", {"error": "vm_not_found", "message": "No such VM: nope",
                                                                "params": {"vm": "nope"}}))

    def test_status_stays_as_it_was(self):
        code, out, _ = self.run_agentbox("status", "--json")
        self.assertEqual(set(json.loads(out)["work"]), {"state", "rss_kb", "base", "system_disk_since", "agents",
                                                        "agents_unknown", "open_until", "claude_token_since",
                                                        "gui_connections"})


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(iso_time):
    return datetime.datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()


NONE = {"count": 0, "last": None, "address": None}
SILENT = "agentbox: work: Orca in the VM does not answer\n"


class Hold:
    """One program's calls of a virsh command, each waiting for the test (FAKE_VIRSH_HOLD in
    fake_virsh.py): env is for that program, reached(n) says that it has come to its nth
    such call, go(n) lets that call go on, go() every call from now on."""

    def __init__(self, directory, command):
        os.makedirs(directory)
        self.dir = directory
        # virsh's own time limit does not end a call the test holds
        self.env = {"FAKE_VIRSH_HOLD": f"{command}:{directory}", "AGENTBOX_VIRSH_TIMEOUT": str(WAIT)}

    def reached(self, n):
        return os.path.exists(os.path.join(self.dir, str(n)))

    def go(self, n=None):
        open(os.path.join(self.dir, "go" if n is None else f"{n}.go"), "w").close()


class RespondingTest(WatchTest):
    """A VM libvirt reports as running whose Orca does not answer (#54): `starting` after
    its start, `not_responding` after the wake time limit, what answers, failed wakes.

    The programs here (watch, wakes, a Restart) run at the same time and take as long to
    start as the Host is busy, so no test relies on one program being faster than another:
    where one has to act before another, the other's next call of virsh waits for the test
    (hold), and what watch is to find within a time limit happens as watch asks for it
    (work_starts). Time limits that only have to be over are short, those that must not run
    out are long, and how long something took is read from the times in the wakes file and
    the fakes' records, not from the test's clock. What still rests on time: watch's first
    look at a started VM within its 2 s limit (work_starts), its first round within the 4 s
    of test_watch_looks_again_when_the_time_limit_of_a_start_runs_out, a held guest-ping,
    which the production gives up after 6 s, a wake ended within those 6 s
    (test_a_wake_ended_before_it_recorded_the_incident_leaves_it_to_the_next), and the 12 s a
    wake may take from its guest-ping to the wakes file
    (test_a_hanging_guest_agent_holds_up_the_wakes_file_only_briefly)."""

    def qemu(self, vm, ago):
        """The VM's QEMU process, started `ago` seconds ago; its start (whole seconds)."""
        started = int(time.time()) - ago
        fake_proc.qemu(self.root, vm, started)
        return started

    def describe(self, vm="work"):
        code, out, err = self.run_agentbox("describe", vm, "--json")
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)

    def wake(self, address, vm="work"):
        return self.run_agentbox(f"--actor=Orca ({address})", "wake", vm)

    def hold(self, command):
        """A Hold of this virsh command for one program; whatever still waits goes on when
        the test ends."""
        self.holds = getattr(self, "holds", 0) + 1
        hold = Hold(os.path.join(self.dir, f"hold-{self.holds}"), command)
        self.addCleanup(hold.go)
        return hold

    def in_background(self, *args, **env):
        """agentbox with these arguments, running while the test goes on; wait() gives (exit
        code, stderr)."""
        p = subprocess.Popen([AGENTBOX, *args], env={**self.env(), **env}, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())

        def wait():
            _, err = p.communicate(timeout=WAIT)
            return p.returncode, err
        wait.process = p
        return wait

    def wake_in_background(self, address, vm="work", held=False, **env):
        """A wake that waits while the test goes on; wait() gives (exit code, stderr). held:
        when it gives up, it stops before its look at the VM (a wake asks virsh for the VM's
        state twice: before it waits for Orca, and when it gave up, before it counts its
        failure): wait.gave_up() waits until it is there, having waited its whole time limit,
        and wait.go() lets it go on. So the test says what happens before the wake's
        failure is counted, without a time limit long enough for it."""
        hold = self.hold("domstate") if held else None
        if held:
            hold.go(1)
            env = {**hold.env, **env}
        limit = float({**self.env(), **env}.get("AGENTBOX_WAKE_SECONDS", "0"))
        began = time.monotonic()
        wait = self.in_background(f"--actor=Orca ({address})", "wake", vm, **env)
        if held:
            def gave_up():
                self.until(lambda: hold.reached(2), "the wake does not give up")
                # the second look comes after the whole limit: no other look of the wake's took its place
                self.assertGreaterEqual(time.monotonic() - began, limit)
            wait.gave_up = gave_up
            wait.go = lambda: hold.go(2)
        return wait

    def vm_starts(self, vm="work"):
        """Wait until libvirt has started the VM's QEMU process (for a wake in the background)."""
        self.until(lambda: os.path.exists(self.path(f"run/libvirt/qemu/agentbox-{vm}.pid")), "the VM is not started")

    def observer(self, vm="work"):
        """An Observer of the VM: a look that finds Orca answering then asks it for the agents,
        a call fake_orca.py records, so that looks() counts these looks too."""
        fake(self.path("opt/Orca/resources/bin/orca-ide"), "fake_orca.py")
        os.makedirs(self.path(f"var/lib/agentbox/observer-{vm}"))

    def looks(self):
        """How many of watch's looks at a running VM have left a call by now: one that finds
        Orca silent asks the guest agent, one that finds it answering asks the Observer."""
        return len(self.calls("guest-ping")) + len(self.calls("orca"))

    def more_looks(self, n=2):
        """Wait until watch has looked n more times, from beginning to end. Its looks come one
        after the other and each leaves its call on the way (looks()): the first call from
        now on may be of a look that began before, and a look is over once the next has left
        its call."""
        seen = self.looks()
        self.until(lambda: self.looks() >= seen + n + 2, "watch does not look")

    def polled(self):
        """Wait until watch has polled once more, from beginning to end, so that what its looks
        found before is sent (a poll begins with systemctl list-units)."""
        def polls():
            return len([c for c in self.calls("systemctl") if c["argv"][0] == "list-units"])
        seen = polls()
        self.until(lambda: polls() >= seen + 2, "watch does not poll")

    def wakes(self):
        """work's wakes file as it is written."""
        with open(self.wakes_file()) as f:
            return json.load(f)

    def clock(self):
        """Now, on the clock agentbox counts on (responding.clock)."""
        return time.clock_gettime(time.CLOCK_BOOTTIME)

    def shown(self, iso_time):
        """A time agentbox shows (whole seconds on the wall clock, via the boot time of the
        fake /proc), on clock()."""
        with open(self.path("proc/stat")) as f:
            boot = int(re.search(r"^btime (\d+)$", f.read(), re.MULTILINE).group(1))
        return epoch(iso_time) - boot

    def began(self):
        """When work's QEMU process started, on clock(), from the start named in the wakes file."""
        return int(self.wakes()["start"].rpartition(":")[2]) / os.sysconf("SC_CLK_TCK")

    def silent_until_recorded(self):
        """Seconds from the first failure known to the incident's Activity entry, as the wakes
        file has them: silent_since, and the file's own time (it is written right after the
        entry, and not again while the VM stays silent)."""
        written = os.stat(self.wakes_file()).st_mtime
        known = self.wakes()
        self.assertIs(known["recorded"], True)
        return (self.clock() - known["silent_since"]) - (time.time() - written)

    def failed_wakes(self):
        return self.describe()["failed_wakes"]

    def actions(self):
        return [e["AGENTBOX_ACTION"] for e in self.journald.entries()]

    def recorded(self):
        return [(e["AGENTBOX_ACTION"], e["AGENTBOX_ACTOR"], e["AGENTBOX_OUTCOME"]) for e in self.journald.entries()]

    def answering(self, v):
        return v["name"] == "work" and (v["responding"] or {}).get("orca") is True

    def silent(self, v):
        return v["name"] == "work" and (v["responding"] or {}).get("orca") is False

    def silent_orca(self):
        """Another Orca address that refuses connections (for a wake while watch's Orca answers)."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.addCleanup(s.close)
        return "127.0.0.1:%d" % s.getsockname()[1]

    def wakes_file(self, vm="work"):
        return self.path(f"var/lib/agentbox/wakes-{vm}.json")

    def is_vm(self, state):
        return lambda v: v["name"] == "work" and v["state"] == state

    # --- describe: one look ---------------------------------------------------------

    def test_a_vm_just_started_is_starting_until_its_orca_answers(self):
        started = self.qemu("work", 10)
        work = self.describe()
        self.assertEqual((work["state"], work["libvirt_state"]), ("starting", "running"))
        self.assertEqual(work["responding"], {"guest_agent": True, "orca": False, "since": iso(started)})
        self.assertIsNone(work["agents"])
        self.orca_answers()
        work = self.describe()
        self.assertEqual((work["state"], work["responding"]),
                         ("running", {"guest_agent": None, "orca": True, "since": None}))
        # the guest agent is asked only while Orca does not answer
        self.assertEqual(len(self.calls("guest-ping")), 1)
        self.assertEqual(self.calls("guest-ping")[0]["argv"], ["--timeout", "3"])

    def test_not_responding_after_the_time_limit_system_up_orca_not(self):
        """Orca silent since the start, as watch found while the VM started and after its time
        limit: describe shows it, with what answers."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        since = self.start_work()["responding"]["since"]
        self.event("vm", self.is_vm("not_responding"))
        self.stop()
        work = self.describe()
        self.assertEqual(work["state"], "not_responding")
        self.assertEqual(work["responding"], {"guest_agent": True, "orca": False, "since": since})
        self.assertEqual(work["failed_wakes"], NONE)
        code, out, _ = self.run_agentbox("describe", "work")
        self.assertIn(f"work: not_responding\n  Answers:      Orca does not answer (since {since}), "
                      "guest agent answers\n", out)

    def test_not_responding_after_the_time_limit_system_not_booted(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        since = self.start_work(agent_down=True)["responding"]["since"]
        self.event("vm", self.is_vm("not_responding"))
        self.stop()
        work = self.describe()
        self.assertEqual(work["state"], "not_responding")
        self.assertEqual(work["responding"], {"guest_agent": False, "orca": False, "since": since})

    def test_one_failed_look_at_a_vm_nobody_watched_is_not_enough(self):
        """A VM that ran before anyone watched it (before an update, or its wakes file is gone)
        and fails one look (under load) is not taken for not responding: its Orca may well
        have answered before."""
        self.qemu("work", 300)
        work = self.describe()
        self.assertEqual((work["state"], work["responding"]["orca"]), ("running", False))

    def test_a_vm_that_is_not_running_is_not_asked(self):
        priv = self.describe("priv")
        self.assertEqual((priv["state"], priv["responding"], priv["failed_wakes"]), ("paused", None, NONE))
        self.assertEqual(self.calls("guest-ping"), [])

    def test_a_vm_whose_orca_answered_is_not_starting_again(self):
        self.qemu("work", 10)
        self.orca_answers()
        self.assertEqual(self.wake("192.0.2.36"), (0, "", ""))
        self.orca_silent()
        work = self.describe()
        self.assertEqual((work["state"], work["responding"]["orca"]), ("running", False))

    def test_describe_takes_a_wake_that_gave_up_as_orca_silent_for_the_time_limit(self):
        """describe looks once: after a wake reached Orca, one failed look proves nothing,
        but a wake that waited the whole time limit in vain does."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.qemu("work", 300)
        self.orca_answers()
        self.assertEqual(self.wake("192.0.2.36"), (0, "", ""))
        self.orca_silent()
        self.assertEqual(self.describe()["state"], "running")
        called = self.clock()
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        ended = self.clock()
        work = self.describe()
        self.assertEqual((work["state"], work["responding"]["orca"], work["failed_wakes"]["count"]),
                         ("not_responding", False, 1))
        # since the wake began to wait (shown on the wall clock via the boot time, to a second):
        # after it was called, and the time limit before it ended
        since = self.shown(work["responding"]["since"])
        self.assertGreater(since, called - 1)
        self.assertLessEqual(since, ended - 2)

    def test_only_libvirts_qemu_process_counts(self):
        """Any local user can name a process like the VM's QEMU process; the start comes from
        the process in libvirt's pid file."""
        self.qemu("work", 300)
        impostor = time.time() - 5
        fake_proc.impostor(self.root, "work", impostor)
        work = self.describe()
        # an impostor's start would make it `starting` (since its start)
        self.assertEqual(work["state"], "running")
        self.assertGreater(epoch(work["responding"]["since"]), impostor + 3)

    # --- watch: looks every few seconds and remembers ---------------------------------

    def start_watch(self):
        """watch, with its guest-pings held from the second on: after its first look that finds
        Orca silent, the next one ends only after go() of the Hold given back (or when the
        production gives a guest-ping up, after 6 s), when the test has seen what the first one
        made of the VM."""
        pings = self.hold("qemu-agent-command")
        pings.go(1)
        self.start(**pings.env)
        return pings

    def work_starts(self, **scenario):
        """watch runs (start_watch), then work starts. watch looks at it within any time limit,
        however busy the Host: the VM starts at the moment watch asks libvirt for its state
        (start_waits), and it is the only VM, so that watch's look follows without another
        program to start."""
        os.unlink(self.path("etc/agentbox/vms/priv.json"))
        self.scenario(vms={"work": {"state": "shut off", "start_waits": True}}, systemctl_units=UNITS, **scenario)
        pings = self.start_watch()
        self.event("vms")
        self.extra["AGENTBOX_VIRSH_TIMEOUT"] = str(WAIT)          # the start waits for watch's next poll
        self.assertEqual(self.run_agentbox("start", "work")[0], 0)
        return pings

    def start_work(self, **scenario):
        """watch runs, then work starts (work_starts): its first object once Orca was asked."""
        pings = self.work_starts(**scenario)
        work = self.event("vm", lambda v: self.is_vm("starting")(v) and v["responding"] is not None)
        pings.go()
        return work

    def test_watch_starting_then_running(self):
        work = self.start_work()
        self.assertEqual((work["responding"]["orca"], work["responding"]["guest_agent"]), (False, True))
        self.assertIsNone(work["agents"])
        self.orca_answers()
        work = self.event("vm", self.is_vm("running"))
        self.assertEqual(work["responding"], {"guest_agent": None, "orca": True, "since": None})

    def test_watch_starting_then_not_responding_system_up(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        starting = self.start_work()
        work = self.event("vm", self.is_vm("not_responding"))
        self.assertEqual((work["responding"]["orca"], work["responding"]["guest_agent"]), (False, True))
        # not answering since the start
        self.assertEqual(work["responding"]["since"], starting["responding"]["since"])

    def test_watch_starting_then_not_responding_system_not_booted(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.start_work(agent_down=True)
        work = self.event("vm", self.is_vm("not_responding"))
        self.assertEqual((work["responding"]["orca"], work["responding"]["guest_agent"]), (False, False))

    def test_watch_one_failed_check_does_not_make_a_vm_not_responding(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.qemu("work", 300)
        self.orca_answers()
        pings = self.start_watch()
        self.assertEqual(self.event("vms")["vms"][0]["state"], "running")
        self.event("vm", self.answering)
        self.orca_silent()
        work = self.event("vm", self.silent)
        self.assertEqual(work["state"], "running")
        pings.go()
        self.event("vm", self.is_vm("not_responding"))
        # only once Orca was silent for the time limit, from the first failed check
        self.assertGreater(self.silent_until_recorded(), 1.9)

    def test_watch_after_a_restart_the_vm_is_starting_again(self):
        """What watch knows of a VM's answers belongs to one start: after a Restart between two
        looks at Orca the VM is `starting`, not `running` on the earlier start's answer."""
        self.extra["AGENTBOX_WATCH_AGENTS_MS"] = "600000"         # no second round of looks in this test
        self.qemu("work", 300)
        self.orca_answers()
        self.start()
        self.event("vms")
        self.event("vm", lambda v: self.is_vm("running")(v) and self.answering(v))
        self.assertEqual(self.run_agentbox("restart", "work")[0], 0)
        work = self.event("vm", self.is_vm("starting"))
        self.assertEqual((work["responding"], work["agents"]), (None, None))

    def test_watch_needs_a_failed_check_after_the_time_limit(self):
        """A slow round of checks (several Orca CLIs that hang) does not let the time limit run
        out on one failed check: the next check has to fail too."""
        self.extra.update(AGENTBOX_WAKE_SECONDS="1", AGENTBOX_WATCH_AGENTS_MS="2000")
        self.qemu("work", 300)
        self.orca_answers()
        pings = self.start_watch()
        self.event("vm", self.answering)
        self.orca_silent()
        work = self.event("vm", self.silent)
        self.assertEqual(work["state"], "running")
        pings.go()
        # the time limit runs out before the next check, 2 s after the first: not responding
        # only with that one, which asked the guest agent as the first did
        self.event("vm", self.is_vm("not_responding"))
        self.assertGreaterEqual(len(self.calls("guest-ping")), 2)
        self.assertGreater(self.silent_until_recorded(), 1.9)

    def test_watch_takes_a_wake_that_reached_orca_before_its_own_look(self):
        """Before watch looked at a start (its first objects), a wake that reached Orca
        decides: a VM started moments ago whose Orca answered a wake is running, not starting."""
        self.qemu("work", 10)
        self.orca_answers()
        self.assertEqual(self.wake("192.0.2.36"), (0, "", ""))
        self.start()
        [work] = [v for v in self.event("vms")["vms"] if v["name"] == "work"]
        self.assertEqual((work["state"], work["responding"]), ("running", None))

    def test_watch_looks_again_when_the_time_limit_of_a_start_runs_out(self):
        """A VM whose Orca comes up late in the time limit is not shown as not responding
        until the next round of looks: watch looks again when the limit runs out."""
        # no next round in this test; and watch's first look (with its guest-ping) is over
        # well before the time limit, or watch would leave the VM to that next round
        self.extra.update(AGENTBOX_WAKE_SECONDS="4", AGENTBOX_WATCH_AGENTS_MS="600000")
        pings = self.work_starts()
        self.until(lambda: self.calls("guest-ping"), "watch does not look at the VM that starts")
        self.orca_answers()
        pings.go()
        work = self.event("vm", lambda v: v["name"] == "work" and v["state"] != "starting")
        self.assertEqual((work["state"], work["responding"]["orca"]), ("running", True))
        # found answering by the look when the limit ran out
        self.assertGreaterEqual(self.wakes()["answered"] - self.began(), 4)

    def test_watch_shows_a_wake_that_gave_up(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        self.start()
        self.event("vms")
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        work = self.event("vm", lambda v: v["name"] == "work" and v["failed_wakes"]["count"] == 1)
        self.assertEqual(work["failed_wakes"]["address"], "192.0.2.36")

    def test_watch_looks_at_once_at_a_vm_that_starts(self):
        """A VM resumed from a Pause with Start (no wake) is looked at right away, not after the
        next round of looks: its Orca answers at once, so it is running, not starting."""
        self.extra["AGENTBOX_WATCH_AGENTS_MS"] = "600000"         # the next round of looks: not in this test
        self.orca_answers()
        self.start()
        self.event("vms")
        self.assertEqual(self.run_agentbox("start", "priv")[0], 0)
        priv = self.event("vm", lambda v: v["name"] == "priv" and v["state"] == "running")
        self.assertEqual(priv["responding"], {"guest_agent": None, "orca": True, "since": None})

    def test_watch_keeps_what_it_knows_while_libvirt_suspends_a_vm(self):
        """A moment outside running (suspended by libvirt, unknown while libvirtd restarts) is
        the same start: what watch knew of its answers stays."""
        self.extra.update(AGENTBOX_WAKE_SECONDS="3", AGENTBOX_WATCH_AGENTS_MS="200")
        self.qemu("work", 300)
        self.orca_answers()
        self.assertEqual(self.wake("192.0.2.36"), (0, "", ""))
        self.orca_silent()
        self.start()
        self.event("vms")
        before = self.event("vm", self.is_vm("not_responding"))
        self.domains(lambda d: d.setdefault("agentbox-work", {}).update(state="paused"))
        self.event("vm", self.is_vm("suspended"))
        time.sleep(1)
        self.domains(lambda d: d["agentbox-work"].update(state="running"))
        # as before, with what watch's last look found, before it looks again
        work = self.event("vm", lambda v: v["name"] == "work" and v["state"] != "suspended")
        self.assertEqual((work["state"], work["responding"]), ("not_responding", before["responding"]))
        # and after more looks at it
        self.more_looks()
        self.polled()
        self.assertNotIn("running", [e["data"]["state"] for e in self.events()
                                     if e["event"] == "vm" and e["data"]["name"] == "work"])

    def test_watch_takes_a_wake_that_gave_up_before_its_own_look(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        called = self.clock()
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        ended = self.clock()
        self.start()
        [work] = [v for v in self.event("vms")["vms"] if v["name"] == "work"]
        self.assertEqual((work["state"], work["responding"]["guest_agent"], work["responding"]["orca"]),
                         ("not_responding", None, False))
        # silent since the wake began to wait (after it was called, the time limit before it
        # ended): nobody looked at the VM before
        since = self.shown(work["responding"]["since"])
        self.assertGreater(since, called - 1)
        self.assertLessEqual(since, ended - 1)

    # --- failed wakes -------------------------------------------------------------------

    def test_failed_wakes_count_since_the_start(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        called = time.time()
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        ended = time.time()
        work = self.describe()
        self.assertEqual(work["state"], "not_responding")
        wakes = work["failed_wakes"]
        self.assertEqual((wakes["count"], wakes["address"]), (2, "192.0.2.41"))
        # when the last one gave up (to a second): the time limit after it was called, before it ended
        self.assertGreaterEqual(epoch(wakes["last"]), int(called) + 1)
        self.assertLessEqual(epoch(wakes["last"]), ended)
        code, out, _ = self.run_agentbox("describe", "work")
        self.assertIn(f"  Wakes:        2 failed, last {wakes['last']} (192.0.2.41)\n", out)
        # a new start of the VM (another QEMU process, e.g. at the Host's boot) counts afresh
        self.qemu("work", 5)
        self.assertEqual(self.failed_wakes(), NONE)

    def test_a_successful_wake_start_restart_reset_and_pause_clear_them(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)

        def fails():
            self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
            self.assertEqual(self.failed_wakes()["count"], 1)

        fails()
        self.orca_answers()
        self.assertEqual(self.wake("192.0.2.36"), (0, "", ""))
        self.assertEqual(self.failed_wakes(), NONE)
        self.orca_silent()
        # Reset without a start and Pause leave the VM off, where the last start's count would
        # show; after a Restart the new start alone would hide it
        for command in (["restart", "work"], ["reset", "work", "--no-start"], ["pause", "work"]):
            with self.subTest(command=command):
                self.qemu("work", 300)            # the VM ran long enough to count as not answering
                fails()
                code, _, err = self.run_agentbox(*command)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.failed_wakes(), NONE)
                self.assertFalse(os.path.exists(self.wakes_file()))
                self.assertEqual(self.run_agentbox("start", "work")[0], 0)
        # a VM that stopped by itself keeps them until it is started again
        self.qemu("work", 300)
        fails()
        self.domains(lambda d: d.update({"agentbox-work": {"state": "shut off"}}))
        fake_proc.gone(self.root, "work")
        self.assertEqual(self.failed_wakes()["count"], 1)
        self.assertEqual(self.run_agentbox("start", "work")[0], 0)
        self.assertEqual(self.failed_wakes(), NONE)
        self.assertFalse(os.path.exists(self.wakes_file()))

    def test_wakes_at_the_same_time_are_all_counted_with_one_entry(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        waiting = [self.wake_in_background(f"192.0.2.{n}") for n in range(30, 36)]
        self.assertEqual([w() for w in waiting], [(1, SILENT)] * 6)
        self.assertEqual(self.failed_wakes()["count"], 6)
        self.assertEqual(self.actions(), ["wake.not-responding"])

    def hung(self):
        """work runs for 300 s without answering and ignores a shutdown; the incident's entry
        is recorded."""
        self.extra.update(AGENTBOX_WAKE_SECONDS="1", AGENTBOX_SHUTDOWN_SECONDS="1")
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}, "work": {"shutdown": "ignored"}},
                      systemctl_units=UNITS)
        self.qemu("work", 300)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        self.assertEqual(self.actions(), ["wake.not-responding"])

    def shutting_down(self, *command):
        """A wake of the hung VM that gives up while this Restart or Reset shuts the VM down:
        after the shutdown was asked for, and before the VM is forced off (the destroy waits
        for the wake to end). The command's (exit code, stderr)."""
        self.hung()
        waiting = self.wake_in_background("192.0.2.41", held=True)
        waiting.gave_up()                         # it is past the lifecycle lock, which the command takes
        destroy = self.hold("destroy")
        command = self.in_background(*command, **destroy.env)
        self.until(lambda: self.calls("shutdown"), "no shutdown")
        waiting.go()
        self.assertEqual(waiting(), (1, SILENT))
        destroy.go(1)
        return command()

    def test_a_wake_that_gives_up_while_a_restart_shuts_the_vm_down_is_no_new_incident(self):
        code, err = self.shutting_down("restart", "work")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.actions(), ["wake.not-responding", "restart"])
        self.assertEqual(self.failed_wakes(), NONE)

    def test_a_reset_without_start_clears_a_wake_that_gave_up_while_the_vm_shut_down(self):
        code, err = self.shutting_down("reset", "work", "--no-start")
        self.assertEqual(code, 0, err)
        work = self.describe()
        self.assertEqual((work["state"], work["failed_wakes"]), ("stopped", NONE))
        self.assertEqual(self.actions(), ["wake.not-responding", "reset"])

    def test_a_wake_that_waited_through_a_restart_counts_for_neither_start(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        waiting = self.wake_in_background("192.0.2.41", held=True)
        # it waited for the start the Restart ends, and counts its failure only after the Restart
        waiting.gave_up()
        self.assertEqual(self.run_agentbox("restart", "work")[0], 0)
        waiting.go()
        self.assertEqual(waiting(), (1, SILENT))
        self.assertEqual(self.failed_wakes(), NONE)
        self.assertEqual(self.actions(), ["restart"])

    def test_a_wake_that_started_the_vm_is_recorded_when_the_vm_stops_while_it_waits(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off", "managed_save": True}},
                      systemctl_units=UNITS)
        waiting = self.wake_in_background("192.0.2.41", held=True)
        self.vm_starts()
        self.assertEqual(len(self.calls("start")), 1)
        # the guest powers off while the wake waits for Orca (after its start's call), before
        # the wake looks at the VM again
        self.domains(lambda d: d["agentbox-work"].update(state="shut off"))
        fake_proc.gone(self.root, "work")
        waiting.go()
        self.assertEqual(waiting(), (1, SILENT))
        [e] = self.journald.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_ACTOR"]),
                         ("wake", "failed", "Orca (192.0.2.41)"))
        self.assertEqual(self.failed_wakes(), NONE)

    def test_a_clock_step_changes_neither_the_start_nor_the_state(self):
        """The wall clock set after the VM's start (chrony right after the Host's boot) moves
        the boot time in /proc/stat, so the start's wall-clock time; neither which start it is
        nor how long the VM has run."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        fake_proc.step_clock(self.root, 3600)
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        work = self.describe()
        self.assertEqual((work["state"], work["failed_wakes"]["count"]), ("not_responding", 2))
        self.assertEqual(self.actions(), ["wake.not-responding"])

    def test_a_wake_ended_before_it_recorded_the_incident_leaves_it_to_the_next(self):
        """systemd ends a wake after 180 s (lifecycle lock and waiting for Orca together), maybe
        while it asks the guest agent what does not answer: the next wake that gives up records
        the incident instead."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}}, systemctl_units=UNITS,
                      agent_hangs=8)
        self.qemu("work", 300)
        waiting = self.wake_in_background("192.0.2.36")
        self.until(lambda: self.calls("guest-ping"), "the wake does not ask the guest agent")
        waiting.process.terminate()
        waiting()
        self.assertEqual(self.actions(), [])     # ended before it recorded the incident
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}}, systemctl_units=UNITS)
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        self.assertEqual(self.actions(), ["wake.not-responding"])
        self.assertEqual(self.failed_wakes()["count"], 2)

    def test_a_wake_that_started_the_vm_has_its_own_entry_when_another_recorded_the_incident(self):
        """Wakes of several connections while the VM was off: the one that started it gives up
        after another one recorded the incident. Its own entry says who caused the start."""
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off", "managed_save": True}},
                      systemctl_units=UNITS)
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        first = self.wake_in_background("192.0.2.36", held=True)
        self.vm_starts()
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        first.go()                                # its failure is counted after the other's
        self.assertEqual(first(), (1, SILENT))
        self.assertEqual(len(self.calls("start")), 1)
        self.assertEqual(self.recorded(), [("wake.not-responding", "Orca (192.0.2.41)", "failed"),
                                           ("wake", "Orca (192.0.2.36)", "failed")])
        self.assertEqual(self.failed_wakes()["count"], 2)

    def test_a_wake_that_started_the_vm_has_its_own_entry_when_watch_recorded_the_incident(self):
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off", "managed_save": True}},
                      systemctl_units=UNITS)
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.start()
        self.event("vms")
        waiting = self.wake_in_background("192.0.2.36", held=True)
        # watch notices first: the wake's failure is counted after that
        self.event("vm", self.is_vm("not_responding"))
        waiting.go()
        self.assertEqual(waiting(), (1, SILENT))
        self.assertEqual(self.recorded(), [("wake.not-responding", "watch", "failed"),
                                           ("wake", "Orca (192.0.2.36)", "failed")])

    def test_a_hanging_guest_agent_holds_up_the_wakes_file_only_briefly(self):
        """The guest-ping that says what does not answer runs under the wakes file's lock: a
        guest agent or libvirtd that hangs is given up after a few seconds, not virsh's 15 s,
        so other wakes and watch wait no longer."""
        # virsh's limit is 40 s here, so that a busy Host does not make the few seconds look like it
        self.extra.update(AGENTBOX_WAKE_SECONDS="1", AGENTBOX_VIRSH_TIMEOUT="40")
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}}, systemctl_units=UNITS,
                      agent_hangs=120)
        self.qemu("work", 300)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        # from the guest-ping to the wakes file, written right after the incident's entry: 6 s
        [ping] = self.calls("guest-ping")
        # (virsh's own limit is 15 s)
        self.assertLess(os.stat(self.wakes_file()).st_mtime - ping["time"], 12)
        [e] = self.journald.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_PARAM_REASON"]), ("wake.not-responding", "system-not-booted"))

    def test_a_wake_that_gave_up_while_another_reached_orca_is_no_incident(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        waiting = self.wake_in_background("192.0.2.36", held=True)
        waiting.gave_up()
        # another connection's wake reaches Orca meanwhile (its own Orca address answers),
        # before the first one's failure is counted
        other = socket.socket()
        other.bind(("127.0.0.1", 0))
        other.listen(4)
        self.addCleanup(other.close)
        r = subprocess.run([AGENTBOX, "--actor=Orca (192.0.2.41)", "wake", "work"], capture_output=True,
                           text=True, timeout=60,
                           env={**self.env(), "AGENTBOX_ORCA_ADDR": "127.0.0.1:%d" % other.getsockname()[1]})
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        waiting.go()
        self.assertEqual(waiting(), (1, SILENT))
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.failed_wakes(), NONE)

    def test_a_wake_records_its_failure_when_the_wakes_file_cannot_be_written(self):
        """Bookkeeping that fails does not decide the Activity: every wake that gives up is a
        failed `wake` entry then, as before the wakes file."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        os.makedirs(os.path.join(self.wakes_file(), "x"))
        for address in ("192.0.2.36", "192.0.2.41"):
            code, _, err = self.wake(address)
            self.assertEqual(code, 1)
            self.assertTrue(err.endswith(SILENT), err)
        self.assertEqual([(e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"]) for e in self.journald.entries()],
                         [("wake", "failed")] * 2)

    def test_a_start_by_a_wake_clears_the_failed_wakes_of_the_last_start(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))

        def powers_off():
            self.domains(lambda d: d.update({"agentbox-work": {"state": "shut off"}}))
            fake_proc.gone(self.root, "work")
        powers_off()
        self.assertEqual(self.failed_wakes()["count"], 1)
        # a wake starts it again (and is ended, systemd's time limit); it powers off again
        self.extra["AGENTBOX_WAKE_SECONDS"] = "30"
        waiting = self.wake_in_background("192.0.2.41")
        self.vm_starts()
        waiting.process.kill()
        waiting()
        powers_off()
        self.assertEqual(self.failed_wakes(), NONE)

    def test_a_running_vm_whose_start_is_unknown_takes_no_record_of_a_known_start(self):
        """libvirt reports the VM running, but its pid file cannot be read: what the wakes
        found for a known start (times on another boot's clock, its incident's entry) is not
        taken for it."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        fake_proc.gone(self.root, "work")         # libvirt still reports it running
        self.assertEqual(self.failed_wakes(), NONE)
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        self.assertEqual(self.failed_wakes()["count"], 1)
        self.assertEqual(self.actions(), ["wake.not-responding", "wake.not-responding"])
        # stopped, it shows what the wakes of its last start found
        self.domains(lambda d: d.update({"agentbox-work": {"state": "shut off"}}))
        self.assertEqual(self.failed_wakes()["address"], "192.0.2.41")

    def test_bookkeeping_that_cannot_be_cleared_does_not_stop_a_restart(self):
        os.makedirs(os.path.join(self.wakes_file(), "x"))
        code, _, err = self.run_agentbox("restart", "work")
        self.assertEqual(code, 0, err)
        self.assertIn("wakes-work.json", err)

    def test_one_activity_entry_per_incident(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        for address in ("192.0.2.36", "192.0.2.41", "192.0.2.36"):
            self.assertEqual(self.wake(address), (1, "", SILENT))
        [e] = self.journald.entries()
        self.assertEqual({k: e[k] for k in ("AGENTBOX_KIND", "AGENTBOX_ACTION", "AGENTBOX_VM", "AGENTBOX_ACTOR",
                                            "AGENTBOX_OUTCOME", "AGENTBOX_PARAM_REASON", "AGENTBOX_DESCRIPTION")},
                         {"AGENTBOX_KIND": "action", "AGENTBOX_ACTION": "wake.not-responding", "AGENTBOX_VM": "work",
                          "AGENTBOX_ACTOR": "Orca (192.0.2.36)", "AGENTBOX_OUTCOME": "failed",
                          "AGENTBOX_PARAM_REASON": "orca-not-answering",
                          "AGENTBOX_DESCRIPTION": "The VM is not responding: Orca does not answer (the guest agent "
                                                  "answers)"})
        # Orca answers again (a wake of a running VM is no entry); the next failure is a new incident
        self.orca_answers()
        self.assertEqual(self.wake("192.0.2.36"), (0, "", ""))
        self.orca_silent()
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}}, systemctl_units=UNITS,
                      agent_down=True)
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        entries = self.journald.entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual((entries[1]["AGENTBOX_PARAM_REASON"], entries[1]["AGENTBOX_DESCRIPTION"]),
                         ("system-not-booted", "The VM is not responding: its system did not boot (neither Orca nor "
                                               "the guest agent answers)"))

    def test_a_wake_that_started_the_vm_and_gave_up_is_one_entry(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off", "managed_save": True}},
                      systemctl_units=UNITS)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        [e] = self.journald.entries()
        self.assertEqual(e["AGENTBOX_ACTION"], "wake.not-responding")
        self.assertEqual(self.failed_wakes()["count"], 1)

    # --- what watch finds: kept for describe, a watch started later and the wakes ---------

    def test_describe_and_a_new_watch_know_that_orca_answered(self):
        """watch keeps that Orca answered in this start: for describe and a watch started later,
        one failed look at a VM that answered is not enough either. (The VM started within the
        time limit: without that answer it would be `starting`. No look of this test is as
        long after another as the time limit.)"""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "600"
        self.qemu("work", 300)
        self.orca_answers()
        self.start()
        self.event("vm", self.answering)
        self.stop()
        self.orca_silent()
        work = self.describe()
        self.assertEqual((work["state"], work["responding"]["orca"]), ("running", False))
        self.start()
        [work] = [v for v in self.event("vms")["vms"] if v["name"] == "work"]
        self.assertEqual(work["state"], "running")
        self.assertEqual(self.event("vm", self.silent)["state"], "running")
        self.assertEqual(self.actions(), [])

    def test_describe_and_a_new_watch_show_the_incident_watch_found(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.qemu("work", 300)
        self.orca_answers()
        self.start()
        self.event("vm", self.answering)
        self.orca_silent()
        stream = self.event("vm", self.is_vm("not_responding"))["responding"]
        work = self.describe()
        self.assertEqual((work["state"], work["responding"]["since"]), ("not_responding", stream["since"]))
        # a watch started now shows it before its first look
        self.stop()
        self.start()
        [work] = [v for v in self.event("vms")["vms"] if v["name"] == "work"]
        self.assertEqual((work["state"], work["responding"]["since"]), ("not_responding", stream["since"]))

    def test_a_wake_that_gave_up_while_watch_saw_orca_answer_is_no_incident(self):
        """watch's looks are answers the wakes know of: a wake that waited the whole time limit
        in vain while Orca answered watch is not counted (as when another wake reached it)."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.qemu("work", 300)
        self.observer()
        self.orca_answers()
        self.start()
        self.event("vm", self.answering)
        waiting = self.wake_in_background("192.0.2.36", held=True, AGENTBOX_ORCA_ADDR=self.silent_orca())
        waiting.gave_up()
        # watch looks once more: it notes that Orca still answers half the time limit after
        # it last did, so by now after the wake began to wait
        self.more_looks(1)
        waiting.go()
        self.assertEqual(waiting(), (1, SILENT))
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.failed_wakes(), NONE)

    def test_watch_writes_the_wakes_file_only_with_news(self):
        """Not on every look: when Orca first answers in a start, then at most every half time
        limit while it keeps answering."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "120"
        self.qemu("work", 300)
        self.observer()
        self.orca_answers()
        self.start()
        self.event("vm", self.answering)
        written = os.stat(self.wakes_file())
        self.more_looks(5)
        now = os.stat(self.wakes_file())
        self.assertEqual((now.st_ino, now.st_mtime_ns), (written.st_ino, written.st_mtime_ns))

    # --- the incident's entry: from a wake or from watch, once ----------------------------

    def test_watch_takes_one_failed_look_at_a_vm_it_did_not_see_start_for_nothing(self):
        """After an update every running VM lacks a wakes file: a first look that fails once (a
        VM under load) makes it neither not responding nor an entry. (It ran for longer than
        the time limit, which does not run out between the looks of this test.)"""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "60"
        self.qemu("work", 300)
        self.start()
        self.assertEqual(self.event("vm", self.silent)["state"], "running")
        self.orca_answers()
        self.event("vm", self.answering)
        self.assertEqual(self.actions(), [])

    def test_watch_records_a_vm_that_stops_responding_while_nobody_connects(self):
        """A VM that hangs while no Orca client connects (after a night-time Reset, an Orca
        crash) gets its entry from watch, once however long it stays so."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.start_work(agent_down=True)
        self.event("vm", self.is_vm("not_responding"))
        self.more_looks()
        self.assertEqual(self.actions(), ["start", "wake.not-responding"])
        e = self.journald.entries()[1]
        self.assertEqual({k: e[k] for k in ("AGENTBOX_KIND", "AGENTBOX_VM", "AGENTBOX_ACTOR", "AGENTBOX_OUTCOME",
                                            "AGENTBOX_PARAM_REASON", "AGENTBOX_DESCRIPTION")},
                         {"AGENTBOX_KIND": "action", "AGENTBOX_VM": "work", "AGENTBOX_ACTOR": "watch",
                          "AGENTBOX_OUTCOME": "failed", "AGENTBOX_PARAM_REASON": "system-not-booted",
                          "AGENTBOX_DESCRIPTION": "The VM is not responding: its system did not boot (neither Orca "
                                                  "nor the guest agent answers)"})

    def test_watch_ends_an_incident_when_orca_answers_it(self):
        """Orca answers watch again: the incident is over without a wake, and the next silence
        is a new one, with its own entry and its own since."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.qemu("work", 300)
        self.start()
        first = self.event("vm", self.is_vm("not_responding"))["responding"]
        self.orca_answers()
        self.event("vm", self.is_vm("running"))
        self.orca_silent()
        second = self.event("vm", self.is_vm("not_responding"))["responding"]
        self.assertGreater(epoch(second["since"]), epoch(first["since"]))
        self.assertEqual(self.describe()["responding"]["since"], second["since"])
        self.assertEqual(self.recorded(), [("wake.not-responding", "watch", "failed")] * 2)
        self.assertEqual(self.journald.entries()[1]["AGENTBOX_PARAM_REASON"], "orca-not-answering")

    def test_one_entry_when_watch_notices_first(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        self.start()
        self.event("vm", self.is_vm("not_responding"))
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        self.assertEqual(self.recorded(), [("wake.not-responding", "watch", "failed")])
        self.assertEqual(self.failed_wakes()["count"], 1)

    def test_one_entry_when_a_wake_notices_first(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        self.start()
        self.event("vm", lambda v: self.silent(v) and v["responding"]["guest_agent"] is True)
        self.more_looks()
        self.assertEqual(self.recorded(), [("wake.not-responding", "Orca (192.0.2.36)", "failed")])

    def test_wakes_and_watch_at_the_same_time_record_one_entry(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        waiting = [self.wake_in_background(f"192.0.2.{n}") for n in range(30, 36)]
        self.start()
        self.assertEqual([w() for w in waiting], [(1, SILENT)] * 6)
        self.event("vm", self.is_vm("not_responding"))
        self.more_looks()
        self.assertEqual(self.actions(), ["wake.not-responding"])
        self.assertEqual(self.failed_wakes()["count"], 6)

    def test_watch_records_nothing_when_the_wakes_file_cannot_be_written(self):
        """Without the wakes file watch could not tell that it recorded the incident already:
        it records nothing and says why, once."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        os.makedirs(os.path.join(self.wakes_file(), "x"))
        self.start()
        self.event("vm", self.is_vm("not_responding"))
        self.more_looks()
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.stderr().count("wakes-work.json"), 1, self.stderr())


if __name__ == "__main__":
    unittest.main()
