"""Control Room backend (`web/agentbox-web`): the Allowlist endpoints of #14, against a
fake `agentbox` (see fake_agentbox.py), with the server harness of test_control_room.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest, failure  # noqa: E402

# the throwaway certificate and password of the harness
setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule

ENTRIES = [
    {"domain": "github.com", "scope": "all", "expires": None, "note": "", "added": 1793700000, "added_by": "migration",
     "expired": False, "remaining": None, "uses_24h": 12},
    {"domain": "docs.rs", "scope": "priv", "expires": 1793703600, "note": "Rust", "added": 1793700000,
     "added_by": "Firefox/Linux (192.0.2.23)", "expired": False, "remaining": 3540, "uses_24h": 3},
]
ENTRY = ENTRIES[1]


class AllowlistTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()

    def allow(self, body):
        return self.request("POST", "/api/v1/allowlist", body)[:2]

    def assert_never_forced(self):
        for argv in self.raw_calls():
            self.assertNotIn("--force", argv)
            self.assertFalse(any(a.startswith("--force") for a in argv), argv)


class ListTest(AllowlistTest):
    def test_every_entry(self):
        self.respond(ENTRIES)
        self.assertEqual(self.get("/api/v1/allowlist"), (200, ENTRIES))
        self.assertEqual(self.calls(), [["allowlist", "--json"]])

    def test_filter_by_vm_or_all(self):
        self.respond(ENTRIES)
        self.assertEqual(self.get("/api/v1/allowlist?vm=priv")[0], 200)
        self.assertEqual(self.get("/api/v1/allowlist?vm=all")[0], 200)
        self.assertEqual(self.get("/api/v1/allowlist?vm=")[0], 200)
        self.assertEqual(self.calls(), [["allowlist", "priv", "--json"], ["allowlist", "all", "--json"],
                                        ["allowlist", "--json"]])

    def test_odd_filters_never_reach_agentbox(self):
        for query, status in (("vm=--force", 404), ("vm=Priv", 404), ("vm=a", 404), ("vm=priv&vm=work", 400)):
            with self.subTest(query=query):
                self.assertEqual(self.get(f"/api/v1/allowlist?{query}")[0], status)
        self.assertEqual(self.calls(), [])

    def test_needs_a_session(self):
        self.cookie = None
        self.assertError(self.request("GET", "/api/v1/allowlist"), 401, "unauthenticated")
        self.assertEqual(self.calls(), [])


class AllowTest(AllowlistTest):
    def test_permanent_by_default(self):
        self.respond(ENTRY)
        self.assertEqual(self.allow({"scope": "work", "domain": " Example.org "}), (201, ENTRY))
        self.assertEqual(self.calls(), [["allow", "work", "example.org", "--json"]])

    def test_for_all_vms_with_duration_and_note(self):
        self.respond(ENTRY)
        self.allow({"scope": "all", "domain": "example.org", "duration": "8h", "note": "Doku"})
        self.allow({"scope": "all", "domain": "example.org", "duration": "permanent", "note": ""})
        self.assertEqual(self.calls(), [["allow", "all", "example.org", "--for=8h", "--note=Doku", "--json"],
                                        ["allow", "all", "example.org", "--note=", "--json"]])

    def test_a_blocked_domain_with_a_duration_or_the_default(self):
        self.respond(ENTRY)
        for duration in ("15min", "1h", "8h", "7d"):
            self.allow({"scope": "priv", "domain": "blocked.example", "duration": duration})
        self.allow({"scope": "priv", "domain": "blocked.example", "duration": "default"})
        self.assertEqual(self.calls(), [["allow", "priv", "blocked.example", f"--for={d}", "--json"]
                                        for d in ("15min", "1h", "8h", "7d")]
                         + [["allow", "priv", "blocked.example", "--temporary", "--json"]])

    def test_strong_signals_are_refused_with_the_explanation(self):
        reason = "evil.example is suspicious (exfil). Allowing it only with --force"
        self.respond("", stderr=failure("domain_suspicious", reason, domain="evil.example"), exitcode=1)
        answer = self.request("POST", "/api/v1/allowlist", {"scope": "priv", "domain": "evil.example",
                                                            "duration": "1h"})
        self.assertError(answer, 409, "domain_suspicious")
        self.assertEqual(answer[1]["error"]["detail"], reason)
        self.assert_never_forced()

    def test_never_forced(self):
        self.respond(ENTRY)
        for body in ({"force": True}, {"--force": True}, {"note": "--force"}, {"note": " --force"},
                     {"options": ["--force"]}):
            with self.subTest(body=body):
                self.allow({"scope": "priv", "domain": "evil.example", **body})
        for method, path, body in (("POST", "/api/v1/allowlist/priv/evil.example/extend",
                                    {"duration": "1h", "force": True}),
                                   ("POST", "/api/v1/allowlist/priv/evil.example/make-permanent", {"force": True}),
                                   ("PUT", "/api/v1/allowlist/priv/evil.example/note", {"note": "--force"}),
                                   ("POST", "/api/v1/allowlist/priv/regex/make-permanent?regex=--force",
                                    {"force": True})):
            self.request(method, path, body)
        self.assertEqual(len(self.raw_calls()), 9)
        self.assertEqual(self.calls()[2], ["allow", "priv", "evil.example", "--note=--force", "--json"])
        self.assert_never_forced()

    def test_odd_values_are_rejected_not_converted(self):
        for body in ({"domain": "--force"}, {"domain": "-x.example"}, {}, {"domain": ["a.example"]},
                     {"domain": "a.example", "duration": "2h"}, {"domain": "a.example", "duration": 3600},
                     {"domain": "a.example", "duration": ["1h"]}, {"domain": "a.example", "duration": True},
                     {"domain": "a.example", "note": 5}, {"domain": "a.example", "note": ["x"]}):
            with self.subTest(body=body):
                self.assertError(self.request("POST", "/api/v1/allowlist", {"scope": "priv", **body}), 400,
                                 "invalid_field")
        self.assertEqual(self.calls(), [])


class ChangeTest(AllowlistTest):
    def test_extend(self):
        self.respond(ENTRY)
        self.assertEqual(self.request("POST", "/api/v1/allowlist/priv/docs.rs/extend", {"duration": "1h"})[:2],
                         (200, ENTRY))
        self.assertEqual(self.calls(), [["extend", "priv", "docs.rs", "--for=1h", "--json"]])

    def test_extend_needs_a_duration(self):
        for duration in (None, "permanent", "default", "1d"):
            with self.subTest(duration=duration):
                self.assertError(self.request("POST", "/api/v1/allowlist/priv/docs.rs/extend", {"duration": duration}),
                                 400, "invalid_field")
        self.assertEqual(self.calls(), [])

    def test_make_permanent_and_remove(self):
        self.respond(ENTRY)
        self.request("POST", "/api/v1/allowlist/all/docs.rs/make-permanent", {})
        self.request("DELETE", "/api/v1/allowlist/all/docs.rs")
        self.assertEqual(self.calls(), [["make-permanent", "all", "docs.rs", "--json"],
                                        ["disallow", "all", "docs.rs", "--json"]])

    def test_set_note(self):
        self.respond(ENTRY)
        self.request("PUT", "/api/v1/allowlist/priv/docs.rs/note", {"note": "Rust-Doku"})
        self.request("PUT", "/api/v1/allowlist/priv/docs.rs/note", {"note": ""})
        self.assertEqual(self.calls(), [["set-note", "priv", "docs.rs", "--note=Rust-Doku", "--json"],
                                        ["set-note", "priv", "docs.rs", "--note=", "--json"]])
        self.assertError(self.request("PUT", "/api/v1/allowlist/priv/docs.rs/note", {}), 400, "invalid_field")
        self.assertEqual(len(self.calls()), 2)

    def test_changes_need_a_session_and_the_header(self):
        changes = (("POST", "/api/v1/allowlist", {"scope": "priv", "domain": "docs.rs"}),
                   ("POST", "/api/v1/allowlist/priv/docs.rs/extend", {"duration": "1h"}),
                   ("POST", "/api/v1/allowlist/priv/docs.rs/make-permanent", {}),
                   ("PUT", "/api/v1/allowlist/priv/docs.rs/note", {"note": "x"}),
                   ("DELETE", "/api/v1/allowlist/priv/docs.rs", None))
        for method, path, body in changes:
            with self.subTest(path=path, method=method):
                self.assertError(self.request(method, path, None, header=False), 403, "csrf_header_missing")
        self.cookie = None
        for method, path, body in changes:
            with self.subTest(path=path, method=method):
                self.assertError(self.request(method, path, body), 401, "unauthenticated")
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
