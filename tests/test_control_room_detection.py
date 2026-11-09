"""Control Room backend (`web/agentbox-web`): the detection endpoints of #20 (domain
check, harmless list, Alerts marked as seen), against a fake `agentbox`.

Uses the server harness of test_control_room.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import http.client
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest, failure  # noqa: E402

REPORT = {"domain": "evil.example", "port": 443, "alert": True, "strong": True, "harmless": None,
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

    def get(self, path):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ssl, timeout=60)
        try:
            conn.request("GET", path, headers={"Cookie": self.cookie} if self.cookie else {})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()


class CheckDomainTest(DetectionTest):
    def test_answers_with_the_json_of_agentbox(self):
        self.respond(REPORT)
        status, obj, _ = self.post("/api/check-domain", {"domain": " Evil.Example "})
        self.assertEqual((status, obj), (200, REPORT))
        self.assertEqual(self.calls(), [["check-domain", "evil.example", "--json"]])

    def test_failure_is_reported(self):
        self.respond("", stderr=failure("invalid_domain", "invalid domain: a_b.example", domain="a_b.example"),
                     exitcode=1)
        status, obj, _ = self.post("/api/check-domain", {"domain": "a_b.example"})
        self.assertEqual((status, obj), (400, {"ok": False, "message": "invalid domain: a_b.example"}))

    def test_missing_or_option_like_domains_never_reach_agentbox(self):
        for body in ({}, {"domain": ""}, {"domain": "  "}, {"domain": "--json"}, {"domain": ["x.example"]},
                     {"domain": 5}, {"domain": None}):
            with self.subTest(body=body):
                status, obj, _ = self.post("/api/check-domain", body)
                self.assertEqual(status, 400)
                self.assertFalse(obj["ok"])
        self.assertEqual(self.calls(), [])


class HarmlessTest(DetectionTest):
    def test_list(self):
        self.respond(["githbu.com"])
        self.assertEqual(self.get("/api/harmless"), (200, ["githbu.com"]))
        self.assertEqual(self.calls(), [["harmless", "--json"]])

    def test_mark_and_unmark(self):
        self.respond("githbu.com als harmlos markiert")
        status, obj, _ = self.post("/api/mark-harmless", {"domain": "githbu.com"})
        self.assertEqual((status, obj), (200, {"ok": True, "message": "githbu.com als harmlos markiert"}))
        self.respond("githbu.com nicht mehr als harmlos markiert")
        status, obj, _ = self.post("/api/unmark-harmless", {"domain": "githbu.com"})
        self.assertEqual(status, 200)
        # never with --force or other options: the Control Room only silences Alerts
        self.assertEqual(self.calls(), [["mark-harmless", "githbu.com"], ["unmark-harmless", "githbu.com"]])
        self.assertIn("- mark-harmless -> ok: githbu.com als harmlos markiert", self.server_log())

    def test_requires_session_and_header(self):
        self.cookie = None
        self.assertEqual(self.post("/api/mark-harmless", {"domain": "githbu.com"})[0], 401)
        self.assertEqual(self.get("/api/harmless")[0], 401)
        self.login()
        self.assertEqual(self.post("/api/mark-harmless", raw=b"", header=False)[0], 403)
        self.assertEqual(self.calls(), [])

    def test_unknown_global_actions(self):
        for path in ("/api/allow", "/api/harmless", "/api/check-domain/x"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {"domain": "githbu.com"})[0], 404)
        self.assertEqual(self.calls(), [])


class MarkSeenTest(DetectionTest):
    def test_all_alerts_of_a_vm(self):
        self.respond("work: Alerts als gesehen markiert")
        for body in ({}, {"domain": None}, {"domain": ""}):
            with self.subTest(body=body):
                status, obj, _ = self.post("/api/work/mark-seen", body)
                self.assertEqual((status, obj), (200, {"ok": True, "message": "work: Alerts als gesehen markiert"}))
        self.assertEqual(self.calls(), [["mark-seen", "work"]] * 3)

    def test_alerts_of_one_destination(self):
        self.respond("work: Alerts zu evil.example als gesehen markiert")
        status, _, _ = self.post("/api/work/mark-seen", {"domain": "evil.example"})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls(), [["mark-seen", "work", "evil.example"]])

    def test_option_like_domain_is_rejected(self):
        status, obj, _ = self.post("/api/work/mark-seen", {"domain": "--json"})
        self.assertEqual(status, 400)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
