"""SSH access to a VM through its SSH port (#129, #130, #131, ADR 0020).

`agentbox set-ssh`, `add-ssh-key`, `remove-ssh-key` and `ssh` keep SSH access and the SSH keys in the VM's record,
the Host makes the VM's host key once, `regenerate` and the commands write the fw_cfg file the
VM reads at boot and the units of the SSH port (2200 + network number) while SSH access is on:
on the loopback address in Local-only, on every address behind the firewall rules of the home
network in Network mode (the fake firewall-cmd). A running VM takes each change at once through
agentbox-ssh, which the fake virsh runs for real in the VM's own root; a paused one when it
resumes; removing a key and turning SSH access off end every SSH session.
The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and the fakes of
test_registry.py (openssl is the real one, as in test_github_app.py); the guest side
(vm/files/usr/lib/agentbox/fwcfg-ssh, which guest-setup runs, and the wrapper agentbox-ssh)
against an fw_cfg file or stdin in a temporary directory.

Run from the repo root: python3 -m unittest discover -s tests
"""
import base64
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import socket
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
PUBLIC_KEY = "etc/agentbox/ssh-host-keys/{}.pub"
SSH_FILE = "var/lib/agentbox/vm-ssh/{}.ssh"
SOCKET = "etc/systemd/system/agentbox-ssh-{}.socket"
SERVICE = "etc/systemd/system/agentbox-ssh-{}@.service"
FWCFG_SSH = os.path.join(REPO, "vm", "files", "usr", "lib", "agentbox", "fwcfg-ssh")
GUEST_SETUP = os.path.join(REPO, "vm", "files", "usr", "lib", "agentbox", "guest-setup")
# the guest-agent wrapper that hands a change to a running VM, as the VM has it and in the repo
WRAPPER = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-ssh"
AGENTBOX_SSH = os.path.join(REPO, "vm", "files", WRAPPER.lstrip("/"))
PENDING = "var/lib/agentbox/pending/ssh-{}"
DONE = "guest-setup: context work, Orca port 6801, the Host at 10.77.1.1\n"     # its last line
# a key and its fingerprint as `ssh-keygen -lf` gives it
LAPTOP = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f laptop"
LAPTOP_FP = "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA"
# the firewall rules of the SSH ports in Network mode (the home network of vm_records.NETWORK_HOST):
# the same zone and priorities as those of the Orca ports
SSH_RULES = ['rule priority="-110" family="ipv4" source address="192.0.2.0/24" port port="2201-2454" protocol="tcp" '
             'accept',
             'rule priority="-100" family="ipv4" port port="2201-2454" protocol="tcp" reject',
             'rule priority="-100" family="ipv6" port port="2201-2454" protocol="tcp" reject']


def ssh_string(data):
    return struct.pack(">I", len(data)) + data


# the base point of each curve (FIPS 186-4, D.1.2), for ECDSA keys that are points on it
GENERATOR = {
    "nistp256": (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
                 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5),
    "nistp384": (0xaa87ca22be8b05378eb1c71ef320ad746e1d3b628ba79b9859f741e082542a385502f25dbf55296c3a545e3872760ab7,
                 0x3617de4a96262c6f5d9e98bf9292dc29f8f41dbd289a147ce9da3113b5f0b8c00a60b1ce1d7e819d7a431d7c90ea0e5f),
    "nistp521": (0xc6858e06b70404e9cd9e3ecb662395b4429c648139053fb521f828af606b4d3dbaa14b5e77efe75928fe1dc127a2ffa8de3348b3c1856a429bf97e7e31c2e5bd66,
                 0x11839296a789a3bc0045c8a5fb42c7d1bd998f54449579b446817afbd17273e662c97ee72995ef42640c550b9013fad0761353c7086a272c24088be94769fd16650),
}


def ec_point(curve, k):
    """k times the curve's base point, uncompressed (0x04, x, y): a public key OpenSSH takes."""
    p = sshkeys.CURVES[curve][0]

    def plus(a, b):
        if a is None or b is None:
            return a or b
        if a[0] == b[0] and (a[1] + b[1]) % p == 0:
            return None
        slope = ((3 * a[0] * a[0] - 3) * pow(2 * a[1], -1, p) if a == b
                 else (b[1] - a[1]) * pow(b[0] - a[0], -1, p)) % p
        x = (slope * slope - a[0] - b[0]) % p
        return x, (slope * (a[0] - x) - a[1]) % p

    point, step = None, GENERATOR[curve]
    while k:
        if k & 1:
            point = plus(point, step)
        step, k = plus(step, step), k >> 1
    size = (p.bit_length() + 7) // 8
    return b"\x04" + point[0].to_bytes(size, "big") + point[1].to_bytes(size, "big")


def line_of(kind, *parts):
    blob = ssh_string(kind.encode()) + b"".join(ssh_string(p) for p in parts)
    return f"{kind} {base64.b64encode(blob).decode()}"


