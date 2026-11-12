"""Updating and removing the RPM (rpm/agentbox.spec, ADR 0009, #82).

GeneratedUnitsTest needs only the repository: every unit `agentbox regenerate` writes is one the
package disables and deletes on removal, and none of the package's own units is.

The other classes are the update and removal test. rpm/upgrade-test.sh runs them as root in a
fresh Fedora container, after each of its steps on a Host in use (tests/rpm_host.py): the
update from an older Release to this one (where restarting the proxies fails once), two
reinstalls of this one, the removal. It names the
step in AGENTBOX_RPM_STEP and its directory in AGENTBOX_RPM_UPGRADE_STATE, which holds the
file system snapshots (`before` the update, then one per step), the calls of systemctl,
systemd-update-helper, virsh, virt-install, qemu-img, podman, nft and firewall-cmd during each
step (`calls-<step>`), and the files of both Releases (`files-older`, `files-current`).
Anywhere else they skip.

Run from the repo root: python3 -m unittest discover -s tests
"""
import fnmatch
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)
import vm_records  # noqa: E402

STATE = os.environ.get("AGENTBOX_RPM_UPGRADE_STATE", "")
STEP = os.environ.get("AGENTBOX_RPM_STEP", "")
RECORDS = ("/etc/agentbox", "/var/lib/agentbox")      # the Host's records and state
SYSTEMD = "/etc/systemd"
PACKAGE_UNITS = sorted(n for n in os.listdir(os.path.join(REPO, "rpm", "units")) if n.endswith((".service", ".timer")))
# on every update (%postun)
RESTARTED = {"agentbox-web.service", "agentbox-allowlist-expire.timer", "agentbox-container-image-cleanup.timer"}
# on an update that changes the proxy code (%posttrans): the proxies that run, at once
PROXY_RESTART = ["systemctl", "try-restart", "agentbox-proxy-*.service"]
PENDING = "/run/agentbox-proxy-restart"      # a restart of the proxies that failed, for the next update
# systemd's own file triggers: daemon-reload and the restart of what the scriptlets marked
SYSTEMD_TRIGGERS = {"system-reload-restart", "system-reload", "system-restart", "user-reload-restart",
                    "user-reload", "user-restart"}


def read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def generated_patterns():
    """The file names the spec's %preun disables and deletes on removal."""
    return re.search(r'^\s*generated="([^"]*)"', read("rpm", "agentbox.spec"), re.MULTILINE).group(1).split()


