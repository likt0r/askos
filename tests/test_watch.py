"""`agentbox watch` (the Control Room's event source) and `agentbox describe` (#12, #13, #21).

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and fake virsh,
qemu-img, systemctl, journalctl and dnf on PATH. watch is started like the backend
starts it (stdin and stdout pipes); the tests read its JSON lines, change the fakes'
world (VM state, journal, Build records) and close stdin to end it.

Run from the repo root: python3 -m unittest discover -s tests
"""
import datetime
import fcntl
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
        with open(os.path.join(self.dir, "scenario.json"), "w") as f:
            json.dump(s, f)

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

    def start(self):
        self.err = open(os.path.join(self.dir, "watch.err"), "w")
        self.addCleanup(self.err.close)
        self.proc = subprocess.Popen([AGENTBOX, "watch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.err, env=self.env(), text=True)
        self.addCleanup(self.stop)
        self.lines = queue.Queue()
        self.raw = []

        def read():
            for line in self.proc.stdout:
                self.raw.append(line)
                self.lines.put(json.loads(line))
            self.lines.put(None)
        threading.Thread(target=read, daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.proc:
            self.proc.stdout.close()

    def event(self, name, check=lambda d: True, timeout=10):
        """The next event of this name whose data passes check."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                e = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                self.fail(f"no {name} event; stderr: {self.stderr()}")
            if e is None:
                self.fail(f"watch ended; stderr: {self.stderr()}")
            self.assertEqual(set(e), {"event", "time", "data"})
            if e["event"] == name and check(e["data"]):
                return e["data"]

    def events(self, seconds):
        """Every event in the next seconds."""
        out, deadline = [], time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                e = self.lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if e is not None:
                out.append(e)
        return out

    def followers(self):
        """Wait for the journal followers, which start after the first poll and report only
        what is written after them (--lines=0)."""
        deadline = time.monotonic() + 10
        while len([c for c in self.calls("journalctl") if "--follow" in c["argv"]]) < 2:
            self.assertLess(time.monotonic(), deadline, "no journal followers")
            time.sleep(0.05)

    def stderr(self):
        with open(os.path.join(self.dir, "watch.err")) as f:
            return f.read()

    def calls(self, cmd):
        with open(os.path.join(self.dir, "calls.jsonl")) as f:
            return [c for c in map(json.loads, f) if c.get("cmd") == cmd]


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
        services = self.event("services")["units"]
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
        rebuild = [["agentbox-rebuild.service", "loaded", "inactive", "dead", "Neu bauen"],
                   ["agentbox-rebuild.timer", "loaded", "active", "waiting", "Woechentlich neu bauen"]]
        self.scenario(vms=vms, systemctl_units=[*UNITS, feeds, socket, *rebuild],
                      systemctl_triggered_by={**triggered, "agentbox-rebuild.service": ["agentbox-rebuild.timer"]})
        units = self.event("services", lambda d: any(s["triggered_by"] for s in d["units"]
                                                     if s["unit"] == "agentbox-rebuild.service"))["units"]
        self.assertEqual([s["triggered_by"] for s in units if s["unit"] == "agentbox-rebuild.timer"], [[]])

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
                 AGENTBOX_DESCRIPTION=f"Token {CLAUDE} und {TOKEN}")
        self.log(f"[work] refused: '{TOKEN}': invalid-request: {CODE}", "agentbox-gh-work")
        self.log(f"Pairing: {CODE}", "agentbox", namespace=None, _SYSTEMD_UNIT="agentbox-build@base.service")
        self.event("activity")
        self.event("github-token")
        self.event("build-log")
        out = "".join(self.raw)
        for secret in (CLAUDE, TOKEN, "Zz9_secret-code"):
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
    def test_one_vm_as_json(self):
        code, out, err = self.run_agentbox("describe", "work", "--json")
        self.assertEqual((code, err), (0, ""))
        work = json.loads(out)
        self.assertEqual((work["name"], work["state"], work["devices"][0]["address"]), ("work", "running",
                                                                                        "192.0.2.36"))
        # running without an Observer: agents unknown
        self.assertIsNone(work["agents"])

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


class RespondingTest(WatchTest):
    """A VM libvirt reports as running whose Orca does not answer (#54): `starting` after
    its start, `not_responding` after the wake time limit, what answers, failed wakes."""

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

    def wake_in_background(self, address, vm="work"):
        """A wake that waits while the test goes on; wait() gives (exit code, stderr)."""
        p = subprocess.Popen([AGENTBOX, f"--actor=Orca ({address})", "wake", vm], env=self.env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())

        def wait():
            _, err = p.communicate(timeout=60)
            return p.returncode, err
        wait.process = p
        return wait

    def wake_waiting(self, address, vm="work"):
        """wake_in_background, once the wake has checked the VM under its lifecycle lock (so a
        Restart or Reset started now does not come first) and waits for Orca."""
        lock = self.path(f"var/lib/agentbox/locks/lifecycle-{vm}.lock")
        if os.path.exists(lock):
            os.unlink(lock)
        wait = self.wake_in_background(address, vm)
        held, deadline = False, time.monotonic() + 3
        while time.monotonic() < deadline:
            if os.path.exists(lock):
                with open(lock) as f:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        if held:
                            break                 # taken by the wake (around virsh) and given back
                    except BlockingIOError:
                        held = True
            time.sleep(0.005)
        time.sleep(0.5)                           # it reads the VM's start right after
        return wait

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
        waiting = int(time.time())
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        work = self.describe()
        self.assertEqual((work["state"], work["responding"]["orca"], work["failed_wakes"]["count"]),
                         ("not_responding", False, 1))
        # since the wake began to wait (shown on the wall clock via the boot time, to a second)
        self.assertLessEqual(abs(epoch(work["responding"]["since"]) - waiting), 2)

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

    def start_work(self, **scenario):
        """watch runs, then work starts: its first object once Orca was asked."""
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off", "managed_save": True}},
                      systemctl_units=UNITS, **scenario)
        self.start()
        self.event("vms")
        self.assertEqual(self.run_agentbox("start", "work")[0], 0)
        return self.event("vm", lambda v: self.is_vm("starting")(v) and v["responding"] is not None)

    def test_watch_starting_then_running(self):
        work = self.start_work()
        self.assertEqual((work["responding"]["orca"], work["responding"]["guest_agent"]), (False, True))
        self.assertIsNone(work["agents"])
        self.orca_answers()
        work = self.event("vm", self.is_vm("running"))
        self.assertEqual(work["responding"], {"guest_agent": None, "orca": True, "since": None})

    def test_watch_starting_then_not_responding_system_up(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "3"
        starting = self.start_work()
        work = self.event("vm", self.is_vm("not_responding"))
        self.assertEqual((work["responding"]["orca"], work["responding"]["guest_agent"]), (False, True))
        # not answering since the start
        self.assertEqual(work["responding"]["since"], starting["responding"]["since"])

    def test_watch_starting_then_not_responding_system_not_booted(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "3"
        self.start_work(agent_down=True)
        work = self.event("vm", self.is_vm("not_responding"))
        self.assertEqual((work["responding"]["orca"], work["responding"]["guest_agent"]), (False, False))

    def test_watch_one_failed_check_does_not_make_a_vm_not_responding(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "3"
        self.qemu("work", 300)
        self.orca_answers()
        self.start()
        self.assertEqual(self.event("vms")["vms"][0]["state"], "running")
        self.event("vm", lambda v: v["name"] == "work" and (v["responding"] or {}).get("orca") is True)
        self.orca_silent()
        failing = time.monotonic()
        work = self.event("vm", lambda v: v["name"] == "work" and (v["responding"] or {}).get("orca") is False)
        self.assertEqual(work["state"], "running")
        self.event("vm", self.is_vm("not_responding"))
        self.assertGreater(time.monotonic() - failing, 2.5)

    def test_watch_after_a_restart_the_vm_is_starting_again(self):
        """What watch knows of a VM's answers belongs to one start: after a Restart between two
        looks at Orca the VM is `starting`, not `running` on the earlier start's answer."""
        self.extra["AGENTBOX_WATCH_AGENTS_MS"] = "10000"
        self.qemu("work", 300)
        self.orca_answers()
        self.start()
        self.event("vms")
        self.event("vm", lambda v: self.is_vm("running")(v) and (v["responding"] or {}).get("orca") is True)
        self.assertEqual(self.run_agentbox("restart", "work")[0], 0)
        work = self.event("vm", self.is_vm("starting"), timeout=5)
        self.assertEqual((work["responding"], work["agents"]), (None, None))

    def test_watch_needs_a_failed_check_after_the_time_limit(self):
        """A slow round of checks (several Orca CLIs that hang) does not let the time limit run
        out on one failed check: the next check has to fail too."""
        self.extra.update(AGENTBOX_WAKE_SECONDS="2", AGENTBOX_WATCH_AGENTS_MS="5000")
        self.qemu("work", 300)
        self.orca_answers()
        self.start()
        self.event("vm", lambda v: v["name"] == "work" and (v["responding"] or {}).get("orca") is True)
        self.orca_silent()
        work = self.event("vm", lambda v: v["name"] == "work" and (v["responding"] or {}).get("orca") is False)
        self.assertEqual(work["state"], "running")
        # the time limit runs out before the next check
        later = [e["data"]["state"] for e in self.events(3.5) if e["event"] == "vm" and e["data"]["name"] == "work"]
        self.assertNotIn("not_responding", later)
        self.event("vm", self.is_vm("not_responding"), timeout=10)

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
        self.extra.update(AGENTBOX_WAKE_SECONDS="3", AGENTBOX_WATCH_AGENTS_MS="6000")
        self.start_work()
        self.orca_answers()
        states = [e["data"]["state"] for e in self.events(5) if e["event"] == "vm" and e["data"]["name"] == "work"]
        self.assertNotIn("not_responding", states)
        self.assertEqual(states[-1:], ["running"])

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
        self.extra["AGENTBOX_WATCH_AGENTS_MS"] = "10000"
        self.orca_answers()
        self.start()
        self.event("vms")
        self.assertEqual(self.run_agentbox("start", "priv")[0], 0)
        priv = self.event("vm", lambda v: v["name"] == "priv" and v["state"] == "running", timeout=5)
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
        self.event("vm", self.is_vm("not_responding"), timeout=15)
        self.domains(lambda d: d.setdefault("agentbox-work", {}).update(state="paused"))
        self.event("vm", self.is_vm("suspended"))
        time.sleep(1)
        self.domains(lambda d: d["agentbox-work"].update(state="running"))
        states = [e["data"]["state"] for e in self.events(4) if e["event"] == "vm" and e["data"]["name"] == "work"]
        self.assertEqual(states[:1], ["not_responding"])
        self.assertNotIn("running", states)

    def test_watch_takes_a_wake_that_gave_up_before_its_own_look(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        waiting = time.time()
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        self.start()
        [work] = [v for v in self.event("vms")["vms"] if v["name"] == "work"]
        self.assertEqual((work["state"], work["responding"]["guest_agent"], work["responding"]["orca"]),
                         ("not_responding", None, False))
        # silent since the wake began to wait: nobody looked at the VM before
        self.assertLessEqual(abs(epoch(work["responding"]["since"]) - waiting), 2)

    # --- failed wakes -------------------------------------------------------------------

    def test_failed_wakes_count_since_the_start(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        work = self.describe()
        self.assertEqual(work["state"], "not_responding")
        wakes = work["failed_wakes"]
        self.assertEqual((wakes["count"], wakes["address"]), (2, "192.0.2.41"))
        self.assertLess(abs(datetime.datetime.strptime(wakes["last"], "%Y-%m-%dT%H:%M:%SZ")
                            .replace(tzinfo=datetime.timezone.utc).timestamp() - time.time()), 10)
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
        self.extra.update(AGENTBOX_WAKE_SECONDS="2", AGENTBOX_SHUTDOWN_SECONDS="5")
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}, "work": {"shutdown": "ignored"}},
                      systemctl_units=UNITS)
        self.qemu("work", 300)
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        self.assertEqual(self.actions(), ["wake.not-responding"])

    def test_a_wake_that_gives_up_while_a_restart_shuts_the_vm_down_is_no_new_incident(self):
        self.hung()
        waiting = self.wake_waiting("192.0.2.41")
        # the wake gives up while the Restart waits up to 5 s for the shutdown
        code, _, err = self.run_agentbox("restart", "work")
        self.assertEqual(code, 0, err)
        self.assertEqual(waiting(), (1, SILENT))
        self.assertEqual(self.actions(), ["wake.not-responding", "restart"])
        self.assertEqual(self.failed_wakes(), NONE)

    def test_a_reset_without_start_clears_a_wake_that_gave_up_while_the_vm_shut_down(self):
        self.hung()
        waiting = self.wake_waiting("192.0.2.41")
        code, _, err = self.run_agentbox("reset", "work", "--no-start")
        self.assertEqual(code, 0, err)
        self.assertEqual(waiting(), (1, SILENT))
        work = self.describe()
        self.assertEqual((work["state"], work["failed_wakes"]), ("stopped", NONE))
        self.assertEqual(self.actions(), ["wake.not-responding", "reset"])

    def test_a_wake_that_waited_through_a_restart_counts_for_neither_start(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "4"
        self.qemu("work", 300)
        waiting = self.wake_waiting("192.0.2.41")
        self.assertEqual(self.run_agentbox("restart", "work")[0], 0)
        self.assertEqual(waiting(), (1, SILENT))
        self.assertEqual(self.failed_wakes(), NONE)
        self.assertEqual(self.actions(), ["restart"])

    def test_a_wake_that_started_the_vm_is_recorded_when_the_vm_stops_while_it_waits(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off", "managed_save": True}},
                      systemctl_units=UNITS)
        waiting = self.wake_in_background("192.0.2.41")
        deadline = time.monotonic() + 10
        while not os.path.exists(self.path("run/libvirt/qemu/agentbox-work.pid")) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.calls("start")), 1)
        # the guest powers off while the wake waits for Orca (after its start's call)
        self.domains(lambda d: d["agentbox-work"].update(state="shut off"))
        fake_proc.gone(self.root, "work")
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
        deadline = time.monotonic() + 10
        pings = os.path.join(self.dir, "calls.jsonl")
        while not (os.path.exists(pings) and self.calls("guest-ping")) and time.monotonic() < deadline:
            time.sleep(0.05)
        waiting.process.terminate()
        waiting()
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}}, systemctl_units=UNITS)
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        self.assertEqual(self.actions(), ["wake.not-responding"])
        self.assertEqual(self.failed_wakes()["count"], 2)

    def test_a_wake_that_started_the_vm_has_its_own_entry_when_another_recorded_the_incident(self):
        """Wakes of several connections while the VM was off: the one that started it gives up
        after another one recorded the incident. Its own entry says who caused the start."""
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off", "managed_save": True}},
                      systemctl_units=UNITS)
        self.extra["AGENTBOX_WAKE_SECONDS"] = "3"
        first = self.wake_in_background("192.0.2.36")
        deadline = time.monotonic() + 10
        while not os.path.exists(self.path("run/libvirt/qemu/agentbox-work.pid")) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.assertEqual(self.wake("192.0.2.41"), (1, "", SILENT))
        self.assertEqual(first(), (1, SILENT))
        self.assertEqual(len(self.calls("start")), 1)
        self.assertEqual(self.recorded(), [("wake.not-responding", "Orca (192.0.2.41)", "failed"),
                                           ("wake", "Orca (192.0.2.36)", "failed")])
        self.assertEqual(self.failed_wakes()["count"], 2)

    def test_a_wake_that_started_the_vm_has_its_own_entry_when_watch_recorded_the_incident(self):
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off", "managed_save": True}},
                      systemctl_units=UNITS)
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.start()
        self.event("vms")
        # the wake waits longer than watch's time limit: watch notices first
        self.extra["AGENTBOX_WAKE_SECONDS"] = "4"
        waiting = self.wake_in_background("192.0.2.36")
        self.event("vm", self.is_vm("not_responding"))
        self.assertEqual(waiting(), (1, SILENT))
        self.assertEqual(self.recorded(), [("wake.not-responding", "watch", "failed"),
                                           ("wake", "Orca (192.0.2.36)", "failed")])

    def test_a_hanging_guest_agent_holds_up_the_wakes_file_only_briefly(self):
        """The guest-ping that says what does not answer runs under the wakes file's lock: a
        guest agent or libvirtd that hangs is given up after a few seconds, not virsh's 15 s,
        so other wakes and watch wait no longer."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.scenario(vms={"priv": {"state": "shut off", "managed_save": True}}, systemctl_units=UNITS,
                      agent_hangs=30)
        self.qemu("work", 300)
        began = time.monotonic()
        self.assertEqual(self.wake("192.0.2.36"), (1, "", SILENT))
        self.assertLess(time.monotonic() - began, 10)
        [e] = self.journald.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_PARAM_REASON"]), ("wake.not-responding", "system-not-booted"))

    def test_a_wake_that_gave_up_while_another_reached_orca_is_no_incident(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
        self.qemu("work", 300)
        waiting = self.wake_waiting("192.0.2.36")
        # another connection's wake reaches Orca meanwhile (its own Orca address answers)
        other = socket.socket()
        other.bind(("127.0.0.1", 0))
        other.listen(4)
        self.addCleanup(other.close)
        r = subprocess.run([AGENTBOX, "--actor=Orca (192.0.2.41)", "wake", "work"], capture_output=True,
                           text=True, timeout=60,
                           env={**self.env(), "AGENTBOX_ORCA_ADDR": "127.0.0.1:%d" % other.getsockname()[1]})
        self.assertEqual((r.returncode, r.stderr), (0, ""))
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
        deadline = time.monotonic() + 10
        while not os.path.exists(self.path("run/libvirt/qemu/agentbox-work.pid")) and time.monotonic() < deadline:
            time.sleep(0.05)
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
        one failed look at a VM that answered is not enough either."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
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
        self.orca_answers()
        self.start()
        self.event("vm", self.answering)
        r = subprocess.run([AGENTBOX, "--actor=Orca (192.0.2.36)", "wake", "work"], capture_output=True,
                           text=True, timeout=60, env={**self.env(), "AGENTBOX_ORCA_ADDR": self.silent_orca()})
        self.assertEqual((r.returncode, r.stderr), (1, SILENT))
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.failed_wakes(), NONE)

    def test_watch_writes_the_wakes_file_only_with_news(self):
        """Not on every look: when Orca first answers in a start, then at most every half time
        limit while it keeps answering."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "120"
        self.qemu("work", 300)
        self.orca_answers()
        self.start()
        self.event("vm", self.answering)
        written = os.stat(self.wakes_file())
        self.events(1.5)                          # about ten looks
        now = os.stat(self.wakes_file())
        self.assertEqual((now.st_ino, now.st_mtime_ns), (written.st_ino, written.st_mtime_ns))

    # --- the incident's entry: from a wake or from watch, once ----------------------------

    def test_watch_takes_one_failed_look_at_a_vm_it_did_not_see_start_for_nothing(self):
        """After an update every running VM lacks a wakes file: a first look that fails once (a
        VM under load) makes it neither not responding nor an entry."""
        self.extra["AGENTBOX_WAKE_SECONDS"] = "2"
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
        self.events(1.5)                          # more looks at it
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
        self.events(1)                            # more looks at it
        self.assertEqual(self.recorded(), [("wake.not-responding", "Orca (192.0.2.36)", "failed")])

    def test_wakes_and_watch_at_the_same_time_record_one_entry(self):
        self.extra["AGENTBOX_WAKE_SECONDS"] = "1"
        self.qemu("work", 300)
        waiting = [self.wake_in_background(f"192.0.2.{n}") for n in range(30, 36)]
        self.start()
        self.assertEqual([w() for w in waiting], [(1, SILENT)] * 6)
        self.event("vm", self.is_vm("not_responding"))
        self.events(1)
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
        self.events(1)
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.stderr().count("wakes-work.json"), 1, self.stderr())


if __name__ == "__main__":
    unittest.main()
