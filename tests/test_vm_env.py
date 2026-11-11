"""VM variables (#29): environment variables per VM for orca serve, agents, terminals and shells.

`agentbox env`, `set-env` and `unset-env` change the record and the file QEMU passes into the
VM as fw_cfg opt/agentbox/env; `regenerate` writes that file too. No VM variable may switch a
Safeguard off: names the safeguard check knows, names Agent Box sets and every variable of the
Image's DefaultEnvironment are refused. The real CLI runs with AGENTBOX_ROOT pointing at a
temporary root and the fakes of test_registry.py; the guest side
(vm/files/usr/lib/agentbox/fwcfg-env) runs against an fw_cfg file in a temporary directory.

Run from the repo root: python3 -m unittest discover -s tests
"""
import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from test_registry import AGENTBOX, REPO  # noqa: E402
from test_vms import VmsTest  # noqa: E402

sys.path.insert(0, REPO)
from agentbox import safeguards, vmenv  # noqa: E402

ENV_FILE = "var/lib/agentbox/vm-env/{}.env"
FWCFG_ENV = os.path.join(REPO, "vm", "files", "usr", "lib", "agentbox", "fwcfg-env")
GUEST_SETUP = os.path.join(REPO, "vm", "files", "usr", "lib", "agentbox", "guest-setup")
# names that would switch a Safeguard off or change what Agent Box sets, one or more per rule
REFUSED = [
    # safeguards.OVERRIDES
    "npm_config_ignore_scripts", "NPM_CONFIG_IGNORE_SCRIPTS", "npm_config_min_release_age",
    "pnpm_config_minimum_release_age_exclude", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS", "UV_NO_CONFIG", "UV_CONFIG_FILE",
    # safeguards.MENTIONS
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy",
    "GIT_CONFIG", "UV_EXCLUDE_NEWER", "BUN_CONFIG_MINIMUM_RELEASE_AGE", "pnpm_config_minimum_release_age",
    "YARN_IGNORE_SCRIPTS", "CORE_HOOKS_PATH", "ENABLE_CLAUDEAI_MCP_SERVERS",
    # reserved: the unit of orca serve, guest-setup, what every process or shell reads first
    "PATH", "HOME", "XDG_RUNTIME_DIR", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "NODE_OPTIONS", "BASH_ENV",
    "ENV", "CLAUDE_CODE_OAUTH_TOKEN", "AGENTBOX_CONTEXT", "AGENTBOX_GH_ADDR", "AGENTBOX_ANYTHING", "ORCA_PORT",
    "PAIRING_ADDRESS",
    # the rest of the Image's DefaultEnvironment
    "NODE_USE_ENV_PROXY", "DO_NOT_TRACK", "ORCA_TELEMETRY_DISABLED", "DISABLE_AUTOUPDATER", "RUSTUP_HOME",
    "JAVA_HOME", "JAVA_TOOL_OPTIONS",
]
# broad prefixes stay allowed: only what a Safeguard or Agent Box itself reads is blocked
ALLOWED = ["npm_config_foo", "npm_config_registry", "UV_PYTHON", "UV_INDEX_URL", "DATABASE_URL", "NODE_ENV",
           "pnpm_config_store_dir", "GIT_AUTHOR_NAME", "PROXYCHAINS_CONF", "path", "_"]
# values whose characters every quoting step must keep as they are
ODD = {"QUOTES": 'say "hi" and \'bye\'', "BACKSLASH": "C:\\path\\n \\\\ end\\", "DOLLAR": "$HOME ${X} $(id) `id`",
       "HASH": "# no comment", "EQUALS": "a=b==c", "SPACES": "  lead and trail  ", "UNICODE": "Grüße ✓ 日本",
       "TAB": "col\tcol", "COMMA": "a,b,entry9.name=x", "EMPTY": ""}


def fw_cfg_line(name, value):
    return f"{name}={base64.b64encode(value.encode()).decode()}"


