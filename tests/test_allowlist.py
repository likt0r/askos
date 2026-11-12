"""Allowlist with scope and temporary entries (#14).

Seam C: the real proxy (`proxy/agentbox-proxy`) on a Unix socket with a temporary
configuration. Allowed tunnels go to a local echo server instead of the resolved
address (AGENTBOX_PROXY_TEST_UPSTREAM, honoured only next to AGENTBOX_CONF_DIR), so
the tests can hold tunnels open and see the proxy close them. The proxy checks for
withdrawn access every 0.1 s here (AGENTBOX_PROXY_CHECK_SECONDS).

Seam A: the `agentbox` CLI against a temporary root (AGENTBOX_ROOT) with a fake
classifier, a fake journalctl (fake_journalctl.py) and a stand-in for journald's
socket (fake_journald.py).

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from client_hello import ACCESS_DENIED, answer, client_hello  # noqa: E402
from fake_journald import Journald  # noqa: E402
from journal_writers import FORGED, proxy, write_passwd, written_by  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(REPO, "proxy", "agentbox-proxy")
AGENTBOX = os.path.join(REPO, "bin", "agentbox")
TESTS = os.path.join(REPO, "tests")
CHECK = 0.1                     # the proxy's check interval in these tests
HELLO = 2.0                     # how long the proxy waits for a ClientHello in these tests
SETTLE = 6 * CHECK              # long enough for the proxy to have acted on a change
FIREFOX = "Firefox/Linux (192.0.2.23)"


def write(path, data, mode=0o644):
    """Replace a file atomically, as agentbox does."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def entry(domain, scope, expires=None, note="", added=1793700000, added_by="bewr"):
    return {"domain": domain, "scope": scope, "expires": expires, "note": note, "added": added,
            "added_by": added_by}


# --- Seam C: the proxy over a socket ------------------------------------------------

class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            while data := self.request.recv(4096):
                self.request.sendall(data)
        except OSError:
            pass


class EchoServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


class ProxyTest(unittest.TestCase):
    """The proxy of VM `work`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.conf = os.path.join(self.dir, "conf")
        write(os.path.join(self.conf, "suspicious-domains"), "exfil.example exfil # Test\n")
        write(os.path.join(self.conf, "legitimate-domains"), "# leer\n")
        self.sock = os.path.join(self.dir, "proxy.sock")
        self.log_path = os.path.join(self.dir, "proxy.log")
        self.echo = EchoServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=self.echo.serve_forever, daemon=True).start()
        self.addCleanup(self.echo.server_close)
        self.addCleanup(self.echo.shutdown)
        self.proxy = None

    def store(self, *entries, raw=None):
        write(os.path.join(self.conf, "allowlist.json"),
              raw if raw is not None else json.dumps({"entries": list(entries)}))

    def open_web(self, seconds=600):
        write(os.path.join(self.conf, "open-work"), f"{int(time.time()) + seconds}\n")

    def close_web(self):
        os.remove(os.path.join(self.conf, "open-work"))

    def start(self, upstream=None):
        """The proxy, every allowed destination going to upstream (port on 127.0.0.1; the echo
        server by default; False: the proxy resolves names and connects itself)."""
        port = self.echo.server_address[1] if upstream is None else upstream
        env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "AGENTBOX_CONF_DIR": self.conf,
               "AGENTBOX_FEEDS_DIR": os.path.join(self.dir, "feeds"), "AGENTBOX_PROXY_CHECK_SECONDS": str(CHECK),
               "AGENTBOX_PROXY_HELLO_SECONDS": str(HELLO),
               **({"AGENTBOX_PROXY_TEST_UPSTREAM": "127.0.0.1:%d" % port} if port is not False else {})}
        with open(self.log_path, "wb") as err:
            self.proxy = subprocess.Popen([sys.executable, "-I", PROXY, "work", self.sock], env=env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 15
        while "[work] ready" not in self.log():
            if self.proxy.poll() is not None or time.monotonic() > deadline:
                self.fail("proxy did not start: " + self.log())
            time.sleep(0.02)

    def stop(self):
        if self.proxy and self.proxy.poll() is None:
            self.proxy.terminate()
            self.proxy.wait(10)

    def log(self):
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def request(self, host):
        """A socket after the ClientHello for host, and the proxy's answer (client_hello.answer)."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.settimeout(10)
        s.connect(self.sock)
        hello = client_hello(host)
        s.sendall(hello)
        return s, answer(s, hello)

    def tunnel(self, host):
        s, answered = self.request(host)
        self.assertEqual(answered, "open", self.log())
        self.assertTrue(self.alive(s))
        return s

    def refused(self, host):
        s, answered = self.request(host)
        s.close()
        return answered == ACCESS_DENIED

    def alive(self, s):
        """The tunnel still carries data both ways."""
        try:
            s.sendall(b"ping")
            return s.recv(4) == b"ping"
        except OSError:
            return False

    def closed(self, s, timeout=5):
        """The proxy closed the tunnel within the time limit."""
        s.settimeout(timeout)
        try:
            return s.recv(1) == b""
        except socket.timeout:
            return False
        except OSError:
            return True


