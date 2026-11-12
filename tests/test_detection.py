"""Detection (#20): the HaGeZi lists, legitimate domains, the harmless list, Alerts
marked as seen, and the self-test.

Seam C: the real proxy (`proxy/agentbox-proxy`, which loads `proxy/agentbox-suspicious`)
on a Unix socket with a temporary configuration; the tests send TLS ClientHellos and
read what the proxy answers and logs. Nothing is allowlisted and the web is only opened
for strongly suspicious destinations, so the proxy never resolves a name.

Seam A: the `agentbox` CLI against a temporary root (AGENTBOX_ROOT) with the real
classifier installed in it and a fake `journalctl` on PATH.

The fetcher (`proxy/agentbox-feeds`) runs against a local HTTPS server with a
throwaway certificate (trusted through SSL_CERT_FILE) that serves small fixture lists
under the paths of HaGeZi's repositories.

Run from the repo root: python3 -m unittest discover -s tests
"""
import gzip
import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from client_hello import ACCESS_DENIED, client_hello  # noqa: E402
from journal_writers import proxy, write_passwd  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(ROOT, "proxy", "agentbox-proxy")
SUSPICIOUS = os.path.join(ROOT, "proxy", "agentbox-suspicious")
FEEDS = os.path.join(ROOT, "proxy", "agentbox-feeds")
AGENTBOX = os.path.join(ROOT, "bin", "agentbox")

WATCHLIST = "# Testliste\nexfil.example    exfil    # Testvorfall 2026\n"
LEGITIMATE = "# legitim\nregistry.example\ncdn.vendor.example\npnpm.io\n"


def write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def feed_list(entries):
    """A list as agentbox-feeds stores it: sorted bytewise, one domain per line."""
    return b"".join(e.encode() + b"\n" for e in sorted(set(entries)))


# --- Seam C: the proxy over a socket ------------------------------------------

class ProxyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.conf = os.path.join(self.dir, "conf")
        self.feeds = os.path.join(self.dir, "feeds")
        write(os.path.join(self.conf, "suspicious-domains"), WATCHLIST)
        write(os.path.join(self.conf, "legitimate-domains"), LEGITIMATE)
        write(os.path.join(self.conf, "allowlist.json"), '{"entries": []}\n')     # nothing allowed
        self.feed("threat-intelligence", ["bad.example", "registry.example", "cdn.registry.example", "vendor.example"])
        self.feed("newly-registered", ["fresh.example", "npmjs.com", "mirror.registry.example"])
        self.sock = os.path.join(self.dir, "proxy.sock")
        self.log_path = os.path.join(self.dir, "proxy.log")
        self.proxy = None

    def feed(self, name, entries):
        write(os.path.join(self.feeds, name), feed_list(entries))

    def harmless(self, *domains):
        write(os.path.join(self.conf, "harmless"), "".join(d + "\n" for d in domains))

    def open_web(self):
        write(os.path.join(self.conf, "open-test"), f"{int(time.time()) + 600}\n")

    def start(self):
        env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8",
               "AGENTBOX_CONF_DIR": self.conf, "AGENTBOX_FEEDS_DIR": self.feeds}
        with open(self.log_path, "wb") as err:
            self.proxy = subprocess.Popen([sys.executable, "-I", PROXY, "test", self.sock], env=env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 15
        while "] ready" not in self.log_text():
            if self.proxy.poll() is not None or time.monotonic() > deadline:
                self.fail("proxy did not start: " + self.log_text())
            time.sleep(0.02)

    def stop(self):
        if self.proxy and self.proxy.poll() is None:
            self.proxy.terminate()
            self.proxy.wait(10)

    def log_text(self):
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def connect(self, host):
        """The proxy's answer to a ClientHello for host (nothing is allowed here: an alert)."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.sock)
            s.sendall(client_hello(host))
            data = b""
            while chunk := s.recv(4096):
                data += chunk
        return data

    def alert(self, host):
        """A ClientHello, then the Signals of the Alert the proxy logged for it (None: no Alert)."""
        before = len(self.log_text().splitlines())
        self.assertEqual(self.connect(host), ACCESS_DENIED)
        tags = None
        for line in self.log_text().splitlines()[before:]:
            if f"] ALERT {host} [" in line:
                tags = line.rsplit("[", 1)[1].rstrip("]").split(",")
        return tags

    def blocked_reason(self, host):
        reasons = [line.rsplit(" (", 1)[1].rstrip(")") for line in self.log_text().splitlines()
                   if f"] BLOCKED {host} (" in line]
        return reasons[-1] if reasons else None


class FeedSignalsTest(ProxyTest):
    def test_newly_registered_domain_raises_the_young_signal(self):
        self.start()
        self.assertEqual(self.alert("fresh.example"), ["young"])
        self.assertEqual(self.alert("www.fresh.example"), ["young"])

    def test_threat_list_is_part_of_the_watchlist_by_host_suffix(self):
        self.start()
        self.assertEqual(self.alert("bad.example"), ["threat"])
        self.assertEqual(self.alert("a.b.bad.example"), ["threat"])
        # a name ending in the entry is not below it
        self.assertIsNone(self.alert("notbad.example"))

    def test_hand_watchlist_by_host_suffix(self):
        self.start()
        self.assertEqual(self.alert("upload.exfil.example"), ["exfil"])

    def test_legitimate_domains_lift_list_entries_for_themselves_and_below(self):
        # registry.example and cdn.vendor.example are legitimate (file), npmjs.com too
        # (built in); the lists name them and a subdomain of one, and are stored as fetched
        self.start()
        for host in ("registry.example", "cdn.registry.example", "mirror.registry.example", "npmjs.com",
                     "cdn.vendor.example", "x.cdn.vendor.example"):
            with self.subTest(host=host):
                self.assertIsNone(self.alert(host))
        # vendor.example, a parent of cdn.vendor.example, is no Legitimate domain: its entry
        # counts for it and its other subdomains
        for host in ("vendor.example", "api.vendor.example"):
            with self.subTest(host=host):
                self.assertEqual(self.alert(host), ["threat"])

    def test_threat_list_hit_stays_blocked_in_open_web(self):
        self.open_web()
        self.start()
        self.assertEqual(self.connect("x.bad.example"), ACCESS_DENIED)
        self.assertEqual(self.blocked_reason("x.bad.example"), "suspicious: threat")

    def test_replaced_list_is_used_without_restart(self):
        self.start()
        self.assertIsNone(self.alert("later.example"))
        self.feed("threat-intelligence", ["bad.example", "later.example"])
        self.assertEqual(self.alert("later.example"), ["threat"])

    def test_without_lists_there_are_no_list_signals(self):
        shutil.rmtree(self.feeds)
        self.start()
        self.assertIsNone(self.alert("fresh.example"))
        self.assertEqual(self.alert("upload.exfil.example"), ["exfil"])


class LookalikeTest(ProxyTest):
    def test_names_of_five_or_more_characters_are_typo_checked(self):
        self.start()
        self.assertEqual(self.alert("npnjs.com"), ["lookalike"])      # five characters, npmjs
        self.assertEqual(self.alert("githbu.com"), ["lookalike"])     # six characters, github

    def test_shorter_names_are_not_typo_checked(self):
        # pypx is one edit away from pypi, but has four characters
        self.start()
        self.assertIsNone(self.alert("pypx.com"))
        self.assertIsNone(self.alert("bunny.example"))                # bun is too short as a template

    def test_legitimate_domains_get_no_pattern_signals(self):
        self.start()
        self.assertIsNone(self.alert("pnpm.io"))                       # ends with npm, but legitimate
        self.assertEqual(self.alert("pnpm.example"), ["lookalike"])

    def test_the_blockchain_pattern_stays_for_legitimate_domains(self):
        # of the Host and built in; it stays blocked in Open web
        self.open_web()
        self.start()
        self.assertEqual(self.alert("rpc.pnpm.io"), ["blockchain"])
        self.assertEqual(self.alert("mainnet.github.io"), ["blockchain"])
        self.assertEqual(self.blocked_reason("mainnet.github.io"), "suspicious: blockchain")


class HarmlessTest(ProxyTest):
    def test_harmless_mark_suppresses_alerts_without_restart(self):
        self.start()
        self.assertEqual(self.alert("githbu.com"), ["lookalike"])
        self.harmless("githbu.com")
        self.assertIsNone(self.alert("githbu.com"))
        self.assertIsNone(self.alert("www.githbu.com"))
        self.assertEqual(self.alert("githbu.org"), ["lookalike"])

    def test_harmless_mark_covers_list_signals(self):
        self.harmless("bad.example", "fresh.example")
        self.start()
        self.assertIsNone(self.alert("bad.example"))
        self.assertIsNone(self.alert("fresh.example"))

    def test_harmless_domain_stays_blocked_in_open_web(self):
        # the mark silences Alerts; it does not open a way out
        self.harmless("bad.example")
        self.open_web()
        self.start()
        self.assertEqual(self.connect("bad.example"), ACCESS_DENIED)
        self.assertEqual(self.blocked_reason("bad.example"), "suspicious: threat")
        self.assertNotIn("ALERT bad.example", self.log_text())

    def test_raw_ips_and_metadata_addresses_stay_blocked_in_open_web(self):
        # strong Signals: Open web lets them nowhere, without resolving anything
        self.open_web()
        self.start()
        # an address in any form a resolver reads as one: 1.2.3.4 as one number, in hex, in three parts
        for host, why in (("10.0.0.1", "suspicious: raw-ip"), ("169.254.169.254", "suspicious: metadata,raw-ip"),
                          ("metadata.google.internal", "suspicious: metadata"), ("16909060", "suspicious: raw-ip"),
                          ("0x01020304", "suspicious: raw-ip"), ("1.2.3", "suspicious: raw-ip"),
                          ("cdn.example.0x1f", "suspicious: raw-ip"), ("cdn.example.123", "suspicious: raw-ip")):
            with self.subTest(host=host):
                self.assertEqual(self.connect(host), ACCESS_DENIED)
                self.assertEqual(self.blocked_reason(host), why)

    def test_signals_that_do_not_depend_on_the_name_still_raise_alerts(self):
        self.harmless("githbu.com", "metadata.google.internal")
        self.start()
        self.assertEqual(self.alert("metadata.google.internal"), ["metadata"])
        self.assertEqual(self.alert("169.254.169.254"), ["metadata", "raw-ip"])


# --- the reasons of the Signals (#69) ---------------------------------------------

class ReasonTest(unittest.TestCase):
    """Each Signal's reason: a stable code, its parameters (text) and the English text,
    which the Control Room words in its own language (the classifier with --json)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conf, self.feeds = os.path.join(tmp.name, "conf"), os.path.join(tmp.name, "feeds")
        write(os.path.join(self.conf, "suspicious-domains"), WATCHLIST + "drop.example paste\n")
        write(os.path.join(self.conf, "legitimate-domains"), LEGITIMATE)
        write(os.path.join(self.feeds, "threat-intelligence"), feed_list(["bad.example"]))
        write(os.path.join(self.feeds, "newly-registered"), feed_list(["fresh.example"]))

    def reasons(self, *args):
        r = subprocess.run([sys.executable, "-I", SUSPICIOUS, "--json", *args], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "AGENTBOX_CONF_DIR": self.conf,
                                "AGENTBOX_FEEDS_DIR": self.feeds}, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [(s["signal"], s["code"], s["params"], s["reason"]) for s in json.loads(r.stdout)["signals"]]

    def test_every_reason_has_a_code_parameters_and_english_text(self):
        cases = [
            (["16909060"], [("raw-ip", "raw-ip", {}, "IP address instead of a domain")]),
            (["169.254.169.254"], [("metadata", "metadata", {}, "address of a cloud metadata service"),
                                   ("raw-ip", "raw-ip", {}, "IP address instead of a domain")]),
            (["upload.exfil.example"], [("exfil", "watchlist", {"domain": "exfil.example", "source": "Testvorfall 2026"},
                                         "Watchlist: exfil.example (Testvorfall 2026)")]),
            (["x.drop.example"], [("paste", "watchlist", {"domain": "drop.example"}, "Watchlist: drop.example")]),
            (["x.bad.example"], [("threat", "threat-feed", {"domain": "bad.example"},
                                  "HaGeZi Threat Intelligence Feeds: bad.example")]),
            (["www.fresh.example"], [("young", "young-feed", {"domain": "fresh.example"},
                                      "newly registered (HaGeZi NRD): fresh.example")]),
            (["npm-cache.example"], [("lookalike", "brand", {"brand": "npm", "domain": "npm-cache.example"},
                                      "brand name npm in the foreign domain npm-cache.example")]),
            (["githbu.example"], [("lookalike", "typo", {"target": "github", "distance": "2"},
                                   "typo of github (distance 2)")]),
            (["thing.xyz"], [("tld", "tld", {"tld": "xyz"}, "unusual TLD .xyz")]),
            (["0123456789abcdef0123.example"], [("random-label", "random-label", {"label": "0123456789abcdef0123"},
                                                 "random-looking or very long label 0123456789abcdef0123")]),
            (["a.b.c.d.e.f.example"], [("many-labels", "many-labels", {}, "more than six labels")]),
            (["rpc.pnpm.io"], [("blockchain", "blockchain-pattern", {}, "pattern of a blockchain RPC service")]),
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(self.reasons(*args), want)

    def test_the_renamed_signals_are_strong_and_do_not_depend_on_the_name(self):
        # a Harmless mark does not suppress them (the classifier reads the file; the CLI marks domains only)
        write(os.path.join(self.conf, "harmless"), "metadata.google.internal\n10.0.0.1\n")
        for host, signal in (("metadata.google.internal", "metadata"), ("10.0.0.1", "raw-ip")):
            with self.subTest(host=host):
                r = subprocess.run([sys.executable, "-I", SUSPICIOUS, "--json", host],
                                   capture_output=True, text=True, timeout=30,
                                   env={"PATH": os.environ["PATH"], "AGENTBOX_CONF_DIR": self.conf,
                                        "AGENTBOX_FEEDS_DIR": self.feeds})
                result = json.loads(r.stdout)
                self.assertEqual([(s["signal"], s["strong"], s["suppressed"]) for s in result["signals"]],
                                 [(signal, True, False)])
                self.assertEqual((result["alert"], result["strong"], result["harmless"]), (True, True, host))
        write(os.path.join(self.conf, "watchlist.json"), '{"entries": []}\n')
        r = subprocess.run([sys.executable, "-I", SUSPICIOUS, "10.0.0.1"], capture_output=True, text=True, timeout=30,
                           env={"PATH": os.environ["PATH"], "AGENTBOX_CONF_DIR": self.conf,
                                "AGENTBOX_FEEDS_DIR": self.feeds})
        self.assertEqual((r.returncode, r.stdout), (2, "raw-ip\n"))


# --- self-test ------------------------------------------------------------------

class SelfTest(unittest.TestCase):
    def run_self_test(self, conf, classifier=SUSPICIOUS):
        r = subprocess.run([sys.executable, "-I", classifier, "--self-test"], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "AGENTBOX_CONF_DIR": conf},
                           timeout=120)
        return r.returncode, r.stdout, r.stderr

    def test_passes_with_the_lists_of_the_repository(self):
        code, out, err = self.run_self_test(os.path.join(ROOT, "proxy"))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Self-test of the detection passed", out)
        self.assertNotIn("FAILED", out)

    def test_fails_without_the_legitimate_domains(self):
        with tempfile.TemporaryDirectory() as conf:
            shutil.copy(os.path.join(ROOT, "proxy", "suspicious-domains"), conf)
            code, out, _ = self.run_self_test(conf)
        self.assertEqual(code, 1)
        self.assertIn("FAILED  lists of the Host: legitimate.json and legitimate-domains missing", out)

    def test_fails_when_detection_regresses(self):
        # a classifier that typo-checks short names again: the proxy next to it now
        # raises an Alert for pypx.example
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(PROXY, d)
            with open(SUSPICIOUS) as f:
                source = f.read()
            self.assertIn("MIN_TYPO = 5\n", source)
            write(os.path.join(d, "agentbox-suspicious"), source.replace("MIN_TYPO = 5\n", "MIN_TYPO = 4\n"), 0o755)
            code, out, _ = self.run_self_test(os.path.join(ROOT, "proxy"), os.path.join(d, "agentbox-suspicious"))
        self.assertEqual(code, 1)
        self.assertIn("FAILED  pypx.example", out)


# --- Seam A: the CLI ------------------------------------------------------------

FAKE_JOURNALCTL = '''#!PYTHON
"""Fake journalctl: prints the canned proxy log (one entry per line, as -o json
prints them; newest first with --reverse)."""
import os, sys
try:
    with open(os.path.join(DIR, "journal.txt")) as f:
        lines = f.read().splitlines()
except FileNotFoundError:
    lines = []
for line in reversed(lines) if "--reverse" in sys.argv else lines:
    print(line)
'''


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.root = os.path.join(self.dir, "root")
        self.conf = os.path.join(self.root, "etc", "agentbox")
        self.feeds = os.path.join(self.root, "var", "lib", "agentbox", "feeds")
        vm_records.write(self.root)
        write_passwd(self.root)
        write(os.path.join(self.conf, "suspicious-domains"), WATCHLIST)
        write(os.path.join(self.conf, "legitimate-domains"), LEGITIMATE)
        libexec = os.path.join(self.root, "usr", "libexec")
        os.makedirs(libexec)
        shutil.copy2(SUSPICIOUS, libexec)
        os.makedirs(os.path.join(self.dir, "bin"))
        with open(os.path.join(self.dir, "bin", "journalctl"), "w") as f:
            f.write(FAKE_JOURNALCTL.replace("PYTHON", sys.executable, 1).replace("DIR", repr(self.dir), 1))
        os.chmod(os.path.join(self.dir, "bin", "journalctl"), 0o755)

    def run_agentbox(self, *args):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
               "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "CET-1"}
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def feed(self, name, entries):
        write(os.path.join(self.feeds, name), feed_list(entries))

    def status(self, **feeds):
        write(os.path.join(self.feeds, "status.json"), json.dumps(feeds))

    def read_conf(self, name):
        with open(os.path.join(self.conf, name), "rb") as f:
            return f.read()


class CheckDomainTest(CliTest):
    def setUp(self):
        super().setUp()
        self.feed("newly-registered", ["fresh.example"])
        self.feed("threat-intelligence", ["bad.example"])
        self.status(**{"newly-registered": {"entries": 1, "updated": 1793700000, "error": None, "days": 14},
                       "threat-intelligence": {"entries": 1, "updated": 1793700000,
                                               "error": "tif-onlydomains.txt: HTTP 503"}})

    def test_text_names_each_signal_and_why(self):
        code, out, err = self.run_agentbox("check-domain", "www.fresh.example")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out,
                         "www.fresh.example: suspicious: Alert\n"
                         "  young         newly registered (HaGeZi NRD): fresh.example\n"
                         "Feeds: newly-registered 1 entries, as of 2026-11-03 11:00; threat-intelligence 1 "
                         "entries, as of 2026-11-03 11:00, last fetch failed (tif-onlydomains.txt: HTTP 503)\n")

    def test_strong_signals_and_their_source(self):
        code, out, _ = self.run_agentbox("check-domain", "upload.exfil.example")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:2],
                         ["upload.exfil.example: strongly suspicious: Alert, blocked in Open web, "
                          "allowed only with --force",
                          "  exfil         Watchlist: exfil.example (Testvorfall 2026)"])

    def test_unremarkable_and_legitimate_domains(self):
        self.assertEqual(self.run_agentbox("check-domain", "plain.example")[1].splitlines()[0],
                         "plain.example: unremarkable")
        self.assertEqual(self.run_agentbox("check-domain", "cdn.registry.example")[1].splitlines()[0],
                         "cdn.registry.example: unremarkable (known Legitimate domain registry.example)")

    def test_json_for_the_control_room(self):
        code, out, err = self.run_agentbox("check-domain", "bad.example", "--json")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["domain"], "bad.example")
        self.assertEqual(result["signals"], [{"signal": "threat", "strong": True, "suppressed": False,
                                              "reason": "HaGeZi Threat Intelligence Feeds: bad.example",
                                              "code": "threat-feed", "params": {"domain": "bad.example"}}])
        self.assertEqual((result["alert"], result["strong"], result["harmless"], result["legitimate"]),
                         (True, True, None, None))
        self.assertEqual(result["feeds"]["newly-registered"],
                         {"present": True, "entries": 1, "updated": 1793700000, "attempted": None, "error": None,
                          "days": 14})

    def test_missing_lists_are_reported(self):
        shutil.rmtree(self.feeds)
        code, out, _ = self.run_agentbox("check-domain", "fresh.example")
        self.assertEqual(code, 0)
        self.assertEqual(out, "fresh.example: unremarkable\nFeeds: newly-registered missing; threat-intelligence missing\n")

    def test_rejects_invalid_domains_and_options(self):
        for args, err in ((["Example.org"], "invalid domain: Example.org"), (["1.2.3.4"], "invalid domain: 1.2.3.4"),
                          ([], "invalid domain: "),
                          (["example.org", "--force"], "unknown option: --force"),
                          (["example.org", "other.org"], "too many arguments: other.org")):
            with self.subTest(args=args):
                self.assertEqual(self.run_agentbox("check-domain", *args), (1, "", f"agentbox: {err}\n"))
        # with --json the code and the domain for the Control Room
        code, out, err = self.run_agentbox("check-domain", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err), {"error": "invalid_domain", "message": "invalid domain: --json",
                                           "params": {"domain": "--json"}})


