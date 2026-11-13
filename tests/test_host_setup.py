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
import ssl
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
        self.assertIn("ListenStream=10.77.2.1:443\n", self.read("etc/systemd/system/agentbox-proxy-priv.socket"))
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
        with open(os.path.join(cls.conf.name, "cert.pem")) as f:
            cls.cert = ssl.PEM_cert_to_DER_cert(f.read())

    @classmethod
    def tearDownClass(cls):
        cls.conf.cleanup()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # the backend reads the Host setup as the CLI does, under AGENTBOX_ROOT
        self.root = tmp.name
        self.setup_path = os.path.join(self.root, "etc", "agentbox", "host.json")
        os.makedirs(os.path.dirname(self.setup_path))

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOX_WEB_")}
        env.update(AGENTBOX_WEB_CONF=self.conf.name, AGENTBOX_ROOT=self.root)
        return env

    def write(self, setup):
        with open(self.setup_path, "w") as f:
            f.write(setup if isinstance(setup, str) else json.dumps(setup))

    def start(self, setup):
        if setup is not None:
            self.write(setup)
        proc = subprocess.Popen([sys.executable, "-I", SERVER], env=self.env(), text=True,
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

    def serve(self, setup_for, address):
        """The backend listening on the port of the record, which the Host setup cannot leave to the
        kernel (port 0 is no valid web_port): it takes a free port, and if another test running at
        the same time has taken it by the time the backend binds, another one."""
        for _ in range(10):
            holder = socket.socket()
            holder.bind(("127.0.0.1", 0))
            self.port = holder.getsockname()[1]
            holder.close()
            proc = self.start(setup_for(self.port))
            if self.listens(proc, address) is not None:
                return proc
        self.fail("the backend found its port in use 10 times: " + self.in_use)

    def listens(self, proc, address):
        """True once the backend answers on address and the port (with its certificate: another
        test's server on that port is not it), False when it never does, None when it exited
        because the port is in use."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read()
                if "Address already in use" in err:
                    self.in_use = err
                    return None
                self.fail("server exited early: " + err)
            try:
                served = ssl.get_server_certificate((address, self.port), timeout=1)
                if ssl.PEM_cert_to_DER_cert(served) == self.cert:
                    return True
            except OSError:
                pass
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
        proc = self.serve(lambda port: {"mode": "local", "web_port": port}, "127.0.0.1")
        self.assertTrue(self.listens(proc, "127.0.0.1"))

    def address(self, setup):
        """(address, port) the backend takes from this record (its bind_port(), loaded but not
        started): Network mode needs the Host's address in the home network, which a test cannot
        bind."""
        if setup is not None:
            self.write(setup)
        code = ("import importlib.machinery, importlib.util, json, sys\n"
                "loader = importlib.machinery.SourceFileLoader('agentbox_web', sys.argv[1])\n"
                "web = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))\n"
                "loader.exec_module(web)\n"
                "print(json.dumps(web.bind_port()))")
        r = subprocess.run([sys.executable, "-I", "-c", code, SERVER], capture_output=True, text=True,
                           env=self.env(), timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return tuple(json.loads(r.stdout))

    def test_network_mode_listens_on_the_bind_address(self):
        self.assertEqual(self.address({"mode": "network", "bind": "192.0.2.8", "web_port": 8443,
                                       "home_network": "192.0.2.0/24"}), ("192.0.2.8", 8443))

    def test_local_only_without_a_record_and_with_every_field(self):
        self.assertEqual(self.address(None), ("127.0.0.1", 8800))
        self.assertEqual(self.address({}), ("127.0.0.1", 8800))
        self.assertEqual(self.address({"mode": "local", "bind": "127.0.0.5", "web_port": 9000, "cert_name": "box.lan",
                                       "owner_user": "alice"}), ("127.0.0.5", 9000))

    def test_a_broken_record_stops_the_server(self):
        # the CLI's checks, with its message and without a traceback
        for name, setup in {"not json": "{", "local on a LAN address": {"mode": "local", "bind": "192.0.2.8"},
                            "bad port": {"web_port": 70000}, "unknown field": {"mode": "local", "port": 8800},
                            "network without home_network": {"mode": "network", "bind": "192.0.2.8"},
                            "network on loopback": {"mode": "network", "bind": "127.0.0.2",
                                                    "home_network": "127.0.0.0/8"}}.items():
            with self.subTest(name):
                code, err = self.fails(setup)
                self.assertNotEqual(code, 0)
                self.assertTrue(err.startswith(f"agentbox-web: Host setup {self.setup_path}: "), err)
                self.assertNotIn("Traceback", err)

    def test_hashing_a_password_does_not_need_a_valid_record(self):
        self.write("{")
        r = subprocess.run([sys.executable, "-I", SERVER, "--hash-password"], input="x" * 16 + "\n",
                           capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
