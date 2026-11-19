"""Secrets per VM (#30): values a VM gets like VM variables, which the Host never shows or logs.

`agentbox set-secret` reads the value from stdin (without echo on a terminal) and `unset-secret`
removes one; the record holds only the names, the values live only in
/etc/agentbox/secrets/env-<vm>, which QEMU passes in as fw_cfg opt/agentbox/secrets.
`regenerate` creates that file as "none" and never writes a value from the record. The tests
search every output, the record, the domain and every program call for a known value. The real
CLI runs with AGENTBOX_ROOT pointing at a temporary root and the fakes of test_registry.py; the
guest side runs the part of guest-setup that reads the fw_cfg entries against files in a
temporary directory.

Run from the repo root: python3 -m unittest discover -s tests
"""
import base64
import fcntl
import grp
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from test_registry import AGENTBOX  # noqa: E402
from test_vm_env import ENV_FILE, FWCFG_ENV, GUEST_SETUP, EnvTest, fw_cfg_line  # noqa: E402

SECRETS_FILE = "etc/agentbox/secrets/env-{}"
# a value searched for everywhere: a comma (virt-install splits --sysinfo there), quotes and $
VALUE = 'Geheim-s3cr3t, with "quotes" and $(id)'
OTHER = "another-value-0123456789"


class SecretTest(EnvTest):
    def agentbox_env(self):
        return {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
                "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}

    def secret(self, *args, value=VALUE, code=0):
        """agentbox with the value on stdin: (stdout, stderr), failing unless it exits with code."""
        data = value if isinstance(value, bytes) else value.encode()
        r = subprocess.run([AGENTBOX, *args], input=data, capture_output=True, env=self.agentbox_env(), timeout=60)
        self.assertEqual(r.returncode, code, r.stderr)
        return r.stdout.decode(), r.stderr.decode()

    def refused_secret(self, *args, value=VALUE):
        """The --json failure of a command that changes nothing (no file, no call)."""
        before, calls = self.files(), len(self.calls())
        out, err = self.secret(*args, "--json", value=value, code=1)
        self.assertEqual(out, "")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.calls()[calls:], [])
        return json.loads(err)

    def secrets_file(self, vm="work"):
        return self.read(SECRETS_FILE.format(vm))

    def record(self, vm="work"):
        return vm_records.read(self.root, vm)

    def host_texts(self):
        """Everything of the Host a value could end up in apart from its secrets file: every file
        under the test root, and every call of another program with its arguments."""
        texts = []
        for folder, _, names in os.walk(self.root):
            for n in names:
                path = os.path.join(folder, n)
                if not n.startswith("env-") and os.path.isfile(path):
                    with open(path, "rb") as f:
                        texts.append(f.read().decode("utf-8", "replace"))
        texts.append(json.dumps(self.calls()))
        return texts

    def assert_nowhere(self, value, *outputs):
        for text in [*outputs, *self.host_texts()]:
            self.assertNotIn(value, text)