class ScopeTest(ProxyTest):
    def test_entries_for_all_vms_and_for_this_vm(self):
        self.store(entry("all.example", "all"), entry("work.example", "work"), entry("priv.example", "priv"))
        self.start()
        self.tunnel("all.example")
        self.tunnel("cdn.all.example")          # subdomains too
        self.tunnel("work.example")
        self.assertTrue(self.refused("priv.example"))
        self.assertTrue(self.refused("notall.example"))
        log = self.log()
        self.assertIn("[work] ALLOWED all.example (allowlist all.example all)\n", log)
        self.assertIn("[work] ALLOWED cdn.all.example (allowlist all.example all)\n", log)
        self.assertIn("[work] ALLOWED work.example (allowlist work.example work)\n", log)
        self.assertIn("[work] BLOCKED priv.example (not on the Allowlist)\n", log)

    def test_an_allowlist_entry_of_a_user_content_host_lets_a_feed_hit_through_with_an_alert(self):
        # Known residual risk, to change with #59 (the Allowlist no longer passing before the Open
        # web check): claudeusercontent.com is on the starting Allowlist, and decide() lets an
        # Allowlist domain entry pass before the Open web check, so a threat-intelligence Feed hit
        # below it is allowed with an Alert, not blocked as it is when the web is open.
        self.store(entry("claudeusercontent.com", "all", added_by="migration"))
        write(os.path.join(self.dir, "feeds", "threat-intelligence"), "evil.claudeusercontent.com\n")
        self.start()
        self.tunnel("evil.claudeusercontent.com")
        log = self.log()
        self.assertIn("[work] ALERT evil.claudeusercontent.com [threat]\n", log)
        self.assertIn("[work] ALLOWED evil.claudeusercontent.com (allowlist claudeusercontent.com all)\n", log)

    def test_the_most_specific_entry_is_logged_the_vms_own_on_a_tie(self):
        self.store(entry("example.org", "all"), entry("example.org", "work"), entry("api.example.org", "all"))
        self.start()
        self.tunnel("api.example.org")
        self.tunnel("www.example.org")
        self.assertIn("ALLOWED api.example.org (allowlist api.example.org all)\n", self.log())
        self.assertIn("ALLOWED www.example.org (allowlist example.org work)\n", self.log())

    def test_changes_apply_without_restart(self):
        self.store()
        self.start()
        self.assertTrue(self.refused("later.example"))
        self.store(entry("later.example", "work"))
        self.tunnel("later.example")

    def test_entries_with_wrong_types_allow_nothing(self):
        self.store({"domain": "a.example", "scope": "work", "expires": "never"},
                   {"domain": "b.example", "scope": ["work"], "expires": None}, entry("c.example", "work"))
        self.start()
        self.assertTrue(self.refused("a.example"))
        self.assertTrue(self.refused("b.example"))
        self.tunnel("c.example")

    def test_a_broken_file_keeps_the_entries_before_it(self):
        self.store(entry("a.example", "work"))
        self.start()
        t = self.tunnel("a.example")
        self.store(raw="{kaputt")
        time.sleep(SETTLE)
        self.tunnel("a.example")
        self.assertTrue(self.alive(t))
        self.assertIn("[work] ERROR ", self.log())

    def test_legacy_files_apply_until_the_store_exists(self):
        write(os.path.join(self.conf, "allowlist"), "# alle\nall.example  # Kommentar\nUPPER.Example.\n")
        write(os.path.join(self.conf, "allowlist-work"), "work.example\n")
        write(os.path.join(self.conf, "allowlist-priv"), "priv.example\n")
        self.start()
        self.tunnel("all.example")
        self.tunnel("upper.example")
        self.tunnel("work.example")
        self.assertTrue(self.refused("priv.example"))
        self.assertIn("ALLOWED work.example (allowlist work.example work)\n", self.log())
        # once allowlist.json exists, the old files count no more
        self.store(entry("new.example", "work"))
        self.assertTrue(self.refused("all.example"))
        self.tunnel("new.example")

    def test_log_keywords_are_english(self):
        self.store(entry("a.example", "work"))
        self.start()
        self.tunnel("a.example")
        self.assertTrue(self.refused("b.example"))
        self.assertTrue(self.refused("upload.exfil.example"))
        log = self.log()
        for keyword in ("ALLOWED", "BLOCKED", "ALERT"):
            self.assertRegex(log, rf"\[work\] {keyword} ")
        for german in ("ERLAUBT", "BLOCKIERT", "VERDACHT", "bereit"):
            self.assertNotIn(german, log)


class ExpiryTest(ProxyTest):
    def test_expired_entry_stops_new_connections_but_the_open_tunnel_continues(self):
        self.store(entry("a.example", "work", expires=int(time.time()) + 2))
        self.start()
        t = self.tunnel("a.example")
        time.sleep(2.5)
        self.assertTrue(self.refused("a.example"))
        self.assertIn("[work] BLOCKED a.example (not on the Allowlist)\n", self.log())
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))
        # the timer removes the expired entry: still nothing is closed
        self.store()
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))
        self.assertNotIn("CLOSED", self.log())

    def test_extending_or_making_permanent_closes_nothing(self):
        self.store(entry("a.example", "work", expires=int(time.time()) + 600))
        self.start()
        t = self.tunnel("a.example")
        self.store(entry("a.example", "work", expires=int(time.time()) + 7200))
        time.sleep(SETTLE)
        self.store(entry("a.example", "work"))
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))
        self.assertNotIn("CLOSED", self.log())


