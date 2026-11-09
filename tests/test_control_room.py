"""Control Room backend (`web/agentbox-web`): the `logs` action, Activity (the actor on
every agentbox call, sign-ins, the timeline), against a fake `agentbox` (see
fake_agentbox.py).

The real server runs on 127.0.0.1 with a throwaway certificate and password; the tests
only look at HTTP responses, the argv agentbox received, the server's stderr (which
goes to the Host journal in production) and the entries it sends to a stand-in for
journald's socket (fake_journald.py), never to the Host's journal.

Note: without the X-Agentbox header the server answers 403 without reading the request
body and closes the connection; with a non-empty body the client may then see a TCP reset
instead of the 403, so those tests send an empty body.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_journald import Journald  # noqa: E402
import served_port  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "web", "agentbox-web")
STATIC = os.path.join(ROOT, "web", "static")
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
                   AGENTBOX_WEB_CONF=conf.name, AGENTBOX_WEB_STATIC=STATIC,
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
        """argv of every agentbox call without the leading --actor (see ActorTest)."""
        return [c[1:] if c and c[0].startswith("--actor=") else c for c in self.raw_calls()]

    def post(self, path, body=None, header=True, raw=None):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ssl, timeout=60)
        headers = {"Content-Type": "application/json"}
        if header:
            headers["X-Agentbox"] = "1"
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        try:
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, json.loads(payload), resp.getheader("Set-Cookie")
        finally:
            conn.close()

    def get(self, path):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ssl, timeout=60)
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def login(self):
        status, _, cookie = self.post("/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(";")[0]

    def logs(self, body=None, ctx="priv"):
        status, obj, _ = self.post(f"/api/{ctx}/logs", body or {})
        return status, obj

    def logs_argv(self, body):
        """argv agentbox received for one `logs` request with this body."""
        self.respond(canned())
        status, _ = self.logs(body)
        self.assertEqual(status, 200)
        return self.calls()[-1]


class LogsParametersTest(ControlRoomTest):
    """#8: every filter maps onto its `agentbox logs` option, always with --json."""

    def setUp(self):
        super().setUp()
        self.login()

    def test_without_filters_asks_for_json_logs_of_the_vm(self):
        self.assertEqual(self.logs_argv({}), ["logs", "priv", "--json"])

    def test_context_is_passed_through(self):
        self.respond(canned(vm="work"))
        status, _ = self.logs({}, ctx="work")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls()[-1], ["logs", "work", "--json"])

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
                self.assertEqual(self.logs_argv({param: value}), ["logs", "priv", option, "--json"])

    def test_all_filters_together(self):
        argv = self.logs_argv({"unit": "sshd.service", "priority": "3", "since": "today", "until": "now",
                               "boot": "current", "grep": "fail", "lines": "10", "after_cursor": "c1"})
        self.assertEqual(argv[:2], ["logs", "priv"])
        self.assertEqual(argv[-1], "--json")
        self.assertCountEqual(argv[2:-1], ["--unit=sshd.service", "--priority=3", "--since=today",
                                           "--until=now", "--boot=current", "--grep=fail", "--lines=10",
                                           "--after-cursor=c1"])

    def test_numeric_lines_is_forwarded_as_value(self):
        self.assertEqual(self.logs_argv({"lines": 50}), ["logs", "priv", "--lines=50", "--json"])

    def test_empty_and_blank_filters_are_dropped(self):
        self.assertEqual(self.logs_argv({"unit": "", "grep": "   ", "since": ""}), ["logs", "priv", "--json"])

    def test_unknown_parameters_are_not_forwarded(self):
        argv = self.logs_argv({"search": "x", "output": "verbose", "--output": "cat", "after-cursor": "c",
                               "json": False, "ctx": "work", "D": "/var/log"})
        self.assertEqual(argv, ["logs", "priv", "--json"])

    def test_value_starting_with_dash_stays_a_value(self):
        argv = self.logs_argv({"grep": "--output=cat", "unit": "-x", "since": "-1d"})
        self.assertCountEqual(argv[2:-1], ["--grep=--output=cat", "--unit=-x", "--since=-1d"])
        self.assertNotIn("--output=cat", argv)
        self.assertNotIn("-x", argv)

    def test_null_means_no_filter(self):
        argv = self.logs_argv({"unit": None, "grep": None, "lines": None})
        self.assertEqual(argv, ["logs", "priv", "--json"])

    def test_search_text_keeps_its_spaces(self):
        argv = self.logs_argv({"grep": " error"})
        self.assertIn("--grep= error", argv)

    def test_values_are_forwarded_stripped(self):
        argv = self.logs_argv({"unit": "  agentbox-orca.service  "})
        self.assertIn("--unit=agentbox-orca.service", argv)

    def test_odd_values_are_rejected_not_converted(self):
        for odd in ({"unit": ["--output=cat", "x"]}, {"grep": {"--all": True}}, {"lines": 5.5},
                    {"boot": True}, {"boot": False}, {"lines": 0.0}):
            with self.subTest(body=odd):
                before = len(self.calls())
                status, obj = self.logs(odd)
                self.assertEqual(status, 400)
                self.assertFalse(obj["ok"])
                self.assertEqual(len(self.calls()), before)

    def test_control_characters_in_values_stay_inside_one_argument(self):
        argv = self.logs_argv({"since": "1\n--output=cat", "priority": "3 --all", "after_cursor": "a --b"})
        self.assertIn("--since=1\n--output=cat", argv)
        self.assertIn("--priority=3 --all", argv)
        self.assertIn("--after-cursor=a --b", argv)


