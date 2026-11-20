"""Forbids (#39, ADR 0024): a domain the proxy refuses one VM at once, for a while or for good.

Seam C: the real proxy (`proxy/agentbox-proxy`) on a Unix socket, as in test_allowlist: a
forbidden destination is refused at once, never held, also in Open web; an Allowlist entry wins
over a Forbid; an expired or removed Forbid makes the destination held again.

Seam A: the `agentbox` CLI against a temporary root (AGENTBOX_ROOT), as in test_allowlist: forbid,
unforbid, forbid-extend, forbid-make-permanent, forbid-set-note, forbids, the expiry timer, and the
rule that keeps the Allowlist and the Forbids from overlapping.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import time
import unittest

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
        # an Allowlist entry wins over a Forbid; Open web still lets the rest through
        self.assertTrue(self.alive(allowed))
        self.assertTrue(self.alive(other))
        self.at_once("telemetry.example")
        self.assertIn("[work] BLOCKED telemetry.example (forbidden)\n", self.log())
        self.tunnel("docs.telemetry.example")
        self.assertNotIn("HELD", self.log())

    def test_an_allowlist_entry_wins_over_a_forbid(self):
        self.store(entry("telemetry.example", "all"))
        self.forbids(forbid("telemetry.example", "work"))
        self.start()
        self.tunnel("telemetry.example")
        self.assertIn("[work] ALLOWED telemetry.example (allowlist telemetry.example all)\n", self.log())

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
        self.at_once("anything.example")
        self.assertIn("[work] BLOCKED anything.example (forbidden)\n", self.log())
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
                                  "added_by": result["added_by"], "allowlist_removed": False,
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

    def test_it_is_refused_while_another_entry_in_force_allows_the_domain_for_the_vm(self):
        now = int(time.time())
        pattern = {k: v for k, v in entry("x.example", "work").items() if k != "domain"} | {
            "regex": "^eu\\.[a-z]+\\.example$"}
        for allows, refused in (
                # for all VMs, of a parent domain, a pattern: each wins over a forbid
                ([entry("telemetry.example", "all")], "the Allowlist entry telemetry.example for all VMs"),
                ([entry("telemetry.example", "work", expires=now + 600)], "the Allowlist entry telemetry.example for work"),
                ([pattern], "the Allowlist entry pattern ^eu\\.[a-z]+\\.example$ for work"),
                # another VM's, an expired one, an unrelated or narrower one: nothing in the way
                ([entry("telemetry.example", "priv"), entry("telemetry.example", "all", expires=now - 60),
                  entry("example.org", "work"), entry("x.eu.telemetry.example", "work")], None)):
            with self.subTest(allows=allows):
                self.store(*allows)
                self.forbids()
                if refused:
                    self.assertEqual(self.refused("forbid", "work", "eu.telemetry.example"),
                                     f"work: eu.telemetry.example is allowed by {refused}, which wins over a forbid; "
                                     "remove or narrow it first")
                else:
                    self.ok("forbid", "work", "eu.telemetry.example")
                    self.assertEqual(self.forbidden(), [("eu.telemetry.example", "work", None, "")])
                    self.assertEqual(len(self.stored()), 4)

    def test_the_refusal_names_the_entry_for_the_control_room(self):
        self.store(entry("example.org", "all"))
        code, out, err = self.run_agentbox("forbid", "work", "api.example.org", "--json")
        self.assertEqual((code, out), (1, ""))
        failure = json.loads(err)
        self.assertEqual((failure["error"], failure["params"]),
                         ("forbid_overruled", {"vm": "work", "domain": "api.example.org", "scope": "all", "entry": "example.org"}))


class AllowLiftsTest(CliTest):
    """An Allowlist entry wins over a Forbid, so allow, extend and make-permanent lift the Forbids the
    entry covers within its scope (ADR 0024)."""

    def setUp(self):
        super().setUp()
        self.forbids(forbid("a.example", "work"), forbid("eu.a.example", "work"), forbid("b.example", "work"),
                     forbid("a.example", "priv"), forbid("mirror7.example", "work"))

    def test_allow_lifts_the_forbids_it_covers_in_its_scope(self):
        self.assertEqual(self.run_agentbox("--actor", FIREFOX, "allow", "work", "a.example"),
                         (0, "work: a.example allowed; Forbid lifted: a.example (work), eu.a.example (work)\n", ""))
        self.assertEqual(self.forbidden(), [("a.example", "priv", None, ""), ("b.example", "work", None, ""),
                                            ("mirror7.example", "work", None, "")])
        self.assertEqual(self.activity()[:3], [
            ("allowlist", "allowlist.forbid-lifted", "work",
             "Forbid a.example for work lifted: the Allowlist entry a.example for work allows it"),
            ("allowlist", "allowlist.forbid-lifted", "work",
             "Forbid eu.a.example for work lifted: the Allowlist entry a.example for work allows it"),
            ("allowlist", "allow", "work", "Allow a.example"),
        ])
        # an entry for all VMs lifts every VM's
        self.assertEqual(self.json("allow", "all", "a.example")["forbids_lifted"], [{"domain": "a.example", "scope": "priv"}])
        # a Regex entry lifts what its pattern matches
        self.assertEqual(self.json("allow", "work", "--regex", "^mirror[0-9]+\\.example$", "--confirm-pattern")
                         ["forbids_lifted"], [{"domain": "mirror7.example", "scope": "work"}])
        self.assertEqual(self.forbidden(), [("b.example", "work", None, "")])

    def test_extend_or_make_permanent_of_an_expired_entry_lifts_them_too(self):
        now = int(time.time())
        self.store(entry("b.example", "work", expires=now - 60), entry("a.example", "work", expires=now - 60))
        self.assertEqual(self.json("extend", "work", "b.example", "--for", "1h")["forbids_lifted"],
                         [{"domain": "b.example", "scope": "work"}])
        self.assertEqual(self.json("make-permanent", "work", "a.example")["forbids_lifted"],
                         [{"domain": "a.example", "scope": "work"}, {"domain": "eu.a.example", "scope": "work"}])
        self.assertEqual(self.forbidden(), [("a.example", "priv", None, ""), ("mirror7.example", "work", None, "")])

    def test_a_subdomain_allowed_leaves_the_parents_forbid(self):
        self.assertEqual(self.json("allow", "work", "x.b.example")["forbids_lifted"], [])
        self.assertIn(("b.example", "work", None, ""), self.forbidden())


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