class SetSecretTest(SecretTest):
    def test_the_value_goes_into_its_file_only_and_the_record_holds_the_name(self):
        out, err = self.secret("set-secret", "work", "API_KEY", "--no-prompt", "--json")
        result = json.loads(out)
        self.assertEqual(result.pop("message"), "work: secret API_KEY set, applies from the next Restart (agentbox "
                                                "restart work) or Reset; until then the VM keeps the secrets it "
                                                "started with")
        self.assertEqual(result, {"vm": "work", "secrets": ["API_KEY"], "changed": True, "state": "running",
                                  "applies": "next_restart"})
        self.assertEqual(err, "")
        self.assertEqual(self.secrets_file(), fw_cfg_line("API_KEY", VALUE) + "\n")
        self.assertEqual(self.mode(SECRETS_FILE.format("work")), 0o640)
        self.assertEqual(self.mode("etc/agentbox/secrets"), 0o750)
        self.assertEqual(self.record()["secrets"], ["API_KEY"])
        self.assertNotIn("env", self.record())
        # the domain names the file already: nothing is defined again, nothing stops or starts
        self.assertEqual(self.virsh("define", "shutdown", "destroy", "start", "managedsave"), [])
        self.assertEqual(self.secrets_file("priv"), "none\n")
        self.assertEqual(self.json("regenerate")["vms"], {"work": [], "priv": []})

    def test_a_value_is_shown_or_logged_nowhere_on_the_host(self):
        outputs = [*self.secret("set-secret", "work", "API_KEY"),
                   *self.secret("set-secret", "work", "API_KEY", "--json"),
                   *self.secret("set-secret", "work", "OTHER", "--no-prompt", value=OTHER)]
        for args in (("describe",), ("describe", "work"), ("env", "work"), ("status",)):
            for more in ((), ("--json",)):
                code, out, err = self.run_agentbox(*args, *more)
                self.assertEqual(code, 0, err)
                outputs += [out, err]
        os.remove(self.path("var/lib/agentbox/domains/work.xml"))       # defined again: virt-install runs
        outputs += self.secret("regenerate", value="")
        self.assertIn(f"entry6.file={self.path(SECRETS_FILE.format('work'))}", json.dumps(self.calls()))
        for value in (VALUE, OTHER):
            self.assert_nowhere(value, *outputs)
            self.assert_nowhere(base64.b64encode(value.encode()).decode(), *outputs)
        # what the Host shows are the names
        self.assertEqual(self.json("env", "work")["secrets"], ["API_KEY", "OTHER"])
        self.assertEqual(self.ok("env", "work"),
                         "API_KEY (secret, its value is not shown)\nOTHER (secret, its value is not shown)\n")
        self.assertIn("  Secrets:      API_KEY, OTHER (values not shown)\n", self.ok("describe", "work"))
        described = self.json("describe", "work")
        self.assertEqual((described["secrets"], described["secrets_refused"]), (["API_KEY", "OTHER"], []))
        self.assertEqual(self.secrets_file(), fw_cfg_line("API_KEY", VALUE) + "\n" + fw_cfg_line("OTHER", OTHER) + "\n")

    def test_on_a_terminal_the_value_is_typed_without_echo(self):
        main, terminal = os.openpty()
        p = subprocess.Popen([AGENTBOX, "set-secret", "work", "API_KEY"], stdin=terminal, stdout=subprocess.PIPE,
                             stderr=terminal, env=self.agentbox_env())
        os.close(terminal)
        seen, deadline = b"", time.monotonic() + 30
        try:
            while b"then Enter:" not in seen:
                self.assertLess(time.monotonic(), deadline, seen)
                if select.select([main], [], [], 1)[0]:
                    seen += os.read(main, 4096)
            os.write(main, VALUE.encode() + b"\n")
            out, _ = p.communicate(timeout=60)
            while select.select([main], [], [], 1)[0]:
                try:
                    chunk = os.read(main, 4096)
                except OSError:                 # the terminal's last process is gone
                    break
                if not chunk:
                    break
                seen += chunk
        finally:
            os.close(main)
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, seen)
        self.assertIn(b"The value of the secret API_KEY for work (it is not shown), then Enter:", seen)
        self.assertNotIn(VALUE.encode(), seen + out)
        self.assertTrue(out.decode().startswith("work: secret API_KEY set, applies from the next Restart"), out)
        self.assertEqual(self.secrets_file(), fw_cfg_line("API_KEY", VALUE) + "\n")

    def test_one_line_break_at_the_end_is_not_part_of_the_value(self):
        for value, stored in (("abc\n", "abc"), ("abc\r\n", "abc"), ("abc", "abc"), ("", ""), ("\n", ""),
                              ("tab\tin it\n", "tab\tin it")):
            with self.subTest(value=value):
                self.secret("set-secret", "work", "A", "--no-prompt", value=value)
                self.assertEqual(self.secrets_file(), fw_cfg_line("A", stored) + "\n")

    def test_setting_the_stored_value_again_still_counts_as_a_change(self):
        """No answer tells whether a value was the stored one."""
        self.secret("set-secret", "work", "API_KEY")
        result = json.loads(self.secret("set-secret", "work", "API_KEY", "--json")[0])
        self.assertEqual((result["changed"], result["applies"]), (True, "next_restart"))

    def test_more_secrets_keep_their_order_and_a_new_value_replaces_the_old(self):
        for name, value in (("A", "1"), ("B", "2"), ("A", "3")):
            self.secret("set-secret", "work", name, value=value)
        self.assertEqual(self.record()["secrets"], ["A", "B"])
        self.assertEqual(self.secrets_file(), "A=Mw==\nB=Mg==\n")

    def test_vm_that_is_off_gets_them_at_its_next_start_and_a_paused_one_is_not_refused(self):
        self.turned_off("priv")
        result = json.loads(self.secret("set-secret", "priv", "A", "--json")[0])
        self.assertEqual((result["state"], result["applies"], result["message"]),
                         ("shut off", "next_start", "priv: secret A set, applies from the next start"))
        self.ok("pause", "work")
        self.forget_calls()
        result = json.loads(self.secret("set-secret", "work", "A", "--json")[0])
        self.assertEqual((result["state"], result["applies"]), ("paused", "next_restart"))
        self.assertEqual(self.virsh("define", "start", "managedsave-remove"), [])

    def test_the_value_is_never_an_argument(self):
        said = self.refused_secret("set-secret", "work", "API_KEY", "the-value")
        self.assertEqual(said["error"], "invalid_argument")
        self.assertEqual(said["message"], "set-secret <vm> NAME [--no-prompt] [--json]  (the value on stdin)")
        cases = {(): "agentbox: set-secret <vm> NAME [--no-prompt] [--json]  (the value on stdin)\n",
                 ("work",): "agentbox: set-secret <vm> NAME [--no-prompt] [--json]  (the value on stdin)\n",
                 ("work", "A", "--value"): "agentbox: unknown option: ?\n",       # not repeated: it could be a value
                 ("nope", "A"): "agentbox: No such VM: nope\n"}
        for args, message in cases.items():
            with self.subTest(args=args):
                self.assertEqual(self.secret("set-secret", *args, code=1)[1], message)

    def test_values_that_cannot_be_one_are_refused_without_repeating_them(self):
        for value, error in (("Geheim\x1bvalue", "invalid_secret_value"), ("Geheim\nvalue\n", "invalid_secret_value"),
                             ("Geheim\0value", "invalid_secret_value"), (b"Geheim\xffvalue", "invalid_secret_value"),
                             ("Geheim" + "x" * 4091, "secret_too_large"), ("Geheim" + "ü" * 2046, "secret_too_large")):
            with self.subTest(value=value[:20]):
                said = self.refused_secret("set-secret", "work", "A", value=value)
                self.assertEqual((said["error"], said["params"]["name"]), (error, "A"))
                self.assertNotIn("Geheim", json.dumps(said))
        # 4096 bytes are allowed
        self.secret("set-secret", "work", "A", value="x" * 4096 + "\n")
        self.assertEqual(self.secrets_file(), fw_cfg_line("A", "x" * 4096) + "\n")


