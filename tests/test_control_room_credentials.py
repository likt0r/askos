"""Control Room backend (`web/agentbox-web`), credentials (#18): the Claude token, the GitHub
App's ID and key, where the App is installed and the GitHub tokens per VM, against a fake
`agentbox` (see fake_agentbox.py) with the server harness of test_control_room.py.

Secrets go to agentbox on stdin only (the fake records it in stdin.jsonl) and never show up
in its arguments, in a response, or in the server's log (stderr, the Host journal in
production) or its Activity entries.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest, failure  # noqa: E402

# the throwaway certificate and password of the harness
setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule

TOKEN = "sk-ant-oat01-Geheimes-Token_0123456789abcdef"
KEY = ("-----BEGIN RSA PRIVATE KEY-----\n"
       "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
       "VTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u+qKhbwKfBstIs+bMY2Zkp18gnTxK\n"
       "-----END RSA PRIVATE KEY-----\n")
APP = {"id": 12345, "slug": "agentbox-test", "name": "Agent Box Test", "owner": "likt0r",
       "html_url": "https://github.com/apps/agentbox-test",
       "install_url": "https://github.com/apps/agentbox-test/installations/new", "stored": True}
OVERVIEW = {"app": {k: v for k, v in APP.items() if k != "stored"},
            "installations": [{"id": 11, "account": "likt0r", "type": "User", "repository_selection": "all",
                               "suspended": False, "html_url": "https://github.com/settings/installations/11",
                               "vms": ["priv"]}],
            "missing": [{"owner": "acme-corp", "vms": ["work"]}]}
STORED = {"vm": "priv", "stored": True, "length": 44}
TOKENS = {"vm": "priv", "repositories": [{"repository": "likt0r/askos", "tokens": 2,
                                          "last_token": "2026-11-03T10:00:00.000000Z", "refused": 0,
                                          "last_refused": None}],
          "requests": [], "truncated": False, "retention_days": 90}


class CredentialsTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()

    def assert_nowhere(self, secret, *responses):
        """Not in a response, the server's log, its journal entries or agentbox's arguments."""
        lines = [l.strip() for l in secret.splitlines() if len(l.strip()) >= 16 and not l.startswith("-----")]
        texts = [json.dumps(r) for r in responses] + [self.server_log(), json.dumps(self.raw_calls()),
                                                      b"".join(self.journald.raw()).decode()]
        for part in [secret.strip(), *lines]:
            for text in texts:
                self.assertNotIn(part, text)


class ClaudeTokenTest(CredentialsTest):
    def put(self, body):
        return self.request("PUT", "/api/v1/vms/priv/claude-token", body)[:2]

    def test_token_goes_on_stdin_only(self):
        self.respond(STORED)
        status, obj = self.put({"token": TOKEN})
        self.assertEqual((status, obj), (200, STORED))
        self.assertEqual(self.calls(), [["set-claude-token", "priv", "--no-prompt", "--json"]])
        self.assertEqual(self.stdins(), [TOKEN])
        self.assertIn("priv v1 PUT put_claude_token -> ok", self.server_log())
        self.assert_nowhere(TOKEN, obj)

    def test_refusal_is_shown_and_logged_without_the_token(self):
        self.respond("", stderr=failure("claude_token_rejected", "Anthropic rejects the token: authentication_error",
                                        vm="priv"), exitcode=1)
        answer = self.put({"token": TOKEN})
        self.assertError(answer, 422, "claude_token_rejected")
        self.assertEqual(answer[1]["error"]["detail"], "Anthropic rejects the token: authentication_error")
        self.assertIn("priv v1 PUT put_claude_token -> FAILED claude_token_rejected", self.server_log())
        self.assert_nowhere(TOKEN, answer[1])

    def test_a_token_echoed_by_mistake_is_hidden(self):
        wrapped = TOKEN[:20] + "\n" + TOKEN[20:] + "\n"
        self.respond("", stderr=failure("action_failed", f"broken: {TOKEN}"), exitcode=1)
        answer = self.put({"token": wrapped})
        self.assertError(answer, 500, "action_failed")
        self.assertEqual(answer[1]["error"]["detail"], "broken: [hidden]")
        self.assertEqual(self.stdins(), [wrapped])
        self.assert_nowhere(TOKEN, answer[1])

    def test_anything_but_text_is_refused(self):
        for body in ({}, {"token": ""}, {"token": "  "}, {"token": None}, {"token": [TOKEN]}, {"token": 42},
                     {"token": {"t": TOKEN}}):
            with self.subTest(body=body):
                answer = self.put(body)
                self.assertError(answer, 400, "invalid_field")
                self.assertEqual(answer[1]["error"]["field"], "token")
        self.assertEqual(self.calls(), [])
        self.assert_nowhere(TOKEN)


class SetOwnersTest(CredentialsTest):
    def put(self, body):
        return self.request("PUT", "/api/v1/vms/priv/github-owners", body)

    def test_owners_become_arguments(self):
        self.respond({"vm": "priv", "github_owners": ["likt0r", "acme-corp"]})
        self.assertEqual(self.put({"owners": [" likt0r ", "acme-corp", ""]})[0], 200)
        self.assertEqual(self.calls(), [["set-owners", "priv", "likt0r", "acme-corp", "--json"]])

    def test_odd_owners_are_refused(self):
        for body, code in (({"owners": "likt0r"}, "invalid_field"), ({"owners": [1]}, "invalid_field"),
                           ({}, "github_owner_required"), ({"owners": ["--json"]}, "invalid_field"),
                           ({"owners": ["ok", "-x"]}, "invalid_field")):
            with self.subTest(body=body):
                answer = self.put(body)
                self.assertError(answer, 400, code)
                self.assertEqual(answer[1]["error"]["field"], "owners")
        self.assertEqual(self.calls(), [])


class GitHubAppTest(CredentialsTest):
    def put(self, body, header=True):
        return self.request("PUT", "/api/v1/github-app", body, header=header)

    def test_replacing_app_id_and_key(self):
        self.respond(APP)
        for app_id in ("12345", 12345, " 12345 "):
            with self.subTest(app_id=app_id):
                status, obj, _ = self.put({"app_id": app_id, "private_key": KEY})
                self.assertEqual((status, obj), (200, APP))
                self.assertEqual(self.calls()[-1], ["set-github-app", "12345", "--no-prompt", "--json"])
                self.assertEqual(self.stdins()[-1], KEY)
                self.assert_nowhere(KEY, obj)
        self.assertIn("- v1 PUT put_github_app -> ok", self.server_log())

    def test_refusal_from_agentbox(self):
        message = "GitHub lehnt App-ID oder Schluessel ab (HTTP 401: Integration not found), nichts geaendert"
        self.respond("", stderr=failure("github_app_rejected", message), exitcode=1)
        answer = self.put({"app_id": "54321", "private_key": KEY})
        self.assertError(answer, 422, "github_app_rejected")
        self.assertEqual(answer[1]["error"]["detail"], message)
        self.assertIn("- v1 PUT put_github_app -> FAILED github_app_rejected", self.server_log())
        self.assert_nowhere(KEY, answer[1])

    def test_key_echoed_by_mistake_is_hidden(self):
        self.respond("", stderr=failure("invalid_private_key", "broken:\n" + KEY), exitcode=1)
        answer = self.put({"app_id": "1", "private_key": KEY.replace("\n", "\r\n")})
        self.assertError(answer, 400, "invalid_private_key")
        self.assert_nowhere(KEY, answer[1])

    def test_odd_input_is_refused_before_agentbox(self):
        cases = [({"private_key": KEY}, "invalid_app_id"),
                 ({"app_id": True, "private_key": KEY}, "invalid_app_id"),
                 ({"app_id": "abc", "private_key": KEY}, "invalid_app_id"),
                 ({"app_id": "-1", "private_key": KEY}, "invalid_app_id"),
                 ({"app_id": "--json", "private_key": KEY}, "invalid_app_id"),
                 ({"app_id": 12.5, "private_key": KEY}, "invalid_app_id"),
                 ({"app_id": ["1"], "private_key": KEY}, "invalid_app_id"),
                 # the key in the wrong field is not echoed either
                 ({"app_id": KEY, "private_key": KEY}, "invalid_app_id"),
                 ({"app_id": "1"}, "invalid_field"),
                 ({"app_id": "1", "private_key": " "}, "invalid_field"),
                 ({"app_id": "1", "private_key": [KEY]}, "invalid_field")]
        for body, code in cases:
            with self.subTest(body=body):
                answer = self.put(body)
                self.assertError(answer, 400, code)
                self.assert_nowhere(KEY, answer[1])
        self.assertEqual(self.calls(), [])

    def test_needs_session_and_header(self):
        self.assertError(self.put(None, header=False), 403, "csrf_header_missing")
        self.cookie = None
        self.assertError(self.put({"app_id": "1", "private_key": KEY}), 401, "unauthenticated")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.stdins(), [])

    def test_installations(self):
        self.respond(OVERVIEW)
        self.assertEqual(self.get("/api/v1/github-app"), (200, OVERVIEW))
        self.assertEqual(self.calls(), [["github-app", "--json"]])

    def test_github_failing_is_a_bad_gateway(self):
        self.respond("", stderr=failure("github_unavailable", "GitHub cannot be reached"), exitcode=1)
        self.assertError(self.request("GET", "/api/v1/github-app"), 502, "github_unavailable")

    def test_installations_need_a_session(self):
        self.cookie = None
        self.assertError(self.request("GET", "/api/v1/github-app"), 401, "unauthenticated")
        self.assertEqual(self.calls(), [])


class GitHubTokensTest(CredentialsTest):
    def test_tokens_of_a_vm(self):
        self.respond(TOKENS)
        self.assertEqual(self.get("/api/v1/vms/priv/github-tokens"), (200, TOKENS))
        self.assertEqual(self.calls(), [["github-tokens", "priv", "--json"]])

    def test_filters_map_to_options(self):
        self.respond(TOKENS)
        status, _ = self.get("/api/v1/vms/priv/github-tokens?since=-7d&until=2026-11-03%2010:00&lines=50&kind=alert"
                             "&json=0")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls(), [["github-tokens", "priv", "--since=-7d", "--until=2026-11-03 10:00",
                                         "--lines=50", "--json"]])

    def test_values_stay_values(self):
        self.respond(TOKENS)
        self.get("/api/v1/vms/priv/github-tokens?since=--json&lines=")
        self.assertEqual(self.calls(), [["github-tokens", "priv", "--since=--json", "--json"]])

    def test_refusals(self):
        self.assertError(self.request("GET", "/api/v1/vms/priv/github-tokens?since=-1d&since=-2d"), 400,
                         "invalid_query")
        self.assertError(self.request("GET", "/api/v1/vms/WORK/github-tokens"), 404, "not_found")
        self.assertEqual(self.calls(), [])
        self.respond("", stderr=failure("vm_not_found", "No such VM: nope", vm="nope"), exitcode=1)
        self.assertError(self.request("GET", "/api/v1/vms/nope/github-tokens"), 404, "vm_not_found")


if __name__ == "__main__":
    unittest.main()
