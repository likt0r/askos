"""The Watchlist and the Legitimate domains as records on the Host (#48).

Seam A: the `agentbox` CLI against a temporary root (AGENTBOX_ROOT) with the real
classifier installed in it, small HaGeZi lists and a stand-in for journald's socket
(fake_journald.py), so every change can be checked in Activity.

Seam C: the real proxy (`proxy/agentbox-proxy`, which loads `proxy/agentbox-suspicious`)
on a Unix socket with records in a temporary configuration; nothing is allowlisted, so
the proxy never resolves a name. The tests change the records while it runs, directly
and through the CLI.

The detection self-test runs against records as well.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from client_hello import client_hello  # noqa: E402
from fake_journald import Journald  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(ROOT, "proxy", "agentbox-proxy")
SUSPICIOUS = os.path.join(ROOT, "proxy", "agentbox-suspicious")
AGENTBOX = os.path.join(ROOT, "bin", "agentbox")
FIREFOX = "Firefox/Linux (192.0.2.23)"

LEGACY_WATCHLIST = ("# Testliste\nexfil.example    exfil    # Testvorfall 2026\n"
                    "jsonkeeper.example payload # Contagious Interview\nnocategory.example\n"
                    "Bad_Name.example exfil\nodd.example spam # unbekannte Kategorie\n")
LEGACY_LEGITIMATE = "# legitim\nregistry.example   # Paketquelle\ngithub.com\n\ncdn.vendor.example\n"
# every line can be carried over into the record
CLEAN_WATCHLIST = ("# Testliste\nexfil.example    exfil    # Testvorfall 2026\n"
                   "jsonkeeper.example payload # Contagious Interview\nnocategory.example\n")


def write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def watch(domain, category="exfil", source="", note="", added=1793700000, added_by="bewr"):
    return {"domain": domain, "category": category, "source": source, "note": note, "added": added,
            "added_by": added_by}


def legit(domain, note="", added=1793700000, added_by="bewr"):
    return {"domain": domain, "note": note, "added": added, "added_by": added_by}


def record(*entries):
    return json.dumps({"entries": list(entries)})


# --- Seam A: the CLI ----------------------------------------------------------------

class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.root = os.path.join(self.dir, "root")
        self.conf = os.path.join(self.root, "etc", "agentbox")
        self.feeds = os.path.join(self.root, "var", "lib", "agentbox", "feeds")
        vm_records.write(self.root)
        libexec = os.path.join(self.root, "usr", "libexec")
        os.makedirs(libexec)
        shutil.copy2(SUSPICIOUS, libexec)
        write(os.path.join(self.feeds, "threat-intelligence"),
              b"".join(d.encode() + b"\n" for d in sorted(["bad.example", "a.pages.example", "b.pages.example"])))
        self.journald = Journald(os.path.join(self.root, "run", "systemd", "journal.agentbox", "socket"))
        self.addCleanup(self.journald.close)
        self.seen = 0

    def run_agentbox(self, *args):
        env = {"PATH": os.environ["PATH"], "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def ok(self, *args):
        code, out, err = self.run_agentbox(*args)
        self.assertEqual((code, err), (0, ""), args)
        return out

    def json(self, *args):
        return json.loads(self.ok(*args, "--json"))

    def files(self):
        out = {}
        for f in sorted(os.listdir(self.conf)):
            if os.path.isfile(os.path.join(self.conf, f)):
                with open(os.path.join(self.conf, f), "rb") as fh:
                    out[f] = fh.read()
        return out

    def refused(self, *args):
        """The command fails with this message and changes nothing."""
        before = self.files()
        code, out, err = self.run_agentbox(*args)
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(self.files(), before)
        self.assertTrue(err.startswith("agentbox: "), err)
        return err[len("agentbox: "):].rstrip("\n")

    def stored(self, name):
        with open(os.path.join(self.conf, name)) as f:
            return json.load(f)["entries"]

    def activity(self):
        """Activity entries since the start or the last forget_activity()."""
        return [(e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"])
                for e in self.journald.entries()[self.seen:]]

    def forget_activity(self):
        self.seen = len(self.journald.entries())

    def check(self, domain):
        return json.loads(self.ok("check-domain", domain, "--json"))


class WatchlistTest(CliTest):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.conf, "watchlist.json"), record(watch("exfil.example", source="Testvorfall")))
        write(os.path.join(self.conf, "legitimate.json"), record())

    def test_add_takes_effect_without_restart(self):
        self.assertEqual(self.check("x.npm-drop.example")["signals"][0]["signal"], "lookalike")
        out = self.ok("--actor", FIREFOX, "watch-add", "drop.example", "--category", "paste",
                      "--source=Vorfall 11/2026", "--note", "gesehen in work")
        self.assertEqual(out, "drop.example on the Watchlist (paste)\n")
        [e] = [e for e in self.stored("watchlist.json") if e["domain"] == "drop.example"]
        self.assertEqual({k: v for k, v in e.items() if k != "added"},
                         {"domain": "drop.example", "category": "paste", "source": "Vorfall 11/2026",
                          "note": "gesehen in work", "added_by": FIREFOX})
        self.assertLessEqual(abs(e["added"] - time.time()), 60)
        self.assertEqual(os.stat(os.path.join(self.conf, "watchlist.json")).st_mode & 0o777, 0o644)
        rep = self.check("cdn.drop.example")
        self.assertEqual((rep["strong"], [s["reason"] for s in rep["signals"]]),
                         (True, ["Watchlist: drop.example (Vorfall 11/2026)"]))

    def test_listing_with_strong_flag_and_fields(self):
        self.ok("watch-add", "drop.example", "--category", "payload")
        result = self.json("watchlist")
        self.assertEqual([(e["domain"], e["category"], e["strong"], e["source"], e["added_by"])
                          for e in result["entries"]],
                         [("drop.example", "payload", False, "", pwd.getpwuid(os.getuid()).pw_name),
                          ("exfil.example", "exfil", True, "Testvorfall", "bewr")])
        self.assertEqual(self.ok("watchlist").splitlines()[1], "exfil.example                    exfil       "
                                                                "Testvorfall")

    def test_add_again_changes_category_source_and_note(self):
        self.assertEqual(self.ok("watch-add", "exfil.example", "--category", "c2", "--note", "C2 statt Exfil"),
                         "exfil.example changed on the Watchlist (c2)\n")
        self.assertEqual(self.stored("watchlist.json"),
                         [watch("exfil.example", "c2", "Testvorfall", "C2 statt Exfil")])
        self.forget_activity()
        self.assertEqual(self.ok("watch-add", "exfil.example", "--category", "c2"),
                         "exfil.example is on the Watchlist like this already (c2)\n")
        self.assertEqual(self.activity(), [])

    def test_strong_entries_go_or_get_weaker_only_with_force(self):
        self.assertEqual(self.refused("watch-remove", "exfil.example"),
                         "exfil.example is on the Watchlist as exfil, a strong Signal. "
                         "Removing it only with --force")
        self.assertEqual(self.refused("watch-add", "exfil.example", "--category", "payload"),
                         "exfil.example is on the Watchlist as exfil, a strong Signal. "
                         "Downgrading to payload only with --force")
        self.ok("watch-add", "exfil.example", "--category", "payload", "--force")
        self.assertEqual(self.check("exfil.example")["strong"], False)
        self.assertEqual(self.json("watch-remove", "exfil.example"),
                         {"domain": "exfil.example", "regex": None, "removed": True,
                          "message": "exfil.example removed from the Watchlist"})
        self.assertEqual(self.stored("watchlist.json"), [])
        self.assertEqual(self.check("exfil.example")["signals"], [])

    def test_remove_with_force_and_missing_entries(self):
        self.ok("watch-remove", "exfil.example", "--force")
        self.forget_activity()
        self.assertEqual(self.json("watch-remove", "exfil.example"),
                         {"domain": "exfil.example", "regex": None, "removed": False,
                          "message": "exfil.example is not on the Watchlist"})
        self.assertEqual(self.activity(), [])
        self.assertEqual(self.refused("watch-set-note", "other.example", "--note", "x"),
                         "other.example is not on the Watchlist")

    def test_set_note(self):
        self.assertEqual(self.json("watch-set-note", "exfil.example", "--note", " weiter beobachten ")["note"],
                         "weiter beobachten")
        self.ok("watch-set-note", "exfil.example", "--note=")
        self.assertEqual(self.stored("watchlist.json")[0]["note"], "")
        self.assertEqual(self.refused("watch-set-note", "exfil.example"), "watch-set-note needs --note TEXT")

    def test_invalid_arguments(self):
        cases = (
            (["watch-add", "drop.example"], "watch-add needs --category"),
            (["watch-add", "drop.example", "--category", "threat"],
             "--category must be one of exfil, c2, tunnel, paste, phishing, blockchain, payload"),
            (["watch-add", "Drop.example", "--category", "exfil"], "invalid domain: Drop.example"),
            (["watch-add", "1.2.3.4", "--category", "exfil"], "invalid domain: 1.2.3.4"),
            (["watch-add", "a.example", "b.example", "--category", "exfil"], "too many arguments: b.example"),
            (["watch-add", "a.example", "--category", "exfil", "--source", "x\ty"],
             "--source: at most 200 printable characters on one line"),
            (["watch-add", "a.example", "--category", "exfil", "--note", "x" * 201],
             "--note: at most 200 printable characters on one line"),
            (["watch-remove", "a.example", "--all"], "unknown option: --all"),
            (["watchlist", "x"], "too many arguments: x"),
        )
        for args, message in cases:
            with self.subTest(args=args):
                self.assertEqual(self.refused(*args), message)

    def test_an_unreadable_record_is_reported_and_kept(self):
        # as the proxy sees it: it closes Open web then
        path = os.path.join(self.conf, "watchlist.json")
        closed = "Watchlist unreadable, Open web closed: "
        write(path, "{kaputt")
        self.assertEqual(self.refused("watch-add", "a.example", "--category", "exfil"),
                         f"{closed}{path}: not valid JSON")
        # the listing says why in place of the entries, and still shows the Feeds
        shown = self.json("watchlist")
        self.assertEqual((shown["entries"], shown["unreadable"]), ([], f"{path}: not valid JSON"))
        self.assertEqual(set(shown["feeds"]), {"threat-intelligence", "newly-registered"})
        lines = self.ok("watchlist").splitlines()
        self.assertEqual(lines[0], f"{closed}{path}: not valid JSON")
        self.assertIn("threat-intelligence", lines[1])
        write(path, '{"entries": {}}')
        self.assertEqual(self.json("watchlist")["unreadable"], f"{path}: no list entries")
        # missing, and no text file from before either
        os.remove(path)
        self.assertEqual(self.json("watchlist")["unreadable"], f"{path} missing")
        self.assertEqual(self.refused("watch-remove", "a.example"), f"{closed}{path} missing")
        # readable: no reason
        write(path, record(watch("exfil.example")))
        self.assertIsNone(self.json("watchlist")["unreadable"])

    def test_a_broken_entry_makes_the_record_unreadable_for_the_cli_and_the_classifier(self):
        # a typo in the category of a strong entry would make it weak for the proxy otherwise
        path = os.path.join(self.conf, "watchlist.json")
        partial = {k: v for k, v in watch("a.example").items() if k != "note"}
        # the CLI and the classifier say the same, byte for byte
        cases = [
            (record(watch("a.example", "exfill")), "broken entry (unknown category exfill)"),
            (record(watch("a.example"), watch("a.example", "c2")), "a.example listed twice"),
            (record(partial), "broken entry (field note missing or of the wrong type)"),
            (record(watch("a.example", added=True)), "broken entry (field added missing or of the wrong type)"),
            (record(watch("A.example")), "broken entry (invalid domain A.example)"),
            (record(watch("a.example", note="x" * 201)), "broken entry (text too long)"),
            (json.dumps({"entries": ["a.example"]}), "broken entry (not a JSON object)"),
        ]
        env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "AGENTBOX_CONF_DIR": self.conf,
               "AGENTBOX_FEEDS_DIR": self.feeds}
        for data, why in cases:
            with self.subTest(why=why):
                write(path, data)
                closed = f"Watchlist unreadable, Open web closed: {path}: {why}"
                self.assertEqual(self.refused("watch-remove", "a.example", "--force"), closed)
                self.assertEqual(self.refused("open-web", "work", "30"), closed)
                r = subprocess.run([sys.executable, "-I", SUSPICIOUS, "a.example"], capture_output=True, text=True,
                                   env=env, timeout=30)
                self.assertEqual((r.returncode, r.stderr), (3, f"{path}: {why}\n"))

    def test_open_web_only_while_the_record_can_be_read(self):
        path = os.path.join(self.conf, "watchlist.json")
        open_file = os.path.join(self.conf, "open-work")
        write(path, "{kaputt")
        self.assertEqual(self.refused("open-web", "work", "30"),
                         f"Watchlist unreadable, Open web closed: {path}: not valid JSON")
        os.remove(path)
        self.assertEqual(self.refused("open-web", "work", "30"),
                         f"Watchlist unreadable, Open web closed: {path} missing")
        self.assertFalse(os.path.exists(open_file))
        # the text file from before is a readable Watchlist as well
        write(os.path.join(self.conf, "suspicious-domains"), "exfil.example exfil\n")
        self.ok("open-web", "work", "30")
        self.assertTrue(os.path.exists(open_file))
        self.ok("close-web", "work")
        os.remove(os.path.join(self.conf, "suspicious-domains"))
        write(path, record())
        self.ok("open-web", "work", "30")

    def test_without_a_readable_watchlist_the_allowlist_takes_nothing_without_force(self):
        # what is strongly suspicious is unknown then; an entry would let it through, as the
        # proxy lets Allowlist entries through while it keeps Open web closed
        path = os.path.join(self.conf, "watchlist.json")
        allowlist = os.path.join(self.conf, "allowlist.json")
        until = int(time.time()) + 600
        write(allowlist, json.dumps({"entries": [
            {"domain": "temp.example", "scope": "work", "expires": until, "note": "", "added": 1793700000,
             "added_by": "bewr"}]}))
        for broken, why in (("{kaputt", f"{path}: not valid JSON"), (None, f"{path} missing")):
            with self.subTest(why=why):
                if broken is None:
                    os.remove(path)
                else:
                    write(path, broken)
                refusal = (f"Watchlist unreadable, Open web closed: {why}; whether webhook.site is strongly "
                           f"suspicious cannot be checked. Allowing it only with --force")
                self.assertEqual(self.refused("allow", "work", "webhook.site"), refusal)
                self.assertEqual(self.refused("allow", "all", "webhook.site", "--for", "1h"), refusal)
                self.assertEqual(self.refused("extend", "work", "temp.example", "--for", "1h"),
                                 refusal.replace("webhook.site", "temp.example"))
                self.assertEqual(self.refused("make-permanent", "work", "temp.example"),
                                 refusal.replace("webhook.site", "temp.example"))
        # the CLI on the Host still can
        self.ok("allow", "work", "docs.example", "--force")
        self.ok("extend", "work", "temp.example", "--for", "1h", "--force")
        self.ok("make-permanent", "work", "temp.example", "--force")
        # readable again: a destination without strong Signals needs no --force
        write(path, record(watch("exfil.example")))
        self.ok("allow", "work", "plain.example")
        self.assertEqual(self.refused("allow", "work", "x.exfil.example"),
                         "x.exfil.example is suspicious (exfil). Allowing it only with --force")

    def test_activity_of_each_command(self):
        for args in (["watch-add", "drop.example", "--category", "paste", "--source", "S", "--note", "N"],
                     ["watch-set-note", "drop.example", "--note", "anders"],
                     ["watch-remove", "drop.example", "--force"]):
            self.ok("--actor", FIREFOX, *args)
        self.run_agentbox("--actor", FIREFOX, "watch-remove", "exfil.example")
        # only a known category goes into the description, never what the request said
        self.run_agentbox("--actor", FIREFOX, "watch-add", "x.example", "--category=see https://attacker.example now")
        self.assertEqual(self.activity(), [
            ("watch-add", "ok", "Watchlist: add drop.example (paste)"),
            ("watch-set-note", "ok", "Watchlist: note on drop.example"),
            ("watch-remove", "ok", "Watchlist: remove drop.example with --force"),
            ("watch-remove", "failed", "Watchlist: remove exfil.example: exfil.example is on the Watchlist "
                                       "as exfil, a strong Signal. Removing it only with --force"),
            ("watch-add", "failed", "Watchlist: add x.example: --category must be one of exfil, c2, tunnel, "
                                    "paste, phishing, blockchain, payload"),
        ])
        # the same for the parameters, the error's too: a known category only
        self.assertEqual([{k: v for k, v in e.items() if k.startswith(("AGENTBOX_PARAM_", "AGENTBOX_ERROR"))}
                          for e in self.journald.entries()][::4], [
            {"AGENTBOX_PARAM_DOMAIN": "drop.example", "AGENTBOX_PARAM_CATEGORY": "paste"},
            {"AGENTBOX_PARAM_DOMAIN": "x.example", "AGENTBOX_ERROR": "invalid_category"}])
        self.assertNotIn(b"attacker", b"".join(self.journald.raw()))
        self.assertEqual({e["AGENTBOX_ACTOR"] for e in self.journald.entries()}, {FIREFOX})


class LegitimateTest(CliTest):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.conf, "watchlist.json"), record(watch("exfil.example")))
        write(os.path.join(self.conf, "legitimate.json"), record(legit("registry.example", "Paketquelle")))

    def test_listing_marks_the_built_in_domains(self):
        rows = self.json("legitimate")
        self.assertIn({"domain": "registry.example", "regex": None, "note": "Paketquelle", "added": 1793700000,
                       "added_by": "bewr", "builtin": False, "refused": None}, rows)
        self.assertIn({"domain": "github.com", "regex": None, "note": "", "added": None, "added_by": None,
                       "builtin": True, "refused": None}, rows)
        self.assertEqual([r["domain"] for r in rows], sorted(r["domain"] for r in rows))
        text = self.ok("legitimate").splitlines()
        self.assertIn("github.com                       built-in", text)
        self.assertIn("registry.example                 Paketquelle", text)

    def test_add_takes_effect_without_restart(self):
        self.assertEqual([s["signal"] for s in self.check("www.mirror.example")["signals"]], [])
        self.ok("--actor", FIREFOX, "legitimate-add", "mirror.example", "--note", "Spiegel")
        self.assertEqual(self.check("www.mirror.example")["legitimate"], "mirror.example")
        [e] = [e for e in self.stored("legitimate.json") if e["domain"] == "mirror.example"]
        self.assertEqual((e["note"], e["added_by"]), ("Spiegel", FIREFOX))
        self.assertEqual(self.activity(), [("legitimate-add", "ok", "Add the Legitimate domain mirror.example")])

    def test_lifting_strong_signals_needs_force(self):
        self.assertEqual(self.refused("legitimate-add", "npm-mirror.example"),
                         "npm-mirror.example has strong Signals (lookalike). Legitimate only with --force")
        self.assertEqual(self.refused("legitimate-add", "pages.example"),
                         "pages.example has strong Signals (threat-intelligence list: a.pages.example, "
                         "b.pages.example). Legitimate only with --force")
        self.assertEqual(self.refused("legitimate-add", "bad.example"),
                         "bad.example has strong Signals (threat; threat-intelligence list: bad.example). "
                         "Legitimate only with --force")
        self.ok("--actor", FIREFOX, "legitimate-add", "npm-mirror.example", "--force")
        self.assertEqual(self.check("npm-mirror.example")["signals"], [])
        self.assertEqual(self.activity()[-1],
                         ("legitimate-add", "ok", "Add the Legitimate domain npm-mirror.example with --force"))

    def test_watchlist_entries_stay_for_legitimate_domains(self):
        self.ok("legitimate-add", "exfil.example")
        self.assertEqual([s["signal"] for s in self.check("exfil.example")["signals"]], ["exfil"])

    def test_the_blockchain_pattern_stays_below_legitimate_domains(self):
        # a Legitimate domain lifts no blockchain pattern, so adding one needs no --force for it
        write(os.path.join(self.conf, "allowlist.json"), '{"entries": []}\n')
        self.ok("legitimate-add", "base.example")
        self.ok("legitimate-add", "infura.example")
        for host in ("mainnet.base.example", "rpc.base.example", "infura.example", "mainnet.github.io"):
            with self.subTest(host=host):
                rep = self.check(host)
                self.assertEqual(([s["signal"] for s in rep["signals"]], rep["strong"]), (["blockchain"], True))
        rep = self.check("www.base.example")
        self.assertEqual((rep["legitimate"], rep["signals"]), ("base.example", []))
        # still blocked in Open web, and only the CLI can allow it
        self.assertIn("is suspicious (blockchain)", self.refused("allow", "all", "mainnet.base.example"))
        self.ok("allow", "all", "mainnet.base.example", "--force")

    def test_feed_entries_below_a_legitimate_domain_of_the_host_still_count(self):
        # the Feeds are stored as fetched: a new Legitimate domain is checked against every entry
        # at or below it, also those registry.example lifts now (it can go again, from the
        # Control Room as well, and the new one would keep them lifted)
        write(os.path.join(self.feeds, "threat-intelligence"),
              b"".join(d.encode() + b"\n" for d in sorted(["bad.example", "c2.registry.example",
                                                           "x.cdn.vendor.example"])))
        self.assertEqual(self.check("c2.registry.example")["signals"], [])
        self.assertEqual(self.refused("legitimate-add", "c2.registry.example"),
                         "c2.registry.example has strong Signals (threat; threat-intelligence list: "
                         "c2.registry.example). Legitimate only with --force")
        self.assertEqual(self.refused("legitimate-add", "www.c2.registry.example"),
                         "www.c2.registry.example has strong Signals (threat). Legitimate only with --force")
        # nothing at or below it: no --force, also below one of the Host
        self.ok("legitimate-add", "cdn.registry.example")
        # above one of the Host: the entries that one lifts count as well
        self.assertEqual(self.refused("legitimate-add", "cdn.vendor.example"),
                         "cdn.vendor.example has strong Signals (threat-intelligence list: x.cdn.vendor.example). "
                         "Legitimate only with --force")
        self.ok("legitimate-add", "cdn.vendor.example", "--force")
        self.assertEqual(self.refused("legitimate-add", "vendor.example"),
                         "vendor.example has strong Signals (threat-intelligence list: x.cdn.vendor.example). "
                         "Legitimate only with --force")
        # removing a Legitimate domain brings its entries back at once
        self.ok("legitimate-remove", "registry.example")
        self.assertEqual([s["signal"] for s in self.check("c2.registry.example")["signals"]], ["threat"])

    def test_below_a_domain_of_the_host_its_own_strong_signals_still_need_force(self):
        # the parent can go again (also from the Control Room); the child would keep the lift
        self.ok("legitimate-add", "npm-mirror.example", "--force")
        self.assertEqual(self.refused("legitimate-add", "www.npm-mirror.example"),
                         "www.npm-mirror.example has strong Signals (lookalike). Legitimate only with --force")
        # a built-in one stays: below it nothing more is lifted
        self.ok("legitimate-add", "cdn.github.com")

    def test_without_the_threat_intelligence_list_only_with_force(self):
        # before the first fetch nothing tells which of its entries the domain would lift
        os.remove(os.path.join(self.feeds, "threat-intelligence"))
        self.assertEqual(self.refused("legitimate-add", "pages.example"),
                         "pages.example: cannot be checked without the threat-intelligence list (not fetched "
                         "yet). Legitimate only with --force")
        self.ok("legitimate-add", "pages.example", "--force")

    def test_a_broken_record_is_unreadable_for_the_cli_and_the_classifier(self):
        # it only adds Signals: without it only the built-in Legitimate domains count
        path = os.path.join(self.conf, "legitimate.json")
        partial = {k: v for k, v in legit("x.example").items() if k != "added_by"}
        cases = [
            ("{kaputt", "not valid JSON"),
            (record(legit("registry.example"), partial), "broken entry (field added_by missing or of the wrong type)"),
            (record(legit("registry.example"), legit("registry.example")), "registry.example listed twice"),
            (record(legit("registry.example"), legit("A.example")), "broken entry (invalid domain A.example)"),
        ]
        for data, why in cases:
            with self.subTest(why=why):
                write(path, data)
                closed = f"Legitimate domains unreadable: {path}: {why}"
                self.assertEqual(self.refused("legitimate"), closed)
                self.assertEqual(self.refused("legitimate-add", "mirror.example"), closed)
                rep = self.check("cdn.registry.example")
                self.assertEqual((rep["legitimate"], [s["signal"] for s in rep["signals"]]), (None, []))
                self.assertEqual(self.check("cdn.github.com")["legitimate"], "github.com")

    def test_built_in_domains_stay(self):
        self.assertEqual(self.refused("legitimate-remove", "github.com"), "github.com is built in and stays legitimate")
        self.assertEqual(self.refused("legitimate-set-note", "github.com", "--note", "x"),
                         "github.com is built in and stays legitimate")
        self.assertEqual(self.json("legitimate-add", "github.com"),
                         {"domain": "github.com", "regex": None, "builtin": True,
                          "message": "github.com is built in and legitimate already"})
        self.assertEqual(self.activity(), [("legitimate-remove", "failed",
                                            "Remove the Legitimate domain github.com: github.com is built in and "
                                            "stays legitimate"),
                                           ("legitimate-set-note", "failed",
                                            "Note on the Legitimate domain github.com: github.com is built in and "
                                            "stays legitimate")])

    def test_remove_and_notes(self):
        self.assertEqual(self.json("legitimate-set-note", "registry.example", "--note", "npm-Spiegel")["note"],
                         "npm-Spiegel")
        self.assertEqual(self.refused("legitimate-set-note", "other.example", "--note", "x"),
                         "other.example is not an added Legitimate domain")
        self.assertEqual(self.json("legitimate-remove", "registry.example")["removed"], True)
        self.assertEqual(self.stored("legitimate.json"), [])
        self.forget_activity()
        self.assertEqual(self.json("legitimate-remove", "registry.example"),
                         {"domain": "registry.example", "regex": None, "removed": False,
                          "message": "registry.example is not an added Legitimate domain"})
        self.assertEqual(self.activity(), [])

    def test_adding_again_only_changes_the_note(self):
        self.forget_activity()
        self.assertEqual(self.ok("legitimate-add", "registry.example"), "registry.example is legitimate already\n")
        self.assertEqual(self.activity(), [])
        self.ok("legitimate-add", "registry.example", "--note", "neu")
        self.assertEqual(self.stored("legitimate.json"), [legit("registry.example", "neu")])


class MigrationTest(CliTest):
    """Hosts from before the records: their text files, which a new Host also starts with."""

    def setUp(self):
        super().setUp()
        write(os.path.join(self.conf, "suspicious-domains"), LEGACY_WATCHLIST)
        write(os.path.join(self.conf, "legitimate-domains"), LEGACY_LEGITIMATE)

    def test_read_only_commands_and_the_classifier_use_the_text_files(self):
        self.assertEqual([(e["domain"], e["category"], e["source"]) for e in self.json("watchlist")["entries"]],
                         [("exfil.example", "exfil", "Testvorfall 2026"),
                          ("jsonkeeper.example", "payload", "Contagious Interview")])
        self.assertIn("registry.example", [r["domain"] for r in self.json("legitimate") if not r["builtin"]])
        self.assertEqual(self.check("cdn.registry.example")["legitimate"], "registry.example")
        self.assertFalse(os.path.exists(os.path.join(self.conf, "watchlist.json")))

    def test_migration_takes_every_entry_over_once(self):
        result = self.json("migrate-lists")
        self.assertEqual(result["watchlist"], {"migrated": 2, "file": "suspicious-domains",
                                               "skipped": ["Bad_Name.example exfil", "odd.example spam"]})
        self.assertEqual(result["legitimate"], {"migrated": 2, "file": "legitimate-domains", "skipped": []})
        watched = self.stored("watchlist.json")
        self.assertEqual([(e["domain"], e["category"], e["source"], e["note"], e["added_by"]) for e in watched],
                         [("exfil.example", "exfil", "Testvorfall 2026", "", "migration"),
                          ("jsonkeeper.example", "payload", "Contagious Interview", "", "migration")])
        # github.com is built in and not taken over
        self.assertEqual([(e["domain"], e["note"], e["added_by"]) for e in self.stored("legitimate.json")],
                         [("cdn.vendor.example", "", "migration"), ("registry.example", "Paketquelle", "migration")])
        names = os.listdir(self.conf)
        self.assertIn("suspicious-domains.migrated", names)
        self.assertNotIn("suspicious-domains", names)
        self.assertNotIn("legitimate-domains", names)
        self.assertEqual(self.check("cdn.registry.example")["legitimate"], "registry.example")
        self.assertEqual(self.activity(), [
            ("migrate-lists", "ok", "Watchlist: 2 entries taken over from suspicious-domains, 2 lines skipped"),
            ("migrate-lists", "ok", "Legitimate domains: 2 entries taken over from legitimate-domains, no line "
                                    "skipped"),
        ])
        self.assertEqual([{k: v for k, v in e.items() if k.startswith("AGENTBOX_PARAM_")}
                          for e in self.journald.entries()], [
            {"AGENTBOX_PARAM_LIST": "watchlist", "AGENTBOX_PARAM_LEGACY": "suspicious-domains",
             "AGENTBOX_PARAM_MIGRATED": "2", "AGENTBOX_PARAM_SKIPPED": "2"},
            {"AGENTBOX_PARAM_LIST": "legitimate", "AGENTBOX_PARAM_LEGACY": "legitimate-domains",
             "AGENTBOX_PARAM_MIGRATED": "2", "AGENTBOX_PARAM_SKIPPED": "0"}])
        # again: nothing changes, no Activity
        before = self.files()
        self.forget_activity()
        self.assertEqual(self.ok("migrate-lists"), "Watchlist and Legitimate domains: nothing to take over\n")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.activity(), [])

    def test_the_first_change_migrates(self):
        write(os.path.join(self.conf, "suspicious-domains"), CLEAN_WATCHLIST)
        self.ok("--actor", FIREFOX, "watch-add", "drop.example", "--category", "c2")
        self.assertEqual([e["domain"] for e in self.stored("watchlist.json")],
                         ["drop.example", "exfil.example", "jsonkeeper.example"])
        self.assertTrue(os.path.exists(os.path.join(self.conf, "suspicious-domains.migrated")))
        self.assertTrue(os.path.exists(os.path.join(self.conf, "legitimate-domains")))
        self.assertEqual(self.activity(), [
            ("migrate-lists", "ok", "Watchlist: 2 entries taken over from suspicious-domains, no line skipped"),
            ("watch-add", "ok", "Watchlist: add drop.example (c2)"),
        ])
        self.assertEqual({e["AGENTBOX_ACTOR"] for e in self.journald.entries()}, {FIREFOX})

    def test_a_change_that_changes_nothing_still_records_the_migration(self):
        write(os.path.join(self.conf, "suspicious-domains"), CLEAN_WATCHLIST)
        self.assertEqual(self.ok("watch-remove", "missing.example"), "missing.example is not on the Watchlist\n")
        self.assertEqual(self.ok("legitimate-add", "registry.example"), "registry.example is legitimate already\n")
        self.assertEqual(self.activity(), [
            ("migrate-lists", "ok", "Watchlist: 2 entries taken over from suspicious-domains, no line skipped"),
            ("migrate-lists", "ok", "Legitimate domains: 2 entries taken over from legitimate-domains, no line "
                                    "skipped"),
        ])
        self.assertEqual({"suspicious-domains.migrated", "legitimate-domains.migrated"} - set(os.listdir(self.conf)),
                         set())

    def test_a_change_never_migrates_lines_it_cannot_carry_over(self):
        # the classifier read "threat" as a strong category and an odd name as an entry; left
        # out, a change from the Control Room would weaken them without --force
        legacy = os.path.join(self.conf, "suspicious-domains")
        refusal = (f"{legacy}: 3 lines cannot be taken over (Bad_Name.example exfil, odd.example spam, x.example "
                   f"threat); run agentbox migrate-lists on the Host first")
        write(legacy, LEGACY_WATCHLIST + "x.example threat\n")
        self.assertEqual(self.refused("--actor", FIREFOX, "watch-add", "drop.example", "--category", "c2"), refusal)
        self.assertEqual(self.refused("watch-remove", "missing.example"), refusal)
        self.assertFalse(os.path.exists(os.path.join(self.conf, "watchlist.json")))
        # the same for the Legitimate domains
        write(os.path.join(self.conf, "legitimate-domains"), LEGACY_LEGITIMATE + "Bad_Name.example\n")
        self.assertEqual(self.refused("legitimate-add", "mirror.example"),
                         f"{self.conf}/legitimate-domains: 1 line cannot be taken over (Bad_Name.example); run agentbox "
                         f"migrate-lists on the Host first")
        # migrate-lists on the Host takes the rest over and names what it left out
        result = self.json("migrate-lists")
        self.assertEqual(result["watchlist"]["skipped"], ["Bad_Name.example exfil", "odd.example spam", "x.example threat"])
        self.assertEqual(result["legitimate"]["skipped"], ["Bad_Name.example"])
        self.assertIn("Watchlist: 2 entries taken over from suspicious-domains, 3 lines skipped",
                      [a[2] for a in self.activity()])

    def test_a_domain_listed_twice_keeps_its_strongest_category(self):
        # the classifier took the last line; the migration never weakens an entry
        write(os.path.join(self.conf, "suspicious-domains"),
              "first.example exfil # A\nfirst.example payload # B\nlast.example payload\nlast.example c2 # C\n"
              "both.example payload\nboth.example payload # D\n")
        self.ok("watch-add", "drop.example", "--category", "c2")
        self.assertEqual([(e["domain"], e["category"], e["source"]) for e in self.stored("watchlist.json")],
                         [("both.example", "payload", "D"), ("drop.example", "c2", ""), ("first.example", "exfil", "A"),
                          ("last.example", "c2", "C")])

    def test_a_leftover_text_file_is_merged_into_the_record(self):
        # a record already, and the text file again (put back by hand): its new entries are
        # added, the record's own stay as they are
        write(os.path.join(self.conf, "watchlist.json"), record(watch("exfil.example", "c2", "Host", "eigene Notiz")))
        result = self.json("migrate-lists")
        self.assertEqual(result["watchlist"], {"migrated": 1, "file": "suspicious-domains",
                                               "skipped": ["Bad_Name.example exfil", "odd.example spam"]})
        self.assertEqual([(e["domain"], e["category"], e["source"], e["note"], e["added_by"])
                          for e in self.stored("watchlist.json")],
                         [("exfil.example", "c2", "Host", "eigene Notiz", "bewr"),
                          ("jsonkeeper.example", "payload", "Contagious Interview", "", "migration")])
        names = os.listdir(self.conf)
        self.assertIn("suspicious-domains.migrated", names)
        self.assertNotIn("suspicious-domains", names)
        self.assertEqual(self.activity()[-1][0:2], ("migrate-lists", "ok"))

    def test_with_nothing_to_migrate_a_broken_record_is_a_warning(self):
        # agentbox setup runs migrate-lists late: the Activity journal, the first Feeds and the
        # Control Room come after it
        os.remove(os.path.join(self.conf, "suspicious-domains"))
        os.remove(os.path.join(self.conf, "legitimate-domains"))
        write(os.path.join(self.conf, "watchlist.json"), record(watch("exfil.example")))
        path = os.path.join(self.conf, "legitimate.json")
        write(path, record(legit("a.example"), legit("a.example")))
        self.assertEqual(self.ok("migrate-lists"),
                         f"WARNING: Legitimate domains unreadable: {path}: a.example listed twice\n")
        self.assertEqual(self.activity(), [])
        # with a text file to merge into it, it fails
        write(os.path.join(self.conf, "legitimate-domains"), "mirror.example\n")
        self.assertEqual(self.refused("migrate-lists"), f"Legitimate domains unreadable: {path}: a.example listed twice")

    def test_a_host_without_lists_keeps_open_web_closed_until_the_first_watch_add(self):
        # an empty Watchlist would open Open web with nothing on it
        os.remove(os.path.join(self.conf, "suspicious-domains"))
        os.remove(os.path.join(self.conf, "legitimate-domains"))
        path = os.path.join(self.conf, "watchlist.json")
        said = ("Watchlist: neither watchlist.json nor suspicious-domains, nothing created "
                "(Open web stays closed until the first agentbox watch-add --force)")
        created = "Legitimate domains: legitimate.json created empty, no legitimate-domains"
        self.assertEqual(self.ok("migrate-lists"), f"{said}; {created}\n")
        self.assertFalse(os.path.exists(path))
        # a missing record of the Legitimate domains only adds Signals: it may start empty,
        # and Activity says so
        self.assertEqual(self.stored("legitimate.json"), [])
        self.assertEqual(self.activity(), [("migrate-lists", "ok", created)])
        self.forget_activity()
        self.assertEqual(self.json("migrate-lists")["message"], said)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.activity(), [])
        closed = f"Watchlist unreadable, Open web closed: {path} missing"
        self.assertEqual(self.refused("open-web", "work", "30"), closed)
        self.assertEqual(self.json("watchlist")["unreadable"], f"{path} missing")
        self.assertEqual(self.refused("watch-remove", "a.example"), closed)
        self.assertEqual(self.refused("watch-set-note", "a.example", "--note", "x"), closed)
        # the first watch-add creates the record, which opens Open web again: only with --force,
        # so the Control Room can never recreate a deleted Watchlist; a code of its own tells it
        # why (lowering a strong entry is force_required)
        code, out, err = self.run_agentbox("watch-add", "drop.example", "--category", "c2", "--json")
        self.assertEqual((code, out, json.loads(err)["error"]), (1, "", "watchlist_recreate_force_required"))
        self.assertFalse(os.path.exists(path))
        self.forget_activity()
        recreate = (f"Watchlist unreadable, Open web closed: {path} is missing; agentbox watch-add with --force on "
                    f"the Host creates it again and so opens Open web again. Creating it again only with --force")
        self.assertEqual(self.refused("watch-add", "drop.example", "--category", "c2"), recreate)
        self.ok("watch-add", "drop.example", "--category", "c2", "--force")
        self.assertEqual([(e["domain"], e["category"]) for e in self.stored("watchlist.json")],
                         [("drop.example", "c2")])
        self.assertEqual(self.activity(), [
            ("watch-add", "failed", f"Watchlist: add drop.example (c2): {recreate}"),
            ("watch-add", "ok", "Watchlist: add drop.example (c2) with --force"),
        ])
        self.ok("open-web", "work", "30")
        self.assertEqual(self.ok("migrate-lists"), "Watchlist and Legitimate domains: nothing to take over\n")

    def test_the_repository_seed_migrates_completely(self):
        shutil.copy(os.path.join(ROOT, "proxy", "suspicious-domains"), self.conf)
        shutil.copy(os.path.join(ROOT, "proxy", "legitimate-domains"), self.conf)
        result = self.json("migrate-lists")
        self.assertEqual((result["watchlist"]["skipped"], result["legitimate"]["skipped"]), ([], []))
        domains = [e["domain"] for e in self.stored("watchlist.json")]
        self.assertIn("webhook.site", domains)
        self.assertEqual(len(domains), result["watchlist"]["migrated"])
        self.assertIn("claudeusercontent.com", [e["domain"] for e in self.stored("legitimate.json")])


# --- Seam C: the proxy with records ---------------------------------------------------

class ProxyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        # laid out as a test root, so the CLI (AGENTBOX_ROOT) changes what the proxy reads
        self.root = os.path.join(self.dir, "root")
        self.conf = os.path.join(self.root, "etc", "agentbox")
        self.feeds = os.path.join(self.root, "var", "lib", "agentbox", "feeds")
        libexec = os.path.join(self.root, "usr", "libexec")
        os.makedirs(libexec)
        shutil.copy2(SUSPICIOUS, libexec)
        write(os.path.join(self.conf, "watchlist.json"), record(watch("exfil.example", source="Test")))
        write(os.path.join(self.conf, "legitimate.json"), record(legit("registry.example")))
        # the text files from before are ignored once the records exist
        write(os.path.join(self.conf, "suspicious-domains"), "old.example exfil\n")
        write(os.path.join(self.conf, "legitimate-domains"), "npm-old.example\n")
        write(os.path.join(self.conf, "allowlist.json"), '{"entries": []}\n')
        write(os.path.join(self.feeds, "threat-intelligence"), b"bad.example\ncdn.registry.example\n")
        self.sock = os.path.join(self.dir, "proxy.sock")
        self.log_path = os.path.join(self.dir, "proxy.log")
        env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "AGENTBOX_CONF_DIR": self.conf,
               "AGENTBOX_FEEDS_DIR": self.feeds}
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
        if self.proxy.poll() is None:
            self.proxy.terminate()
            self.proxy.wait(10)

    def log_text(self):
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def alert(self, host):
        """A ClientHello, then the Signals of the Alert the proxy logged for it (None: no Alert)."""
        before = len(self.log_text().splitlines())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.sock)
            s.sendall(client_hello(host))
            while s.recv(4096):
                pass
        alerts = [line for line in self.log_text().splitlines()[before:] if f"] ALERT {host} [" in line]
        return alerts[-1].rsplit("[", 1)[1].rstrip("]").split(",") if alerts else None

    def test_records_decide(self):
        self.assertEqual(self.alert("upload.exfil.example"), ["exfil"])
        self.assertIsNone(self.alert("old.example"))
        self.assertEqual(self.alert("npm-old.example"), ["lookalike"])
        self.assertIsNone(self.alert("cdn.registry.example"))
        self.assertEqual(self.alert("bad.example"), ["threat"])

    def cli(self, *args):
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, timeout=60,
                           env={"PATH": os.environ["PATH"], "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8"})
        self.assertEqual((r.returncode, r.stderr), (0, ""), args)

    def test_changes_through_the_cli_reach_the_running_proxy(self):
        self.assertIsNone(self.alert("x.drop.example"))
        self.cli("watch-add", "drop.example", "--category", "c2")
        self.assertEqual(self.alert("x.drop.example"), ["c2"])
        self.cli("watch-remove", "exfil.example", "--force")
        self.assertIsNone(self.alert("upload.exfil.example"))
        self.assertEqual(self.alert("bad.example"), ["threat"])
        self.cli("legitimate-add", "bad.example", "--force")
        self.assertIsNone(self.alert("bad.example"))
        self.cli("legitimate-remove", "registry.example")
        self.assertEqual(self.alert("cdn.registry.example"), ["threat"])

    def test_changes_apply_without_restart(self):
        write(os.path.join(self.conf, "watchlist.json"), record(watch("exfil.example"), watch("drop.example", "c2")))
        write(os.path.join(self.conf, "legitimate.json"), record(legit("registry.example"), legit("bad.example")))
        self.assertEqual(self.alert("x.drop.example"), ["c2"])
        self.assertIsNone(self.alert("bad.example"))
        write(os.path.join(self.conf, "watchlist.json"), record())
        self.assertIsNone(self.alert("www.exfil.example"))

    def open_web(self):
        write(os.path.join(self.conf, "open-test"), f"{int(time.time()) + 600}\n")

    def blocked_reason(self, host):
        reasons = [line.rsplit(" (", 1)[1].rstrip(")") for line in self.log_text().splitlines()
                   if f"] BLOCKED {host} (" in line]
        return reasons[-1] if reasons else None

    def test_a_broken_record_keeps_the_last_good_state_for_alerts(self):
        self.assertEqual(self.alert("a.exfil.example"), ["exfil"])
        write(os.path.join(self.conf, "watchlist.json"), "{kaputt")
        write(os.path.join(self.conf, "legitimate.json"), '{"entries": 5}')
        self.assertEqual(self.alert("b.exfil.example"), ["exfil"])
        self.assertIsNone(self.alert("www.cdn.registry.example"))
        self.assertEqual(self.proxy.poll(), None)

    def test_a_deleted_watchlist_keeps_the_last_good_state_for_alerts(self):
        self.assertEqual(self.alert("a.exfil.example"), ["exfil"])
        os.remove(os.path.join(self.conf, "watchlist.json"))
        os.remove(os.path.join(self.conf, "suspicious-domains"))
        self.assertEqual(self.alert("b.exfil.example"), ["exfil"])
        self.open_web()
        self.alert("free.example")
        self.assertEqual(self.blocked_reason("free.example"), "Watchlist unreadable, Open web closed")

    def test_a_broken_watchlist_closes_open_web_also_with_a_last_good_state(self):
        # nothing is allowlisted here: a destination Open web let through would be resolved,
        # so every host below is blocked either way, and the log says why
        self.assertEqual(self.alert("a.exfil.example"), ["exfil"])
        self.open_web()
        write(os.path.join(self.conf, "watchlist.json"), "{kaputt")
        self.assertEqual(self.alert("b.exfil.example"), ["exfil"])
        self.assertEqual(self.blocked_reason("b.exfil.example"), "Watchlist unreadable, Open web closed")
        self.assertIsNone(self.alert("free.example"))
        self.assertEqual(self.blocked_reason("free.example"), "Watchlist unreadable, Open web closed")
        # repaired: Open web is open again, strong Signals stay blocked
        write(os.path.join(self.conf, "watchlist.json"), record(watch("exfil.example")))
        self.alert("c.exfil.example")
        self.assertEqual(self.blocked_reason("c.exfil.example"), "suspicious: exfil")
        # a broken record of the Legitimate domains only adds Signals: Open web stays
        write(os.path.join(self.conf, "legitimate.json"), "{kaputt")
        self.alert("d.exfil.example")
        self.assertEqual(self.blocked_reason("d.exfil.example"), "suspicious: exfil")

    def test_a_broken_watchlist_moved_away_and_back_still_closes_open_web(self):
        # back under its name the broken file is the same file (inode, mtime, size): it must not
        # count as the last good state it stood for while it was broken
        path = os.path.join(self.conf, "watchlist.json")
        self.assertEqual(self.alert("a.exfil.example"), ["exfil"])
        self.open_web()
        write(path, "{kaputt")
        self.alert("free.example")
        self.assertEqual(self.blocked_reason("free.example"), "Watchlist unreadable, Open web closed")
        os.rename(path, path + ".away")
        # meanwhile the text file from before stands in for it
        self.alert("x.old.example")
        self.assertEqual(self.blocked_reason("x.old.example"), "suspicious: exfil")
        os.rename(path + ".away", path)
        self.alert("back.example")
        self.assertEqual(self.blocked_reason("back.example"), "Watchlist unreadable, Open web closed")

    def test_a_broken_watchlist_closes_open_web_from_the_start(self):
        write(os.path.join(self.conf, "watchlist.json"), '{"entries": "x"}')
        self.open_web()
        self.alert("free.example")
        self.assertEqual(self.blocked_reason("free.example"), "Watchlist unreadable, Open web closed")

    def test_a_missing_watchlist_closes_open_web_unless_the_text_file_is_there(self):
        os.remove(os.path.join(self.conf, "watchlist.json"))
        self.open_web()
        # the text file from before stands in for it
        self.assertEqual(self.alert("x.old.example"), ["exfil"])
        self.assertEqual(self.blocked_reason("x.old.example"), "suspicious: exfil")
        os.remove(os.path.join(self.conf, "suspicious-domains"))
        self.alert("free.example")
        self.assertEqual(self.blocked_reason("free.example"), "Watchlist unreadable, Open web closed")


# --- the self-test with records ---------------------------------------------------------

class SelfTest(unittest.TestCase):
    def run_self_test(self, conf):
        r = subprocess.run([sys.executable, "-I", SUSPICIOUS, "--self-test"], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "AGENTBOX_CONF_DIR": conf},
                           timeout=120)
        return r.returncode, r.stdout, r.stderr

    def test_passes_with_the_records_and_checks_one_of_their_entries(self):
        with tempfile.TemporaryDirectory() as conf:
            write(os.path.join(conf, "watchlist.json"), record(watch("tunnel.example", "tunnel"), watch("zz.example")))
            write(os.path.join(conf, "legitimate.json"), record(legit("registry.example")))
            code, out, err = self.run_self_test(conf)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("ok      selftest.tunnel.example: expected tunnel, got tunnel", out)
        self.assertIn("ok      cdn.legit-selftest.example: expected -, got -", out)
        # its lists are stored as fetched: an entry above a Legitimate domain counts for its neighbours
        self.assertIn("ok      cdn.vendor-selftest.example: expected -, got -", out)
        self.assertIn("ok      api.vendor-selftest.example: expected threat, got threat", out)
        # a broken Watchlist closes Open web
        self.assertIn("ok      free-selftest.example: Open web: expected blocked for Watchlist unreadable", out)

    def test_ordinary_entries_of_the_host_do_not_fail_it(self):
        # entries at or above the real names the self-test checks: their category is expected too
        with tempfile.TemporaryDirectory() as conf:
            write(os.path.join(conf, "watchlist.json"), record(
                watch("githubusercontent.com", "payload"), watch("npmjs.com", "c2"), watch("example.org", "paste"),
                watch("pypx.com", "exfil")))
            write(os.path.join(conf, "legitimate.json"), record(legit("githbu.com"), legit("example.org")))
            code, out, err = self.run_self_test(conf)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("ok      objects.githubusercontent.com: expected payload, got payload", out)
        self.assertIn("ok      npmjs.com: expected c2, got c2", out)

    def test_needs_no_particular_entry(self):
        with tempfile.TemporaryDirectory() as conf:
            write(os.path.join(conf, "watchlist.json"), record())
            write(os.path.join(conf, "legitimate.json"), record())
            code, out, _ = self.run_self_test(conf)
        self.assertEqual(code, 0, out)

    def test_fails_with_a_broken_record(self):
        with tempfile.TemporaryDirectory() as conf:
            write(os.path.join(conf, "watchlist.json"), record())
            write(os.path.join(conf, "legitimate.json"), "{kaputt")
            code, out, _ = self.run_self_test(conf)
        self.assertEqual(code, 1)
        self.assertIn("FAILED  lists of the Host:", out)
        # a single broken Watchlist entry, as the proxy sees it
        with tempfile.TemporaryDirectory() as conf:
            write(os.path.join(conf, "watchlist.json"), record(watch("a.example", "exfill")))
            write(os.path.join(conf, "legitimate.json"), record())
            code, out, _ = self.run_self_test(conf)
        self.assertEqual(code, 1)
        self.assertIn("watchlist.json: broken entry (unknown category exfill)", out)

    def test_fails_with_a_broken_legitimate_entry(self):
        with tempfile.TemporaryDirectory() as conf:
            write(os.path.join(conf, "watchlist.json"), record())
            write(os.path.join(conf, "legitimate.json"), record(legit("a.example"), legit("a.example")))
            code, out, _ = self.run_self_test(conf)
        self.assertEqual(code, 1)
        self.assertIn("legitimate.json: a.example listed twice", out)

    def test_fails_without_a_list(self):
        with tempfile.TemporaryDirectory() as conf:
            write(os.path.join(conf, "watchlist.json"), record())
            code, out, _ = self.run_self_test(conf)
        self.assertEqual(code, 1)
        self.assertIn("legitimate.json and legitimate-domains missing", out)


class ClassifierConstantsTest(unittest.TestCase):
    def test_categories_are_those_of_the_seed_and_strong_as_the_classifier_says(self):
        r = subprocess.run([sys.executable, "-I", SUSPICIOUS, "--constants"], capture_output=True, text=True,
                           timeout=30)
        c = json.loads(r.stdout)
        self.assertEqual(c["categories"], {"exfil": True, "c2": True, "tunnel": True, "paste": True,
                                           "phishing": True, "blockchain": True, "payload": False})
        with open(os.path.join(ROOT, "proxy", "suspicious-domains")) as f:
            used = {line.split()[1] for line in f if line.split() and not line.startswith("#")}
        self.assertLessEqual(used, set(c["categories"]))
        self.assertIn("github.com", c["genuine"])

    def test_activity_knows_the_same_categories(self):
        # it names only a known category in a description and cannot ask the classifier for them
        r = subprocess.run([sys.executable, "-I", SUSPICIOUS, "--constants"], capture_output=True, text=True,
                           timeout=30)
        sys.path.insert(0, ROOT)
        from agentbox import activity
        self.assertEqual(list(activity.WATCH_CATEGORIES), list(json.loads(r.stdout)["categories"]))


if __name__ == "__main__":
    unittest.main()
