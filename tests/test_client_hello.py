"""The proxy takes the destination from the TLS ClientHello (ADR 0014).

The parser (`client_hello` in proxy/agentbox-proxy) against handshakes Python's ssl
(OpenSSL) really writes: with and without a server name, TLS 1.2 and 1.3, fragmented,
truncated, garbage, and random mutations of a real one. Then the proxy on its socket
(test_allowlist.ProxyTest): what it answers and logs for each kind of connection, that
the ClientHello reaches the destination first, and, socket-activated on TCP as on the
Host, that its log names the client's address.

Run from the repo root: python3 -m unittest discover -s tests
"""
import importlib.machinery
import importlib.util
import os
import random
import socket
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_allowlist as allowlist_tests  # noqa: E402
from client_hello import (ACCESS_DENIED, INTERNAL_ERROR, UNRECOGNIZED_NAME, answer, client_hello,  # noqa: E402
                          raw_client_hello, server_name)
from test_allowlist import PROXY, entry  # noqa: E402


def load_proxy():
    """The proxy as a module, the way it loads the classifier."""
    loader = importlib.machinery.SourceFileLoader("agentbox_proxy", PROXY)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


proxy = load_proxy()


def record(payload):
    """One TLS handshake record around payload."""
    return b"\x16\x03\x01" + len(payload).to_bytes(2, "big") + payload


def records(hello, size):
    """The handshake of a ClientHello split into records of size bytes."""
    payload = hello[5:]
    return b"".join(record(payload[i:i + size]) for i in range(0, len(payload), size))


class ParserTest(unittest.TestCase):
    def name(self, data):
        return proxy.client_hello(data)

    def refused(self, data, reason=proxy.NOT_TLS):
        with self.assertRaises(proxy.Refused) as caught:
            proxy.client_hello(data)
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def test_real_handshakes_name_their_server(self):
        self.assertEqual(self.name(client_hello("registry.npmjs.org")), "registry.npmjs.org")
        self.assertEqual(self.name(client_hello("deb.debian.org", tls12=True)), "deb.debian.org")
        self.assertEqual(self.name(raw_client_hello("example.org")), "example.org")

    def test_without_a_server_name_it_is_refused_with_the_alert_for_it(self):
        for hello in (client_hello(None), client_hello(None, tls12=True)):
            e = self.refused(hello, "no server name")
            self.assertEqual(e.alert, proxy.UNRECOGNIZED_NAME)
        self.assertIsNone(self.refused(b"GET / HTTP/1.1\r\n\r\n").alert)

    def test_the_name_is_lowercased_without_its_trailing_dot(self):
        self.assertEqual(self.name(client_hello("Registry.NPMJS.org.")), "registry.npmjs.org")

    def test_a_name_that_is_no_host_name_counts_as_none(self):
        for name in ("", ".", "bad name.example", "a/b.example", "x" * 254, "é.example", "a:b", "a_b.example"):
            with self.subTest(name=name):
                self.refused(raw_client_hello(name), "no server name")

    def test_with_an_encrypted_client_hello_extension_the_outer_name_decides(self):
        # browsers send one in every handshake as GREASE, with the real destination outside
        grease = (0xfe0d, b"\x00\x00\x01\x00\x01" + bytes(1) + b"\x00\x20" + bytes(32) + b"\x00\x40" + bytes(64))
        for hello in (raw_client_hello("registry.npmjs.org", after=[grease]),
                      raw_client_hello("registry.npmjs.org", before=[grease])):
            self.assertEqual(self.name(hello), "registry.npmjs.org")
        # other extensions around the server name change nothing
        self.assertEqual(self.name(raw_client_hello("example.org", before=[(0x002b, b"\x02\x03\x04")],
                                                    after=[(0x0015, bytes(10))])), "example.org")

    def test_an_extension_twice_is_refused(self):
        # the proxy would decide on one server_name, the destination might read the other
        self.refused(raw_client_hello("allowed.example", after=[(0, server_name("other.example"))]))
        self.refused(raw_client_hello("example.org", after=[(21, b""), (21, bytes(3))]))

    def test_several_host_names_count_as_none(self):
        e = self.refused(raw_client_hello(("allowed.example", "other.example")), "no server name")
        self.assertEqual(e.alert, proxy.UNRECOGNIZED_NAME)
        self.assertEqual(self.name(raw_client_hello(("allowed.example",))), "allowed.example")

    def test_an_address_as_server_name_is_passed_on_for_the_signals(self):
        # RFC 6066 forbids it; the classifier flags it (raw-ip, a strong Signal) and it stays blocked
        self.assertEqual(self.name(raw_client_hello("10.0.0.1")), "10.0.0.1")

    def test_a_hello_in_pieces_asks_for_more_until_it_is_whole(self):
        hello = client_hello("example.org")
        for n in (0, 1, 4, 5, 6, 40, len(hello) - 1):
            self.assertIsNone(self.name(hello[:n]), n)
        self.assertEqual(self.name(hello), "example.org")
        self.assertEqual(self.name(hello + b"more"), "example.org")

    def test_a_hello_spread_over_several_records(self):
        hello = client_hello("example.org")
        for size in (1, 7, 64, 100):
            data = records(hello, size)
            self.assertEqual(self.name(data), "example.org", size)
            self.assertIsNone(self.name(data[:-3]), size)

    def test_what_is_no_tls_is_refused(self):
        hello = client_hello("example.org")
        for data in (b"GET / HTTP/1.1\r\nHost: example.org\r\n\r\n", b"SSH-2.0-OpenSSH\r\n", b"\x00" * 64,
                     b"\x80\x2e\x01\x03\x01",                        # an SSLv2 ClientHello
                     b"\x15\x03\x03\x00\x02\x02\x50",                # an alert
                     b"\x16\x02\x00" + hello[3:],                    # a version that is no TLS
                     b"\x16\x03\x01\x00\x00" + hello[5:],            # an empty record
                     b"\x16\x03\x01\x7f\xff" + hello[5:],            # longer than a record may be
                     hello[:5] + b"\x02" + hello[6:],                # a ServerHello
                     record(b"\x01\x00\x00\x02\x03\x03")):          # a ClientHello of two bytes
            with self.subTest(data=data[:8]):
                self.refused(data)

    def test_lengths_inside_the_hello_that_do_not_add_up_are_refused(self):
        hello = raw_client_hello("example.org")
        # the cipher suites (at 44: the headers, version, random and empty session id) claim 65535 bytes
        self.refused(hello[:44] + b"\xff\xff" + hello[46:])
        # the extensions claim less than the message holds
        self.refused(raw_client_hello("example.org", padding=1))

    def test_a_fuzzed_hello_is_only_ever_a_name_more_or_refused(self):
        rng = random.Random(20261111)
        hellos = [client_hello("example.org"), client_hello("example.org", tls12=True), client_hello(None)]
        deadline, runs = time.monotonic() + 2, 0
        while time.monotonic() < deadline or runs < 500:
            data = bytearray(rng.choice(hellos))
            for _ in range(rng.randint(1, 4)):
                what = rng.random()
                if what < 0.5:
                    data[rng.randrange(len(data))] = rng.randrange(256)
                elif what < 0.6:
                    del data[:rng.randrange(len(data))]
                elif what < 0.75:
                    del data[rng.randrange(len(data)):]
                else:
                    at = rng.randrange(len(data) + 1)
                    data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
                if not data:
                    data = bytearray(b"\x16")
            try:
                result = proxy.client_hello(bytes(data))
            except proxy.Refused as e:
                self.assertIn(e.reason, (proxy.NOT_TLS, "no server name"))
            else:
                self.assertTrue(result is None or isinstance(result, str), result)
            runs += 1


