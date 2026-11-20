"""The Anthropic route of the token service (github-app/agentbox-claude-route, ADR 0023): what it
accepts, what it refuses, how a request goes upstream and how the answer comes back.

The route runs as it does on the Host, one process per connection with the connection as its
stdin, here one end of a socketpair the test talks HTTP on as the VM would. AGENTBOX_ROOT points at
a temporary root with the VM's placeholder and token files. Upstream is a local TLS server whose
certificate is for api.anthropic.com, signed by a CA made for the test; the route is pointed at it
and that CA only through AGENTBOX_CLAUDE_TEST_UPSTREAM and AGENTBOX_CLAUDE_TEST_CA, which it
honours only under AGENTBOX_ROOT. The name it verifies stays api.anthropic.com.

Run from the repo root: python3 -m unittest discover -s tests
"""
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTE = os.path.join(REPO, "github-app", "agentbox-claude-route")

TOKEN = b"sk-ant-oat01-Real_Owner-Token-" + b"x" * 70
OTHER_TOKEN = b"sk-ant-oat01-Second-Owner-Token-" + b"y" * 60
PLACEHOLDER = b"agentbox-claude-" + b"P" * 43
PROMPT = b"SECRET-PROMPT-TEXT"
BODY = json.dumps({"model": "claude-opus-5-5", "max_tokens": 10, "stream": True,
                   "messages": [{"role": "user", "content": PROMPT.decode()}]}).encode()
EVENT = b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"%d"}}\n\n'
LINE = re.compile(r"\[priv\] claude POST /v1/messages (\d{3}) model=(\S+) (\d+\.\d\d)s (\d+)B request-id=(\S+)( interrupted)?")

certs = None


def openssl(*args):
    subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=certs.name)


def make_ca(name):
    openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "2",
            "-keyout", f"{name}.key", "-out", f"{name}.pem", "-subj", f"/CN=Agent Box test {name}",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", "subjectKeyIdentifier=hash")


def setUpModule():
    """A CA, a certificate for api.anthropic.com it signed, and a second CA that signed nothing."""
    global certs
    certs = tempfile.TemporaryDirectory()
    make_ca("ca")
    make_ca("other-ca")
    with open(os.path.join(certs.name, "server.ext"), "w") as f:
        f.write("basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
                "subjectAltName=DNS:api.anthropic.com\nsubjectKeyIdentifier=hash\n"
                "authorityKeyIdentifier=keyid,issuer\n")
    openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "server.key",
            "-out", "server.csr", "-subj", "/CN=api.anthropic.com")
    openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-days", "2", "-out", "server.pem", "-extfile", "server.ext")


def tearDownModule():
    certs.cleanup()


def cert(name):
    return os.path.join(certs.name, name)


class Upstream:
    """api.anthropic.com as the route reaches it in these tests: TLS with the test CA's certificate,
    one request per connection, recorded as (method, target, [(name, value)], body); `answer` writes
    the answer to the connection."""

    def __init__(self):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert("server.pem"), cert("server.key"))
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.requests, self.connections = [], 0
        self.gate = threading.Event()
        self.answer = self.json_answer
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        self.gate.set()
        self.listener.close()

    def serve(self):
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.handle, args=(raw,), daemon=True).start()

    def handle(self, raw):
        try:
            with self.context.wrap_socket(raw, server_side=True) as conn:
                conn.settimeout(20)
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                head, _, body = data.partition(b"\r\n\r\n")
                lines = head.split(b"\r\n")
                method, target, _ = lines[0].decode().split(" ")
                headers = [(n.decode().lower(), v.strip()) for n, _, v in (l.partition(b":") for l in lines[1:])]
                length = int(dict(headers).get("content-length", b"0"))
                while len(body) < length:
                    body += conn.recv(65536)
                self.requests.append((method, target, headers, body))
                self.answer(conn)
        except (OSError, ssl.SSLError, ValueError):
            pass

    @staticmethod
    def chunk(data):
        return b"%x\r\n" % len(data) + data + b"\r\n"

    def json_answer(self, conn):
        body = b'{"type":"message","content":[{"type":"text","text":"Hi"}]}'
        conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nrequest-id: req_011json\r\n"
                     b"content-length: %d\r\n\r\n" % len(body) + body)

    def stream_answer(self, conn):
        """An event stream: one event, then the next only once the test opened the gate."""
        conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nrequest-id: req_011stream\r\n"
                     b"transfer-encoding: chunked\r\n\r\n" + self.chunk(EVENT % 1))
        self.gate.wait(20)
        conn.sendall(self.chunk(EVENT % 2) + b"0\r\n\r\n")


class RouteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        os.makedirs(self.path("etc/agentbox/claude"))
        self.write("placeholder-priv", PLACEHOLDER + b"\n")
        self.write("token-priv", TOKEN + b"\n")
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)
        self.routes = []
        self.addCleanup(self.stop_routes)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, name, data):
        """A file of <CONF>/claude, replaced in one rename as agentbox does."""
        tmp = self.path(f"etc/agentbox/claude/.{name}")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self.path(f"etc/agentbox/claude/{name}"))

    def env(self, ca="ca.pem", port=None, request_seconds=None):
        extra = {"AGENTBOX_CLAUDE_TEST_REQUEST_SECONDS": str(request_seconds)} if request_seconds else {}
        return {**extra, "PATH": os.environ["PATH"], "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8",
                "AGENTBOX_CLAUDE_TEST_UPSTREAM": f"127.0.0.1:{port or self.upstream.port}",
                "AGENTBOX_CLAUDE_TEST_CA": cert(ca),
                # no proxy is taken from the environment: these would lead nowhere
                "HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"}

    def route(self, vm="priv", **env):
        """A connection to the route: the VM's end of it."""
        ours, theirs = socket.socketpair()
        proc = subprocess.Popen([sys.executable, "-I", ROUTE, vm], stdin=theirs, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env=self.env(**env))
        theirs.close()
        ours.settimeout(30)
        conn = Connection(ours, proc)
        self.routes.append(conn)
        return conn

    def stop_routes(self):
        for conn in self.routes:
            conn.finish()

    def request(self, method="POST", target="/v1/messages?beta=true", headers=None, body=BODY, auth=PLACEHOLDER):
        lines = [f"{method} {target} HTTP/1.1".encode(), b"Host: host.agentbox:9101"]
        if auth is not None:
            lines.append(b"Authorization: Bearer " + auth)
        lines += headers if headers is not None else [b"content-type: application/json",
                                                        b"anthropic-version: 2023-06-01",
                                                        b"anthropic-beta: oauth-2025-04-20,claude-code-20250219"]
        if body is not None and not any(h.lower().startswith((b"content-length:", b"transfer-encoding:"))
                                        for h in lines):
            lines.append(b"Content-Length: %d" % len(body))
        return b"\r\n".join(lines) + b"\r\n\r\n" + (body or b"")

    def ask(self, raw, **env):
        """One request on a new connection: (status, headers, body, the route's journal lines)."""
        conn = self.route(**env)
        conn.send(raw)
        status, headers, body = conn.response()
        return status, headers, body, conn.finish()

    def assert_refused(self, raw, status, reason, **env):
        got, headers, body, log = self.ask(raw, **env)
        self.assertEqual(got, status, body)
        self.assertEqual(json.loads(body)["type"], "error")
        self.assertIn(("connection", "close"), headers)
        self.assertEqual(len(log), 1, log)
        self.assertRegex(log[0], rf"^\[priv\] refused: {reason}( [a-z]+=\S+)*: \S")
        self.assertEqual(self.upstream.requests, [])
        self.assert_quiet(log)
        return json.loads(body)["error"]["message"], log[0]

    def assert_quiet(self, log):
        """The journal lines carry neither the token, the placeholder, a header value nor a body."""
        text = "\n".join(log)
        for secret in (TOKEN, OTHER_TOKEN, PLACEHOLDER, PROMPT, b"oauth-2025-04-20", b"claude-code-20250219",
                       b"session-4711"):
            self.assertNotIn(secret.decode(), text)