class RemovalTest(ProxyTest):
    def test_removing_an_entry_closes_only_its_tunnels(self):
        self.store(entry("a.example", "work", expires=int(time.time()) + 600), entry("b.example", "all"))
        self.start()
        ta, ta2, tb = self.tunnel("a.example"), self.tunnel("www.a.example"), self.tunnel("b.example")
        self.store(entry("b.example", "all"))
        self.assertTrue(self.closed(ta))
        self.assertTrue(self.closed(ta2))
        self.assertTrue(self.alive(tb))
        self.assertIn("[work] CLOSED a.example (removed from the Allowlist)\n", self.log())
        self.assertIn("[work] CLOSED www.a.example (removed from the Allowlist)\n", self.log())

    def test_a_tunnel_another_entry_still_allows_stays(self):
        self.store(entry("example.org", "all"), entry("example.org", "work"), entry("api.example.org", "work"))
        self.start()
        t = self.tunnel("api.example.org")
        self.store(entry("example.org", "work"), entry("example.org", "all"))
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))
        self.store(entry("example.org", "all"))
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))
        self.store()
        self.assertTrue(self.closed(t))

    def test_an_entry_of_another_vm_closes_nothing_here(self):
        self.store(entry("a.example", "work"), entry("a.example", "priv"))
        self.start()
        t = self.tunnel("a.example")
        self.store(entry("a.example", "work"))
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))

    def test_removing_an_entry_while_the_web_is_open_keeps_the_tunnel_until_it_closes(self):
        self.store(entry("a.example", "work"))
        self.open_web()
        self.start()
        t = self.tunnel("a.example")
        self.store()
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))
        self.close_web()
        self.assertTrue(self.closed(t))
        self.assertIn("[work] CLOSED a.example (Open web ended)\n", self.log())


class OpenWebTest(ProxyTest):
    def test_closing_open_web_closes_the_tunnels_only_it_allowed(self):
        self.store(entry("listed.example", "work"))
        self.open_web()
        self.start()
        free, listed = self.tunnel("free.example"), self.tunnel("listed.example")
        self.assertIn("[work] ALLOWED free.example (open web)\n", self.log())
        self.assertIn("[work] ALLOWED listed.example (allowlist listed.example work)\n", self.log())
        self.close_web()
        self.assertTrue(self.closed(free))
        self.assertTrue(self.alive(listed))
        self.assertIn("[work] CLOSED free.example (Open web ended)\n", self.log())
        self.assertTrue(self.refused("free.example"))

    def test_open_web_running_out_closes_them_too(self):
        self.store()
        self.open_web(seconds=2)
        self.start()
        t = self.tunnel("free.example")
        self.assertTrue(self.closed(t, timeout=8))

    def test_an_entry_added_meanwhile_keeps_the_tunnel(self):
        self.store()
        self.open_web()
        self.start()
        t = self.tunnel("free.example")
        self.store(entry("free.example", "all"))
        self.close_web()
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))

    def test_strongly_suspicious_destinations_stay_blocked_unless_allowed(self):
        self.store()
        self.open_web()
        self.start()
        self.assertTrue(self.refused("upload.exfil.example"))
        self.assertIn("[work] BLOCKED upload.exfil.example (suspicious: exfil)\n", self.log())
        # an entry (only possible with allow --force) lets it through
        self.store(entry("exfil.example", "work"))
        self.tunnel("upload.exfil.example")
        self.assertIn("[work] ALERT upload.exfil.example [exfil]\n", self.log())

    def test_an_unreadable_watchlist_closes_open_web_but_not_the_allowlist(self):
        # the Watchlist is missing (no record, no text file from before): what Open web would
        # keep blocked is unknown, so it lets nothing through; the Allowlist still does
        os.remove(os.path.join(self.conf, "suspicious-domains"))
        self.store(entry("listed.example", "work"))
        self.open_web()
        self.start()
        self.assertTrue(self.refused("free.example"))
        self.assertIn("[work] BLOCKED free.example (Watchlist unreadable, Open web closed)\n", self.log())
        self.tunnel("listed.example")
        # back: Open web lets it through again
        write(os.path.join(self.conf, "suspicious-domains"), "exfil.example exfil # Test\n")
        self.tunnel("free.example")


# --- Seam A: the CLI ------------------------------------------------------------------

# agentbox-suspicious: evil.* strongly suspicious (exit 2), weak.* a weak Signal, the rest unremarkable
CLASSIFIER = '''#!/bin/sh
echo "$1" >> "{dir}/classifier.log"
case "$1" in evil.*) echo exfil; exit 2;; weak.*) echo young; exit 0;; esac
echo
'''