class EnvTest(VmsTest):
    def setUp(self):
        super().setUp()
        self.ok("regenerate")
        self.forget_calls()

    def env_file(self, vm="work"):
        return self.read(ENV_FILE.format(vm))

    def record_env(self, vm="work"):
        return vm_records.read(self.root, vm).get("env")

    def turned_off(self, vm):
        os.remove(os.path.join(self.dir, "domains.json"))
        self.scenario(vms={vm: {"state": "shut off"}})

    def failure(self, *args):
        """The --json failure of a command that changes nothing."""
        err = self.refused(*args, "--json")
        return json.loads(err)


class SetEnvTest(EnvTest):
    def test_running_vm_gets_them_at_its_next_restart(self):
        result = self.json("set-env", "work", "DATABASE_URL=postgres://db/x", "NODE_ENV=development")
        self.assertEqual(result.pop("message"), "work: VM variables DATABASE_URL, NODE_ENV set, applies from the "
                                                "next Restart (agentbox restart work) or Reset; until then the VM "
                                                "keeps the VM variables it started with")
        self.assertEqual(result, {"vm": "work", "env": {"DATABASE_URL": "postgres://db/x", "NODE_ENV": "development"},
                                  "changed": True, "state": "running", "applies": "next_restart"})
        self.assertEqual(self.record_env(), {"DATABASE_URL": "postgres://db/x", "NODE_ENV": "development"})
        self.assertEqual(self.env_file(), "DATABASE_URL=cG9zdGdyZXM6Ly9kYi94\nNODE_ENV=ZGV2ZWxvcG1lbnQ=\n")
        self.assertEqual(self.mode(ENV_FILE.format("work")), 0o640)
        self.assertEqual(self.mode("var/lib/agentbox/vm-env"), 0o750)
        # the domain names the file already: nothing is defined again, nothing stops or starts
        self.assertEqual(self.virsh("define", "shutdown", "destroy", "start", "managedsave"), [])
        self.assertIsNone(self.record_env("priv"))
        self.assertEqual(self.env_file("priv"), "none\n")
        # regenerate finds everything as it should be
        self.assertEqual(self.json("regenerate")["vms"], {"work": [], "priv": []})

    def test_text_output(self):
        self.assertEqual(self.ok("set-env", "work", "A=1"),
                         "work: VM variables A set, applies from the next Restart (agentbox restart work) or Reset; "
                         "until then the VM keeps the VM variables it started with\n")

    def test_adds_and_changes_keeping_the_others(self):
        self.ok("set-env", "work", "A=1", "B=2")
        result = self.json("set-env", "work", "B=3", "C=4")
        self.assertEqual(result["env"], {"A": "1", "B": "3", "C": "4"})
        self.assertTrue(result["message"].startswith("work: VM variables B, C set,"), result["message"])
        self.assertEqual(self.env_file(), "A=MQ==\nB=Mw==\nC=NA==\n")

    def test_replace_makes_them_the_whole_list(self):
        self.ok("set-env", "work", "A=1", "B=2")
        result = self.json("set-env", "work", "B=2", "C=3", "--replace")
        self.assertEqual((result["env"], result["applies"]), ({"B": "2", "C": "3"}, "next_restart"))
        self.assertTrue(result["message"].startswith("work: VM variables C set; A removed,"), result["message"])
        # nothing given: no VM variables, the file says none (QEMU refuses empty fw_cfg files)
        result = self.json("set-env", "work", "--replace")
        self.assertEqual(result["env"], {})
        self.assertEqual(self.record_env(), {})
        self.assertEqual(self.env_file(), "none\n")

    def test_vm_that_is_off_gets_them_at_its_next_start(self):
        self.turned_off("priv")
        result = self.json("set-env", "priv", "A=1")
        self.assertEqual((result["state"], result["applies"], result["message"]),
                         ("shut off", "next_start", "priv: VM variables A set, applies from the next start"))
        self.assertEqual(self.virsh("start"), [])

    def test_paused_vm_is_not_refused_and_gets_them_at_its_next_restart(self):
        self.ok("pause", "work")
        self.forget_calls()
        result = self.json("set-env", "work", "A=1")
        self.assertEqual((result["state"], result["applies"]), ("paused", "next_restart"))
        self.assertEqual(self.virsh("define", "start", "managedsave-remove"), [])

    def test_same_values_change_nothing(self):
        self.ok("set-env", "work", "A=1")
        before = self.files()
        self.assertEqual(self.ok("set-env", "work", "A=1"), "work: VM variables unchanged\n")
        result = self.json("set-env", "work", "A=1")
        self.assertEqual((result["changed"], result["applies"], result["env"]), (False, None, {"A": "1"}))
        self.assertEqual(self.files(), before)

    def test_odd_values_are_kept_as_they_are(self):
        self.ok("set-env", "work", *(f"{k}={v}" for k, v in ODD.items()))
        self.assertEqual(self.record_env(), ODD)
        self.assertEqual(self.json("env", "work")["env"], ODD)
        lines = self.env_file().splitlines()
        self.assertEqual({line.partition("=")[0]: base64.b64decode(line.partition("=")[2]).decode()
                          for line in lines}, ODD)

    def test_a_value_with_a_comma_never_reaches_the_sysinfo_string(self):
        """virt-install splits --sysinfo at commas: the values travel as a file."""
        self.ok("set-env", "work", "COMMA=a,b,entry9.name=x")
        os.remove(self.path("var/lib/agentbox/domains/work.xml"))       # define again
        self.ok("regenerate")
        argv = [c["argv"] for c in self.calls("virt-install") if "agentbox-work" in c["argv"]][-1]
        sysinfo = argv[argv.index("--sysinfo") + 1]
        self.assertTrue(sysinfo.endswith(f",entry4.name=opt/agentbox/env,entry4.file={self.path(ENV_FILE.format('work'))}"),
                        sysinfo)
        self.assertNotIn("entry9", " ".join(argv))
        self.assertNotIn(base64.b64encode(b"a,b,entry9.name=x").decode(), " ".join(argv))

    def test_usage(self):
        cases = {(): "agentbox: set-env <vm> NAME=VALUE [NAME=VALUE ...] [--replace] [--json]\n",
                 ("work",): "agentbox: set-env <vm> NAME=VALUE [NAME=VALUE ...] [--replace] [--json]\n",
                 ("work", "A"): "agentbox: expected NAME=VALUE, not A\n",
                 ("work", "A=1", "A=2"): "agentbox: A given twice\n",
                 ("work", "A=1", "--force"): "agentbox: unknown option: --force\n",
                 ("nope", "A=1"): "agentbox: No such VM: nope\n"}
        for args, message in cases.items():
            with self.subTest(args=args):
                self.assertEqual(self.refused("set-env", *args), message)


