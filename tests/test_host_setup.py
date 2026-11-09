"""The Host setup (ADR 0009): how a Host is exposed, read by the CLI (`regenerate`) and by
the Control Room backend (`web/agentbox-web`).

The CLI runs for real with AGENTBOX_ROOT pointing at a temporary root and fake virsh,
qemu-img, systemctl and virt-install on PATH (see test_registry.py). The backend runs for
real on loopback with a throwaway certificate.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from test_registry import PRIV_UUID, RegistryTest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "web", "agentbox-web")
STATIC = os.path.join(ROOT, "web", "static")


class RegenerateHostSetupTest(RegistryTest):
    def setUp(self):
        super().setUp()
        vm_records.write(self.root)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})

    def gui_socket(self):
        return self.read("etc/systemd/system/agentbox-gui-priv.socket")

    def pairing_address(self):
        argv = [c["argv"] for c in self.calls("virt-install") if "agentbox-priv" in c["argv"]][-1]
        sysinfo = argv[argv.index("--sysinfo") + 1].split(",")
        return [e for e in sysinfo if e.startswith("entry3=")], \
            [e for e in sysinfo if e == "entry3.name=opt/agentbox/pairing_address"]

    def test_without_a_record_the_host_is_local_only(self):
        self.assertEqual(self.run_agentbox("regenerate")[0], 0)
        self.assertIn("ListenStream=127.0.0.1:6802\n", self.gui_socket())
        self.assertEqual(self.pairing_address(), (["entry3=127.0.0.1"], ["entry3.name=opt/agentbox/pairing_address"]))

    def test_local_only_leaves_the_vm_network_sockets_alone(self):
        vm_records.write_host_setup(self.root, {"mode": "local"})
        self.assertEqual(self.run_agentbox("regenerate")[0], 0)
        self.assertIn("ListenStream=127.0.0.1:6802\n", self.gui_socket())
        self.assertIn("ListenStream=10.77.2.1:3128\n", self.read("etc/systemd/system/agentbox-proxy-priv.socket"))
        self.assertIn("ListenStream=10.77.2.1:9100\n", self.read("etc/systemd/system/agentbox-ghvm-priv.socket"))

    def test_network_mode_listens_on_every_address_and_pairs_through_the_bind_address(self):
        vm_records.write_host_setup(self.root, bind="192.168.5.7", home_network="192.168.5.0/24")
        self.assertEqual(self.run_agentbox("regenerate")[0], 0)
        self.assertIn("ListenStream=6802\n", self.gui_socket())
        self.assertEqual(self.pairing_address(), (["entry3=192.168.5.7"], ["entry3.name=opt/agentbox/pairing_address"]))

    def test_a_record_that_makes_no_sense_stops_everything_before_anything_is_written(self):
        net = vm_records.NETWORK_HOST
        cases = {
            "not json": "{",
            "not an object": "[]",
            "unknown field": json.dumps({"colour": "red"}),
            "unknown mode": json.dumps({"mode": "both"}),
            "local on a LAN address": json.dumps({"mode": "local", "bind": "192.0.2.8"}),
            "local with a home network": json.dumps({"mode": "local", "home_network": "192.0.2.0/24"}),
            "network without bind": json.dumps({k: v for k, v in net.items() if k != "bind"}),
            "network on loopback": json.dumps({**net, "bind": "127.0.0.1"}),
            "network on every address": json.dumps({**net, "bind": "0.0.0.0"}),
            "network without home network": json.dumps({k: v for k, v in net.items() if k != "home_network"}),
            "home network with host bits": json.dumps({**net, "home_network": "192.0.2.8/24"}),
            "bind is not an address": json.dumps({**net, "bind": "titan"}),
            "port zero": json.dumps({**net, "web_port": 0}),
            "port as text": json.dumps({**net, "web_port": "8800"}),
        }
        for name, content in cases.items():
            with self.subTest(name):
                self.write("etc/agentbox/host.json", content)
                self.forget_calls()
                code, out, err = self.run_agentbox("regenerate")
                self.assertEqual(code, 1)
                self.assertIn("Host setup", err)
                self.assertEqual(os.listdir(self.path("etc/systemd/system")), [])
                self.assertEqual(self.calls("virt-install"), [])

    def test_settings_cannot_change_it(self):
        vm_records.write_host_setup(self.root, {"mode": "local"})
        before = self.snapshot()["etc/agentbox/host.json"]
        for field in ("mode=network", "bind=0.0.0.0", "web_port=9000", "home_network=10.0.0.0/8"):
            with self.subTest(field):
                self.assertNotEqual(self.run_agentbox("set-settings", field)[0], 0)
        self.assertEqual(self.snapshot()["etc/agentbox/host.json"], before)


class BackendHostSetupTest(unittest.TestCase):
    """The Control Room backend takes address and port from the Host setup."""

    @classmethod
    def setUpClass(cls):
        cls.conf = tempfile.TemporaryDirectory()
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "1", "-subj", "/CN=t",
                        "-keyout", os.path.join(cls.conf.name, "key.pem"),
                        "-out", os.path.join(cls.conf.name, "cert.pem")], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.conf.cleanup()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_path = os.path.join(tmp.name, "host.json")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]

    def start(self, setup):
        if setup is not None:
            with open(self.setup_path, "w") as f:
                f.write(setup if isinstance(setup, str) else json.dumps(setup))
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOX_WEB_")}
        env.update(AGENTBOX_WEB_CONF=self.conf.name, AGENTBOX_WEB_STATIC=STATIC, AGENTBOX_WEB_HOST_SETUP=self.setup_path)
        proc = subprocess.Popen([sys.executable, "-I", SERVER], env=env, text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stderr.close()

    def listens(self, proc, address):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail("server exited early: " + proc.stderr.read())
            try:
                socket.create_connection((address, self.port), timeout=1).close()
                return True
            except OSError:
                time.sleep(0.05)
        return False

    def fails(self, setup):
        proc = self.start(setup)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("the server started although the Host setup makes no sense")
        return proc.returncode, proc.stderr.read()

    def test_local_only_listens_on_loopback_on_the_port_of_the_record(self):
        proc = self.start({"mode": "local", "web_port": self.port})
        self.assertTrue(self.listens(proc, "127.0.0.1"))

    def test_network_mode_listens_on_the_bind_address(self):
        # any address in 127.0.0.0/8 is local on Linux, so this stands in for the Host's LAN address
        proc = self.start({"mode": "network", "bind": "127.0.0.2", "web_port": self.port,
                           "home_network": "127.0.0.0/8"})
        self.assertTrue(self.listens(proc, "127.0.0.2"))
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()

    def test_a_broken_record_stops_the_server(self):
        for name, setup in {"not json": "{", "local on a LAN address": {"mode": "local", "bind": "192.0.2.8"},
                            "bad port": {"web_port": 70000}}.items():
            with self.subTest(name):
                code, err = self.fails(setup)
                self.assertNotEqual(code, 0)
                self.assertIn("Host setup", err)

    def test_hashing_a_password_does_not_need_a_valid_record(self):
        with open(self.setup_path, "w") as f:
            f.write("{")
        env = dict(os.environ, AGENTBOX_WEB_HOST_SETUP=self.setup_path)
        r = subprocess.run([sys.executable, "-I", SERVER, "--hash-password"], input="x" * 16 + "\n",
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