def key(kind="ssh-ed25519", seed=1, bits=3072, comment="", application=b"ssh:"):
    """A public key line of this type (made up, of the right form; an ECDSA key a point on its
    curve)."""
    body = hashlib.sha512(str(seed).encode()).digest() * 8
    if kind == "ssh-rsa":
        n = int.from_bytes(body[:bits // 8], "big") | (1 << (bits - 1)) | 1
        parts = [b"\x01\x00\x01", n.to_bytes(bits // 8 + 1, "big")]
    elif kind in ("ssh-ed25519", "sk-ssh-ed25519@openssh.com"):
        parts = [body[:32]] + ([application] if kind.startswith("sk-") else [])
    elif kind == "ssh-dss":
        parts = [body[:128], body[:20], body[:128], body[:128]]
    else:
        curve = kind.split("@")[0].rsplit("-", 1)[1]
        parts = [curve.encode(), ec_point(curve, int.from_bytes(body[:32], "big"))] + (
            [application] if kind.startswith("sk-") else [])
    return line_of(kind, *parts) + (f" {comment}" if comment else "")


def ssh_parts(line):
    """The SSH wire strings of a key line's blob."""
    blob, parts = base64.b64decode(line.split()[1]), []
    while blob:
        n = struct.unpack(">I", blob[:4])[0]
        parts.append(blob[4:4 + n])
        blob = blob[4 + n:]
    return parts


def long_key(seed):
    """A security key whose application string makes its line ~14.8 KB long: four fit into the
    file the VM reads, a fifth does not."""
    return key("sk-ssh-ed25519@openssh.com", seed=seed, application=b"ssh:" + b"a" * 11000)


def fwcfg_ssh():
    """fwcfg-ssh as a module, for its limits."""
    loader = importlib.machinery.SourceFileLoader("fwcfg_ssh", FWCFG_SSH)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True    # no __pycache__ in vm/files
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
    return module


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
        return [(e["AGENTBOX_ACTION"], e.get("AGENTBOX_VM"), e["AGENTBOX_OUTCOME"],
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
                          "known_hosts": None, "keys": [], "pending": False})
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
        rsa_padded = line_of("ssh-rsa", b"\x00\x01\x00\x01", ssh_parts(key("ssh-rsa"))[2])
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
                 "bad name": {"ssh_keys": [{**good, "name": "a\nb"}]},
                 "not in its shortest form": {"ssh_keys": [{**good, "key": rsa_padded, "fingerprint": fingerprint(rsa_padded)}]},
                 "too long together": {"ssh_keys": [{**good, "key": long_key(i), "fingerprint": fingerprint(long_key(i))}
                                                    for i in range(5)]}}
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
            for kind in sshkeys.TYPES:
                with self.subTest(kind), tempfile.NamedTemporaryFile("w", suffix=".pub") as f:
                    f.write(key(kind) + "\n")
                    f.flush()
                    r = subprocess.run(["ssh-keygen", "-lf", f.name], capture_output=True, text=True, timeout=30)
                    self.assertEqual(r.stdout.split()[1], sshkeys.parse(key(kind))["fingerprint"])

    def test_keys_made_by_ssh_keygen_are_taken_with_its_fingerprint(self):
        if not shutil.which("ssh-keygen"):
            self.skipTest("no ssh-keygen")
        with tempfile.TemporaryDirectory() as tmp:
            for kind, bits in (("ed25519", None), ("ecdsa", 256), ("ecdsa", 384), ("ecdsa", 521), ("rsa", 3072)):
                with self.subTest(kind=kind, bits=bits):
                    path = os.path.join(tmp, f"{kind}{bits}")
                    subprocess.run(["ssh-keygen", "-q", "-t", kind, *(["-b", str(bits)] if bits else []), "-N", "",
                                    "-C", f"{kind}{bits}", "-f", path], check=True, capture_output=True, timeout=60)
                    shown = subprocess.run(["ssh-keygen", "-lf", path + ".pub"], capture_output=True, text=True,
                                           check=True, timeout=30).stdout.split()[1]
                    with open(path + ".pub") as f:
                        said = self.add(line=f.read().strip())
                    self.assertEqual((said["fingerprint"], said["name"]), (shown, f"{kind}{bits}"))

    def test_keys_openssh_would_refuse_are_refused(self):
        g = ec_point("nistp256", 1)
        off_curve = g[:-1] + bytes([g[-1] ^ 1])
        rsa = ssh_parts(key("ssh-rsa"))
        cases = {"ECDSA point of zeros": line_of("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + bytes(64)),
                 "ECDSA point off its curve": line_of("ecdsa-sha2-nistp256", b"nistp256", off_curve),
                 "security-key ECDSA point off its curve": line_of("sk-ecdsa-sha2-nistp256@openssh.com", b"nistp256",
                                                                   off_curve, b"ssh:"),
                 "ECDSA coordinate beyond p": line_of("ecdsa-sha2-nistp521", b"nistp521", b"\x04" + b"\xff" * 132),
                 "RSA exponent with a leading zero": line_of("ssh-rsa", b"\x00" + rsa[1], rsa[2]),
                 "RSA modulus with another leading zero": line_of("ssh-rsa", rsa[1], b"\x00" + rsa[2]),
                 "RSA modulus negative": line_of("ssh-rsa", rsa[1], rsa[2][1:]),
                 "RSA exponent zero": line_of("ssh-rsa", b"\x00", rsa[2])}
        for what, line in cases.items():
            with self.subTest(what):
                said = self.add_refused(line)
                self.assertEqual(said["error"], "ssh_key_invalid", said)
        said = self.add_refused(key("ssh-rsa", bits=16392))
        self.assertEqual((said["error"], said["message"]),
                         ("ssh_key_invalid", "not a valid SSH public key: RSA keys have at most 16384 bits, this one "
                                             "has 16392"))
        self.add(line=key("ssh-rsa", bits=16384))

    def test_one_key_has_one_encoding_so_it_is_no_duplicate(self):
        """A leading zero byte more would change the fingerprint, though not the key (ssh-keygen
        -lf shows the same for both)."""
        self.add(line=key("ssh-rsa"))
        rsa = ssh_parts(key("ssh-rsa"))
        self.assertEqual(self.add_refused(line_of("ssh-rsa", b"\x00" + rsa[1], rsa[2]))["error"], "ssh_key_invalid")
        self.assertEqual(len(self.record()["ssh_keys"]), 1)

    def test_the_keys_fit_into_the_file_the_vm_reads(self):
        self.ok("set-ssh", "work", "on")
        for i in range(4):
            self.add(line=long_key(i))
        said = self.add_refused(long_key(4))
        self.assertEqual((said["error"], said["params"]), ("ssh_keys_too_large", {"max_bytes": 64512}))
        self.assertTrue(said["message"].startswith("the SSH keys of a VM take at most 64512 bytes together in the "
                                                   "file the VM reads; with this one they would take "),
                        said["message"])
        self.assertEqual(len(self.record()["ssh_keys"]), 4)
        # what the VM reads stays within what it takes, with the longest host key
        with open(self.path(SSH_FILE.format("work")), "rb") as f:
            self.assertLessEqual(len(f.read()), fwcfg_ssh().MAX_ENTRY)
        longest = sshkeys.fw_cfg(True, [], sshkeys.host_key_file(bytes(32), bytes(32), "x" * 16 + ".agentbox"))
        self.assertLessEqual(len(longest), sshkeys.HOST_KEY_LINE)
        self.assertEqual(sshkeys.MAX_ENTRY, fwcfg_ssh().MAX_ENTRY)

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
    def test_on_refuses_a_port_another_service_holds(self):
        self.listening(tcp=[("0100007F:0899", "0A")])   # 127.0.0.1:2201
        code, out, err = self.run_input("set-ssh", "work", "on")
        self.assertNotEqual(code, 0)
        self.assertIn("port 2201 on 127.0.0.1 is taken: another service listens there", err)
        self.assertEqual(self.systemctl(), [])
        self.assertFalse(self.exists(SOCKET.format("work")))
        self.assertEqual(self.json("ssh", "work")["enabled"], False)

    def test_on_makes_the_host_key_and_the_ssh_port(self):
        self.add()
        result = self.json("set-ssh", "work", "on")
        self.assertEqual(result.pop("message"),
                         "work: SSH access on, port 2201 (ssh -p 2201 agent@127.0.0.1); it applies in the VM now")
        self.assertEqual(result, {"vm": "work", "enabled": True, "port": 2201, "keys": 1, "changed": True,
                                  "state": "running", "applies": "now"})
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
                      "ExecStart=/usr/bin/socat STDIO TCP:10.77.1.2:22,retry=20,interval=0.5\n", service)
        self.assertIn("StandardInput=socket\nStandardOutput=socket\n", service)
        # sshd may start a moment after Orca answers: the forward tries port 22 for 10 seconds
        self.assertEqual(re.findall(r"retry=(\d+),interval=([\d.]+)", service), [("20", "0.5")])
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
        self.assertEqual(shown["known_hosts"], f"work.agentbox {public}")
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
        self.assertEqual(result["message"], "work: SSH access off, port 2201 closed and every SSH session ended, its "
                                            "SSH keys stay")
        self.assertEqual((result["enabled"], result["keys"], result["applies"]), (False, 1, "now"))
        # the port closes, then every connection unit of it stops
        self.assertEqual(self.systemctl(), [["disable", "--now", "agentbox-ssh-work.socket"], ["daemon-reload"],
                                            ["stop", "agentbox-ssh-work@*.service"]])
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

    def test_off_closes_the_port_first_also_when_a_later_step_fails(self):
        self.add()
        self.ok("set-ssh", "work", "on")
        self.forget_calls()
        self.journald.entries()
        self.journald.datagrams.clear()
        self.scenario(virt_install_fails=["agentbox-work"])
        code, out, err = self.run_agentbox("set-ssh", "work", "off", "--json")
        self.assertEqual((code, out), (1, ""))
        said = json.loads(err)
        self.assertTrue(said["message"].startswith("work: the change is saved, but not applied: "), said["message"])
        self.assertIs(self.record()["ssh_access"], False)
        # no old key connects through the Host meanwhile
        self.assertEqual(self.systemctl()[0], ["disable", "--now", "agentbox-ssh-work.socket"])
        self.assertFalse(self.exists(SOCKET.format("work")))
        self.assertFalse(self.exists(SERVICE.format("work")))
        self.assertEqual(self.read(SSH_FILE.format("work")), "none\n")
        self.assertEqual([(a, o) for a, _, o, _ in self.activity()], [("ssh.disabled", "failed")])
        self.scenario()
        self.assertEqual(self.json("regenerate")["vms"]["work"], [])

    def test_the_same_again_failing_claims_no_saved_change(self):
        self.ok("set-ssh", "work", "on")
        self.scenario(virt_install_fails=["agentbox-work"])
        before = self.record()
        code, out, err = self.run_agentbox("set-ssh", "work", "on", "--json")
        self.assertEqual((code, out), (1, ""))
        said = json.loads(err)
        self.assertEqual((said["error"], said["params"]), ("action_failed", {"vm": "work"}))
        self.assertTrue(said["message"].startswith("work: nothing changed, but applying the VM's SSH access failed: "),
                        said["message"])
        self.assertNotIn("saved", said["message"])
        self.assertEqual(self.record(), before)

    def test_a_paused_vm_is_defined_once_it_has_no_pause(self):
        self.add()
        self.ok("pause", "work")
        saved = self.path("var/lib/agentbox/domains/work.xml")
        with open(saved) as f:
            xml = f.read()
        with open(saved, "w") as f:
            f.write(re.sub(r",entry5\.name=opt/agentbox/ssh,entry5\.file=[^\"]*?(?=\")", "", xml))
        self.forget_calls()
        result = self.json("set-ssh", "work", "on")
        self.assertEqual((result["changed"], result["state"], result["applies"]), (True, "paused", "on_resume"))
        self.assertEqual(result["message"],
                         "work: SSH access on, port 2201 (ssh -p 2201 agent@127.0.0.1); the VM gets it when it "
                         "resumes; it is defined again only once it no longer has a Pause (agentbox start work, then "
                         "agentbox regenerate)")
        said = self.add(line=key(seed=2, comment="phone"))
        self.assertTrue(said["message"].endswith("; the VM gets it when it resumes; it is defined again only once it "
                                                 "no longer has a Pause (agentbox start work, then agentbox "
                                                 "regenerate)"), said["message"])
        self.assertEqual(self.virsh("define", "start", "managedsave-remove", "guest-exec"), [])
        self.assertIs(self.record()["ssh_access"], True)
        self.assertTrue(self.exists(SOCKET.format("work")))
        self.ok("start", "work")
        # resumed from its Pause, it takes both changes at once
        self.assertEqual([c["path"] for c in self.virsh("guest-exec")], [WRAPPER])
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["domain"])
        [defined] = [c["xml"] for c in self.virsh("define") if c["dom"] == "agentbox-work"]
        self.assertIn("entry5.name=opt/agentbox/ssh", defined)

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


class NetworkModeTest(SshTest):
    """The SSH port in Network mode (#130): on every address like the Orca port, behind firewall
    rules in the default zone that accept the home network on 2201-2454 and reject everything
    else there, added by regenerate and by set-ssh before the port listens. Local-only has no
    rule. The fake firewall-cmd keeps the zones in firewall.json."""

    def rules(self, side="permanent"):
        """The rich rules of the default zone."""
        try:
            with open(os.path.join(self.dir, "firewall.json")) as f:
                return json.load(f)[side]["public"]["rich_rules"]
        except FileNotFoundError:
            return []

    def firewall_changes(self):
        """The firewall-cmd calls that change something: adding, removing, reloading."""
        return [c["argv"] for c in self.calls("firewall-cmd")
                if any(a.startswith(("--add-", "--remove-")) or a == "--reload" for a in c["argv"])]

    def index(self, cmd, argv):
        return next(i for i, c in enumerate(self.calls()) if c["cmd"] == cmd and c.get("argv") == argv)

    def test_the_ssh_port_listens_on_every_address_behind_the_rules_of_the_home_network(self):
        vm_records.write_host_setup(self.root)
        self.ok("set-ssh", "work", "on")
        self.assertIn("\nListenStream=2201\n", self.read(SOCKET.format("work")))
        for side in ("permanent", "runtime"):
            self.assertEqual(self.rules(side), SSH_RULES)
        # the rules are loaded before systemd reads the new socket and opens it
        reload = self.index("firewall-cmd", ["--reload"])
        self.assertLess(reload, self.index("systemctl", ["daemon-reload"]))
        self.assertLess(reload, self.index("systemctl", ["enable", "--now", "agentbox-ssh-work.socket"]))
        # the Orca port the same way, once regenerate applies the Host setup
        self.forget_calls()
        self.assertEqual(self.json("regenerate")["ssh_rules"], 0)
        self.assertIn("\nListenStream=6801\n", self.read("etc/systemd/system/agentbox-gui-work.socket"))
        self.assertEqual(self.firewall_changes(), [])

    def test_regenerate_adds_the_rules_without_setup_after_an_update(self):
        # a Host in Network mode as the Release before left it: the SSH port on the loopback
        # address and no rule for the SSH ports
        vm_records.write_host_setup(self.root)
        self.ok("set-ssh", "work", "on")
        self.write(SOCKET.format("work"), self.read(SOCKET.format("work")).replace(
            "ListenStream=2201", "ListenStream=127.0.0.1:2201"))
        with open(os.path.join(self.dir, "firewall.json")) as f:
            zones = json.load(f)
        for side in ("permanent", "runtime"):
            zones[side]["public"]["rich_rules"] = []
        with open(os.path.join(self.dir, "firewall.json"), "w") as f:
            json.dump(zones, f)
        self.forget_calls()
        code, out, err = self.run_agentbox("regenerate")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("3 firewall rules of the SSH ports added for the home network\n", out)
        self.assertIn("work: changed sockets restarted on their new addresses\n", out)
        self.assertIn("\nListenStream=2201\n", self.read(SOCKET.format("work")))
        for side in ("permanent", "runtime"):
            self.assertEqual(self.rules(side), SSH_RULES)
        self.assertEqual(sorted(self.firewall_changes()), sorted(
            [["--permanent", "--zone=public", f"--add-rich-rule={r}"] for r in SSH_RULES] + [["--reload"]]))
        # the port moves to every address only after the rules are loaded
        self.assertLess(self.index("firewall-cmd", ["--reload"]),
                        self.index("systemctl", ["restart", "agentbox-ssh-work.socket"]))
        # nothing changes the firewall when nothing changed: regenerate and set-ssh only ask
        self.forget_calls()
        self.assertEqual(self.json("regenerate")["ssh_rules"], 0)
        self.assertEqual(self.ok("set-ssh", "work", "on"), "work: SSH access stays on\n")
        self.assertEqual(self.firewall_changes(), [])

    def test_the_rules_come_from_the_host_setup_also_without_ssh_access(self):
        vm_records.write_host_setup(self.root)
        self.assertEqual(self.json("regenerate")["ssh_rules"], 3)
        self.assertEqual(self.rules(), SSH_RULES)
        self.assertFalse(self.exists(SOCKET.format("work")))

    def test_local_only_has_no_rule_and_asks_the_firewall_nothing(self):
        self.ok("set-ssh", "work", "on")
        self.assertEqual(self.json("regenerate")["ssh_rules"], 0)
        self.assertIn("\nListenStream=127.0.0.1:2201\n", self.read(SOCKET.format("work")))
        self.assertEqual(self.rules(), [])
        # only regenerate's questions about the zone of the VM networks
        self.assertEqual([c["argv"] for c in self.calls("firewall-cmd") if "--zone=libvirt" not in c["argv"]], [])

    def test_without_the_rules_the_ssh_port_does_not_open(self):
        vm_records.write_host_setup(self.root)
        self.add()
        self.scenario(firewall_down=True)
        self.forget_calls()
        code, out, err = self.run_input("set-ssh", "work", "on")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("work: the change is saved, but not applied: ", err)
        self.assertIn("agentbox regenerate finishes it", err)
        self.assertFalse(self.exists(SOCKET.format("work")))
        self.assertEqual(self.systemctl(), [])
        # regenerate stops there as well, before any unit is written
        code, _, err = self.run_agentbox("regenerate")
        self.assertEqual(code, 1)
        self.assertFalse(self.exists(SOCKET.format("work")))

    def test_a_port_another_service_holds_is_named_without_the_loopback_address(self):
        vm_records.write_host_setup(self.root)
        self.listening(tcp=[("00000000:0899", "0A")])   # 0.0.0.0:2201
        code, out, err = self.run_input("set-ssh", "work", "on")
        self.assertNotEqual(code, 0)
        self.assertIn("work: port 2201 is taken: another service listens there", err)
        self.assertEqual(self.firewall_changes(), [])


class AddWhileOffTest(SshTest):
    def test_a_key_added_without_ssh_access_waits_for_it(self):
        result = self.add()
        self.assertEqual(result.pop("message"), f"work: SSH key {LAPTOP_FP} (laptop) added; SSH access is off "
                                                "(agentbox set-ssh work on)")
        self.assertEqual(result, {"vm": "work", "fingerprint": LAPTOP_FP, "name": "laptop", "type": "ssh-ed25519",
                                  "enabled": False, "keys": 1, "state": "running", "applies": "now"})
        # the host key is made with the first key; the VM still learns it has no SSH access
        self.assertTrue(self.exists(HOST_KEY.format("work")))
        self.assertEqual(self.read(SSH_FILE.format("work")), "none\n")
        self.assertFalse(self.exists(SOCKET.format("work")))
        # nothing that reaches the VM changed, so the VM is not asked
        self.assertEqual(self.virsh("guest-exec"), [])

    def test_a_key_added_with_ssh_access_goes_into_the_file(self):
        self.ok("set-ssh", "work", "on")
        result = self.add(line=key(seed=7, comment="phone"))
        self.assertEqual(result["message"], f"work: SSH key {fingerprint(key(seed=7))} (phone) added; it applies in "
                                            "the VM now")
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


class SshConfigTest(SshTest):
    """agentbox ssh-config (#134): a ~/.ssh/config block per VM with SSH access, named
    <vm>.agentbox, and the known_hosts lines of the host keys under that HostKeyAlias. Root and
    the members of libvirt may run it; this user is in libvirt unless a test says otherwise."""

    def setUp(self):
        super().setUp()
        self.in_libvirt(True)

    def in_libvirt(self, yes):
        gid = os.getegid() if yes else max(os.getgroups() + [os.getegid()]) + 4711
        os.makedirs(self.path("etc"), exist_ok=True)
        with open(self.path("etc/group"), "w") as f:
            f.write(f"wheel:x:10:\nlibvirt:x:{gid}:\n")

    def public(self, vm="work"):
        """The host key's public line, from the secret the Host keeps."""
        return sshkeys.public_line(sshkeys.host_public(self.read(HOST_KEY.format(vm)).encode()))

    @staticmethod
    def block(vm, hostname, port):
        return (f"Host {vm}.agentbox\n    HostName {hostname}\n    Port {port}\n    User agent\n"
                f"    HostKeyAlias {vm}.agentbox\n    ForwardAgent no\n    ForwardX11 no\n"
                "    StrictHostKeyChecking yes\n    UserKnownHostsFile ~/.ssh/known_hosts.agentbox\n")

    def test_local_only_a_block_per_vm_with_ssh_access_and_its_line_under_the_alias(self):
        self.ok("set-ssh", "work", "on")
        self.ok("create", "lab")
        self.add("lab")
        self.ok("set-ssh", "lab", "on")
        # priv has no SSH access: left out
        self.assertEqual(self.ok("ssh-config"), self.block("work", "127.0.0.1", 2201) + "\n"
                         + self.block("lab", "127.0.0.1", 2203))
        self.assertEqual(self.ok("ssh-config", "--known-hosts"),
                         f"work.agentbox {self.public('work')}\nlab.agentbox {self.public('lab')}\n")
        result = self.json("ssh-config")
        self.assertEqual(result["mode"], "local")
        self.assertEqual([h["vm"] for h in result["hosts"]], ["work", "lab"])
        work = result["hosts"][0]
        self.assertEqual(work, {"vm": "work", "host": "work.agentbox",
                                "options": {"HostName": "127.0.0.1", "Port": 2201, "User": "agent",
                                            "HostKeyAlias": "work.agentbox", "ForwardAgent": "no",
                                            "ForwardX11": "no", "StrictHostKeyChecking": "yes",
                                            "UserKnownHostsFile": "~/.ssh/known_hosts.agentbox"},
                                "config": self.block("work", "127.0.0.1", 2201),
                                "known_hosts": f"work.agentbox {self.public('work')}"})
        # agentbox ssh gives the same line, under the alias as well
        self.assertEqual(self.json("ssh", "work")["known_hosts"], work["known_hosts"])
        self.assertIn(f"known_hosts: {work['known_hosts']}\n", self.ok("ssh", "work"))
        # read-only: no Activity entry of its own
        self.assertEqual(self.activity()[-1][0], "ssh.enabled")

    def test_network_mode_names_the_pairing_address_and_keeps_the_known_hosts_line(self):
        self.ok("set-ssh", "work", "on")
        local = self.json("ssh-config")["hosts"][0]
        vm_records.write_host_setup(self.root)
        self.ok("regenerate")
        self.assertEqual(self.ok("ssh-config"), self.block("work", "192.0.2.8", 2201))
        network = self.json("ssh-config")
        self.assertEqual(network["mode"], "network")
        # the switch changes HostName only: the known_hosts line stays as it was
        self.assertEqual(network["hosts"][0]["known_hosts"], local["known_hosts"])
        self.assertEqual({k: v for k, v in network["hosts"][0]["options"].items() if k != "HostName"},
                         {k: v for k, v in local["options"].items() if k != "HostName"})

    def test_ssh_takes_the_block_and_finds_the_line_under_the_alias(self):
        if not (shutil.which("ssh") and shutil.which("ssh-keygen")):
            self.skipTest("no OpenSSH client")
        self.ok("set-ssh", "work", "on")
        with tempfile.TemporaryDirectory() as tmp:
            config, known = os.path.join(tmp, "agentbox.conf"), os.path.join(tmp, "known_hosts.agentbox")
            with open(config, "w") as f:
                f.write(self.ok("ssh-config"))
            with open(known, "w") as f:
                f.write(self.ok("ssh-config", "--known-hosts"))
            r = subprocess.run(["ssh", "-G", "-F", config, "work.agentbox"], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            got = dict(line.split(" ", 1) for line in r.stdout.splitlines() if " " in line)
            self.assertEqual({k: got[k] for k in ("hostname", "port", "user", "hostkeyalias", "forwardagent",
                                                  "forwardx11", "stricthostkeychecking")},
                             {"hostname": "127.0.0.1", "port": "2201", "user": "agent",
                              "hostkeyalias": "work.agentbox", "forwardagent": "no", "forwardx11": "no",
                              "stricthostkeychecking": "true"})
            r = subprocess.run(["ssh-keygen", "-F", "work.agentbox", "-f", known], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(self.public(), r.stdout)

    def test_named_vms_only_and_one_without_ssh_access_left_out(self):
        self.ok("set-ssh", "work", "on")
        self.ok("create", "lab")
        self.ok("set-ssh", "lab", "on")
        self.assertEqual(self.ok("ssh-config", "lab"), self.block("lab", "127.0.0.1", 2203))
        code, out, err = self.run_agentbox("ssh-config", "priv")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "agentbox: priv has no SSH access (sudo agentbox set-ssh priv on)\n")
        self.assertEqual([h["vm"] for h in self.json("ssh-config", "priv", "work")["hosts"]], ["work"])
        code, out, err = self.run_agentbox("ssh-config", "nope", "--json")
        self.assertEqual((code, out, json.loads(err)["error"]), (1, "", "vm_not_found"))

    def test_without_any_ssh_access_it_says_so(self):
        code, out, err = self.run_agentbox("ssh-config")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "agentbox: no VM has SSH access (sudo agentbox set-ssh <vm> on)\n")
        self.assertEqual(self.json("ssh-config")["hosts"], [])

    def test_it_reads_only_what_everyone_may_read(self):
        """A member of libvirt cannot read the host key: its public half beside it is enough."""
        self.ok("set-ssh", "work", "on")
        self.assertEqual(self.read(PUBLIC_KEY.format("work")), self.public() + "\n")
        self.assertEqual((self.mode(PUBLIC_KEY.format("work")), self.mode("etc/agentbox/ssh-host-keys")),
                         (0o644, 0o755))
        line = f"work.agentbox {self.public()}"
        os.chmod(self.path("etc/agentbox/secrets"), 0o000)
        try:
            self.assertEqual(self.ok("ssh-config", "--known-hosts"), line + "\n")
        finally:
            os.chmod(self.path("etc/agentbox/secrets"), 0o750)
        # a host key made before its public half was kept: regenerate writes it
        os.remove(self.path(PUBLIC_KEY.format("work")))
        code, out, err = self.run_agentbox("ssh-config", "--known-hosts")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("work has no host key yet (sudo agentbox regenerate)", err)
        self.ok("regenerate")
        self.assertEqual(self.ok("ssh-config", "--known-hosts"), line + "\n")

    def test_no_public_host_key_yet_leaves_known_hosts_empty_and_a_garbled_one_fails(self):
        self.ok("set-ssh", "work", "on")
        pub = self.path(PUBLIC_KEY.format("work"))
        os.remove(pub)
        code, out, err = self.run_agentbox("ssh-config")
        self.assertEqual((code, out), (0, self.block("work", "127.0.0.1", 2201)))
        self.assertIn("work has no host key yet (sudo agentbox regenerate)", err)
        result = self.json("ssh-config")
        self.assertIsNone(result["hosts"][0]["known_hosts"])
        self.assertEqual(result["hosts"][0]["config"], self.block("work", "127.0.0.1", 2201))
        with open(pub, "w") as f:
            f.write("this is not a key\n")
        for args in (("ssh-config",), ("ssh-config", "--json"), ("ssh-config", "--known-hosts")):
            code, out, err = self.run_agentbox(*args)
            self.assertEqual(code, 1, args)
            self.assertIn("not an SSH public key; agentbox regenerate writes it again", out + err)

    def test_only_root_and_the_members_of_libvirt(self):
        self.ok("set-ssh", "work", "on")
        self.in_libvirt(False)
        for args in (("ssh-config",), ("ssh-config", "--json")):
            code, out, err = self.run_agentbox(*args)
            self.assertEqual((code, out), (1, ""), err)
            self.assertIn("ssh-config is for root and the members of libvirt, who control every VM anyway: "
                          "sudo agentbox ssh-config", err)
        self.assertEqual(json.loads(err)["error"], "sudo_failed")
        os.remove(self.path("etc/group"))
        self.assertEqual(self.run_agentbox("ssh-config")[0], 1)
        self.in_libvirt(True)
        self.assertTrue(self.ok("ssh-config").startswith("Host work.agentbox\n"))

    def test_usage(self):
        code, out, err = self.run_agentbox("ssh-config", "--bogus")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("unknown option: --bogus", err)


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

    def test_failures_before_the_change_are_recorded_too(self):
        """A wrong argument, an unknown VM, a broken record: each command records its own entries."""
        self.refused("set-ssh", "work", "yes")
        self.refused("set-ssh", "work", "off", "again")
        self.refused("set-ssh", "nope", "off")
        self.refused("add-ssh-key", "work", "a", "b")
        code, _, _ = self.run_input("add-ssh-key", "nope", stdin=LAPTOP)
        self.assertEqual(code, 1)
        vm_records.write(self.root, {**self.record(), "ssh_access": "yes"}, self.record("priv"))
        self.refused("set-ssh", "work", "on")
        code, _, _ = self.run_input("add-ssh-key", "work", stdin=LAPTOP)
        self.assertEqual(code, 1)
        self.assertEqual([(a, vm, o) for a, vm, o, _ in self.activity()],
                         [("ssh.enabled", "work", "failed"), ("ssh.disabled", "work", "failed"),
                          ("ssh.disabled", None, "failed"), ("ssh.key-added", "work", "failed"),
                          ("ssh.key-added", None, "failed"), ("ssh.enabled", "work", "failed"),
                          ("ssh.key-added", "work", "failed")])

    def test_the_actor_is_who_caused_it(self):
        self.ok("--actor", "Firefox (192.0.2.4)", "set-ssh", "work", "on")
        [entry] = self.journald.entries()
        self.assertEqual((entry["AGENTBOX_ACTOR"], entry["AGENTBOX_KIND"]), ("Firefox (192.0.2.4)", "action"))


class CreateDeleteTest(SshTest):
    def test_delete_removes_keys_host_key_and_port_also_when_the_data_disk_is_archived(self):
        self.ok("create", "lab")
        self.add("lab")
        self.ok("set-ssh", "lab", "on")
        for rel in (HOST_KEY, PUBLIC_KEY, SSH_FILE, SOCKET, SERVICE):
            self.assertTrue(self.exists(rel.format("lab")), rel)
        self.forget_calls()
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertEqual(len(self.archive()), 1)
        for rel in (HOST_KEY, PUBLIC_KEY, SSH_FILE, SOCKET, SERVICE):
            self.assertFalse(self.exists(rel.format("lab")), rel)
        self.assertFalse(self.exists("etc/agentbox/vms/lab.json"))
        self.assertIn(["stop", "agentbox-gui-lab@*.service", "agentbox-ssh-lab@*.service",
                       "agentbox-ghvm-lab@*.service"], self.systemctl())
        self.assertIn("agentbox-ssh-lab.socket", self.systemctl()[0])

    def test_create_refuses_what_an_earlier_vm_left(self):
        for rel, what in ((HOST_KEY, "SSH host key"), (PUBLIC_KEY, "SSH host key"), (SSH_FILE, "SSH access"),
                          (SOCKET, "units")):
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


# --- changes that apply at once (#131) ----------------------------------------------------------

class LiveTest(SshTest):
    """A running VM takes every change at once through agentbox-ssh (guest-exec of the fake virsh
    runs the real wrapper with the real fwcfg-ssh in the VM's own root); removing a key and
    turning SSH access off end every SSH session of the VM."""

    PHONE = key(seed=7, comment="phone")

    def guest(self, rel, vm="work"):
        return os.path.join(self.dir, f"guest-{vm}", rel)

    def authorized(self, vm="work"):
        """The VM's key file, None when it has none."""
        try:
            with open(self.guest("run/agentbox/ssh/authorized_keys", vm)) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def guest_systemctl(self, vm="work"):
        try:
            with open(self.guest("systemctl.log", vm)) as f:
                return [line for line in f.read().splitlines() if not line.startswith("is-active")]
        except FileNotFoundError:
            return []

    def applied(self):
        """The guest-exec calls of agentbox-ssh, each with what it got on stdin."""
        return [(c["arg"], c.get("input")) for c in self.virsh("guest-exec") if c["path"] == WRAPPER]

    def failed(self, *args, stdin=""):
        code, out, err = self.run_input(*args, "--json", stdin=stdin)
        self.assertEqual((code, out), (1, ""), err)
        return json.loads(err)

    def wake(self, actor, vm="work"):
        """agentbox wake as a connection to the VM's port runs it, Orca in the VM answering."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        self.addCleanup(srv.close)
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC",
               "AGENTBOX_ORCA_ADDR": "127.0.0.1:%d" % srv.getsockname()[1]}
        r = subprocess.run([AGENTBOX, f"--actor={actor}", "wake", vm], capture_output=True, text=True, env=env,
                           timeout=60)
        return r.returncode, r.stdout, r.stderr

    def on_with_two_keys(self):
        self.add()
        self.add(line=self.PHONE)
        self.ok("set-ssh", "work", "on")
        self.forget_calls()
        self.journald.entries()
        self.journald.datagrams.clear()

    def test_a_key_added_to_a_running_vm_works_at_once(self):
        self.ok("set-ssh", "work", "on")
        self.assertEqual(self.authorized(), "")         # SSH access without a key: nothing to log in with
        self.assertEqual(self.guest_systemctl(), ["stop sshd.service"])
        self.forget_calls()
        result = self.add()
        self.assertEqual((result["state"], result["applies"]), ("running", "now"))
        self.assertTrue(result["message"].endswith(" added; it applies in the VM now"), result["message"])
        # the wrapper gets exactly what the VM reads at boot, on stdin; no argument carries a key
        self.assertEqual(self.applied(), [(["apply"], self.read(SSH_FILE.format("work")))])
        self.assertEqual(self.authorized(), LAPTOP.rsplit(" ", 1)[0] + "\n")
        self.assertEqual(self.guest_systemctl()[-1], "start sshd.service")
        # adding ends no session
        self.assertNotIn("stop", [c[0] for c in self.systemctl()])
        self.assertFalse(self.exists(PENDING.format("work")))
        self.assertIs(self.json("ssh", "work")["pending"], False)

    def test_removing_a_key_takes_it_out_of_the_vm_and_ends_every_session(self):
        self.on_with_two_keys()
        result = self.json("remove-ssh-key", "work", LAPTOP_FP)
        self.assertEqual(result.pop("message"), f"work: SSH key {LAPTOP_FP} (laptop) removed and every SSH session "
                                                "ended; it applies in the VM now")
        self.assertEqual(result, {"vm": "work", "fingerprint": LAPTOP_FP, "name": "laptop", "type": "ssh-ed25519",
                                  "enabled": True, "keys": 1, "state": "running", "applies": "now"})
        # every connection unit of the SSH port stops first; the port itself stays open
        self.assertEqual(self.systemctl()[0], ["stop", "agentbox-ssh-work@*.service"])
        self.assertNotIn(["disable", "--now", "agentbox-ssh-work.socket"], self.systemctl())
        self.assertTrue(self.exists(SOCKET.format("work")))
        self.assertEqual(self.authorized(), self.PHONE.rsplit(" ", 1)[0] + "\n")
        self.assertEqual(self.guest_systemctl()[-1], "start sshd.service")
        self.assertNotIn(LAPTOP.split()[1], self.read(SSH_FILE.format("work")))
        self.assertEqual([k["fingerprint"] for k in self.record()["ssh_keys"]], [fingerprint(self.PHONE)])
        self.assertEqual(self.activity(), [("ssh.key-removed", "work", "ok",
                                            {"fingerprint": LAPTOP_FP, "name": "laptop"})])
        # the last key: sshd stops, the key file is empty
        self.ok("remove-ssh-key", "work", fingerprint(self.PHONE))
        self.assertEqual(self.authorized(), "")
        self.assertEqual(self.guest_systemctl()[-1], "stop sshd.service")

    def test_off_stops_sshd_in_the_vm_and_ends_every_session(self):
        self.on_with_two_keys()
        result = self.json("set-ssh", "work", "off")
        self.assertEqual(result["applies"], "now")
        # the port closes first, then its connection units stop, then the VM is told
        self.assertEqual(self.systemctl()[0], ["disable", "--now", "agentbox-ssh-work.socket"])
        self.assertIn(["stop", "agentbox-ssh-work@*.service"], self.systemctl())
        self.assertEqual(self.applied(), [(["apply"], "none\n")])
        self.assertEqual(os.listdir(self.guest("run/agentbox/ssh")), [])
        self.assertEqual(self.guest_systemctl()[-1], "stop sshd.service")
        # on again: the keys are back at once
        self.ok("set-ssh", "work", "on")
        self.assertEqual(self.authorized().count("\n"), 2)
        self.assertEqual(self.guest_systemctl()[-1], "start sshd.service")

    def test_sessions_end_also_when_a_later_step_fails(self):
        self.on_with_two_keys()
        self.scenario(virt_install_fails=["agentbox-work"])
        said = self.failed("remove-ssh-key", "work", LAPTOP_FP)
        self.assertTrue(said["message"].startswith("work: the change is saved, but not applied: "), said["message"])
        self.assertEqual(self.systemctl()[0], ["stop", "agentbox-ssh-work@*.service"])
        self.assertEqual(len(self.record()["ssh_keys"]), 1)
        self.assertEqual(self.activity()[0][:3], ("ssh.key-removed", "work", "failed"))
        self.assertEqual(self.activity()[0][3], {"fingerprint": LAPTOP_FP, "name": "laptop"})
        # the VM still has the key: the marker stays, and the next connection hands the change over first
        self.assertEqual(self.applied(), [])
        self.assertTrue(self.exists(PENDING.format("work")))
        self.assertIs(self.json("ssh", "work")["pending"], True)
        self.assertIn("\npending: ", self.ok("ssh", "work"))
        code, _, err = self.wake("SSH (192.0.2.7)")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.authorized(), self.PHONE.rsplit(" ", 1)[0] + "\n")
        self.assertFalse(self.exists(PENDING.format("work")))

    def test_sessions_end_also_when_closing_the_port_fails(self):
        self.on_with_two_keys()
        self.scenario(systemctl_fails=["disable"])
        said = self.failed("set-ssh", "work", "off")
        self.assertTrue(said["message"].startswith("work: the change is saved, but not applied: "), said["message"])
        self.assertEqual(self.systemctl(), [["disable", "--now", "agentbox-ssh-work.socket"],
                                            ["stop", "agentbox-ssh-work@*.service"]])
        self.assertTrue(self.exists(PENDING.format("work")))
        self.scenario()
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["units", "vm-ssh"])
        self.assertFalse(self.exists(SOCKET.format("work")))

    def test_a_vm_that_does_not_take_it_refuses_ssh_connections_until_it_does(self):
        self.on_with_two_keys()
        os.makedirs(self.guest(""), exist_ok=True)
        open(self.guest("starting"), "w").close()              # guest-setup never finishes
        said = self.failed("remove-ssh-key", "work", LAPTOP_FP)
        self.assertEqual(said["error"], "action_failed")
        self.assertEqual(said["message"], "work: the change is saved, but the VM did not take it (the VM is still "
                                          "starting); each connection to its SSH port hands it over first and is "
                                          "refused while that fails")
        self.assertEqual(self.systemctl()[0], ["stop", "agentbox-ssh-work@*.service"])
        self.assertEqual(self.authorized().count("\n"), 2)
        self.assertTrue(self.exists(PENDING.format("work")))
        # a connection to the SSH port is refused; one to the Orca port goes on, with a message
        code, _, err = self.wake("SSH (192.0.2.7)")
        self.assertEqual(code, 1)
        self.assertIn("work: the VM is still starting", err)
        code, _, err = self.wake("Orca (192.0.2.7)")
        self.assertEqual(code, 0, err)
        self.assertIn("agentbox: work: the VM is still starting", err)
        self.assertTrue(self.exists(PENDING.format("work")))
        # once the VM takes it, the next connection goes through
        os.remove(self.guest("starting"))
        code, _, err = self.wake("SSH (192.0.2.7)")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.authorized(), self.PHONE.rsplit(" ", 1)[0] + "\n")
        self.assertFalse(self.exists(PENDING.format("work")))
        # nothing pending: a connection does not ask the VM
        calls = len(self.applied())
        self.assertEqual(self.wake("SSH (192.0.2.7)")[0], 0)
        self.assertEqual(len(self.applied()), calls)

    def test_the_same_call_again_hands_a_pending_change_over(self):
        self.on_with_two_keys()
        self.scenario(agent_down=True)
        said = self.failed("set-ssh", "work", "off")
        self.assertEqual(said["error"], "guest_agent_unavailable")
        self.assertTrue(said["message"].startswith("work: the change is saved, but the VM did not take it (the guest "
                                                   "agent does not answer"), said["message"])
        self.assertFalse(self.exists(SOCKET.format("work")))
        self.scenario()
        self.assertEqual(self.ok("set-ssh", "work", "off"), "work: SSH access stays off\n")
        self.assertIsNone(self.authorized())
        self.assertFalse(self.exists(PENDING.format("work")))

    def test_the_answer_of_the_vm_is_checked(self):
        self.ok("set-ssh", "work", "on")
        for answer in ('{"keys": 5, "sshd": true}', '{"keys": 1, "sshd": true, "more": 1}', '{"keys": true, '
                       '"sshd": true}', "not json", '{"keys": 1, "sshd": 1}', "x" * 300):
            with self.subTest(answer=answer):
                self.scenario(responses=[{"stdout": answer}])
                said = self.failed("add-ssh-key", "work", stdin=key(seed=len(answer)) + "\n")
                self.assertEqual(said["error"], "action_failed")
                self.assertIn("(agentbox-ssh in the VM answered something other than what the Host sent)",
                              said["message"])
                self.assertNotIn(answer[:10], said["message"])
                self.assertTrue(self.exists(PENDING.format("work")))
        for response, why in (({"exitcode": 1, "stdout": '{"keys": 1, "sshd": true}'}, "failed (exit 1)"),
                              ({"signal": 9}, "did not end within")):
            with self.subTest(why=why):
                self.scenario(responses=[response])
                said = self.failed("add-ssh-key", "work", stdin=key(seed=len(why) + 50) + "\n")
                self.assertIn(why, said["message"])

    def test_a_paused_vm_gets_it_when_it_resumes(self):
        self.on_with_two_keys()
        self.ok("pause", "work")
        self.forget_calls()
        result = self.json("remove-ssh-key", "work", LAPTOP_FP)
        self.assertEqual((result["state"], result["applies"]), ("paused", "on_resume"))
        self.assertTrue(result["message"].endswith("; the VM gets it when it resumes"), result["message"])
        self.assertEqual(self.applied(), [])
        self.assertTrue(self.exists(PENDING.format("work")))
        # start resumes it and hands the change over at once
        self.ok("start", "work")
        self.assertEqual(self.applied(), [(["apply"], self.read(SSH_FILE.format("work")))])
        self.assertEqual(self.authorized(), self.PHONE.rsplit(" ", 1)[0] + "\n")
        self.assertFalse(self.exists(PENDING.format("work")))
        # and so does a connection that wakes it
        self.ok("pause", "work")
        self.json("set-ssh", "work", "off")
        self.assertTrue(self.exists(PENDING.format("work")))
        code, _, err = self.wake("SSH (192.0.2.7)")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.applied()[-1], (["apply"], "none\n"))
        self.assertFalse(self.exists(PENDING.format("work")))

    def test_a_vm_that_starts_anew_reads_it_from_fw_cfg(self):
        self.on_with_two_keys()
        self.ok("pause", "work")
        self.ok("remove-ssh-key", "work", LAPTOP_FP)
        self.assertTrue(self.exists(PENDING.format("work")))
        # a Restart discards the Pause: the VM boots with the file, nothing is pending
        self.ok("restart", "work")
        self.assertFalse(self.exists(PENDING.format("work")))
        self.assertEqual(self.applied(), [])
        # a VM that is off gets no marker at all
        self.turned_off("work")
        result = self.json("remove-ssh-key", "work", fingerprint(self.PHONE))
        self.assertEqual(result["applies"], "next_start")
        self.assertFalse(self.exists(PENDING.format("work")))
        self.ok("start", "work")
        self.assertEqual(self.applied(), [])

    def test_a_build_without_the_wrapper_keeps_the_change_and_says_ssh_missing(self):
        self.add()
        self.scenario(missing_wrappers=[WRAPPER])
        said = self.failed("set-ssh", "work", "on")
        self.assertEqual((said["error"], said["params"]), ("ssh_missing", {"vm": "work"}))
        self.assertEqual(said["message"], "work: the change is saved, but the VM's Build cannot apply SSH access "
                                          "while it runs (it has no agentbox-ssh), so the VM keeps the SSH keys it "
                                          "started with until a Reset onto a newer Build (agentbox build <image> "
                                          "--reset work)")
        # turning it on is not refused: saved, the port open, the file the VM reads at its next start written
        self.assertIs(self.record()["ssh_access"], True)
        self.assertTrue(self.exists(SOCKET.format("work")))
        self.assertTrue(self.read(SSH_FILE.format("work")).startswith("host_key "))
        self.assertFalse(self.exists(PENDING.format("work")))
        self.assertEqual(self.activity()[-1][:3], ("ssh.enabled", "work", "failed"))
        said = self.failed("add-ssh-key", "work", stdin=self.PHONE + "\n")
        self.assertEqual(said["error"], "ssh_missing")
        self.assertEqual(len(self.record()["ssh_keys"]), 2)
        # a paused VM of such a Build: its resume drops the marker, which it can never take
        self.ok("pause", "work")
        self.ok("remove-ssh-key", "work", LAPTOP_FP)
        code, _, err = self.wake("SSH (192.0.2.7)")
        self.assertEqual(code, 0, err)
        self.assertIn("it has no agentbox-ssh", err)
        self.assertFalse(self.exists(PENDING.format("work")))

    def test_an_unknown_fingerprint_is_refused(self):
        self.add()
        self.journald.entries()
        self.journald.datagrams.clear()
        for given in ("SHA256:" + "A" * 43, "laptop"):
            with self.subTest(given=given):
                said = self.failed("remove-ssh-key", "work", given)
                self.assertEqual((said["error"], said["params"]), ("ssh_key_not_found", {"vm": "work"}))
                self.assertEqual(said["message"], f"work: no SSH key {given} (agentbox ssh work lists them)")
        self.assertEqual(len(self.record()["ssh_keys"]), 1)
        self.assertEqual(self.systemctl(), [])
        # recorded, with the fingerprint only when it is one
        self.assertEqual([(a, o, p) for a, _, o, p in self.activity()],
                         [("ssh.key-removed", "failed", {"fingerprint": "SHA256:" + "A" * 43}),
                          ("ssh.key-removed", "failed", {})])
        for args in (("work",), ("work", LAPTOP_FP, "x"), ()):
            with self.subTest(args=args):
                self.assertEqual(self.refused("remove-ssh-key", *args),
                                 "agentbox: remove-ssh-key <vm> <fingerprint> [--json]\n")
        self.assertEqual(self.refused("remove-ssh-key", "nope", LAPTOP_FP), "agentbox: No such VM: nope\n")

    def test_removing_a_key_without_ssh_access(self):
        self.add()
        self.forget_calls()
        result = self.json("remove-ssh-key", "work", LAPTOP_FP)
        self.assertEqual(result["message"], f"work: SSH key {LAPTOP_FP} (laptop) removed; SSH access is off")
        self.assertEqual(self.record()["ssh_keys"], [])
        # nothing that reaches the VM changed
        self.assertEqual(self.applied(), [])

    def test_remove_waits_for_the_locks(self):
        self.add()
        path = self.path("var/lib/agentbox/locks/lifecycle-work.lock")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        p = subprocess.Popen([AGENTBOX, "remove-ssh-key", "work", LAPTOP_FP], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
        try:
            time.sleep(1)
            self.assertIsNone(p.poll())
            self.assertEqual(len(self.record()["ssh_keys"]), 1)
        finally:
            os.close(fd)
        _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.record()["ssh_keys"], [])


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
        # each file gets its type before it is renamed into place, so sshd never meets it without
        self.assertEqual(self.calls(), [f"chcon -t sshd_key_t {self.out}/host_key.new",
                                        f"chcon -t ssh_home_t {self.out}/authorized_keys.new",
                                        "systemctl start --no-block sshd.service"])

    def run_guest_setup(self, lines):
        """guest-setup itself, as at a boot, its paths moved into a temporary root: the fw_cfg
        entries (SSH access from lines, None for none), the home, the Data disk, /run and /etc;
        fwcfg-env, home-paths and fwcfg-ssh are the real ones. What needs root or a VM (ip,
        mount, install's owners, ...) is a fake. Returns (exit code, stdout, stderr)."""
        root = os.path.join(self.dir, "root")
        lib = os.path.join(root, "usr", "lib", "agentbox")
        os.makedirs(lib)
        for name in ("fwcfg-env", "home-paths", "fwcfg-ssh"):
            os.symlink(os.path.join(os.path.dirname(FWCFG_SSH), name), os.path.join(lib, name))
        open(os.path.join(lib, "bunfig.toml"), "w").close()
        entries = {"context": "work", "orca_port": "6801", "pairing_address": "192.0.2.1", "env": "none",
                   "claude_token": "", "ssh": "".join(line + "\n" for line in lines) if lines else "none\n"}
        for name, text in entries.items():
            folder = os.path.join(root, "sys", "firmware", "qemu_fw_cfg", "by_name", "opt", "agentbox", name)
            os.makedirs(folder)
            with open(os.path.join(folder, "raw"), "w") as f:
                f.write(text)
        for folder in ("var/lib/agentbox-data", "var/home", "etc"):
            os.makedirs(os.path.join(root, folder))
        open(os.path.join(root, "etc", "hosts"), "w").close()
        with open(GUEST_SETUP) as f:
            script = f.read()
        for path in ("/sys/firmware/", "/usr/lib/agentbox/", "/run/agentbox", "/var/lib/agentbox-data",
                     "/var/home/agent", "/etc/hosts", "/etc/resolv.conf"):
            self.assertIn(path, script)
            script = script.replace(path, root + path)
        moved = os.path.join(self.dir, "guest-setup")
        with open(moved, "w") as f:
            f.write(script)
        os.chmod(moved, 0o755)
        for name in ("setsebool", "modprobe", "hostnamectl", "mountpoint", "mount", "chown", "runuser"):
            self.fake(name, "exit 0\n")
        self.fake("ip", '[ "$1" = -4 ] && echo "2: eth0    inet 10.77.1.2/24 brd 10.77.1.255 scope global eth0"\n'
                        "exit 0\n")
        # the owners need root: install without them
        self.fake("install", 'for a; do shift; case "$skip$a" in -o|-g) skip=1;; 1*) skip=;; *) set -- "$@" "$a";; '
                             'esac; done\nexec /usr/bin/install "$@"\n')
        r = subprocess.run([moved], capture_output=True, text=True, timeout=60,
                           env={"PATH": self.bin + ":" + os.environ["PATH"], "LANG": "C.UTF-8",
                                "CALLS": os.path.join(self.dir, "calls")})
        self.out = os.path.join(root, "run", "agentbox", "ssh")
        return r.returncode, r.stdout, r.stderr

    def test_guest_setup_with_ssh_access_starts_sshd_last(self):
        code, out, err = self.run_guest_setup(self.entry_lines(key()))
        self.assertEqual((code, out), (0, DONE), err)
        self.assertEqual(self.files(), ["authorized_keys", "host_key"])
        with open(os.path.join(self.out, "authorized_keys")) as f:
            self.assertEqual(f.read(), f"{key()}\n")
        self.assertEqual(self.calls(), ["systemctl start --no-block sshd.service"])
        self.assertIn("fwcfg-ssh: SSH access on with 1 SSH key(s), sshd started", err)
        # the home was set up before
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "root", "var", "home", "agent", ".bunfig.toml")))

    def test_guest_setup_without_ssh_access_starts_no_sshd(self):
        code, out, err = self.run_guest_setup(None)
        self.assertEqual((code, out), (0, DONE), err)
        self.assertEqual(self.files(), [])
        self.assertEqual(self.calls(), [])
        self.assertIn("fwcfg-ssh: no SSH access, sshd stays off", err)

    def test_guest_setup_goes_on_when_ssh_access_fails(self):
        run = os.path.join(self.dir, "root", "run", "agentbox")
        os.makedirs(run)
        os.symlink(self.dir, os.path.join(run, "ssh"))          # no directory fwcfg-ssh writes into
        code, out, err = self.run_guest_setup(self.entry_lines(key()))
        self.assertEqual((code, out), (0, DONE), err)
        self.assertIn("guest-setup: SSH access not set up", err)
        self.assertEqual(self.calls(), [])

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