def fake(path, program):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nexec python3 "{os.path.join(TESTS, program)}" "$@"\n')
    os.chmod(path, 0o755)


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.root = os.path.join(self.dir, "root")
        self.conf = os.path.join(self.root, "etc", "agentbox")
        vm_records.write(self.root)
        write_passwd(self.root)
        fake(os.path.join(self.dir, "bin", "journalctl"), "fake_journalctl.py")
        classifier = os.path.join(self.root, "usr", "libexec", "agentbox-suspicious")
        os.makedirs(os.path.dirname(classifier))
        with open(classifier, "w") as f:
            f.write(CLASSIFIER.format(dir=self.dir))
        os.chmod(classifier, 0o755)
        self.journald = Journald(os.path.join(self.root, "run", "systemd", "journal.agentbox", "socket"))
        self.addCleanup(self.journald.close)
        self.scenario()

    def scenario(self, **s):
        with open(os.path.join(self.dir, "scenario.json"), "w") as f:
            json.dump(s, f)

    def run_agentbox(self, *args):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_DIR": self.dir,
               "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def ok(self, *args):
        code, out, err = self.run_agentbox(*args)
        self.assertEqual((code, err), (0, ""), args)
        return out

    def json(self, *args):
        return json.loads(self.ok(*args, "--json"))

    def refused(self, *args):
        """The command fails with this message and changes nothing."""
        before = self.files()
        code, out, err = self.run_agentbox(*args)
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(self.files(), before)
        self.assertTrue(err.startswith("agentbox: "), err)
        return err[len("agentbox: "):].rstrip("\n")

    def files(self):
        out = {}
        for f in sorted(os.listdir(self.conf)):
            if os.path.isfile(os.path.join(self.conf, f)):
                with open(os.path.join(self.conf, f), "rb") as fh:
                    out[f] = fh.read()
        return out

    def store(self, *entries):
        write(os.path.join(self.conf, "allowlist.json"), json.dumps({"entries": list(entries)}))

    def stored(self):
        with open(os.path.join(self.conf, "allowlist.json")) as f:
            return json.load(f)["entries"]

    def one(self, domain, scope):
        [e] = [e for e in self.stored() if e["domain"] == domain and e["scope"] == scope]
        return e

    def classified(self):
        try:
            with open(os.path.join(self.dir, "classifier.log")) as f:
                return f.read().split()
        except FileNotFoundError:
            return []

    def activity(self):
        return [(e["AGENTBOX_KIND"], e["AGENTBOX_ACTION"], e.get("AGENTBOX_VM"), e["AGENTBOX_DESCRIPTION"])
                for e in self.journald.entries()]

    def journal(self, entries):
        with open(os.path.join(self.dir, "journal.json"), "w") as f:
            json.dump(entries, f)

    def until(self, expires):
        """How the CLI shows the end of a temporary entry (TZ=UTC)."""
        if time.strftime("%Y-%m-%d", time.gmtime(expires)) == time.strftime("%Y-%m-%d", time.gmtime()):
            return time.strftime("%H:%M", time.gmtime(expires))
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(expires))


