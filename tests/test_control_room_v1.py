"""Control Room backend (`web/agentbox-web`): the versioned API /api/v1, the live events
from `agentbox watch`, notifications, the version event and serving the new UI (#12,
#13, #21), against a fake `agentbox` (fake_agentbox.py, which also plays `agentbox
watch`), fake kernel files and a placeholder UI build (tests/ui_build, not a real Nuxt
build).

The real server runs on 127.0.0.1 with a throwaway certificate and password. The tests
look at HTTP responses, the events on the stream, the argv agentbox received, what it
got on stdin and the server's stderr (the Host journal in production).

Run from the repo root: python3 -m unittest discover -s tests
"""
import ast
import base64
import hashlib
import http.client
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_journald import Journald  # noqa: E402
import served_port  # noqa: E402
from test_host_readings import DEVICE_LINKS, TITAN, Stream  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "web", "agentbox-web")
STATIC = os.path.join(ROOT, "web", "static")
UI_BUILD = os.path.join(ROOT, "tests", "ui_build")
FAKE = os.path.join(ROOT, "tests", "fake_agentbox.py")
PASSWORD = "correct horse battery staple"
CLAUDE = "sk-ant-oat01-" + "c" * 60
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA" + "k" * 64 + "\n-----END RSA PRIVATE KEY-----\n"
CODE = "orca://pair?code=Qq7_secret"
FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
ACTOR = "--actor=Firefox/Linux (127.0.0.1)"

conf = None


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


def vm(name, state="running", **fields):
    return {"name": name, "state": state, "libvirt_state": "running", "network": 1, "orca_port": 6801,
            "image": "base", "build_pin": None, "agents": {}, "devices": [], **fields}


def sha256(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


class V1Test(unittest.TestCase):
    UI = True                   # install a UI build
    UI_SOURCE = UI_BUILD        # which one: the placeholder (test_control_room_ui.py: the real one)
    ENV = {}
    WATCH = []                  # lines `agentbox watch` prints at once

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ui = os.path.join(self.dir, "ui")
        if self.UI:
            shutil.copytree(self.UI_SOURCE, self.ui)
        for path, content in TITAN.items():
            self.write(path, content)
        for link, target in DEVICE_LINKS.items():
            os.symlink(os.path.join(self.dir, target), os.path.join(self.dir, link))
        os.makedirs(os.path.join(self.dir, "bin"))
        if self.WATCH:
            self.watch_lines(*self.WATCH)
        self.journald = Journald(os.path.join(self.dir, "journal.socket"))
        self.addCleanup(self.journald.close)
        self.stderr_path = os.path.join(self.dir, "server.err")
        env = dict(os.environ, PATH=os.path.join(self.dir, "bin"), AGENTBOX_ROOT=os.path.join(self.dir, "root"),
                   AGENTBOX_WEB_CONF=conf.name, AGENTBOX_WEB_STATIC=STATIC, AGENTBOX_WEB_UI=self.ui,
                   AGENTBOX_WEB_BIND="127.0.0.1", AGENTBOX_WEB_PORT="0",
                   AGENTBOX_WEB_COMMAND=f"{sys.executable} -I {FAKE}", FAKE_AGENTBOX_DIR=self.dir,
                   AGENTBOX_WEB_JOURNAL=self.journald.path,
                   AGENTBOX_WEB_PROC=os.path.join(self.dir, "proc"), AGENTBOX_WEB_SYS=os.path.join(self.dir, "sys"),
                   AGENTBOX_WEB_SAMPLE_SECONDS="3600", AGENTBOX_WEB_HEARTBEAT_SECONDS="0.5",
                   AGENTBOX_WEB_WATCH_RETRY_SECONDS="0.1", AGENTBOX_WEB_UI_CHECK_SECONDS="0.1")
        env.update(self.ENV)
        with open(self.stderr_path, "w") as err:
            self.server = subprocess.Popen([sys.executable, "-I", SERVER], env=env, start_new_session=True,
                                           stdout=subprocess.DEVNULL, stderr=err)
        self.addCleanup(self.stop_server)
        self.ssl = ssl._create_unverified_context()
        self.wait_for_server()
        self.cookie = None

    def stop_server(self):
        if self.server.poll() is None:
            os.killpg(self.server.pid, signal.SIGTERM)
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.server.pid, signal.SIGKILL)
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

    def write(self, path, content):
        full = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full + ".new", "w") as f:
            f.write(content)
        os.replace(full + ".new", full)

    # --- fake agentbox -----------------------------------------------------------

    def respond(self, stdout=None, stderr="", exitcode=0):
        r = {"stderr": stderr, "exitcode": exitcode}
        if stdout is not None:
            r["stdout"] = stdout if isinstance(stdout, str) else json.dumps(stdout)
        self.write("response.json", json.dumps(r))

    def raw_calls(self):
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def calls(self):
        return [c[1:] if c and c[0].startswith("--actor=") else c for c in self.raw_calls()]

    def stdin(self):
        with open(os.path.join(self.dir, "stdin.jsonl")) as f:
            return [json.loads(line) for line in f]

    def watch_lines(self, *events):
        """Lines `agentbox watch` prints (now, or as soon as it notices them)."""
        path = os.path.join(self.dir, "watch.jsonl")
        try:
            with open(path) as f:
                old = f.read()
        except FileNotFoundError:
            old = ""
        lines = "".join(json.dumps({"event": n, "time": "2026-11-03T12:00:00Z", "data": d}) + "\n" for n, d in events)
        self.write("watch.jsonl", old + lines)

    def watch_starts(self):
        try:
            with open(os.path.join(self.dir, "watch-starts.jsonl")) as f:
                return len(f.readlines())
        except FileNotFoundError:
            return 0

    # --- HTTP --------------------------------------------------------------------

    def request(self, method, path, body=None, header=True, cookie=None, headers=None, raw=False):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ssl, timeout=60)
        h = {"User-Agent": FIREFOX}
        if body is not None:
            h["Content-Type"] = "application/json"
        if header:
            h["X-Agentbox"] = "1"
        if cookie or self.cookie:
            h["Cookie"] = cookie or self.cookie
        h.update(headers or {})
        data = None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, payload if raw else (json.loads(payload) if payload else None), resp
        finally:
            conn.close()

    def login(self):
        status, obj, resp = self.request("POST", "/api/v1/session", {"password": PASSWORD})
        self.assertEqual(status, 200, obj)
        self.cookie = resp.getheader("Set-Cookie").split(";")[0]

    def stream(self, last_id=None, path="/api/v1/events"):
        s = Stream(self, last_id=last_id)
        if path != "/api/events":
            s.close()
            s.conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ssl, timeout=10)
            headers = {"Cookie": self.cookie}
            if last_id:
                headers["Last-Event-ID"] = last_id
            s.conn.request("GET", path, headers=headers)
            s.resp = s.conn.getresponse()
            s.status = s.resp.status
        self.addCleanup(s.close)
        return s

    def assertError(self, answer, status, code):
        self.assertEqual(answer[0], status, answer[1])
        self.assertEqual(answer[1]["error"]["code"], code)
        self.assertRegex(answer[1]["error"]["message"], r"^[A-Z][\x20-\x7e]*$")


# --- sign-in, CSRF, routing ------------------------------------------------------

