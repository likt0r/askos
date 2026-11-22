"""Forbids (#39, ADR 0024): a domain the proxy refuses one VM at once, for a while or for good.

Seam C: the real proxy (`proxy/agentbox-proxy`) on a Unix socket, as in test_allowlist: a
forbidden destination is refused at once, never held, also in Open web; of an Allowlist entry and
a Forbid the more specific wins; an expired or removed Forbid makes the destination held again.

Seam A: the `agentbox` CLI against a temporary root (AGENTBOX_ROOT), as in test_allowlist: forbid,
unforbid, forbid-extend, forbid-make-permanent, forbid-set-note, forbids, the expiry timer, and the
rule that the same domain for the same VM is never both allowed and forbidden.

Run from the repo root: python3 -m unittest discover -s tests
"""
import errno
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_allowlist as allowlist_tests  # noqa: E402
from client_hello import ACCESS_DENIED  # noqa: E402

entry, write, FIREFOX = allowlist_tests.entry, allowlist_tests.write, allowlist_tests.FIREFOX
proxy_line = allowlist_tests.proxy_line


def forbid(domain, scope, expires=None, note="", added=1793700000, added_by="bewr"):
    return {"domain": domain, "scope": scope, "expires": expires, "note": note, "added": added,
            "added_by": added_by}


# --- Seam C: the proxy -----------------------------------------------------------------