class SecretNamesTest(SecretTest):
    def test_the_names_of_vm_variables_and_no_others(self):
        for name in ("NODE_OPTIONS", "PATH", "npm_config_ignore_scripts", "LD_PRELOAD", "AGENTBOX_X",
                     "CLAUDE_CODE_OAUTH_TOKEN", "UV_EXCLUDE_NEWER", "SHELLOPTS"):
            with self.subTest(name=name):
                said = self.refused_secret("set-secret", "work", name)
                self.assertEqual((said["error"], said["params"]), ("secret_name_reserved", {"name": name}))
                self.assertIn(f"{name} cannot be a secret: ", said["message"])
        for name in ("1A", "A-B", "A" * 65, "--x=1"):
            with self.subTest(name=name):
                self.assertIn(self.refused_secret("set-secret", "work", name)["error"],
                              ("invalid_secret_name", "invalid_argument"))
        for name in ("HTTPS_PROXY", "NPM_TOKEN", "DATABASE_PASSWORD", "A" * 64):
            with self.subTest(name=name):
                self.secret("set-secret", "work", name)

    def test_a_name_cannot_be_both_a_vm_variable_and_a_secret(self):
        self.ok("set-env", "work", "DATABASE_URL=postgres://db/x")
        said = self.refused_secret("set-secret", "work", "DATABASE_URL")
        self.assertEqual((said["error"], said["params"]), ("secret_name_is_env", {"vm": "work", "name": "DATABASE_URL"}))
        self.secret("set-secret", "work", "API_KEY")
        for args in (("API_KEY=1",), ("API_KEY=1", "--replace")):
            with self.subTest(args=args):
                said = self.failure("set-env", "work", *args)
                self.assertEqual((said["error"], said["params"]), ("env_name_is_secret", {"vm": "work", "name": "API_KEY"}))
        self.assertEqual(self.record()["env"], {"DATABASE_URL": "postgres://db/x"})
        # once removed, the name is free for the other kind
        self.ok("unset-secret", "work", "API_KEY")
        self.ok("set-env", "work", "API_KEY=1")

    def test_the_limit_of_64_counts_both_together(self):
        self.ok("set-env", "work", *(f"V{i}=x" for i in range(62)))
        self.secret("set-secret", "work", "S1")
        self.secret("set-secret", "work", "S2")
        said = self.refused_secret("set-secret", "work", "S3")
        self.assertEqual((said["error"], said["params"]), ("secret_too_large", {"max_variables": 64}))
        self.assertEqual(said["message"], "65 VM variables and secrets together, at most 64 are allowed")
        said = self.failure("set-env", "work", "ONE_MORE=x")
        self.assertEqual((said["error"], said["params"]), ("env_too_large", {"max_variables": 64}))
        # replacing a value adds nothing
        self.secret("set-secret", "work", "S2", value=OTHER)

    def test_a_secret_whose_name_is_refused_now_is_kept_but_never_passed_on(self):
        vm_records.write(self.root, vm_records.record("work", 1, secrets=["NODE_OPTIONS", "A"]))
        self.write(SECRETS_FILE.format("work"), fw_cfg_line("NODE_OPTIONS", VALUE) + "\n" + fw_cfg_line("A", "1") + "\n",
                   mode=0o640)
        self.assertEqual(self.json("env", "work")["secrets_refused"], ["NODE_OPTIONS"])
        self.assertIn("work: the secret NODE_OPTIONS is not passed on to the VM", self.ok("env", "work"))
        self.assertIn("NODE_OPTIONS not passed on", self.ok("describe", "work"))
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["vm-secrets"])
        self.assertEqual(self.secrets_file(), "A=MQ==\n")
        self.secret("set-secret", "work", "B", value="2")
        self.assertEqual(self.record()["secrets"], ["NODE_OPTIONS", "A", "B"])
        self.assertEqual(self.refused_secret("set-secret", "work", "NODE_OPTIONS")["error"], "secret_name_reserved")
        self.ok("unset-secret", "work", "NODE_OPTIONS")
        self.assertEqual(self.record()["secrets"], ["A", "B"])

    def test_a_record_with_malformed_secrets_is_broken(self):
        for secrets in ({"A": "1"}, ["1A"], ["A", "A"], [1], [f"S{i}" for i in range(65)]):
            with self.subTest(secrets=str(secrets)[:40]):
                vm_records.write(self.root, vm_records.record("work", 1, secrets=secrets))
                code, out, err = self.run_agentbox("env", "work")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("work.json: broken record (", err)
        vm_records.write(self.root, vm_records.record("work", 1, secrets=["S"],
                                                      env={f"V{i}": "x" for i in range(64)}))
        code, out, err = self.run_agentbox("env", "work")
        self.assertIn("more than 64 VM variables and secrets together", err)


