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
import importlib.machinery
import importlib.util
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
DONE = "guest-setup: context work, Orca port 6801, the Host at 10.77.1.1\n"     # its last line
# a key and its fingerprint as `ssh-keygen -lf` gives it
LAPTOP = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f laptop"
LAPTOP_FP = "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA"


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
        self.assertEqual((result["changed"], result["state"], result["applies"]), (True, "paused", "next_restart"))
        self.assertEqual(result["message"],
                         "work: SSH access on, port 2201 (ssh -p 2201 agent@127.0.0.1); the VM is paused, so it is "
                         "defined again only once it no longer has a Pause (agentbox start work, then agentbox "
                         "regenerate), and SSH access applies from the next Restart or Reset after that")
        said = self.add(line=key(seed=2, comment="phone"))
        self.assertTrue(said["message"].endswith("; the VM is paused, so it is defined again only once it no longer "
                                                 "has a Pause (agentbox start work, then agentbox regenerate), and "
                                                 "SSH access applies from the next Restart or Reset after that"),
                        said["message"])
        self.assertEqual(self.virsh("define", "start", "managedsave-remove"), [])
        self.assertIs(self.record()["ssh_access"], True)
        self.assertTrue(self.exists(SOCKET.format("work")))
        self.ok("start", "work")
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


if __name__ == "__main__":
    unittest.main()