class ProxyTest(allowlist_tests.ProxyTest):
    """The proxy of VM `work`, holding for 30 s (HoldTest's helpers); the Watchlist is the text
    file from before: exfil.example (exfil)."""

    held = allowlist_tests.HoldTest.held
    holding = allowlist_tests.HoldTest.holding
    answered = allowlist_tests.HoldTest.answered
    at_once = allowlist_tests.HoldTest.at_once

    def setUp(self):
        super().setUp()
        self.hellos = {}
        self.hold(30)

    def forbids(self, *entries, raw=None):
        write(os.path.join(self.conf, "forbids.json"),
              raw if raw is not None else json.dumps({"entries": list(entries)}))

    def test_a_forbidden_destination_is_refused_at_once_without_a_hold_its_subdomains_too(self):
        self.forbids(forbid("telemetry.example", "work"))
        self.start()
        for host in ("telemetry.example", "eu.telemetry.example"):
            with self.subTest(host=host):
                self.at_once(host)
                self.assertIn(f"[work] BLOCKED {host} (forbidden)\n", self.log())
        # a neighbour is unknown: held as before
        s = self.held("other.example")
        s.close()
        self.assertNotIn("HELD telemetry.example", self.log())

    def test_a_forbid_of_another_vm_changes_nothing_here(self):
        self.forbids(forbid("telemetry.example", "priv"))
        self.start()
        s = self.held("telemetry.example")
        self.assertIn("[work] HELD telemetry.example (up to 30 s)\n", self.log())
        self.forbids(forbid("telemetry.example", "priv"), forbid("telemetry.example", "work"))
        # a forbid added during the hold refuses the held connection at once
        started = time.monotonic()
        self.assertEqual(self.answered(s), ACCESS_DENIED)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("[work] HELD telemetry.example (up to 30 s)\n[work] BLOCKED telemetry.example (forbidden)\n",
                      self.log())

    def test_an_expired_or_removed_forbid_makes_the_destination_held_again(self):
        self.forbids(forbid("telemetry.example", "work", expires=int(time.time()) - 1))
        self.start()
        s = self.held("telemetry.example")
        s.close()
        self.forbids(forbid("telemetry.example", "work", expires=int(time.time()) + 600))
        time.sleep(allowlist_tests.SETTLE)
        self.at_once("eu.telemetry.example")
        self.forbids()
        s = self.held("eu.telemetry.example")
        self.assertIn("[work] HELD eu.telemetry.example (up to 30 s)\n", self.log())
        s.close()

    def test_a_forbid_blocks_in_open_web_too_and_closes_the_tunnels_only_open_web_allows(self):
        self.open_web()
        self.store(entry("docs.telemetry.example", "work"))
        self.start()
        web = self.tunnel("telemetry.example")
        allowed = self.tunnel("docs.telemetry.example")
        other = self.tunnel("other.example")
        self.forbids(forbid("telemetry.example", "work"))
        self.assertTrue(self.closed(web))
        self.assertIn("[work] CLOSED telemetry.example (forbidden)\n", self.log())
        # a more specific entry wins over the Forbid; Open web still lets the rest through
        self.assertTrue(self.alive(allowed))
        self.assertTrue(self.alive(other))
        self.at_once("telemetry.example")
        self.assertIn("[work] BLOCKED telemetry.example (forbidden)\n", self.log())
        self.tunnel("docs.telemetry.example")
        self.assertNotIn("HELD", self.log())

    def test_a_forbid_of_a_subdomain_under_an_entry_for_all_vms_blocks_for_that_vm_only(self):
        self.store(entry("forge.example", "all"))
        self.forbids(forbid("gist.forge.example", "work"), forbid("raw.forge.example", "priv"))
        self.start()
        for host in ("gist.forge.example", "eu.gist.forge.example"):
            with self.subTest(host=host):
                self.at_once(host)
                self.assertIn(f"[work] BLOCKED {host} (forbidden)\n", self.log())
        self.tunnel("forge.example")
        self.tunnel("raw.forge.example")       # priv's Forbid
        self.assertIn("[work] ALLOWED raw.forge.example (allowlist forge.example all)\n", self.log())

    def test_a_more_specific_entry_under_a_forbid_passes(self):
        self.forbids(forbid("gist.forge.example", "work"))
        self.store(entry("api.gist.forge.example", "all"))
        self.start()
        self.tunnel("v2.api.gist.forge.example")
        self.assertIn("[work] ALLOWED v2.api.gist.forge.example (allowlist api.gist.forge.example all)\n",
                      self.log())
        self.at_once("gist.forge.example")

    def test_at_the_same_domain_one_vms_beats_all_vms_and_a_full_tie_goes_to_the_forbid(self):
        self.store(entry("x.example", "all"), entry("y.example", "work"))
        # agentbox never stores y.example as both for work; a hand edit can
        self.forbids(forbid("x.example", "work"), forbid("y.example", "work"))
        self.start()
        for host in ("x.example", "y.example"):
            with self.subTest(host=host):
                self.at_once(host)
                self.assertIn(f"[work] BLOCKED {host} (forbidden)\n", self.log())

    def test_a_named_domain_beats_a_pattern(self):
        self.store({k: v for k, v in entry("x.example", "work").items() if k != "domain"}
                   | {"regex": "^mirror[0-9]+\\.vendor\\.example$"})
        # a Forbid of a shorter domain still beats the pattern
        self.forbids(forbid("vendor.example", "work"))
        self.start()
        self.at_once("mirror1.vendor.example")
        self.assertIn("[work] BLOCKED mirror1.vendor.example (forbidden)\n", self.log())
        self.forbids()
        time.sleep(allowlist_tests.SETTLE)
        self.tunnel("mirror1.vendor.example")

    def test_a_forbid_that_comes_to_win_closes_the_tunnels_it_wins_for(self):
        self.store(entry("forge.example", "all"), entry("api.gist.forge.example", "work"))
        self.start()
        gist = self.tunnel("gist.forge.example")
        api = self.tunnel("api.gist.forge.example")
        top = self.tunnel("forge.example")
        self.forbids(forbid("gist.forge.example", "work"))
        self.assertTrue(self.closed(gist))
        self.assertIn("[work] CLOSED gist.forge.example (forbidden)\n", self.log())
        # the more specific entry still wins; the parent is not forbidden
        self.assertTrue(self.alive(api))
        self.assertTrue(self.alive(top))

    def test_a_forbid_taking_the_place_of_an_entry_closes_its_tunnel_also_in_open_web(self):
        # agentbox forbid removes the VM's own entry: access ends on purpose (ADR 0005), and
        # Open web does not keep a tunnel a Forbid covers
        self.open_web()
        self.store(entry("telemetry.example", "work"))
        self.start()
        s = self.tunnel("telemetry.example")
        self.forbids(forbid("telemetry.example", "work"))
        self.store()
        self.assertTrue(self.closed(s))
        self.assertRegex(self.log(), r"\[work\] CLOSED telemetry\.example \((removed from the Allowlist|forbidden)\)\n")

    def test_strong_signals_come_first(self):
        self.forbids(forbid("exfil.example", "work"))
        self.start()
        self.at_once("upload.exfil.example")
        self.assertIn("[work] BLOCKED upload.exfil.example (suspicious: exfil)\n", self.log())

    def test_a_broken_file_keeps_the_forbids_before_it_and_a_starting_proxy_holds_nothing(self):
        self.forbids(raw="{broken")
        self.store(entry("ok.example", "work"))
        self.open_web()
        self.start()
        self.assertIn("forbids.json unreadable (not valid JSON), every destination no entry allows is refused until "
                      "it is fixed", self.log())
        # its own reason: no Forbid names it
        self.at_once("anything.example")
        self.assertIn("[work] BLOCKED anything.example (forbids unreadable)\n", self.log())
        self.tunnel("ok.example")
        # fixed: Open web again; broken again: the last good state stays
        self.forbids(forbid("telemetry.example", "work"))
        time.sleep(allowlist_tests.SETTLE)
        self.tunnel("anything.example")
        self.forbids(raw='{"entries": [{"domain": "x.example", "scope": "all"}]}')
        time.sleep(allowlist_tests.SETTLE)
        self.tunnel("anything.example")
        self.at_once("telemetry.example")
        self.assertIn("the previous forbids stay", self.log())