HEADER = b"# Domains with a Harmless mark, with their subdomains (agentbox mark-harmless/unmark-harmless)\n"


class WatchlistCliTest(CliTest):
    def test_json_lists_the_hand_watchlist_and_the_feed_state(self):
        write(os.path.join(self.conf, "suspicious-domains"),
              WATCHLIST + "jsonkeeper.example payload # Contagious Interview\nnosource.example tunnel\n")
        self.feed("threat-intelligence", ["bad.example"])
        self.status(**{"threat-intelligence": {"entries": 1, "updated": 1793700000, "attempted": 1793710000,
                                               "error": "tif-onlydomains.txt: HTTP 503"}})
        code, out, err = self.run_agentbox("watchlist", "--json")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual([{k: e[k] for k in ("domain", "category", "strong", "source", "note", "added_by")}
                          for e in result["entries"]], [
            {"domain": "exfil.example", "category": "exfil", "strong": True, "source": "Testvorfall 2026",
             "note": "", "added_by": "migration"},
            {"domain": "jsonkeeper.example", "category": "payload", "strong": False,
             "source": "Contagious Interview", "note": "", "added_by": "migration"},
            {"domain": "nosource.example", "category": "tunnel", "strong": True, "source": "", "note": "",
             "added_by": "migration"}])
        self.assertEqual(result["feeds"], {
            "threat-intelligence": {"present": True, "entries": 1, "updated": 1793700000, "attempted": 1793710000,
                                    "error": "tif-onlydomains.txt: HTTP 503", "days": None},
            "newly-registered": {"present": False, "entries": None, "updated": None, "attempted": None,
                                 "error": None, "days": None}})
        self.assertEqual((result["feed_interval_hours"], result["young_domain_days"]), (12, 14))

    def test_text_and_settings(self):
        self.run_agentbox("set-settings", "feed_interval_hours=6")
        code, out, _ = self.run_agentbox("watchlist")
        self.assertEqual(code, 0)
        self.assertEqual(out, "exfil.example                    exfil       Testvorfall 2026\n"
                              "Feeds: newly-registered missing; threat-intelligence missing\n")
        self.assertEqual(json.loads(self.run_agentbox("watchlist", "--json")[1])["feed_interval_hours"], 6)

    def test_rejects_arguments(self):
        self.assertEqual(self.run_agentbox("watchlist", "--all"), (1, "", "agentbox: unknown option: --all\n"))


