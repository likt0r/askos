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
TOKENS = {"vm": "priv", "repositories": [{"repository": "likt0r/askos", "tokens": 2,
                                          "last_token": "2026-11-03T10:00:00.000000Z", "refused": 0,
                                          "last_refused": None}],
          "requests": [], "truncated": False, "retention_days": 90}


class CredentialsTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()

    def stdins(self):
        try:
            with open(os.path.join(self.dir, "stdin.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def assert_nowhere(self, secret, *responses):
        """Not in a response, the server's log, its journal entries or agentbox's arguments."""
        lines = [l.strip() for l in secret.splitlines() if len(l.strip()) >= 16 and not l.startswith("-----")]
        texts = [json.dumps(r) for r in responses] + [self.server_log(), json.dumps(self.raw_calls()),
                                                      b"".join(self.journald.raw()).decode()]
        for part in [secret.strip(), *lines]:
            for text in texts:
                self.assertNotIn(part, text)


class ClaudeTokenTest(CredentialsTest):
    def test_token_goes_on_stdin_only(self):
        self.respond(stdout="Token gueltig und gespeichert (44 Zeichen). Wirkt nach: agentbox restart priv")
        status, obj, _ = self.post("/api/priv/claude-token", {"token": TOKEN})
        self.assertEqual((status, obj), (200, {"ok": True, "message": "Token gueltig und gespeichert (44 Zeichen). "
                                                                      "Wirkt nach: agentbox restart priv"}))
        self.assertEqual(self.calls(), [["set-claude-token", "priv", "--no-prompt"]])
        self.assertEqual(self.stdins(), [TOKEN])
        self.assertIn("priv claude-token -> ok: (confidential)", self.server_log())
        self.assert_nowhere(TOKEN, obj)

    def test_refusal_is_shown_and_logged_without_the_token(self):
        self.respond("", stderr="agentbox: Anthropic rejects the token: authentication_error", exitcode=1)
        status, obj, _ = self.post("/api/priv/claude-token", {"token": TOKEN})
        self.assertEqual((status, obj), (400, {"ok": False,
                                               "message": "Anthropic rejects the token: authentication_error"}))
        self.assertIn("priv claude-token -> FAILED: Anthropic rejects the token", self.server_log())
        self.assert_nowhere(TOKEN, obj)

    def test_a_token_echoed_by_mistake_is_hidden(self):
        wrapped = TOKEN[:20] + "\n" + TOKEN[20:] + "\n"
        self.respond("", stderr=f"agentbox: broken: {TOKEN}", exitcode=1)
        status, obj, _ = self.post("/api/priv/claude-token", {"token": wrapped})
        self.assertEqual((status, obj), (400, {"ok": False, "message": "broken: [hidden]"}))
        self.assertEqual(self.stdins(), [wrapped])
        self.assert_nowhere(TOKEN, obj)

    def test_anything_but_text_is_refused(self):
        for body in ({}, {"token": ""}, {"token": "  "}, {"token": None}, {"token": [TOKEN]}, {"token": 42},
                     {"token": {"t": TOKEN}}):
            with self.subTest(body=body):
                status, obj, _ = self.post("/api/priv/claude-token", body)
                self.assertEqual((status, obj), (400, {"ok": False,
                                                       "message": "invalid input: token is missing or not text"}))
        self.assertEqual(self.calls(), [])
        self.assert_nowhere(TOKEN)


class SetOwnersTest(CredentialsTest):
    def test_owners_become_arguments(self):
        self.respond(stdout="priv: Owner gesetzt: likt0r acme-corp")
        status, obj, _ = self.post("/api/priv/set-owners", {"owners": [" likt0r ", "acme-corp", ""]})
        self.assertEqual((status, obj["ok"]), (200, True))
        self.assertEqual(self.calls(), [["set-owners", "priv", "likt0r", "acme-corp"]])

    def test_odd_owners_are_refused(self):
        for body, message in (({"owners": "likt0r"}, "owners must be a list of names"),
                              ({"owners": [1]}, "owners must be a list of names"),
                              ({}, "owners must be a list of names"),
                              ({"owners": ["--json"]}, "invalid GitHub name"),
                              ({"owners": ["ok", "-x"]}, "invalid GitHub name")):
            with self.subTest(body=body):
                status, obj, _ = self.post("/api/priv/set-owners", body)
                self.assertEqual((status, obj), (400, {"ok": False, "message": f"invalid input: {message}"}))
        self.assertEqual(self.calls(), [])


class GitHubAppTest(CredentialsTest):
    def test_replacing_app_id_and_key(self):
        self.respond(APP)
        for app_id in ("12345", 12345, " 12345 "):
            with self.subTest(app_id=app_id):
                status, obj, _ = self.post("/api/github-app", {"app_id": app_id, "private_key": KEY})
                self.assertEqual((status, obj), (200, APP))
                self.assertEqual(self.calls()[-1], ["set-github-app", "12345", "--no-prompt", "--json"])
                self.assertEqual(self.stdins()[-1], KEY)
                self.assert_nowhere(KEY, obj)
        self.assertIn("- github-app -> ok: (confidential)", self.server_log())

    def test_refusal_from_agentbox(self):
        message = "GitHub lehnt App-ID oder Schluessel ab (HTTP 401: Integration not found), nichts geaendert"
        self.respond("", stderr=failure("github_app_rejected", message), exitcode=1)
        status, obj, _ = self.post("/api/github-app", {"app_id": "54321", "private_key": KEY})
        self.assertEqual((status, obj), (400, {"ok": False, "message": message}))
        self.assertIn(f"- github-app -> FAILED: {message}", self.server_log())
        self.assert_nowhere(KEY, obj)

    def test_key_echoed_by_mistake_is_hidden(self):
        self.respond("", stderr=failure("invalid_private_key", "broken:\n" + KEY), exitcode=1)
        status, obj, _ = self.post("/api/github-app", {"app_id": "1", "private_key": KEY.replace("\n", "\r\n")})
        self.assertEqual(status, 400)
        self.assert_nowhere(KEY, obj)

    def test_odd_input_is_refused_before_agentbox(self):
        cases = [({"private_key": KEY}, "app_id must be a number"),
                 ({"app_id": True, "private_key": KEY}, "app_id must be a number"),
                 ({"app_id": "abc", "private_key": KEY}, "app_id must be a number"),
                 ({"app_id": "-1", "private_key": KEY}, "app_id must be a number"),
                 ({"app_id": "--json", "private_key": KEY}, "app_id must be a number"),
                 ({"app_id": 12.5, "private_key": KEY}, "app_id must be a number"),
                 ({"app_id": ["1"], "private_key": KEY}, "app_id must be a number"),
                 # the key in the wrong field is not echoed either
                 ({"app_id": KEY, "private_key": KEY}, "app_id must be a number"),
                 ({"app_id": "1"}, "private_key is missing or not text"),
                 ({"app_id": "1", "private_key": " "}, "private_key is missing or not text"),
                 ({"app_id": "1", "private_key": [KEY]}, "private_key is missing or not text")]
        for body, message in cases:
            with self.subTest(body=body):
                status, obj, _ = self.post("/api/github-app", body)
                self.assertEqual((status, obj), (400, {"ok": False, "message": f"invalid input: {message}"}))
                self.assert_nowhere(KEY, obj)
        self.assertEqual(self.calls(), [])

    def test_needs_session_and_header(self):
        self.cookie = None
        status, _, _ = self.post("/api/github-app", {"app_id": "1", "private_key": KEY})
        self.assertEqual(status, 401)
        self.login()
        status, _, _ = self.post("/api/github-app", raw=b"", header=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.calls(), [])

    def test_installations(self):
        self.respond(OVERVIEW)
        self.assertEqual(self.get("/api/github-app"), (200, OVERVIEW))
        self.assertEqual(self.calls(), [["github-app", "--json"]])

    def test_github_failing_is_a_bad_gateway(self):
        self.respond("", stderr=failure("github_unavailable", "GitHub cannot be reached"), exitcode=1)
        self.assertEqual(self.get("/api/github-app"), (502, {"error": "GitHub cannot be reached"}))

    def test_installations_need_a_session(self):
        self.cookie = None
        self.assertEqual(self.get("/api/github-app")[0], 401)
        self.assertEqual(self.calls(), [])


class GitHubTokensTest(CredentialsTest):
    def test_tokens_of_a_vm(self):
        self.respond(TOKENS)
        self.assertEqual(self.get("/api/priv/github-tokens"), (200, TOKENS))
        self.assertEqual(self.calls(), [["github-tokens", "priv", "--json"]])

    def test_filters_map_to_options(self):
        self.respond(TOKENS)
        status, _ = self.get("/api/priv/github-tokens?since=-7d&until=2026-11-03%2010:00&lines=50&kind=alert&json=0")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls(), [["github-tokens", "priv", "--since=-7d", "--until=2026-11-03 10:00",
                                         "--lines=50", "--json"]])

    def test_values_stay_values(self):
        self.respond(TOKENS)
        self.get("/api/priv/github-tokens?since=--json&lines=")
        self.assertEqual(self.calls(), [["github-tokens", "priv", "--since=--json", "--json"]])

    def test_refusals(self):
        status, obj = self.get("/api/priv/github-tokens?since=-1d&since=-2d")
        self.assertEqual((status, obj), (400, {"error": "give since only once"}))
        self.assertEqual(self.get("/api/WORK/github-tokens")[0], 404)
        self.assertEqual(self.calls(), [])
        self.respond("", stderr=failure("vm_not_found", "No such VM: nope", vm="nope"), exitcode=1)
        self.assertEqual(self.get("/api/nope/github-tokens"), (400, {"error": "No such VM: nope"}))


if __name__ == "__main__":
    unittest.main()