class AllowTest(CliTest):
    def test_permanent_entry_for_one_vm(self):
        self.assertEqual(self.run_agentbox("--actor", FIREFOX, "allow", "work", "example.org"),
                         (0, "work: example.org allowed\n", ""))
        e = self.one("example.org", "work")
        self.assertAlmostEqual(e.pop("added"), time.time(), delta=60)
        self.assertEqual(e, {"domain": "example.org", "scope": "work", "expires": None, "note": "",
                             "added_by": FIREFOX})
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.conf, "allowlist.json")).st_mode), 0o644)
        self.assertEqual(self.classified(), ["example.org"])

    def test_entry_for_all_vms(self):
        self.assertEqual(self.ok("allow", "all", "example.org"), "all VMs: example.org allowed\n")
        self.assertEqual(self.one("example.org", "all")["expires"], None)

    def test_temporary_entries(self):
        for duration, seconds in (("15min", 900), ("1h", 3600), ("8h", 8 * 3600), ("7d", 7 * 86400)):
            with self.subTest(duration=duration):
                domain = f"d{seconds}.example"
                out = self.ok("allow", "work", domain, "--for", duration)
                expires = self.one(domain, "work")["expires"]
                self.assertAlmostEqual(expires, time.time() + seconds, delta=60)
                self.assertEqual(out, f"work: {domain} allowed until {self.until(expires)}\n")

    def test_temporary_with_the_default_duration(self):
        self.ok("allow", "priv", "example.org", "--temporary", "--note=fuer eine Recherche")
        e = self.one("example.org", "priv")
        self.assertAlmostEqual(e["expires"], time.time() + 3600, delta=60)
        self.assertEqual(e["note"], "fuer eine Recherche")

    def test_temporary_follows_the_settings(self):
        with open(os.path.join(self.root, "etc/agentbox/settings.json"), "w") as f:
            f.write('{"temporary_entry_default": "15min"}\n')
        self.ok("allow", "priv", "example.org", "--temporary")
        self.assertAlmostEqual(self.one("example.org", "priv")["expires"], time.time() + 900, delta=60)

    def test_json(self):
        result = self.json("allow", "all", "example.org", "--for=8h", "--note", " Doku ")
        self.assertAlmostEqual(result.pop("added"), time.time(), delta=60)
        expires = result.pop("expires")
        self.assertEqual(result, {"domain": "example.org", "regex": None, "scope": "all", "note": "Doku",
                                  "added_by": result["added_by"],
                                  "message": f"all VMs: example.org allowed until {self.until(expires)}"})

    def test_an_existing_entry_is_widened_never_shortened(self):
        self.ok("allow", "work", "perm.example", "--note", "erste")
        self.ok("allow", "work", "perm.example", "--for", "1h")
        self.assertEqual((self.one("perm.example", "work")["expires"], self.one("perm.example", "work")["note"]),
                         (None, "erste"))
        self.ok("allow", "work", "temp.example", "--for", "8h")
        long = self.one("temp.example", "work")["expires"]
        self.ok("allow", "work", "temp.example", "--for", "1h", "--note", "zweite")
        self.assertEqual((self.one("temp.example", "work")["expires"], self.one("temp.example", "work")["note"]),
                         (long, "zweite"))
        self.ok("allow", "work", "temp.example", "--for", "7d")
        self.assertAlmostEqual(self.one("temp.example", "work")["expires"], time.time() + 7 * 86400, delta=60)
        self.ok("allow", "work", "temp.example")
        self.assertIsNone(self.one("temp.example", "work")["expires"])
        self.assertEqual(len(self.stored()), 2)

    def test_same_domain_for_all_vms_and_one_vm_are_two_entries(self):
        self.ok("allow", "all", "example.org")
        self.ok("allow", "work", "example.org", "--for", "1h")
        self.assertEqual(sorted((e["scope"], e["expires"] is None) for e in self.stored()),
                         [("all", True), ("work", False)])

    def test_strong_signals_only_with_force(self):
        self.assertEqual(self.refused("allow", "work", "evil.example"),
                         "evil.example is suspicious (exfil). Allowing it only with --force")
        self.assertEqual(self.refused("allow", "all", "evil.example", "--for", "15min"),
                         "evil.example is suspicious (exfil). Allowing it only with --force")
        self.assertEqual(self.ok("allow", "work", "evil.example", "--force"), "work: evil.example allowed\n")

    def test_weak_signals_need_no_force(self):
        self.assertEqual(self.ok("allow", "work", "weak.example"), "work: weak.example allowed\n")

    def test_invalid_arguments(self):
        cases = [
            (["dev", "example.org"], "No such VM: dev"),
            (["status", "example.org"], "No such VM: status"),
            ([], "no VM given"),
            (["work", "--for", "2h", "example.org"], "--for must be 15min, 1h, 8h or 7d"),
            (["work", "example.org", "--for", "permanent"], "--for must be 15min, 1h, 8h or 7d"),
            (["work", "example.org", "--for", "1H"], "--for must be 15min, 1h, 8h or 7d"),
            (["work", "example.org", "--for"], "--for needs a value"),
            (["work", "example.org", "--for", "1h", "--temporary"], "--for and --temporary not together"),
            (["work", "example.org", "--temporary=1h"], "--temporary takes no value"),
            (["work", "example.org", "--note", "x" * 201], "--note: at most 200 printable characters on one line"),
            (["work", "example.org", "--note", "zwei\nZeilen"], "--note: at most 200 printable characters on one line"),
            (["work", "example.org", "--note", "a‮b"], "--note: at most 200 printable characters on one line"),
            (["work", "example.org", "--scope", "all"], "unknown option: --scope"),
            (["work", "example.org", "other.org"], "too many arguments: other.org"),
        ]
        for d in ("Example.org", "exa mple.org", "localhost", "-example.org", "example-.org", "example.org.", ""):
            cases.append((["work", d], f"invalid domain: {d}"))
        for args, message in cases:
            with self.subTest(args=args):
                self.assertEqual(self.refused("allow", *args), message)
        self.assertEqual(self.classified(), [])
        self.assertFalse(os.path.exists(os.path.join(self.conf, "allowlist.json")))

    def test_concurrent_allows_keep_every_entry(self):
        """The Control Room may send several requests at once; none may be lost."""
        domains = [f"d{i}.example" for i in range(12)]
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_DIR": self.dir,
               "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8"}
        procs = [subprocess.Popen([AGENTBOX, "allow", "priv", d], env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE) for d in domains]
        errors = [p.communicate(timeout=60)[1] for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * len(domains), errors)
        self.assertEqual(sorted(e["domain"] for e in self.stored()), sorted(domains))


class DisallowTest(CliTest):
    def setUp(self):
        super().setUp()
        self.store(entry("example.org", "work"), entry("example.org", "all"), entry("pypi.org", "work"))

    def test_removes_the_entry_of_that_scope_only(self):
        self.assertEqual(self.ok("disallow", "work", "example.org"), "work: example.org removed\n")
        self.assertEqual([(e["domain"], e["scope"]) for e in self.stored()],
                         [("example.org", "all"), ("pypi.org", "work")])
        self.assertEqual(self.json("disallow", "all", "example.org"),
                         {"scope": "all", "domain": "example.org", "regex": None, "removed": True,
                          "message": "all VMs: example.org removed"})
        self.assertEqual([e["domain"] for e in self.stored()], ["pypi.org"])

    def test_not_on_the_list_changes_nothing_and_is_no_activity(self):
        self.assertEqual(self.ok("disallow", "priv", "example.org"),
                         "priv: example.org is not on the Allowlist (applies to all VMs: agentbox disallow all "
                         "example.org)\n")
        self.assertEqual(self.json("disallow", "work", "other.org")["removed"], False)
        self.assertEqual(len(self.stored()), 3)
        self.assertEqual(self.activity(), [])

    def test_rejects_invalid_domains(self):
        self.assertEqual(self.refused("disallow", "work", "Example.org"), "invalid domain: Example.org")