class ForbidsFileTest(unittest.TestCase):
    """How the proxy reads forbids.json, as a module (test_client_hello.load_proxy): an error
    reading or stat'ing it is tried again at the next check, and before any good read the proxy
    fails closed, as for a broken file; each lasting error is logged once."""

    def setUp(self):
        from test_client_hello import load_proxy
        self.proxy = load_proxy()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "forbids.json")
        write(self.path, json.dumps({"entries": [forbid("ads.example", "work")]}))
        self.said = []
        for patch in (mock.patch.object(self.proxy, "CONF_DIR", tmp.name),
                      mock.patch.object(self.proxy, "log", self.said.append)):
            patch.start()
            self.addCleanup(patch.stop)
        self.forbids = self.proxy.Forbids("work")

    def failing(self, call, error):
        """os.stat or open (call) failing with error for forbids.json, as it does for others."""
        real = getattr(os, call) if call == "stat" else open

        def fake(path, *args, **kwargs):
            if str(path) == self.path:
                raise OSError(error, os.strerror(error), path)
            return real(path, *args, **kwargs)
        return mock.patch("os.stat" if call == "stat" else "builtins.open", fake)

    def verdict(self, host="other.example"):
        return self.proxy.allowance("work", self.proxy.Allowlist("work"), self.forbids, host, time.time(), False)

    def test_a_read_error_is_tried_again_at_the_next_check_failing_closed_meanwhile(self):
        with self.failing("open", errno.EIO):
            self.assertEqual(self.forbids.current(), {})
            self.forbids.current()
        self.assertTrue(self.forbids.unreadable)
        self.assertEqual(self.verdict(), (None, "forbids unreadable"))
        # the file has not changed, yet the next check reads it
        self.assertEqual(self.forbids.current(), {"ads.example": None})
        self.assertFalse(self.forbids.unreadable)
        self.assertEqual(self.verdict(), (None, "not on the Allowlist"))
        self.assertEqual(self.verdict("ads.example"), (None, "forbidden"))
        self.assertEqual(self.said, [f"[work] ERROR {self.path} unreadable (OSError), every destination no entry "
                                     "allows is refused until it is fixed"])

    def test_a_stat_error_before_any_good_read_fails_closed_after_one_keeps_it(self):
        with self.failing("stat", errno.EACCES):
            self.forbids.current()
            self.forbids.current()
        self.assertTrue(self.forbids.unreadable)
        self.assertEqual(self.verdict(), (None, "forbids unreadable"))
        self.assertEqual(len(self.said), 1)
        self.forbids.current()
        with self.failing("stat", errno.EACCES), self.failing("open", errno.EACCES):
            self.assertEqual(self.forbids.current(), {"ads.example": None})
        self.assertFalse(self.forbids.unreadable)
        self.assertEqual(self.verdict(), (None, "not on the Allowlist"))
        self.assertEqual(self.said[1], f"[work] ERROR {self.path} unreadable (PermissionError), the previous "
                                       "forbids stay")

    def test_a_missing_file_is_no_forbids(self):
        os.remove(self.path)
        self.assertEqual(self.forbids.current(), {})
        self.assertFalse(self.forbids.unreadable)
        self.assertEqual(self.verdict(), (None, "not on the Allowlist"))