class HarmlessCliTest(CliTest):
    def test_mark_creates_the_list(self):
        self.assertEqual(self.run_agentbox("mark-harmless", "githbu.com"), (0, "githbu.com marked harmless\n", ""))
        self.assertEqual(self.read_conf("harmless"), HEADER + b"githbu.com\n")
        self.assertEqual(os.stat(os.path.join(self.conf, "harmless")).st_mode & 0o777, 0o644)

    def test_mark_is_idempotent_and_appends(self):
        self.run_agentbox("mark-harmless", "githbu.com")
        self.run_agentbox("mark-harmless", "githbu.com")
        self.run_agentbox("mark-harmless", "fresh.example")
        self.assertEqual(self.read_conf("harmless"), HEADER + b"githbu.com\nfresh.example\n")

    def test_list_and_unmark(self):
        self.run_agentbox("mark-harmless", "githbu.com")
        self.run_agentbox("mark-harmless", "fresh.example")
        self.assertEqual(self.run_agentbox("harmless"), (0, "githbu.com\nfresh.example\n", ""))
        self.assertEqual(json.loads(self.run_agentbox("harmless", "--json")[1]),
                         [{"domain": "githbu.com", "regex": None, "refused": None},
                          {"domain": "fresh.example", "regex": None, "refused": None}])
        self.assertEqual(self.run_agentbox("unmark-harmless", "githbu.com"),
                         (0, "githbu.com no longer marked harmless\n", ""))
        self.assertEqual(self.read_conf("harmless"), HEADER + b"fresh.example\n")
        self.assertEqual(self.run_agentbox("unmark-harmless", "other.example", "--json"),
                         (0, '{"domain": "other.example", "regex": null, '
                             '"message": "other.example no longer marked harmless"}\n', ""))

    def test_without_list(self):
        self.assertEqual(self.run_agentbox("harmless", "--json"), (0, "[]\n", ""))
        self.assertEqual(self.run_agentbox("unmark-harmless", "githbu.com")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.conf, "harmless")))

    def test_json(self):
        code, out, err = self.run_agentbox("mark-harmless", "githbu.com", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"domain": "githbu.com", "regex": None, "message": "githbu.com marked harmless"})

    def test_check_domain_shows_the_mark(self):
        self.feed("threat-intelligence", ["bad.example"])
        self.run_agentbox("mark-harmless", "bad.example")
        code, out, _ = self.run_agentbox("check-domain", "www.bad.example")
        self.assertEqual(out.splitlines()[:2],
                         ["www.bad.example: marked harmless (bad.example): no Alert, stays blocked in Open web, "
                          "allowed only with --force",
                          "  threat        HaGeZi Threat Intelligence Feeds: bad.example  [suppressed]"])

    def test_harmless_mark_does_not_lift_the_force_rule_of_allow(self):
        self.run_agentbox("mark-harmless", "githbu.com")
        self.assertEqual(self.run_agentbox("allow", "work", "githbu.com"),
                         (1, "", "agentbox: githbu.com is suspicious (lookalike). Allowing it only with --force\n"))

    def test_rejects_invalid_domains(self):
        for d in ("Example.org", "10.0.0.1", "-x.org", "example.org.", ""):
            with self.subTest(domain=d):
                self.assertEqual(self.run_agentbox("mark-harmless", d), (1, "", f"agentbox: invalid domain: {d}\n"))
        self.assertEqual(self.run_agentbox("mark-harmless", "githbu.com", "--force"),
                         (1, "", "agentbox: unknown option: --force\n"))
        self.assertFalse(os.path.exists(os.path.join(self.conf, "harmless")))