class SafeguardNamesTest(EnvTest):
    """No VM variable may switch a Safeguard off (ADR 0008): what the safeguard check knows, what
    Agent Box sets and every variable of the Image's DefaultEnvironment is refused, and nothing
    changes; broad prefixes stay allowed."""

    def test_names_of_safeguards_and_agent_box_are_refused_and_nothing_changes(self):
        self.ok("set-env", "work", "KEEP=1")
        for name in REFUSED:
            with self.subTest(name=name):
                said = self.failure("set-env", "work", "GOOD=1", f"{name}=false")
                self.assertEqual((said["error"], said["params"]), ("env_name_reserved", {"name": name}))
                self.assertEqual(said["message"], f"{name} cannot be a VM variable: a Safeguard or Agent Box "
                                                  "itself sets or reads it")
                # with --replace too: the list stays as it was
                self.failure("set-env", "work", f"{name}=", "--replace")
        self.assertEqual(self.record_env(), {"KEEP": "1"})
        self.assertEqual(self.env_file(), "KEEP=MQ==\n")

    def test_every_rule_of_the_safeguard_check_is_one_source(self):
        # the patterns come from safeguards.py itself: a name it would reject in a Build is refused
        for name in REFUSED:
            with self.subTest(name=name):
                self.assertTrue(vmenv.reserved(name))
        self.assertTrue(safeguards.OVERRIDES.fullmatch("npm_config_ignore_scripts"))
        self.assertTrue(safeguards.MENTIONS.search("HTTPS_PROXY"))

    def test_broad_prefixes_are_accepted(self):
        result = self.json("set-env", "work", *(f"{n}=1" for n in ALLOWED))
        self.assertEqual(list(result["env"]), ALLOWED)

    def test_every_variable_of_the_images_default_environment_is_reserved(self):
        """agentbox.conf of vm/files and the agentbox-packages.conf package-safeguards writes: a
        variable added there (DOCKER_HOST, ...) must be reserved before a VM variable can undo it."""
        with tempfile.TemporaryDirectory() as tmp:
            dropins = os.path.join(tmp, "usr/lib/systemd/system.conf.d")
            os.makedirs(dropins)
            shutil.copy(os.path.join(REPO, "vm/files/usr/lib/systemd/system.conf.d/agentbox.conf"), dropins)
            subprocess.run([os.path.join(REPO, "vm/files/usr/lib/agentbox/package-safeguards"), "2", tmp],
                           check=True, capture_output=True)
            self.assertEqual(sorted(os.listdir(dropins)), ["agentbox-packages.conf", "agentbox.conf"])
            names = set(safeguards.systemd_environment(tmp))
        self.assertGreaterEqual(names, {"HTTPS_PROXY", "ENABLE_CLAUDEAI_MCP_SERVERS", "UV_EXCLUDE_NEWER",
                                        "pnpm_config_minimum_release_age", "JAVA_TOOL_OPTIONS"})
        self.assertEqual({n for n in names if not vmenv.reserved(n)}, set())
        self.assertEqual(names - vmenv.DEFAULT_ENVIRONMENT, set())

    def test_names_follow_the_pattern(self):
        for name in ("1A", "A-B", "A B", "Ä", "A.B", "A" * 65, "-x"):
            with self.subTest(name=name):
                said = self.failure("set-env", "work", f"{name}=1")
                self.assertEqual(said["error"], "invalid_env_name")
        self.assertEqual(self.failure("set-env", "work", "=1")["error"], "invalid_env_name")
        self.ok("set-env", "work", "A" * 64 + "=1")

    def test_limits(self):
        # 64 variables, values up to 4 KiB in UTF-8
        self.ok("set-env", "work", *(f"V{i}=x" for i in range(64)))
        said = self.failure("set-env", "work", "ONE_MORE=x")
        self.assertEqual((said["error"], said["params"]), ("env_too_large", {"max_variables": 64}))
        self.ok("set-env", "work", "--replace", "BIG=" + "x" * 4096)
        said = self.failure("set-env", "work", "BIG=" + "x" * 4097)
        self.assertEqual((said["error"], said["params"]), ("env_too_large", {"name": "BIG", "max_bytes": 4096}))
        self.assertEqual(self.failure("set-env", "work", "BIG=" + "ü" * 2049)["error"], "env_too_large")
        # no NUL (no argument can carry one), no line break, no other control character
        for value in ("a\nb", "a\rb", "a\x1b[31mred", "\x7f"):
            with self.subTest(value=value):
                said = self.failure("set-env", "work", f"A={value}")
                self.assertEqual((said["error"], said["params"]), ("invalid_env_value", {"name": "A"}))
        self.assertEqual(self.record_env(), {"BIG": "x" * 4096})

    def test_a_stored_variable_whose_name_is_refused_now_is_kept_but_never_passed_on(self):
        """A later release may reserve a name a VM has already: its record stays valid (a broken
        record would stop regenerate and create for every VM), the VM never gets that variable,
        and agentbox env and describe say so. Setting it stays refused, removing it works."""
        vm_records.write(self.root, vm_records.record("work", 1, env={"HTTPS_PROXY": "x", "A": "1"}))
        self.assertEqual(self.json("env", "work"),
                         {"vm": "work", "env": {"HTTPS_PROXY": "x", "A": "1"}, "refused": ["HTTPS_PROXY"]})
        self.assertEqual(self.ok("env", "work"),
                         "HTTPS_PROXY=x\nA=1\nwork: HTTPS_PROXY is not passed on to the VM: a Safeguard or Agent "
                         "Box itself sets or reads it (agentbox unset-env work HTTPS_PROXY removes it)\n")
        described = {v["name"]: v for v in self.json("describe")["vms"]}
        self.assertEqual((described["work"]["env_refused"], described["priv"]["env_refused"]), (["HTTPS_PROXY"], []))
        self.assertIn("  VM variables: HTTPS_PROXY, A (agentbox env work); HTTPS_PROXY not passed on\n",
                      self.ok("describe", "work"))
        # regenerate and create go on; the file QEMU passes in never carries the name
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["vm-env"])
        self.assertEqual(self.env_file(), "A=MQ==\n")
        self.ok("create", "lab")
        # other variables can still be set, the refused one stays in the record only
        self.ok("set-env", "work", "B=2")
        self.assertEqual(self.record_env(), {"HTTPS_PROXY": "x", "A": "1", "B": "2"})
        self.assertEqual(self.env_file(), "A=MQ==\nB=Mg==\n")
        said = self.failure("set-env", "work", "HTTPS_PROXY=y")
        self.assertEqual((said["error"], said["params"]), ("env_name_reserved", {"name": "HTTPS_PROXY"}))
        self.ok("unset-env", "work", "HTTPS_PROXY")
        self.assertEqual(self.json("env", "work"), {"vm": "work", "env": {"A": "1", "B": "2"}, "refused": []})

    def test_a_record_with_malformed_variables_is_broken(self):
        for env in ({"A": 1}, ["A"], {"A": "x\n"}, {"1A": "x"}, {"A": "x" * 4097},
                    {f"V{i}": "x" for i in range(65)}):
            with self.subTest(env=str(env)[:40]):
                vm_records.write(self.root, vm_records.record("work", 1, env=env))
                code, out, err = self.run_agentbox("env", "work")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("work.json: broken record (", err)
        # without the field: no VM variables
        vm_records.write(self.root, vm_records.record("work", 1))
        self.assertEqual(self.json("env", "work"), {"vm": "work", "env": {}, "refused": []})

    def test_the_file_never_carries_a_refused_name(self):
        self.assertEqual(vmenv.fw_cfg({"HTTPS_PROXY": "x", "A": "1", "PATH": "/tmp"}), b"A=MQ==\n")
        self.assertEqual(vmenv.fw_cfg({"LD_PRELOAD": "/x.so"}), b"none\n")