class ChangeTest(CliTest):
    def test_extend_adds_to_the_remaining_time(self):
        soon = int(time.time()) + 600
        self.store(entry("a.example", "work", expires=soon), entry("b.example", "all", expires=1000))
        out = self.ok("extend", "work", "a.example", "--for", "1h")
        self.assertEqual(self.one("a.example", "work")["expires"], soon + 3600)
        self.assertEqual(out, f"work: a.example allowed until {self.until(soon + 3600)}\n")
        # an expired one (not yet removed by the timer) runs again from now
        result = self.json("extend", "all", "b.example", "--for=15min")
        self.assertAlmostEqual(result["expires"], time.time() + 900, delta=60)

    def test_extend_refuses_permanent_and_missing_entries(self):
        self.store(entry("a.example", "work"))
        self.assertEqual(self.refused("extend", "work", "a.example", "--for", "1h"),
                         "work: a.example is allowed permanently")
        self.assertEqual(self.refused("extend", "all", "a.example", "--for", "1h"),
                         "all VMs: a.example is not on the Allowlist")
        self.assertEqual(self.refused("extend", "work", "a.example"), "extend needs --for 15min, 1h, 8h or 7d")
        self.assertEqual(self.refused("extend", "work", "a.example", "--for", "2d"),
                         "--for must be 15min, 1h, 8h or 7d")

    def test_make_permanent(self):
        self.store(entry("a.example", "work", expires=int(time.time()) + 600))
        self.assertEqual(self.ok("make-permanent", "work", "a.example"), "work: a.example allowed permanently\n")
        self.assertIsNone(self.one("a.example", "work")["expires"])
        # already permanent: nothing changes, nothing to record
        self.assertEqual(self.ok("make-permanent", "work", "a.example"), "work: a.example allowed permanently\n")
        self.assertEqual(len(self.activity()), 1)
        self.assertEqual(self.refused("make-permanent", "priv", "a.example"),
                         "priv: a.example is not on the Allowlist")

    def test_strong_signals_need_force_to_extend_or_make_permanent(self):
        later = int(time.time()) + 600
        self.store(entry("evil.example", "work", expires=later))
        for args in (["extend", "work", "evil.example", "--for", "1h"], ["make-permanent", "work", "evil.example"]):
            with self.subTest(args=args):
                self.assertEqual(self.refused(*args), "evil.example is suspicious (exfil). Allowing it only with --force")
        self.ok("extend", "work", "evil.example", "--for", "1h", "--force")
        self.ok("make-permanent", "work", "evil.example", "--force")
        self.assertIsNone(self.one("evil.example", "work")["expires"])

    def test_set_note(self):
        self.store(entry("a.example", "all", note="alt"))
        self.assertEqual(self.ok("set-note", "all", "a.example", "--note", "Paketquelle von X"),
                         "all VMs: note on a.example saved\n")
        self.assertEqual(self.one("a.example", "all")["note"], "Paketquelle von X")
        self.ok("set-note", "all", "a.example", "--note=")
        self.assertEqual(self.one("a.example", "all")["note"], "")
        self.assertEqual(self.refused("set-note", "all", "a.example"), "set-note needs --note TEXT")
        self.assertEqual(self.refused("set-note", "work", "a.example", "--note", "x"),
                         "work: a.example is not on the Allowlist")
        self.assertEqual(self.classified(), [])

    def test_activity_of_each_command(self):
        self.store(entry("a.example", "all", expires=int(time.time()) + 600))
        for args in (["allow", "all", "b.example", "--for", "1h"], ["allow", "work", "c.example", "--temporary"],
                     ["allow", "priv", "evil.example", "--note=x", "--force"],
                     ["extend", "all", "a.example", "--for=8h"], ["make-permanent", "all", "a.example"],
                     ["set-note", "all", "a.example", "--note", "geheim?"], ["disallow", "all", "a.example"]):
            self.ok("--actor", FIREFOX, *args)
        self.assertEqual(self.activity(), [
            ("allowlist", "allow", None, "Allow b.example for all VMs for 1 hour"),
            ("allowlist", "allow", "work", "Allow c.example temporarily"),
            ("allowlist", "allow", "priv", "Allow evil.example with --force"),
            ("allowlist", "extend", None, "Extend the Allowlist entry a.example for all VMs by 8 hours"),
            ("allowlist", "make-permanent", None, "Make the Allowlist entry a.example for all VMs permanent"),
            ("allowlist", "set-note", None, "Note on the Allowlist entry a.example for all VMs"),
            ("allowlist", "disallow", None, "Remove the Allowlist entry a.example for all VMs"),
        ])
        self.assertEqual({e["AGENTBOX_ACTOR"] for e in self.journald.entries()}, {FIREFOX})


def proxy_line(vm, message, age):
    return {"__REALTIME_TIMESTAMP": str(int((time.time() - age) * 10**6)), "__CURSOR": f"c{age}",
            "SYSLOG_IDENTIFIER": f"agentbox-proxy-{vm}", "MESSAGE": f"[{vm}] {message}", **proxy(vm)}