class SeenTest(CliTest):
    def journal(self, *alerts):
        """Proxy log of `work` with one Alert per (seconds from now, host, tags)."""
        now = time.time()
        with open(os.path.join(self.dir, "journal.txt"), "w") as f:
            for offset, host, tags in alerts:
                f.write(json.dumps({"__REALTIME_TIMESTAMP": str(int((now + offset) * 10**6)),
                                    "SYSLOG_IDENTIFIER": "agentbox-proxy-work",
                                    "MESSAGE": f"[work] ALERT {host} [{tags}]", **proxy("work")}) + "\n")

    def seen_files(self):
        state = os.path.join(self.root, "var", "lib", "agentbox")
        return sorted(f for f in os.listdir(state) if f.startswith("alerts-seen")) if os.path.isdir(state) else []

    def alerts(self):
        code, out, err = self.run_agentbox("alerts", "work", "--json")
        self.assertEqual((code, err), (0, ""))
        return {e["domain"]: e["seen"] for e in json.loads(out)}

    def test_alerts_are_new_until_marked_seen(self):
        self.journal((-600, "a.example", "young"), (-300, "b.example", "lookalike"))
        self.assertEqual(self.alerts(), {"a.example": False, "b.example": False})
        code, out, err = self.run_agentbox("mark-seen", "work")
        self.assertEqual((code, out, err), (0, "work: Alerts marked as seen\n", ""))
        self.assertEqual(self.alerts(), {"a.example": True, "b.example": True})
        code, out, _ = self.run_agentbox("alerts", "work")
        self.assertTrue(out.splitlines()[0].endswith("lookalike  (seen)"), out)

    def test_a_later_alert_is_new_again(self):
        self.journal((-600, "a.example", "young"))
        self.run_agentbox("mark-seen", "work")
        self.journal((-600, "a.example", "young"), (60, "a.example", "young"))
        self.assertEqual(self.alerts(), {"a.example": False})

    def test_marking_one_destination(self):
        self.journal((-600, "a.example", "young"), (-300, "b.example", "lookalike"))
        code, out, err = self.run_agentbox("mark-seen", "work", "a.example", "--json")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual((result["vm"], result["domain"], result["message"]),
                         ("work", "a.example", "work: Alerts for a.example marked as seen"))
        self.assertAlmostEqual(result["seen"], time.time(), delta=60)
        self.assertEqual(self.alerts(), {"a.example": True, "b.example": False})
        # the marks belong to this VM only
        self.assertEqual(self.seen_files(), ["alerts-seen-work.json"])

    def test_an_address_in_a_form_only_a_resolver_reads_can_be_marked(self):
        # the classifier raises raw-ip for them (is_address); garbage of the same characters stays refused
        self.journal((-600, "16909060", "raw-ip"), (-500, "0x01020304", "raw-ip"), (-400, "b.example", "young"))
        for host in ("16909060", "0x01020304"):
            self.assertEqual(self.run_agentbox("mark-seen", "work", host)[0], 0, host)
        self.assertEqual(self.alerts(), {"16909060": True, "0x01020304": True, "b.example": False})
        for bad in ("0xg1", "abc", "..", "1" * 254):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_agentbox("mark-seen", "work", bad)[:2], (1, ""))

    def test_alerts_of_harmless_destinations_are_left_out(self):
        # a Signal that does not depend on the name (metadata) keeps its Alert
        self.journal((-600, "githbu.com", "lookalike"), (-500, "www.githbu.com", "lookalike"),
                     (-400, "x.githbu.com", "metadata"), (-300, "b.example", "young"))
        self.run_agentbox("mark-harmless", "githbu.com")
        self.assertEqual(self.alerts(), {"x.githbu.com": False, "b.example": False})

    def test_rejects_invalid_arguments(self):
        for args, err in ((["dev"], "No such VM: dev"), ([], "no VM given"),
                          (["work", "Bad.example"], "invalid domain: Bad.example"),
                          (["work", "a.example", "b.example"], "too many arguments: b.example"),
                          (["work", "--all"], "unknown option: --all")):
            with self.subTest(args=args):
                self.assertEqual(self.run_agentbox("mark-seen", *args), (1, "", f"agentbox: {err}\n"))
        self.assertEqual(self.seen_files(), [])

    def test_an_address_can_be_marked(self):
        self.journal((-600, "169.254.169.254", "metadata,raw-ip"))
        self.assertEqual(self.run_agentbox("mark-seen", "work", "169.254.169.254")[0], 0)
        self.assertEqual(self.alerts(), {"169.254.169.254": True})