class UnsetAndShowTest(EnvTest):
    def test_unset_removes_them(self):
        self.ok("set-env", "work", "A=1", "B=2", "C=3")
        result = self.json("unset-env", "work", "A", "C", "NOT_SET")
        self.assertEqual((result["env"], result["applies"]), ({"B": "2"}, "next_restart"))
        self.assertTrue(result["message"].startswith("work: VM variables A, C removed,"), result["message"])
        self.assertEqual(self.env_file(), "B=Mg==\n")
        self.assertEqual(self.ok("unset-env", "work", "NOT_SET"), "work: VM variables unchanged\n")
        self.ok("unset-env", "work", "B")
        self.assertEqual(self.env_file(), "none\n")

    def test_unset_usage(self):
        self.assertEqual(self.refused("unset-env", "work"), "agentbox: unset-env <vm> NAME [NAME ...] [--json]\n")
        self.assertEqual(self.failure("unset-env", "work", "A-B")["error"], "invalid_env_name")

    def test_env_shows_names_and_values(self):
        self.assertEqual(self.ok("env", "work"), "work: no VM variables\n")
        self.ok("set-env", "work", "A=x y", "B=")
        self.assertEqual(self.ok("env", "work"), "A=x y\nB=\n")
        self.assertEqual(self.json("env", "work"), {"vm": "work", "env": {"A": "x y", "B": ""}, "refused": []})
        self.assertEqual(self.refused("env"), "agentbox: env <vm> [--json]\n")

    def test_each_waits_for_the_registry_the_record_and_the_lifecycle_lock(self):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        for lock, args in (("vms.lock", ("set-env", "work", "A=1")), ("work.json.lock", ("set-env", "work", "B=1")),
                           ("lifecycle-work.lock", ("unset-env", "work", "A"))):
            with self.subTest(lock=lock):
                before = self.record_env()
                path = self.path(f"var/lib/agentbox/locks/{lock}")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                p = subprocess.Popen([AGENTBOX, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env=env)
                try:
                    time.sleep(1)
                    self.assertIsNone(p.poll())
                    self.assertEqual(self.record_env(), before)
                finally:
                    os.close(fd)
                _, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.record_env(), {"B": "1"})


