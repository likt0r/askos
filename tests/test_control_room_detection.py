"""Control Room backend (`web/agentbox-web`): the detection endpoints of #20 (domain
check, harmless list, Alerts marked as seen), against a fake `agentbox`.

Uses the server harness of test_control_room.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest, failure  # noqa: E402

REPORT = {"domain": "evil.example", "alert": True, "strong": True, "harmless": None,
          "legitimate": None, "feeds": {},
          "signals": [{"signal": "threat", "strong": True, "suppressed": False, "reason": "HaGeZi: evil.example"}]}


def setUpModule():
    test_control_room.setUpModule()


def tearDownModule():
    test_control_room.tearDownModule()


class DetectionTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()


class CheckDomainTest(DetectionTest):
    def test_answers_with_the_json_of_agentbox(self):
        self.respond(REPORT)
        self.assertEqual(self.get("/api/v1/watchlist/check?domain=%20Evil.Example%20"), (200, REPORT))
        self.assertEqual(self.calls(), [["check-domain", "evil.example", "--json"]])

    def test_failure_is_reported(self):
        self.respond("", stderr=failure("invalid_domain", "invalid domain: a_b.example", domain="a_b.example"),
                     exitcode=1)
        answer = self.request("GET", "/api/v1/watchlist/check?domain=a_b.example")
        self.assertError(answer, 400, "invalid_domain")
        self.assertEqual(answer[1]["error"]["detail"], "invalid domain: a_b.example")

    def test_missing_or_option_like_domains_never_reach_agentbox(self):
        for query in ("", "?domain=", "?domain=%20%20", "?domain=--json", "?domain=a&domain=b"):
            with self.subTest(query=query):
                status, obj = self.get("/api/v1/watchlist/check" + query)
                self.assertEqual(status, 400)
                self.assertIn(obj["error"]["code"], ("invalid_domain", "invalid_query"))
        self.assertEqual(self.calls(), [])


class HarmlessTest(DetectionTest):
    def test_list(self):
        self.respond(["githbu.com"])
        self.assertEqual(self.get("/api/v1/watchlist/harmless"), (200, ["githbu.com"]))
        self.assertEqual(self.calls(), [["harmless", "--json"]])

    def test_mark_and_unmark(self):
        self.respond({"domain": "githbu.com", "harmless": True})
        self.assertEqual(self.request("PUT", "/api/v1/watchlist/harmless/Githbu.com")[:2],
                         (200, {"domain": "githbu.com", "harmless": True}))
        self.respond({"domain": "githbu.com", "harmless": False})
        self.assertEqual(self.request("DELETE", "/api/v1/watchlist/harmless/githbu.com")[0], 200)
        # never with --force or other options: the Control Room only silences Alerts
        self.assertEqual(self.calls(), [["mark-harmless", "githbu.com", "--json"],
                                        ["unmark-harmless", "githbu.com", "--json"]])
        self.assertIn("- v1 PUT put_harmless -> ok", self.server_log())

    def test_option_like_domains_never_reach_agentbox(self):
        for method in ("PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertError(self.request(method, "/api/v1/watchlist/harmless/--force"), 400, "invalid_field")
        self.assertEqual(self.calls(), [])

    def test_requires_session_and_header(self):
        self.assertError(self.request("PUT", "/api/v1/watchlist/harmless/githbu.com", header=False), 403,
                         "csrf_header_missing")
        self.cookie = None
        self.assertError(self.request("PUT", "/api/v1/watchlist/harmless/githbu.com"), 401, "unauthenticated")
        self.assertError(self.request("GET", "/api/v1/watchlist/harmless"), 401, "unauthenticated")
        self.assertEqual(self.calls(), [])


class MarkSeenTest(DetectionTest):
    def test_all_alerts_of_a_vm(self):
        self.respond({"vm": "work", "marked": 2})
        for body in ({}, {"domain": None}, {"domain": ""}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/v1/vms/work/alerts/seen", body)[:2],
                                 (200, {"vm": "work", "marked": 2}))
        self.assertEqual(self.calls(), [["mark-seen", "work", "--json"]] * 3)

    def test_alerts_of_one_destination(self):
        self.respond({"vm": "work", "marked": 1})
        self.assertEqual(self.request("POST", "/api/v1/vms/work/alerts/seen", {"domain": "evil.example"})[0], 200)
        self.assertEqual(self.calls(), [["mark-seen", "work", "evil.example", "--json"]])

    def test_option_like_domain_is_rejected(self):
        self.assertError(self.request("POST", "/api/v1/vms/work/alerts/seen", {"domain": "--json"}), 400,
                         "invalid_field")
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