# --- Seam A: the CLI ---------------------------------------------------------------------

class CliTest(allowlist_tests.CliTest):
    def forbids(self, *entries):
        write(os.path.join(self.conf, "forbids.json"), json.dumps({"entries": list(entries)}))

    def forbidden(self):
        """(domain, scope, expires, note) of every stored Forbid."""
        try:
            with open(os.path.join(self.conf, "forbids.json")) as f:
                return [(e["domain"], e["scope"], e["expires"], e["note"]) for e in json.load(f)["entries"]]
        except FileNotFoundError:
            return []


class ForbidTest(CliTest):
    def test_permanent_and_temporary(self):
        self.assertEqual(self.run_agentbox("--actor", FIREFOX, "forbid", "work", "telemetry.example"),
                         (0, "work: telemetry.example forbidden\n", ""))
        result = self.json("forbid", "priv", "telemetry.example", "--for", "1h", "--note", " Telemetrie ")
        self.assertAlmostEqual(result.pop("added"), time.time(), delta=60)
        expires = result.pop("expires")
        self.assertAlmostEqual(expires, time.time() + 3600, delta=60)
        self.assertEqual(result, {"domain": "telemetry.example", "scope": "priv", "note": "Telemetrie",
                                  "added_by": result["added_by"], "allowlist_removed": False, "allowlist_unreadable": None,
                                  "message": f"priv: telemetry.example forbidden until {self.until(expires)}"})
        self.ok("forbid", "work", "ads.example", "--temporary")
        # stored by VM, then domain
        self.assertEqual(self.forbidden(), [("telemetry.example", "priv", expires, "Telemetrie"),
                                            ("ads.example", "work", self.forbidden()[1][2], ""),
                                            ("telemetry.example", "work", None, "")])
        self.assertAlmostEqual(self.forbidden()[1][2], time.time() + 3600, delta=60)     # temporary_entry_default
        self.assertEqual(os.stat(os.path.join(self.conf, "forbids.json")).st_mode & 0o777, 0o644)
        self.assertEqual(self.activity()[:2], [
            ("allowlist", "forbid", "work", "Forbid telemetry.example"),
            ("allowlist", "forbid", "priv", "Forbid telemetry.example for 1 hour"),
        ])

    def test_an_existing_forbid_is_widened_never_shortened(self):
        later = int(time.time()) + 8 * 3600
        self.forbids(forbid("a.example", "work", expires=later, note="x"), forbid("b.example", "work"))
        self.ok("forbid", "work", "a.example", "--for", "15min")
        self.ok("forbid", "work", "b.example", "--for", "15min", "--note", "y")
        self.assertEqual(self.forbidden(), [("a.example", "work", later, "x"), ("b.example", "work", None, "y")])
        self.ok("forbid", "work", "a.example")
        self.assertEqual(self.forbidden()[0], ("a.example", "work", None, "x"))

    def test_invalid_arguments(self):
        for args, message in (
                (["forbid", "all", "a.example"], "a forbid is for one VM, not all"),
                (["forbid", "nope", "a.example"], "No such VM: nope"),
                (["forbid", "work", "not a domain"], "invalid domain: not a domain"),
                (["forbid", "work", "a.example", "b.example"], "too many arguments: b.example"),
                (["forbid", "work", "a.example", "--for", "2h"], "--for must be 15min, 1h, 8h or 7d"),
                (["forbid", "work", "--regex", "^a$"], None)):
            with self.subTest(args=args):
                code, out, err = self.run_agentbox(*args)
                self.assertNotEqual(code, 0)
                if message:
                    self.assertIn(message, err)
        self.assertEqual(self.forbidden(), [])

    def test_it_takes_the_place_of_the_vms_own_entry(self):
        self.store(entry("telemetry.example", "work", note="once"), entry("telemetry.example", "priv"))
        self.assertEqual(self.json("--actor", FIREFOX, "forbid", "work", "telemetry.example")["allowlist_removed"], True)
        self.assertEqual([(e["domain"], e["scope"]) for e in self.stored()], [("telemetry.example", "priv")])
        self.assertEqual(self.forbidden(), [("telemetry.example", "work", None, "")])
        self.assertEqual(self.activity(), [
            ("allowlist", "allowlist.entry-forbidden", "work",
             "Allowlist entry telemetry.example removed: a Forbid for work takes its place"),
            ("allowlist", "forbid", "work", "Forbid telemetry.example"),
        ])

    def test_it_is_stored_under_a_broader_entry_which_stays(self):
        # the more specific wins (ADR 0024): a Forbid beats a parent domain's entry, one for all VMs
        # and a pattern, so they stay where they are
        pattern = {k: v for k, v in entry("x.example", "work").items() if k != "domain"} | {
            "regex": "^eu\\.[a-z]+\\.example$"}
        self.store(entry("telemetry.example", "work"), entry("eu.telemetry.example", "all"), pattern,
                   entry("x.eu.telemetry.example", "work"))
        before = self.stored()
        result = self.json("forbid", "work", "eu.telemetry.example")
        self.assertEqual(result["allowlist_removed"], False)
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.forbidden(), [("eu.telemetry.example", "work", None, "")])