class OlderDomainTest(EnvTest):
    """A domain defined before VM variables has no fw_cfg entry opt/agentbox/env, and an RPM
    upgrade runs no regenerate: set-env and unset-env define the VM again, as set-memory does,
    so it has its variables from the next start."""

    def older_domain(self, vm="work"):
        """The saved definition becomes one from before VM variables, without the entry."""
        saved = self.path(f"var/lib/agentbox/domains/{vm}.xml")
        with open(saved) as f:
            xml = f.read()
        older = re.sub(r",entry4\.name=opt/agentbox/env,entry4\.file=[^\"]*", "", xml)
        self.assertNotEqual(older, xml)
        with open(saved, "w") as f:
            f.write(older)
        self.forget_calls()

    def defined(self, vm="work"):
        return [c["xml"] for c in self.virsh("define") if c["dom"] == f"agentbox-{vm}"]

    def test_set_env_defines_the_vm_again(self):
        self.older_domain()
        result = self.json("set-env", "work", "A=1")
        self.assertEqual((result["changed"], result["state"], result["applies"]), (True, "running", "next_restart"))
        self.assertEqual(len(self.defined()), 1)
        self.assertIn("entry4.name=opt/agentbox/env", self.defined()[0])
        self.assertEqual(self.defined("priv"), [])
        # nothing stops, starts or pauses it now
        self.assertEqual(self.virsh("shutdown", "destroy", "start", "managedsave", "managedsave-remove"), [])
        self.assertEqual(self.json("regenerate")["vms"], {"work": [], "priv": []})

    def test_unchanged_variables_still_define_it(self):
        self.ok("set-env", "work", "A=1")
        self.older_domain()
        result = self.json("unset-env", "work", "NOT_SET")
        self.assertEqual((result["changed"], result["applies"]), (True, "next_restart"))
        self.assertTrue(result["message"].startswith("work: VM variables written again, applies from the next "
                                                     "Restart"), result["message"])
        self.assertEqual(len(self.defined()), 1)
        # defined now: the same again changes nothing
        self.assertEqual(self.ok("set-env", "work", "A=1"), "work: VM variables unchanged\n")

    def test_vm_that_is_off_gets_them_at_its_next_start(self):
        self.turned_off("priv")
        self.older_domain("priv")
        result = self.json("set-env", "priv", "A=1")
        self.assertEqual((result["state"], result["applies"]), ("shut off", "next_start"))
        self.assertEqual(len(self.defined("priv")), 1)

    def test_paused_vm_is_defined_once_it_has_no_pause(self):
        self.ok("pause", "work")
        self.older_domain()
        result = self.json("set-env", "work", "A=1")
        self.assertEqual((result["changed"], result["state"]), (True, "paused"))
        self.assertEqual(result["message"],
                         "work: VM variables A set; the VM is paused, so it is defined again only once it no longer "
                         "has a Pause (agentbox start work, then agentbox regenerate), and the VM variables apply "
                         "from the next Restart or Reset after that")
        self.assertEqual(self.virsh("define", "start", "managedsave-remove"), [])
        self.assertEqual(self.record_env(), {"A": "1"})
        self.ok("start", "work")
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["domain"])

    def test_failing_definition_keeps_the_variables_and_says_how_to_finish(self):
        self.older_domain()
        self.scenario(virt_install_fails=["agentbox-work"])
        code, out, err = self.run_agentbox("set-env", "work", "A=1", "--json")
        self.assertEqual((code, out), (1, ""))
        said = json.loads(err)
        self.assertEqual((said["error"], said["params"]), ("env_not_applied", {"vm": "work"}))
        self.assertTrue(said["message"].startswith("work: VM variables saved, but the VM was not defined again: "
                                                   "work: virt-install fails"), said["message"])
        self.assertTrue(said["message"].endswith("; agentbox regenerate finishes it"), said["message"])
        self.assertEqual((self.record_env(), self.env_file()), ({"A": "1"}, "A=MQ==\n"))
        self.scenario()
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["domain"])
        self.assertIn("entry4.name=opt/agentbox/env", self.defined()[-1])