class Connection:
    """The VM's end of one connection to the route, reading HTTP/1.1 answers piece by piece."""

    def __init__(self, sock, proc):
        self.sock, self.proc, self.buf, self.log = sock, proc, b"", None

    def send(self, data):
        self.sock.sendall(data)

    def _more(self):
        data = self.sock.recv(65536)
        if not data:
            raise EOFError("the route closed the connection")
        self.buf += data

    def _line(self):
        while b"\r\n" not in self.buf:
            self._more()
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _exactly(self, n):
        while len(self.buf) < n:
            self._more()
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def head(self):
        status = int(self._line().split(b" ")[1])
        headers = []
        while True:
            line = self._line()
            if not line:
                return status, headers
            name, _, value = line.partition(b":")
            headers.append((name.decode().lower(), value.strip().decode("latin-1")))

    def chunk(self):
        """The next chunk of a chunked body, b"" at its end."""
        n = int(self._line(), 16)
        data = self._exactly(n)
        self._exactly(2)
        return data

    def response(self):
        status, headers = self.head()
        names = dict(headers)
        if names.get("transfer-encoding") == "chunked":
            body = b""
            while data := self.chunk():
                body += data
        else:
            body = self._exactly(int(names.get("content-length", "0")))
        return status, headers, body

    def closed(self):
        """Whether the route closed its side (EOF) rather than waiting for another request."""
        try:
            self.sock.settimeout(5)
            return self.sock.recv(1) == b""
        except socket.timeout:
            return False

    def finish(self):
        """The route's journal lines, once it ended."""
        if self.log is None:
            self.sock.close()
            _, err = self.proc.communicate(timeout=30)
            self.log = err.decode().splitlines()
        return self.log