class AccessTest(V1Test):
    def test_everything_but_signing_in_needs_a_session(self):
        for method, path in (("GET", "/api/v1/vms"), ("GET", "/api/v1/session"), ("POST", "/api/v1/vms/work/reset"),
                             ("GET", "/api/v1/nope"), ("GET", "/api/v1/events"), ("PATCH", "/api/v1/settings"),
                             ("GET", "/api/v1/vms/work/logs")):
            with self.subTest(path=path):
                self.assertError(self.request(method, path, {} if method != "GET" else None), 401, "unauthenticated")
        self.assertError(self.request("GET", "/api/v1/vms", cookie="agentbox=bogus"), 401, "unauthenticated")
        self.assertEqual(self.calls(), [])

    def test_changes_need_the_csrf_header(self):
        self.login()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertError(self.request(method, "/api/v1/vms/work/reset", None, header=False), 403,
                                 "csrf_header_missing")
        self.assertError(self.request("POST", "/api/v1/session", None, header=False), 403, "csrf_header_missing")
        self.assertEqual(self.calls(), [])

    def test_sign_in_and_out(self):
        status, obj, resp = self.request("POST", "/api/v1/session", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(obj["authenticated"], True)
        self.assertRegex(obj["expires"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        cookie = resp.getheader("Set-Cookie")
        self.assertRegex(cookie, r"^agentbox=[A-Za-z0-9_-]{43}; Max-Age=43200; Path=/; Secure; HttpOnly; "
                                 r"SameSite=Strict$")
        self.cookie = cookie.split(";")[0]
        self.assertEqual(self.request("GET", "/api/v1/session")[:2], (200, obj))
        status, obj, resp = self.request("DELETE", "/api/v1/session")
        self.assertEqual((status, obj), (200, {"authenticated": False}))
        self.assertIn("Max-Age=0", resp.getheader("Set-Cookie"))
        self.assertError(self.request("GET", "/api/v1/session"), 401, "unauthenticated")
        kinds = [(e["AGENTBOX_ACTION"], e["AGENTBOX_ACTOR"]) for e in self.journald.entries()]
        self.assertEqual(kinds, [("signin", "Firefox/Linux (127.0.0.1)"), ("signout", "Firefox/Linux (127.0.0.1)")])

    def test_login_brake(self):
        for _ in range(5):
            self.assertError(self.request("POST", "/api/v1/session", {"password": "guess-4711"}), 401,
                             "wrong_password")
        answer = self.request("POST", "/api/v1/session", {"password": PASSWORD})
        self.assertError(answer, 429, "too_many_attempts")
        self.assertTrue(0 < int(answer[2].getheader("Retry-After")) <= 900)
        # the brake is shared with the old sign-in
        self.assertEqual(self.request("POST", "/api/login", {"password": PASSWORD})[0], 429)
        self.assertNotIn(PASSWORD, self.server_log())
        self.assertNotIn("guess-4711", json.dumps(self.journald.entries()))

    def test_odd_requests(self):
        self.login()
        self.assertError(self.request("POST", "/api/v1/session", {"password": 5}), 400, "invalid_field")
        self.assertError(self.request("GET", "/api/v1/nope"), 404, "not_found")
        answer = self.request("PUT", "/api/v1/vms")
        self.assertError(answer, 405, "method_not_allowed")
        self.assertEqual(answer[2].getheader("Allow"), "GET, POST")
        self.assertError(self.request("POST", "/api/v1/vms", b"[1]"), 400, "invalid_body")
        self.assertError(self.request("POST", "/api/v1/vms", b"{"), 400, "invalid_body")
        self.assertError(self.request("POST", "/api/v1/vms", b"{" + b" " * 9000 + b"}"), 413, "body_too_large")
        self.assertEqual(self.calls(), [])

    def test_old_api_unchanged(self):
        self.login()
        self.respond({"vm": "work", "state": "running", "message": "work neu gestartet"})
        self.assertEqual(self.request("POST", "/api/work/restart", {})[:2],
                         (200, {"ok": True, "message": json.dumps({"vm": "work", "state": "running",
                                                                   "message": "work neu gestartet"})}))
        self.assertEqual(self.request("GET", "/api/nope")[:2], (404, {"error": "not found"}))


class SessionLanguageTest(V1Test):
    """GET and POST /session carry the effective `default_language` of the Settings (#22),
    read from the file like session_hours, so the UI needs no GET /settings (a sudo call)."""

    def setUp(self):
        settings_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, settings_dir, True)
        self.settings = os.path.join(settings_dir, "settings.json")
        self.ENV = {"AGENTBOX_WEB_SETTINGS": self.settings}       # never the Host's file
        super().setUp()

    def settings_file(self, text):
        with open(self.settings, "w") as f:
            f.write(text)

    def sign_in(self):
        status, obj, resp = self.request("POST", "/api/v1/session", {"password": PASSWORD})
        self.assertEqual(status, 200, obj)
        self.cookie = resp.getheader("Set-Cookie").split(";")[0]
        return obj

    def language(self):
        status, obj, _ = self.request("GET", "/api/v1/session")
        self.assertEqual(status, 200, obj)
        return obj["default_language"]

    def test_browser_without_a_settings_file(self):
        obj = self.sign_in()
        self.assertEqual(obj["default_language"], "browser")
        self.assertEqual(set(obj), {"authenticated", "expires", "default_language"})
        self.assertEqual(self.language(), "browser")
        self.assertEqual(self.calls(), [])

    def test_the_value_of_the_file_at_every_request(self):
        self.settings_file('{"default_language": "de"}')
        self.assertEqual(self.sign_in()["default_language"], "de")
        self.settings_file('{"session_hours": 3, "default_language": "en"}')
        self.assertEqual(self.language(), "en")
        self.settings_file('{"default_language": "browser"}')
        self.assertEqual(self.language(), "browser")
        self.assertEqual(self.calls(), [])

    def test_unusable_values_mean_browser(self):
        self.sign_in()
        for text in ("kaputt", "[]", "{}", '{"default_language": "fr"}', '{"default_language": "EN"}',
                     '{"default_language": 1}', '{"default_language": ["de"]}', '{"default_language": null}'):
            with self.subTest(text=text):
                self.settings_file(text)
                self.assertEqual(self.language(), "browser")

    def test_not_without_a_session(self):
        self.settings_file('{"default_language": "de"}')
        self.assertError(self.request("GET", "/api/v1/session"), 401, "unauthenticated")
        self.sign_in()
        status, obj, _ = self.request("DELETE", "/api/v1/session")
        self.assertEqual((status, obj), (200, {"authenticated": False}))


# --- every endpoint maps onto one agentbox call --------------------------------------

ANSWER = {"vm": "work", "state": "running", "message": "irgendwas"}
ENDPOINTS = [
    ("GET", "/api/v1/vms", None, ["describe", "--json"], 200),
    ("GET", "/api/v1/vms/work", None, ["describe", "work", "--json"], 200),
    ("POST", "/api/v1/vms", {"name": "lab", "image": "rust", "memory_mib": 8192, "data_disk_gib": "50",
                             "copy_allowlist_from": "work", "github_owners": ["likt0r", " acme-corp ", ""]},
     ["create", "lab", "--image=rust", "--memory-mib=8192", "--data-disk-gib=50", "--copy-allowlist-from=work",
      "--owner=likt0r", "--owner=acme-corp", "--json"], 201),
    ("POST", "/api/v1/vms", {"name": "lab"}, ["create", "lab", "--json"], 201),
    ("DELETE", "/api/v1/vms/lab", {"confirm": "lab"}, ["delete", "lab", "--confirm=lab", "--json"], 200),
    ("DELETE", "/api/v1/vms/lab", {"confirm": "lab", "delete_data_disk": True},
     ["delete", "lab", "--confirm=lab", "--delete-data-disk", "--json"], 200),
    ("POST", "/api/v1/vms/work/start", {}, ["start", "work", "--json"], 200),
    ("POST", "/api/v1/vms/work/pause", None, ["pause", "work", "--json"], 200),
    ("POST", "/api/v1/vms/work/restart", {}, ["restart", "work", "--json"], 200),
    ("POST", "/api/v1/vms/work/reset", {}, ["reset", "work", "--json"], 200),
    ("POST", "/api/v1/vms/work/reset", {"start": False}, ["reset", "work", "--no-start", "--json"], 200),
    ("POST", "/api/v1/vms/work/focus", {}, ["focus", "work", "--json"], 200),
    ("DELETE", "/api/v1/focus", None, ["focus", "off", "--json"], 200),
    ("PUT", "/api/v1/vms/work/github-owners", {"owners": ["likt0r", "acme-corp"]},
     ["set-owners", "work", "likt0r", "acme-corp", "--json"], 200),
    ("PUT", "/api/v1/vms/work/memory", {"memory_mib": 16384}, ["set-memory", "work", "16384", "--json"], 200),
    # the whole list in one step; a value stays the value of its name, whatever it looks like
    ("PUT", "/api/v1/vms/work/env", {"env": {"DATABASE_URL": "postgres://db/x", "FLAG": "--json", "E": ""}},
     ["set-env", "work", "--replace", "DATABASE_URL=postgres://db/x", "FLAG=--json", "E=", "--json"], 200),
    ("PUT", "/api/v1/vms/work/env", {"env": {}}, ["set-env", "work", "--replace", "--json"], 200),
    ("PUT", "/api/v1/vms/work/image", {"image": "rust", "build": None}, ["set-image", "work", "rust", "--json"], 200),
    ("PUT", "/api/v1/vms/work/image", {"image": "base", "build": "20261101-120000"},
     ["set-image", "work", "base", "--build=20261101-120000", "--json"], 200),
    ("POST", "/api/v1/vms/work/open-web", {"minutes": 45}, ["open-web", "work", "45", "--json"], 200),
    ("DELETE", "/api/v1/vms/work/open-web", None, ["close-web", "work", "--json"], 200),
    ("GET", "/api/v1/vms/work/logs?unit=orca.service&priority=err&since=-1h&grep=a%20b&lines=50&after_cursor=s%3D1",
     None, ["logs", "work", "--unit=orca.service", "--priority=err", "--since=-1h", "--grep=a b", "--lines=50",
            "--after-cursor=s=1", "--json"], 200),
    ("GET", "/api/v1/vms/work/blocked", None, ["blocked", "work", "--json"], 200),
    ("GET", "/api/v1/vms/work/alerts", None, ["alerts", "work", "--json"], 200),
    ("POST", "/api/v1/vms/work/alerts/seen", {}, ["mark-seen", "work", "--json"], 200),
    ("POST", "/api/v1/vms/work/alerts/seen", {"domain": "Evil.Example"}, ["mark-seen", "work", "evil.example",
                                                                         "--json"], 200),
    ("GET", "/api/v1/vms/work/github-tokens?since=-7d&lines=20", None,
     ["github-tokens", "work", "--since=-7d", "--lines=20", "--json"], 200),
    ("GET", "/api/v1/vms/work/allowlist", None, ["allowlist", "work", "--json"], 200),
    ("GET", "/api/v1/vms/work/pairings", None, ["pairings", "work", "--json"], 200),
    ("POST", "/api/v1/vms/work/pairings/remove", {"device": "laptop-1"}, ["unpair", "work", "laptop-1", "--json"],
     200),
    ("POST", "/api/v1/vms/work/pairings/revoke", {"confirm": "work"},
     ["revoke-pairings", "work", "--confirm=work", "--json"], 200),
    ("GET", "/api/v1/allowlist", None, ["allowlist", "--json"], 200),
    ("GET", "/api/v1/allowlist?vm=work", None, ["allowlist", "work", "--json"], 200),
    ("POST", "/api/v1/allowlist", {"scope": "all", "domain": "Docs.Example.org", "duration": "1h", "note": "docs"},
     ["allow", "all", "docs.example.org", "--for=1h", "--note=docs", "--json"], 201),
    ("POST", "/api/v1/allowlist", {"scope": "work", "domain": "x.org", "duration": "default"},
     ["allow", "work", "x.org", "--temporary", "--json"], 201),
    ("POST", "/api/v1/allowlist", {"scope": "work", "domain": "x.org"}, ["allow", "work", "x.org", "--json"], 201),
    ("DELETE", "/api/v1/allowlist/work/x.org", None, ["disallow", "work", "x.org", "--json"], 200),
    ("POST", "/api/v1/allowlist/all/x.org/extend", {"duration": "7d"}, ["extend", "all", "x.org", "--for=7d",
                                                                        "--json"], 200),
    ("POST", "/api/v1/allowlist/all/x.org/make-permanent", {}, ["make-permanent", "all", "x.org", "--json"], 200),
    ("PUT", "/api/v1/allowlist/work/x.org/note", {"note": "fuer --docs"},
     ["set-note", "work", "x.org", "--note=fuer --docs", "--json"], 200),
    # Regex entries (ADR 0013): the pattern verbatim, as the value of --regex, never lowercased;
    # in a path under /regex it is the query parameter regex
    ("POST", "/api/v1/allowlist", {"scope": "work", "regex": " \t^Mirror\\d+\\.example$\r\n", "duration": "1h",
                                   "confirm": True},
     ["allow", "work", "--regex=^Mirror\\d+\\.example$", "--confirm-pattern", "--for=1h", "--json"], 201),
    ("POST", "/api/v1/allowlist", {"scope": "all", "regex": "--force", "domain": None, "confirm": True},
     ["allow", "all", "--regex=--force", "--confirm-pattern", "--json"], 201),
    ("DELETE", "/api/v1/allowlist/work/regex?regex=%5Ea%2Fb%2B%5C.example%24", None,
     ["disallow", "work", "--regex=^a/b+\\.example$", "--json"], 200),
    ("POST", "/api/v1/allowlist/all/regex/extend?regex=x%26y", {"duration": "1h"},
     ["extend", "all", "--regex=x&y", "--for=1h", "--json"], 200),
    ("POST", "/api/v1/allowlist/all/regex/make-permanent?regex=x", {}, ["make-permanent", "all", "--regex=x", "--json"],
     200),
    ("PUT", "/api/v1/allowlist/all/regex/note?regex=x", {"note": "n"}, ["set-note", "all", "--regex=x", "--note=n",
                                                                       "--json"], 200),
    ("GET", "/api/v1/regex/check?list=allowlist&regex=%5Ea%24&host=A.example&scope=work", None,
     ["check-regex", "allowlist", "--regex=^a$", "a.example", "--scope=work", "--json"], 200),
    ("GET", "/api/v1/regex/check?list=watchlist&regex=.*", None, ["check-regex", "watchlist", "--regex=.*", "--json"],
     200),
    ("GET", "/api/v1/watchlist", None, ["watchlist", "--json"], 200),
    ("GET", "/api/v1/watchlist/check?domain=Paste.Example", None, ["check-domain", "paste.example", "--json"], 200),
    ("GET", "/api/v1/watchlist/harmless", None, ["harmless", "--json"], 200),
    ("PUT", "/api/v1/watchlist/harmless/cdn.example", None, ["mark-harmless", "cdn.example", "--json"], 200),
    ("DELETE", "/api/v1/watchlist/harmless/cdn.example", None, ["unmark-harmless", "cdn.example", "--json"], 200),
    ("PUT", "/api/v1/watchlist/harmless/regex?regex=%5Ecdn%5B0-9%5D%2B%5C.example%24", None,
     ["mark-harmless", "--regex=^cdn[0-9]+\\.example$", "--json"], 200),
    ("DELETE", "/api/v1/watchlist/harmless/regex?regex=x", None, ["unmark-harmless", "--regex=x", "--json"], 200),
    ("POST", "/api/v1/watchlist/entries", {"domain": "Drop.Example", "category": "paste", "source": "Vorfall",
                                           "note": "--force"},
     ["watch-add", "drop.example", "--category=paste", "--source=Vorfall", "--note=--force", "--json"], 201),
    ("POST", "/api/v1/watchlist/entries", {"domain": "drop.example", "category": "c2"},
     ["watch-add", "drop.example", "--category=c2", "--json"], 201),
    ("DELETE", "/api/v1/watchlist/entries/drop.example", None, ["watch-remove", "drop.example", "--json"], 200),
    ("PUT", "/api/v1/watchlist/entries/drop.example/note", {"note": ""},
     ["watch-set-note", "drop.example", "--note=", "--json"], 200),
    ("POST", "/api/v1/watchlist/entries", {"regex": "^Drop[0-9]+\\.xyz$", "category": "paste"},
     ["watch-add", "--regex=^Drop[0-9]+\\.xyz$", "--category=paste", "--json"], 201),
    ("DELETE", "/api/v1/watchlist/entries/regex?regex=x", None, ["watch-remove", "--regex=x", "--json"], 200),
    ("PUT", "/api/v1/watchlist/entries/regex/note?regex=x", {"note": ""},
     ["watch-set-note", "--regex=x", "--note=", "--json"], 200),
    ("GET", "/api/v1/watchlist/legitimate", None, ["legitimate", "--json"], 200),
    ("POST", "/api/v1/watchlist/legitimate", {"domain": "Mirror.Example", "note": "Spiegel"},
     ["legitimate-add", "mirror.example", "--note=Spiegel", "--json"], 201),
    ("DELETE", "/api/v1/watchlist/legitimate/mirror.example", None, ["legitimate-remove", "mirror.example", "--json"],
     200),
    ("PUT", "/api/v1/watchlist/legitimate/mirror.example/note", {"note": "x"},
     ["legitimate-set-note", "mirror.example", "--note=x", "--json"], 200),
    ("POST", "/api/v1/watchlist/legitimate", {"regex": "^m[0-9]\\.example$", "note": "n"},
     ["legitimate-add", "--regex=^m[0-9]\\.example$", "--note=n", "--json"], 201),
    ("DELETE", "/api/v1/watchlist/legitimate/regex?regex=x", None, ["legitimate-remove", "--regex=x", "--json"], 200),
    ("PUT", "/api/v1/watchlist/legitimate/regex/note?regex=x", {"note": "x"},
     ["legitimate-set-note", "--regex=x", "--note=x", "--json"], 200),
    ("GET", "/api/v1/activity?kind=alert,build&vm=work&search=x%20y&lines=10", None,
     ["activity", "--kind=alert,build", "--vm=work", "--search=x y", "--lines=10", "--json"], 200),
    ("GET", "/api/v1/images", None, ["images", "--json"], 200),
    ("POST", "/api/v1/builds", {"image": "all"}, ["build", "--all", "--json"], 202),
    ("POST", "/api/v1/builds", {"image": "rust"}, ["build", "rust", "--json"], 202),
    ("POST", "/api/v1/builds", {"image": "base", "reset": ["work", "priv"]},
     ["build", "base", "--reset=work", "--reset=priv", "--json"], 202),
    ("POST", "/api/v1/builds", {"image": "base", "reset": []}, ["build", "base", "--json"], 202),
    ("POST", "/api/v1/updates", {}, ["update-now", "--json"], 202),
    ("POST", "/api/v1/updates", {"reset": ["work", "priv"]},
     ["update-now", "--reset=work", "--reset=priv", "--json"], 202),
    ("GET", "/api/v1/images/base/log", None, ["build-log", "base", "--json"], 200),
    ("GET", "/api/v1/images/base/log?build=20261101-120000&after_cursor=c1&lines=100", None,
     ["build-log", "base", "20261101-120000", "--after-cursor=c1", "--lines=100", "--json"], 200),
    ("GET", "/api/v1/images/base/containerfile", None, ["containerfile", "base", "--json"], 200),
    ("GET", "/api/v1/images/rust-2/containerfile", None, ["containerfile", "rust-2", "--json"], 200),
    ("GET", "/api/v1/github-app", None, ["github-app", "--json"], 200),
    ("DELETE", "/api/v1/github-app/777", None, ["remove-github-app", "777", "--json"], 200),
    ("GET", "/api/v1/settings", None, ["settings", "--json"], 200),
    ("PATCH", "/api/v1/settings", {"reset_age_days": 10, "default_language": "en"},
     ["set-settings", "reset_age_days=10", "default_language=en", "--json"], 200),
    ("PATCH", "/api/v1/settings", {"auto_update": False}, ["set-settings", "auto_update=false", "--json"], 200),
    ("PATCH", "/api/v1/settings", {"auto_update": "true"}, ["set-settings", "auto_update=true", "--json"], 200),
]


class EndpointTest(V1Test):
    def setUp(self):
        super().setUp()
        self.login()
        self.respond(ANSWER)

    def test_each_endpoint_is_one_agentbox_call_with_the_actor(self):
        for method, path, body, argv, status in ENDPOINTS:
            with self.subTest(method=method, path=path):
                before = len(self.raw_calls())
                answer = self.request(method, path, body)
                self.assertEqual(answer[:2], (status, ANSWER))
                calls = self.raw_calls()[before:]
                self.assertEqual(calls, [[ACTOR, *argv]])

    def test_activity_entries_keep_their_code_and_parameters(self):
        # #69: the UI words an entry from its code and parameters, a failure also from its error
        # code; an entry from before has its description only
        timeline = {"entries": [
            {"time": "2026-11-09T12:00:00.000000Z", "vm": "work", "actor": "Firefox/Linux (127.0.0.1)",
             "kind": "allowlist", "action": "allow", "outcome": "failed",
             "description": "Allow evil.example: evil.example is suspicious (exfil). Allowing it only with --force",
             "code": "allow", "params": {"domain": "evil.example", "scope": "work"}, "error": "domain_suspicious",
             "error_params": {"domain": "evil.example"}},
            {"time": "2026-11-09T11:00:00.000000Z", "vm": None, "actor": "timer", "kind": "allowlist",
             "action": "allowlist.expired", "outcome": "ok", "description": "Temporary Allowlist entry a.example "
             "for all VMs expired (2026-11-09 11:00)", "code": "allowlist.expired",
             "params": {"domain": "a.example", "scope": "all"}},
            {"time": "2026-11-01T10:00:00.000000Z", "vm": "work", "actor": "x", "kind": "allowlist", "action": "allow",
             "outcome": "ok", "description": "Freigabe example.org"}],
            "truncated": False, "retention_days": 90}
        self.respond(timeline)
        self.assertEqual(self.request("GET", "/api/v1/activity")[:2], (200, timeline))
        self.assertEqual(self.calls()[-1], ["activity", "--json"])

    def test_changes_are_logged_without_content(self):
        self.request("POST", "/api/v1/vms/work/reset", {})
        self.request("GET", "/api/v1/vms")
        log = self.server_log()
        self.assertIn("127.0.0.1: work v1 POST post_reset -> ok", log)
        self.assertNotIn("describe", log)

    def test_values_stay_values(self):
        self.request("POST", "/api/v1/vms", {"name": "lab", "image": "--delete-data-disk"})
        self.request("PUT", "/api/v1/allowlist/work/x.org/note", {"note": "--json"})
        self.assertEqual(self.calls(), [["create", "lab", "--image=--delete-data-disk", "--json"],
                                        ["set-note", "work", "x.org", "--note=--json", "--json"]])

    def test_invalid_requests_never_reach_agentbox(self):
        cases = [
            ("POST", "/api/v1/vms", {"name": "Lab"}, "invalid_name"),
            ("POST", "/api/v1/vms", {}, "invalid_field"),
            ("POST", "/api/v1/vms", {"name": "lab", "memory_mib": True}, "invalid_field"),
            ("POST", "/api/v1/vms", {"name": "lab", "github_owners": "likt0r"}, "invalid_field"),
            ("POST", "/api/v1/vms", {"name": "lab", "github_owners": ["-x"]}, "invalid_field"),
            ("DELETE", "/api/v1/vms/lab", {"confirm": "work"}, "confirmation_required"),
            ("DELETE", "/api/v1/vms/lab", {"confirm": "lab", "delete_data_disk": "yes"}, "invalid_field"),
            ("POST", "/api/v1/vms/work/reset", {"start": "no"}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/github-owners", {"owners": []}, "github_owner_required"),
            ("PUT", "/api/v1/vms/work/memory", {}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/memory", {"memory_mib": "16384"}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/memory", {"memory_mib": True}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/memory", {"memory_mib": 8192.5}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/memory", {"memory_mib": -8192}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/env", {}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/env", {"env": ["A=1"]}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/env", {"env": {"A": 1}}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/env", {"env": {"A": None}}, "invalid_field"),
            # a name agentbox would read differently (NAME=VALUE, an option), a NUL no argument carries
            ("PUT", "/api/v1/vms/work/env", {"env": {"A=B": "c"}}, "invalid_env_name"),
            ("PUT", "/api/v1/vms/work/env", {"env": {"--replace": ""}}, "invalid_env_name"),
            ("PUT", "/api/v1/vms/work/env", {"env": {"": "x"}}, "invalid_env_name"),
            ("PUT", "/api/v1/vms/work/env", {"env": {"A" * 65: "x"}}, "invalid_env_name"),
            ("PUT", "/api/v1/vms/work/env", {"env": {"A": "a\u0000b"}}, "invalid_env_value"),
            # a lone UTF-16 surrogate (JSON "\\ud800") is no UTF-8 an argument could carry
            ("PUT", "/api/v1/vms/work/env", {"env": {"A": "a\ud800b"}}, "invalid_env_value"),
            ("PUT", "/api/v1/vms/work/env", {"env": {"A": "\udfff"}}, "invalid_env_value"),
            ("PUT", "/api/v1/vms/work/image", {"image": "../x"}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/image", {"image": "base", "build": "latest"}, "invalid_field"),
            ("PUT", "/api/v1/vms/work/claude-token", {"token": " "}, "invalid_field"),
            ("POST", "/api/v1/vms/work/open-web", {"minutes": "30; rm"}, "invalid_field"),
            ("POST", "/api/v1/vms/work/pairing-code", {"confirm": "yes"}, "confirmation_required"),
            ("POST", "/api/v1/vms/work/pairings/remove", {"device": "--all"}, "invalid_field"),
            ("POST", "/api/v1/vms/work/pairings/revoke", {}, "confirmation_required"),
            ("POST", "/api/v1/allowlist", {"scope": 5, "domain": "x.org"}, "invalid_field"),
            ("POST", "/api/v1/allowlist", {"scope": "Everyone", "domain": "x.org"}, "invalid_field"),
            ("POST", "/api/v1/allowlist", {"scope": "all", "domain": "-x.org"}, "invalid_field"),
            ("POST", "/api/v1/allowlist", {"scope": "all", "domain": "x.org", "duration": "2h"}, "invalid_field"),
            ("POST", "/api/v1/allowlist/all/x.org/extend", {"duration": "permanent"}, "invalid_field"),
            ("PUT", "/api/v1/allowlist/all/x.org/note", {}, "invalid_field"),
            ("DELETE", "/api/v1/allowlist/all/-x", None, "invalid_field"),
            ("GET", "/api/v1/allowlist?vm=Work", None, "vm_not_found"),
            ("GET", "/api/v1/watchlist/check", None, "invalid_domain"),
            ("POST", "/api/v1/watchlist/entries", {"domain": "x.org"}, "invalid_field"),
            ("POST", "/api/v1/watchlist/entries", {"domain": "x.org", "category": "--force"}, "invalid_field"),
            ("POST", "/api/v1/watchlist/entries", {"domain": "-x.org", "category": "exfil"}, "invalid_field"),
            ("POST", "/api/v1/watchlist/entries", {"domain": "x.org", "category": "exfil", "source": 5},
             "invalid_field"),
            ("DELETE", "/api/v1/watchlist/entries/--force", None, "invalid_field"),
            ("PUT", "/api/v1/watchlist/entries/x.org/note", {}, "invalid_field"),
            ("POST", "/api/v1/watchlist/legitimate", {"domain": ["x.org"]}, "invalid_field"),
            ("POST", "/api/v1/watchlist/legitimate", {"domain": "x.org", "note": 1}, "invalid_field"),
            ("DELETE", "/api/v1/watchlist/legitimate/-x.org", None, "invalid_field"),
            ("PUT", "/api/v1/watchlist/legitimate/x.org/note", {"note": None}, "invalid_field"),
            # a regex Allowlist entry only confirmed, after the preview
            ("POST", "/api/v1/allowlist", {"scope": "all", "regex": "^a\\.example$"}, "pattern_confirmation_required"),
            ("POST", "/api/v1/allowlist", {"scope": "all", "regex": "^a\\.example$", "confirm": "yes"},
             "pattern_confirmation_required"),
            ("POST", "/api/v1/allowlist", {"scope": "all", "regex": "^a$", "domain": "a.org", "confirm": True},
             "invalid_field"),
            ("POST", "/api/v1/allowlist", {"scope": "all", "regex": 5, "confirm": True}, "invalid_field"),
            ("POST", "/api/v1/allowlist", {"scope": "all", "regex": " ", "confirm": True}, "invalid_field"),
            # nothing but printable ASCII without spaces: a NUL byte cannot even be a process argument
            ("POST", "/api/v1/allowlist", {"scope": "all", "regex": "a\u0000b", "confirm": True}, "invalid_field"),
            ("POST", "/api/v1/allowlist", {"scope": "all", "regex": "a b", "confirm": True}, "invalid_field"),
            ("POST", "/api/v1/watchlist/entries", {"regex": "\u00e4\\.example", "category": "exfil"}, "invalid_field"),
            ("DELETE", "/api/v1/allowlist/all/regex?regex=a%00b", None, "invalid_field"),
            ("PUT", "/api/v1/watchlist/harmless/regex?regex=a%0Ab", None, "invalid_field"),
            ("GET", "/api/v1/regex/check?list=watchlist&regex=a%09b", None, "invalid_field"),
            # only spaces, tabs, CR and LF are dropped around it; other control or Unicode
            # whitespace is refused, not trimmed away
            ("POST", "/api/v1/allowlist", {"scope": "all", "regex": "\u001c^a\\.example$", "confirm": True},
             "invalid_field"),
            ("POST", "/api/v1/allowlist", {"scope": "all", "regex": "^a\\.example$\u0085", "confirm": True},
             "invalid_field"),
            ("POST", "/api/v1/watchlist/legitimate", {"regex": "\u00a0^a\\.example$"}, "invalid_field"),
            ("DELETE", "/api/v1/allowlist/all/regex?regex=%1Fa", None, "invalid_field"),
            ("DELETE", "/api/v1/allowlist/all/regex", None, "invalid_field"),
            ("DELETE", "/api/v1/allowlist/all/regex?regex=a&regex=b", None, "invalid_query"),
            ("POST", "/api/v1/allowlist/all/regex/extend?regex=a", {}, "invalid_field"),
            ("GET", "/api/v1/regex/check?list=allow&regex=a", None, "invalid_field"),
            ("GET", "/api/v1/regex/check?list=watchlist", None, "invalid_field"),
            ("GET", "/api/v1/regex/check?list=allowlist&regex=a&host=--x", None, "invalid_field"),
            ("GET", "/api/v1/regex/check?list=allowlist&regex=a&scope=Every", None, "invalid_field"),
            ("POST", "/api/v1/watchlist/entries", {"regex": ["a"], "category": "exfil"}, "invalid_field"),
            ("PUT", "/api/v1/watchlist/harmless/regex", None, "invalid_field"),
            ("PUT", "/api/v1/watchlist/entries/regex/note?regex=a", {}, "invalid_field"),
            ("GET", "/api/v1/activity?vm=a&vm=b", None, "invalid_query"),
            ("POST", "/api/v1/builds", {"image": "../x"}, "invalid_field"),
            ("POST", "/api/v1/builds", {"image": "all", "reset": ["work"]}, "invalid_field"),
            ("POST", "/api/v1/builds", {"image": "base", "reset": "work"}, "invalid_field"),
            ("POST", "/api/v1/builds", {"image": "base", "reset": ["--all"]}, "invalid_field"),
            ("POST", "/api/v1/builds", {"image": "base", "reset": ["Work"]}, "invalid_field"),
            ("POST", "/api/v1/builds", {"image": "base", "reset": [None]}, "invalid_field"),
            ("POST", "/api/v1/updates", {"reset": "work"}, "invalid_field"),
            ("POST", "/api/v1/updates", {"reset": ["--all"]}, "invalid_field"),
            ("POST", "/api/v1/updates", {"reset": ["work"] * 65}, "invalid_field"),
            ("GET", "/api/v1/images/base/log?build=x", None, "invalid_build"),
            ("PUT", "/api/v1/github-app", {"app_id": "12a", "private_key": PEM}, "invalid_app_id"),
            ("DELETE", "/api/v1/github-app/0777", None, "invalid_app_id"),
            ("DELETE", "/api/v1/github-app/--json", None, "invalid_app_id"),
            ("PATCH", "/api/v1/settings", {}, "invalid_field"),
            ("PATCH", "/api/v1/settings", {"password": "x"}, "unknown_setting"),
            ("PATCH", "/api/v1/settings", {"reset_age_days": [1]}, "invalid_field"),
            ("PATCH", "/api/v1/settings", {"reset_age_days": True}, "invalid_field"),
            ("PATCH", "/api/v1/settings", {"auto_update": None}, "invalid_field"),
        ]
        for method, path, body, code in cases:
            with self.subTest(path=path, body=body):
                answer = self.request(method, path, body)
                self.assertError(answer, 404 if code == "vm_not_found" else 400, code)
        self.assertEqual(self.calls(), [])

    def test_vm_names_in_paths(self):
        for path in ("/api/v1/vms/Work", "/api/v1/vms/x/reset", "/api/v1/vms/--all/reset", "/api/v1/vms/../reset"):
            with self.subTest(path=path):
                self.assertError(self.request("POST", path, {}), 404, "not_found")
        self.assertEqual(self.calls(), [])

    def test_image_names_in_paths(self):
        for path in ("/api/v1/images/Base/containerfile", "/api/v1/images/-x/containerfile",
                     "/api/v1/images/../containerfile", "/api/v1/images/base/containerfile/x",
                     "/api/v1/images/base%2F..%2Fx/containerfile"):
            with self.subTest(path=path):
                self.assertError(self.request("GET", path), 404, "not_found")
        # read only
        self.assertError(self.request("POST", "/api/v1/images/base/containerfile", {}), 405, "method_not_allowed")
        self.assertEqual(self.calls(), [])


# --- errors from agentbox ---------------------------------------------------------------

def failure(error, message, **params):
    """What agentbox writes on stderr when a call with --json fails (agentbox/cli.py)."""
    return json.dumps({"error": error, "message": message, "params": params})


def server_module():
    loader = importlib.machinery.SourceFileLoader("agentbox_web", SERVER)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class ErrorTest(V1Test):
    def setUp(self):
        super().setUp()
        self.login()

    def fails(self, stderr, exitcode=1, method="POST", path="/api/v1/vms/work/reset", body=None):
        self.respond("", stderr=stderr, exitcode=exitcode)
        return self.request(method, path, {} if body is None and method != "GET" else body)

    def test_codes_become_statuses(self):
        cases = [
            ("vm_not_found", "No such VM: lab", 1, 404),
            ("vm_exists", "lab exists already", 1, 409),
            ("name_reserved", "The name status is reserved", 1, 400),
            ("vm_not_running", "priv: the VM is not running (agentbox start priv)", 3, 409),
            ("guest_agent_unavailable", "priv: the guest agent does not answer (timeout)", 4, 503),
            ("vm_query_timeout", "x: query stopped in the VM after 20 s, narrow the filters", 5, 504),
            ("vm_journal_failed", "priv: journalctl fails in the VM: x", 6, 502),
            ("domain_suspicious", "evil.example is suspicious (exfil). Allowing it only with --force", 1, 409),
            ("allowlist_entry_not_found", "all VMs: x.org is not on the Allowlist", 1, 404),
            ("force_required", "x.org is on the Watchlist as exfil. Removing it only with --force", 1, 409),
            ("watchlist_recreate_force_required", "Watchlist unreadable, Open web closed: x missing; ... Creating it "
                                                  "again only with --force", 1, 409),
            ("builtin_entry", "github.com is built in and stays legitimate", 1, 409),
            ("watchlist_entry_not_found", "x.org is not on the Watchlist", 1, 404),
            ("legitimate_domain_not_found", "x.org is not an added Legitimate domain", 1, 404),
            ("invalid_category", "--category must be exfil, c2", 1, 400),
            ("watchlist_unreadable", "Watchlist unreadable, Open web closed", 1, 409),
            ("legitimate_unreadable", "Legitimate domains unreadable", 1, 409),
            ("migration_required", "1 line cannot be taken over", 1, 409),
            ("build_running", "a Build of base runs already", 1, 409),
            ("update_running", "an update runs already", 1, 409),
            ("vm_uses_other_image", "work uses Image rust, not base", 1, 409),
            ("vm_pinned", "priv is pinned to Build 20261101", 1, 409),
            ("build_not_found", "Build 20261101 of Image base does not exist", 1, 404),
            ("containerfile_not_found", "no Containerfile for Image cuda", 1, 404),
            ("invalid_image", "invalid Image: all", 1, 400),
            ("claude_token_rejected", "Anthropic rejects the token: HTTP 401", 1, 422),
            ("github_app_rejected", "GitHub rejects the App ID or key", 1, 422),
            ("github_app_not_found", "GitHub App 777 is not set up", 1, 404),
            ("unpair_unsupported", "work: Orca cannot remove a single device", 1, 501),
            ("unknown_setting", "unknown setting: x", 1, 400),
            ("invalid_argument", "invalid value for --since: yesterday", 2, 400),
            ("vm_not_off", "work: the VM is not off (running), the System disk stays unchanged", 1, 409),
            ("reset_start_failed", "work: fresh System disk, but the start fails", 1, 500),
            ("build_mismatch", "work: the VM runs on another Build", 1, 500),
            ("sudo_failed", "reset needs root: sudo agentbox reset work", 1, 500),
            ("action_failed", "error: Failed to start domain 'agentbox-work'", 1, 500),
        ]
        for error, message, exitcode, status in cases:
            with self.subTest(error=error):
                answer = self.fails(failure(error, message, vm="work"), exitcode)
                self.assertError(answer, status, error)
                # agentbox's own text only as detail
                self.assertEqual(answer[1]["error"]["detail"], message)
                self.assertNotIn("field", answer[1]["error"])

    def test_the_code_decides_not_the_wording(self):
        # a message that names a missing VM, but the code says what happened
        answer = self.fails(failure("invalid_argument", "No such VM: lab"))
        self.assertError(answer, 400, "invalid_argument")
        answer = self.fails(failure("vm_not_found", "anything at all"))
        self.assertError(answer, 404, "vm_not_found")

    def test_agentbox_s_own_line_counts(self):
        # programs agentbox ran may have written before it, the untrusted warning comes after
        stderr = "\n".join([failure("vm_not_found", "from a program agentbox ran"), "error: libvirt said x",
                            failure("vm_paused", "work is paused"),
                            "agentbox: 2 journal entries left out: no known writer wrote them"])
        answer = self.fails(stderr)
        self.assertError(answer, 409, "vm_paused")
        self.assertEqual(answer[1]["error"]["detail"], "work is paused")

    def test_without_a_code_the_exit_code_decides(self):
        cases = [
            ("agentbox: invalid value for --since: yesterday", 2, 400, "invalid_argument"),
            ("agentbox: priv: the VM is not running", 3, 409, "vm_not_running"),
            ("agentbox: priv: the guest agent does not answer", 4, 503, "guest_agent_unavailable"),
            ("agentbox: priv: query took too long", 5, 504, "vm_query_timeout"),
            ("agentbox: priv: journalctl fails in the VM", 6, 502, "vm_journal_failed"),
            ("agentbox: No such VM: lab", 1, 500, "action_failed"),
            ("error: Failed to start domain 'agentbox-work'", 1, 500, "action_failed"),
            ("Traceback (most recent call last):", 1, 500, "action_failed"),
            ("sudo: a password is required", 1, 500, "sudo_failed"),
            # half a line, or an object that is not a failure
            ('{"error": "vm_not_found", "mess', 3, 409, "vm_not_running"),
            (json.dumps({"error": "vm_not_found"}), 1, 500, "action_failed"),
            (json.dumps(["vm_not_found", "x"]), 1, 500, "action_failed"),
        ]
        for stderr, exitcode, status, code in cases:
            with self.subTest(stderr=stderr, exitcode=exitcode):
                self.assertError(self.fails(stderr, exitcode), status, code)
        answer = self.fails("agentbox: priv: the VM is not running", 3)
        self.assertEqual(answer[1]["error"]["detail"], "priv: the VM is not running")

    def test_a_code_the_table_does_not_know_is_action_failed(self):
        # agentbox named a code, so its exit code says nothing more: exit 3 is not vm_not_running
        for exitcode in (1, 2, 3, 4, 5, 6):
            with self.subTest(exitcode=exitcode):
                answer = self.fails(failure("something_new", "a new refusal", vm="work"), exitcode)
                self.assertError(answer, 500, "action_failed")
                self.assertEqual(answer[1]["error"]["detail"], "a new refusal")
                self.assertNotIn("field", answer[1]["error"])

    def test_what_a_pattern_breaks_goes_to_the_ui(self):
        # which rule, or which host of the breadth check's sample, so the UI can word it
        body = {"regex": "(a+)+", "category": "c2"}
        answer = self.fails(failure("invalid_pattern", "--regex (a+)+: a quantifier ...", reason="nested-quantifier"),
                            path="/api/v1/watchlist/entries", body=body)
        self.assertError(answer, 400, "invalid_pattern")
        self.assertEqual(answer[1]["error"]["params"], {"reason": "nested-quantifier"})
        answer = self.fails(failure("pattern_too_broad", "--regex .*: too broad", host="google.com"),
                            path="/api/v1/watchlist/entries", body=body)
        self.assertError(answer, 400, "pattern_too_broad")
        self.assertEqual(answer[1]["error"]["params"], {"host": "google.com"})
        # only those parameters, in their form; other codes pass none
        for stderr in (failure("invalid_pattern", "x", reason="<b>x</b>", pattern="(a+)+"),
                       failure("pattern_too_broad", "x", host=["google.com"]),
                       failure("vm_not_found", "No such VM: lab", vm="lab")):
            with self.subTest(stderr=stderr):
                self.assertNotIn("params", self.fails(stderr, path="/api/v1/watchlist/entries", body=body)[1]["error"])

    def test_an_invalid_setting_names_its_field(self):
        # the UI shows the message next to that input: the setting comes from the parameters,
        # and only a known key is a field
        settings = dict(method="PATCH", path="/api/v1/settings", body={"reset_time": "99:99"})
        answer = self.fails(failure("invalid_setting", "reset_time: invalid value 99:99", setting="reset_time",
                                    value="99:99"), **settings)
        self.assertError(answer, 400, "invalid_setting")
        self.assertEqual(answer[1]["error"]["field"], "reset_time")
        answer = self.fails(failure("invalid_setting", "reset_time: invalid value", setting="nokey"), **settings)
        self.assertNotIn("field", answer[1]["error"])
        answer = self.fails(failure("invalid_setting", "reset_time: invalid value"), **settings)
        self.assertNotIn("field", answer[1]["error"])
        answer = self.fails(failure("invalid_setting", "x", setting=["reset_time"]), **settings)
        self.assertNotIn("field", answer[1]["error"])

    def test_memory_limit_refusals(self):
        # out of range (set-memory, create) names the field; a paused VM must be started first
        for method, path, body in (("PUT", "/api/v1/vms/work/memory", {"memory_mib": 512}),
                                   ("POST", "/api/v1/vms", {"name": "lab", "memory_mib": 512})):
            with self.subTest(path=path):
                message = "the memory limit (MiB) must be a whole number from 1024 to 64000"
                answer = self.fails(failure("invalid_memory", message, value="512"), 1, method=method, path=path,
                                    body=body)
                self.assertError(answer, 400, "invalid_memory")
                self.assertEqual(answer[1]["error"]["field"], "memory_mib")
                self.assertEqual(answer[1]["error"]["detail"], message)
        answer = self.fails(failure("vm_paused", "work is paused", vm="work"), 1, method="PUT",
                            path="/api/v1/vms/work/memory", body={"memory_mib": 16384})
        self.assertError(answer, 409, "vm_paused")
        self.assertNotIn("field", answer[1]["error"])
        # saved, but libvirt refused the definition: the UI says that regenerate finishes it
        answer = self.fails(failure("memory_not_applied", "work: memory limit saved, but not applied", vm="work"),
                            1, method="PUT", path="/api/v1/vms/work/memory", body={"memory_mib": 16384})
        self.assertError(answer, 500, "memory_not_applied")

    def test_vm_variable_refusals_name_the_variable(self):
        # the UI says which variable a Safeguard or Agent Box needs, or which is too long
        env = dict(method="PUT", path="/api/v1/vms/work/env", body={"env": {"HTTPS_PROXY": ""}})
        answer = self.fails(failure("env_name_reserved", "HTTPS_PROXY cannot be a VM variable", name="HTTPS_PROXY"),
                            **env)
        self.assertError(answer, 400, "env_name_reserved")
        self.assertEqual((answer[1]["error"]["field"], answer[1]["error"]["params"]), ("env", {"name": "HTTPS_PROXY"}))
        answer = self.fails(failure("env_too_large", "the value of BIG is 5000 bytes long", name="BIG",
                                    max_bytes=4096), **env)
        self.assertEqual((answer[0], answer[1]["error"]["params"]), (400, {"name": "BIG"}))
        answer = self.fails(failure("env_too_large", "65 VM variables", max_variables=64), **env)
        self.assertNotIn("params", answer[1]["error"])
        for error in ("invalid_env_name", "invalid_env_value"):
            with self.subTest(error=error):
                self.assertError(self.fails(failure(error, "x"), **env), 400, error)
        # a name only in the form of one
        answer = self.fails(failure("env_name_reserved", "x", name="<b>x</b>"), **env)
        self.assertNotIn("params", answer[1]["error"])

    def test_long_details_are_cut(self):
        answer = self.fails(failure("action_failed", "x" * 2000))
        self.assertEqual(answer[1]["error"]["detail"], "x" * 500)

    def test_unreadable_answer(self):
        self.respond("not JSON")
        self.assertError(self.request("GET", "/api/v1/vms"), 502, "bad_agentbox_answer")


class ErrorTableTest(unittest.TestCase):
    """The codes agentbox raises, the web service's table, the UI's translations and the
    API docs name the same codes."""

    @classmethod
    def setUpClass(cls):
        cls.web = server_module()

    def raised_codes(self):
        """The codes of every die() and Fail() in agentbox/: each has error=, a code (or a
        choice of codes) or one passed on from a failure it caught."""
        def literal(expr):
            if isinstance(expr, ast.Constant):
                return {expr.value}
            if isinstance(expr, ast.IfExp):
                return literal(expr.body) | literal(expr.orelse)
            return set()
        codes = set()
        folder = os.path.join(ROOT, "agentbox")
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py") or name == "errors.py":
                continue
            with open(os.path.join(folder, name)) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in ("die", "Fail")):
                    continue
                with self.subTest(module=name, line=node.lineno):
                    error = [k.value for k in node.keywords if k.arg == "error"]
                    self.assertEqual(len(error), 1, "no error= code")
                    codes |= literal(error[0])
        return codes

    def test_the_name_of_a_vm_variable_is_the_same_everywhere(self):
        sys.path.insert(0, ROOT)
        from agentbox import vmenv
        for name in ("A", "_", "a_B9", "A" * 64, "A" * 65, "9A", "A-B", "A=B", "--x", "", "\u00c4"):
            with self.subTest(name=name):
                self.assertEqual(bool(self.web.ENV_NAME.fullmatch(name)),
                                 bool(vmenv.NAME.fullmatch(name)) and len(name) <= vmenv.MAX_NAME)

    def test_every_code_agentbox_raises_has_a_status(self):
        raised = self.raised_codes()
        self.assertIn("vm_not_found", raised)
        self.assertEqual(raised - set(self.web.AGENTBOX_ERRORS), set())
        self.assertLessEqual(set(self.web.AGENTBOX_EXITS.values()), set(self.web.AGENTBOX_ERRORS))
        for code, (status, message) in self.web.AGENTBOX_ERRORS.items():
            with self.subTest(code=code):
                self.assertRegex(code, r"^[a-z][a-z_]*$")
                self.assertIn(status, (400, 404, 409, 422, 500, 501, 502, 503, 504))
                self.assertRegex(message, r"^[A-Z][\x20-\x7e]*$")

    def test_the_ui_translates_every_code_and_the_docs_name_it(self):
        with open(os.path.join(ROOT, "docs", "control-room-api.md")) as f:
            docs = f.read()
        for language in ("de", "en"):
            with open(os.path.join(ROOT, "web", "ui", "i18n", "locales", f"{language}.ts")) as f:
                block = re.search(r"\n  errors: \{\n(.*?)\n  \},", f.read(), re.S).group(1)
            translated = set(re.findall(r"^    ([a-z_]+): ", block, re.M))
            with self.subTest(language=language):
                self.assertEqual(set(self.web.AGENTBOX_ERRORS) - translated, set())
        for code in self.web.AGENTBOX_ERRORS:
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", docs)

    def test_every_call_of_agentbox_names_its_code(self):
        # run() reads the code from agentbox's --json failure, so every call passes --json
        with open(SERVER) as f:
            tree = ast.parse(f.read())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id in ("run", "run_json")]
        self.assertGreater(len(calls), 40)
        for call in calls:
            if all(isinstance(a, ast.Starred) for a in call.args):
                continue                    # run_json passing its arguments on
            with self.subTest(line=call.lineno):
                self.assertTrue(any(isinstance(a, ast.Constant) and a.value == "--json" for a in call.args))


# --- secrets ------------------------------------------------------------------------------

class SecretTest(V1Test):
    def setUp(self):
        super().setUp()
        self.login()

    def test_claude_token_only_on_stdin(self):
        self.respond({"vm": "work", "stored": True, "length": len(CLAUDE)})
        status, obj, _ = self.request("PUT", "/api/v1/vms/work/claude-token", {"token": CLAUDE})
        self.assertEqual((status, obj), (200, {"vm": "work", "stored": True, "length": len(CLAUDE)}))
        self.assertEqual(self.calls(), [["set-claude-token", "work", "--no-prompt", "--json"]])
        self.assertEqual(self.stdin(), [CLAUDE])
        # agentbox echoing it in an error: never back, never logged
        self.respond("", stderr=failure("claude_token_rejected", f"Anthropic rejects the token: {CLAUDE}",
                                        vm="work", token=CLAUDE), exitcode=1)
        answer = self.request("PUT", "/api/v1/vms/work/claude-token", {"token": CLAUDE})
        self.assertError(answer, 422, "claude_token_rejected")
        self.assertEqual(answer[1]["error"]["detail"], "Anthropic rejects the token: [hidden]")
        self.assertNotIn(CLAUDE, json.dumps(answer[1]))
        self.assertNotIn(CLAUDE, self.server_log())
        self.assertNotIn(CLAUDE, json.dumps(self.journald.entries()))

    def test_github_app_key_only_on_stdin(self):
        self.respond("", stderr=failure("invalid_private_key", f"this is not an RSA key: {PEM.splitlines()[1]}"),
                     exitcode=1)
        answer = self.request("PUT", "/api/v1/github-app", {"app_id": 123456, "private_key": PEM})
        self.assertError(answer, 400, "invalid_private_key")
        self.assertEqual(self.calls(), [["set-github-app", "123456", "--no-prompt", "--json"]])
        self.assertEqual(self.stdin(), [PEM])
        for line in PEM.splitlines()[1:2]:
            self.assertNotIn(line, json.dumps(answer[1]))
            self.assertNotIn(line, self.server_log())

    def test_pairing_code_goes_only_to_the_browser(self):
        self.respond({"vm": "work", "code": CODE})
        status, obj, _ = self.request("POST", "/api/v1/vms/work/pairing-code", {"confirm": True})
        self.assertEqual((status, obj), (200, {"vm": "work", "code": CODE}))
        self.assertEqual(self.calls(), [["pair", "work", "--json"]])
        self.assertNotIn("Qq7_secret", self.server_log())


# --- live events from agentbox watch -----------------------------------------------------------

STATE = {"vms": [vm("work"), vm("priv", "paused")], "problems": []}


class EventsTest(V1Test):
    WATCH = [("vms", STATE), ("services", {"units": [{"unit": "ollama.service", "active": "active"}]}),
             ("host-reboot", {"needed": False, "packages": [], "checked": "2026-11-03T12:00:00Z"})]

    def setUp(self):
        super().setUp()
        self.login()

    def until_state(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            s = self.stream()
            blocks = [s.block() for _ in range(6)]
            s.close()
            if any(b.get("event") == "state" for b in blocks):
                return blocks
            time.sleep(0.1)
        self.fail("no state")

    def test_connect_gets_hello_then_the_current_state(self):
        blocks = self.until_state()
        self.assertEqual(blocks[0]["event"], "hello")
        names = [b["event"] for b in blocks[1:]]
        self.assertEqual([n for n in names if n != "host"], ["version", "watch", "state", "host-reboot", "services"])
        by = {b["event"]: json.loads(b["data"]) for b in blocks[1:]}
        self.assertEqual(by["state"], STATE)
        self.assertEqual(by["watch"]["running"], True)
        self.assertEqual(by["host-reboot"]["needed"], False)
        self.assertEqual(by["version"]["ui"], "nuxt")
        ids = [b["id"] for b in blocks[1:]]
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{8}-\d+", i) for i in ids))

    def test_old_stream_carries_them_too(self):
        self.until_state()
        s = self.stream(path="/api/events")
        self.assertEqual(s.block()["event"], "hello")
        s.event("state")

    def test_vm_changes_are_events_and_update_the_snapshot(self):
        self.until_state()
        s = self.stream()
        s.event("state")
        self.watch_lines(("vm", vm("work", "stopped")))
        changed = s.event("vm")
        self.assertEqual(json.loads(changed["data"])["state"], "stopped")
        # a new connection gets the changed list
        fresh = self.stream()
        self.assertEqual([v["state"] for v in json.loads(fresh.event("state")["data"])["vms"]], ["stopped", "paused"])
        # and one that reconnects gets what it missed
        s.close()
        self.watch_lines(("vm", vm("priv", "running")))
        fresh.event("vm")
        again = self.stream(last_id=changed["id"])
        self.assertEqual(json.loads(again.block()["data"])["resumed"], True)
        self.assertEqual(json.loads(again.event("vm")["data"])["name"], "priv")

    def test_forwarded_events(self):
        self.until_state()
        s = self.stream()
        s.event("state")
        activity = {"time": "2026-11-03T12:00:01Z", "vm": "work", "actor": "x", "kind": "action", "action": "reset",
                    "outcome": "failed", "description": "Reset without start: interrupted", "code": "reset",
                    "params": {"no_start": "true"}, "error": "interrupted", "error_params": {}}
        log = {"unit": "agentbox-build@base.service", "target": "base", "image": "base", "build": "1", "entries": [],
               "cursor": None, "truncated": False}
        self.watch_lines(("activity", activity), ("build-log", log), ("unknown-later", {"x": 1}))
        self.assertEqual(json.loads(s.event("activity")["data"]), activity)
        self.assertEqual(json.loads(s.event("build-log")["data"]), log)

    def test_notifications(self):
        self.until_state()
        s = self.stream()
        s.event("state")
        self.watch_lines(
            ("alert", {"time": "t", "vm": "work", "domain": "paste.example:443", "signals": ["paste", "young"]}),
            ("github-token", {"time": "t", "vm": "work", "action": "issued", "repository": "a/b", "request": "a/b"}),
            ("github-token", {"time": "t", "vm": "work", "action": "refused", "request": "evil/repo",
                              "reason": "Owner evil nicht erlaubt"}),
            ("build", {"image": "base", "build": "20261103-120000", "status": "running", "step": "build"}),
            ("build", {"image": "rust", "build": "20261103-130000", "status": "failed", "error": "podman"}),
            ("build", {"image": "base", "build": "20261103-120000", "status": "rejected", "error": "npm"}),
            ("host-reboot", {"needed": True, "packages": ["kernel-core"], "checked": "t"}),
            ("host-reboot", {"needed": True, "packages": ["kernel-core"], "checked": "t2"}))
        got = [json.loads(s.event("notification")["data"]) for _ in range(5)]
        self.assertEqual([(n["kind"], n["severity"], n["vm"]) for n in got], [
            ("alert", "critical", "work"), ("github-refused", "warning", "work"), ("build-failed", "warning", None),
            ("build-rejected", "critical", None), ("host-reboot", "warning", None)])
        self.assertEqual(got[0]["text"], "work tried to reach paste.example:443 (paste, young)")
        self.assertEqual(got[1]["text"], "work: GitHub token refused for evil/repo")
        self.assertEqual(got[1]["detail"], "Owner evil nicht erlaubt")
        self.assertEqual(got[2]["text"], "Build rust-20261103-130000 failed; the previous Build stays in use")
        for n in got:
            self.assertRegex(n["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        # the second host-reboot (still needed) is no new notification
        later = [b for b in (s.block() for _ in range(8)) if b.get("event") == "notification"]
        self.assertEqual(later, [])

    def test_system(self):
        self.until_state()
        status, obj, _ = self.request("GET", "/api/v1/system")
        self.assertEqual(status, 200)
        self.assertEqual(obj["watch"]["running"], True)
        self.assertEqual(obj["host_reboot"]["needed"], False)
        self.assertEqual(obj["services"], [{"unit": "ollama.service", "active": "active"}])
        self.assertEqual(obj["ui"]["ui"], "nuxt")

    def test_one_watch_for_every_client_and_never_a_call(self):
        self.until_state()
        for _ in range(3):
            self.stream().event("state")
        self.assertEqual(self.watch_starts(), 1)
        self.assertEqual(self.calls(), [])


class WatchRestartTest(V1Test):
    WATCH = [("vms", STATE)]

    def test_a_watch_that_ends_is_started_again(self):
        self.write("watch.json", json.dumps({"exit": 1, "stderr": "agentbox: journalctl is missing"}))
        self.login()
        # the first watch blocks; ending it (as if it died) brings the next one
        deadline = time.monotonic() + 15
        while self.watch_starts() < 3:
            self.assertLess(time.monotonic(), deadline, self.server_log())
            # only this server's watch
            subprocess.run(["pkill", "-P", str(self.server.pid), "-f", "fake_agentbox.py watch"], capture_output=True)
            time.sleep(0.1)
        s = self.stream()
        status = json.loads(s.event("watch")["data"])
        self.assertGreaterEqual(status["restarts"], 2)
        self.assertEqual(status["last_exit"], 1)
        self.assertIn("agentbox watch ended (exit 1)", self.server_log())
        self.assertIn("agentbox watch: agentbox: journalctl is missing", self.server_log())
        # a watch that stays up again
        os.remove(os.path.join(self.dir, "watch.json"))
        deadline = time.monotonic() + 15
        while True:
            _, obj, _ = self.request("GET", "/api/v1/system")
            if obj["watch"]["running"]:
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.1)


# --- temperature ----------------------------------------------------------------------------

class TemperatureTest(V1Test):
    ENV = {"AGENTBOX_WEB_SAMPLE_SECONDS": "0.2"}

    def test_a_temperature_over_its_threshold_notifies_once(self):
        self.login()
        s = self.stream()
        s.event("host")
        self.write("sys/class/hwmon/hwmon4/temp1_input", "91000\n")
        n = json.loads(s.event("notification")["data"])
        self.assertEqual({k: n[k] for k in ("kind", "severity", "reading", "value", "threshold")},
                         {"kind": "temperature", "severity": "warning", "reading": "cpu.temp", "value": 91.0,
                          "threshold": 85})
        self.assertEqual(n["text"], "CPU temperature is 91 °C (threshold 85 °C)")
        blocks = [s.block() for _ in range(8)]
        self.assertNotIn("notification", [b.get("event") for b in blocks])
        # below and above again: a new one
        self.write("sys/class/hwmon/hwmon4/temp1_input", "50000\n")
        time.sleep(0.6)
        self.write("sys/class/hwmon/hwmon4/temp1_input", "90000\n")
        self.assertEqual(json.loads(s.event("notification", limit=200)["data"])["value"], 90.0)


# --- the UI -----------------------------------------------------------------------------------

class UiTest(V1Test):
    def get(self, path, headers=None):
        return self.request("GET", path, header=False, raw=True, headers=headers)

    def index(self):
        with open(os.path.join(self.ui, "index.html")) as f:
            return f.read()

    def test_index_with_csp_hashes_of_its_inline_scripts(self):
        status, body, resp = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), self.index())
        self.assertEqual(resp.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-cache")
        html = self.index()
        color = re.search(r"<script>(\"use strict\".*?)</script>", html, re.S).group(1)
        config = re.search(r"<script>(window\.__NUXT__.*?)</script>", html, re.S).group(1)
        style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        csp = resp.getheader("Content-Security-Policy")
        self.assertEqual(csp, "default-src 'self'; style-src 'self' " + sha256(style) + "; script-src 'self' "
                         + " ".join(sorted([sha256(color), sha256(config)])) + "; img-src 'self' data:; "
                         "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                         "form-action 'none'")
        self.assertNotIn("unsafe", csp)

    def test_spa_fallback(self):
        for path in ("/vms/work", "/vms/work/log", "/settings?tab=x", "/allowlist/"):
            with self.subTest(path=path):
                status, body, resp = self.get(path)
                self.assertEqual((status, body.decode()), (200, self.index()))
                self.assertEqual(resp.getheader("Cache-Control"), "no-cache")

    def test_assets(self):
        cases = [("/_nuxt/entry.Bx3k9Q.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
                 ("/_nuxt/entry.Dk2a1.css", "text/css; charset=utf-8", "public, max-age=31536000, immutable"),
                 ("/_fonts/inter-latin.woff2", "font/woff2", "public, max-age=31536000, immutable"),
                 ("/favicon.ico", "image/x-icon", "no-cache")]
        for path, ctype, cache in cases:
            with self.subTest(path=path):
                status, body, resp = self.get(path)
                self.assertEqual(status, 200)
                with open(os.path.join(self.ui, path.lstrip("/")), "rb") as f:
                    self.assertEqual(body, f.read())
                self.assertEqual((resp.getheader("Content-Type"), resp.getheader("Cache-Control")), (ctype, cache))
                self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
                etag = resp.getheader("ETag")
                self.assertEqual(self.get(path, {"If-None-Match": etag})[0], 304)

    def test_missing_assets_are_404_not_html(self):
        for path in ("/_nuxt/gone.js", "/_fonts/x.woff2", "/robots.txt", "/x/../../etc/passwd", "/%2e%2e/key.pem",
                     "/_nuxt/..%2f..%2findex.html"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_api_paths_are_never_the_ui(self):
        self.assertEqual(self.request("GET", "/api/whatever")[:2], (401, {"error": "not signed in"}))
        self.assertError(self.request("GET", "/api/v1/whatever"), 401, "unauthenticated")

    def test_old_ui_stays_reachable(self):
        status, body, resp = self.get("/legacy/")
        with open(os.path.join(STATIC, "index.html"), "rb") as f:
            self.assertEqual(body, f.read())
        self.assertNotIn("sha256", resp.getheader("Content-Security-Policy"))
        self.assertEqual(self.get("/app.js")[2].getheader("Content-Type"), "text/javascript; charset=utf-8")

    def test_version_event_when_a_new_build_is_installed(self):
        self.login()
        s = self.stream()
        first = json.loads(s.event("version")["data"])
        self.assertEqual(first["version"], hashlib.sha256(self.index().encode()).hexdigest()[:16])
        self.assertEqual(self.request("GET", "/api/v1/version")[1]["version"], first["version"])
        # an update installs a new build: rpm writes each file under a temporary name and
        # renames it over the old one
        html = self.index().replace("placeholder-1", "placeholder-2").replace("light", "lite")
        with open(os.path.join(self.ui, "index.html;update"), "w") as f:
            f.write(html)
        os.replace(os.path.join(self.ui, "index.html;update"), os.path.join(self.ui, "index.html"))
        second = json.loads(s.event("version")["data"])
        self.assertNotEqual(second["version"], first["version"])
        self.assertEqual(second["version"], hashlib.sha256(html.encode()).hexdigest()[:16])
        # the new page and its CSP
        status, body, resp = self.get("/")
        self.assertEqual(body.decode(), html)
        color = re.search(r"<script>(\"use strict\".*?)</script>", html, re.S).group(1)
        self.assertIn(sha256(color), resp.getheader("Content-Security-Policy"))


class LegacyUiTest(V1Test):
    UI = False

    def test_without_a_new_build_the_old_ui(self):
        status, body, resp = self.request("GET", "/", header=False, raw=True)
        with open(os.path.join(STATIC, "index.html"), "rb") as f:
            self.assertEqual((status, body), (200, f.read()))
        self.assertEqual(resp.getheader("Content-Security-Policy"),
                         "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(self.request("GET", "/vms/work", header=False)[:2], (404, {"error": "not found"}))
        self.login()
        version = json.loads(self.stream().event("version")["data"])
        self.assertEqual(version["ui"], "legacy")
        self.assertTrue(version["version"].startswith("legacy-"))


if __name__ == "__main__":
    unittest.main()