class BrokenAllowlistTest(CliTest):
    def test_a_forbid_is_stored_while_the_allowlist_cannot_be_read(self):
        write(os.path.join(self.conf, "allowlist.json"), "{broken")
        result = self.json("--actor", FIREFOX, "forbid", "work", "ads.example", "--for", "1h")
        self.assertEqual((result["allowlist_removed"], result["allowlist_unreadable"]),
                         (False, f"{self.conf}/allowlist.json: not valid JSON"))
        self.assertIn("; the Allowlist cannot be read (", result["message"])
        self.assertIn("so an entry of ads.example for work stays where it is: the Forbid wins over it",
                      result["message"])
        self.assertEqual([f[:2] for f in self.forbidden()], [("ads.example", "work")])
        with open(os.path.join(self.conf, "allowlist.json")) as f:
            self.assertEqual(f.read(), "{broken")
        [e] = self.journald.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_PARAM_ALLOWLIST_UNREADABLE"]),
                         ("forbid", "ok", "true"))


class StillForbiddenTest(CliTest):
    """allow, extend and make-permanent name the VMs whose more specific Forbid still wins over the
    entry (forbidden_for), so "allowed" never hides a VM that stays refused."""

    def setUp(self):
        super().setUp()
        self.forbids(forbid("x.example", "work"), forbid("eu.x.example", "priv"), forbid("mirror7.example", "work"),
                     forbid("old.x.example", "work", expires=int(time.time()) - 60))

    def test_an_entry_for_all_vms_a_parent_or_a_pattern(self):
        self.assertEqual(self.run_agentbox("allow", "all", "x.example"),
                         (0, "all VMs: x.example allowed; a more specific Forbid still wins for priv (eu.x.example), "
                             "work (x.example)\n", ""))
        self.assertEqual(self.json("allow", "all", "x.example")["forbidden_for"],
                         [{"vm": "priv", "domain": "eu.x.example"}, {"vm": "work", "domain": "x.example"}])
        # for one VM: its Forbid of the domain is lifted, the other VM's is none of its business
        result = self.json("allow", "priv", "x.example")
        self.assertEqual((result["forbids_lifted"], result["forbidden_for"]),
                         ([], [{"vm": "priv", "domain": "eu.x.example"}]))
        self.assertEqual(self.json("allow", "work", "--regex", "^mirror[0-9]+\\.example$", "--confirm-pattern")
                         ["forbidden_for"], [{"vm": "work", "domain": "mirror7.example"}])
        # a more specific entry wins: nothing to name
        self.assertEqual(self.json("allow", "priv", "api.eu.x.example")["forbidden_for"], [])

    def test_extend_and_make_permanent_name_them_too(self):
        self.store(entry("x.example", "all", expires=int(time.time()) + 600))
        self.assertEqual(self.json("extend", "all", "x.example", "--for", "1h")["forbidden_for"],
                         [{"vm": "priv", "domain": "eu.x.example"}, {"vm": "work", "domain": "x.example"}])
        self.assertEqual(len(self.json("make-permanent", "all", "x.example")["forbidden_for"]), 2)