class RegenerateEnvTest(EnvTest):
    def test_regenerate_writes_the_file_only_when_it_changed(self):
        self.assertEqual((self.env_file("work"), self.env_file("priv")), ("none\n", "none\n"))
        before = self.files()
        self.assertEqual(self.json("regenerate")["vms"], {"work": [], "priv": []})
        self.assertEqual(self.files(), before)
        # a record changed (set-env died before writing the file, or by hand): the file follows,
        # the domain stays as it is
        vm_records.write(self.root, vm_records.record("priv", 2, env={"A": "1"}))
        self.assertEqual(self.json("regenerate")["vms"], {"work": [], "priv": ["vm-env"]})
        self.assertEqual(self.env_file("priv"), "A=MQ==\n")
        self.assertEqual(self.virsh("define"), [])
        # a file changed by hand is written again
        self.write(ENV_FILE.format("priv"), "PATH=L3RtcA==\n", mode=0o640)
        self.assertEqual(self.json("regenerate")["vms"]["priv"], ["vm-env"])
        self.assertEqual(self.env_file("priv"), "A=MQ==\n")

    def test_written_before_the_domain_is_defined(self):
        vm_records.write(self.root, vm_records.record("lab", 3, env={"A": "1"}))
        self.forget_calls()
        result = self.json("regenerate")["vms"]["lab"]
        self.assertLess(result.index("vm-env"), result.index("domain"))
        self.assertEqual(self.env_file("lab"), "A=MQ==\n")


