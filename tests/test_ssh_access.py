"""SSH access to a VM through its SSH port (#129, ADR 0020), the tracer bullet in Local-only.

`agentbox set-ssh`, `add-ssh-key` and `ssh` keep SSH access and the SSH keys in the VM's record,
the Host makes the VM's host key once, `regenerate` and the commands write the fw_cfg file the
VM reads at boot and the units of the SSH port (2200 + network number) while SSH access is on.
The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and the fakes of
test_registry.py (openssl is the real one, as in test_github_app.py); the guest side
(vm/files/usr/lib/agentbox/fwcfg-ssh, which guest-setup runs) against an fw_cfg file in a
temporary directory.

Run from the repo root: python3 -m unittest discover -s tests
"""
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from fake_journald import Journald  # noqa: E402
from test_registry import AGENTBOX, REPO  # noqa: E402
from test_vms import VmsTest  # noqa: E402

sys.path.insert(0, REPO)
from agentbox import sshkeys  # noqa: E402

HOST_KEY = "etc/agentbox/secrets/ssh-host-key-{}"
SSH_FILE = "var/lib/agentbox/vm-ssh/{}.ssh"
SOCKET = "etc/systemd/system/agentbox-ssh-{}.socket"
SERVICE = "etc/systemd/system/agentbox-ssh-{}@.service"
FWCFG_SSH = os.path.join(REPO, "vm", "files", "usr", "lib", "agentbox", "fwcfg-ssh")
GUEST_SETUP = os.path.join(REPO, "vm", "files", "usr", "lib", "agentbox", "guest-setup")
# a key and its fingerprint as `ssh-keygen -lf` gives it
LAPTOP = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f laptop"
LAPTOP_FP = "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA"


def ssh_string(data):
    return struct.pack(">I", len(data)) + data