class AllowLiftsTest(CliTest):
    """The same domain for the same VM is never both allowed and forbidden: allow, extend and
    make-permanent of an entry for one VM lift that VM's Forbid of the domain; an entry for all VMs,
    a pattern or a parent domain lifts nothing, since the Forbids it meets are more specific and keep
    winning (ADR 0024)."""

    def setUp(self):
        super().setUp()
        self.forbids(forbid("a.example", "work"), forbid("eu.a.example", "work"), forbid("a.example", "priv"),
                     forbid("mirror7.example", "work"))

    def test_allow_for_a_vm_lifts_only_its_forbid_of_the_same_domain(self):
        self.assertEqual(self.run_agentbox("--actor", FIREFOX, "allow", "work", "a.example"),
                         (0, "work: a.example allowed; Forbid lifted: a.example (work); a more specific Forbid "
                             "still wins for work (eu.a.example)\n", ""))
        self.assertEqual(self.forbidden(), [("a.example", "priv", None, ""), ("eu.a.example", "work", None, ""),
                                            ("mirror7.example", "work", None, "")])
        self.assertEqual(self.activity(), [
            ("allowlist", "allowlist.forbid-lifted", "work",
             "Forbid a.example for work lifted: an Allowlist entry for work takes its place"),
            ("allowlist", "allow", "work", "Allow a.example"),
        ])

    def test_an_entry_for_all_vms_or_a_pattern_lifts_nothing(self):
        self.assertEqual(self.json("allow", "all", "a.example")["forbids_lifted"], [])
        self.assertEqual(self.json("allow", "work", "--regex", "^mirror[0-9]+\\.example$", "--confirm-pattern")
                         ["forbids_lifted"], [])
        self.assertEqual(self.json("allow", "work", "x.eu.a.example")["forbids_lifted"], [])
        self.assertEqual(len(self.forbidden()), 4)
        self.assertNotIn("allowlist.forbid-lifted", [a[1] for a in self.activity()])

    def test_extend_or_make_permanent_of_an_expired_entry_lifts_it_too(self):
        now = int(time.time())
        self.store(entry("a.example", "work", expires=now - 60), entry("eu.a.example", "work", expires=now - 60))
        self.assertEqual(self.json("extend", "work", "a.example", "--for", "1h")["forbids_lifted"],
                         [{"domain": "a.example", "scope": "work"}])
        self.assertEqual(self.json("make-permanent", "work", "eu.a.example")["forbids_lifted"],
                         [{"domain": "eu.a.example", "scope": "work"}])
        self.assertEqual(self.forbidden(), [("a.example", "priv", None, ""), ("mirror7.example", "work", None, "")])