class UnsetSecretTest(SecretTest):
    def test_unset_removes_the_name_and_the_value(self):
        self.secret("set-secret", "work", "A", value="1")
        self.secret("set-secret", "work", "B", value=VALUE)
        result = self.json("unset-secret", "work", "B")
        self.assertEqual((result["secrets"], result["changed"], result["applies"]), (["A"], True, "next_restart"))
        self.assertTrue(result["message"].startswith("work: secret B removed, applies from the next Restart"))
        self.assertEqual(self.secrets_file(), "A=MQ==\n")
        self.assertEqual(self.ok("unset-secret", "work", "NOT_SET"), "work: secrets unchanged\n")
        self.ok("unset-secret", "work", "A")
        self.assertEqual(self.secrets_file(), "none\n")
        self.assertNotIn("secrets", self.record())

    def test_unset_usage(self):
        self.assertEqual(self.refused("unset-secret", "work"), "agentbox: unset-secret <vm> NAME [--json]\n")
        self.assertEqual(self.failure("unset-secret", "work", "A-B")["error"], "invalid_secret_name")

    def test_each_waits_for_the_registry_the_record_and_the_lifecycle_lock(self):
        for lock, args in (("vms.lock", ("set-secret", "work", "A")), ("work.json.lock", ("set-secret", "work", "B")),
                           ("lifecycle-work.lock", ("unset-secret", "work", "A"))):
            with self.subTest(lock=lock):
                before = self.record().get("secrets")
                path = self.path(f"var/lib/agentbox/locks/{lock}")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                p = subprocess.Popen([AGENTBOX, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=self.agentbox_env())
                try:
                    p.stdin.write(b"1\n")
                    p.stdin.close()
                    time.sleep(1)
                    self.assertIsNone(p.poll())
                    self.assertEqual(self.record().get("secrets"), before)
                finally:
                    os.close(fd)
                _, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.record()["secrets"], ["B"])


