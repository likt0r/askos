"""Regex entries in the Allowlist, the Watchlist, the Legitimate domains and the Harmless
marks (#2, ADR 0013).

The rules a pattern has to follow and the breadth check are the classifier's
(proxy/agentbox-suspicious, pattern_problem), which the proxy loads and agentbox loads the
same way (agentbox/patterns.py).

Seam C: the real proxy (`proxy/agentbox-proxy`) with the real classifier next to it, on a
Unix socket with a temporary configuration, as in test_allowlist.py, test_lists.py and
test_detection.py: a regex Allowlist entry never lets a destination with strong Signals
through, checked on every connection.

Seam A: the `agentbox` CLI against a temporary root (AGENTBOX_ROOT) with the real
classifier, a fake journalctl and a stand-in for journald's socket.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_allowlist as allowlist_tests  # noqa: E402
import test_detection as detection_tests  # noqa: E402
import test_lists as lists_tests  # noqa: E402
import test_vms as vms_tests  # noqa: E402
from journal_writers import proxy  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUSPICIOUS = os.path.join(REPO, "proxy", "agentbox-suspicious")
PROXY = os.path.join(REPO, "proxy", "agentbox-proxy")
write = allowlist_tests.write
proxy_line = allowlist_tests.proxy_line
SETTLE = allowlist_tests.SETTLE
# mirrors of a vendor, numbered: the kind of family a pattern is for
MIRRORS = r"^mirror[0-9]+\.vendor\.example$"
PATTERN = r"^mirror[0-9]+\.(vendor|exfil)\.example$"


def load(path, name):
    """A program of proxy/ as a module, the way the proxy and agentbox load the classifier."""
    module = types.ModuleType(name)
    module.__file__ = path
    with open(path) as f:
        exec(compile(f.read(), path, "exec"), module.__dict__)
    return module


RULES = load(SUSPICIOUS, "agentbox_suspicious_rules")


def rx(pattern, scope="all", expires=None, note=""):
    """An Allowlist entry with a pattern in place of the domain."""
    return {"regex": pattern, "scope": scope, "expires": expires, "note": note, "added": 1793700000,
            "added_by": "bewr"}


def watch_rx(pattern, category="exfil", source="", note=""):
    return {"regex": pattern, "category": category, "source": source, "note": note, "added": 1793700000,
            "added_by": "bewr"}


def legit_rx(pattern, note=""):
    return {"regex": pattern, "note": note, "added": 1793700000, "added_by": "bewr"}


# --- the rules ------------------------------------------------------------------------

class RulesTest(unittest.TestCase):
    def reason(self, pattern, broad=True):
        why = RULES.pattern_problem(pattern, broad)
        return why and why[0]

    def test_the_patterns_the_issue_names(self):
        self.assertIsNone(self.reason(r"^npm-[a-z]+\.(com|net)$"))
        for pattern in (".*", r".*\.com", r"[a-z]+\.[a-z]+"):
            with self.subTest(pattern=pattern):
                self.assertEqual(RULES.pattern_problem(pattern)[0::2], ("too-broad", "google.com"))
                # the Watchlist does without the breadth check: there it only raises false Alerts
                self.assertIsNone(self.reason(pattern, broad=False))

    def test_each_rule(self):
        cases = {
            "x" * 201: "too-long",
            "a b": "characters", "ä.example": "characters", "": "characters", "a\nb": "characters",
            "(": "syntax", "[a-": "syntax", "a{2,1}": "syntax", "[[a]x": "syntax",        # re warns: refused
            "NPM-x": "uppercase", "[A-Z]+": "uppercase",
            r"(a)\1": "backreference", r"(?P<x>a)(?P=x)": "backreference", r"(a)?(?(1)b|c)": "backreference",
            r"(?=a)b": "lookaround", r"(?!a)b": "lookaround", r"(?<=a)b": "lookaround", r"(?<!a)b": "lookaround",
            r"(a+)+b": "nested-quantifier", r"(a*)*b": "nested-quantifier", r"(a|ab)+c": "nested-quantifier",
            r"(?:a|a)*b": "nested-quantifier", r"([a-z]+\.)+example\.org": "nested-quantifier",
            r"a*a*a*a*b": "too-complex", r".*a.*b.*c": "too-complex", "a?" * 16 + "a" * 16: "too-complex",
        }
        for pattern, reason in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(self.reason(pattern, broad=False), reason)
                self.assertIn(RULES.PATTERN_REASONS[reason], RULES.pattern_problem(pattern, False)[1])

    def test_what_the_rules_let_through(self):
        for pattern in (MIRRORS, PATTERN, r"(?i)^NPM-[a-z]+\.example$", r"^[a-z0-9-]+\.s3\.[a-z0-9-]+\.example\.com$",
                        r"^.*foo.*\.example$", r"^(eu|us)-[0-9]{1,3}\.vendor\.example$", r"^(ab){3}\.example$",
                        r"^(.*\.)?vendor\.example$", "x" * 200):
            with self.subTest(pattern=pattern):
                self.assertIsNone(RULES.pattern_problem(pattern))

    def test_matching_is_fullmatch_on_host_names(self):
        compiled = RULES.compile_pattern(MIRRORS)
        self.assertTrue(RULES.pattern_matches(compiled, "mirror1.vendor.example"))
        for host in ("x.mirror1.vendor.example", "mirror1.vendor.example.org", "mirror.vendor.example"):
            with self.subTest(host=host):
                self.assertFalse(RULES.pattern_matches(compiled, host))
        # no host is tried that is longer than a host name can be, or has other characters
        anything = re.compile(r"[a-z.]+|.*")
        self.assertTrue(RULES.pattern_matches(anything, "a" * 249 + ".com"))
        self.assertFalse(RULES.pattern_matches(anything, "a" * 250 + ".com"))
        self.assertFalse(RULES.pattern_matches(anything, "a b.com"))
        self.assertFalse(RULES.pattern_matches(anything, "ä.com"))

    def test_what_the_rules_let_through_matches_hostile_hosts_quickly(self):
        # near the limit of the rules (found by fuzzing them), against hosts made to backtrack
        for pattern in (r".*a.*b", r"[^.]{0,5}[ab]{0,5}[a-z0-9-]+?.?.{1,3}[ab]+?", r"[a-z0-9-]*[^.]*?([ab])+?",
                        "a?" * 15 + "a" * 15, r"(com|co)(com|co)(com|co)x"):
            self.assertIsNone(self.reason(pattern, broad=False), pattern)
            compiled = re.compile(pattern)
            for host in ("a" * 253, "a" * 252 + "!", "ab" * 126, "co" * 126, "a." * 126):
                with self.subTest(pattern=pattern, host=host[:10]):
                    start = time.perf_counter()
                    compiled.fullmatch(host)
                    self.assertLess(time.perf_counter() - start, 1.0)


# --- Seam C: a regex Allowlist entry in the proxy ---------------------------------------

class AllowlistProxyTest(allowlist_tests.ProxyTest):
    """The proxy of VM work; the Watchlist (its text file) has exfil.example (exfil)."""

    def test_a_pattern_allows_what_it_matches_and_the_log_names_it(self):
        self.store(rx(MIRRORS))
        self.start()
        self.tunnel("mirror1.vendor.example")
        self.tunnel("MIRROR22.vendor.example.")                # lowercased, without its trailing dot
        self.assertIn(f"[work] ALLOWED mirror1.vendor.example (allowlist regex {MIRRORS} all)\n", self.log())
        self.assertIn(f"[work] ALLOWED mirror22.vendor.example (allowlist regex {MIRRORS} all)\n", self.log())
        # the whole name: no subdomains, nothing around it
        for host in ("mirror.vendor.example", "x.mirror1.vendor.example", "mirror1.vendor.example.org"):
            with self.subTest(host=host):
                self.assertTrue(self.refused(host))
                self.assertIn(f"[work] BLOCKED {host} (not on the Allowlist)\n", self.log())

    def test_never_a_destination_with_strong_signals_checked_on_every_connection(self):
        self.store(rx(r"^[a-z]+\.exfil\.example$"), rx(r"^npm-[a-z]+\.example$"), rx(r"^[a-z]+\.later\.example$"))
        self.start()
        self.assertTrue(self.refused("up.exfil.example"))
        self.assertIn("[work] BLOCKED up.exfil.example (suspicious: exfil)\n", self.log())
        self.assertTrue(self.refused("npm-cache.example"))
        self.assertIn("[work] BLOCKED npm-cache.example (suspicious: lookalike)\n", self.log())
        t = self.tunnel("a.later.example")
        # a Watchlist entry added later counts from the next connection on; the open tunnel
        # stays, as for a domain entry (ADR 0005)
        write(os.path.join(self.conf, "suspicious-domains"), "exfil.example exfil # Test\nlater.example c2\n")
        self.assertTrue(self.refused("b.later.example"))
        self.assertIn("[work] BLOCKED b.later.example (suspicious: c2)\n", self.log())
        self.assertTrue(self.alive(t))
        # Open web does not let them through either
        self.open_web()
        self.assertTrue(self.refused("up.exfil.example"))
        self.assertNotIn("ALLOWED up.exfil.example", self.log())
        # a domain entry does (allow takes one only with --force on the CLI)
        self.store(rx(r"^[a-z]+\.exfil\.example$"), allowlist_tests.entry("exfil.example", "all"))
        self.tunnel("up.exfil.example")
        self.assertIn("[work] ALLOWED up.exfil.example (allowlist exfil.example all)\n", self.log())

    def test_while_the_watchlist_cannot_be_read_patterns_allow_nothing(self):
        self.store(rx(MIRRORS), allowlist_tests.entry("docs.example", "all"))
        watchlist = os.path.join(self.conf, "watchlist.json")
        write(watchlist, "{broken")
        self.start()
        self.assertTrue(self.refused("mirror1.vendor.example"))
        self.assertIn("[work] BLOCKED mirror1.vendor.example (Watchlist unreadable, regex entries closed)\n",
                      self.log())
        self.tunnel("docs.example")                          # domain entries keep working
        os.remove(watchlist)                                 # the text file from before applies again
        self.tunnel("mirror1.vendor.example")

    def test_removing_a_pattern_closes_only_the_tunnels_it_allowed(self):
        self.store(rx(MIRRORS), rx(r"^docs[0-9]\.example$", "work"))
        self.start()
        mirror, docs = self.tunnel("mirror1.vendor.example"), self.tunnel("docs1.example")
        self.store(rx(r"^docs[0-9]\.example$", "work"))
        self.assertTrue(self.closed(mirror))
        self.assertTrue(self.alive(docs))
        self.assertIn("[work] CLOSED mirror1.vendor.example (removed from the Allowlist)\n", self.log())

    def test_a_tunnel_another_entry_still_allows_stays(self):
        self.store(rx(MIRRORS), allowlist_tests.entry("vendor.example", "work"))
        self.start()
        t = self.tunnel("mirror1.vendor.example")
        # the log names a domain entry before a pattern
        self.assertIn("[work] ALLOWED mirror1.vendor.example (allowlist vendor.example work)\n", self.log())
        self.store(rx(MIRRORS))
        time.sleep(SETTLE)
        self.store(allowlist_tests.entry("vendor.example", "work"))
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))
        self.assertNotIn("CLOSED", self.log())

    def test_an_expired_pattern_stops_new_connections_only(self):
        self.store(rx(MIRRORS, "work", expires=int(time.time()) + 2))
        self.start()
        t = self.tunnel("mirror1.vendor.example")
        time.sleep(2.5)
        self.assertTrue(self.refused("mirror2.vendor.example"))
        # the timer removes the expired entry: nothing is closed
        self.store()
        time.sleep(SETTLE)
        self.assertTrue(self.alive(t))
        self.assertNotIn("CLOSED", self.log())

    def test_scope(self):
        self.store(rx(MIRRORS, "priv"), rx(r"^docs[0-9]\.example$", "work"))
        self.start()
        self.assertTrue(self.refused("mirror1.vendor.example"))
        self.tunnel("docs1.example")
        self.assertIn("[work] ALLOWED docs1.example (allowlist regex ^docs[0-9]\\.example$ work)\n", self.log())

    def test_a_pattern_the_rules_refuse_is_left_out(self):
        # only a hand edit can put one there: agentbox refuses it
        self.store(rx(r"(a+)+\.example"), rx(".*"), rx(MIRRORS), {"domain": "x.example", "regex": MIRRORS,
                                                                  "scope": "all", "expires": None})
        self.start()
        for host in ("aaa.example", "anything.example", "x.example"):
            with self.subTest(host=host):
                self.assertTrue(self.refused(host))
        self.tunnel("mirror1.vendor.example")
        self.assertIn("regex entry '(a+)+\\\\.example' left out (a quantifier", self.log())
        self.assertIn("regex entry '.*' left out (too broad: it matches google.com", self.log())


class CompiledOnceTest(unittest.TestCase):
    """The proxy as a module: patterns are compiled when allowlist.json changes, not per
    connection."""

    def test_patterns_are_compiled_once_per_reload(self):
        with tempfile.TemporaryDirectory() as conf:
            write(os.path.join(conf, "allowlist.json"), json.dumps({"entries": [rx(MIRRORS)]}))
            write(os.path.join(conf, "watchlist.json"), '{"entries": []}')
            with mock.patch.dict(os.environ, {"AGENTBOX_CONF_DIR": conf, "AGENTBOX_FEEDS_DIR": conf}):
                proxy = load(PROXY, "agentbox_proxy_compiled_once")
            lists = proxy.Allowlist("work")
            compiled = []
            real = proxy.suspicious.compile_pattern
            with mock.patch.object(proxy.suspicious, "compile_pattern",
                                   side_effect=lambda p, *a: compiled.append(p) or real(p, *a)):
                for _ in range(20):
                    keys, refused = proxy.granting("mirror1.vendor.example", [], lists, time.time())
                    self.assertEqual((keys, refused), ({proxy.Key(MIRRORS, "all", True)}, None))
                self.assertEqual(compiled, [MIRRORS])
                write(os.path.join(conf, "allowlist.json"), json.dumps({"entries": [rx(MIRRORS), rx("^b\\.example$")]}))
                proxy.granting("b.example", [], lists, time.time())
                self.assertEqual(compiled, [MIRRORS, MIRRORS, "^b\\.example$"])


# --- Seam A: the Allowlist through the CLI ------------------------------------------------

class AllowlistCliTest(allowlist_tests.CliTest):
    """The real classifier; a Watchlist with exfil.example; a proxy log of work and priv."""

    def setUp(self):
        super().setUp()
        shutil.copy2(SUSPICIOUS, os.path.join(self.root, "usr", "libexec", "agentbox-suspicious"))
        write(os.path.join(self.conf, "watchlist.json"), lists_tests.record(lists_tests.watch("exfil.example")))
        self.now = int(time.time())
        self.journal([
            proxy_line("work", "ALLOWED mirror1.vendor.example (open web)", 3 * 86400),
            proxy_line("work", "BLOCKED mirror2.vendor.example (not on the Allowlist)", 600),
            proxy_line("work", "BLOCKED mirror2.vendor.example (not on the Allowlist)", 500),
            proxy_line("work", "BLOCKED mirror9.exfil.example (not on the Allowlist)", 400),
            proxy_line("priv", "BLOCKED mirror3.vendor.example (not on the Allowlist)", 300),
            proxy_line("work", "BLOCKED mirror4.vendor.example (not on the Allowlist)", 8 * 86400),   # too old
            proxy_line("work", "BLOCKED other.example (not on the Allowlist)", 200),
            proxy_line("work", f"ALLOWED mirror5.vendor.example (allowlist regex {PATTERN} work)", 100),
            proxy_line("work", f"ALLOWED mirror5.vendor.example (allowlist {PATTERN} work)", 90),     # no domain
        ])

    def failure(self, *args):
        code, out, err = self.run_agentbox(*args, "--json")
        self.assertEqual((code, out), (1, ""), err)
        said = json.loads(err)
        return said["error"], said["params"], said["message"]

    def test_without_confirmation_it_shows_what_it_would_allow_and_changes_nothing(self):
        said = self.refused("allow", "work", "--regex", PATTERN)
        self.assertEqual(said.splitlines(), [
            f"pattern {PATTERN} for work matches 4 hosts of the proxy log of the last 7 days:",
            "  mirror1.vendor.example (allowed 1x, blocked 0x)",
            "  mirror2.vendor.example (allowed 0x, blocked 2x)",
            "  mirror5.vendor.example (allowed 2x, blocked 0x)",
            "  mirror9.exfil.example (allowed 0x, blocked 1x; stays blocked, strong Signals: exfil)",
            "Allowing it only with --confirm-pattern"])
        # for all VMs: every VM's log
        self.assertIn("  mirror3.vendor.example (allowed 0x, blocked 1x)", self.refused("allow", "all", "--regex", PATTERN))
        self.assertEqual(self.failure("allow", "work", "--regex", PATTERN)[:2],
                         ("confirmation_required", {"scope": "work", "regex": PATTERN, "hosts": 4}))
        self.assertFalse(os.path.exists(os.path.join(self.conf, "allowlist.json")))
        [e, *_] = [e for e in self.journald.entries() if e["AGENTBOX_ACTION"] == "allow"]
        self.assertEqual((e["AGENTBOX_OUTCOME"], e["AGENTBOX_ERROR"], e["AGENTBOX_PARAM_REGEX"]),
                         ("failed", "confirmation_required", PATTERN))
        self.assertTrue(e["AGENTBOX_DESCRIPTION"].startswith(f"Allow pattern {PATTERN}: pattern {PATTERN} for work "
                                                             "matches 4 hosts"), e["AGENTBOX_DESCRIPTION"])

    def test_confirmed_it_is_stored_and_listed_with_its_uses(self):
        result = self.json("--actor", allowlist_tests.FIREFOX, "allow", "work", "--regex", PATTERN, "--confirm-pattern",
                           "--for", "1h", "--note", "Spiegel")
        expires = result.pop("expires")
        self.assertAlmostEqual(expires, self.now + 3600, delta=60)
        self.assertEqual({k: v for k, v in result.items() if k != "added"},
                         {"domain": None, "regex": PATTERN, "scope": "work", "note": "Spiegel",
                          "added_by": allowlist_tests.FIREFOX,
                          "message": f"work: pattern {PATTERN} allowed until {self.until(expires)}"})
        [stored] = self.stored()
        self.assertEqual(set(stored), {"regex", "scope", "expires", "note", "added", "added_by"})
        [row] = self.json("allowlist", "work")
        self.assertEqual((row["domain"], row["regex"], row["uses_24h"], row["expired"]), (None, PATTERN, 1, False))
        self.assertTrue(self.ok("allowlist", "work").startswith(f"regex {PATTERN}"))
        # Activity names the pattern, as a parameter of its own
        [e] = [e for e in self.journald.entries() if e["AGENTBOX_ACTION"] == "allow"]
        self.assertEqual((e["AGENTBOX_PARAM_REGEX"], e["AGENTBOX_PARAM_SCOPE"], e["AGENTBOX_PARAM_DURATION"]),
                         (PATTERN, "work", "1h"))
        self.assertNotIn("AGENTBOX_PARAM_DOMAIN", e)
        self.assertEqual(e["AGENTBOX_DESCRIPTION"], f"Allow pattern {PATTERN} for 1 hour")

    def test_patterns_the_rules_refuse(self):
        cases = [
            (".*", "pattern_too_broad", {"host": "google.com"}),
            (r".*\.com", "pattern_too_broad", {"host": "google.com"}),
            (r"[a-z]+\.[a-z]+", "pattern_too_broad", {"host": "google.com"}),
            ("x" * 201, "invalid_pattern", {"reason": "too-long"}),
            ("(", "invalid_pattern", {"reason": "syntax"}),
            ("Mirror1\\.example", "invalid_pattern", {"reason": "uppercase"}),
            (r"(a)\1\.example", "invalid_pattern", {"reason": "backreference"}),
            (r"(?!x)a\.example", "invalid_pattern", {"reason": "lookaround"}),
            (r"(a+)+\.example", "invalid_pattern", {"reason": "nested-quantifier"}),
            (r"a*a*a*a*\.example", "invalid_pattern", {"reason": "too-complex"}),
        ]
        for pattern, error, params in cases:
            with self.subTest(pattern=pattern):
                self.assertEqual(self.failure("allow", "work", "--regex", pattern, "--confirm-pattern")[:2],
                                 (error, params))
        self.assertEqual(self.refused("allow", "work", "--regex", ".*", "--confirm-pattern"),
                         "--regex .*: too broad: it matches google.com, one of the unrelated hosts it must not match")
        self.assertEqual(self.refused("allow", "work", "--regex", MIRRORS, "--confirm-pattern", "--force"),
                         "--force does not apply to a regex entry: it never allows a destination with strong Signals")
        self.assertEqual(self.refused("allow", "work", "mirror1.vendor.example", "--confirm-pattern"),
                         "--confirm-pattern only with --regex")
        self.assertIn("too many arguments", self.refused("allow", "work", "mirror1.vendor.example", "--regex", MIRRORS))
        self.assertFalse(os.path.exists(os.path.join(self.conf, "allowlist.json")))

    def test_the_other_commands_take_the_same_pattern(self):
        self.store(rx(MIRRORS, "work", expires=self.now + 600), rx("docs.example", "all"),
                   allowlist_tests.entry("docs.example", "all"))
        self.assertAlmostEqual(self.json("extend", "work", "--regex", MIRRORS, "--for", "1h")["expires"],
                               self.now + 4200, delta=60)
        self.assertEqual(self.refused("extend", "work", "--regex", MIRRORS, "--for", "1h", "--force"),
                         "--force does not apply to a regex entry: it never allows a destination with strong Signals")
        self.assertEqual(self.json("set-note", "work", "--regex", MIRRORS, "--note", "Spiegel")["note"], "Spiegel")
        self.assertIsNone(self.json("make-permanent", "work", "--regex", MIRRORS)["expires"])
        self.assertEqual(self.refused("set-note", "priv", "--regex", MIRRORS, "--note", "x"),
                         f"priv: pattern {MIRRORS} is not on the Allowlist")
        self.assertEqual(self.json("disallow", "work", "--regex", MIRRORS),
                         {"scope": "work", "domain": None, "regex": MIRRORS, "removed": True,
                          "message": f"work: pattern {MIRRORS} removed"})
        # a domain and a pattern of the same text are two entries
        self.assertEqual(self.json("disallow", "all", "docs.example")["removed"], True)
        self.assertEqual(self.stored(), [rx("docs.example", "all")])
        self.assertEqual(self.ok("disallow", "work", "--regex", "docs.example"),
                         "work: pattern docs.example is not on the Allowlist (applies to all VMs: agentbox disallow "
                         "all --regex docs.example)\n")
        self.assertIn(("disallow", f"Remove the Allowlist entry pattern {MIRRORS}"),
                      [(a[1], a[3]) for a in self.activity()])

    def test_temporary_patterns_expire_and_are_recorded(self):
        self.store(rx(MIRRORS, "work", expires=self.now - 60), rx(PATTERN, "all", expires=self.now + 600))
        result = self.json("expire-allowlist")
        self.assertEqual([(e["domain"], e["regex"], e["scope"]) for e in result["expired"]], [(None, MIRRORS, "work")])
        self.assertEqual(self.stored(), [rx(PATTERN, "all", expires=self.now + 600)])
        [e] = [e for e in self.journald.entries() if e["AGENTBOX_ACTION"] == "allowlist.expired"]
        self.assertEqual((e["AGENTBOX_PARAM_REGEX"], e["AGENTBOX_PARAM_SCOPE"], e["AGENTBOX_VM"]), (MIRRORS, "work", "work"))
        self.assertTrue(e["AGENTBOX_DESCRIPTION"].startswith(f"Temporary Allowlist entry pattern {MIRRORS} expired"))

    def test_check_regex(self):
        result = self.json("check-regex", "allowlist", "--regex", PATTERN, "mirror7.vendor.example", "vendor.example",
                           "--scope", "work")
        self.assertEqual({k: v for k, v in result.items() if k != "preview"},
                         {"list": "allowlist", "regex": PATTERN, "scope": "work",
                          "matches": {"mirror7.vendor.example": True, "vendor.example": False}})
        self.assertEqual(result["preview"]["hosts"][3],
                         {"host": "mirror9.exfil.example", "allowed": 0, "blocked": 1, "signals": ["exfil"],
                          "strong": True})
        self.assertEqual((len(result["preview"]["hosts"]), result["preview"]["more"],
                          result["preview"]["watchlist_unreadable"], result["preview"]["truncated"]), (4, 0, None, False))
        # the Watchlist takes broad patterns, the others do not
        self.assertEqual(self.json("check-regex", "watchlist", "--regex", r".*\.xyz", "a.xyz"),
                         {"list": "watchlist", "regex": r".*\.xyz", "scope": None, "matches": {"a.xyz": True},
                          "preview": None})
        for which in ("allowlist", "legitimate", "harmless"):
            with self.subTest(which=which):
                self.assertEqual(self.failure("check-regex", which, "--regex", r".*\.xyz")[:2],
                                 ("pattern_too_broad", {"host": "mxv-9kq.xyz"}))
        self.assertEqual(self.failure("check-regex", "watchlist", "--regex", "(a+)+")[:2],
                         ("invalid_pattern", {"reason": "nested-quantifier"}))
        self.assertEqual(self.failure("check-regex", "watchlist", "--regex", "x", "--scope", "work")[:2],
                         ("invalid_argument", {"option": "--scope"}))
        # it changes nothing and records nothing
        self.assertEqual(self.journald.entries(), [])
        # without the proxy log the preview says so, and adding still needs the confirmation
        self.scenario(journalctl_exit=4)
        self.assertIn("Permission denied", self.json("check-regex", "allowlist", "--regex", PATTERN)["preview"]
                      ["log_unreadable"])
        self.assertEqual(self.refused("allow", "work", "--regex", PATTERN).splitlines()[-1],
                         "Allowing it only with --confirm-pattern")
        self.assertIn("The proxy log cannot be read (", self.refused("allow", "work", "--regex", PATTERN))
        self.scenario()
        # while the Watchlist cannot be read, a pattern lets nothing through
        write(os.path.join(self.conf, "watchlist.json"), "{broken")
        preview = self.json("check-regex", "allowlist", "--regex", PATTERN)["preview"]
        self.assertIn("watchlist.json: not valid JSON", preview["watchlist_unreadable"])
        self.assertIn("While the Watchlist cannot be read", self.refused("allow", "all", "--regex", PATTERN))


class CopyTest(vms_tests.VmsTest):
    def test_a_new_vm_copies_the_patterns_of_its_source(self):
        libexec = self.path("usr/libexec")
        os.makedirs(libexec, exist_ok=True)
        shutil.copy2(SUSPICIOUS, libexec)
        self.write("etc/agentbox/allowlist.json", json.dumps({"entries": [
            rx(MIRRORS, "priv", note="Spiegel"), allowlist_tests.entry("docs.rs", "priv")]}))
        self.assertIn("lab: 2 Allowlist entries copied from priv",
                      self.json("create", "lab", "--copy-allowlist-from", "priv")["message"])
        rows = json.loads(self.ok("allowlist", "lab", "--json"))
        self.assertEqual([(r["domain"], r["regex"], r["note"]) for r in rows],
                         [("docs.rs", None, ""), (None, MIRRORS, "Spiegel")])


# --- Seam A: the Watchlist and the Legitimate domains through the CLI ----------------------

class ListsCliTest(lists_tests.CliTest):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.conf, "watchlist.json"), lists_tests.record(lists_tests.watch("exfil.example")))
        write(os.path.join(self.conf, "legitimate.json"), lists_tests.record())
        lists_tests.write(os.path.join(self.feeds, "newly-registered"), b"mirror1.vendor.example\n")
        lists_tests.write(os.path.join(self.feeds, "threat-intelligence"), b"bad.example\nmirror2.vendor.example\n")

    def failure(self, *args):
        code, out, err = self.run_agentbox(*args, "--json")
        self.assertEqual((code, out), (1, ""), err)
        said = json.loads(err)
        return said["error"], said["params"]

    def test_a_watchlist_pattern_and_check_domain_names_it(self):
        drop = r"^drop[0-9]+\.(xyz|top)$"
        out = self.ok("--actor", lists_tests.FIREFOX, "watch-add", "--regex", drop, "--category", "paste",
                      "--source", "Vorfall 11/2026")
        self.assertEqual(out, f"pattern {drop} on the Watchlist (paste)\n")
        self.assertIn(watch_rx(drop, "paste", "Vorfall 11/2026") | {"added_by": lists_tests.FIREFOX},
                      [e | {"added": 1793700000} for e in self.stored("watchlist.json")])
        rep = self.check("drop7.xyz")
        self.assertEqual([(s["signal"], s["code"], s["params"], s["reason"], s["strong"]) for s in rep["signals"]
                          if s["code"].startswith("watchlist")],
                         [("paste", "watchlist-regex", {"pattern": drop, "source": "Vorfall 11/2026"},
                           f"Watchlist: pattern {drop} (Vorfall 11/2026)", True)])
        self.assertIn(f"Watchlist: pattern {drop} (Vorfall 11/2026)", self.ok("check-domain", "drop7.xyz"))
        self.assertEqual(self.check("x.drop7.xyz")["signals"][0]["code"], "tld")      # the whole name only
        [row] = [e for e in self.json("watchlist")["entries"] if e["regex"]]
        self.assertEqual((row["domain"], row["regex"], row["strong"]), (None, drop, True))
        # a broad pattern is fine here: it only raises false Alerts
        self.ok("watch-add", "--regex", r".*\.zip", "--category", "payload")
        # strong ones go only with --force, as for a domain
        self.assertEqual(self.failure("watch-remove", "--regex", drop), ("force_required", {"regex": drop,
                                                                                            "category": "paste"}))
        self.assertEqual(self.json("watch-remove", "--regex", drop, "--force"),
                         {"domain": None, "regex": drop, "removed": True,
                          "message": f"pattern {drop} removed from the Watchlist"})
        self.assertEqual(self.json("watch-set-note", "--regex", r".*\.zip", "--note", "breit")["note"], "breit")
        self.assertEqual(self.failure("watch-add", "--regex", "(a+)+", "--category", "c2"),
                         ("invalid_pattern", {"reason": "nested-quantifier"}))
        self.assertEqual(self.activity()[0],
                         ("watch-add", "ok", f"Watchlist: add pattern {drop} (paste)"))

    def test_a_legitimate_pattern_lifts_only_weak_signals(self):
        mirrors = r"^mirror[0-9]+\.vendor\.example$"
        self.assertEqual([s["signal"] for s in self.check("mirror1.vendor.example")["signals"]], ["young"])
        self.assertEqual(self.ok("legitimate-add", "--regex", mirrors, "--note", "Spiegel"),
                         f"pattern {mirrors} added as a Legitimate domain\n")
        rep = self.check("mirror1.vendor.example")
        self.assertEqual((rep["signals"], rep["legitimate"], rep["legitimate_regex"]), ([], mirrors, True))
        self.assertIn(f"unremarkable (Legitimate pattern {mirrors}, which lifts only weak Signals)",
                      self.ok("check-domain", "mirror1.vendor.example"))
        # the threat-intelligence list stays: a pattern lifts no strong Signal, so it needs no --force
        rep = self.check("mirror2.vendor.example")
        self.assertEqual(([s["signal"] for s in rep["signals"]], rep["strong"]), (["threat"], True))
        self.ok("legitimate-add", "--regex", r"^npm-[a-z]+\.example$")
        self.assertEqual([s["signal"] for s in self.check("npm-cache.example")["signals"]], ["lookalike"])
        self.assertEqual(self.failure("legitimate-add", "--regex", r"^x\.example$", "--force"),
                         ("invalid_argument", {"option": "--force"}))
        self.assertEqual(self.failure("legitimate-add", "--regex", r".*\.example\.(com|org)"),
                         ("pattern_too_broad", {"host": "ab.cd.ef.gh.ij.example.org"}))
        rows = [r for r in self.json("legitimate") if r["regex"]]
        self.assertEqual([(r["domain"], r["regex"], r["builtin"]) for r in rows],
                         [(None, mirrors, False), (None, r"^npm-[a-z]+\.example$", False)])
        self.assertEqual(self.json("legitimate-remove", "--regex", mirrors)["removed"], True)
        self.assertEqual(self.failure("legitimate-set-note", "--regex", mirrors, "--note", "x"),
                         ("legitimate_domain_not_found", {"regex": mirrors}))
        self.assertEqual([s["signal"] for s in self.check("mirror1.vendor.example")["signals"]], ["young"])

    def test_a_pattern_that_breaks_the_rules_makes_the_record_unreadable_alike(self):
        path = os.path.join(self.conf, "watchlist.json")
        env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "AGENTBOX_CONF_DIR": self.conf,
               "AGENTBOX_FEEDS_DIR": self.feeds}
        cases = [
            (lists_tests.record(watch_rx("(a+)+")), "broken entry (invalid regex (a+)+ (a quantifier"),
            (lists_tests.record(watch_rx("x"), watch_rx("x", "c2")), "x listed twice"),
            (lists_tests.record(watch_rx("x") | {"domain": "x.example"}), "broken entry (both domain and regex)"),
            (lists_tests.record(watch_rx(7)), "broken entry (field regex missing or of the wrong type)"),
        ]
        for data, why in cases:
            with self.subTest(why=why):
                write(path, data)
                said = self.refused("watch-set-note", "exfil.example", "--note", "x")
                self.assertTrue(said.startswith(f"Watchlist unreadable, Open web closed: {path}: {why}"), said)
                r = subprocess.run([sys.executable, "-I", SUSPICIOUS, "a.example"], capture_output=True, text=True,
                                   env=env, timeout=30)
                self.assertEqual(r.returncode, 3)
                self.assertEqual(r.stderr, said[len("Watchlist unreadable, Open web closed: "):] + "\n")
        # a domain and a pattern of the same text are two entries
        write(path, lists_tests.record(lists_tests.watch("x.example"), watch_rx("x.example")))
        self.assertEqual(len(self.json("watchlist")["entries"]), 2)


# --- Seam C: the Watchlist, Legitimate domains and Harmless marks in the proxy ----------

class ListsProxyTest(detection_tests.ProxyTest):
    def test_a_watchlist_pattern_raises_alerts_and_blocks_in_open_web(self):
        write(os.path.join(self.conf, "watchlist.json"), lists_tests.record(watch_rx(r"^drop[0-9]+\.example$", "c2")))
        self.start()
        self.assertEqual(self.alert("drop7.example"), ["c2"])
        self.assertIsNone(self.alert("www.drop7.example"))
        self.open_web()
        self.assertEqual(self.connect("drop8.example"), "HTTP/1.1 403 Forbidden")
        self.assertEqual(self.blocked_reason("drop8.example"), "suspicious: c2")

    def test_a_legitimate_pattern_lifts_weak_signals_only(self):
        write(os.path.join(self.conf, "watchlist.json"), lists_tests.record())
        write(os.path.join(self.conf, "legitimate.json"),
              lists_tests.record(legit_rx(r"^(fresh|bad)\.example$"), legit_rx(r"^npm-[a-z]+\.example$")))
        self.start()
        self.assertIsNone(self.alert("fresh.example"))                     # young, lifted
        self.assertEqual(self.alert("bad.example"), ["threat"])            # strong: stays
        self.assertEqual(self.alert("npm-cache.example"), ["lookalike"])

    def test_a_harmless_pattern_stops_alerts_but_not_the_block_in_open_web(self):
        self.harmless("npm-other.example", r"regex ^npm-[a-z]+-mirror\.example$")
        self.start()
        self.assertIsNone(self.alert("npm-cache-mirror.example"))
        self.assertEqual(self.alert("npm-cache.example"), ["lookalike"])
        self.assertEqual(self.alert("npm-cache-mirror.example", 22), ["port"])     # not by its name
        self.open_web()
        self.assertEqual(self.connect("npm-x-mirror.example"), "HTTP/1.1 403 Forbidden")
        self.assertEqual(self.blocked_reason("npm-x-mirror.example"), "suspicious: lookalike")

    def test_a_harmless_pattern_the_rules_refuse_counts_as_nothing(self):
        self.harmless("regex .*", r"regex ^(npm-[a-z]+)+\.example$")
        self.start()
        self.assertEqual(self.alert("npm-cache.example"), ["lookalike"])


# --- Seam A: the Harmless marks through the CLI ---------------------------------------------

class HarmlessCliTest(detection_tests.CliTest):
    MARK = r"^npm-[a-z]+-mirror\.example$"

    def test_mark_list_check_and_unmark(self):
        code, out, err = self.run_agentbox("mark-harmless", "--regex", self.MARK, "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"domain": None, "regex": self.MARK,
                                           "message": f"pattern {self.MARK} marked harmless"})
        self.run_agentbox("mark-harmless", "githbu.com")
        self.run_agentbox("mark-harmless", "--regex", self.MARK)                       # once only
        self.assertEqual(self.read_conf("harmless"), detection_tests.HEADER + f"regex {self.MARK}\ngithbu.com\n".encode())
        self.assertEqual(json.loads(self.run_agentbox("harmless", "--json")[1]),
                         [{"domain": None, "regex": self.MARK}, {"domain": "githbu.com", "regex": None}])
        self.assertEqual(self.run_agentbox("harmless")[1], f"regex {self.MARK}\ngithbu.com\n")
        rep = json.loads(self.run_agentbox("check-domain", "npm-cache-mirror.example", "--json")[1])
        self.assertEqual((rep["harmless"], rep["harmless_regex"], rep["alert"], rep["strong"]),
                         (self.MARK, True, False, True))
        self.assertIn(f"marked harmless (pattern {self.MARK}): no Alert, stays blocked in Open web",
                      self.run_agentbox("check-domain", "npm-cache-mirror.example")[1])
        self.assertEqual(self.run_agentbox("unmark-harmless", "--regex", self.MARK),
                         (0, f"pattern {self.MARK} no longer marked harmless\n", ""))
        self.assertEqual(self.read_conf("harmless"), detection_tests.HEADER + b"githbu.com\n")

    def test_refusals(self):
        for pattern, error in (("[a-z]+\\.[a-z]+", "pattern_too_broad"), ("(a|ab)+", "invalid_pattern")):
            with self.subTest(pattern=pattern):
                code, out, err = self.run_agentbox("mark-harmless", "--regex", pattern, "--json")
                self.assertEqual((code, json.loads(err)["error"]), (1, error))
        self.assertFalse(os.path.exists(os.path.join(self.conf, "harmless")))
        code, _, err = self.run_agentbox("mark-harmless", "a.example", "--regex", self.MARK)
        self.assertEqual(code, 1)
        self.assertIn("too many arguments", err)

    def test_alerts_of_destinations_a_pattern_marks_are_left_out(self):
        write(os.path.join(self.conf, "harmless"), f"regex {self.MARK}\n")
        now = time.time()
        with open(os.path.join(self.dir, "journal.txt"), "w") as f:
            for host, tags, age in (("npm-a-mirror.example", "lookalike", 60), ("npm-b.example", "lookalike", 50),
                                    ("npm-c-mirror.example", "lookalike,port", 40)):
                f.write(json.dumps({"__REALTIME_TIMESTAMP": str(int((now - age) * 10**6)),
                                    "SYSLOG_IDENTIFIER": "agentbox-proxy-work",
                                    "MESSAGE": f"[work] ALERT {host} [{tags}]", **proxy("work")}) + "\n")
        code, out, err = self.run_agentbox("alerts", "work", "--json")
        self.assertEqual(code, 0, err)
        # a Signal that does not depend on the name still counts
        self.assertEqual(sorted(r["domain"] for r in json.loads(out)), ["npm-b.example", "npm-c-mirror.example"])


# --- the self-test -------------------------------------------------------------------------

class SelfTestTest(unittest.TestCase):
    def run_self_test(self, classifier):
        r = subprocess.run([sys.executable, "-I", classifier, "--self-test"], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8",
                                "AGENTBOX_CONF_DIR": os.path.join(REPO, "proxy")}, timeout=120)
        return r.returncode, r.stdout

    def test_it_covers_one_pattern_per_list(self):
        code, out = self.run_self_test(SUSPICIOUS)
        self.assertEqual(code, 0, out)
        for line in ("ok      c2-7-selftest.example: expected c2, got c2",               # Watchlist
                     "ok      mirror1-selftest.example: expected -, got -",            # Legitimate domains
                     "ok      pypi-1-selftest.example: expected -, got -",             # Harmless marks
                     "ok      y.exfil-selftest.example: Allowlist pattern: expected blocked (suspicious: exfil)",
                     "ok      free1-selftest.example: Allowlist pattern: expected blocked (Watchlist unreadable, "
                     "regex entries closed)"):
            self.assertIn(line, out)

    def test_it_fails_when_a_pattern_lets_strong_signals_through(self):
        with tempfile.TemporaryDirectory() as d:
            with open(PROXY) as f:
                source = f.read()
            old = "    if strong(tags):\n        return f\"suspicious: {','.join(tags)}\", f\"{host} is suspicious"
            self.assertIn(old, source)
            write(os.path.join(d, "agentbox-proxy"), source.replace(old, "    if False:\n        return f\"\", f\""
                                                                         "{host} is suspicious"), 0o755)
            shutil.copy(SUSPICIOUS, d)
            code, out = self.run_self_test(os.path.join(d, "agentbox-suspicious"))
        self.assertEqual(code, 1)
        self.assertIn("FAILED  y.exfil-selftest.example: Allowlist pattern", out)


if __name__ == "__main__":
    unittest.main()