class ListTest(CliTest):
    def setUp(self):
        super().setUp()
        self.now = int(time.time())
        self.store(entry("github.com", "all"), entry("docs.rs", "priv", expires=self.now + 3600, note="Rust"),
                   entry("old.example", "work", expires=self.now - 60), entry("pypi.org", "work"))
        self.journal([
            proxy_line("work", "ALLOWED api.github.com (allowlist github.com all)", 600),
            proxy_line("priv", "ALLOWED github.com (allowlist github.com all)", 500),
            proxy_line("priv", "ALLOWED docs.rs (allowlist docs.rs priv)", 400),
            proxy_line("work", "ALLOWED files.pypi.org (allowlist pypi.org work)", 300),
            proxy_line("work", "ALLOWED github.com (allowlist github.com all)", 25 * 3600),     # older than a day
            proxy_line("work", "ERLAUBT github.com", 200),                                   # German: ignored
            proxy_line("work", "ALLOWED pypi.org (open web)", 100),
            proxy_line("work", "BLOCKED pypi.org (not on the Allowlist)", 50),
        ])

    def rows(self, *args):
        return {(r["domain"], r["scope"]): r for r in self.json("allowlist", *args)}

    def test_every_entry_with_remaining_time_and_uses(self):
        rows = self.json("allowlist")
        self.assertEqual([(r["domain"], r["scope"]) for r in rows],
                         [("github.com", "all"), ("docs.rs", "priv"), ("old.example", "work"), ("pypi.org", "work")])
        docs = rows[1]
        self.assertAlmostEqual(docs.pop("remaining"), 3600, delta=60)
        self.assertEqual(docs, {**entry("docs.rs", "priv", expires=self.now + 3600, note="Rust"), "regex": None,
                                "expired": False, "uses_24h": 1, "refused": None})
        self.assertEqual((rows[0]["uses_24h"], rows[0]["remaining"], rows[0]["expired"]), (2, None, False))
        self.assertEqual((rows[2]["expired"], rows[2]["remaining"], rows[2]["uses_24h"]), (True, 0, 0))
        self.assertEqual(rows[3]["uses_24h"], 1)

    def test_reads_the_proxy_logs_of_the_last_24_hours(self):
        self.json("allowlist")
        with open(os.path.join(self.dir, "calls.jsonl")) as f:
            [call] = [json.loads(line)["argv"] for line in f]
        self.assertIn("--namespace=+agentbox", call)
        self.assertEqual(sorted(a for a in call if a.startswith("--identifier=")),
                         ["--identifier=agentbox-proxy-priv", "--identifier=agentbox-proxy-work"])
        since = int([a for a in call if a.startswith("--since=@")][0][len("--since=@"):])
        self.assertAlmostEqual(since, time.time() - 86400, delta=60)

    def test_uses_count_only_the_vms_proxy(self):
        line = proxy_line("priv", "ALLOWED docs.rs (allowlist docs.rs priv)", 30)
        self.journal([proxy_line("priv", "ALLOWED docs.rs (allowlist docs.rs priv)", 400),
                      written_by(line, FORGED), written_by(line, proxy("work"))])
        code, out, err = self.run_agentbox("allowlist", "priv", "--json")
        # a local user's entries are not even read; the proxy of work's are, and left out
        self.assertEqual((code, err), (0, "agentbox: journal entries without a known writer left out: 1\n"))
        self.assertEqual({r["domain"]: r["uses_24h"] for r in json.loads(out)}, {"docs.rs": 1, "github.com": 0})

    def test_filter_by_vm_or_all(self):
        self.assertEqual(sorted(self.rows("priv")), [("docs.rs", "priv"), ("github.com", "all")])
        self.assertEqual(sorted(self.rows("all")), [("github.com", "all")])
        self.assertEqual(self.refused("allowlist", "dev"), "No such VM: dev")
        self.assertEqual(self.refused("allowlist", "work", "priv"), "too many arguments: priv")

    def test_text(self):
        lines = self.ok("allowlist", "work").splitlines()
        self.assertEqual(lines, [
            "github.com                               all VMs          permanent              2x",
            "old.example                              work             expired                0x",
            "pypi.org                                 work             permanent              1x",
        ])
        docs = self.ok("allowlist", "priv").splitlines()[1]
        self.assertTrue(docs.startswith("docs.rs                                  priv             until "), docs)
        self.assertTrue(docs.endswith("     1x  Rust"), docs)

    def test_an_unreadable_journal_leaves_uses_open(self):
        self.scenario(journalctl_exit=4)
        code, out, err = self.run_agentbox("allowlist", "--json")
        self.assertEqual(code, 0)
        self.assertEqual({r["uses_24h"] for r in json.loads(out)}, {None})
        self.assertIn("uses not readable", err)

    def test_a_broken_store_is_an_error(self):
        write(os.path.join(self.conf, "allowlist.json"), '{"entries": [{"domain": "x"}]}')
        code, out, err = self.run_agentbox("allowlist")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("allowlist.json: broken entry", err)
        self.assertEqual(self.run_agentbox("allow", "work", "a.example")[0], 1)