class ChangeTest(CliTest):
    def test_unforbid(self):
        self.forbids(forbid("a.example", "work"), forbid("a.example", "priv"))
        self.assertEqual(self.json("--actor", FIREFOX, "unforbid", "work", "a.example"),
                         {"scope": "work", "domain": "a.example", "removed": True,
                          "message": "work: Forbid a.example removed"})
        self.assertEqual(self.forbidden(), [("a.example", "priv", None, "")])
        # not forbidden: changes nothing, no Activity
        self.assertEqual(self.ok("unforbid", "work", "a.example"), "work: a.example is not forbidden\n")
        self.assertEqual(self.activity(), [("allowlist", "unforbid", "work", "Remove the Forbid a.example")])

    def test_extend_make_permanent_and_note(self):
        now = int(time.time())
        self.forbids(forbid("a.example", "work", expires=now + 600), forbid("p.example", "work"))
        self.assertAlmostEqual(self.json("forbid-extend", "work", "a.example", "--for", "1h")["expires"],
                               now + 600 + 3600, delta=5)
        self.assertEqual(self.refused("forbid-extend", "work", "p.example", "--for", "1h"),
                         "work: p.example is forbidden permanently")
        self.assertEqual(self.refused("forbid-extend", "work", "nope.example", "--for", "1h"),
                         "work: nope.example is not forbidden")
        self.assertEqual(self.refused("forbid-extend", "work", "a.example"),
                         "forbid-extend needs --for 15min, 1h, 8h or 7d")
        self.assertIsNone(self.json("forbid-make-permanent", "work", "a.example")["expires"])
        self.assertEqual(self.json("forbid-set-note", "work", "a.example", "--note", "ads")["note"], "ads")
        self.assertEqual(self.forbidden(), [("a.example", "work", None, "ads"), ("p.example", "work", None, "")])
        self.assertEqual([a[1:] for a in self.activity()], [
            ("forbid-extend", "work", "Extend the Forbid a.example by 1 hour"),
            ("forbid-extend", "work", "Extend the Forbid p.example by 1 hour: work: p.example is forbidden permanently"),
            ("forbid-extend", "work", "Extend the Forbid nope.example by 1 hour: work: nope.example is not forbidden"),
            ("forbid-extend", "work", "Extend the Forbid a.example: forbid-extend needs --for 15min, 1h, 8h or 7d"),
            ("forbid-make-permanent", "work", "Make the Forbid a.example permanent"),
            ("forbid-set-note", "work", "Note on the Forbid a.example"),
        ])


class ListAndExpireTest(CliTest):
    def test_every_forbid_with_remaining_time_and_refusals(self):
        now = int(time.time())
        self.forbids(forbid("a.example", "work", expires=now + 600), forbid("b.example", "work", expires=now - 60),
                     forbid("a.example", "priv"))
        self.journal([
            proxy_line("work", "BLOCKED a.example (forbidden)", 300),
            proxy_line("work", "BLOCKED eu.a.example (forbidden)", 200),
            proxy_line("work", "BLOCKED a.example (not on the Allowlist)", 100),
            proxy_line("priv", "BLOCKED a.example (forbidden)", 100),
            proxy_line("work", "BLOCKED a.example (forbidden)", 25 * 3600),        # older than a day
        ])
        rows = {(r["domain"], r["scope"]): r for r in self.json("forbids")}
        self.assertEqual({k: (r["refused_24h"], r["expired"]) for k, r in rows.items()},
                         {("a.example", "work"): (2, False), ("b.example", "work"): (0, True),
                          ("a.example", "priv"): (1, False)})
        self.assertAlmostEqual(rows[("a.example", "work")]["remaining"], 600, delta=10)
        self.assertEqual([r["scope"] for r in self.json("forbids", "priv")], ["priv"])
        lines = self.ok("forbids", "work").splitlines()
        self.assertRegex(lines[0], r"^a\.example +work +until \d\d:\d\d +2x$")
        self.assertRegex(lines[1], r"^b\.example +work +expired +0x$")

    def test_the_timer_removes_expired_forbids_and_records_each(self):
        now = int(time.time())
        self.forbids(forbid("a.example", "work", expires=now - 60), forbid("b.example", "work", expires=now + 600))
        result = self.json("--actor=timer", "expire-allowlist")
        self.assertEqual([(e["domain"], e["scope"]) for e in result["forbids_expired"]], [("a.example", "work")])
        self.assertEqual(self.forbidden(), [("b.example", "work", now + 600, "")])
        [e] = self.journald.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_VM"], e["AGENTBOX_ACTOR"], e["AGENTBOX_PARAM_DOMAIN"]),
                         ("allowlist.forbid-expired", "work", "timer", "a.example"))
        self.assertTrue(e["AGENTBOX_DESCRIPTION"].startswith("Temporary Forbid a.example for work expired ("))


if __name__ == "__main__":
    unittest.main()