class ForwardTest(RouteTest):
    def test_forwards_with_the_real_token_and_only_the_headers_claude_code_needs(self):
        headers = [b"content-type: application/json", b"anthropic-version: 2023-06-01",
                   b"anthropic-beta: oauth-2025-04-20,claude-code-20250219", b"x-claude-code-session-id: session-4711",
                   b"X-Stainless-Lang: js", b"User-Agent: claude-cli/2.1.328", b"x-app: cli", b"Accept: application/json",
                   b"Cookie: a=b", b"Proxy-Authorization: Basic eA==", b"Forwarded: for=10.0.0.1",
                   b"X-Forwarded-For: 10.0.0.1", b"Accept-Encoding: gzip, br", b"Keep-Alive: timeout=5",
                   b"TE: trailers", b"X-Other: something"]
        status, answer, body, log = self.ask(self.request(headers=headers))
        self.assertEqual((status, body), (200, b'{"type":"message","content":[{"type":"text","text":"Hi"}]}'))
        [(method, target, sent, data)] = self.upstream.requests
        self.assertEqual((method, target, data), ("POST", "/v1/messages?beta=true", BODY))
        got = dict(sent)
        self.assertEqual(got["authorization"], b"Bearer " + TOKEN)
        self.assertEqual(got["host"], b"api.anthropic.com")
        self.assertEqual(got["anthropic-beta"], b"oauth-2025-04-20,claude-code-20250219")
        self.assertEqual(got["accept-encoding"], b"identity")
        for name in ("anthropic-version", "x-claude-code-session-id", "x-stainless-lang", "user-agent", "x-app",
                     "accept", "content-type"):
            self.assertIn(name, got)
        self.assertEqual(sorted(n for n, _ in sent),
                         sorted(["host", "accept-encoding", "authorization", "content-type", "anthropic-version",
                                 "anthropic-beta", "x-claude-code-session-id", "x-stainless-lang", "user-agent",
                                 "x-app", "accept", "content-length"]))
        self.assertNotIn(PLACEHOLDER, b"".join(v for _, v in sent))
        self.assertEqual(log, [log[0]])
        m = LINE.fullmatch(log[0])
        self.assertTrue(m, log)
        self.assertEqual((m.group(1), m.group(2), m.group(4), m.group(5), m.group(6)),
                         ("200", "claude-opus-5-5", str(len(body)), "req_011json", None))
        self.assert_quiet(log)

    def test_count_tokens_and_a_chunked_request_body(self):
        chunked = b"5\r\n" + BODY[:5] + b"\r\n" + b"%x\r\n" % (len(BODY) - 5) + BODY[5:] + b"\r\n0\r\n\r\n"
        raw = self.request(target="/v1/messages/count_tokens?beta=true",
                           headers=[b"content-type: application/json", b"Transfer-Encoding: chunked"], body=None)
        status, _, _, log = self.ask(raw + chunked)
        self.assertEqual(status, 200)
        [(method, target, sent, data)] = self.upstream.requests
        self.assertEqual((method, target, data), ("POST", "/v1/messages/count_tokens?beta=true", BODY))
        self.assertEqual(dict(sent)["content-length"], str(len(BODY)).encode())
        self.assertNotIn("transfer-encoding", dict(sent))
        self.assertRegex(log[0], r"^\[priv\] claude POST /v1/messages/count_tokens 200 model=claude-opus-5-5 ")

    def test_only_the_answer_headers_claude_code_reads_come_back_and_no_redirect_is_followed(self):
        def answer(conn):
            conn.sendall(b"HTTP/1.1 307 Temporary Redirect\r\nLocation: https://elsewhere.example/\r\n"
                         b"Set-Cookie: s=1\r\ncontent-type: application/json\r\nrequest-id: req_x\r\n"
                         b"retry-after: 3\r\nx-should-retry: false\r\n"
                         b"anthropic-ratelimit-unified-status: allowed\r\nX-Other: 1\r\ncontent-length: 2\r\n\r\n{}")
        self.upstream.answer = answer
        status, headers, body, log = self.ask(self.request())
        self.assertEqual((status, body), (307, b"{}"))
        self.assertEqual(sorted(n for n, _ in headers),
                         ["anthropic-ratelimit-unified-status", "content-type", "request-id", "retry-after",
                          "transfer-encoding", "x-should-retry"])
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertRegex(log[0], r" 307 model=")

    def test_anthropics_errors_pass_through_unchanged(self):
        error = b'{"type":"error","error":{"type":"invalid_request_error","message":"thinking is not supported"}}'

        def answer(conn):
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\ncontent-type: application/json\r\nrequest-id: req_e\r\n"
                         b"content-length: %d\r\n\r\n" % len(error) + error)
        self.upstream.answer = answer
        status, _, body, log = self.ask(self.request())
        self.assertEqual((status, body), (400, error))
        self.assertTrue(LINE.fullmatch(log[0]), log)

    def test_hello_is_answered_here_and_not_logged(self):
        status, headers, body, log = self.ask(self.request(method="HEAD", target="/api/hello", headers=[], body=None))
        self.assertEqual((status, body, log), (204, b"", []))
        self.assertEqual(self.upstream.connections, 0)

    def test_one_connection_serves_request_after_request(self):
        conn = self.route()
        for _ in range(3):
            conn.send(self.request())
            self.assertEqual(conn.response()[0], 200)
        self.assertEqual(len(self.upstream.requests), 3)
        self.assertEqual(len(conn.finish()), 3)


class StreamTest(RouteTest):
    def setUp(self):
        super().setUp()
        self.upstream.answer = self.upstream.stream_answer

    def test_an_event_stream_reaches_the_vm_event_by_event(self):
        conn = self.route()
        conn.send(self.request())
        status, headers = conn.head()
        self.assertEqual((status, dict(headers)["content-type"]), (200, "text/event-stream"))
        # the first event arrives while Anthropic still holds back the second
        self.assertEqual(conn.chunk(), EVENT % 1)
        self.assertFalse(self.upstream.gate.is_set())
        self.upstream.gate.set()
        rest = b""
        while data := conn.chunk():
            rest += data
        self.assertEqual(rest, EVENT % 2)
        log = conn.finish()
        m = LINE.fullmatch(log[0])
        self.assertTrue(m, log)
        self.assertEqual((m.group(4), m.group(5)), (str(2 * len(EVENT % 1)), "req_011stream"))

    def test_a_stream_the_vm_leaves_is_logged_as_interrupted(self):
        conn = self.route()
        conn.send(self.request())
        conn.head()
        conn.chunk()
        conn.sock.close()
        self.upstream.gate.set()
        _, err = conn.proc.communicate(timeout=30)
        conn.log = err.decode().splitlines()
        m = LINE.fullmatch(conn.log[0])
        self.assertTrue(m, conn.log)
        self.assertEqual(m.group(6), " interrupted")