class CreateDeleteEnvTest(EnvTest):
    def test_create_refuses_a_leftover_file_and_delete_removes_it(self):
        self.write("var/lib/agentbox/vm-env/lab.env", "OLD=MQ==\n")
        self.assertEqual(self.refused("create", "lab"),
                         "agentbox: lab: leftovers of an earlier VM exist (VM variables), remove them first\n")
        os.remove(self.path("var/lib/agentbox/vm-env/lab.env"))
        self.ok("create", "lab")
        self.assertEqual(self.env_file("lab"), "none\n")
        self.ok("set-env", "lab", "A=1")
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertFalse(self.exists("var/lib/agentbox/vm-env/lab.env"))


def systemd_environment_file(text):
    """{name: value} as systemd's EnvironmentFile= reads the lines guest-setup writes (systemd.exec:
    unquoted values without leading and trailing whitespace; in double quotes \\ before one of
    "\\`$ keeps that character, before anything else both stay)."""
    env = {}
    for line in text.splitlines():
        name, eq, raw = line.partition("=")
        if not eq or line.lstrip().startswith(("#", ";")):
            continue
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            value, i, inner = "", 0, raw[1:-1]
            while i < len(inner):
                if inner[i] == "\\" and i + 1 < len(inner):
                    value += inner[i + 1] if inner[i + 1] in '"\\`$' else inner[i:i + 2]
                    i += 2
                    continue
                if inner[i] == '"':
                    raise AssertionError(f"unescaped quote in {line!r}")
                value += inner[i]
                i += 1
        else:
            value = raw
        env[name.strip()] = value
    return env


