"""Control Room backend (`web/agentbox-web`): the logs of a VM, Activity (the actor on every
agentbox call, sign-ins, the timeline), against a fake `agentbox` (see fake_agentbox.py).
Its harness, ControlRoomTest, also serves the other test_control_room_* modules.

The real server runs on 127.0.0.1 with a throwaway certificate and password and no UI
build; the tests only look at HTTP responses of the API (/api/v1, docs/control-room-api.md),
the argv agentbox received, the server's stderr (which goes to the Host journal in
production) and the entries it sends to a stand-in for journald's socket
(fake_journald.py), never to the Host's journal.

Note: without the X-Agentbox header the server answers 403 without reading the request
body and closes the connection; with a non-empty body the client may then see a TCP reset
instead of the 403, so those tests send no body.

Run from the repo root: python3 -m unittest discover -s tests
"""
import http.client
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_journald import Journald  # noqa: E402
import served_port  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "web", "agentbox-web")
FAKE = os.path.join(ROOT, "tests", "fake_agentbox.py")
PASSWORD = "correct horse battery staple"
CODE = "orca://pair?code=SECRET"

conf = None  # TemporaryDirectory with cert.pem, key.pem, password


def setUpModule():
    global conf
    conf = tempfile.TemporaryDirectory()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-days", "1", "-subj", "/CN=t",
                    "-keyout", os.path.join(conf.name, "key.pem"), "-out", os.path.join(conf.name, "cert.pem")],
                   check=True, capture_output=True)
    r = subprocess.run([sys.executable, "-I", SERVER, "--hash-password"], input=PASSWORD + "\n",
                       capture_output=True, text=True, check=True)
    with open(os.path.join(conf.name, "password"), "w") as f:
        f.write(r.stdout)


def tearDownModule():
    conf.cleanup()


def canned(*messages, vm="priv"):
    entries = [{"time": "2025-10-29T10:00:00.123456Z", "unit": "agentbox-orca.service", "identifier": "orca",
                "priority": 6, "message": m, "boot": "b1", "cursor": f"s=1a;i={i};b=b1"}
               for i, m in enumerate(messages)]
    return {"vm": vm, "entries": entries, "cursor": entries[-1]["cursor"] if entries else None,
            "truncated": False}


def failure(error, message, **params):
    """What agentbox writes on stderr when a call with --json fails (agentbox/cli.py)."""
    return json.dumps({"error": error, "message": message, "params": params})


class ControlRoomTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.stderr_path = os.path.join(self.dir, "server.err")
        self.journald = Journald(os.path.join(self.dir, "journal.socket"))
        self.addCleanup(self.journald.close)
        env = dict(os.environ,
                   AGENTBOX_WEB_CONF=conf.name, AGENTBOX_WEB_UI=os.path.join(self.dir, "ui"),
                   AGENTBOX_WEB_BIND="127.0.0.1", AGENTBOX_WEB_PORT="0",
                   AGENTBOX_WEB_COMMAND=f"{sys.executable} -I {FAKE}",
                   AGENTBOX_WEB_JOURNAL=self.journald.path,
                   FAKE_AGENTBOX_DIR=self.dir)
        with open(self.stderr_path, "w") as err:
            self.server = subprocess.Popen([sys.executable, "-I", SERVER], env=env,
                                           stdout=subprocess.DEVNULL, stderr=err)
        self.addCleanup(self.stop_server)
        self.ssl = ssl._create_unverified_context()
        self.wait_for_server()
        self.cookie = None
        self.user_agent = None

    def tearDown(self):
        self.stop_server()

    def stop_server(self):
        if self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()

    def wait_for_server(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.port = served_port.port(self.server_log())
            if self.port:
                return
            if self.server.poll() is not None:
                self.fail("server exited early: " + self.server_log())
            time.sleep(0.05)
        self.fail("server did not start: " + self.server_log())

    def server_log(self):
        with open(self.stderr_path) as f:
            return f.read()

    def respond(self, stdout=None, stderr="", exitcode=0):
        r = {"stderr": stderr, "exitcode": exitcode}
        if stdout is not None:
            r["stdout"] = stdout if isinstance(stdout, str) else json.dumps(stdout)
        with open(os.path.join(self.dir, "response.json"), "w") as f:
            json.dump(r, f)

    def raw_calls(self):
        """argv of every agentbox call, as received."""
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def calls(self):
        """argv of every agentbox call without the --actor after the command (see ActorTest)."""
        return [[c[0], *c[2:]] if len(c) > 1 and c[1].startswith("--actor=") else c for c in self.raw_calls()]

    def stdins(self):
        """What the calls that read a secret got on stdin."""
        try:
            with open(os.path.join(self.dir, "stdin.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def request(self, method, path, body=None, header=True):
        """(status, JSON answer, response) of one request; body None sends none."""
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ssl, timeout=60)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if header:
            headers["X-Agentbox"] = "1"
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        data = None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, json.loads(payload) if payload else None, resp
        finally:
            conn.close()

    def get(self, path):
        return self.request("GET", path)[:2]

    def login(self):
        status, obj, resp = self.request("POST", "/api/v1/session", {"password": PASSWORD})
        self.assertEqual(status, 200, obj)
        self.cookie = resp.getheader("Set-Cookie").split(";")[0]

    def assertError(self, answer, status, code):
        """The v1 error format: {"error": {"code", "message", ...}}."""
        self.assertEqual(answer[0], status, answer[1])
        self.assertEqual(answer[1]["error"]["code"], code)


class LogsTest(ControlRoomTest):
    """#8: GET /api/v1/vms/<vm>/logs maps its query onto `agentbox logs`, always with --json;
    the answer is agentbox's JSON, and nothing of it reaches the Host journal."""

    def setUp(self):
        super().setUp()
        self.login()

    def logs(self, query="", vm="priv"):
        return self.get(f"/api/v1/vms/{vm}/logs" + (f"?{query}" if query else ""))

    def logs_argv(self, **params):
        """argv agentbox received for one request with these query parameters."""
        self.respond(canned())
        status, obj = self.logs(urlencode(params))
        self.assertEqual(status, 200, obj)
        return self.calls()[-1]

    def test_without_filters_asks_for_json_logs_of_the_vm(self):
        self.assertEqual(self.logs_argv(), ["logs", "priv", "--json"])

    def test_each_filter_maps_to_its_option(self):
        cases = {
            "unit": ("agentbox-orca.service", "--unit=agentbox-orca.service"),
            "priority": ("warning", "--priority=warning"),
            "since": ("-1h", "--since=-1h"),
            "until": ("2025-10-29 10:00", "--until=2025-10-29 10:00"),
            "boot": ("all", "--boot=all"),
            "grep": ("Orca bereit", "--grep=Orca bereit"),
            "lines": ("50", "--lines=50"),
            "after_cursor": ("s=1a;i=7;b=b1", "--after-cursor=s=1a;i=7;b=b1"),
        }
        for param, (value, option) in cases.items():
            with self.subTest(param=param):
                self.assertEqual(self.logs_argv(**{param: value}), ["logs", "priv", option, "--json"])

    def test_empty_and_blank_filters_are_dropped(self):
        self.assertEqual(self.logs_argv(unit="", grep="   ", since=""), ["logs", "priv", "--json"])

    def test_unknown_parameters_are_not_forwarded(self):
        argv = self.logs_argv(**{"search": "x", "output": "verbose", "--output": "cat", "after-cursor": "c",
                                 "json": "0", "ctx": "work", "D": "/var/log"})
        self.assertEqual(argv, ["logs", "priv", "--json"])

    def test_value_starting_with_dash_stays_a_value(self):
        argv = self.logs_argv(grep="--output=cat", unit="-x", since="-1d")
        self.assertCountEqual(argv[2:-1], ["--grep=--output=cat", "--unit=-x", "--since=-1d"])
        self.assertNotIn("--output=cat", argv)
        self.assertNotIn("-x", argv)

    def test_control_characters_in_values_stay_inside_one_argument(self):
        argv = self.logs_argv(since="1\n--output=cat", priority="3 --all", after_cursor="a --b")
        self.assertIn("--since=1\n--output=cat", argv)
        self.assertIn("--priority=3 --all", argv)
        self.assertIn("--after-cursor=a --b", argv)

    def test_a_repeated_filter_is_refused(self):
        self.assertError(self.request("GET", "/api/v1/vms/priv/logs?unit=a&unit=b"), 400, "invalid_query")
        self.assertEqual(self.calls(), [])

    def test_any_vm_name_goes_to_agentbox_which_knows_the_registry(self):
        self.respond(canned(vm="research-lab"))
        self.assertEqual(self.logs(vm="research-lab")[0], 200)
        self.assertEqual(self.calls(), [["logs", "research-lab", "--json"]])
        self.respond(stdout="", stderr=failure("vm_not_found", "No such VM: other", vm="other"), exitcode=1)
        self.assertError(self.request("GET", "/api/v1/vms/other/logs"), 404, "vm_not_found")

    def test_response_body_is_the_json_from_agentbox(self):
        answer = canned("Orca bereit", "zweite Zeile")
        answer["truncated"] = True
        self.respond(answer)
        self.assertEqual(self.logs("lines=2"), (200, answer))

    def test_log_content_and_pairing_code_are_not_written_to_the_journal(self):
        self.respond(canned("Orca bereit", f"Pairing: {CODE}", "geheime Logzeile"),
                     stderr="agentbox: Ausgabe gekuerzt, Filter enger fassen")
        status, obj = self.logs("grep=Pairing")
        self.assertEqual(status, 200)
        self.assertIn(CODE, json.dumps(obj))
        log = self.server_log()
        for secret in (CODE, "SECRET", "Orca bereit", "geheime Logzeile", "s=1a;i=", "entries"):
            self.assertNotIn(secret, log)

    def test_invalid_json_from_agentbox_is_an_error_without_its_content(self):
        self.respond(f"kein JSON {CODE}")
        answer = self.request("GET", "/api/v1/vms/priv/logs")
        self.assertError(answer, 502, "bad_agentbox_answer")
        self.assertNotIn("SECRET", json.dumps(answer[1]))
        self.assertNotIn("SECRET", self.server_log())

    def test_failure_text_of_logs_is_not_written_to_the_journal(self):
        self.respond("", stderr=failure("vm_journal_failed", "priv: journalctl fails in the VM: secret text from "
                                        "the VM", vm="priv"), exitcode=6)
        answer = self.request("GET", "/api/v1/vms/priv/logs")
        self.assertError(answer, 502, "vm_journal_failed")
        self.assertIn("secret text", answer[1]["error"]["detail"])
        log = self.server_log()
        self.assertIn("priv v1 GET get_logs -> FAILED vm_journal_failed", log)
        self.assertNotIn("secret text", log)


FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
SAFARI_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "Version/18.0 Mobile/15E148 Safari/604.1")
EDGE_WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
CHROME_ANDROID = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Mobile Safari/537.36")


class ActorTest(ControlRoomTest):
    """#15: every agentbox call names the browser's device and address with --actor, right after
    the command (which comes first for the sudoers rule, #50)."""

    def setUp(self):
        super().setUp()
        self.login()

    def test_actions_pass_device_and_address_after_the_command(self):
        self.user_agent = FIREFOX_LINUX
        self.respond({"vm": "priv", "state": "paused"})
        self.assertEqual(self.request("POST", "/api/v1/vms/priv/pause", {})[:2],
                         (200, {"vm": "priv", "state": "paused"}))
        self.assertEqual(self.raw_calls(), [["pause", "--actor=Firefox/Linux (127.0.0.1)", "priv", "--json"]])

    def test_reading_calls_pass_it_too(self):
        self.user_agent = EDGE_WINDOWS
        self.respond({"vms": [], "problems": []})
        self.assertEqual(self.get("/api/v1/vms"), (200, {"vms": [], "problems": []}))
        self.respond(canned())
        self.get("/api/v1/vms/priv/logs")
        self.assertEqual([c[1] for c in self.raw_calls()], ["--actor=Edge/Windows (127.0.0.1)"] * 2)

    def test_device_from_the_user_agent(self):
        for agent, device in ((FIREFOX_LINUX, "Firefox/Linux"), (SAFARI_IPHONE, "Safari/iPhone"),
                              (EDGE_WINDOWS, "Edge/Windows"), (CHROME_ANDROID, "Chrome/Android"),
                              ("curl/8.11.1", "curl"), (None, "unknown device"),
                              ("--json\x1b[31m; rm -rf /", "unknown device")):
            with self.subTest(agent=agent):
                self.user_agent = agent
                self.respond({"vm": "priv"})
                self.request("DELETE", "/api/v1/vms/priv/open-web")
                self.assertEqual(self.raw_calls()[-1], ["close-web", f"--actor={device} (127.0.0.1)", "priv", "--json"])

    def test_secret_actions_carry_the_actor_but_not_the_secret_in_argv(self):
        self.respond({"vm": "priv", "stored": True})
        self.request("PUT", "/api/v1/vms/priv/claude-token", {"token": "sk-ant-oat01-geheim"})
        argv = self.raw_calls()[-1]
        self.assertEqual(argv, ["set-claude-token", "--actor=unknown device (127.0.0.1)", "priv", "--no-prompt",
                                "--json"])


class SignInActivityTest(ControlRoomTest):
    """#15: sign-ins are Activity entries in the journal, with address and device, never the password."""

    def entries(self, action=None):
        return [e for e in self.journald.entries() if action is None or e.get("AGENTBOX_ACTION") == action]

    def assert_no_password(self, *passwords):
        raw = b"".join(self.journald.raw())
        for pw in passwords:
            self.assertNotIn(pw.encode(), raw)
            self.assertNotIn(pw, self.server_log())

    def sign_in(self, password):
        return self.request("POST", "/api/v1/session", {"password": password})

    def test_successful_sign_in(self):
        self.user_agent = FIREFOX_LINUX
        self.login()
        [e] = self.entries()
        self.assertEqual({k: v for k, v in e.items() if k != "MESSAGE"}, {
            "PRIORITY": "5", "SYSLOG_IDENTIFIER": "agentbox-activity", "AGENTBOX_KIND": "signin",
            "AGENTBOX_ACTION": "signin", "AGENTBOX_CODE": "signin", "AGENTBOX_OUTCOME": "ok",
            "AGENTBOX_ACTOR": "Firefox/Linux (127.0.0.1)", "AGENTBOX_DESCRIPTION": "Signed in",
            "AGENTBOX_ADDRESS": "127.0.0.1", "AGENTBOX_DEVICE": "Firefox/Linux"})
        self.assert_no_password(PASSWORD)

    def test_failed_sign_in(self):
        self.user_agent = SAFARI_IPHONE
        self.assertError(self.sign_in("wrong horse battery staple"), 401, "wrong_password")
        [e] = self.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_CODE"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_ACTOR"],
                          e["PRIORITY"], e["AGENTBOX_DESCRIPTION"]),
                         ("signin.failed", "signin.failed", "failed", "Safari/iPhone (127.0.0.1)", "4",
                          "Sign-in with a wrong password"))
        self.assertFalse([k for k in e if k.startswith(("AGENTBOX_PARAM_", "AGENTBOX_ERROR"))])
        self.assert_no_password("wrong horse battery staple")

    def test_locked_sign_in_after_too_many_failures(self):
        for i in range(5):
            self.sign_in(f"guess-{i}")
        self.assertError(self.sign_in(PASSWORD), 429, "too_many_attempts")
        self.assertEqual([e["AGENTBOX_ACTION"] for e in self.entries()], ["signin.failed"] * 5 + ["signin.locked"])
        self.assertEqual((self.entries()[-1]["AGENTBOX_CODE"], self.entries()[-1]["AGENTBOX_DESCRIPTION"]),
                         ("signin.locked", "Sign-in refused: too many failed attempts"))
        self.assert_no_password(PASSWORD, *(f"guess-{i}" for i in range(5)))

    def test_sign_out(self):
        self.login()
        self.request("DELETE", "/api/v1/session")
        self.assertEqual([(e["AGENTBOX_ACTION"], e["AGENTBOX_CODE"], e["AGENTBOX_DESCRIPTION"])
                          for e in self.entries()], [("signin", "signin", "Signed in"),
                                                     ("signout", "signout", "Signed out")])
        # without a session there is nobody to sign out
        self.request("DELETE", "/api/v1/session")
        self.assertEqual(len(self.entries()), 2)

    def test_actions_are_recorded_by_agentbox_not_here(self):
        self.login()
        self.respond({"vm": "priv"})
        self.request("POST", "/api/v1/vms/priv/pause", {})
        self.assertEqual([e["AGENTBOX_ACTION"] for e in self.entries()], ["signin"])