class RedactTest(RouteTest):
    def test_the_token_is_taken_out_of_headers_and_body_also_across_chunks(self):
        def answer(conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nrequest-id: req_r\r\n"
                         b"anthropic-ratelimit-unified-echo: a" + TOKEN + b"b\r\ntransfer-encoding: chunked\r\n\r\n")
            # the token whole, then split across three pieces, each sent and given time to arrive alone
            for piece in (b"data: " + TOKEN + b"\n\n", b"data: x" + TOKEN[:9], TOKEN[9:40], TOKEN[40:] + b"y\n\n",
                          b"data: s"):
                conn.sendall(Upstream.chunk(piece))
                time.sleep(0.2)
            conn.sendall(b"0\r\n\r\n")
        self.upstream.answer = answer
        status, headers, body, log = self.ask(self.request())
        self.assertEqual(status, 200)
        self.assertEqual(dict(headers)["anthropic-ratelimit-unified-echo"], "a[redacted]b")
        self.assertEqual(body, b"data: [redacted]\n\ndata: x[redacted]y\n\ndata: s")
        self.assertNotIn(TOKEN, body)

    def test_redactor_at_every_split(self):
        route = load_route()
        stream = b"data: " + TOKEN + b" and sk-ant-" + b" and " + TOKEN + b"!"
        for size in range(1, len(stream) + 1):
            for start in range(0, size):
                with self.subTest(size=size, start=start):
                    pieces = [stream[:start]] + [stream[i:i + size] for i in range(start, len(stream), size)]
                    redactor = route.Redactor(TOKEN)
                    out = b"".join(redactor.feed(p) for p in pieces if p) + redactor.flush()
                    self.assertEqual(out, b"data: [redacted] and sk-ant- and [redacted]!")
            if size > 12:
                break


class TokenFileTest(RouteTest):
    def test_the_token_file_is_read_at_each_request(self):
        conn = self.route()
        conn.send(self.request())
        self.assertEqual(conn.response()[0], 200)
        self.write("token-priv", OTHER_TOKEN + b"\n")             # agentbox set-claude-token
        conn.send(self.request())
        self.assertEqual(conn.response()[0], 200)
        self.write("token-priv", b"none\n")                      # agentbox remove-claude-token
        conn.send(self.request())
        status, _, body = conn.response()
        self.assertEqual(status, 401)
        self.assertIn("agentbox set-claude-token priv", json.loads(body)["error"]["message"])
        self.assertTrue(conn.closed())
        self.assertEqual([dict(h)["authorization"] for _, _, h, _ in self.upstream.requests],
                         [b"Bearer " + TOKEN, b"Bearer " + OTHER_TOKEN])
        log = conn.finish()
        self.assertEqual(len(log), 3)
        self.assertRegex(log[2], r"^\[priv\] refused: no-token: ")
        self.assert_quiet(log)

    def test_a_running_stream_finishes_with_the_token_it_started_with(self):
        self.upstream.answer = self.upstream.stream_answer
        conn = self.route()
        conn.send(self.request())
        conn.head()
        self.assertEqual(conn.chunk(), EVENT % 1)
        self.write("token-priv", b"none\n")
        self.upstream.gate.set()
        self.assertEqual(conn.chunk(), EVENT % 2)
        self.assertEqual(conn.chunk(), b"")
        self.assertEqual(dict(self.upstream.requests[0][2])["authorization"], b"Bearer " + TOKEN)
        self.assertTrue(LINE.fullmatch(conn.finish()[0]))

    def test_no_token_stored(self):
        self.write("token-priv", b"none\n")
        message, line = self.assert_refused(self.request(), 401, "no-token")
        self.assertIn("agentbox set-claude-token priv", message)


