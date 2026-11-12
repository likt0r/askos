"""`agentbox setup` (ADR 0009): Local-only, the default mode, Network mode, and its Host steps
(#102).

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root, the Release's programs and
lists installed under it as the RPM lays them out, and fake virsh, qemu-img, systemctl,
virt-install, firewall-cmd, getent, usermod, gpasswd and chattr on PATH (see test_registry.py).
The password hash is made by the real Control Room backend, the certificate by the real
openssl, and the detection self-test by the real proxy and classifier.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from test_registry import AGENTBOX, PRIV_UUID, REPO, RegistryTest, fake  # noqa: E402

PASSWORD = "correct horse battery staple"
# alice runs `sudo agentbox setup` (SUDO_USER) and becomes the owner user
LOCAL_RECORD = {"mode": "local", "bind": "127.0.0.1", "web_port": 8800, "owner_user": "alice"}
NETWORK_ARGS = ["--mode", "network", "--bind", "192.0.2.8", "--home-network", "192.0.2.0/24"]
LIBVIRT_GID = 975
# the units setup enables itself; all of them run once it is done
ENABLED = ["agentbox-allowlist-expire.timer", "agentbox-vmnet.service", "agentbox-web.service",
           "libvirt-guests.service"]
SEEDS = ("allowlist", "suspicious-domains", "legitimate-domains")


def accounts(home):
    """A Fedora Host after `dnf install agentbox`: alice and bob, the Control Room's service user,
    libvirt and agentbox-journal without members."""
    return {"users": {"root": {"uid": 0, "gid": 0, "home": "/root"},
                      "alice": {"uid": 1000, "gid": 1000, "home": os.path.join(home, "alice")},
                      "bob": {"uid": 1001, "gid": 1001, "home": os.path.join(home, "bob")},
                      "agentbox-web": {"uid": 990, "gid": 990, "home": "/", "shell": "/usr/sbin/nologin"}},
            "groups": {"alice": {"gid": 1000, "members": []}, "bob": {"gid": 1001, "members": []},
                       "libvirt": {"gid": LIBVIRT_GID, "members": []},
                       "agentbox-journal": {"gid": 974, "members": []}}}


def meaningful(text):
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def rich_rules(net="192.0.2.0/24", web=8800):
    """(accept, reject) rules of Network mode, as setup sets them."""
    accept = [f'rule priority="-110" family="ipv4" source address="{net}" port port="{p}" protocol="tcp" accept'
              for p in (str(web), "6801-7054")]
    reject = [f'rule priority="-100" family="{fam}" port port="{p}" protocol="tcp" reject'
              for p in (str(web), "6768-7054", "16768") for fam in ("ipv4", "ipv6")]
    return accept, reject


class SetupTest(RegistryTest):
    def setUp(self):
        super().setUp()
        bin_dir = os.path.join(self.dir, "bin")
        fake(os.path.join(bin_dir, "firewall-cmd"), "fake_firewall_cmd.py")
        fake(os.path.join(bin_dir, "chattr"), "fake_chattr.py")
        for program in ("getent", "usermod", "gpasswd"):
            with open(os.path.join(bin_dir, program), "w") as f:
                f.write(f'#!/bin/sh\nexec python3 "{os.path.join(REPO, "tests", "fake_accounts.py")}" {program} "$@"\n')
            os.chmod(os.path.join(bin_dir, program), 0o755)
        # what the RPM installs: the Control Room backend, the proxy and classifier of the
        # self-test, the lists a new Host starts with
        libexec = self.path("usr/libexec")
        os.makedirs(libexec)
        for name, folder in (("agentbox-web", "web"), ("agentbox-proxy", "proxy"), ("agentbox-suspicious", "proxy")):
            os.symlink(os.path.join(REPO, folder, name), os.path.join(libexec, name))
        os.makedirs(self.path("usr/share/agentbox/lists"))
        for name in SEEDS:
            shutil.copy(os.path.join(REPO, "proxy", name), self.path("usr/share/agentbox/lists"))
        os.makedirs(self.path("etc/sysconfig"))
        self.password_file = os.path.join(self.dir, "password")
        with open(self.password_file, "w") as f:
            f.write(PASSWORD + "\n")

    def scenario(self, **s):
        """A Host fresh from the RPM unless said otherwise: its users and groups, and no VMs
        from before the registry (work and priv not defined in libvirt)."""
        vms = {"work": {"defined": False}, "priv": {"defined": False}, **s.pop("vms", {})}
        super().scenario(**{"accounts": accounts(os.path.join(self.dir, "home")), "vms": vms, **s})

    def setup_run(self, *args, stdin=None, sudo_user="alice", sudo_uid=None):
        """(exit code, stdout, stderr) of `agentbox setup` as run with sudo by sudo_user (None:
        by root itself), with SUDO_UID when given; stdin is never a terminal."""
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        if sudo_user:
            env["SUDO_USER"] = sudo_user
        if sudo_uid is not None:
            env["SUDO_UID"] = str(sudo_uid)
        r = subprocess.run([AGENTBOX, "setup", *args], capture_output=True, text=True, env=env, timeout=120,
                           input=stdin if stdin is not None else "")
        return r.returncode, r.stdout, r.stderr

    def ok(self, *args, **kw):
        code, out, err = self.setup_run(*args, **kw)
        self.assertEqual(code, 0, err)
        return out

    def host_setup(self):
        return json.loads(self.read("etc/agentbox/host.json"))

    def san(self):
        out = subprocess.run(["openssl", "x509", "-in", self.path("etc/agentbox/web/cert.pem"), "-noout", "-ext",
                              "subjectAltName"], capture_output=True, text=True).stdout
        return out

    def firewall_calls(self):
        return self.calls("firewall-cmd")

    def rich(self, verb):
        """The rich rules passed to --add-rich-rule / --remove-rich-rule, in order."""
        prefix = f"--{verb}-rich-rule="
        return [a[len(prefix):] for c in self.firewall_calls() for a in c["argv"] if a.startswith(prefix)]

    def fake_state(self, name, default=None):
        """What a fake keeps as changed so far (firewall.json, systemctl.json, accounts.json)."""
        try:
            with open(os.path.join(self.dir, name)) as f:
                return json.load(f)
        except FileNotFoundError:
            return default

    def members(self, group):
        state = self.fake_state("accounts.json") or accounts(os.path.join(self.dir, "home"))
        return sorted(state["groups"][group]["members"])

    def enabled(self):
        return self.fake_state("systemctl.json", {"enabled": []})["enabled"]

    def zone(self, name, side="permanent"):
        return self.fake_state("firewall.json")[side][name]

    def changing_calls(self):
        """The calls that change something: firewall-cmd adding, removing or reloading, usermod,
        gpasswd and chattr, and systemctl other than asking and other than regenerate's `enable --now`
        of units setup does not enable itself (they are enabled already from then on)."""
        out = []
        for c in self.calls():
            argv = c["argv"]
            if c["cmd"] in ("usermod", "gpasswd", "chattr"):
                out.append(c)
            elif c["cmd"] == "firewall-cmd" and any(a.startswith(("--add-", "--remove-")) or a == "--reload"
                                                    for a in argv):
                out.append(c)
            elif c["cmd"] == "systemctl" and argv[0] not in ("is-enabled", "is-active") \
                    and not (argv[:2] == ["enable", "--now"] and not set(argv[2:]) & set(ENABLED)):
                out.append(c)
        return out

class LocalOnlyTest(SetupTest):
    def test_sets_the_host_up_for_this_machine_only(self):
        out = self.ok("--password-file", self.password_file)
        self.assertEqual(self.host_setup(), LOCAL_RECORD)
        self.assertEqual(self.mode("etc/agentbox/host.json"), 0o644)
        self.assertIn("https://127.0.0.1:8800", out)
        self.assertRegex(out, r"fingerprint[^:]*: ([0-9A-F]{2}:){31}[0-9A-F]{2}")
        # the Control Room is started with the new mode
        systemctl = [c["argv"] for c in self.calls("systemctl")]
        self.assertEqual(systemctl[-2:], [["enable", "agentbox-web.service"], ["restart", "agentbox-web.service"]])
        # nothing is opened towards a network: no rule, only the zone of the VM networks changes
        self.assertEqual(self.rich("add"), [])
        changes = [c["argv"] for c in self.changing_calls() if c["cmd"] == "firewall-cmd"]
        self.assertTrue(changes)
        self.assertTrue(all("--zone=libvirt" in a or a == ["--reload"] for a in changes), changes)

    def test_stores_a_hash_of_the_password_readable_by_the_control_room_group_only(self):
        self.ok("--password-file", self.password_file)
        stored = self.read("etc/agentbox/web/password")
        self.assertRegex(stored, r"^scrypt\$32768\$8\$1\$[0-9a-f]+\$[0-9a-f]+\n$")
        self.assertNotIn(PASSWORD, stored)
        self.assertEqual(self.mode("etc/agentbox/web/password"), 0o640)
        self.assertEqual(self.mode("etc/agentbox/web"), 0o750)

    def test_creates_a_certificate_for_loopback_and_the_given_name(self):
        out = self.ok("--password-stdin", "--cert-name", "laptop", "--json", stdin=PASSWORD + "\n")
        self.assertTrue(json.loads(out)["certificate_created"])
        self.assertEqual(self.mode("etc/agentbox/web/key.pem"), 0o640)
        self.assertEqual(self.mode("etc/agentbox/web/cert.pem"), 0o644)
        san = self.san()
        for name in ("DNS:localhost", "IP Address:127.0.0.1", "DNS:laptop"):
            self.assertIn(name, san)
        self.assertEqual(self.host_setup(), {**LOCAL_RECORD, "cert_name": "laptop"})

    def test_remembers_the_owner_user(self):
        self.ok("--password-file", self.password_file, "--owner-user", "bob")
        self.assertEqual(self.host_setup(), {**LOCAL_RECORD, "owner_user": "bob"})

    def test_refuses_a_password_that_is_too_short_and_writes_nothing(self):
        for pw in ("", "fifteen chars!!"):
            with self.subTest(pw):
                code, out, err = self.setup_run("--password-stdin", stdin=pw + "\n")
                self.assertEqual(code, 1)
                self.assertIn("at least 16 characters", err)
                self.assertFalse(os.path.exists(self.path("etc/agentbox/host.json")))
                self.assertFalse(os.path.exists(self.path("etc/agentbox/web/password")))
                self.assertEqual(self.calls("systemctl"), [])

    def test_without_a_password_source_and_without_a_terminal_it_asks_for_one_option(self):
        code, _, err = self.setup_run()
        self.assertEqual(code, 1)
        self.assertIn("--password-file", err)
        self.assertFalse(os.path.exists(self.path("etc/agentbox/host.json")))

    def test_refuses_options_and_modes_it_does_not_know(self):
        for args in (["--bogus"], ["--mode", "both"], ["--mode"], ["--cert-name", "a b"], ["--owner-user", "x/y"]):
            with self.subTest(args):
                code, _, err = self.setup_run(*args, "--password-file", self.password_file)
                self.assertEqual(code, 2, err)
                self.assertFalse(os.path.exists(self.path("etc/agentbox/host.json")))


class NetworkModeTest(SetupTest):
    def test_sets_the_host_up_for_the_named_home_network(self):
        out = self.ok(*NETWORK_ARGS, "--cert-name", "titan", "--password-file", self.password_file)
        self.assertEqual(self.host_setup(), {"mode": "network", "bind": "192.0.2.8", "web_port": 8800,
                                             "home_network": "192.0.2.0/24",
                                             "cert_name": "titan", "owner_user": "alice"})
        self.assertIn("https://192.0.2.8:8800", out)
        self.assertIn("192.0.2.0/24", out)
        systemctl = [c["argv"] for c in self.calls("systemctl")]
        self.assertEqual(systemctl[-2:], [["enable", "agentbox-web.service"], ["restart", "agentbox-web.service"]])

    def test_the_firewall_accepts_only_that_network_and_rejects_the_rest(self):
        self.ok(*NETWORK_ARGS, "--password-file", self.password_file)
        accept, reject = rich_rules()
        self.assertEqual(sorted(self.rich("add")), sorted(accept + reject))
        self.assertEqual(self.rich("remove"), [])
        # the accept rules are the ones titan has today, the reject rules cover both address families
        self.assertIn('rule priority="-110" family="ipv4" source address="192.0.2.0/24" port port="8800" '
                      'protocol="tcp" accept', accept)
        self.assertIn('rule priority="-100" family="ipv6" port port="6768-7054" protocol="tcp" reject', reject)
        calls = [c["argv"] for c in self.firewall_calls()]
        self.assertEqual(calls[-1], ["--reload"])
        self.assertTrue(all("--permanent" in a for a in calls if any("rich-rule" in x for x in a)))

    def test_rules_that_are_already_there_are_not_added_again(self):
        accept, reject = rich_rules()
        self.scenario(firewall_rules=accept + reject)
        self.ok(*NETWORK_ARGS, "--password-file", self.password_file)
        self.assertEqual(self.rich("add"), [])

    def test_the_certificate_names_the_address_and_the_given_name(self):
        self.ok(*NETWORK_ARGS, "--cert-name", "titan.example", "--password-file", self.password_file)
        san = self.san()
        for name in ("IP Address:192.0.2.8", "IP Address:127.0.0.1", "DNS:titan.example", "DNS:localhost"):
            self.assertIn(name, san)

    def test_another_port_is_used_everywhere(self):
        self.ok(*NETWORK_ARGS, "--web-port", "9443", "--password-file", self.password_file)
        self.assertEqual(self.host_setup()["web_port"], 9443)
        self.assertTrue(any('port port="9443"' in r for r in self.rich("add")))
        self.assertFalse(any('port port="8800"' in r for r in self.rich("add")))

    def test_switching_from_local_only_rebinds_the_orca_ports_of_existing_vms(self):
        vm_records.write(self.root)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})
        self.ok("--password-file", self.password_file)
        self.assertIn("ListenStream=127.0.0.1:6802\n", self.read("etc/systemd/system/agentbox-gui-priv.socket"))
        self.ok(*NETWORK_ARGS, "--password-file", self.password_file, "--force")
        self.assertIn("ListenStream=6802\n", self.read("etc/systemd/system/agentbox-gui-priv.socket"))

    def test_another_home_network_takes_back_only_what_no_longer_applies(self):
        self.ok(*NETWORK_ARGS, "--password-file", self.password_file)
        old_accept, old_reject = rich_rules()
        new_accept, _ = rich_rules(net="10.0.0.0/24")
        self.scenario(firewall_rules=old_accept + old_reject)
        self.forget_calls()
        self.ok(*NETWORK_ARGS[:5], "10.0.0.0/24", "--password-file", self.password_file, "--force")
        self.assertEqual(sorted(self.rich("remove")), sorted(old_accept))
        self.assertEqual(sorted(self.rich("add")), sorted(new_accept))

    def test_refuses_what_makes_no_sense_and_changes_nothing(self):
        base = ["--password-file", self.password_file]
        cases = {
            "no address": (2, NETWORK_ARGS[:2] + NETWORK_ARGS[4:]),
            "no home network": (2, NETWORK_ARGS[:4] + NETWORK_ARGS[6:]),
            "address is a name": (1, ["--mode", "network", "--bind", "titan", *NETWORK_ARGS[4:]]),
            "loopback address": (1, ["--mode", "network", "--bind", "127.0.0.1", *NETWORK_ARGS[4:]]),
            "every address": (1, ["--mode", "network", "--bind", "0.0.0.0", *NETWORK_ARGS[4:]]),
            "network with host bits": (1, [*NETWORK_ARGS[:4], "--home-network", "192.0.2.8/24"]),
            "not a network": (1, [*NETWORK_ARGS[:4], "--home-network", "home"]),
            "network option in Local-only": (2, ["--bind", "192.0.2.8"]),
            "port as text": (2, [*NETWORK_ARGS, "--web-port", "https"]),
            "port out of range": (1, [*NETWORK_ARGS, "--web-port", "70000"]),
        }
        for name, (exit_code, args) in cases.items():
            with self.subTest(name):
                code, _, err = self.setup_run(*args, *base)
                self.assertEqual(code, exit_code, err)
                self.assertFalse(os.path.exists(self.path("etc/agentbox/host.json")))
                self.assertFalse(os.path.exists(self.path("etc/agentbox/web/password")))
                self.assertEqual(self.firewall_calls(), [])
                self.assertEqual(self.calls("systemctl"), [])


class ExistingHostTest(SetupTest):
    def test_does_not_overwrite_an_existing_host_setup(self):
        vm_records.write_host_setup(self.root)
        before = self.snapshot()
        code, _, err = self.setup_run("--password-file", self.password_file)
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls("systemctl"), [])

    def test_force_replaces_it_and_rebinds_the_orca_ports_of_existing_vms(self):
        vm_records.write(self.root)
        accept, reject = rich_rules()
        # the firewall as Network mode left it (regenerate changes only the zone of the VM networks)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}}, firewall_rules=accept + reject)
        vm_records.write_host_setup(self.root)
        self.assertEqual(self.run_agentbox("regenerate")[0], 0)
        self.assertIn("ListenStream=6802\n", self.read("etc/systemd/system/agentbox-gui-priv.socket"))
        self.ok("--password-file", self.password_file, "--force")
        self.assertEqual(self.host_setup(), LOCAL_RECORD)
        self.assertIn("ListenStream=127.0.0.1:6802\n", self.read("etc/systemd/system/agentbox-gui-priv.socket"))
        # Local-only takes back what Network mode had opened, and adds nothing
        self.assertEqual(sorted(self.rich("remove")), sorted(accept + reject))
        self.assertEqual(self.rich("add"), [])

    def test_never_touches_settings_the_registry_the_certificate_or_the_github_app(self):
        vm_records.write(self.root)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})
        self.write("etc/agentbox/settings.json", '{"builds_kept": 5}\n')
        self.write("etc/agentbox/github/app-id", "12345\n")
        os.makedirs(self.path("etc/agentbox/web"), exist_ok=True)
        self.write("etc/agentbox/web/cert.pem", "an existing certificate\n")
        self.write("etc/agentbox/web/key.pem", "an existing key\n", 0o640)
        keep = ["etc/agentbox/settings.json", "etc/agentbox/github/app-id", "etc/agentbox/web/cert.pem",
                "etc/agentbox/web/key.pem"] + [f"etc/agentbox/vms/{n}" for n in self.registry()]
        before = {k: v for k, v in self.snapshot().items() if k in keep}
        result = json.loads(self.ok("--password-file", self.password_file, "--json"))
        self.assertFalse(result["certificate_created"])
        self.assertEqual({k: v for k, v in self.snapshot().items() if k in keep}, before)


def watch(domain):
    return {"domain": domain, "category": "exfil", "source": "Host", "note": "", "added": 1793700000,
            "added_by": "bewr"}


def legit(domain):
    return {"domain": domain, "note": "", "added": 1793700000, "added_by": "bewr"}


def allowed(domain):
    return {"domain": domain, "scope": "all", "expires": None, "note": "", "added": 1793700000, "added_by": "bewr"}


def entries(*items):
    return json.dumps({"entries": list(items)}) + "\n"


class HostStepsTest(SetupTest):
    """The Host steps, done by setup on a Host fresh from the RPM (#102)."""

    def seed_domains(self, name):
        with open(os.path.join(REPO, "proxy", name)) as f:
            return {line.split("#", 1)[0].split()[0] for line in f if line.split("#", 1)[0].strip()}

    def stored(self, name):
        return {e["domain"] for e in json.loads(self.read(f"etc/agentbox/{name}"))["entries"]}

    def test_a_service_on_the_proxy_port_of_all_addresses_stops_setup_before_any_change(self):
        os.makedirs(self.path("proc/net"))
        self.write("proc/net/tcp6", "  sl  local_address remote_address st tx_queue rx_queue tr tm->when retrnsmt"
                                    "   uid  timeout inode\n   0: " + "0" * 32 + ":01BB " + "0" * 32 + ":0000 0A "
                                    "00000000:00000000 00:00000000 00000000     0        0 4242 1\n")
        code, out, err = self.setup_run("--password-file", self.password_file)
        self.assertEqual(code, 1)
        self.assertIn("another service listens on 443/tcp on all of the Host's addresses", err)
        self.assertEqual(out, "Self-test of the detection: passed\n")
        self.assertEqual(self.changing_calls(), [])

    def test_a_fresh_host_gets_every_host_step(self):
        out = self.ok("--password-file", self.password_file)
        # the owner controls the VMs, and who controls them may write Activity
        self.assertEqual(self.members("libvirt"), ["alice"])
        self.assertEqual(self.members("agentbox-journal"), ["alice"])
        # the lists of the Release, taken over into records, and an empty list of Harmless marks
        self.assertEqual(self.stored("allowlist.json"), self.seed_domains("allowlist"))
        self.assertEqual(self.stored("watchlist.json"), self.seed_domains("suspicious-domains"))
        # (but for those the classifier has built in)
        self.assertLessEqual(self.stored("legitimate.json"), self.seed_domains("legitimate-domains"))
        self.assertGreater(len(self.stored("legitimate.json")), 50)
        conf = sorted(os.listdir(self.path("etc/agentbox")))
        for name in SEEDS:
            self.assertIn(f"{name}.migrated", conf)
            self.assertNotIn(name, conf)
        self.assertEqual(meaningful(self.read("etc/agentbox/harmless")), [])
        for name in ("allowlist.json", "watchlist.json", "legitimate.json", "harmless"):
            self.assertEqual(self.mode(f"etc/agentbox/{name}"), 0o644, name)
        # Builds need nothing of the owner's (#113: Orca comes from its GitHub release)
        self.assertFalse(os.path.lexists(self.path("etc/agentbox/build.conf")))
        # VM networks: proxy and token service open, ssh closed, also in the running firewall
        for side in ("permanent", "runtime"):
            self.assertEqual(sorted(self.zone("libvirt", side)["ports"]), ["443/tcp", "53/tcp", "53/udp", "9100/tcp"])
            self.assertNotIn("ssh", self.zone("libvirt", side)["services"])
        # units enabled at boot and running now
        self.assertLessEqual(set(ENABLED), set(self.enabled()))
        systemctl = [c["argv"] for c in self.calls("systemctl")]
        for unit in ("libvirt-guests.service", "agentbox-allowlist-expire.timer"):
            self.assertIn(["start", unit], systemctl)
        self.assertIn(["restart", "agentbox-vmnet.service"], systemctl)     # by regenerate, with the new rules
        self.assertNotIn(["restart", "libvirt-guests.service"], systemctl)  # that would shut every VM down
        # the first Feeds right away, not only when their timer is due
        self.assertIn(["start", "--no-block", "agentbox-feeds.service"], systemctl)
        self.assertTrue(os.path.isfile(self.path("etc/agentbox/vm-input.nft")))
        guests = self.read("etc/sysconfig/libvirt-guests")
        for line in ("ON_BOOT=ignore", "ON_SHUTDOWN=shutdown", "PARALLEL_SHUTDOWN=254", "SHUTDOWN_TIMEOUT=60"):
            self.assertIn(line, meaningful(guests))
        # the image directory the package would have made, without copy-on-write, and the
        # modes the package gives them
        self.assertEqual(self.mode("var/lib/agentbox/images"), 0o711)
        self.assertEqual(self.mode("var/lib/agentbox/vms"), 0o711)
        self.assertIn(f"{self.path('var/lib/agentbox/images')} created (without copy-on-write)", out)
        self.assertEqual([c["argv"] for c in self.calls("chattr")], [["+C", self.path("var/lib/agentbox/images")]])
        # each step says what it did
        for step in ("Self-test of the detection: passed", "Groups: alice in the group libvirt",
                     "Lists: suspicious-domains from", "Firewall zone libvirt: port 53/udp open",
                     "Network rules of the VMs: agentbox-vmnet.service enabled", "migrate: up to date",
                     "migrate-allowlist: Allowlist:", "regenerate: Host setup written", "migrate-lists: ",
                     "Control Room: agentbox-web.service enabled and restarted"):
            self.assertIn(step, out)

    def test_the_steps_run_in_their_order(self):
        """ARCHITECTURE.md, "Setting a Host up": the self-test before anything changes, the
        migrations around regenerate, the sockets of the Activity journal only after regenerate
        gave the units their group, the Control Room last."""
        out = self.ok("--password-file", self.password_file, "--json")
        self.assertEqual([s["step"] for s in json.loads(out)["steps"]],
                         ["Self-test of the detection", "Directories", "Groups", "Lists", "Firewall zone libvirt",
                          "Network rules of the VMs", "libvirt-guests", "Expiry of Temporary allowlist entries", "migrate",
                          "migrate-allowlist", "Certificate and password", "Firewall for the home network", "regenerate",
                          "Proxies", "migrate-lists", "Activity journal", "Feeds", "Control Room"])

    def test_libvirt_guests_shuts_the_vms_down_and_is_never_restarted(self):
        """The configuration test_lifecycle.py checks; stopping or restarting the service would
        shut every running VM down, also when setup runs again."""
        self.ok("--password-file", self.password_file)
        self.assertEqual(meaningful(self.read("etc/sysconfig/libvirt-guests")),
                         ["URIS=qemu:///system", "ON_BOOT=ignore", "ON_SHUTDOWN=shutdown", "PARALLEL_SHUTDOWN=254",
                          "SHUTDOWN_TIMEOUT=60"])
        self.scenario(systemctl_active=ENABLED)
        self.ok("--password-file", self.password_file, "--force")
        verbs = {c["argv"][0] for c in self.calls("systemctl") if "libvirt-guests.service" in c["argv"]}
        self.assertIn("enable", verbs)
        self.assertEqual(verbs & {"stop", "restart", "try-restart", "reload-or-restart", "kill"}, set())

    def fetched(self):
        """The first Feeds as agentbox-feeds leaves them."""
        os.makedirs(self.path("var/lib/agentbox/feeds"), exist_ok=True)
        self.write("var/lib/agentbox/feeds/status.json", "{}\n")

    def test_a_second_run_changes_nothing(self):
        self.ok("--password-file", self.password_file)
        self.scenario(systemctl_active=ENABLED)        # what the first run started runs
        self.fetched()
        before = self.snapshot()
        self.forget_calls()
        out = self.ok()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.changing_calls(), [])
        self.assertIn("Host setup unchanged: Local-only", out)
        for step in ("Directories", "Groups", "Lists", "Firewall zone libvirt", "Network rules of the VMs",
                     "libvirt-guests", "migrate", "migrate-allowlist", "regenerate", "Proxies", "migrate-lists",
                     "Activity journal", "Feeds", "Control Room"):
            self.assertIn(f"\n{step}: up to date\n", "\n" + out)
        # also in Network mode, with its rules in place
        self.forget_calls()
        self.ok(*NETWORK_ARGS, "--password-file", self.password_file, "--force")
        before = self.snapshot()
        self.forget_calls()
        self.ok("--json")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.changing_calls(), [])

    def test_running_it_again_finishes_what_a_stopped_run_left_and_keeps_the_host_setup(self):
        vm_records.write_host_setup(self.root, LOCAL_RECORD, owner_user="bob")
        before = self.read("etc/agentbox/host.json")
        result = json.loads(self.ok("--json", sudo_user=None))
        self.assertEqual(result["host_setup"], "kept")
        self.assertEqual(self.read("etc/agentbox/host.json"), before)
        self.assertFalse(os.path.exists(self.path("etc/agentbox/web/password")))
        # the owner user comes from the Host setup
        self.assertEqual(self.members("libvirt"), ["bob"])
        self.assertLessEqual(set(ENABLED), set(self.enabled()))
        systemctl = [c["argv"] for c in self.calls("systemctl")]
        self.assertIn(["start", "agentbox-web.service"], systemctl)
        self.assertNotIn(["restart", "agentbox-web.service"], systemctl)

    def test_a_host_setup_without_owner_user_gets_it_once(self):
        """Written by setup before it kept the owner user: the first run without options records
        the sudo user, and from then on another sudo user does not replace it."""
        before_owner = {k: v for k, v in LOCAL_RECORD.items() if k != "owner_user"}
        vm_records.write_host_setup(self.root, before_owner)
        result = json.loads(self.ok("--json"))
        self.assertEqual(result["host_setup"], "owner_user_added")
        self.assertEqual(self.host_setup(), LOCAL_RECORD)
        self.forget_calls()
        self.assertIn("Host setup unchanged", self.ok(sudo_user="bob"))
        self.assertEqual(self.host_setup(), LOCAL_RECORD)
        self.assertEqual(self.members("libvirt"), ["alice"])
        # --owner-user names it instead, but only while none is recorded
        code, _, err = self.setup_run("--owner-user", "bob")
        self.assertEqual(code, 1, err)
        self.assertIn("--force", err)
        vm_records.write_host_setup(self.root, before_owner)
        out = self.ok("--owner-user", "bob")
        self.assertIn("owner_user bob added to the Host setup", out)
        self.assertIn("Host setup completed with the owner bob", out)
        self.assertEqual(self.host_setup(), {**LOCAL_RECORD, "owner_user": "bob"})

    def test_existing_records_and_lists_are_never_overwritten(self):
        self.write("etc/agentbox/allowlist.json", entries(allowed("docs.rs")))
        self.write("etc/agentbox/watchlist.json", entries(watch("webhook.site")))
        self.write("etc/agentbox/legitimate.json", entries(legit("pnpm.io")))
        self.write("etc/agentbox/harmless", "npm.example\n")
        self.write("etc/agentbox/settings.json", '{"builds_kept": 5}\n')
        keep = ["allowlist.json", "watchlist.json", "legitimate.json", "harmless", "settings.json"]
        before = {name: self.read(f"etc/agentbox/{name}") for name in keep}
        self.ok("--password-file", self.password_file)
        self.assertEqual({name: self.read(f"etc/agentbox/{name}") for name in keep}, before)
        conf = os.listdir(self.path("etc/agentbox"))
        for name in SEEDS:
            self.assertNotIn(name, conf)
            self.assertNotIn(f"{name}.migrated", conf)

    def test_a_host_s_text_lists_from_before_the_records_are_taken_over_not_replaced(self):
        self.write("etc/agentbox/suspicious-domains", "own.example exfil\n")
        self.write("etc/agentbox/allowlist", "own-allowed.example\n")
        self.ok("--password-file", self.password_file)
        self.assertEqual(self.stored("watchlist.json"), {"own.example"})
        self.assertEqual(self.stored("allowlist.json"), {"own-allowed.example"})
        # the Legitimate domains had neither: those of the Release
        self.assertIn("legitimate-domains.migrated", os.listdir(self.path("etc/agentbox")))
        self.assertLessEqual(self.stored("legitimate.json"), self.seed_domains("legitimate-domains"))
        self.assertGreater(len(self.stored("legitimate.json")), 50)

    def test_running_proxies_take_the_installed_code_before_the_text_lists_go(self):
        """Proxies with code from before the records read only the text lists: they restart onto
        the installed code, with the units regenerate loaded, before migrate-lists moves those
        lists aside."""
        vm_records.write(self.root)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})
        self.write("etc/agentbox/suspicious-domains", "own.example exfil\n")
        self.write("etc/agentbox/legitimate-domains", "own-legit.example\n")
        steps = [s["step"] for s in json.loads(self.ok("--password-file", self.password_file, "--json"))["steps"]]
        self.assertEqual(steps[steps.index("regenerate"):steps.index("migrate-lists") + 1],
                         ["regenerate", "Proxies", "migrate-lists"])
        systemctl = [c["argv"] for c in self.calls("systemctl")]
        restart = systemctl.index(["try-restart", "agentbox-proxy-*.service"])
        self.assertLess(max(i for i, a in enumerate(systemctl) if a == ["daemon-reload"]), restart)
        self.assertEqual(self.stored("watchlist.json"), {"own.example"})

    def test_a_proxy_that_cannot_restart_stops_setup_before_the_text_lists_go(self):
        self.ok("--password-file", self.password_file)
        # a text list as before the records
        self.write("etc/agentbox/suspicious-domains", "own.example exfil\n")
        self.scenario(systemctl_fails=["try-restart"])
        code, out, err = self.setup_run()
        self.assertEqual(code, 1, err)
        self.assertIn("setup stopped at Proxies: ", err)
        self.assertIn("run agentbox setup again without options", err)
        self.assertEqual(self.read("etc/agentbox/suspicious-domains"), "own.example exfil\n")
        self.assertNotIn("own.example", self.stored("watchlist.json"))
        # fixed, setup without options goes on from there
        self.scenario()
        self.assertIn("Proxies: running proxies restarted", self.ok())
        self.assertIn("own.example", self.stored("watchlist.json"))
        self.assertFalse(os.path.exists(self.path("etc/agentbox/suspicious-domains")))
        self.assertIn("\nProxies: up to date\n", self.ok())

    def test_a_list_lost_after_its_migration_is_not_filled_from_the_release(self):
        self.write("etc/agentbox/suspicious-domains.migrated", "")
        self.write("etc/agentbox/legitimate-domains.migrated", "")
        code, _, err = self.setup_run("--password-file", self.password_file)
        self.assertEqual(code, 0, err)
        self.assertIn(f"WARNING: {self.path('etc/agentbox/watchlist.json')} is missing", err)
        self.assertIn("Open web stays closed", err)
        self.assertIn(f"WARNING: {self.path('etc/agentbox/legitimate.json')} is missing", err)
        self.assertIn("only the built-in Legitimate domains", err)
        self.assertFalse(os.path.exists(self.path("etc/agentbox/watchlist.json")))
        self.assertFalse(os.path.exists(self.path("etc/agentbox/suspicious-domains")))
        # an empty record of the Legitimate domains only adds Signals: migrate-lists makes one
        self.assertEqual(self.stored("legitimate.json"), set())

    def test_list_source_is_the_text_file_before_the_migration_and_none_after_a_lost_record(self):
        sys.path.insert(0, REPO)
        try:
            from agentbox import hoststeps, paths
        finally:
            sys.path.remove(REPO)
        conf, seeds = os.path.join(self.dir, "ls-conf"), os.path.join(self.dir, "ls-seeds")
        os.makedirs(conf)
        with mock.patch.object(paths, "CONF", conf), mock.patch.object(paths, "SEED_LISTS", seeds):
            args = ("watchlist.json", "suspicious-domains")
            # a new Host: the list of the Release
            self.assertEqual(hoststeps.list_source(*args), os.path.join(seeds, "suspicious-domains"))
            # before the migration: the Host's text file
            with open(os.path.join(conf, "suspicious-domains"), "w") as f:
                f.write("own.example exfil\n")
            self.assertEqual(hoststeps.list_source(*args), os.path.join(conf, "suspicious-domains"))
            # the record wins over the text file
            with open(os.path.join(conf, "watchlist.json"), "w") as f:
                f.write("{}")
            self.assertEqual(hoststeps.list_source(*args), os.path.join(conf, "watchlist.json"))
            # after the migration with the record lost: no Host list, and not the Release's
            os.remove(os.path.join(conf, "watchlist.json"))
            os.rename(os.path.join(conf, "suspicious-domains"), os.path.join(conf, "suspicious-domains.migrated"))
            self.assertIsNone(hoststeps.list_source(*args))

    def test_agentbox_journal_gets_exactly_the_members_of_libvirt(self):
        home = os.path.join(self.dir, "home")
        world = accounts(home)
        world["users"]["carol"] = {"uid": 1002, "gid": LIBVIRT_GID, "home": os.path.join(home, "carol")}
        world["groups"]["libvirt"]["members"] = ["bob"]
        world["groups"]["agentbox-journal"]["members"] = ["mallory"]
        self.scenario(accounts=world)
        self.ok("--password-file", self.password_file)
        self.assertEqual(self.members("libvirt"), ["alice", "bob"])
        self.assertEqual(self.members("agentbox-journal"), ["alice", "bob", "carol"])
        # an owner whose primary group is libvirt is in it already
        self.forget_calls()
        self.ok("--password-file", self.password_file, "--owner-user", "carol", "--force")
        self.assertEqual(self.calls("usermod"), [])

    def test_the_owner_user_must_be_known_and_exist(self):
        code, _, err = self.setup_run("--password-file", self.password_file, sudo_user=None)
        self.assertEqual(code, 2, err)
        self.assertIn("--owner-user", err)
        code, _, err = self.setup_run("--password-file", self.password_file, "--owner-user", "nobody2")
        self.assertEqual(code, 1, err)
        self.assertIn("there is no user nobody2", err)
        self.assertFalse(os.path.exists(self.path("etc/agentbox/host.json")))
        self.assertEqual(self.changing_calls(), [])
        # sudo by root is no owner either; --owner-user names one
        self.assertEqual(self.setup_run("--password-file", self.password_file, sudo_user="root")[0], 2)
        self.ok("--password-file", self.password_file, "--owner-user", "bob", sudo_user=None)
        self.assertEqual(self.host_setup()["owner_user"], "bob")

    def test_the_vm_networks_are_closed_before_any_vm_can_start(self):
        vm_records.write(self.root)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})
        self.ok("--password-file", self.password_file)
        calls = [(c["cmd"], c.get("argv", [])) for c in self.calls()]

        def first(match):
            return next(i for i, call in enumerate(calls) if match(*call))

        zone = first(lambda cmd, argv: cmd == "firewall-cmd" and "--add-port=443/tcp" in argv)
        boot = first(lambda cmd, argv: cmd == "systemctl" and argv == ["enable", "agentbox-vmnet.service"])
        loaded = first(lambda cmd, argv: cmd == "systemctl" and argv == ["restart", "agentbox-vmnet.service"])
        # fake_virsh records its commands by name: a VM network, a domain, a start
        vm = first(lambda cmd, argv: cmd in ("net-define", "net-start", "define", "start", "virt-install")
                   or (cmd == "systemctl" and any(a.startswith("agentbox-gui-") for a in argv)))
        self.assertLess(zone, boot)
        self.assertLess(boot, loaded)
        self.assertLess(loaded, vm)

    def test_on_an_upgraded_host_the_rules_there_are_loaded_before_any_vm_network(self):
        vm_records.write(self.root)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})
        self.write("etc/agentbox/vm-input.nft", "table inet agentbox_vm\n")
        self.ok("--password-file", self.password_file)
        calls = [(c["cmd"], c.get("argv", [])) for c in self.calls()]
        started = calls.index(("systemctl", ["start", "agentbox-vmnet.service"]))
        network = next(i for i, (cmd, _) in enumerate(calls) if cmd == "net-define")
        self.assertLess(started, network)

    def test_a_step_that_fails_stops_setup_and_says_which(self):
        images = self.path("var/lib/agentbox/images")

        def without_libvirt():
            world = accounts(os.path.join(self.dir, "home"))
            del world["groups"]["libvirt"]
            self.scenario(accounts=world)

        def no_sysconfig():
            os.rmdir(self.path("etc/sysconfig"))
            return lambda: os.makedirs(self.path("etc/sysconfig"))

        def image_dir_is_a_file():
            self.write("var/lib/agentbox/images", "")
            return lambda: os.remove(images)

        def broken_watchlist():
            self.write("etc/agentbox/watchlist.json", "{kaputt")
            return lambda: os.remove(self.path("etc/agentbox/watchlist.json"))

        # (step, what breaks it and returns how to undo that, what the message says)
        cases = [("Self-test of the detection", broken_watchlist, "not valid JSON"),
                 ("Directories", image_dir_is_a_file, images),
                 ("Groups", without_libvirt, "the group libvirt is missing"),
                 ("Firewall zone libvirt", lambda: self.scenario(firewall_down=True), "FirewallD is not running"),
                 ("Network rules of the VMs", lambda: self.scenario(systemctl_fails=["enable"]), "systemctl enable"),
                 ("libvirt-guests", no_sysconfig, "libvirt-guests")]
        for step, breaks, said in cases:
            with self.subTest(step):
                for state in ("accounts.json", "firewall.json", "systemctl.json"):
                    if os.path.exists(os.path.join(self.dir, state)):
                        os.remove(os.path.join(self.dir, state))
                self.scenario()
                undo = breaks()
                code, out, err = self.setup_run("--password-file", self.password_file)
                self.assertEqual(code, 1, err)
                self.assertIn(f"agentbox: setup stopped at {step}: ", err)
                self.assertIn(said, err)
                self.assertIn("run setup again", err)
                # the Host setup and the password come only after the steps before them
                self.assertFalse(os.path.exists(self.path("etc/agentbox/host.json")))
                self.assertFalse(os.path.exists(self.path("etc/agentbox/web/password")))
                self.assertNotIn(f"\n{step}: ", "\n" + out)
                self.assertNotIn(["enable", "agentbox-web.service"], [c["argv"] for c in self.calls("systemctl")])
                # no file left half-written
                self.assertEqual([p for p in self.snapshot() if os.path.basename(p).startswith(".")], [])
                if undo:
                    undo()
        # once the cause is gone, the same command finishes
        self.scenario()
        self.ok("--password-file", self.password_file)

    def test_a_failure_after_the_host_setup_is_finished_by_running_it_again_without_options(self):
        self.scenario(systemctl_fails=["restart"])     # regenerate cannot load the VM network rules
        code, _, err = self.setup_run("--password-file", self.password_file)
        self.assertEqual(code, 1, err)
        self.assertIn("setup stopped at regenerate: ", err)
        self.assertIn("run agentbox setup again without options", err)
        self.assertEqual(self.host_setup(), LOCAL_RECORD)
        self.scenario()
        out = self.ok()
        self.assertIn("network rules reloaded", out)
        self.assertIn(["start", "agentbox-web.service"], [c["argv"] for c in self.calls("systemctl")])

    def test_a_broken_list_of_the_host_fails_the_self_test_before_anything_changes(self):
        self.write("etc/agentbox/watchlist.json", "{kaputt")
        before = self.snapshot()
        code, _, err = self.setup_run("--password-file", self.password_file)
        self.assertEqual(code, 1, err)
        self.assertIn("setup stopped at Self-test of the detection: ", err)
        self.assertIn(f"{self.path('etc/agentbox/watchlist.json')}: not valid JSON", err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.changing_calls(), [])

    def test_the_control_room_cannot_run_setup_through_its_sudo_rule(self):
        """Its rule lets agentbox-web run agentbox with any arguments: setup would put it into
        libvirt, which controls every VM, or change how the Host is exposed."""
        refused = "setup is for the owner at the Host, not for agentbox-web"
        for args in (["--password-file", self.password_file],
                     ["--owner-user", "alice", "--password-file", self.password_file]):
            code, _, err = self.setup_run(*args, sudo_user="agentbox-web", sudo_uid=990)
            self.assertEqual(code, 1, err)
            self.assertIn(refused, err)
        # set up already: neither the Host steps nor --force
        vm_records.write_host_setup(self.root, LOCAL_RECORD)
        before = self.snapshot()
        for args in ([], [*NETWORK_ARGS, "--password-file", self.password_file, "--force"]):
            code, _, err = self.setup_run(*args, sudo_user="agentbox-web", sudo_uid=990)
            self.assertEqual(code, 1, err)
            self.assertIn(refused, err)
        # nor any other system user, also without SUDO_UID
        world = accounts(os.path.join(self.dir, "home"))
        world["users"]["backup"] = {"uid": 34, "gid": 34, "home": "/var/backups"}
        self.scenario(accounts=world)
        for uid in (34, None):
            code, _, err = self.setup_run(sudo_user="backup", sudo_uid=uid)
            self.assertEqual(code, 1, err)
            self.assertIn("not for backup", err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([c for c in self.calls() if c["cmd"] != "getent"], [])
        # root through sudo and a person still may
        self.ok(sudo_user="root", sudo_uid=0)
        self.ok(sudo_uid=1000)

    def test_the_owner_user_must_be_a_persons_account(self):
        home = os.path.join(self.dir, "home")
        world = accounts(home)
        world["users"]["backup"] = {"uid": 34, "gid": 34, "home": "/var/backups"}
        world["users"]["kiosk"] = {"uid": 1005, "gid": 1005, "home": os.path.join(home, "kiosk"),
                                   "shell": "/bin/false"}
        self.scenario(accounts=world)
        for user in ("agentbox-web", "backup", "kiosk", "root"):
            with self.subTest(user):
                code, _, err = self.setup_run("--password-file", self.password_file, "--owner-user", user)
                self.assertEqual(code, 1, err)
                self.assertIn(f"{user} cannot be the owner", err)
                self.assertFalse(os.path.exists(self.path("etc/agentbox/host.json")))
                self.assertEqual(self.changing_calls(), [])
        # the Host's UID_MIN counts, for the owner and for who runs sudo
        self.write("etc/login.defs", "# comment\nUID_MIN                  1001   # people\nUID_MAX 60000\n")
        code, _, err = self.setup_run("--password-file", self.password_file, "--owner-user", "alice",
                                      sudo_user="bob")
        self.assertEqual(code, 1, err)
        self.assertIn("alice cannot be the owner: not a person's account (uid 1000", err)
        self.assertIn("uid 1001 or above", err)
        self.assertIn("not for alice", self.setup_run("--password-file", self.password_file)[2])
        self.ok("--password-file", self.password_file, sudo_user="bob")
        self.assertEqual(self.host_setup()["owner_user"], "bob")

    def test_a_password_there_stays_unless_forced(self):
        os.makedirs(self.path("etc/agentbox/web"))
        self.write("etc/agentbox/web/password", "scrypt$32768$8$1$00$00\n", 0o640)
        # no password asked for (stdin is no terminal)
        out = self.ok()
        self.assertIn("the existing password stays", out)
        self.assertEqual(self.read("etc/agentbox/web/password"), "scrypt$32768$8$1$00$00\n")
        self.ok("--password-file", self.password_file, "--force")
        self.assertNotEqual(self.read("etc/agentbox/web/password"), "scrypt$32768$8$1$00$00\n")

    def test_running_sockets_of_the_activity_journal_admit_only_its_group(self):
        """Made before the package's drop-in applied: systemd made them before the package
        was installed."""
        folder = self.path("run/systemd/journal.agentbox")
        os.makedirs(folder)
        for name in ("stdout", "dev-log"):          # not socket: agentbox itself would write there
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.bind(os.path.join(folder, name))
            os.chmod(os.path.join(folder, name), 0o666)
        out = self.ok("--password-file", self.password_file)
        self.assertIn(f"Activity journal: {folder}/stdout: agentbox-journal, mode 660", out)
        for name in ("stdout", "dev-log"):
            self.assertEqual(os.stat(os.path.join(folder, name)).st_mode & 0o777, 0o660)
        self.assertIn("\nActivity journal: up to date\n", self.ok())

    def test_copy_on_write_stays_where_the_file_system_has_none(self):
        self.scenario(chattr_fails=True)
        out = self.ok("--password-file", self.password_file)
        self.assertIn(f"Directories: {self.path('var/lib/agentbox/images')} created;", out)
        self.assertNotIn("copy-on-write", out)


if __name__ == "__main__":
    unittest.main()
