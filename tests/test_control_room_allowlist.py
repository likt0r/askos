"""Control Room backend (`web/agentbox-web`): the Allowlist endpoints of #14, against a
fake `agentbox` (see fake_agentbox.py), with the server harness of test_control_room.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest  # noqa: E402

# the throwaway certificate and password of the harness
setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule

ENTRIES = [
    {"domain": "github.com", "scope": "all", "expires": None, "note": "", "added": 1793700000, "added_by": "migration",
     "expired": False, "remaining": None, "uses_24h": 12},
    {"domain": "docs.rs", "scope": "priv", "expires": 1793703600, "note": "Rust", "added": 1793700000,
     "added_by": "Firefox/Linux (192.0.2.23)", "expired": False, "remaining": 3540, "uses_24h": 3},
]


class AllowlistTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()

    def action(self, path, body):
        status, obj, _ = self.post(path, body)
        return status, obj

    def assert_never_forced(self):
        for argv in self.raw_calls():
            self.assertNotIn("--force", argv)
            self.assertFalse(any(a.startswith("--force") for a in argv), argv)


class ListTest(AllowlistTest):
    def test_every_entry(self):
        self.respond(ENTRIES)
        self.assertEqual(self.get("/api/allowlist"), (200, ENTRIES))
        self.assertEqual(self.calls(), [["allowlist", "--json"]])

    def test_filter_by_vm_or_all(self):
        self.respond(ENTRIES)
        self.assertEqual(self.get("/api/allowlist?vm=priv")[0], 200)
        self.assertEqual(self.get("/api/allowlist?vm=all")[0], 200)
        self.assertEqual(self.get("/api/allowlist?vm=")[0], 200)
        self.assertEqual(self.calls(), [["allowlist", "priv", "--json"], ["allowlist", "all", "--json"],
                                        ["allowlist", "--json"]])

    def test_odd_filters_never_reach_agentbox(self):
        for query in ("vm=--force", "vm=Priv", "vm=priv&vm=work", "vm=a&b=c&d=e"):
            with self.subTest(query=query):
                self.assertEqual(self.get(f"/api/allowlist?{query}")[0], 400)
        self.assertEqual(self.calls(), [])

    def test_the_vms_own_domains_for_the_current_ui(self):
        self.respond(ENTRIES)
        self.assertEqual(self.get("/api/priv/allowlist"), (200, ["docs.rs"]))
        self.assertEqual(self.calls(), [["allowlist", "priv", "--json"]])

    def test_needs_a_session(self):
        self.cookie = None
        self.assertEqual(self.get("/api/allowlist")[0], 401)
        self.assertEqual(self.calls(), [])


class AllowTest(AllowlistTest):
    def test_permanent_as_before(self):
        self.respond("work: example.org freigegeben")
        self.assertEqual(self.action("/api/work/allow", {"domain": " Example.org "}),
                         (200, {"ok": True, "message": "work: example.org freigegeben"}))
        self.assertEqual(self.calls(), [["allow", "work", "example.org"]])

    def test_for_all_vms_with_duration_and_note(self):
        self.respond("alle VMs: example.org freigegeben bis 14:05")
        self.action("/api/all/allow", {"domain": "example.org", "duration": "8h", "note": "Doku"})
        self.action("/api/all/allow", {"domain": "example.org", "duration": "permanent", "note": ""})
        self.assertEqual(self.calls(), [["allow", "all", "example.org", "--for=8h", "--note=Doku"],
                                        ["allow", "all", "example.org", "--note="]])

    def test_a_blocked_domain_with_a_duration_or_the_default(self):
        self.respond("priv: blocked.example freigegeben bis 14:05")
        for duration in ("15min", "1h", "8h", "7d"):
            self.action("/api/priv/allow", {"domain": "blocked.example", "duration": duration})
        self.action("/api/priv/allow", {"domain": "blocked.example", "duration": "default"})
        self.assertEqual(self.calls(), [["allow", "priv", "blocked.example", f"--for={d}"]
                                        for d in ("15min", "1h", "8h", "7d")]
                         + [["allow", "priv", "blocked.example", "--temporary"]])

    def test_strong_signals_are_refused_with_the_explanation(self):
        self.respond("", stderr="agentbox: evil.example is suspicious (exfil). Allowing it only with --force",
                     exitcode=1)
        self.assertEqual(self.action("/api/priv/allow", {"domain": "evil.example", "duration": "1h"}),
                         (400, {"ok": False,
                                "message": "evil.example is suspicious (exfil). Allowing it only with --force"}))

    def test_never_forced(self):
        self.respond("ok")
        for body in ({"domain": "evil.example", "force": True}, {"domain": "evil.example", "--force": True},
                     {"domain": "evil.example", "note": "--force"}, {"domain": "evil.example", "note": " --force"},
                     {"domain": "evil.example", "options": ["--force"]}):
            with self.subTest(body=body):
                self.action("/api/priv/allow", body)
        for path, body in (("/api/priv/extend", {"domain": "evil.example", "duration": "1h", "force": True}),
                           ("/api/priv/make-permanent", {"domain": "evil.example", "force": True}),
                           ("/api/priv/set-note", {"domain": "evil.example", "note": "--force"})):
            self.action(path, body)
        self.assertEqual(len(self.raw_calls()), 8)
        self.assertEqual(self.calls()[2], ["allow", "priv", "evil.example", "--note=--force"])
        self.assert_never_forced()

    def test_odd_values_are_rejected_not_converted(self):
        for body in ({"domain": "--force"}, {"domain": "-x.example"}, {}, {"domain": ["a.example"]},
                     {"domain": "a.example", "duration": "2h"}, {"domain": "a.example", "duration": 3600},
                     {"domain": "a.example", "duration": ["1h"]}, {"domain": "a.example", "duration": True},
                     {"domain": "a.example", "note": 5}, {"domain": "a.example", "note": ["x"]}):
            with self.subTest(body=body):
                status, obj = self.action("/api/priv/allow", body)
                self.assertEqual(status, 400)
                self.assertFalse(obj["ok"])
        self.assertEqual(self.calls(), [])


class ChangeTest(AllowlistTest):
    def test_extend(self):
        self.respond("priv: docs.rs freigegeben bis 15:05")
        self.assertEqual(self.action("/api/priv/extend", {"domain": "docs.rs", "duration": "1h"}),
                         (200, {"ok": True, "message": "priv: docs.rs freigegeben bis 15:05"}))
        self.assertEqual(self.calls(), [["extend", "priv", "docs.rs", "--for=1h"]])

    def test_extend_needs_a_duration(self):
        for duration in (None, "permanent", "default", "1d"):
            with self.subTest(duration=duration):
                self.assertEqual(self.action("/api/priv/extend", {"domain": "docs.rs", "duration": duration})[0], 400)
        self.assertEqual(self.calls(), [])

    def test_make_permanent_and_remove(self):
        self.respond("ok")
        self.action("/api/all/make-permanent", {"domain": "docs.rs"})
        self.action("/api/all/disallow", {"domain": "docs.rs"})
        self.assertEqual(self.calls(), [["make-permanent", "all", "docs.rs"], ["disallow", "all", "docs.rs"]])

    def test_set_note(self):
        self.respond("ok")
        self.action("/api/priv/set-note", {"domain": "docs.rs", "note": "Rust-Doku"})
        self.action("/api/priv/set-note", {"domain": "docs.rs", "note": ""})
        self.assertEqual(self.calls(), [["set-note", "priv", "docs.rs", "--note=Rust-Doku"],
                                        ["set-note", "priv", "docs.rs", "--note="]])
        self.assertEqual(self.action("/api/priv/set-note", {"domain": "docs.rs"})[0], 400)
        self.assertEqual(len(self.calls()), 2)

    def test_actions_need_a_session_and_the_header(self):
        self.cookie = None
        for action in ("allow", "extend", "make-permanent", "set-note", "disallow"):
            with self.subTest(action=action):
                self.assertEqual(self.action(f"/api/priv/{action}", {"domain": "docs.rs"})[0], 401)
        self.login()
        self.assertEqual(self.post("/api/priv/allow", raw=b"", header=False)[0], 403)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