class RefusalTest(RouteTest):
    def test_without_the_vms_placeholder(self):
        for auth in (None, b"sk-ant-oat01-a-login-of-someone", PLACEHOLDER[:-1], PLACEHOLDER + b"x",
                     b"agentbox-claude-" + b"Q" * 43):
            with self.subTest(auth=auth):
                self.upstream.requests.clear()
                self.assert_refused(self.request(auth=auth), 401, "wrong-placeholder")
        # the right placeholder in another scheme, or with an API key besides
        self.assert_refused(self.request(auth=None, headers=[b"Authorization: Basic " + PLACEHOLDER]), 401,
                            "wrong-placeholder")
        self.assert_refused(self.request(headers=[b"x-api-key: sk-ant-api03-abc"]), 401, "wrong-placeholder")

    def test_without_a_placeholder_file_nothing_is_accepted(self):
        os.remove(self.path("etc/agentbox/claude/placeholder-priv"))
        self.assert_refused(self.request(), 401, "wrong-placeholder")

    def test_another_vms_placeholder(self):
        self.write("placeholder-work", b"agentbox-claude-" + b"W" * 43 + b"\n")
        self.assert_refused(self.request(auth=b"agentbox-claude-" + b"W" * 43), 401, "wrong-placeholder")

    def test_other_paths_and_methods(self):
        for method, target in (("GET", "/v1/models?limit=1000"), ("POST", "/v1/complete"), ("GET", "/v1/messages"),
                               ("POST", "/v1/messages/batches"), ("POST", "/api/oauth/usage"), ("HEAD", "/v1/messages"),
                               ("DELETE", "/v1/messages")):
            with self.subTest(method=method, target=target):
                _, line = self.assert_refused(self.request(method=method, target=target), 404, "path-not-allowed")
                self.assertIn(f" method={method} path={target.split('?')[0]}: ", line)

    def test_tunnels_and_upgrades(self):
        self.assert_refused(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n"
                            b"Authorization: Bearer " + PLACEHOLDER + b"\r\n\r\n", 400, "upgrade")
        self.upstream.requests.clear()
        self.assert_refused(self.request(headers=[b"Connection: Upgrade", b"Upgrade: websocket"]), 400, "upgrade")
        self.upstream.requests.clear()
        self.assert_refused(self.request(headers=[b"Upgrade: h2c"]), 400, "upgrade")

    def test_ambiguous_requests(self):
        cases = {
            "both lengths": self.request(headers=[b"Content-Length: 5", b"Transfer-Encoding: chunked"]),
            "two lengths": self.request(headers=[b"Content-Length: %d" % len(BODY), b"Content-Length: 1"]),
            "two hosts": self.request(headers=[b"Host: api.anthropic.com", b"Content-Length: %d" % len(BODY)]),
            "two authorizations": self.request(headers=[b"Authorization: Bearer " + PLACEHOLDER]),
            "no host": self.request().replace(b"Host: host.agentbox:9101\r\n", b""),
            "another coding": self.request(headers=[b"Transfer-Encoding: gzip, chunked"], body=None) + b"0\r\n\r\n",
            "expect": self.request(headers=[b"Expect: 100-continue"]),
            "dot segment": self.request(target="/v1/../v1/messages"),
            "dot segment at the end": self.request(target="/v1/messages/."),
            "empty segment": self.request(target="//v1/messages"),
            "escaped": self.request(target="/v1/%6dessages"),
            "absolute form": self.request(target="http://api.anthropic.com/v1/messages"),
            "a fragment": self.request(target="/v1/messages#x"),
            "HTTP/1.0": self.request().replace(b"HTTP/1.1", b"HTTP/1.0", 1),
            "folded header": self.request(headers=[b"anthropic-beta: a", b" b"]),
            "space before the colon": self.request(headers=[b"anthropic-beta : a"]),
            "bare line feed": self.request().replace(b"\r\n", b"\n"),
            "not HTTP": b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03\r\n\r\n",
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.upstream.requests.clear()
                self.assert_refused(raw, 400, "invalid-request")

    def test_a_body_over_32_mb(self):
        raw = self.request(headers=[b"Content-Length: %d" % (32 * 1024 * 1024 + 1)], body=None)
        self.assert_refused(raw, 400, "too-large")

    def test_a_non_ascii_byte_in_the_target(self):
        _, line = self.assert_refused(b"POST /v1/\xff HTTP/1.1\r\nHost: h\r\n\r\n", 400, "invalid-request")
        self.assertEqual(line, "[priv] refused: invalid-request: not an HTTP/1.1 request line")

    def test_a_refused_hello_is_logged(self):
        self.assert_refused(self.request(method="HEAD", target="/api/hello", headers=[], body=None, auth=b"wrong"),
                            401, "wrong-placeholder")
        self.assert_refused(self.request(method="HEAD", target="/api/hello", headers=[b"Content-Length: 1"],
                                         body=None, auth=PLACEHOLDER + b"x"), 401, "wrong-placeholder")

    def test_a_chunked_body_over_32_mb(self):
        conn = self.route()
        conn.send(self.request(headers=[b"Transfer-Encoding: chunked"], body=None))
        piece = b"x" * (1024 * 1024)
        try:
            for _ in range(33):
                conn.send(b"%x\r\n" % len(piece) + piece + b"\r\n")
        except OSError:
            pass                    # the route stopped reading once it was too much
        status, _, _ = conn.response()
        self.assertEqual(status, 400)
        self.assertRegex(conn.finish()[0], r"^\[priv\] refused: too-large: ")
        self.assertEqual(self.upstream.requests, [])

    def test_the_vms_connections_beyond_the_limit_are_refused_and_logged(self):
        slots = self.path("run/agentbox-claude/priv")
        os.makedirs(slots)
        held = []
        for n in range(64):
            fd = os.open(f"{slots}/slot-{n}", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.append(fd)
        try:
            status, headers, _, log = self.ask(self.request())
        finally:
            for fd in held:
                os.close(fd)
        self.assertEqual((status, dict(headers)["x-should-retry"]), (503, "true"))
        self.assertRegex(log[0], r"^\[priv\] refused: too-many-connections: the VM already has 64 connections")
        self.assertEqual(self.ask(self.request())[0], 200)

    def test_an_upstream_whose_certificate_does_not_verify(self):
        message, _ = self.assert_refused(self.request(), 502, "upstream-tls", ca="other-ca.pem")
        self.assertIn("could not be verified", message)
        # once, never retried
        self.assertEqual(self.upstream.connections, 1)

    def test_an_upstream_that_cannot_be_reached(self):
        with socket.create_server(("127.0.0.1", 0)) as s:
            port = s.getsockname()[1]            # closed again: nothing listens there
        self.assert_refused(self.request(), 502, "upstream-error", port=port)

    def test_an_invalid_vm_name(self):
        r = subprocess.run([sys.executable, "-I", ROUTE, "../x"], stdin=subprocess.DEVNULL, capture_output=True,
                           env=self.env(), timeout=30)
        self.assertNotEqual(r.returncode, 0)


def load_route(env=None):
    """The route as a module, its constants read in this environment."""
    loader = importlib.machinery.SourceFileLoader("agentbox_claude_route", ROUTE)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    with mock.patch.dict(os.environ, env if env is not None else {}, clear=env is not None):
        loader.exec_module(module)
    return module


class TestOnlyTest(unittest.TestCase):
    def test_the_upstream_and_its_ca_change_only_under_a_test_root(self):
        override = {"AGENTBOX_CLAUDE_TEST_UPSTREAM": "127.0.0.1:1", "AGENTBOX_CLAUDE_TEST_CA": "/tmp/ca.pem"}
        route = load_route(override)
        self.assertEqual((route.ROOT, route.HOST, route.PORT), ("", "api.anthropic.com", 443))
        with mock.patch.dict(os.environ, override, clear=True):
            self.assertIsNone(route._test_upstream())
        route = load_route({**override, "AGENTBOX_ROOT": "/tmp/root"})
        with mock.patch.dict(os.environ, override, clear=True):
            self.assertEqual(route._test_upstream(), ("127.0.0.1", 1, "/tmp/ca.pem"))

    def test_the_route_runs_isolated_and_asks_for_verified_tls(self):
        with open(ROUTE) as f:
            source = f.read()
        self.assertTrue(source.startswith("#!/usr/bin/python3 -I\n"))
        self.assertIn("ssl.create_default_context(", source)
        self.assertNotIn("CERT_NONE", source)
        self.assertNotIn("check_hostname", source)
        self.assertNotIn("urllib", source)


if __name__ == "__main__":
    unittest.main()