class LogsResponseTest(ControlRoomTest):
    """#8: the response is agentbox's JSON contract; nothing of it reaches the Host journal."""

    def setUp(self):
        super().setUp()
        self.login()

    def test_response_body_is_the_json_from_agentbox(self):
        answer = canned("Orca bereit", "zweite Zeile")
        answer["truncated"] = True
        self.respond(answer)
        status, obj = self.logs({"lines": "2"})
        self.assertEqual(status, 200)
        self.assertEqual(obj, answer)

    def test_empty_result_is_passed_through(self):
        self.respond(canned())
        status, obj = self.logs({"grep": "nichts"})
        self.assertEqual(status, 200)
        self.assertEqual(obj, {"vm": "priv", "entries": [], "cursor": None, "truncated": False})

    def test_log_content_and_pairing_code_are_not_written_to_the_journal(self):
        self.respond(canned("Orca bereit", f"Pairing: {CODE}", "geheime Logzeile"),
                     stderr="agentbox: Ausgabe gekuerzt, Filter enger fassen")
        status, obj = self.logs({"grep": "Pairing"})
        self.assertEqual(status, 200)
        self.assertIn(CODE, json.dumps(obj))
        log = self.server_log()
        self.assertIn("priv logs -> ok", log)
        for secret in (CODE, "SECRET", "Orca bereit", "geheime Logzeile", "s=1a;i=", "entries"):
            self.assertNotIn(secret, log)

    def test_invalid_json_from_agentbox_is_an_error_without_its_content(self):
        self.respond(f"kein JSON {CODE}")
        status, obj = self.logs({})
        self.assertEqual(status, 400)
        self.assertFalse(obj["ok"])
        self.assertNotIn("SECRET", json.dumps(obj))
        self.assertNotIn("SECRET", self.server_log())

    def test_failure_text_of_logs_is_not_written_to_the_journal(self):
        self.respond("", stderr=failure("vm_journal_failed", "priv: journalctl fails in the VM: secret text from "
                                        "the VM", vm="priv"), exitcode=6)
        status, obj = self.logs({})
        self.assertEqual(status, 400)
        self.assertIn("secret text", obj["message"])
        log = self.server_log()
        self.assertIn("priv logs -> FAILED", log)
        self.assertNotIn("secret text", log)

    def test_agentbox_failure_is_reported(self):
        self.respond("", stderr=failure("vm_not_running", "priv: the VM is not running (agentbox start priv)",
                                        vm="priv"), exitcode=3)
        status, obj = self.logs({})
        self.assertEqual(status, 400)
        # the old UI gets the message of agentbox's JSON failure
        self.assertEqual(obj, {"ok": False, "message": "priv: the VM is not running (agentbox start priv)"})
        self.assertIn("priv logs -> FAILED", self.server_log())