class HelloProxyTest(allowlist_tests.ProxyTest):
    """The proxy on its socket, with example.org allowed."""

    def setUp(self):
        super().setUp()
        self.store(entry("example.org", "all"))
        self.start()

    def send(self, *pieces, pause=0.05):
        """A socket after sending the pieces one by one, with pauses."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.settimeout(10)
        s.connect(self.sock)
        for i, piece in enumerate(pieces):
            if i:
                time.sleep(pause)
            s.sendall(piece)
        return s

    def rest(self, s):
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        return data

    def test_an_allowed_hello_opens_the_tunnel_and_reaches_the_destination_first(self):
        s = self.tunnel("example.org")      # the echo server returned the ClientHello itself
        self.assertTrue(self.alive(s))
        self.assertIn("[work] ALLOWED example.org (allowlist example.org all)\n", self.log())

    def test_tls_1_2_as_well(self):
        hello = client_hello("example.org", tls12=True)
        s = self.send(hello)
        self.assertEqual(answer(s, hello), "open")

    def test_a_hello_arriving_in_pieces(self):
        hello = client_hello("example.org")
        s = self.send(hello[:3], hello[3:5], hello[5:60], hello[60:])
        self.assertEqual(answer(s, hello), "open")

    def test_a_hello_in_several_records_arriving_in_pieces(self):
        data = records(client_hello("example.org"), 50)
        s = self.send(*(data[i:i + 37] for i in range(0, len(data), 37)))
        self.assertEqual(answer(s, data), "open")

    def test_without_a_server_name_the_alert_says_so(self):
        s = self.send(client_hello(None))
        self.assertEqual(self.rest(s), UNRECOGNIZED_NAME)
        self.assertIn("[work] BLOCKED unix (no server name)\n", self.log())

    def test_what_is_no_tls_is_closed_without_an_answer(self):
        s = self.send(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n")
        self.assertEqual(self.rest(s), b"")
        self.assertIn("[work] BLOCKED unix (not a TLS handshake)\n", self.log())

    def test_a_hello_that_never_completes_is_refused_at_the_limit(self):
        # a record of the longest length TLS allows, promising more handshake than any ClientHello has
        s = self.send(b"\x16\x03\x01\x40\x00" + b"\x01\xff\xff\xff" + bytes(16380))
        self.assertEqual(self.rest(s), b"")
        self.assertIn("[work] BLOCKED unix (not a TLS handshake)\n", self.log())

    def test_a_hello_in_one_record_of_the_longest_length_is_the_limit(self):
        # padding (extension 21) fills the record to the 16,384 bytes TLS allows: 16,389 with its header
        base = len(raw_client_hello("example.org", after=[(21, b"")]))
        largest = raw_client_hello("example.org", after=[(21, bytes(16389 - base))])
        self.assertEqual(len(largest), 16389)
        self.assertEqual(answer(self.send(largest), largest), "open")
        # one byte more no longer fits one record, and two records do not fit the limit
        over = records(raw_client_hello("example.org", after=[(21, bytes(16389 - base + 1))]), 16384)
        self.assertEqual(self.rest(self.send(over)), b"")
        self.assertIn("[work] BLOCKED unix (not a TLS handshake)\n", self.log())

    def test_a_client_that_sends_nothing_leaves_no_trace(self):
        s = self.send()
        s.close()
        time.sleep(0.2)
        self.assertNotIn("BLOCKED", self.log())

    def test_a_blocked_destination_gets_access_denied(self):
        s = self.send(client_hello("other.example"))
        self.assertEqual(self.rest(s), ACCESS_DENIED)
        self.assertIn("[work] BLOCKED other.example (not on the Allowlist)\n", self.log())


class RefusalTest(allowlist_tests.ProxyTest):
    """What the proxy answers an allowed destination it cannot connect to or that has no
    public address."""

    def refusal(self, host):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(30)
            s.connect(self.sock)
            s.sendall(client_hello(host))
            data = b""
            while chunk := s.recv(4096):
                data += chunk
        return data

    def test_an_unreachable_destination_gets_internal_error(self):
        # a port that is bound but takes no connections: refused at once, and nobody else gets it
        closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(closed.close)
        closed.bind(("127.0.0.1", 0))
        self.store(entry("example.org", "all"))
        self.start(upstream=closed.getsockname()[1])
        self.assertEqual(self.refusal("example.org"), INTERNAL_ERROR)
        self.assertIn("[work] ERROR example.org (", self.log())

    def test_a_destination_without_a_public_address_gets_access_denied(self):
        # localhost comes from the hosts file, no DNS needed; the proxy resolves it itself here
        self.store(entry("localhost", "all"))
        self.start(upstream=False)
        self.assertEqual(self.refusal("localhost"), ACCESS_DENIED)
        self.assertIn("[work] BLOCKED localhost (does not resolve to a public address)\n", self.log())


class SocketActivationTest(allowlist_tests.ProxyTest):
    """The proxy on a TCP socket systemd handed over (LISTEN_FDS), as on the Host."""

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        self.addCleanup(listener.close)
        self.port = listener.getsockname()[1]
        env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "AGENTBOX_CONF_DIR": self.conf,
               "AGENTBOX_FEEDS_DIR": os.path.join(self.dir, "feeds"), "LISTEN_FDS": "1",
               "AGENTBOX_PROXY_TEST_UPSTREAM": "127.0.0.1:%d" % self.echo.server_address[1]}
        # the socket becomes fd 3 (where systemd puts the first one) and the proxy takes over the process
        handover = "import os, sys; os.dup2(int(sys.argv[1]), 3); os.execv(sys.executable, [sys.executable, '-I', sys.argv[2], 'work'])"
        fd = listener.fileno()
        with open(self.log_path, "wb") as err:
            self.proxy = subprocess.Popen([sys.executable, "-I", "-c", handover, str(fd), PROXY], env=env,
                                          pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 15
        while "[work] ready" not in self.log():
            if self.proxy.poll() is not None or time.monotonic() > deadline:
                self.fail("proxy did not start: " + self.log())
            time.sleep(0.02)

    def request(self, host):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(s.close)
        hello = client_hello(host)
        s.sendall(hello)
        return s, answer(s, hello)

    def test_tunnels_over_tcp_and_names_the_client_in_its_log(self):
        self.store(entry("example.org", "all"))
        self.start()
        self.tunnel("example.org")
        self.assertTrue(self.refused("other.example"))
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(s.close)
        s.sendall(b"GET / HTTP/1.1\r\n\r\n")
        self.assertEqual(s.recv(100), b"")
        self.assertIn("[work] BLOCKED 127.0.0.1 (not a TLS handshake)\n", self.log())

    def test_a_client_that_sent_more_than_its_hello_still_gets_the_alert(self):
        # closing with bytes unread would reset the connection and lose the alert
        self.start()
        for _ in range(5):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            self.addCleanup(s.close)
            s.sendall(client_hello("other.example") + bytes(65536))     # more than the proxy reads with it
            time.sleep(0.2)
            data = b""
            while chunk := s.recv(4096):
                data += chunk
            self.assertEqual(data, ACCESS_DENIED)


if __name__ == "__main__":
    unittest.main()