class ExpireTest(CliTest):
    def test_removes_expired_entries_and_records_each(self):
        now = int(time.time())
        self.store(entry("a.example", "work", expires=now - 120), entry("b.example", "all", expires=now - 60),
                   entry("c.example", "work", expires=now + 600), entry("d.example", "priv"))
        out = self.ok("--actor=timer", "expire-allowlist")
        self.assertEqual(out, "all VMs: b.example expired\nwork: a.example expired\n")
        self.assertEqual(sorted(e["domain"] for e in self.stored()), ["c.example", "d.example"])
        entries = self.journald.entries()
        self.assertEqual([(e["AGENTBOX_KIND"], e["AGENTBOX_ACTION"], e.get("AGENTBOX_VM"), e["AGENTBOX_ACTOR"],
                           e["AGENTBOX_PARAM_DOMAIN"], e["AGENTBOX_PARAM_SCOPE"]) for e in entries],
                         [("allowlist", "allowlist.expired", None, "timer", "b.example", "all"),
                          ("allowlist", "allowlist.expired", "work", "timer", "a.example", "work")])
        self.assertTrue(entries[0]["AGENTBOX_DESCRIPTION"].startswith(
            "Temporary Allowlist entry b.example for all VMs expired ("), entries[0])
        self.assertTrue(entries[1]["AGENTBOX_DESCRIPTION"].startswith("Temporary Allowlist entry a.example expired"))
        self.assertEqual(entries[1]["AGENTBOX_EXPIRED"], str(now - 120))

    def test_nothing_expired(self):
        self.store(entry("c.example", "work", expires=int(time.time()) + 600))
        before = os.stat(os.path.join(self.conf, "allowlist.json"))
        self.assertEqual(self.json("--actor=timer", "expire-allowlist"), {"expired": []})
        self.assertEqual(self.ok("--actor=timer", "expire-allowlist"), "")
        after = os.stat(os.path.join(self.conf, "allowlist.json"))
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(self.journald.entries(), [])

    def test_the_installed_timer_runs_it_every_minute_as_timer(self):
        """The package's units; agentbox setup enables and starts the timer (test_setup.py)."""
        with open(os.path.join(REPO, "rpm", "units", "agentbox-allowlist-expire.service")) as f:
            self.assertIn("ExecStart=/usr/bin/agentbox --actor=timer expire-allowlist\n", f.read())
        with open(os.path.join(REPO, "rpm", "units", "agentbox-allowlist-expire.timer")) as f:
            self.assertIn("OnUnitActiveSec=1min\n", f.read())


class MigrateTest(CliTest):
    def legacy(self):
        """The files of the Allowlist before allowlist.json, as on the Host."""
        write(os.path.join(self.conf, "allowlist"),
              "# Domains fuer alle\n\n# GitHub\ngithub.com\nGitHubUserContent.com.  # Kommentar\n*.wildcard.example\n"
              "github.com\n")
        write(os.path.join(self.conf, "allowlist-priv"), "# Freigaben nur fuer priv (agentbox allow/disallow)\ndocs.rs\n")
        write(os.path.join(self.conf, "allowlist-work"), "")

    def test_keeps_every_entry_and_moves_the_files_aside(self):
        self.legacy()
        code, out, err = self.run_agentbox("migrate-allowlist")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "Allowlist: 3 entries taken over from allowlist, allowlist-priv, allowlist-work; "
                              "not a domain, skipped: *.wildcard.example\n")
        self.assertEqual([(e["domain"], e["scope"], e["expires"], e["added_by"]) for e in self.stored()],
                         [("github.com", "all", None, "migration"), ("githubusercontent.com", "all", None, "migration"),
                          ("docs.rs", "priv", None, "migration")])
        self.assertEqual(sorted(self.files()), ["allowlist-priv.migrated", "allowlist-work.migrated",
                                                "allowlist.json", "allowlist.migrated"])
        self.assertEqual([a[:2] for a in self.activity()], [("allowlist", "migrate-allowlist")])

    def test_is_idempotent(self):
        self.legacy()
        self.ok("migrate-allowlist")
        before = self.files()
        self.assertEqual(self.json("migrate-allowlist"),
                         {"migrated": 0, "files": [], "skipped": [], "message": "Allowlist: nothing to take over"})
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.activity()), 1)

    def test_a_file_left_over_is_merged_without_duplicates(self):
        self.store(entry("github.com", "all", note="behalten"))
        write(os.path.join(self.conf, "allowlist"), "github.com\nnpmjs.org\n")
        self.assertEqual(self.json("migrate-allowlist")["migrated"], 1)
        self.assertEqual([(e["domain"], e["note"]) for e in self.stored()], [("github.com", "behalten"), ("npmjs.org", "")])

    def test_a_fresh_host_gets_an_empty_store(self):
        self.assertEqual(self.ok("migrate-allowlist"), "Allowlist: nothing to take over\n")
        self.assertEqual(self.stored(), [])

    def test_before_it_the_old_files_are_shown_and_the_first_change_migrates(self):
        self.legacy()
        self.assertEqual([(r["domain"], r["scope"]) for r in self.json("allowlist", "priv")],
                         [("github.com", "all"), ("githubusercontent.com", "all"), ("docs.rs", "priv")])
        self.assertNotIn("allowlist.json", self.files())
        self.ok("allow", "work", "crates.io")
        self.assertEqual([(e["domain"], e["scope"]) for e in self.stored()],
                         [("github.com", "all"), ("githubusercontent.com", "all"), ("docs.rs", "priv"),
                          ("crates.io", "work")])
        self.assertNotIn("allowlist", self.files())


if __name__ == "__main__":
    unittest.main()