class LogsAccessTest(ControlRoomTest):
    """#8: logs is reachable only with a session, the X-Agentbox header and a known VM."""

    def test_requires_session(self):
        status, obj, _ = self.post("/api/priv/logs", {})
        self.assertEqual(status, 401)
        self.assertEqual(self.calls(), [])

    def test_bogus_session_cookie_is_rejected(self):
        self.cookie = "agentbox=bogus"
        status, _, _ = self.post("/api/priv/logs", {})
        self.assertEqual(status, 401)
        self.assertEqual(self.calls(), [])

    def test_requires_x_agentbox_header(self):
        self.login()
        # empty body: the server answers 403 before reading the body (see the module notes)
        status, _, _ = self.post("/api/priv/logs", raw=b"", header=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.calls(), [])

    def test_requires_header_even_without_session(self):
        status, _, _ = self.post("/api/priv/logs", raw=b"", header=False)
        self.assertEqual(status, 403)

    def test_invalid_vm_names_are_rejected(self):
        self.login()
        for ctx in ("--all", "work;priv", "WORK", "x", "a" * 17, "..", "work%2Fpriv"):
            with self.subTest(ctx=ctx):
                status, _ = self.logs({}, ctx=ctx)
                self.assertEqual(status, 404)
        self.assertEqual(self.calls(), [])

    def test_any_vm_name_goes_to_agentbox_which_knows_the_registry(self):
        self.login()
        self.respond(canned(vm="research-lab"))
        status, _ = self.logs({}, ctx="research-lab")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls(), [["logs", "research-lab", "--json"]])
        self.respond(stdout="", stderr="agentbox: No such VM: other\n", exitcode=1)
        status, obj = self.logs({}, ctx="other")
        self.assertEqual((status, obj), (400, {"ok": False, "message": "No such VM: other"}))

    def test_non_object_body_is_rejected(self):
        self.login()
        status, _, _ = self.post("/api/priv/logs", raw=b'["--unit=x"]')
        self.assertEqual(status, 400)
        self.assertEqual(self.calls(), [])


FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
SAFARI_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "Version/18.0 Mobile/15E148 Safari/604.1")
EDGE_WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
CHROME_ANDROID = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Mobile Safari/537.36")


class ActorTest(ControlRoomTest):
    """#15: every agentbox call names the browser's device and address with --actor."""

    def setUp(self):
        super().setUp()
        self.login()

    def test_actions_pass_device_and_address_before_the_command(self):
        self.user_agent = FIREFOX_LINUX
        self.respond(stdout="priv eingefroren")
        status, obj, _ = self.post("/api/priv/pause", {})
        self.assertEqual((status, obj), (200, {"ok": True, "message": "priv eingefroren"}))
        self.assertEqual(self.raw_calls(), [["--actor=Firefox/Linux (127.0.0.1)", "pause", "priv"]])

    def test_reading_calls_pass_it_too(self):
        self.user_agent = EDGE_WINDOWS
        self.respond({})
        self.assertEqual(self.get("/api/status"), (200, {}))
        self.respond(canned())
        self.logs({})
        self.assertEqual([c[0] for c in self.raw_calls()], ["--actor=Edge/Windows (127.0.0.1)"] * 2)

    def test_device_from_the_user_agent(self):
        for agent, device in ((FIREFOX_LINUX, "Firefox/Linux"), (SAFARI_IPHONE, "Safari/iPhone"),
                              (EDGE_WINDOWS, "Edge/Windows"), (CHROME_ANDROID, "Chrome/Android"),
                              ("curl/8.11.1", "curl"), (None, "unknown device"),
                              ("--json\x1b[31m; rm -rf /", "unknown device")):
            with self.subTest(agent=agent):
                self.user_agent = agent
                self.respond(stdout="ok")
                self.post("/api/priv/close-web", {})
                self.assertEqual(self.raw_calls()[-1], [f"--actor={device} (127.0.0.1)", "close-web", "priv"])

    def test_secret_actions_carry_the_actor_but_not_the_secret_in_argv(self):
        self.respond(stdout="Token gueltig und gespeichert")
        self.post("/api/priv/claude-token", {"token": "sk-ant-oat01-geheim"})
        argv = self.raw_calls()[-1]
        self.assertEqual(argv, ["--actor=unknown device (127.0.0.1)", "set-claude-token", "priv", "--no-prompt"])