def key(kind="ssh-ed25519", seed=1, bits=3072, comment=""):
    """A public key line of this type (made up, of the right form)."""
    body = hashlib.sha512(str(seed).encode()).digest() * 8
    if kind == "ssh-rsa":
        n = int.from_bytes(body[:bits // 8], "big") | (1 << (bits - 1)) | 1
        parts = [b"\x01\x00\x01", b"\x00" + n.to_bytes(bits // 8, "big")]
    elif kind in ("ssh-ed25519", "sk-ssh-ed25519@openssh.com"):
        parts = [body[:32]] + ([b"ssh:"] if kind.startswith("sk-") else [])
    elif kind == "ssh-dss":
        parts = [body[:128], body[:20], body[:128], body[:128]]
    else:
        curve = kind.split("@")[0].rsplit("-", 1)[1]
        size = {"nistp256": 32, "nistp384": 48, "nistp521": 66}[curve]
        parts = [curve.encode(), b"\x04" + body[:2 * size]] + ([b"ssh:"] if kind.startswith("sk-") else [])
    blob = ssh_string(kind.encode()) + b"".join(ssh_string(p) for p in parts)
    return f"{kind} {base64.b64encode(blob).decode()}" + (f" {comment}" if comment else "")


def fingerprint(line):
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


class SshTest(VmsTest):
    def setUp(self):
        super().setUp()
        self.journald = Journald(self.path("run/systemd/journal.agentbox/socket"))
        self.addCleanup(self.journald.close)
        self.ok("regenerate")
        self.forget_calls()
        self.journald.entries()
        self.journald.datagrams.clear()

    def run_input(self, *args, stdin=""):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        r = subprocess.run([AGENTBOX, *args], input=stdin, capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def add(self, vm="work", line=LAPTOP, args=()):
        code, out, err = self.run_input("add-ssh-key", vm, *args, "--json", stdin=line + "\n")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def add_refused(self, line, *args, vm="work"):
        """The --json failure of add-ssh-key; nothing changes."""
        before = self.files()
        code, out, err = self.run_input("add-ssh-key", vm, *args, "--json", stdin=line)
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(self.files(), before)
        return json.loads(err)

    def record(self, vm="work"):
        return vm_records.read(self.root, vm)

    def snapshot(self):
        """Every regular file under the root with its content and mode (not the journal's socket)."""
        files = {}
        for top, _, names in os.walk(self.root):
            for n in names:
                p = os.path.join(top, n)
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        files[os.path.relpath(p, self.root)] = (f.read(), os.stat(p).st_mode & 0o7777)
        return files

    def activity(self):
        return [(e["AGENTBOX_ACTION"], e["AGENTBOX_VM"], e["AGENTBOX_OUTCOME"],
                 {k[len("AGENTBOX_PARAM_"):].lower(): v for k, v in e.items() if k.startswith("AGENTBOX_PARAM_")})
                for e in self.journald.entries()]

    def turned_off(self, vm):
        os.remove(os.path.join(self.dir, "domains.json"))
        self.scenario(vms={vm: {"state": "shut off"}})


class RecordTest(SshTest):
    def test_a_record_without_the_fields_has_no_ssh_access_and_no_keys(self):
        self.assertNotIn("ssh_access", self.record())
        self.assertEqual(self.json("ssh", "work"),
                         {"vm": "work", "enabled": False, "port": 2201, "host_key_fingerprint": None,
                          "known_hosts": None, "keys": []})
        # no SSH port, and the VM learns it has no SSH access
        self.assertFalse(self.exists(SOCKET.format("work")))
        self.assertEqual(self.read(SSH_FILE.format("work")), "none\n")
        self.assertEqual(self.mode(SSH_FILE.format("work")), 0o640)
        self.assertEqual(self.mode("var/lib/agentbox/vm-ssh"), 0o750)
        self.assertFalse(self.exists(HOST_KEY.format("work")))

    def test_the_record_holds_access_and_keys(self):
        self.ok("set-ssh", "work", "on")
        self.add()
        rec = self.record()
        self.assertIs(rec["ssh_access"], True)
        [stored] = rec["ssh_keys"]
        self.assertRegex(stored.pop("added"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(stored, {"key": LAPTOP.rsplit(" ", 1)[0], "name": "laptop", "fingerprint": LAPTOP_FP,
                                  "added_by": stored["added_by"]})
        self.assertTrue(stored["added_by"])

    def test_broken_values_make_the_record_broken(self):
        good = {"key": LAPTOP.rsplit(" ", 1)[0], "name": "laptop", "fingerprint": LAPTOP_FP,
                "added": "2026-11-18T10:00:00Z", "added_by": "bewr"}
        other = key(seed=2)
        cases = {"access no bool": {"ssh_access": "yes"}, "access a number": {"ssh_access": 1},
                 "keys no list": {"ssh_keys": {}},
                 "wrong fingerprint": {"ssh_keys": [{**good, "fingerprint": fingerprint(other)}]},
                 "a field missing": {"ssh_keys": [{k: v for k, v in good.items() if k != "added_by"}]},
                 "a field more": {"ssh_keys": [{**good, "options": "x"}]},
                 "with comment": {"ssh_keys": [{**good, "key": LAPTOP}]},
                 "refused type": {"ssh_keys": [{**good, "key": key("ssh-dss")}]},
                 "twice": {"ssh_keys": [good, good]},
                 "too many": {"ssh_keys": [{**good, "key": key(seed=i), "fingerprint": fingerprint(key(seed=i))}
                                           for i in range(17)]},
                 "bad name": {"ssh_keys": [{**good, "name": "a\nb"}]}}
        for what, fields in cases.items():
            with self.subTest(what):
                vm_records.write(self.root, vm_records.record("work", 1, **fields))
                code, out, err = self.run_agentbox("ssh", "work")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("work.json: broken record (", err)
        vm_records.write(self.root, vm_records.record("work", 1, ssh_access=False, ssh_keys=[good]))
        self.assertEqual(self.json("ssh", "work")["keys"][0]["fingerprint"], LAPTOP_FP)


class KeyRulesTest(SshTest):
    def test_allowed_types_with_their_fingerprints(self):
        for i, kind in enumerate(sshkeys.TYPES):
            with self.subTest(kind):
                line = key(kind, seed=i)
                said = self.add(line=line)
                self.assertEqual((said["type"], said["fingerprint"]), (kind, fingerprint(line)))
        self.assertEqual(len(self.record()["ssh_keys"]), len(sshkeys.TYPES))

    def test_the_fingerprint_is_the_one_of_ssh_keygen(self):
        self.assertEqual(self.add()["fingerprint"], LAPTOP_FP)
        if shutil.which("ssh-keygen"):
            # made-up ECDSA points are no points of their curve, which ssh-keygen checks
            for kind in ("ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ssh-rsa"):
                with self.subTest(kind), tempfile.NamedTemporaryFile("w", suffix=".pub") as f:
                    f.write(key(kind) + "\n")
                    f.flush()
                    r = subprocess.run(["ssh-keygen", "-lf", f.name], capture_output=True, text=True, timeout=30)
                    self.assertEqual(r.stdout.split()[1], sshkeys.parse(key(kind))["fingerprint"])

    def test_weak_and_unknown_types_are_refused(self):
        said = self.add_refused(key("ssh-rsa", bits=2048))
        self.assertEqual((said["error"], said["params"]), ("ssh_key_too_weak", {"bits": 2048, "min_bits": 3072}))
        self.assertEqual(said["message"], "RSA keys need at least 3072 bits, this one has 2048")
        self.assertEqual(self.add_refused(key("ssh-rsa", bits=3064))["error"], "ssh_key_too_weak")
        for kind in ("ssh-dss", "ssh-ed25519-cert-v01@openssh.com", "ssh-ed448", "ecdsa-sha2-nistp224"):
            with self.subTest(kind):
                said = self.add_refused(f"{kind} AAAAB3NzaC1kc3MAAACBAP")
                self.assertEqual((said["error"], said["params"]), ("ssh_key_type_refused", {"type": kind}))
        self.add(line=key("ssh-rsa", bits=3072))

    def test_options_are_refused(self):
        for options in ('command="ls"', 'from="10.0.0.0/8"', "restrict", "no-pty,no-port-forwarding",
                        'environment="A=1" restrict', "cert-authority"):
            with self.subTest(options):
                said = self.add_refused(f"{options} {LAPTOP}")
                self.assertEqual(said["error"], "ssh_key_options_refused")
                self.assertTrue(said["message"].startswith("SSH keys with options"), said["message"])

    def test_invalid_keys_are_refused_without_repeating_them(self):
        ed = LAPTOP.split()[1]
        cases = {"empty": "", "comment only": "# nothing\n", "no key type": "hello world",
                 "no base64": "ssh-ed25519", "not base64": "ssh-ed25519 !!!notbase64",
                 "another type inside": f"ssh-rsa {ed}", "truncated": f"ssh-ed25519 {ed[:-8]}",
                 "two keys": f"{LAPTOP}\n{key(seed=2)}\n", "control character": f"{LAPTOP}\x1b[31m",
                 "short ecdsa point": "ecdsa-sha2-nistp256 " + base64.b64encode(
                     ssh_string(b"ecdsa-sha2-nistp256") + ssh_string(b"nistp256") + ssh_string(b"\x04ab")).decode(),
                 "wrong curve": "ecdsa-sha2-nistp384 " + base64.b64encode(
                     ssh_string(b"ecdsa-sha2-nistp384") + ssh_string(b"nistp256")
                     + ssh_string(b"\x04" + bytes(96))).decode()}
        for what, line in cases.items():
            with self.subTest(what):
                said = self.add_refused(line)
                self.assertEqual(said["error"], "ssh_key_invalid")
                self.assertTrue(said["message"].startswith("not a valid SSH public key: "), said["message"])
                self.assertNotIn(ed, said["message"])
        said = self.add_refused("x" * 20000)
        self.assertEqual(said["error"], "ssh_key_invalid")

    def test_duplicates_and_more_than_16_keys_are_refused(self):
        self.add()
        said = self.add_refused(LAPTOP.replace("laptop", "another comment"))
        self.assertEqual((said["error"], said["params"]), ("ssh_key_duplicate", {"fingerprint": LAPTOP_FP}))
        for i in range(15):
            self.add(line=key(seed=100 + i))
        said = self.add_refused(key(seed=200))
        self.assertEqual((said["error"], said["params"]), ("ssh_keys_too_many", {"max_keys": 16}))
        self.assertEqual(len(self.record()["ssh_keys"]), 16)
        # another VM has its own keys
        self.add("priv")

    def test_the_comment_is_the_name_unless_one_is_given(self):
        self.assertEqual(self.add()["name"], "laptop")
        self.assertEqual(self.add(line=key(seed=2), args=["--name", "Büro PC"])["name"], "Büro PC")
        self.assertEqual(self.add(line=key(seed=3))["name"], "")
        self.assertEqual(self.add(line=key(seed=4, comment="a b   c"))["name"], "a b c")
        self.assertEqual(self.add_refused(key(seed=5), "--name", "x" * 101)["error"], "invalid_argument")
        self.assertEqual(self.add_refused(key(seed=5), "--name", " lead")["error"], "invalid_argument")
        self.assertEqual([k["name"] for k in self.json("ssh", "work")["keys"]], ["laptop", "Büro PC", "", "a b c"])

    def test_from_a_file(self):
        path = os.path.join(self.dir, "id_ed25519.pub")
        with open(path, "w") as f:
            f.write(LAPTOP + "\n")
        result = self.json("add-ssh-key", "work", path)
        self.assertEqual((result["fingerprint"], result["name"]), (LAPTOP_FP, "laptop"))
        code, out, err = self.run_agentbox("add-ssh-key", "work", os.path.join(self.dir, "missing.pub"))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("missing.pub: No such file or directory", err)

    def test_usage(self):
        for args in ((), ("work", "a", "b")):
            with self.subTest(args=args):
                self.assertEqual(self.refused("add-ssh-key", *args),
                                 "agentbox: add-ssh-key <vm> [--name N] [FILE] [--json]\n")
        self.assertEqual(self.refused("add-ssh-key", "nope"), "agentbox: No such VM: nope\n")


class SetSshTest(SshTest):
    def test_on_makes_the_host_key_and_the_ssh_port(self):
        self.add()
        result = self.json("set-ssh", "work", "on")
        self.assertEqual(result.pop("message"),
                         "work: SSH access on, port 2201 (ssh -p 2201 agent@127.0.0.1); the VM gets it from its next "
                         "Restart (agentbox restart work) or Reset")
        self.assertEqual(result, {"vm": "work", "enabled": True, "port": 2201, "keys": 1, "changed": True,
                                  "state": "running", "applies": "next_restart"})
        # the host key: once, root and QEMU only, OpenSSH's own format
        host_key = self.read(HOST_KEY.format("work"))
        self.assertEqual(self.mode(HOST_KEY.format("work")), 0o640)
        self.assertTrue(host_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----\n"), host_key[:40])
        # the file the VM reads: the host key and the keys, without comments
        self.assertEqual(self.read(SSH_FILE.format("work")),
                         f"host_key {base64.b64encode(host_key.encode()).decode()}\n"
                         f"key {LAPTOP.rsplit(' ', 1)[0]}\n")
        # the SSH port on the loopback address; each connection wakes the VM, then goes to port 22
        self.assertEqual(self.read(SOCKET.format("work")),
                         "[Unit]\nDescription=SSH access to VM work\n\n[Socket]\nListenStream=127.0.0.1:2201\n"
                         "Accept=yes\nMaxConnections=32\n\n[Install]\nWantedBy=sockets.target\n")
        service = self.read(SERVICE.format("work"))
        self.assertIn('\nExecStartPre=/usr/bin/agentbox "--actor=SSH (${REMOTE_ADDR})" wake work\n'
                      "ExecStart=/usr/bin/socat STDIO TCP:10.77.1.2:22\n", service)
        self.assertIn("StandardInput=socket\nStandardOutput=socket\n", service)
        self.assertEqual(self.systemctl(), [["daemon-reload"], ["enable", "--now", "agentbox-ssh-work.socket"]])
        # nothing of the VM stops or starts now; priv has no SSH port
        self.assertEqual(self.virsh("shutdown", "destroy", "start", "managedsave"), [])
        self.assertFalse(self.exists(SOCKET.format("priv")))
        # regenerate finds everything as it should be, and keeps the socket listening
        self.forget_calls()
        self.assertEqual(self.json("regenerate")["vms"], {"work": [], "priv": []})
        self.assertIn(["enable", "--now", "agentbox-proxy-work.socket", "agentbox-ghvm-work.socket",
                       "agentbox-gui-work.socket", "agentbox-ssh-work.socket", "agentbox-proxy-priv.socket",
                       "agentbox-ghvm-priv.socket", "agentbox-gui-priv.socket"], self.systemctl())

    def test_the_host_key_is_an_ed25519_key_ssh_reads(self):
        self.ok("set-ssh", "work", "on")
        shown = self.json("ssh", "work")
        data = self.read(HOST_KEY.format("work")).encode()
        public = sshkeys.public_line(sshkeys.host_public(data))
        self.assertTrue(public.startswith("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI"), public)
        self.assertEqual(shown["known_hosts"], f"[127.0.0.1]:2201 {public}")
        self.assertEqual(shown["host_key_fingerprint"], fingerprint(public))
        if shutil.which("ssh-keygen"):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "host_key")
                with open(path, "wb") as f:
                    f.write(data)
                os.chmod(path, 0o600)
                r = subprocess.run(["ssh-keygen", "-y", "-f", path], capture_output=True, text=True, timeout=30)
                self.assertEqual(r.stdout.split()[:2], public.split(), r.stderr)

    def test_a_vm_that_is_off_gets_it_at_its_next_start(self):
        self.turned_off("priv")
        self.add("priv")
        result = self.json("set-ssh", "priv", "on")
        self.assertEqual((result["state"], result["applies"]), ("shut off", "next_start"))
        self.assertTrue(result["message"].endswith("; the VM gets it at its next start"), result["message"])
        self.assertEqual(self.virsh("start"), [])

    def test_without_keys_it_says_so(self):
        result = self.json("set-ssh", "work", "on")
        self.assertEqual(result["message"], "work: SSH access on, port 2201 (ssh -p 2201 agent@127.0.0.1); no SSH key "
                                            "yet (agentbox add-ssh-key work FILE)")
        self.assertEqual(self.read(SSH_FILE.format("work")).count("\n"), 1)

    def test_off_closes_the_port_and_keeps_keys_and_host_key(self):
        self.add()
        self.ok("set-ssh", "work", "on")
        host_key = self.read(HOST_KEY.format("work"))
        self.forget_calls()
        result = self.json("set-ssh", "work", "off")
        self.assertEqual(result["message"], "work: SSH access off, port 2201 closed, its SSH keys stay; sshd in the VM "
                                            "runs until its next Restart (agentbox restart work) or Reset")
        self.assertEqual((result["enabled"], result["keys"], result["applies"]), (False, 1, "next_restart"))
        self.assertEqual(self.systemctl(), [["disable", "--now", "agentbox-ssh-work.socket"], ["daemon-reload"]])
        self.assertFalse(self.exists(SOCKET.format("work")))
        self.assertFalse(self.exists(SERVICE.format("work")))
        self.assertEqual(self.read(SSH_FILE.format("work")), "none\n")
        self.assertIs(self.record()["ssh_access"], False)
        self.assertEqual(len(self.record()["ssh_keys"]), 1)
        # on again: the same host key, the keys are back
        self.ok("set-ssh", "work", "on")
        self.assertEqual(self.read(HOST_KEY.format("work")), host_key)
        self.assertIn(f"key {LAPTOP.rsplit(' ', 1)[0]}\n", self.read(SSH_FILE.format("work")))
        self.assertEqual(self.json("regenerate")["vms"], {"work": [], "priv": []})

    def test_the_same_again_changes_nothing_and_records_nothing(self):
        self.ok("set-ssh", "work", "on")
        self.journald.entries()
        self.journald.datagrams.clear()
        before = self.files()
        self.assertEqual(self.ok("set-ssh", "work", "on"), "work: SSH access stays on\n")
        self.assertEqual(self.ok("set-ssh", "priv", "off"), "priv: SSH access stays off\n")
        self.assertEqual(self.files(), before)
        self.assertNotIn("ssh_access", self.record("priv"))
        self.assertEqual(self.activity(), [])

    def test_the_ssh_port_listens_on_loopback_in_network_mode_too(self):
        """Until the firewall rule of the SSH ports exists (#130), nothing beyond the Host reaches it."""
        vm_records.write_host_setup(self.root)
        self.ok("set-ssh", "work", "on")
        self.assertIn("\nListenStream=127.0.0.1:2201\n", self.read(SOCKET.format("work")))
        self.assertIn("\nListenStream=6801\n", self.read("etc/systemd/system/agentbox-gui-work.socket"))

    def test_regenerate_makes_the_units_only_while_ssh_access_is_on(self):
        rec = self.record()
        vm_records.write(self.root, {**rec, "ssh_access": True}, self.record("priv"))
        # a record turned on by hand: regenerate makes the missing host key and the SSH port
        self.assertEqual(self.json("regenerate")["vms"], {"work": ["units", "ssh-host-key"], "priv": []})
        self.assertTrue(self.exists(HOST_KEY.format("work")))
        self.assertTrue(self.exists(SOCKET.format("work")))
        self.assertTrue(self.read(SSH_FILE.format("work")).startswith("host_key "))
        vm_records.write(self.root, rec, self.record("priv"))
        self.forget_calls()
        self.assertEqual(self.json("regenerate")["vms"], {"work": ["units", "vm-ssh"], "priv": []})
        self.assertFalse(self.exists(SOCKET.format("work")))
        self.assertIn(["disable", "--now", "agentbox-ssh-work.socket"], self.systemctl())
        self.assertTrue(self.exists(HOST_KEY.format("work")))

    def test_the_host_key_stays_across_a_reset(self):
        self.ok("set-ssh", "work", "on")
        host_key = self.read(HOST_KEY.format("work"))
        self.ok("reset", "work")
        self.ok("regenerate")
        self.assertEqual(self.read(HOST_KEY.format("work")), host_key)

    def test_a_domain_from_before_ssh_access_is_defined_again(self):
        saved = self.path("var/lib/agentbox/domains/work.xml")
        with open(saved) as f:
            xml = f.read()
        older = re.sub(r",entry5\.name=opt/agentbox/ssh,entry5\.file=[^\"]*?(?=\")", "", xml)
        self.assertNotEqual(older, xml)
        with open(saved, "w") as f:
            f.write(older)
        self.ok("set-ssh", "work", "on")
        [defined] = [c["xml"] for c in self.virsh("define") if c["dom"] == "agentbox-work"]
        self.assertIn("entry5.name=opt/agentbox/ssh", defined)

    def test_a_failing_definition_keeps_the_change_and_says_how_to_finish(self):
        self.scenario(virt_install_fails=["agentbox-work"])
        code, out, err = self.run_agentbox("set-ssh", "work", "on", "--json")
        self.assertEqual((code, out), (1, ""))
        said = json.loads(err)
        self.assertEqual((said["error"], said["params"]), ("action_failed", {"vm": "work"}))
        self.assertTrue(said["message"].startswith("work: the change is saved, but not applied: "), said["message"])
        self.assertTrue(said["message"].endswith("; agentbox regenerate finishes it"), said["message"])
        self.assertIs(self.record()["ssh_access"], True)
        self.assertEqual([(a, o) for a, _, o, _ in self.activity()], [("ssh.enabled", "failed")])
        self.scenario()
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["units"])
        self.assertTrue(self.exists(SOCKET.format("work")))

    def test_usage(self):
        for args in ((), ("work",), ("work", "yes"), ("work", "on", "off")):
            with self.subTest(args=args):
                self.assertEqual(self.refused("set-ssh", *args), "agentbox: set-ssh <vm> on|off [--json]\n")
        self.assertEqual(self.refused("set-ssh", "nope", "on"), "agentbox: No such VM: nope\n")

    def test_each_waits_for_the_registry_the_record_and_the_lifecycle_lock(self):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        for lock, args, field in (("vms.lock", ("set-ssh", "work", "on"), "ssh_access"),
                                  ("work.json.lock", ("add-ssh-key", "work"), "ssh_keys"),
                                  ("lifecycle-work.lock", ("set-ssh", "work", "off"), "ssh_access")):
            with self.subTest(lock=lock):
                before = self.record().get(field)
                path = self.path(f"var/lib/agentbox/locks/{lock}")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                p = subprocess.Popen([AGENTBOX, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=env)
                try:
                    p.stdin.write(LAPTOP + "\n")
                    p.stdin.close()
                    time.sleep(1)
                    self.assertIsNone(p.poll())
                    self.assertEqual(self.record().get(field), before)
                finally:
                    os.close(fd)
                _, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, err)
        self.assertEqual((self.record()["ssh_access"], len(self.record()["ssh_keys"])), (False, 1))


class AddWhileOffTest(SshTest):
    def test_a_key_added_without_ssh_access_waits_for_it(self):
        result = self.add()
        self.assertEqual(result.pop("message"), f"work: SSH key {LAPTOP_FP} (laptop) added; SSH access is off "
                                                "(agentbox set-ssh work on)")
        self.assertEqual(result, {"vm": "work", "fingerprint": LAPTOP_FP, "name": "laptop", "type": "ssh-ed25519",
                                  "enabled": False, "keys": 1, "state": "running", "applies": "next_restart"})
        # the host key is made with the first key; the VM still learns it has no SSH access
        self.assertTrue(self.exists(HOST_KEY.format("work")))
        self.assertEqual(self.read(SSH_FILE.format("work")), "none\n")
        self.assertFalse(self.exists(SOCKET.format("work")))

    def test_a_key_added_with_ssh_access_goes_into_the_file(self):
        self.ok("set-ssh", "work", "on")
        result = self.add(line=key(seed=7, comment="phone"))
        self.assertEqual(result["message"], f"work: SSH key {fingerprint(key(seed=7))} (phone) added; the VM gets it "
                                            "from its next Restart (agentbox restart work) or Reset")
        self.assertTrue(self.read(SSH_FILE.format("work")).endswith(f"key {key(seed=7)}\n"))


class ShowTest(SshTest):
    def test_text_and_json(self):
        self.assertEqual(self.ok("ssh", "work"), "work: SSH access off, port 2201 (agentbox set-ssh work on)\n"
                                                 "no SSH key (agentbox add-ssh-key work FILE)\n")
        self.add()
        self.ok("set-ssh", "work", "on")
        shown = self.json("ssh", "work")
        [k] = shown["keys"]
        self.assertEqual({n: k[n] for n in ("name", "type", "fingerprint")},
                         {"name": "laptop", "type": "ssh-ed25519", "fingerprint": LAPTOP_FP})
        self.assertEqual(set(k), {"name", "type", "fingerprint", "added", "added_by"})
        self.assertEqual((shown["enabled"], shown["port"]), (True, 2201))
        lines = self.ok("ssh", "work").splitlines()
        self.assertEqual(lines[0], "work: SSH access on, port 2201 (ssh -p 2201 agent@127.0.0.1)")
        self.assertEqual(lines[1], f"host key: {shown['host_key_fingerprint']}")
        self.assertEqual(lines[2], f"known_hosts: {shown['known_hosts']}")
        self.assertTrue(lines[3].startswith(f"SSH key: ssh-ed25519 {LAPTOP_FP} laptop, added "), lines[3])
        self.assertEqual(self.json("ssh", "priv")["port"], 2202)
        self.assertEqual(self.refused("ssh"), "agentbox: ssh <vm> [--json]\n")


class ActivityTest(SshTest):
    def test_changes_are_recorded_with_fingerprint_and_name(self):
        self.ok("--actor", "Firefox (192.0.2.4)", "set-ssh", "work", "on")
        self.add()
        self.ok("set-ssh", "work", "off")
        entries = self.activity()
        self.assertEqual(entries, [("ssh.enabled", "work", "ok", {}),
                                   ("ssh.key-added", "work", "ok", {"fingerprint": LAPTOP_FP, "name": "laptop"}),
                                   ("ssh.disabled", "work", "ok", {})])

    def test_failures_are_recorded(self):
        self.add_refused(key("ssh-rsa", bits=2048))
        self.add()
        self.add_refused(LAPTOP)
        self.assertEqual([(a, o) for a, _, o, _ in self.activity()],
                         [("ssh.key-added", "failed"), ("ssh.key-added", "ok"), ("ssh.key-added", "failed")])

    def test_the_actor_is_who_caused_it(self):
        self.ok("--actor", "Firefox (192.0.2.4)", "set-ssh", "work", "on")
        [entry] = self.journald.entries()
        self.assertEqual((entry["AGENTBOX_ACTOR"], entry["AGENTBOX_KIND"]), ("Firefox (192.0.2.4)", "action"))


class CreateDeleteTest(SshTest):
    def test_delete_removes_keys_host_key_and_port_also_when_the_data_disk_is_archived(self):
        self.ok("create", "lab")
        self.add("lab")
        self.ok("set-ssh", "lab", "on")
        for rel in (HOST_KEY, SSH_FILE, SOCKET, SERVICE):
            self.assertTrue(self.exists(rel.format("lab")), rel)
        self.forget_calls()
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertEqual(len(self.archive()), 1)
        for rel in (HOST_KEY, SSH_FILE, SOCKET, SERVICE):
            self.assertFalse(self.exists(rel.format("lab")), rel)
        self.assertFalse(self.exists("etc/agentbox/vms/lab.json"))
        self.assertIn(["stop", "agentbox-gui-lab@*.service", "agentbox-ssh-lab@*.service",
                       "agentbox-ghvm-lab@*.service"], self.systemctl())
        self.assertIn("agentbox-ssh-lab.socket", self.systemctl()[0])

    def test_create_refuses_what_an_earlier_vm_left(self):
        for rel, what in ((HOST_KEY, "SSH host key"), (SSH_FILE, "SSH access"), (SOCKET, "units")):
            with self.subTest(what):
                path = self.path(rel.format("lab"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write("left\n")
                self.assertEqual(self.refused("create", "lab"),
                                 f"agentbox: lab: leftovers of an earlier VM exist ({what}), remove them first\n")
                os.remove(path)
        self.ok("create", "lab")
        self.assertEqual(self.read(SSH_FILE.format("lab")), "none\n")


# --- in the VM ----------------------------------------------------------------------------------

class GuestTest(unittest.TestCase):
    """fwcfg-ssh, which guest-setup runs at the end of every boot: the host key and the SSH keys
    into the root-owned directory sshd's configuration names, and sshd only with SSH access and a
    key."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.entry, self.out = os.path.join(self.dir, "raw"), os.path.join(self.dir, "run", "ssh")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.selinux(False)
        self.fake("systemctl", 'echo "systemctl $*" >> "$CALLS"\n')
        self.fake("chcon", 'echo "chcon $*" >> "$CALLS"\n')
        self.host_key = sshkeys.host_key_file(bytes(32), bytes(range(32)), "work.agentbox")

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def selinux(self, on):
        self.fake("selinuxenabled", f"exit {0 if on else 1}\n")

    def run_guest(self, lines=None, code=0):
        if lines is not None:
            with open(self.entry, "w") as f:
                f.write("".join(line + "\n" for line in lines))
        r = subprocess.run([FWCFG_SSH, self.entry, self.out], capture_output=True, text=True, timeout=30,
                           env={"PATH": self.bin + ":" + os.environ["PATH"], "LANG": "C.UTF-8",
                                "CALLS": os.path.join(self.dir, "calls")})
        self.assertEqual(r.returncode, code, r.stderr)
        return r.stderr

    def calls(self):
        try:
            with open(os.path.join(self.dir, "calls")) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def files(self):
        return sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []

    def entry_lines(self, *keys):
        return [f"host_key {base64.b64encode(self.host_key).decode()}", *(f"key {k}" for k in keys)]

    def test_with_ssh_access_and_keys_sshd_starts(self):
        err = self.run_guest(self.entry_lines(key(seed=1), key("ssh-rsa", seed=2)))
        self.assertEqual(self.files(), ["authorized_keys", "host_key"])
        with open(os.path.join(self.out, "host_key"), "rb") as f:
            self.assertEqual(f.read(), self.host_key)
        with open(os.path.join(self.out, "authorized_keys")) as f:
            self.assertEqual(f.read(), f"{key(seed=1)}\n{key('ssh-rsa', seed=2)}\n")
        modes = {n: os.stat(os.path.join(self.out, n)).st_mode & 0o7777 for n in self.files()}
        self.assertEqual(modes, {"host_key": 0o600, "authorized_keys": 0o644})
        self.assertEqual(os.stat(self.out).st_mode & 0o7777, 0o755)
        self.assertEqual(self.calls(), ["systemctl start --no-block sshd.service"])
        self.assertIn("SSH access on with 2 SSH key(s), sshd started", err)

    def test_without_ssh_access_sshd_stays_off_and_nothing_is_left(self):
        self.run_guest(self.entry_lines(key()))
        self.assertEqual(self.files(), ["authorized_keys", "host_key"])
        os.remove(os.path.join(self.dir, "calls"))
        for lines in (["none"], None):
            with self.subTest(lines=lines):
                if lines is None and os.path.exists(self.entry):
                    os.remove(self.entry)               # a domain from before SSH access has no entry
                self.assertIn("no SSH access, sshd stays off", self.run_guest(lines))
                self.assertEqual(self.files(), [])
                self.assertEqual(self.calls(), [])

    def test_ssh_access_without_a_key_starts_no_sshd(self):
        err = self.run_guest(self.entry_lines())
        self.assertIn("SSH access on, but no SSH key: sshd stays off", err)
        self.assertEqual(self.calls(), [])

    def test_bad_lines_are_left_out_with_a_message(self):
        ed = key().split()[1]
        err = self.run_guest(self.entry_lines(key(seed=3)) + [
            f'key command="x" ssh-ed25519 {ed}', f"key ssh-dss {ed}", f"key ssh-rsa {ed}", "key ssh-ed25519 !!!",
            "key ssh-ed25519", "junk", f"host_key {base64.b64encode(b'not a key').decode()}"])
        with open(os.path.join(self.out, "authorized_keys")) as f:
            self.assertEqual(f.read(), f"{key(seed=3)}\n")
        self.assertEqual(len([line for line in err.splitlines() if "left out" in line]), 7, err)
        self.assertEqual(self.calls(), ["systemctl start --no-block sshd.service"])
        # without a host key there is no SSH access
        self.run_guest([f"key {key()}"])
        self.assertEqual(self.files(), [])

    def test_at_most_16_keys(self):
        self.run_guest(self.entry_lines(*(key(seed=i) for i in range(20))))
        with open(os.path.join(self.out, "authorized_keys")) as f:
            self.assertEqual(len(f.read().splitlines()), 16)

    def test_selinux_labels_where_selinux_is_on(self):
        self.selinux(True)
        self.run_guest(self.entry_lines(key()))
        self.assertEqual(self.calls(), [f"chcon -t sshd_key_t {self.out}/host_key",
                                        f"chcon -t ssh_home_t {self.out}/authorized_keys",
                                        "systemctl start --no-block sshd.service"])

    def test_guest_setup_runs_it_last_and_goes_on_without_it(self):
        with open(GUEST_SETUP) as f:
            code = [line for line in f.read().replace("\\\n", " ").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        [call] = [i for i, line in enumerate(code) if line.lstrip().startswith("/usr/lib/agentbox/fwcfg-ssh ")]
        self.assertEqual(code[call].split()[1:3],
                         ["/sys/firmware/qemu_fw_cfg/by_name/opt/agentbox/ssh/raw", "/run/agentbox/ssh"])
        self.assertIn("|| echo", code[call])
        # after the home is set up and everything else; only the closing message follows
        self.assertEqual([line.split()[0] for line in code[call + 1:]], ["echo"])
        self.assertNotIn("sshd", "\n".join(code[:call]))


if __name__ == "__main__":
    unittest.main()