class RegenerateSecretsTest(SecretTest):
    def test_regenerate_creates_the_placeholder_and_never_writes_a_value_from_the_record(self):
        self.assertEqual((self.secrets_file("work"), self.secrets_file("priv")), ("none\n", "none\n"))
        self.assertEqual(self.mode(SECRETS_FILE.format("work")), 0o640)
        self.secret("set-secret", "work", "A", value=VALUE)
        before = self.files()
        self.assertEqual(self.json("regenerate")["vms"], {"work": [], "priv": []})
        self.assertEqual(self.files(), before)
        # a value no name in the record points to (set-secret died before saving the record,
        # or the record was changed by hand) goes; a name without a value gives the VM nothing
        vm_records.write(self.root, vm_records.record("work", 1, secrets=["B"]))
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["vm-secrets"])
        self.assertEqual(self.secrets_file(), "none\n")
        # a missing file is created again, as none
        os.remove(self.path(SECRETS_FILE.format("work")))
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["vm-secrets"])
        self.assertEqual(self.secrets_file(), "none\n")
        self.assertEqual(self.virsh("define"), [])

    def test_the_domain_passes_the_file_in_and_never_a_value(self):
        self.secret("set-secret", "work", "A", value="a,b,entry9.name=x")
        os.remove(self.path("var/lib/agentbox/domains/work.xml"))       # define again
        self.ok("regenerate")
        argv = [c["argv"] for c in self.calls("virt-install") if "agentbox-work" in c["argv"]][-1]
        sysinfo = argv[argv.index("--sysinfo") + 1]
        self.assertTrue(sysinfo.endswith(f",entry6.name=opt/agentbox/secrets,"
                                         f"entry6.file={self.path(SECRETS_FILE.format('work'))}"), sysinfo)
        self.assertNotIn("entry9", " ".join(argv))

    def test_set_secret_defines_a_vm_whose_domain_lacks_the_entry(self):
        saved = self.path("var/lib/agentbox/domains/work.xml")
        with open(saved) as f:
            xml = f.read()
        with open(saved, "w") as f:
            f.write(re.sub(r",entry6\.name=opt/agentbox/secrets,entry6\.file=[^\"]*", "", xml))
        self.forget_calls()
        result = json.loads(self.secret("set-secret", "work", "A", "--json")[0])
        self.assertEqual((result["changed"], result["applies"]), (True, "next_restart"))
        defined = [c["xml"] for c in self.virsh("define") if c["dom"] == "agentbox-work"]
        self.assertEqual(len(defined), 1)
        self.assertIn("entry6.name=opt/agentbox/secrets", defined[0])

    def test_failing_definition_keeps_the_secret_and_says_how_to_finish(self):
        os.remove(self.path("var/lib/agentbox/domains/work.xml"))
        self.scenario(virt_install_fails=["agentbox-work"])
        out, err = self.secret("set-secret", "work", "A", "--json", code=1)
        said = json.loads(err)
        self.assertEqual((said["error"], said["params"]), ("secret_not_applied", {"vm": "work"}))
        self.assertTrue(said["message"].endswith("; agentbox regenerate finishes it"), said["message"])
        self.assertNotIn(VALUE, err)
        self.assertEqual((self.record()["secrets"], self.secrets_file()), (["A"], fw_cfg_line("A", VALUE) + "\n"))
        self.scenario()
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["domain"])

    def test_a_file_that_cannot_be_written_changes_nothing(self):
        os.remove(self.path(SECRETS_FILE.format("work")))
        os.makedirs(self.path(SECRETS_FILE.format("work")))          # no file can take its place
        said = self.refused_secret("set-secret", "work", "A")
        self.assertEqual(said["error"], "action_failed")
        self.assertNotIn(VALUE, json.dumps(said))
        self.assertNotIn("secrets", self.record())