# --- the fetcher ----------------------------------------------------------------

TIF = "/hagezi/dns-blocklists/main/wildcard/tif-onlydomains.txt"
NRD7 = "/hagezi/nrd/main/domains/nrd7.txt"
NRD14 = "/hagezi/nrd/main/domains/nrd14-8.txt"


def hagezi(title, entries, declared=None):
    head = (f"# Title: {title}\n# Description: Test\n# Version: 2026.1103.0917.44\n"
            f"# Number of entries: {len(entries) if declared is None else declared}\n#\n")
    return (head + "".join(e + "\n" for e in entries)).encode()


TIF_TITLE = "HaGeZi's Threat Intelligence Feeds - a serious security boost (recommended)"
NRD_TITLE = "Newly Registered Domains (NRD)"


class FakeHaGeZi(http.server.BaseHTTPRequestHandler):
    """Answers from server.files: path -> (status, body, headers); gzip when asked."""

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Accept-Encoding")))
        status, body, headers = self.server.files.get(self.path, (404, b"Not Found", {}))
        if "gzip" in (self.headers.get("Accept-Encoding") or "") and status == 200 and not headers:
            body = gzip.compress(body)
            headers = {"Content-Encoding": "gzip"}
        self.send_response(status)
        for k, v in {"Content-Length": str(len(body)), **headers}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs = tempfile.TemporaryDirectory()
        cls.cert = os.path.join(cls.certs.name, "cert.pem")
        key = os.path.join(cls.certs.name, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", key, "-out", cls.cert], check=True, capture_output=True)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHaGeZi)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cls.cert, key)
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.certs.cleanup()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.conf = os.path.join(self.dir, "conf")
        self.feeds = os.path.join(self.dir, "feeds")
        os.makedirs(self.feeds)
        write(os.path.join(self.conf, "legitimate-domains"), LEGITIMATE)
        self.server.requests = []
        self.server.files = {
            TIF: (200, hagezi(TIF_TITLE, ["zz.example", "bad.example", "registry.example", "a.cdn.registry.example",
                                          "vendor.example", "aa.example"]), {}),
            NRD7: (200, hagezi(NRD_TITLE, ["fresh.example", "new.example", "npmjs.com"]), {}),
            NRD14: (200, hagezi(NRD_TITLE, ["fresh.example", "older.example"]), {}),
        }

    def fetch(self, base=None, trusted=True, **env):
        environ = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "AGENTBOX_CONF_DIR": self.conf,
                   "AGENTBOX_FEEDS_DIR": self.feeds, "AGENTBOX_FEEDS_MIN_ENTRIES": "1",
                   "AGENTBOX_FEEDS_BASE": base or f"https://127.0.0.1:{self.server.server_address[1]}", **env}
        if trusted:
            environ["SSL_CERT_FILE"] = self.cert
        r = subprocess.run([sys.executable, "-I", FEEDS], capture_output=True, text=True, env=environ, timeout=120)
        return r.returncode, r.stdout, r.stderr

    def stored(self, name):
        with open(os.path.join(self.feeds, name), "rb") as f:
            return f.read()

    def status(self):
        with open(os.path.join(self.feeds, "status.json")) as f:
            return json.load(f)

    def previous_lists(self):
        write(os.path.join(self.feeds, "threat-intelligence"), b"old-bad.example\n")
        write(os.path.join(self.feeds, "newly-registered"), b"old-new.example\n")
        write(os.path.join(self.feeds, "status.json"), json.dumps({
            "threat-intelligence": {"entries": 1, "updated": 1793700000, "error": None},
            "newly-registered": {"entries": 1, "updated": 1793700000, "error": None, "days": 14}}))

    def test_young_domain_days_of_the_settings_choose_the_bands(self):
        # the generated unit passes young_domain_days as AGENTBOX_FEEDS_YOUNG_DAYS
        code, out, err = self.fetch(AGENTBOX_FEEDS_YOUNG_DAYS="7")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.stored("newly-registered"), b"fresh.example\nnew.example\nnpmjs.com\n")
        self.assertEqual(self.status()["newly-registered"]["days"], 7)
        self.assertEqual(self.fetch(AGENTBOX_FEEDS_YOUNG_DAYS="14")[0], 0)
        self.assertEqual(self.status()["newly-registered"]["days"], 14)

    def test_stores_sorted_lists_as_fetched(self):
        code, out, err = self.fetch()
        self.assertEqual((code, err), (0, ""))
        # also the entries matching Legitimate domains (the classifier lifts them when it
        # classifies, so a removed Legitimate domain counts at once)
        self.assertEqual(self.stored("threat-intelligence"), b"a.cdn.registry.example\naa.example\nbad.example\n"
                                                             b"registry.example\nvendor.example\nzz.example\n")
        # two bands of seven days merged, no duplicates
        self.assertEqual(self.stored("newly-registered"), b"fresh.example\nnew.example\nnpmjs.com\nolder.example\n")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(out, "threat-intelligence: 6 entries (version 2026.1103.0917.44); "
                              "newly-registered: 4 entries (version 2026.1103.0917.44, 2026.1103.0917.44)\n")
        status = self.status()
        self.assertEqual(status["newly-registered"]["days"], 14)
        self.assertEqual((status["threat-intelligence"]["entries"], status["threat-intelligence"]["error"]), (6, None))
        self.assertAlmostEqual(status["threat-intelligence"]["updated"], time.time(), delta=60)
        self.assertEqual(sorted(os.listdir(self.feeds)), [".lock", "newly-registered", "status.json",
                                                          "threat-intelligence"])
        # compressed transfer, from HaGeZi's paths
        self.assertEqual(sorted(self.server.requests), sorted([(NRD14, "gzip"), (NRD7, "gzip"), (TIF, "gzip")]))

    def test_proxy_finds_entries_in_what_was_fetched(self):
        self.fetch()
        # with a readable Watchlist (without one the classifier cannot tell what is strong)
        write(os.path.join(self.conf, "watchlist.json"), '{"entries": []}\n')
        r = subprocess.run([sys.executable, "-I", SUSPICIOUS, "www.bad.example"], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "AGENTBOX_CONF_DIR": self.conf,
                                "AGENTBOX_FEEDS_DIR": self.feeds})
        self.assertEqual((r.returncode, r.stdout), (2, "threat\n"))

    def assert_kept(self, out, name="threat-intelligence", file="tif-onlydomains.txt", why=""):
        old = b"old-bad.example\n" if name == "threat-intelligence" else b"old-new.example\n"
        self.assertEqual(self.stored(name), old)
        self.assertIn(f"{name}: FAILED {file}: {why}", out)
        self.assertIn("the previous list stays (1 entries of ", out)
        status = self.status()[name]
        self.assertEqual((status["entries"], status["updated"]), (1, 1793700000))
        self.assertTrue(status["error"].startswith(f"{file}: {why}"), status["error"])
        self.assertEqual([f for f in os.listdir(self.feeds) if f.startswith(".") and f != ".lock"], [])

    def test_failed_download_keeps_the_previous_list(self):
        self.previous_lists()
        self.server.files[TIF] = (503, b"busy", {})
        code, out, err = self.fetch()
        self.assertEqual((code, err), (1, ""))
        self.assert_kept(out, why="HTTP 503")
        self.assertEqual(len(out.splitlines()), 1)
        # the other list is updated anyway
        self.assertEqual(self.stored("newly-registered"), b"fresh.example\nnew.example\nnpmjs.com\nolder.example\n")

    def test_truncated_list_keeps_the_previous_one(self):
        self.previous_lists()
        self.server.files[TIF] = (200, hagezi(TIF_TITLE, ["bad.example"], declared=1814363), {})
        code, out, _ = self.fetch()
        self.assertEqual(code, 1)
        self.assert_kept(out, why="1 entries instead of 1814363, incomplete")

    def test_something_else_than_the_list_keeps_the_previous_one(self):
        self.previous_lists()
        for body, why in ((b"<html><body>Rate limited</body></html>\n", "header does not match"),
                          (hagezi("HaGeZi's Pro DNS Blocklist", ["bad.example"]), "header does not match"),
                          (hagezi(TIF_TITLE, ["bad.example", "<script>", "1.2.3.4"]), "2 of 3 lines")):
            with self.subTest(why=why):
                self.server.files[TIF] = (200, body, {})
                code, out, _ = self.fetch()
                self.assertEqual(code, 1)
                self.assert_kept(out, why=why)

    def test_one_failed_band_keeps_the_whole_previous_list(self):
        self.previous_lists()
        del self.server.files[NRD14]
        code, out, _ = self.fetch()
        self.assertEqual(code, 1)
        self.assert_kept(out, "newly-registered", "nrd14-8.txt", "HTTP 404")
        self.assertEqual(self.stored("threat-intelligence"), b"a.cdn.registry.example\naa.example\nbad.example\n"
                                                             b"registry.example\nvendor.example\nzz.example\n")

    def test_too_large_keeps_the_previous_list(self):
        self.previous_lists()
        code, out, _ = self.fetch(AGENTBOX_FEEDS_MAX_BYTES="100")
        self.assertEqual(code, 1)
        self.assert_kept(out, why="larger than 100 bytes")

    def test_too_few_entries_keep_the_previous_list(self):
        self.previous_lists()
        code, out, _ = self.fetch(AGENTBOX_FEEDS_MIN_ENTRIES="7")
        self.assertEqual(code, 1)
        self.assert_kept(out, why="only 6 entries")

    def test_broken_gzip_keeps_the_previous_list(self):
        self.previous_lists()
        data = gzip.compress(hagezi(TIF_TITLE, ["bad.example"]))
        self.server.files[TIF] = (200, data[:len(data) // 2], {"Content-Encoding": "gzip"})
        code, out, _ = self.fetch()
        self.assertEqual(code, 1)
        self.assert_kept(out, why="gzip data incomplete")

    def test_untrusted_certificate_keeps_the_previous_list(self):
        self.previous_lists()
        code, out, _ = self.fetch(trusted=False)
        self.assertEqual(code, 1)
        self.assert_kept(out, why="cannot be reached")
        self.assertIn("CERTIFICATE_VERIFY_FAILED", out)

    def test_redirect_away_from_https_is_refused(self):
        self.previous_lists()
        self.server.files[TIF] = (302, b"", {"Location": "http://127.0.0.1:1/tif.txt"})
        code, out, _ = self.fetch()
        self.assertEqual(code, 1)
        self.assert_kept(out, why="redirect without HTTPS")

    def test_only_https(self):
        code, out, err = self.fetch(base="http://127.0.0.1:1")
        self.assertEqual((code, out, err), (2, "", "agentbox-feeds: HTTPS only\n"))
        self.assertEqual(os.listdir(self.feeds), [])

    def test_first_run_failing_says_so(self):
        self.server.files[TIF] = (503, b"busy", {})
        code, out, _ = self.fetch()
        self.assertEqual(code, 1)
        self.assertIn("threat-intelligence: FAILED tif-onlydomains.txt: HTTP 503, no list yet", out)
        self.assertFalse(os.path.exists(os.path.join(self.feeds, "threat-intelligence")))


if __name__ == "__main__":
    unittest.main()
