"""Where Agent Box finds its own files (agentbox/paths.py, ARCHITECTURE.md "Where the files
live"): one layout under the prefix /usr, where the RPM puts it, the files agentbox opens or
runs under AGENTBOX_ROOT in tests.

The CLI and the Control Room backend run from copies installed under the test root the way
the RPM lays them out, so what they name and open is that installation's.

Run from the repo root: python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from test_registry import AGENTBOX, PRIV_UUID, REPO, RegistryTest  # noqa: E402

RELEASE = "1.2.3"
# a path in a unit that is a file of Agent Box (socat and the like are not)
NAMED = re.compile(r"^ExecStart(?:Pre)?=(/\S*agentbox\S*)", re.MULTILINE)


def rpm(prefix):
    """The files the units name, under prefix."""
    return {f"{prefix}/bin/agentbox", f"{prefix}/libexec/agentbox-proxy", f"{prefix}/libexec/agentbox-gh-tokend",
            f"{prefix}/libexec/agentbox-feeds"}


class InstalledTest(RegistryTest):
    def setUp(self):
        super().setUp()
        vm_records.write(self.root)
        vm_records.write_host_setup(self.root)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})

    def install(self, prefix):
        """The CLI as installed under prefix (package, VERSION, starter, Control Room backend);
        returns the starter."""
        lib = self.path(f"{prefix[1:]}/lib/agentbox")
        shutil.copytree(os.path.join(REPO, "agentbox"), os.path.join(lib, "agentbox"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(lib, "VERSION"), "w") as f:
            f.write(RELEASE + "\n")
        os.makedirs(self.path(f"{prefix[1:]}/libexec"))
        shutil.copy2(os.path.join(REPO, "web", "agentbox-web"), self.path(f"{prefix[1:]}/libexec/agentbox-web"))
        starter = self.path(f"{prefix[1:]}/bin/agentbox")
        os.makedirs(os.path.dirname(starter))
        shutil.copy2(AGENTBOX, starter)
        return starter

    def run_with(self, starter, *args):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        r = subprocess.run([starter, *args], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def units(self):
        """Every unit agentbox regenerate wrote, by name."""
        directory = self.path("etc/systemd/system")
        units = {}
        for name in os.listdir(directory):
            if os.path.isfile(os.path.join(directory, name)):
                with open(os.path.join(directory, name)) as f:
                    units[name] = f.read()
        return units

    def regenerate(self, starter):
        self.assertEqual(self.run_with(starter, "regenerate")[::2], (0, ""))
        return self.units()

    def named(self, units):
        return {path for text in units.values() for path in NAMED.findall(text)}

    def test_a_checkout_names_the_files_of_the_rpm(self):
        self.assertEqual(self.named(self.regenerate(AGENTBOX)), rpm("/usr"))

    def test_the_rpm_installation_names_its_own_files_and_reads_its_version(self):
        starter = self.install("/usr")
        self.assertEqual(self.named(self.regenerate(starter)), rpm("/usr"))
        self.assertEqual(self.run_with(starter, "version"), (0, f"agentbox {RELEASE}\n", ""))

    def test_the_classifier_it_runs_is_the_one_of_its_installation(self):
        for name, starter in (("checkout", AGENTBOX), ("rpm", self.install("/usr"))):
            with self.subTest(name):
                self.assertEqual(self.run_with(starter, "check-domain", "example.org"),
                                 (127, "", f"agentbox: {self.root}/usr/libexec/agentbox-suspicious is missing\n"))

    def test_the_control_room_calls_and_serves_the_files_of_its_installation(self):
        self.install("/usr")
        backend = self.path("usr/libexec/agentbox-web")
        env = {"PATH": os.environ["PATH"], "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8"}
        out = subprocess.run([sys.executable, "-I", "-c", """if True:
            import importlib.machinery, importlib.util, json, sys
            loader = importlib.machinery.SourceFileLoader("agentbox_web", sys.argv[1])
            web = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
            loader.exec_module(web)
            print(json.dumps([web.AGENTBOX, web.STATIC, web.UI, web.release()]))
            """, backend], capture_output=True, text=True, env=env, timeout=60, check=True).stdout
        self.assertEqual(json.loads(out), [["sudo", "-n", "/usr/bin/agentbox"], f"{self.root}/usr/share/agentbox/web",
                                           f"{self.root}/usr/share/agentbox/web/ui", RELEASE])


class CheckoutTest(unittest.TestCase):
    """A checkout's CLI names the RPM's files, not its own, which need not be there: without
    AGENTBOX_ROOT it changes nothing, it only reads.

    In process, with every way to the Host cut off: no test root, so the guard applies, but
    every command and the Activity journal replaced by recorders, so even a broken guard
    reaches nothing real. Root is pretended, so the root check lets every command through."""

    def setUp(self):
        sys.path.insert(0, REPO)
        self.addCleanup(sys.path.remove, REPO)
        from agentbox import activity, cli, paths
        self.cli, self.ran, self.recorded = cli, [], []

        @contextlib.contextmanager
        def command(cmd, args):
            self.recorded.append(cmd)
            yield

        def journal(*args, **kw):
            raise AssertionError(f"the Activity journal was reached: {args}")

        def dispatched(name):
            return lambda args: self.ran.append(name)

        for target, name, value in ((paths, "ROOT", ""), (paths, "INSTALLED", False),
                                    (cli, "COMMANDS", {n: dispatched(n) for n in cli.COMMANDS}),
                                    (activity, "command", command), (activity, "record", journal),
                                    (cli.os, "geteuid", lambda: 0)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self, *args):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = self.cli.main(["--actor=test", *args])
        return code, stderr.getvalue()

    def test_commands_that_change_the_host_refuse_before_anything_runs(self):
        for args in (["regenerate"], ["set-settings", "maintenance_time=05:00"], ["create", "lab"], ["setup"],
                     ["weekly-check"], ["maintenance"], ["build", "base"], ["allow", "work", "example.org"],
                     ["pair", "work"]):
            with self.subTest(args=args):
                self.assertEqual(self.main(*args), (
                    1, f"agentbox: {args[0]}: this agentbox is not installed ({REPO}) and does not change the "
                       "Host, its units would name files that need not exist here; use the installed one: "
                       f"sudo agentbox {' '.join(args)}\n"))
        self.assertEqual((self.ran, self.recorded), ([], []))

    def test_reading_still_works(self):
        self.assertEqual(self.main("version"), (0, ""))
        self.assertEqual(self.ran, ["version"])


class SystemTest(unittest.TestCase):
    """The module itself: system locations, and AGENTBOX_ROOT moving what agentbox opens."""

    def paths(self, **env):
        out = subprocess.run([sys.executable, "-I", "-c", """if True:
            import json, sys
            sys.path.insert(0, sys.argv[1])
            from agentbox import paths
            print(json.dumps({name: getattr(paths, name) for name in (
                "PREFIX", "CLI", "PROXY", "TOKEND", "FEEDS_PROGRAM", "SUSPICIOUS", "WEB_BACKEND", "WEB_STATIC",
                "WEB_UI", "VM_SOURCE", "SEED_LISTS", "VERSION", "CONF", "STATE", "SYSTEMD")}))
            """, REPO], capture_output=True, text=True, env={"PATH": os.environ["PATH"], **env}, timeout=60,
                             check=True).stdout
        return json.loads(out)

    def test_system_locations(self):
        self.assertEqual(self.paths(), {
            "PREFIX": "/usr", "CLI": "/usr/bin/agentbox", "PROXY": "/usr/libexec/agentbox-proxy",
            "TOKEND": "/usr/libexec/agentbox-gh-tokend", "FEEDS_PROGRAM": "/usr/libexec/agentbox-feeds",
            "SUSPICIOUS": "/usr/libexec/agentbox-suspicious", "WEB_BACKEND": "/usr/libexec/agentbox-web",
            "WEB_STATIC": "/usr/share/agentbox/web", "WEB_UI": "/usr/share/agentbox/web/ui",
            "VM_SOURCE": "/usr/share/agentbox/vm", "SEED_LISTS": "/usr/share/agentbox/lists",
            # a checkout's own VERSION; installed, the one beside the package
            "VERSION": os.path.join(REPO, "VERSION"),
            "CONF": "/etc/agentbox", "STATE": "/var/lib/agentbox", "SYSTEMD": "/etc/systemd/system"})

    def test_under_agentbox_root_what_agentbox_opens_moves_and_what_it_names_stays(self):
        root = "/tmp/agentbox-test-root"
        moved = self.paths(AGENTBOX_ROOT=root)
        system = self.paths()
        for name in ("SUSPICIOUS", "WEB_BACKEND", "WEB_STATIC", "WEB_UI", "VM_SOURCE", "SEED_LISTS", "CONF", "STATE",
                     "SYSTEMD"):
            self.assertEqual(moved[name], root + system[name], name)
        for name in ("PREFIX", "CLI", "PROXY", "TOKEND", "FEEDS_PROGRAM", "VERSION"):
            self.assertEqual(moved[name], system[name], name)


if __name__ == "__main__":
    unittest.main()