class CreateDeleteSecretsTest(SecretTest):
    def test_create_refuses_a_leftover_file_and_delete_removes_it(self):
        self.write(SECRETS_FILE.format("lab"), "OLD=MQ==\n")
        self.assertEqual(self.refused("create", "lab"),
                         "agentbox: lab: leftovers of an earlier VM exist (secrets), remove them first\n")
        os.remove(self.path(SECRETS_FILE.format("lab")))
        self.ok("create", "lab")
        self.assertEqual(self.secrets_file("lab"), "none\n")
        self.secret("set-secret", "lab", "A")
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertFalse(self.exists(SECRETS_FILE.format("lab")))
        self.assertFalse(self.exists(ENV_FILE.format("lab")))


class GuestSetupSecretsTest(unittest.TestCase):
    """guest-setup reads the fw_cfg entries of the VM variables and the secrets into
    /run/agentbox/env (0600, orca serve's EnvironmentFile) and /run/agentbox/agent-env (0640,
    group agent, sourced by the shells' profile): its own lines, run with the fw_cfg entries,
    /run and fwcfg-env moved into a temporary directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.fwcfg = os.path.join(self.dir, "fw_cfg")
        self.run_dir = os.path.join(self.dir, "run")

    def entry(self, name, lines):
        os.makedirs(os.path.join(self.fwcfg, "opt", "agentbox", name))
        with open(os.path.join(self.fwcfg, "opt", "agentbox", name, "raw"), "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def guest_setup(self):
        """The lines of guest-setup from creating /run/agentbox to the fixed values of Agent Box."""
        with open(GUEST_SETUP) as f:
            code = f.read()
        start = code.index("install -d -m 755 /run/agentbox")
        end = code.index("printf 'AGENTBOX_CONTEXT=")
        group = grp.getgrgid(os.getgid()).gr_name        # agent in the VM
        part = (code[start:end].replace("/sys/firmware/qemu_fw_cfg/by_name", self.fwcfg)
                .replace("/usr/lib/agentbox/fwcfg-env", FWCFG_ENV).replace("/run/agentbox", self.run_dir + "/agentbox")
                .replace("-g agent ", f"-g {group} "))
        r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + part], capture_output=True, text=True, timeout=30,
                           env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(VALUE, r.stdout + r.stderr)

    def read(self, name):
        path = os.path.join(self.run_dir, "agentbox", name)
        with open(path) as f:
            return f.read(), os.stat(path).st_mode & 0o777

    def test_secrets_go_into_env_and_agent_env_after_the_vm_variables(self):
        self.entry("env", [fw_cfg_line("DATABASE_URL", "postgres://db/x")])
        self.entry("secrets", [fw_cfg_line("API_KEY", VALUE)])
        self.guest_setup()
        quoted = VALUE.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
        self.assertEqual(self.read("env"), (f'DATABASE_URL="postgres://db/x"\nAPI_KEY="{quoted}"\n', 0o600))
        text, mode = self.read("agent-env")
        self.assertEqual(mode, 0o640)
        self.assertEqual(text.splitlines()[0], "export DATABASE_URL=postgres://db/x")
        r = subprocess.run(["bash", "-c", '. "$1"; printf %s "$API_KEY"', "bash",
                            os.path.join(self.run_dir, "agentbox", "agent-env")], capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.stdout, VALUE)

    def test_a_missing_entry_or_none_means_no_secrets(self):
        self.entry("env", ["none"])
        self.guest_setup()
        self.assertEqual((self.read("env"), self.read("agent-env")), (("", 0o600), ("", 0o640)))
        os.remove(os.path.join(self.fwcfg, "opt", "agentbox", "env", "raw"))
        self.entry("secrets", ["none"])
        self.guest_setup()
        self.assertEqual(self.read("env"), ("", 0o600))


if __name__ == "__main__":
    unittest.main()