class ActivityEndpointTest(ControlRoomTest):
    """#15: GET /api/v1/activity maps its query onto `agentbox activity` and returns its JSON."""

    TIMELINE = {"entries": [{"time": "2026-11-03T10:00:00.000000Z", "vm": "priv", "actor": "timer", "kind": "reset",
                             "action": "reset.automatic", "outcome": "ok",
                             "description": "Automatic Reset (idle)", "code": "reset.automatic",
                             "params": {"reason": "idle"}, "reason": "idle"}],
                "truncated": False, "retention_days": 90}

    def test_requires_a_session(self):
        self.assertError(self.request("GET", "/api/v1/activity"), 401, "unauthenticated")
        self.assertEqual(self.calls(), [])

    def test_without_filters(self):
        self.login()
        self.respond(self.TIMELINE)
        self.assertEqual(self.get("/api/v1/activity"), (200, self.TIMELINE))
        self.assertEqual(self.calls()[-1], ["activity", "--json"])

    def test_each_filter_maps_to_its_option(self):
        self.login()
        self.respond(self.TIMELINE)
        status, _ = self.get("/api/v1/activity?kind=allowlist,alert&vm=priv&since=-7d&until=2026-11-03%2010:00"
                             "&search=%20Firefox%20Linux&lines=50&json=0&actor=me")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls()[-1], ["activity", "--kind=allowlist,alert", "--vm=priv", "--since=-7d",
                                            "--until=2026-11-03 10:00", "--search= Firefox Linux", "--lines=50",
                                            "--json"])

    def test_values_stay_values(self):
        self.login()
        self.respond(self.TIMELINE)
        self.get("/api/v1/activity?kind=--json&vm=&search=--actor=x")
        self.assertEqual(self.calls()[-1], ["activity", "--kind=--json", "--search=--actor=x", "--json"])
        self.assertTrue(self.raw_calls()[-1][1].startswith("--actor=unknown device"))

    def test_repeated_parameter_is_refused(self):
        self.login()
        self.assertError(self.request("GET", "/api/v1/activity?kind=alert&kind=signin"), 400, "invalid_query")
        self.assertEqual(self.calls(), [])

    def test_agentbox_refusing_a_filter(self):
        self.login()
        self.respond("", stderr=failure("invalid_argument", "invalid value for --kind: x", option="--kind"),
                     exitcode=2)
        answer = self.request("GET", "/api/v1/activity?kind=x")
        self.assertError(answer, 400, "invalid_argument")
        self.assertEqual(answer[1]["error"]["detail"], "invalid value for --kind: x")


if __name__ == "__main__":
    unittest.main()