class SignInActivityTest(ControlRoomTest):
    """#15: sign-ins are Activity entries in the journal, with address and device, never the password."""

    def entries(self, action=None):
        return [e for e in self.journald.entries() if action is None or e.get("AGENTBOX_ACTION") == action]

    def assert_no_password(self, *passwords):
        raw = b"".join(self.journald.raw())
        for pw in passwords:
            self.assertNotIn(pw.encode(), raw)
            self.assertNotIn(pw, self.server_log())

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
        status, _, _ = self.post("/api/login", {"password": "wrong horse battery staple"})
        self.assertEqual(status, 401)
        [e] = self.entries()
        self.assertEqual((e["AGENTBOX_ACTION"], e["AGENTBOX_CODE"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_ACTOR"],
                          e["PRIORITY"], e["AGENTBOX_DESCRIPTION"]),
                         ("signin.failed", "signin.failed", "failed", "Safari/iPhone (127.0.0.1)", "4",
                          "Sign-in with a wrong password"))
        self.assertFalse([k for k in e if k.startswith(("AGENTBOX_PARAM_", "AGENTBOX_ERROR"))])
        self.assert_no_password("wrong horse battery staple")

    def test_locked_sign_in_after_too_many_failures(self):
        for _ in range(5):
            self.post("/api/login", {"password": f"guess-{_}"})
        status, _, _ = self.post("/api/login", {"password": PASSWORD})
        self.assertEqual(status, 429)
        self.assertEqual([e["AGENTBOX_ACTION"] for e in self.entries()], ["signin.failed"] * 5 + ["signin.locked"])
        self.assertEqual((self.entries()[-1]["AGENTBOX_CODE"], self.entries()[-1]["AGENTBOX_DESCRIPTION"]),
                         ("signin.locked", "Sign-in refused: too many failed attempts"))
        self.assert_no_password(PASSWORD, *(f"guess-{i}" for i in range(5)))

    def test_sign_out(self):
        self.login()
        self.post("/api/logout", {})
        self.assertEqual([(e["AGENTBOX_ACTION"], e["AGENTBOX_CODE"], e["AGENTBOX_DESCRIPTION"])
                          for e in self.entries()], [("signin", "signin", "Signed in"),
                                                     ("signout", "signout", "Signed out")])
        # without a session there is nobody to sign out
        self.post("/api/logout", {})
        self.assertEqual(len(self.entries()), 2)

    def test_actions_are_recorded_by_agentbox_not_here(self):
        self.login()
        self.respond(stdout="ok")
        self.post("/api/priv/pause", {})
        self.assertEqual([e["AGENTBOX_ACTION"] for e in self.entries()], ["signin"])


class ActivityEndpointTest(ControlRoomTest):
    """#15: GET /api/activity maps its query onto `agentbox activity` and returns its JSON."""

    TIMELINE = {"entries": [{"time": "2026-11-03T10:00:00.000000Z", "vm": "priv", "actor": "timer", "kind": "reset",
                             "action": "reset.automatic", "outcome": "ok",
                             "description": "Automatic Reset (idle)", "code": "reset.automatic",
                             "params": {"reason": "idle"}, "reason": "idle"}],
                "truncated": False, "retention_days": 90}

    def test_requires_a_session(self):
        status, _ = self.get("/api/activity")
        self.assertEqual(status, 401)
        self.assertEqual(self.calls(), [])

    def test_without_filters(self):
        self.login()
        self.respond(self.TIMELINE)
        self.assertEqual(self.get("/api/activity"), (200, self.TIMELINE))
        self.assertEqual(self.calls()[-1], ["activity", "--json"])

    def test_each_filter_maps_to_its_option(self):
        self.login()
        self.respond(self.TIMELINE)
        status, _ = self.get("/api/activity?kind=allowlist,alert&vm=priv&since=-7d&until=2026-11-03%2010:00"
                             "&search=%20Firefox%20Linux&lines=50&json=0&actor=me")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls()[-1], ["activity", "--kind=allowlist,alert", "--vm=priv", "--since=-7d",
                                            "--until=2026-11-03 10:00", "--search= Firefox Linux", "--lines=50",
                                            "--json"])

    def test_values_stay_values(self):
        self.login()
        self.respond(self.TIMELINE)
        self.get("/api/activity?kind=--json&vm=&search=--actor=x")
        self.assertEqual(self.calls()[-1], ["activity", "--kind=--json", "--search=--actor=x", "--json"])
        self.assertTrue(self.raw_calls()[-1][0].startswith("--actor=unknown device"))

    def test_repeated_parameter_is_refused(self):
        self.login()
        status, obj = self.get("/api/activity?kind=alert&kind=signin")
        self.assertEqual(status, 400)
        self.assertIn("kind", obj["error"])
        self.assertEqual(self.calls(), [])

    def test_agentbox_refusing_a_filter(self):
        self.login()
        self.respond("", stderr=failure("invalid_argument", "invalid value for --kind: x", option="--kind"),
                     exitcode=2)
        self.assertEqual(self.get("/api/activity?kind=x"), (400, {"error": "invalid value for --kind: x"}))


if __name__ == "__main__":
    unittest.main()