class WrapperTest(unittest.TestCase):
    """agentbox-ssh run directly, as the guest agent runs it in a running VM: the entry on stdin,
    checked again with fwcfg-ssh's own checks, the key files replaced whole, sshd started or
    stopped, and only a small JSON answer. AGENTBOX_SSH_ROOT moves its paths into a temporary
    root with a fake systemctl."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        lib, self.bin = os.path.join(self.root, "usr/lib/agentbox"), os.path.join(self.root, "usr/bin")
        os.makedirs(lib)
        os.makedirs(self.bin)
        os.symlink(FWCFG_SSH, os.path.join(lib, "fwcfg-ssh"))
        self.fake("systemctl", 'echo "$*" >> "$ROOT/calls"\ncase "$1" in is-active) [ ! -e "$ROOT/starting" ];; '
                               '*) [ ! -e "$ROOT/fails" ] || { echo "Job failed." >&2; exit 1; };; esac\n')
        self.fake("selinuxenabled", "exit 1\n")
        self.out = os.path.join(self.root, "run/agentbox/ssh")
        self.host_key = sshkeys.host_key_file(bytes(32), bytes(range(32)), "work.agentbox")

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nROOT="{self.root}"\n' + body)
        os.chmod(path, 0o755)

    def run_wrapper(self, data, *args, wait="1"):
        r = subprocess.run([AGENTBOX_SSH, *(args or ("apply",))], input=data, capture_output=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "AGENTBOX_SSH_ROOT": self.root,
                                "AGENTBOX_SSH_SETUP_WAIT": wait})
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def entry(self, *keys):
        return sshkeys.fw_cfg(True, [{"key": k} for k in keys], self.host_key)

    def calls(self):
        try:
            with open(os.path.join(self.root, "calls")) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def files(self):
        return sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []

    def authorized(self):
        with open(os.path.join(self.out, "authorized_keys")) as f:
            return f.read()

    def test_keys_go_in_whole_and_sshd_starts(self):
        code, out, err = self.run_wrapper(self.entry(key(seed=1), key("ssh-rsa", seed=2)))
        self.assertEqual((code, json.loads(out)), (0, {"keys": 2, "sshd": True}), err)
        self.assertEqual(self.authorized(), f"{key(seed=1)}\n{key('ssh-rsa', seed=2)}\n")
        with open(os.path.join(self.out, "host_key"), "rb") as f:
            self.assertEqual(f.read(), self.host_key)
        modes = {n: os.stat(os.path.join(self.out, n)).st_mode & 0o7777 for n in self.files()}
        self.assertEqual(modes, {"host_key": 0o600, "authorized_keys": 0o644})
        # it waited for guest-setup, then started sshd and waited for it
        self.assertEqual(self.calls(), ["is-active --quiet agentbox-guest-setup.service", "start sshd.service"])
        # a key removed: the file is replaced, with nothing left beside it
        inode = os.stat(os.path.join(self.out, "authorized_keys")).st_ino
        code, out, _ = self.run_wrapper(self.entry(key("ssh-rsa", seed=2)))
        self.assertEqual(json.loads(out), {"keys": 1, "sshd": True})
        self.assertEqual(self.authorized(), f"{key('ssh-rsa', seed=2)}\n")
        self.assertNotEqual(os.stat(os.path.join(self.out, "authorized_keys")).st_ino, inode)
        self.assertEqual(self.files(), ["authorized_keys", "host_key"])

    def test_without_keys_or_without_ssh_access_sshd_stops(self):
        self.run_wrapper(self.entry(key()))
        code, out, _ = self.run_wrapper(self.entry())
        self.assertEqual((code, json.loads(out)), (0, {"keys": 0, "sshd": False}))
        self.assertEqual(self.authorized(), "")
        self.assertEqual(self.calls()[-1], "stop sshd.service")
        code, out, _ = self.run_wrapper(b"none\n")
        self.assertEqual((code, json.loads(out)), (0, {"keys": 0, "sshd": False}))
        self.assertEqual(self.files(), [])
        self.assertEqual(self.calls()[-1], "stop sshd.service")

    def test_it_checks_the_keys_again_as_fwcfg_ssh_does(self):
        ed = key().split()[1]
        data = self.entry(key(seed=3)) + "".join(
            f"key {line}\n" for line in (f'command="x" ssh-ed25519 {ed}', f"ssh-dss {ed}", f"ssh-rsa {ed}",
                                         "ssh-ed25519 !!!", key(seed=3))).encode()
        code, out, err = self.run_wrapper(data)
        self.assertEqual(json.loads(out), {"keys": 1, "sshd": True})
        self.assertEqual(self.authorized(), f"{key(seed=3)}\n")
        self.assertEqual(err.count("left out"), 5, err)
        # at most 16 keys
        code, out, _ = self.run_wrapper(self.entry(*(key(seed=i) for i in range(20))))
        self.assertEqual(json.loads(out), {"keys": 16, "sshd": True})
        # no host key, or more than the entry may hold: no SSH access
        for data in (f"key {key()}\n".encode(), self.entry(key()) + b"#" * sshkeys.MAX_ENTRY):
            with self.subTest(size=len(data)):
                code, out, _ = self.run_wrapper(data)
                self.assertEqual((code, json.loads(out)), (0, {"keys": 0, "sshd": False}))
                self.assertEqual(self.files(), [])

    def test_it_never_writes_before_guest_setup(self):
        open(os.path.join(self.root, "starting"), "w").close()
        code, out, err = self.run_wrapper(self.entry(key()), wait="0.5")
        self.assertEqual((code, out), (75, ""))
        self.assertIn("the VM is still starting", err)
        self.assertEqual(self.files(), [])
        self.assertNotIn("start sshd.service", self.calls())

    def test_failures_print_no_answer(self):
        open(os.path.join(self.root, "fails"), "w").close()
        code, out, err = self.run_wrapper(self.entry(key()))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("starting sshd failed: Job failed.", err)
        os.remove(os.path.join(self.root, "fails"))
        shutil.rmtree(self.out)
        os.symlink(self.root, self.out)                     # no directory it writes into
        code, out, err = self.run_wrapper(self.entry(key()))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("is no directory", err)
        self.assertEqual(self.calls()[-1], "is-active --quiet agentbox-guest-setup.service")

    def test_arguments(self):
        for args in (("apply", "x"), ("prune",), ("--help",)):
            with self.subTest(args=args):
                code, out, err = self.run_wrapper(b"", *args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("agentbox-ssh: apply", err)
        for hook in ("freeze", "thaw"):
            self.assertEqual(self.run_wrapper(b"", hook)[:2], (0, ""))
        self.assertEqual(self.calls(), [])

    def test_it_is_a_fixed_wrapper_beside_the_others(self):
        self.assertTrue(os.access(AGENTBOX_SSH, os.X_OK))
        with open(AGENTBOX_SSH) as f:
            text = f.read()
        self.assertTrue(text.startswith("#!/usr/bin/python3 -I\n"))
        # its checks are fwcfg-ssh's, loaded from the Image's fixed path, not a copy
        self.assertIn('FWCFG_SSH = "/usr/lib/agentbox/fwcfg-ssh"', text)
        self.assertNotIn("TYPES", text)


if __name__ == "__main__":
    unittest.main()