class GeneratedUnitsTest(unittest.TestCase):
    """The checkout's `agentbox regenerate` for two VMs, with fake virsh, virt-install, qemu-img
    and systemctl, as in test_registry.py."""

    def test_removal_takes_every_unit_regenerate_writes_and_none_of_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "root")
            for sub in ("var/lib/agentbox/vms", "etc/agentbox/github", "etc/agentbox/secrets", "etc/systemd/system"):
                os.makedirs(os.path.join(root, sub))
            os.makedirs(os.path.join(tmp, "bin"))
            for name, program in (("virsh", "fake_virsh.py"), ("virt-install", "fake_virt_install.py"),
                                  ("qemu-img", "fake_qemu_img.py"), ("systemctl", "fake_systemctl.py"),
                                  ("firewall-cmd", "fake_firewall_cmd.py")):
                with open(os.path.join(tmp, "bin", name), "w") as f:
                    f.write(f'#!/bin/sh\nexec python3 "{os.path.join(TESTS, program)}" "$@"\n')
                os.chmod(os.path.join(tmp, "bin", name), 0o755)
            vm_records.write(root)
            vm_records.write_host_setup(root)
            env = {"PATH": os.path.join(tmp, "bin") + ":" + os.environ["PATH"], "FAKE_DIR": tmp,
                   "FAKE_VIRSH_DIR": tmp, "AGENTBOX_ROOT": root, "LANG": "C.UTF-8", "TZ": "UTC"}
            r = subprocess.run([os.path.join(REPO, "bin", "agentbox"), "regenerate"], capture_output=True, text=True,
                               env=env, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            written = sorted(os.listdir(os.path.join(root, "etc", "systemd", "system")))
        patterns = generated_patterns()
        self.assertIn("agentbox-proxy-work.service", written)
        self.assertIn("agentbox-rebuild.timer", written)
        self.assertEqual([n for n in written if not any(fnmatch.fnmatchcase(n, p) for p in patterns)], [])
        # never the Control Room, the VM network rules (agentbox-vmnet) or the expiry timer
        self.assertEqual([n for n in PACKAGE_UNITS if any(fnmatch.fnmatchcase(n, p) for p in patterns)], [])


def load(name):
    with open(os.path.join(STATE, name), encoding="utf-8") as f:
        return json.load(f)


def lines(name):
    with open(os.path.join(STATE, name), encoding="utf-8") as f:
        return f.read().splitlines()


def same(a, b):
    """Two snapshot entries alike: everything for files and links; for directories type, mode
    and owners, since an entry added or removed elsewhere changes their time."""
    if a is None or b is None or a[0] != b[0]:
        return a == b
    return a[:4] == b[:4] if a[0] == "d" else a == b


class StepTest(unittest.TestCase):
    """One step of rpm/upgrade-test.sh, compared with the Host before the update."""
    step = None

    @classmethod
    def setUpClass(cls):
        if STEP != cls.step:
            raise unittest.SkipTest(f"only after the step {cls.step} of rpm/upgrade-test.sh")
        cls.before, cls.after = load("before"), load(STEP)
        cls.package = set(lines("files-older"))
        if os.path.exists(os.path.join(STATE, "files-current")):
            cls.package |= set(lines("files-current"))
        cls.calls = [line.split("\t") for line in lines(f"calls-{STEP}")]
        cls.release = read("VERSION").strip()

    def changed(self, paths):
        """Of these paths: those whose entry differs after the step, gone or new."""
        return sorted(p for p in paths if not same(self.before.get(p), self.after.get(p)))

    def under(self, tops):
        """The paths in these directories, before or after the step, with the directories."""
        inside = tuple(top + "/" for top in tops)
        return {p for p in self.before.keys() | self.after.keys() if p in tops or p.startswith(inside)}

    def helper(self, verb):
        """The units of every call of systemd-update-helper with this verb."""
        return [call[2:] for call in self.calls if call[0] == "systemd-update-helper" and call[1] == verb]

    def restarted(self):
        """What the update restarts if it runs: the units the scriptlets marked for systemd's
        try-restart at the end of the transaction, and the calls of systemctl."""
        marked = sorted(unit for units in self.helper("mark-restart-system-units") for unit in units)
        self.assertLessEqual({call[1] for call in self.calls if call[0] == "systemd-update-helper"},
                             {"mark-restart-system-units"} | SYSTEMD_TRIGGERS, self.calls)
        return marked, [call for call in self.calls if call[0] == "systemctl"]

    def assertRecordsUntouched(self):
        """The Host setup, Settings, the VM registry, the lists, password, certificate, GitHub
        App, Builds, disks, domain definitions and every other file of the Host's in
        /etc/agentbox and /var/lib/agentbox: byte for byte, with their modes, owners and times."""
        records = self.under(RECORDS)
        self.assertIn("/etc/agentbox/vms/work.json", records)
        self.assertIn("/var/lib/agentbox/vms/work-data.qcow2", records)
        self.assertEqual(self.changed(records), [])

    def assertNothingElseChanged(self, beside=()):
        """Outside the package's own files (and the paths named), nothing on the file system
        changed: no file written, no unit enabled or disabled, no user added. Only the library
        cache, which glibc's trigger writes again for anything installed in /usr/lib."""
        everything = self.before.keys() | self.after.keys()
        self.assertEqual([p for p in self.changed(everything)
                          if p not in self.package and p not in beside and p != "/etc/ld.so.cache"], [])

    def assertNoVmOperation(self, programs):
        """Nothing ran virsh, virt-install, qemu-img, podman, nft or firewall-cmd: no VM started,
        stopped, paused, reset or defined, no Build, no network rule or firewall change."""
        self.assertLessEqual({call[0] for call in self.calls}, programs, self.calls)


class UpgradedTest(StepTest):
    """`dnf upgrade` from the older Release, whose proxy code differs."""
    step = "upgraded"

    def test_the_host_runs_the_new_release(self):
        version = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}", "agentbox"], capture_output=True, text=True,
                                 check=True).stdout
        self.assertEqual(version, self.release.replace("-", "~"))
        r = subprocess.run(["/usr/bin/agentbox", "version"], capture_output=True, text=True, user="nobody",
                           group="nobody", extra_groups=[], env={"PATH": "/usr/bin"})
        self.assertEqual((r.returncode, r.stdout), (0, f"agentbox {self.release}\n"))
        r = subprocess.run(["rpm", "-V", "agentbox"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_the_records_and_state_of_the_host_are_untouched(self):
        self.assertRecordsUntouched()

    def test_nothing_is_enabled_disabled_or_rewritten(self):
        """Units, the links that enable them, the journald namespace: as before."""
        self.assertIn("/etc/systemd/system/sockets.target.wants/agentbox-proxy-work.socket", self.before)
        self.assertEqual(self.changed(self.under((SYSTEMD,))), [])
        self.assertNothingElseChanged()

    def test_no_vm_is_touched(self):
        self.assertNoVmOperation({"systemd-update-helper", "systemctl"})
        self.assertEqual(self.helper("remove-system-units"), [])
        self.assertEqual(self.helper("install-system-units"), [])

    def test_restarts_the_control_room_the_timer_and_the_proxies_whose_code_changed(self):
        """try-restart: only what runs. The token service, the Orca ports and agentbox-vmnet
        are not restarted. Here the restart of the proxies fails, so it stays pending."""
        self.assertEqual(self.restarted(), (sorted(RESTARTED), [PROXY_RESTART]))
        self.assertTrue(os.path.exists(PENDING))


class RetriedTest(StepTest):
    """`dnf reinstall` of this Release after the update whose restart of the proxies failed:
    the same proxy code, but the proxies still run the old one."""
    step = "retried"

    def test_the_pending_restart_of_the_proxies_is_done(self):
        self.assertEqual(self.restarted(), (sorted(RESTARTED), [PROXY_RESTART]))
        self.assertFalse(os.path.exists(PENDING))

    def test_the_host_is_untouched(self):
        self.assertRecordsUntouched()
        self.assertEqual(self.changed(self.under((SYSTEMD,))), [])
        self.assertNothingElseChanged()
        self.assertNoVmOperation({"systemd-update-helper", "systemctl"})


class ReinstalledTest(StepTest):
    """`dnf reinstall` of this Release again: an update that brings the same proxy code."""
    step = "reinstalled"

    def test_the_proxies_keep_running(self):
        self.assertEqual(self.restarted(), (sorted(RESTARTED), []))
        self.assertFalse(os.path.exists(PENDING))

    def test_the_host_is_untouched(self):
        self.assertRecordsUntouched()
        self.assertEqual(self.changed(self.under((SYSTEMD,))), [])
        self.assertNothingElseChanged()
        self.assertNoVmOperation({"systemd-update-helper"})


class RemovedTest(StepTest):
    """`dnf remove agentbox`."""
    step = "removed"

    def generated(self):
        """The units agentbox regenerate wrote, as found before the update."""
        return {p for p, e in self.before.items()
                if os.path.dirname(p) == SYSTEMD + "/system" and e[0] == "f"
                and any(fnmatch.fnmatchcase(os.path.basename(p), pattern) for pattern in generated_patterns())}

    def test_the_package_is_gone(self):
        r = subprocess.run(["rpm", "-q", "agentbox"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        # the directories that hold the Host's records stay with them
        left = sorted(p for p in lines("files-current") if os.path.lexists(p) and not p.startswith(RECORDS))
        self.assertEqual(left, [])
        self.assertFalse(os.path.exists("/etc/sudoers.d/agentbox-web"))
        check = subprocess.run(["visudo", "-c"], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_the_records_and_state_of_the_host_stay(self):
        self.assertRecordsUntouched()

    def test_the_units_regenerate_wrote_are_disabled_and_deleted(self):
        """With the links that enabled them, and the links of the Control Room and the expiry
        timer. The rest of systemd's configuration stays: the link of agentbox-vmnet (whose rules
        stay loaded until the Host restarts, and which a new install brings back), the
        journald namespace of Activity."""
        generated = self.generated()
        self.assertIn(SYSTEMD + "/system/agentbox-proxy-work.socket", generated)
        self.assertIn(SYSTEMD + "/system/agentbox-gui-priv@.service", generated)
        names = {os.path.basename(p) for p in generated} | RESTARTED
        links = {p for p, e in self.before.items() if p.startswith(SYSTEMD + "/") and e[0] == "l"
                 and os.path.basename(p) in names}
        self.assertIn(SYSTEMD + "/system/timers.target.wants/agentbox-rebuild.timer", links)
        self.assertEqual(self.changed(self.under((SYSTEMD,))), sorted(generated | links))
        self.assertEqual({p for p in self.after if p.startswith(SYSTEMD + "/")} - self.before.keys(), set())
        self.assertIn(SYSTEMD + "/system/multi-user.target.wants/agentbox-vmnet.service", self.after)
        self.assertNothingElseChanged(beside=generated | links)

    def test_no_vm_is_touched(self):
        """Only units are disabled (stopped too where systemd runs): sockets, services and timers
        of what agentbox regenerate wrote, never a template, never agentbox-vmnet. Then systemd
        forgets the units deleted."""
        self.assertNoVmOperation({"systemd-update-helper", "systemctl"})
        removed = [unit for units in self.helper("remove-system-units") for unit in units]
        expected = {os.path.basename(p) for p in self.generated() if not p.endswith("@.service")} | RESTARTED
        self.assertEqual(sorted(removed), sorted(expected))
        self.assertEqual(self.helper("mark-restart-system-units"), [])
        systemctl = [call for call in self.calls if call[0] == "systemctl"]
        self.assertEqual(systemctl[-1], ["systemctl", "daemon-reload"])
        for call in systemctl[:-1]:
            self.assertEqual(call[1:4], ["--no-reload", "disable", "--no-warn"], call)

    def test_the_service_users_stay(self):
        for user in ("agentbox-web", "agentbox-proxy", "agentbox-feeds", "agentbox-gh"):
            pwd.getpwnam(user)


if __name__ == "__main__":
    unittest.main()