class GuestTest(unittest.TestCase):
    """fwcfg-env, which guest-setup runs at every boot, reads an fw_cfg entry into
    /run/agentbox/env (systemd's EnvironmentFile of orca serve) and /run/agentbox/agent-env
    (sourced by the shells' profile)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.entry, self.env, self.agent_env = (os.path.join(self.dir, n) for n in ("raw", "env", "agent-env"))
        for f in (self.env, self.agent_env):
            open(f, "w").close()

    def run_guest(self, lines=None):
        if lines is not None:
            with open(self.entry, "w") as f:
                f.write("".join(line + "\n" for line in lines))
        r = subprocess.run([FWCFG_ENV, self.entry, self.env, self.agent_env], capture_output=True, text=True,
                           timeout=30, env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stderr

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def from_bash(self, *names):
        """What a shell sourcing agent-env has (the profile does that)."""
        script = '. "$1"; shift; for n; do printf "%s\\0" "${!n}"; done'
        r = subprocess.run(["bash", "-c", script, "bash", self.agent_env, *names], capture_output=True,
                           timeout=30, env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return dict(zip(names, r.stdout.decode().split("\0")))

    def test_values_come_out_unchanged_for_systemd_and_bash(self):
        self.assertEqual(self.run_guest([fw_cfg_line(k, v) for k, v in ODD.items()]), "")
        self.assertEqual(systemd_environment_file(self.read(self.env)), ODD)
        self.assertEqual(self.from_bash(*ODD), ODD)

    def test_values_come_out_unchanged_for_systemd_itself(self):
        """The same with systemd's own parser, where a user manager can run a unit (not in CI)."""
        self.run_guest([fw_cfg_line(k, v) for k, v in ODD.items()])
        try:
            r = subprocess.run(["systemd-run", "--user", "--quiet", "--wait", "--pipe", "-p",
                                f"EnvironmentFile={self.env}", "/usr/bin/env", "-0"], capture_output=True,
                               timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("no systemd-run")
        if r.returncode != 0:
            self.skipTest("no user manager to run a unit")
        seen = dict(e.partition("=")[::2] for e in r.stdout.decode().split("\0") if "=" in e)
        self.assertEqual({k: seen.get(k) for k in ODD}, ODD)

    def test_a_missing_entry_or_none_means_no_variables(self):
        self.assertEqual(self.run_guest(), "")
        self.run_guest(["none"])
        self.assertEqual((self.read(self.env), self.read(self.agent_env)), ("", ""))

    def test_bad_lines_are_left_out_with_a_message(self):
        err = self.run_guest(["1BAD=eA==", "A-B=eA==", "NOEQUALS", "$(id)=eA==", "JUNK=!!!",
                              fw_cfg_line("NEWLINE", "a\nb"), fw_cfg_line("CR", "a\rb"), "NUL=YQBi",
                              fw_cfg_line("GOOD", "yes")])
        self.assertEqual(self.read(self.env), 'GOOD="yes"\n')
        self.assertEqual(self.read(self.agent_env), "export GOOD=yes\n")
        self.assertEqual(len(err.splitlines()), 8, err)
        self.assertIn("raw: line 1 is no variable, left out", err)
        self.assertIn("raw: JUNK is not base64, left out", err)
        self.assertIn("raw: NUL has a line break or NUL, left out", err)

    def test_it_adds_to_what_is_there(self):
        with open(self.env, "w") as f:
            f.write("FIRST=1\n")
        self.run_guest([fw_cfg_line("A", "1")])
        self.assertEqual(self.read(self.env), 'FIRST=1\nA="1"\n')

    def test_nothing_is_evaluated(self):
        marker = os.path.join(self.dir, "ran")
        self.run_guest([fw_cfg_line("A", f"$(touch {marker})"), fw_cfg_line("B", f"`touch {marker}`")])
        self.from_bash("A", "B")
        self.assertFalse(os.path.exists(marker))
        with open(FWCFG_ENV) as f:
            code = [line for line in f if not line.lstrip().startswith("#")]
        self.assertFalse(any(re.search(r"\beval\b|\bsource\b|^\s*\.\s", line) for line in code))

    def test_guest_setup_puts_the_fixed_values_after_the_variables(self):
        with open(GUEST_SETUP) as f:
            lines = [line.rstrip("\n") for line in f]
        code = [line for line in lines if not line.lstrip().startswith("#")]
        start = next(i for i, line in enumerate(code) if line.startswith("/usr/lib/agentbox/fwcfg-env "))
        self.assertIn("/sys/firmware/qemu_fw_cfg/by_name/opt/agentbox/env/raw", code[start])
        self.assertEqual(code[start + 1].split(), ["/run/agentbox/env", "/run/agentbox/agent-env"])
        # both files are created empty before, every fixed value is appended after
        created = [i for i, line in enumerate(code) if line.startswith("install -m") and "/run/agentbox/" in line]
        self.assertEqual(len(created), 2)
        self.assertLess(max(created), start)
        writes = [(i, line) for i, line in enumerate(code) if re.search(r">>? */run/agentbox/(env|agent-env)\b", line)]
        self.assertEqual(len(writes), 3)
        for i, line in writes:
            self.assertGreater(i, start, line)
            self.assertRegex(line, r">> /run/agentbox/(env|agent-env)$")
        self.assertFalse(any(re.search(r"\beval\b", line) for line in code))


if __name__ == "__main__":
    unittest.main()
