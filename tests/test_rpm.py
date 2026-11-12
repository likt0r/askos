"""The RPM (rpm/agentbox.spec, ADR 0009) and its installation test.

PackageTest needs only the repository: the spec with the service users, units and sudoers
rule it installs under the prefix /usr, what it requires, and scriptlets that enable, start
and open nothing.

InstalledTest is the installation test. rpm/install-test.sh runs it as root in a fresh
Fedora container right after installing the RPM, with AGENTBOX_RPM_STATE naming the
directory of its snapshots (CI, on every pull request); anywhere else it skips. It checks
the installed paths, owners and modes, the `python3 -I` starters, the reported version, and
that installing wrote no Host setup and enabled, started or opened nothing.

InstalledSetupTest runs after it in the same container (rpm/install-test.sh sets
AGENTBOX_RPM_SETUP as well): the installed `agentbox setup`, twice, as root on the real files,
users and groups, with only systemctl, firewall-cmd and virsh faked (no systemd, firewalld or
libvirt run there). It checks what a container can show of the Host steps: users and groups,
the records and files with their owners and modes, and that the second run changes nothing.

Run from the repo root: python3 -m unittest discover -s tests
"""
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from agentbox import activity, generate, hoststeps, paths  # noqa: E402

RPM = os.path.join(REPO, "rpm")
UNITS = os.path.join(RPM, "units")
STATE = os.environ.get("AGENTBOX_RPM_STATE", "")
# only in a throwaway container, after InstalledTest: it changes /etc of the machine it runs on
SETUP = STATE and os.environ.get("AGENTBOX_RPM_SETUP") and os.geteuid() == 0 and (
    os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv"))
STARTER = "#!/usr/bin/python3 -I"
# the programs in libexec (agentbox-readings is loaded by agentbox-web, not run)
PROGRAMS = {"agentbox-proxy": "proxy", "agentbox-suspicious": "proxy", "agentbox-feeds": "proxy",
            "agentbox-gh-tokend": "github-app", "agentbox-web": "web"}
# what vm/Containerfile copies from outside vm/ (the build context is the repository)
GUEST_TOOLS = ("agentbox-gh-token", "git-credential-agentbox", "gh")
SEEDS = ("allowlist", "suspicious-domains", "legitimate-domains")
# the RPM's own directories in /etc and /var/lib: (owner, group, mode)
DIRECTORIES = {"/etc/agentbox": ("root", "root", 0o755),
               "/etc/agentbox/web": ("root", "agentbox-web", 0o750),
               "/etc/agentbox/github": ("root", "agentbox-gh", 0o750),
               "/var/lib/agentbox": ("root", "root", 0o755),
               "/var/lib/agentbox/images": ("root", "root", 0o711),
               "/var/lib/agentbox/vms": ("root", "root", 0o711)}
SCRIPTLETS = ("pre", "post", "preun", "postun", "pretrans", "posttrans", "preuntrans", "postuntrans")
# the systemctl calls of the scriptlets (tests/test_rpm_upgrade.py): an update that changes the
# proxy code restarts the proxies that run and starts none, and removal has systemd forget the
# units it deleted
SYSTEMCTL_CALLS = ("systemctl try-restart 'agentbox-proxy-*.service'", "systemctl daemon-reload")


def without_systemctl_calls(text):
    for call in SYSTEMCTL_CALLS:
        text = text.replace(call, "")
    return text


def read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def meaningful(text):
    """The lines that do something: no comments, no blank lines."""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def sysusers():
    """(users, groups) the package creates."""
    users, groups = set(), set()
    for line in meaningful(read("rpm", "agentbox.sysusers")):
        kind, name = line.split()[:2]
        (users if kind == "u" else groups).add(name)
    return users, groups


def spec_sections(spec, names):
    """The text of the spec's sections with these names (e.g. post, preun), comments left out."""
    out, current = [], None
    for line in spec.splitlines():
        m = re.match(r"^%(\w+)\b", line)
        if m and m.group(1) in names + ("prep", "build", "install", "check", "files", "changelog", "description",
                                        "package"):
            current = m.group(1)
            continue
        if current in names and not line.lstrip().startswith("#"):
            out.append(line)
    return "\n".join(out)


class PackageTest(unittest.TestCase):
    """The spec and the files it installs, from the repository."""

    @classmethod
    def setUpClass(cls):
        cls.spec = read("rpm", "agentbox.spec")

    def test_the_package_brings_the_units_setup_enables(self):
        """The Control Room, the rules of the VM networks, the expiry timer, the timer of the
        Container image cleanup and the drop-in of the Activity journal's sockets; agentbox setup
        enables the units (libvirt-guests is
        libvirt's own). The Control Room's address and port come from the Host setup instead of
        its unit (agentbox setup), so the package's unit binds nothing on its own."""
        shipped = sorted(os.listdir(UNITS))
        self.assertEqual(shipped, ["agentbox-allowlist-expire.service", "agentbox-allowlist-expire.timer",
                                   "agentbox-container-image-cleanup.service", "agentbox-container-image-cleanup.timer",
                                   "agentbox-journal.conf", "agentbox-vmnet.service", "agentbox-web.service"])
        self.assertLessEqual({hoststeps.EXPIRY_TIMER, hoststeps.CONTAINER_IMAGE_CLEANUP_TIMER, generate.VMNET,
                              "agentbox-web.service"}, set(shipped))
        self.assertNotIn("AGENTBOX_WEB_BIND", read("rpm", "units", "agentbox-web.service"))
        # all of them installed, the journald drop-in where systemd looks for it
        for name in shipped:
            self.assertRegex(self.spec, rf"rpm/units/{re.escape(name)}\b")
        self.assertIn("%{_unitdir}/systemd-journald@agentbox.socket.d/", self.spec)
        # the rules agentbox regenerate writes, loaded at boot and taken away when stopped
        vmnet = meaningful(read("rpm", "units", generate.VMNET))
        self.assertIn(f"ExecStart=/usr/sbin/nft -f {generate.VM_RULES}", vmnet)
        self.assertIn("ExecStop=/usr/sbin/nft delete table inet agentbox_vm", vmnet)

    def test_the_units_name_the_files_of_the_package(self):
        for name in ("agentbox-web.service", "agentbox-allowlist-expire.service", "agentbox-container-image-cleanup.service"):
            text = read("rpm", "units", name)
            program = re.search(r"^ExecStart=(\S+)", text, re.MULTILINE).group(1)
            self.assertIn(program, (paths.CLI, paths.LIBEXEC + "/agentbox-web"))
            self.assertNotIn("/usr/local", text)

    def test_the_service_users_are_those_agentbox_names(self):
        """The users of the Control Room, the proxies, the Feeds and the token service, and the
        group that may write into the Activity journal."""
        users, groups = sysusers()
        self.assertEqual(users, {"agentbox-web", "agentbox-proxy", "agentbox-feeds", "agentbox-gh"})
        self.assertLessEqual({activity.CONTROL_ROOM[0], activity.PROXY_USER, activity.TOKEN_USER}, users)
        self.assertEqual(groups, {activity.JOURNAL_GROUP})
        self.assertIn("install -D -m 0644 rpm/agentbox.sysusers %{buildroot}%{_sysusersdir}/agentbox.conf", self.spec)

    def test_the_runtime_directory_of_the_activity_journal_has_journalds_owner_before_it_starts(self):
        """Else journald's first start per boot chowns the directory and the sockets in it to
        root:systemd-journal, and the proxies cannot connect their stdout."""
        self.assertEqual(meaningful(read("rpm", "agentbox.tmpfiles")),
                         ["d /run/systemd/journal.agentbox 0755 root systemd-journal -"])
        self.assertIn("install -D -m 0644 rpm/agentbox.tmpfiles %{buildroot}%{_tmpfilesdir}/agentbox.conf", self.spec)
        self.assertIn("%{_tmpfilesdir}/agentbox.conf", spec_sections(self.spec, ("files",)))
        self.assertIn("%tmpfiles_create agentbox.conf", spec_sections(self.spec, ("post",)))

    def test_the_sudoers_rule_lets_the_control_room_run_only_the_packages_cli(self):
        rule = meaningful(read("rpm", "agentbox-web.sudoers"))
        # agentbox itself limits the commands and refuses --force (test_control_room_sudo)
        self.assertEqual(rule, [f"agentbox-web ALL=(root) NOPASSWD: {paths.CLI}"])
        # checked when built and again when installed
        self.assertIn("visudo -cqf rpm/agentbox-web.sudoers", spec_sections(self.spec, ("check",)))
        self.assertIn("visudo -cqf %{_sysconfdir}/sudoers.d/agentbox-web", spec_sections(self.spec, ("post",)))

    def test_it_requires_what_a_host_needs(self):
        requires = set(re.findall(r"^Requires:\s+(\S+)", self.spec, re.MULTILINE))
        # the dependencies the parent spec (#73) names
        self.assertLessEqual({"libvirt-daemon-kvm", "qemu-img", "virt-install", "podman", "nftables", "firewalld",
                              "socat", "openssl", "python3", "sudo"}, requires)
        # and the programs agentbox setup runs for the Host steps (agentbox/hoststeps.py)
        self.assertLessEqual({"shadow-utils", "e2fsprogs", "systemd", "firewalld"}, requires)

    def test_it_carries_the_license_of_the_repository(self):
        self.assertTrue(read("LICENSE").startswith("MIT License\n\nCopyright (c) 2026 Benjamin Werner\n"))
        self.assertRegex(self.spec, r"(?m)^License:\s+MIT$")
        self.assertRegex(self.spec, r"(?m)^%license LICENSE$")

    def test_the_version_comes_from_the_version_file(self):
        self.assertRegex(self.spec, r"(?m)^Version:\s+@VERSION@$")
        self.assertIn('sed "s/@VERSION@/$version/"', read("rpm", "build.sh"))
        self.assertIn('release=$(tr -d \'[:space:]\' < "$src/VERSION")', read("rpm", "build.sh"))

    def test_the_scriptlets_enable_start_and_open_nothing(self):
        """No preset on install (it could also disable what an existing Host has enabled), no
        firewall, no agentbox call; on update and removal never the rules of the VM networks."""
        scriptlets = without_systemctl_calls(spec_sections(self.spec, SCRIPTLETS))
        for forbidden in ("systemctl", "firewall-cmd", "nft ", "%{_bindir}/agentbox", "/usr/bin/agentbox",
                          "agentbox setup", "vmnet"):
            self.assertNotIn(forbidden, scriptlets)
        self.assertNotRegex(scriptlets, r"%systemd_(user_)?post\b")
        timers = "agentbox-allowlist-expire.timer agentbox-container-image-cleanup.timer"
        self.assertIn(f"%systemd_preun agentbox-web.service {timers}", scriptlets)
        self.assertIn(f"%systemd_postun_with_restart agentbox-web.service {timers}", scriptlets)


class RetiredTimersTest(unittest.TestCase):
    """#49: an update takes the timers of the version check and the weekly rebuild (and the service
    only the rebuild timer started), as %post runs it: the block of the spec with
    /etc/systemd/system under a temporary directory, systemd-update-helper and systemctl stand-ins
    that record their arguments. The old Reset check's timer stays: until regenerate, its
    weekly-check runs the whole Maintenance window (test_maintenance.TransitionTest)."""

    RETIRED = ("agentbox-rebuild.timer", "agentbox-versions.timer")
    LEFT_FOR_REGENERATE = ("agentbox-weekly.timer", "agentbox-weekly.service")
    KEPT = ("agentbox-maintenance.timer", "agentbox-maintenance.service", "agentbox-feeds.timer",
            "agentbox-feeds.service", "agentbox-versions.service", "agentbox-versions-fetch.service",
            "agentbox-build@.service", "agentbox-proxy-work.socket")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.units = os.path.join(self.dir, "etc", "systemd", "system")
        os.makedirs(self.units)
        os.makedirs(os.path.join(self.dir, "bin"))
        for name in ("systemd-update-helper", "systemctl"):
            with open(os.path.join(self.dir, "bin", name), "w") as f:
                # a unit named in the file "fails" cannot be disabled
                f.write(f'#!/bin/sh\necho "{name} $*" >> "{self.dir}/calls"\n'
                        f'for unit in "$@"; do grep -qxF "$unit" "{self.dir}/fails" 2>/dev/null && exit 1; done\n'
                        'exit 0\n')
            os.chmod(os.path.join(self.dir, "bin", name), 0o755)
        post = spec_sections(read("rpm", "agentbox.spec"), ("post",))
        block = re.search(r"(?ms)^if \[ \$1 -gt 1 \]; then\n    found=\n.*?^fi$", post).group(0)
        self.script = (block.replace("%{_sysconfdir}", os.path.join(self.dir, "etc"))
                       .replace("/usr/lib/systemd/systemd-update-helper", os.path.join(self.dir, "bin",
                                                                                       "systemd-update-helper")))

    def unit(self, *names):
        for name in names:
            with open(os.path.join(self.units, name), "w") as f:
                f.write("[Unit]\n")

    def post(self, arg):
        """The block as %post runs it, $1 = arg (1: install, 2: update); (stderr, calls)."""
        r = subprocess.run(["/bin/sh", "-c", self.script, "post", str(arg)], capture_output=True, text=True,
                           env={"PATH": os.path.join(self.dir, "bin") + ":/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        try:
            with open(os.path.join(self.dir, "calls")) as f:
                calls = f.read().splitlines()
        except FileNotFoundError:
            calls = []
        return r.stderr, calls

    def test_an_update_disables_and_deletes_them_and_says_what_runs_until_regenerate(self):
        self.unit(*self.RETIRED, "agentbox-rebuild.service", *self.LEFT_FOR_REGENERATE, *self.KEPT)
        err, calls = self.post(2)
        self.assertEqual(calls, [f"systemd-update-helper remove-system-units {u}" for u in self.RETIRED]
                         + ["systemctl daemon-reload"])
        self.assertEqual(sorted(os.listdir(self.units)), sorted(self.KEPT + self.LEFT_FOR_REGENERATE))
        self.assertIn("Maintenance window", err)
        self.assertIn("sudo agentbox regenerate", err)
        self.assertIn("the Reset check's timer runs the whole window at the old Reset time", err)

    def test_the_old_reset_timer_stays_for_regenerate(self):
        self.unit(*self.LEFT_FOR_REGENERATE, *self.KEPT)
        self.assertEqual(self.post(2), ("", []))
        self.assertEqual(sorted(os.listdir(self.units)), sorted(self.KEPT + self.LEFT_FOR_REGENERATE))

    def test_only_what_was_disabled_is_deleted(self):
        self.unit(*self.RETIRED, "agentbox-rebuild.service", *self.KEPT)
        with open(os.path.join(self.dir, "fails"), "w") as f:
            f.write("agentbox-rebuild.timer\n")
        err, calls = self.post(2)
        self.assertEqual(calls, [f"systemd-update-helper remove-system-units {u}" for u in self.RETIRED]
                         + ["systemctl daemon-reload"])
        # the rebuild timer could not be disabled: it stays, with its service, for regenerate
        self.assertEqual(sorted(os.listdir(self.units)),
                         sorted(self.KEPT + ("agentbox-rebuild.timer", "agentbox-rebuild.service")))
        self.assertIn("agentbox-rebuild.timer could not be disabled; sudo agentbox regenerate takes it away", err)

    def test_only_those_there_and_a_service_left_alone(self):
        self.unit("agentbox-versions.timer", "agentbox-rebuild.service", *self.KEPT)
        _, calls = self.post(2)
        self.assertEqual(calls, ["systemd-update-helper remove-system-units agentbox-versions.timer",
                                 "systemctl daemon-reload"])
        self.assertEqual(sorted(os.listdir(self.units)), sorted(self.KEPT + ("agentbox-rebuild.service",)))

    def test_a_host_without_them_and_an_install_are_left_alone(self):
        self.unit(*self.KEPT)
        self.assertEqual(self.post(2), ("", []))
        self.unit(*self.RETIRED)
        self.assertEqual(self.post(1), ("", []))
        self.assertEqual(sorted(os.listdir(self.units)), sorted(self.KEPT + self.RETIRED))

    def test_regenerate_takes_away_what_the_package_leaves(self):
        self.assertEqual(set(self.RETIRED + self.LEFT_FOR_REGENERATE[:1]), set(generate.RETIRED_TIMERS))
        self.assertEqual({"agentbox-weekly.service", "agentbox-rebuild.service"}, set(generate.RETIRED_SERVICES))
        self.assertIn("for unit in agentbox-rebuild.timer agentbox-versions.timer; do", self.script)
        self.assertNotIn("agentbox-weekly", self.script)


@unittest.skipUnless(STATE, "only in the installation test (rpm/install-test.sh)")
class InstalledTest(unittest.TestCase):
    """The installed RPM, as root in a fresh Fedora container."""

    @classmethod
    def setUpClass(cls):
        # without the documentation, which Fedora's containers do not install (tsflags=nodocs)
        cls.files = {path for path in subprocess.run(["rpm", "-ql", "agentbox"], capture_output=True, text=True,
                                                     check=True).stdout.splitlines()
                     if not path.startswith("/usr/share/doc/")}
        cls.release = read("VERSION").strip()

    def assertOwned(self, path, owner, group, mode):
        st = os.lstat(path)
        self.assertEqual((pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name, oct(stat.S_IMODE(st.st_mode))),
                         (owner, group, oct(mode)), path)

    def run_as(self, user, argv, env=None, **kwargs):
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, user=user, group=user,
                              extra_groups=[], env=env or {"PATH": "/usr/bin", "LANG": "C.UTF-8"}, **kwargs)

    def installed_paths(self):
        """agentbox.paths as the installed package sees itself (no AGENTBOX_ROOT)."""
        code = ("import json, sys; sys.path.insert(0, '/usr/lib/agentbox'); from agentbox import paths; "
                "print(json.dumps({k: getattr(paths, k) for k in ('PREFIX', 'INSTALLED', 'CLI', 'VERSION', "
                "'PROXY', 'TOKEND', 'FEEDS_PROGRAM', 'SUSPICIOUS', 'WEB_BACKEND', 'WEB_STATIC', 'WEB_UI', "
                "'VM_SOURCE', 'SEED_LISTS', 'CONF', 'STATE', 'SYSTEMD')}))")
        r = subprocess.run(["/usr/bin/python3", "-I", "-c", code], capture_output=True, text=True, check=True,
                           env={"PATH": "/usr/bin"})
        return json.loads(r.stdout)

    def test_the_files_lie_where_agentbox_paths_names_them(self):
        p = self.installed_paths()
        self.assertEqual((p["PREFIX"], p["INSTALLED"], p["CLI"], p["CONF"], p["STATE"], p["SYSTEMD"]),
                         ("/usr", True, "/usr/bin/agentbox", "/etc/agentbox", "/var/lib/agentbox",
                          "/etc/systemd/system"))
        named = [p[k] for k in ("CLI", "VERSION", "PROXY", "TOKEND", "FEEDS_PROGRAM", "SUSPICIOUS", "WEB_BACKEND")]
        named += [os.path.join(p["WEB_STATIC"], "index.html"), os.path.join(p["WEB_UI"], "index.html"),
                  os.path.join(p["VM_SOURCE"], "vm", "Containerfile")]
        named += [os.path.join(p["SEED_LISTS"], name) for name in SEEDS]
        for path in named:
            self.assertIn(path, self.files)
            self.assertTrue(os.path.isfile(path), path)

    def test_the_starters_run_python3_isolated(self):
        """ADR 0006: root-owned, not writable by others, and -I keeps PYTHONPATH and the current
        directory out of the import path."""
        for path in ["/usr/bin/agentbox"] + [f"/usr/libexec/{name}" for name in PROGRAMS]:
            self.assertOwned(path, "root", "root", 0o755)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.readline().rstrip("\n"), STARTER, path)
        self.assertOwned("/usr/libexec/agentbox-readings", "root", "root", 0o644)

    def test_it_holds_the_files_of_this_commit(self):
        """Byte for byte, so nothing rewrote a shebang or a guest file on the way."""
        pairs = [(os.path.join(REPO, "bin", "agentbox"), "/usr/bin/agentbox"),
                 (os.path.join(REPO, "VERSION"), "/usr/lib/agentbox/VERSION"),
                 (os.path.join(REPO, "web", "agentbox-readings"), "/usr/libexec/agentbox-readings")]
        pairs += [(os.path.join(REPO, d, name), f"/usr/libexec/{name}") for name, d in PROGRAMS.items()]
        pairs += [(os.path.join(REPO, "agentbox", n), f"/usr/lib/agentbox/agentbox/{n}")
                  for n in os.listdir(os.path.join(REPO, "agentbox")) if n.endswith(".py")]
        pairs += [(os.path.join(REPO, "web", "static", n), f"/usr/share/agentbox/web/{n}")
                  for n in os.listdir(os.path.join(REPO, "web", "static"))]
        pairs += [(os.path.join(REPO, "proxy", n), f"/usr/share/agentbox/lists/{n}") for n in SEEDS]
        pairs += [(os.path.join(REPO, "github-app", n), f"/usr/share/agentbox/vm/github-app/{n}") for n in GUEST_TOOLS]
        for top, _, names in os.walk(os.path.join(REPO, "vm")):
            for n in names:
                rel = os.path.relpath(os.path.join(top, n), REPO)
                pairs.append((os.path.join(REPO, rel), f"/usr/share/agentbox/vm/{rel}"))
        ui = os.path.join(REPO, "web", "ui", "dist")
        for top, _, names in os.walk(ui):
            for n in names:
                rel = os.path.relpath(os.path.join(top, n), ui)
                pairs.append((os.path.join(ui, rel), f"/usr/share/agentbox/web/ui/{rel}"))
        for source, installed in pairs:
            self.assertIn(installed, self.files)
            with open(source, "rb") as a, open(installed, "rb") as b:
                self.assertEqual(a.read(), b.read(), installed)
            # executable exactly where the repository has it so
            self.assertEqual(bool(os.stat(installed).st_mode & 0o111), bool(os.stat(source).st_mode & 0o111),
                             installed)

    def test_owners_and_modes(self):
        """Everything root's and writable only by root; the two directories the services read
        are their group's, and the sudoers rule readable only by root."""
        for path, (owner, group, mode) in DIRECTORIES.items():
            self.assertIn(path, self.files)
            self.assertOwned(path, owner, group, mode)
        self.assertOwned("/etc/sudoers.d/agentbox-web", "root", "root", 0o440)
        for path in self.files:
            st = os.lstat(path)
            if path not in DIRECTORIES:
                self.assertEqual((st.st_uid, st.st_gid), (0, 0), path)
            self.assertEqual(st.st_mode & 0o022, 0, path)
            self.assertFalse(stat.S_ISLNK(st.st_mode), path)

    def test_reports_the_release(self):
        """The installed CLI reports the file VERSION of this commit, which at a Release is its
        tag; the package carries the same version."""
        tag = os.environ.get("AGENTBOX_RPM_TAG", "")
        if tag:
            self.assertEqual(f"v{self.release}", tag)
        r = self.run_as("nobody", ["/usr/bin/agentbox", "version"])
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, f"agentbox {self.release}\n", ""))
        version = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}", "agentbox"], capture_output=True, text=True,
                                 check=True).stdout
        self.assertEqual(version, self.release.replace("-", "~"))

    def test_creates_the_service_users(self):
        users, groups = sysusers()
        for name in users:
            user = pwd.getpwnam(name)
            self.assertLess(user.pw_uid, 1000, name)
            self.assertIn(user.pw_shell, ("/usr/sbin/nologin", "/sbin/nologin"), name)
        for name in groups:
            self.assertLess(grp.getgrnam(name).gr_gid, 1000, name)

    def test_the_sudoers_rule_is_in_place_and_valid(self):
        with open("/etc/sudoers.d/agentbox-web", encoding="utf-8") as f:
            self.assertEqual(f.read(), read("rpm", "agentbox-web.sudoers"))
        r = subprocess.run(["visudo", "-c"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_rule_visudo_rejects_is_set_aside(self):
        """The install scriptlet as rpm ran it, on a Host whose visudo rejects the rule: the rule
        goes to agentbox-web.rejected (sudo skips names with a dot) and sudo's configuration
        stays valid. The rule is put back afterwards."""
        post = subprocess.run(["rpm", "-q", "--qf", "%{POSTIN}", "agentbox"], capture_output=True, text=True,
                              check=True).stdout
        rule = "/etc/sudoers.d/agentbox-web"
        with tempfile.TemporaryDirectory() as fake:
            with open(os.path.join(fake, "visudo"), "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(os.path.join(fake, "visudo"), 0o755)
            try:
                # $1 = 2: as on an update, so the scriptlet leaves /var/lib/agentbox alone
                r = subprocess.run(["/bin/sh", "-c", post, "post", "2"], capture_output=True, text=True,
                                   env={"PATH": fake + ":/usr/bin:/usr/sbin"})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("set aside as agentbox-web.rejected", r.stderr)
                self.assertFalse(os.path.exists(rule))
                with open(rule + ".rejected", encoding="utf-8") as f:
                    self.assertEqual(f.read(), read("rpm", "agentbox-web.sudoers"))
                check = subprocess.run(["visudo", "-c"], capture_output=True, text=True)
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            finally:
                if os.path.exists(rule + ".rejected"):
                    os.replace(rule + ".rejected", rule)

    def test_the_license_is_installed(self):
        """Also where documentation is left out (tsflags=nodocs, as in this container)."""
        path = "/usr/share/licenses/agentbox/LICENSE"
        self.assertIn(path, self.files)
        self.assertOwned(path, "root", "root", 0o644)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), read("LICENSE"))
        r = subprocess.run(["rpm", "-q", "--qf", "%{LICENSE}", "agentbox"], capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout, "MIT")

    def test_the_activity_journal_admits_only_root_and_its_group(self):
        """The drop-in for the Activity namespace's sockets (ADR 0007), where systemd reads it for
        the instance agentbox of the journald socket its own package ships."""
        dropin = "/usr/lib/systemd/system/systemd-journald@agentbox.socket.d/agentbox-journal.conf"
        self.assertIn(dropin, self.files)
        self.assertOwned(dropin, "root", "root", 0o644)
        self.assertTrue(os.path.isfile("/usr/lib/systemd/system/systemd-journald@.socket"))
        with open(dropin, encoding="utf-8") as f:
            self.assertEqual(meaningful(f.read()), ["[Socket]", "SocketMode=0660", "SocketGroup=agentbox-journal"])
        grp.getgrnam("agentbox-journal")

    def test_installing_wrote_no_host_setup_and_enabled_nothing(self):
        """Between the snapshots before and after `dnf install`: new or changed paths are the
        package's own files, the user and group databases and the library cache (glibc's
        trigger for anything installed in /usr/lib). Nothing else in /etc (no Host setup,
        password, certificate, Settings or lists, no unit enabled, no firewall rule), nothing
        in /var/lib/agentbox."""
        before, after = self.snapshot("before"), self.snapshot("after")
        system = {f"/etc/{name}{suffix}" for name in ("passwd", "group", "shadow", "gshadow")
                  for suffix in ("", "-")} | {"/etc/.pwd.lock", "/etc/ld.so.cache"}
        changed = sorted(path for path, entry in after.items()
                         if before.get(path) != entry and path not in self.files and path not in system)
        self.assertEqual(changed, [])
        self.assertEqual([path for path in before if path not in after], [])
        self.assertEqual(sorted(p for p in after if p.startswith("/etc/agentbox/")),
                         ["/etc/agentbox/github", "/etc/agentbox/web"])
        self.assertEqual(sorted(p for p in after if p.startswith("/var/lib/agentbox/")),
                         ["/var/lib/agentbox/images", "/var/lib/agentbox/vms"])
        self.assertFalse(any(p.startswith(("/etc/systemd/system/", "/etc/firewalld/")) and before.get(p) != after[p]
                             for p in after))

    def snapshot(self, name):
        """path -> (type, mode, owner, group, size, time, link target); for directories only
        type, mode and owners, since adding an entry changes their size and time."""
        out = {}
        with open(os.path.join(STATE, name), encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                path, kind, mode, owner, group, size, time, target = line.rstrip("\n").split("\t")
                out[path] = (kind, mode, owner, group) if kind == "d" else (kind, mode, owner, group, size, time,
                                                                          target)
        return out

    def test_the_scriptlets_start_and_open_nothing(self):
        """As rpm runs them (macros expanded): no preset on install, units stopped and disabled
        only on removal and restarted only on update, never the rules of the VM networks."""
        with open(os.path.join(STATE, "scripts"), encoding="utf-8") as f:
            scripts = without_systemctl_calls("\n".join(meaningful(f.read())))
        for forbidden in ("systemctl", "firewall-cmd", "install-system-units", "agentbox-vmnet", "/usr/bin/agentbox"):
            self.assertNotIn(forbidden, scripts)
        verbs = set(re.findall(r"systemd-update-helper (\S+)", scripts))
        self.assertLessEqual(verbs, {"remove-system-units", "mark-restart-system-units"})

    def test_root_running_it_leaves_the_package_as_installed(self):
        """Compiled when built: root running the CLI writes no bytecode next to it."""
        for argv in (["/usr/bin/agentbox", "version"], ["/usr/bin/agentbox", "version", "--json"]):
            subprocess.run(argv, capture_output=True, timeout=60, env={"PATH": "/usr/bin:/usr/sbin"})
        r = subprocess.run(["rpm", "-V", "agentbox"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        for top, dirs, names in os.walk("/usr/lib/agentbox"):
            for n in dirs + names:
                self.assertIn(os.path.join(top, n), self.files)

    def test_the_control_room_backend_runs_as_its_user(self):
        r = self.run_as("agentbox-web", ["/usr/libexec/agentbox-web", "--hash-password"],
                        input="a password of twenty characters\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip())

    def test_the_detection_self_test_passes_from_the_installed_files(self):
        """The check agentbox setup runs before it changes anything: the installed proxy and
        classifier with the lists a new Host starts with."""
        with tempfile.TemporaryDirectory() as lists:
            for name in ("suspicious-domains", "legitimate-domains"):
                shutil.copy(f"/usr/share/agentbox/lists/{name}", lists)
            os.chmod(lists, 0o755)
            r = self.run_as("agentbox-proxy", ["/usr/libexec/agentbox-suspicious", "--self-test"],
                            env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "AGENTBOX_CONF_DIR": lists})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


def tree(*tops):
    """path -> (type, mode, owner, group, sha256 or link target, time) of everything under tops."""
    out = {}
    for top in tops:
        for folder, dirs, names in os.walk(top):
            for name in [""] + dirs + names:
                path = os.path.join(folder, name) if name else folder
                st = os.lstat(path)
                if stat.S_ISREG(st.st_mode):
                    with open(path, "rb") as f:
                        what = hashlib.sha256(f.read()).hexdigest()
                elif stat.S_ISLNK(st.st_mode):
                    what = os.readlink(path)
                else:
                    what = None
                out[path] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, what,
                             None if stat.S_ISDIR(st.st_mode) else st.st_mtime_ns)
    return out


@unittest.skipUnless(SETUP, "only in the installation test's container, after InstalledTest (rpm/install-test.sh)")
class InstalledSetupTest(unittest.TestCase):
    """`agentbox setup` of the installed RPM, twice, as root on the container's files, users and
    groups. systemd, firewalld and libvirt do not run in a container: systemctl, firewall-cmd and
    virsh are the fakes of the CLI tests (tests/test_setup.py checks what goes through them)."""

    OWNER = "owner"
    # the units setup enables itself (agentbox/hoststeps.py, agentbox/setup.py)
    ENABLED = ["agentbox-allowlist-expire.timer", "agentbox-container-image-cleanup.timer", "agentbox-vmnet.service",
               "agentbox-web.service",
               "libvirt-guests.service"]
    WATCHED = ("/etc", "/var/lib/agentbox", "/var/lib/systemd")
    JOURNAL = "/run/systemd/journal.agentbox"

    @classmethod
    def setUpClass(cls):
        subprocess.run(["useradd", "--create-home", cls.OWNER], check=True)
        cls.fakes = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.fakes, "bin"))
        for name, program in (("systemctl", "fake_systemctl.py"), ("firewall-cmd", "fake_firewall_cmd.py"),
                              ("virsh", "fake_virsh.py")):
            path = os.path.join(cls.fakes, "bin", name)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\nexec python3 -B "{os.path.join(REPO, "tests", program)}" "$@"\n')
            os.chmod(path, 0o755)
        # the state journald's first start leaves when the directory was root:root: all of it
        # root:systemd-journal (not socket: agentbox would write there, and nothing reads it here).
        # %post made the directory already, so it is set back explicitly.
        os.makedirs(cls.JOURNAL, exist_ok=True)
        os.chown(cls.JOURNAL, 0, 0)
        for name in ("stdout", "dev-log"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.bind(os.path.join(cls.JOURNAL, name))
            os.chmod(os.path.join(cls.JOURNAL, name), 0o666)
            os.chown(os.path.join(cls.JOURNAL, name), 0, grp.getgrnam("systemd-journal").gr_gid)
        # a new Host: no VMs from before the registry
        cls.scenario(vms={"work": {"defined": False}, "priv": {"defined": False}})
        cls.first = cls.setup("--password-stdin", "--cert-name", "rpm-test", stdin="a password of twenty characters\n")
        cls.first_calls = cls.calls()
        # what the first run started runs, and the first Feeds are there
        cls.scenario(vms={"work": {"defined": False}, "priv": {"defined": False}}, systemctl_active=cls.ENABLED)
        os.makedirs("/var/lib/agentbox/feeds", exist_ok=True)
        with open("/var/lib/agentbox/feeds/status.json", "w") as f:
            f.write("{}\n")
        cls.after_first = tree(*cls.WATCHED)
        os.remove(os.path.join(cls.fakes, "calls.jsonl"))
        cls.second = cls.setup()
        cls.after_second = tree(*cls.WATCHED)

    @classmethod
    def scenario(cls, **s):
        with open(os.path.join(cls.fakes, "scenario.json"), "w") as f:
            json.dump(s, f)

    @classmethod
    def setup(cls, *args, stdin=""):
        env = {"PATH": os.path.join(cls.fakes, "bin") + ":/usr/bin:/usr/sbin", "LANG": "C.UTF-8",
               "SUDO_USER": cls.OWNER, "FAKE_DIR": cls.fakes, "FAKE_VIRSH_DIR": cls.fakes}
        r = subprocess.run(["/usr/bin/agentbox", "setup", *args], capture_output=True, text=True, env=env,
                           input=stdin, timeout=600)
        return r.returncode, r.stdout, r.stderr

    @classmethod
    def calls(cls):
        try:
            with open(os.path.join(cls.fakes, "calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def fake_state(self, name):
        with open(os.path.join(self.fakes, name)) as f:
            return json.load(f)

    def assertOwned(self, path, owner, group, mode):
        st = os.lstat(path)
        self.assertEqual((pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name,
                          oct(stat.S_IMODE(st.st_mode))), (owner, group, oct(mode)), path)

    def test_both_runs_succeed(self):
        self.assertEqual(self.first[0], 0, self.first[1] + self.first[2])
        self.assertIn("Host set up: Local-only", self.first[1])
        self.assertEqual(self.second[0], 0, self.second[1] + self.second[2])
        self.assertIn("Host setup unchanged: Local-only", self.second[1])
        self.assertIn("\nActivity journal: up to date\n", self.second[1])

    def test_the_running_activity_journal_admits_only_root_and_its_group(self):
        for name in ("stdout", "dev-log"):
            self.assertOwned(os.path.join(self.JOURNAL, name), "root", "agentbox-journal", 0o660)
        self.assertOwned(self.JOURNAL, "root", "systemd-journal", 0o755)

    def test_proxies_that_lost_their_log_stream_restart(self):
        self.assertIn(["try-restart", "agentbox-proxy-*.service", "agentbox-web.service"],
                      [c["argv"] for c in self.first_calls if c["cmd"] == "systemctl"])

    def test_the_owner_controls_the_vms_and_may_write_activity(self):
        libvirt = grp.getgrnam("libvirt")
        self.assertIn(self.OWNER, libvirt.gr_mem)
        primary = {u.pw_name for u in pwd.getpwall() if u.pw_gid == libvirt.gr_gid}
        self.assertEqual(sorted(grp.getgrnam("agentbox-journal").gr_mem), sorted(set(libvirt.gr_mem) | primary))

    def test_the_host_setup_and_the_control_rooms_files(self):
        with open("/etc/agentbox/host.json") as f:
            self.assertEqual(json.load(f), {"mode": "local", "bind": "127.0.0.1", "web_port": 8800,
                                            "cert_name": "rpm-test", "owner_user": self.OWNER})
        self.assertOwned("/etc/agentbox/host.json", "root", "root", 0o644)
        self.assertOwned("/etc/agentbox/web/password", "root", "agentbox-web", 0o640)
        self.assertOwned("/etc/agentbox/web/key.pem", "root", "agentbox-web", 0o640)
        self.assertOwned("/etc/agentbox/web/cert.pem", "root", "agentbox-web", 0o644)

    def test_the_lists_of_a_new_host_become_its_records(self):
        for record, seed in (("allowlist.json", "allowlist"), ("watchlist.json", "suspicious-domains"),
                             ("legitimate.json", "legitimate-domains")):
            self.assertOwned(f"/etc/agentbox/{record}", "root", "root", 0o644)
            with open(f"/etc/agentbox/{record}") as f:
                self.assertTrue(json.load(f)["entries"], record)
            # the text file they were taken over from, moved aside
            with open(f"/etc/agentbox/{seed}.migrated", "rb") as a, \
                    open(f"/usr/share/agentbox/lists/{seed}", "rb") as b:
                self.assertEqual(a.read(), b.read())
        self.assertOwned("/etc/agentbox/harmless", "root", "root", 0o644)

    def test_the_files_of_the_host_steps(self):
        # Builds need nothing of the owner's (#113): no build.conf
        self.assertFalse(os.path.lexists("/etc/agentbox/build.conf"))
        with open("/etc/sysconfig/libvirt-guests") as f:
            self.assertIn("ON_BOOT=ignore\n", f.read())
        for path in ("/etc/sysconfig/libvirt-guests", "/etc/agentbox/vm-input.nft"):
            self.assertOwned(path, "root", "root", 0o644)
        # the units regenerate wrote name the package's programs
        for name in os.listdir("/etc/systemd/system"):
            path = os.path.join("/etc/systemd/system", name)
            if name.startswith("agentbox-") and os.path.isfile(path):
                with open(path) as f:
                    for program in re.findall(r"^ExecStart=(/\S+)", f.read(), re.MULTILINE):
                        self.assertTrue(os.path.isfile(program), f"{name}: {program}")

    def test_the_units_and_the_zone_of_the_vm_networks(self):
        self.assertLessEqual(set(self.ENABLED), set(self.fake_state("systemctl.json")["enabled"]))
        libvirt = self.fake_state("firewall.json")["permanent"]["libvirt"]
        self.assertEqual(sorted(libvirt["ports"]), ["443/tcp", "53/tcp", "53/udp", "9100/tcp"])
        self.assertNotIn("ssh", libvirt["services"])
        self.assertFalse(any(c["cmd"] == "firewall-cmd" and any("rich-rule" in a for a in c["argv"])
                             for c in self.first_calls))

    def test_a_second_run_changes_nothing(self):
        changed = sorted(p for p in set(self.after_first) | set(self.after_second)
                         if self.after_first.get(p) != self.after_second.get(p))
        self.assertEqual(changed, [])
        changing = [c for c in self.calls()
                    if (c["cmd"] == "firewall-cmd" and any(a.startswith(("--add-", "--remove-")) or a == "--reload"
                                                           for a in c["argv"]))
                    or (c["cmd"] == "systemctl" and c["argv"][0] not in ("is-enabled", "is-active", "enable"))
                    or (c["cmd"] == "systemctl" and c["argv"][0] == "enable" and set(c["argv"]) & set(self.ENABLED))]
        self.assertEqual(changing, [])

    def test_the_package_stays_as_installed(self):
        r = subprocess.run(["rpm", "-V", "agentbox"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
