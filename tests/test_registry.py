"""The VM registry: migration of work and priv, configuration generated from it,
and the token service reading GitHub owners from it.

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and fake
virsh, qemu-img, systemctl and virt-install on PATH.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from vm_records import record  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(REPO, "bin", "agentbox")
TOKEND = os.path.join(REPO, "github-app", "agentbox-gh-tokend")
TESTS = os.path.join(REPO, "tests")
PRIV_UUID = "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"


def fake(path, program):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nexec python3 "{os.path.join(TESTS, program)}" "$@"\n')
    os.chmod(path, 0o755)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)
        self.root = os.path.join(self.dir, "root")
        for sub in ("var/lib/agentbox/vms", "etc/agentbox/github", "etc/agentbox/secrets", "etc/systemd/system"):
            os.makedirs(self.path(sub))
        fake(os.path.join(self.dir, "bin", "virsh"), "fake_virsh.py")
        fake(os.path.join(self.dir, "bin", "qemu-img"), "fake_qemu_img.py")
        fake(os.path.join(self.dir, "bin", "systemctl"), "fake_systemctl.py")
        fake(os.path.join(self.dir, "bin", "virt-install"), "fake_virt_install.py")
        fake(os.path.join(self.dir, "bin", "firewall-cmd"), "fake_firewall_cmd.py")
        self.scenario()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, rel):
        return os.path.join(self.root, rel)

    def scenario(self, **s):
        with open(os.path.join(self.dir, "scenario.json"), "w") as f:
            json.dump(s, f)

    def write(self, rel, content, mode=0o644):
        with open(self.path(rel), "w") as f:
            f.write(content)
        os.chmod(self.path(rel), mode)

    def read(self, rel):
        with open(self.path(rel)) as f:
            return f.read()

    def mode(self, rel):
        return stat.S_IMODE(os.stat(self.path(rel)).st_mode)

    def run_agentbox(self, *args):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def json(self, *args):
        code, out, err = self.run_agentbox(*args, "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def calls(self, cmd=None):
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                calls = [json.loads(line) for line in f]
        except FileNotFoundError:
            return []
        return [c for c in calls if cmd is None or c["cmd"] == cmd]

    def forget_calls(self):
        if os.path.exists(os.path.join(self.dir, "calls.jsonl")):
            os.remove(os.path.join(self.dir, "calls.jsonl"))

    def registry(self):
        try:
            return sorted(os.listdir(self.path("etc/agentbox/vms")))
        except FileNotFoundError:
            return []

    def snapshot(self):
        """Every file under the root with its content and mode."""
        files = {}
        for top, _, names in os.walk(self.root):
            for n in names:
                p = os.path.join(top, n)
                with open(p, "rb") as f:
                    files[os.path.relpath(p, self.root)] = (f.read(), stat.S_IMODE(os.stat(p).st_mode))
        return files


class MigrateTest(RegistryTest):
    def legacy(self):
        """The state of the fixed VMs work and priv before the registry."""
        self.write("etc/agentbox/github/owners-priv",
                   "# GitHub-Accounts/Orgs, fuer die priv Tokens bekommt (einer pro Zeile)\nlikt0r\n\n# alt: x\n",
                   mode=0o640)
        self.write("etc/agentbox/github/owners-work",
                   "# GitHub-Accounts/Orgs, fuer die work Tokens bekommt (einer pro Zeile)\n\n", mode=0o640)
        for vm in ("work", "priv"):
            self.write(f"var/lib/agentbox/vms/{vm}-data.qcow2", "{}")

    def test_creates_records_for_work_and_priv_from_the_current_state(self):
        self.legacy()
        code, out, err = self.run_agentbox("migrate")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "work: taken over into the registry (network 1, Orca port 6801)\n"
                              "priv: taken over into the registry (network 2, Orca port 6802)\n")
        self.assertEqual(self.registry(), ["priv.json", "work.json"])
        priv = vm_records.read(self.root, "priv")
        created = priv.pop("created")
        self.assertRegex(created, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        # memory from the domain (fake: 8388608 KiB), Data disk size from qemu-img (fake: 20 GiB)
        self.assertEqual(priv, {"name": "priv", "network": 2, "orca_port": 6802, "image": "base", "build": None,
                                "memory_mib": 8192, "data_disk_gib": 20, "github_owners": ["likt0r"]})
        self.assertEqual(vm_records.read(self.root, "work")["github_owners"], [])
        self.assertEqual(self.mode("etc/agentbox/vms/priv.json"), 0o644)
        self.assertEqual(os.listdir(self.path("etc/agentbox/github")), [])

    def test_keeps_disks_and_secrets(self):
        self.legacy()
        self.write("etc/agentbox/secrets/claude-priv", "sk-ant-x\n", mode=0o640)
        before = {k: v for k, v in self.snapshot().items() if "/github/" not in k}
        self.run_agentbox("migrate")
        after = {k: v for k, v in self.snapshot().items()
                 if ("/vms/" not in k or k.endswith(".qcow2")) and "/locks/" not in k}
        self.assertEqual(after, before)
        self.assertEqual([c for c in self.calls() if c["cmd"] not in ("qemu-img",)], [])

    def test_is_idempotent(self):
        self.legacy()
        self.run_agentbox("migrate")
        records = self.snapshot()
        self.assertEqual(self.json("migrate"), [{"vm": "work", "action": "unchanged"},
                                                {"vm": "priv", "action": "unchanged"}])
        self.assertEqual(self.snapshot(), records)
        self.assertEqual(self.run_agentbox("migrate"), (0, "work: in the registry already\npriv: in the registry already\n", ""))

    def test_json(self):
        self.legacy()
        self.assertEqual(self.json("migrate"), [{"vm": "work", "action": "migrated"},
                                                {"vm": "priv", "action": "migrated"}])

    def test_vm_that_never_existed_is_left_out(self):
        self.write("var/lib/agentbox/vms/priv-data.qcow2", "{}")
        self.scenario(vms={"work": {"defined": False}, "priv": {"defined": False}})
        self.assertEqual(self.json("migrate"), [{"vm": "work", "action": "absent"},
                                                {"vm": "priv", "action": "migrated"}])
        self.assertEqual(self.registry(), ["priv.json"])
        # without domain the default the fixed VMs had
        self.assertEqual(vm_records.read(self.root, "priv")["memory_mib"], 26624)

    def test_existing_record_wins_over_a_left_over_owners_file(self):
        vm_records.write(self.root, record("priv", 2, github_owners=["alice"]))
        self.write("etc/agentbox/github/owners-priv", "bob\n")
        self.scenario(vms={"work": {"defined": False}})
        self.assertEqual(self.json("migrate"), [{"vm": "work", "action": "absent"},
                                                {"vm": "priv", "action": "unchanged"}])
        self.assertEqual(vm_records.read(self.root, "priv")["github_owners"], ["alice"])
        self.assertEqual(os.listdir(self.path("etc/agentbox/github")), [])


PROXY_SOCKET = """[Unit]
Description=Allowlist proxy for VM priv

[Socket]
ListenStream=10.77.2.1:443
FreeBind=yes

[Install]
WantedBy=sockets.target
"""
GUI_SERVICE = """[Unit]
Description=Orca connection to VM priv

[Service]
ExecStartPre=/usr/bin/agentbox "--actor=Orca (${REMOTE_ADDR})" wake priv
ExecStart=/usr/bin/socat STDIO TCP:10.77.2.2:6802
StandardInput=socket
StandardOutput=socket
StandardError=journal
TimeoutStartSec=180
"""
NETWORK = """<network xmlns:dnsmasq='http://libvirt.org/schemas/network/dnsmasq/1.0'>
  <name>agentbox-priv</name>
  <bridge name='abox-priv' stp='on' delay='0'/>
  <ip address='10.77.2.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='10.77.2.2' end='10.77.2.2'/>
      <host mac='52:54:00:77:02:02' ip='10.77.2.2'/>
    </dhcp>
  </ip>
  <dnsmasq:options>
    <dnsmasq:option value='address=/#/10.77.2.1'/>
    <dnsmasq:option value='local=/#/'/>
    <dnsmasq:option value='no-hosts'/>
    <dnsmasq:option value='no-resolv'/>
  </dnsmasq:options>
</network>
"""
NFT = """table inet agentbox_vm
delete table inet agentbox_vm
table inet agentbox_vm {
  chain input {
    type filter hook input priority -5; policy accept
    iifname "abox-*" jump from_vm
  }
  chain from_vm {
    meta nfproto ipv6 drop
    udp dport 67 accept
    iifname "abox-work" ip daddr != 10.77.1.1 drop
    iifname "abox-priv" ip daddr != 10.77.2.1 drop
    ct state established,related accept
    udp dport 53 accept
    tcp dport { 53, 443, 9100 } accept
    drop
  }
}
"""
# a VM network without a resolver, as libvirt dumps it
OLD_NETWORK = """<network>
  <name>agentbox-priv</name>
  <uuid>0b9f5a52-2b7f-4c55-9d0e-6f5c2b1c9a11</uuid>
  <bridge name='abox-priv' stp='on' delay='0'/>
  <mac address='52:54:00:3c:1d:aa'/>
  <dns enable='no'/>
  <ip address='10.77.2.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='10.77.2.2' end='10.77.2.2'/>
      <host mac='52:54:00:77:02:02' ip='10.77.2.2'/>
    </dhcp>
  </ip>
</network>
"""
WORK_NETWORK = (NETWORK.replace("priv", "work").replace("10.77.2.", "10.77.1.")
                .replace("52:54:00:77:02:02", "52:54:00:77:01:02"))
UNITS = ["agentbox-ghvm-{0}.socket", "agentbox-ghvm-{0}@.service", "agentbox-gui-{0}.socket",
         "agentbox-gui-{0}@.service", "agentbox-proxy-{0}.service", "agentbox-proxy-{0}.socket"]


class RegenerateTest(RegistryTest):
    def setUp(self):
        super().setUp()
        vm_records.write(self.root)
        vm_records.write_host_setup(self.root)    # Network mode, as before the Host setup existed
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})

    def units(self):
        """The per-VM units; the timers and services of the Settings are in test_settings.py,
        those of the Builds in test_images.py, those of the version check in test_versions.py."""
        names = os.listdir(self.path("etc/systemd/system"))
        return sorted(u for u in names if not u.startswith(("agentbox-weekly", "agentbox-feeds", "agentbox-rebuild",
                                                             "agentbox-build@", "agentbox-versions")))

    def virt_install(self, vm):
        return [c["argv"] for c in self.calls("virt-install") if f"agentbox-{vm}" in c["argv"]][-1]

    def test_generates_everything_for_each_vm(self):
        code, _, err = self.run_agentbox("regenerate")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.units(), sorted(u.format(vm) for vm in ("work", "priv") for u in UNITS))
        self.assertEqual(self.read("etc/systemd/system/agentbox-proxy-priv.socket"), PROXY_SOCKET)
        self.assertEqual(self.read("etc/systemd/system/agentbox-gui-priv@.service"), GUI_SERVICE)
        self.assertIn("ExecStart=/usr/libexec/agentbox-proxy priv\n",
                      self.read("etc/systemd/system/agentbox-proxy-priv.service"))
        self.assertIn("ListenStream=10.77.2.1:9100\n", self.read("etc/systemd/system/agentbox-ghvm-priv.socket"))
        self.assertIn("ExecStart=/usr/libexec/agentbox-gh-tokend priv\n",
                      self.read("etc/systemd/system/agentbox-ghvm-priv@.service"))
        self.assertIn("ListenStream=6802\n", self.read("etc/systemd/system/agentbox-gui-priv.socket"))
        self.assertEqual(self.read("etc/agentbox/vm-input.nft"), NFT)

    def test_no_explicit_proxy_port_and_dns_switched_on(self):
        self.assertEqual(self.run_agentbox("regenerate")[0], 0)
        self.assertNotIn("3128", self.read("etc/agentbox/vm-input.nft"))
        for name in os.listdir(self.path("etc/systemd/system")):
            with self.subTest(unit=name):
                self.assertNotIn("3128", self.read(f"etc/systemd/system/{name}"))
        for c in self.calls("net-define"):
            with self.subTest(net=c["net"]):
                self.assertNotIn("3128", c["xml"])
                self.assertNotRegex(c["xml"], r"<dns\b[^>]*enable=")
        sys.path.insert(0, REPO)
        from agentbox import hoststeps
        self.assertFalse([p for p in hoststeps.VM_PORTS if p.startswith("3128/")])

    def test_only_dhcp_passes_before_the_address_check(self):
        # a port accepted before it would reach every address of the Host: a resolver on its
        # LAN address, or the .1 of another VM's network
        self.assertEqual(self.run_agentbox("regenerate")[0], 0)
        rules = self.read("etc/agentbox/vm-input.nft").splitlines()
        start = rules.index("  chain from_vm {") + 1
        chain = rules[start:rules.index("  }", start) + 1]
        checks = [i for i, line in enumerate(chain) if " ip daddr != " in line]
        self.assertTrue(checks)
        before = [line for line in chain[:min(checks)] if "accept" in line]
        self.assertEqual(before, ["    udp dport 67 accept"])
        after = chain[max(checks) + 1:]
        self.assertIn("    udp dport 53 accept", after)
        self.assertIn("    tcp dport { 53, 443, 9100 } accept", after)
        self.assertEqual(after[-2:], ["    drop", "  }"])

    def test_defines_networks_that_are_missing(self):
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}},
                      networks={"agentbox-work": {"active": True, "autostart": True, "xml": WORK_NETWORK}})
        self.run_agentbox("regenerate")
        self.assertEqual([(c["net"], c["xml"]) for c in self.calls("net-define")], [("agentbox-priv", NETWORK)])
        self.assertEqual([(c["cmd"], c["net"]) for c in self.calls() if c["cmd"] in ("net-autostart", "net-start")],
                         [("net-autostart", "agentbox-priv"), ("net-start", "agentbox-priv")])

    def test_defines_the_vm_through_virt_install_keeping_its_uuid(self):
        self.run_agentbox("regenerate")
        argv = self.virt_install("priv")
        for opt, val in [("--name", "agentbox-priv"), ("--uuid", PRIV_UUID), ("--memory", "26624"),
                         ("--vcpus", "24"), ("--cpu", "host-passthrough"), ("--machine", "q35"),
                         ("--osinfo", "linux2024"), ("--boot", "hd"),
                         ("--sysinfo", "type=fwcfg,entry0.name=opt/agentbox/context,entry0=priv,"
                                       "entry1.name=opt/agentbox/orca_port,entry1=6802,"
                                       "entry2.name=opt/agentbox/claude_token,"
                                       f"entry2.file={self.path('etc/agentbox/secrets/claude-priv')},"
                                       "entry3.name=opt/agentbox/pairing_address,entry3=192.0.2.8"),
                         ("--network", "network=agentbox-priv,mac=52:54:00:77:02:02,model=virtio"),
                         ("--channel", "unix,target.type=virtio,target.name=org.qemu.guest_agent.0")]:
            with self.subTest(opt=opt):
                self.assertIn(val, [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == opt])
        disks = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--disk"]
        self.assertEqual(len(disks), 2)
        self.assertTrue(disks[0].startswith(f"path={self.path('var/lib/agentbox/vms/priv-system.qcow2')},"))
        self.assertTrue(disks[1].startswith(f"path={self.path('var/lib/agentbox/vms/priv-data.qcow2')},"))
        self.assertIn("serial=agentbox-data", disks[1])
        for flag in ("--print-xml", "--dry-run", "--import"):
            self.assertIn(flag, argv)
        defined = [c for c in self.calls("define") if c["dom"] == "agentbox-priv"]
        self.assertEqual(len(defined), 1)
        self.assertIn(f"<uuid>{PRIV_UUID}</uuid>", defined[0]["xml"])

    def test_creates_data_disk_token_placeholder_and_observer_directory(self):
        self.run_agentbox("regenerate")
        creates = [c["argv"] for c in self.calls("qemu-img") if c["argv"][0] == "create"]
        self.assertEqual(creates, [["create", "-q", "-f", "qcow2", self.path(f"var/lib/agentbox/vms/{vm}-data.qcow2"),
                                    "300G"] for vm in ("work", "priv")])
        self.assertEqual(self.read("etc/agentbox/secrets/claude-priv"), "none\n")
        self.assertEqual(self.mode("etc/agentbox/secrets/claude-priv"), 0o640)
        self.assertEqual(self.mode("var/lib/agentbox/observer-priv"), 0o700)

    def test_keeps_existing_data_disk_and_token(self):
        self.write("var/lib/agentbox/vms/priv-data.qcow2", '{"mine": true}')
        self.write("etc/agentbox/secrets/claude-priv", "sk-ant-x\n", mode=0o640)
        self.run_agentbox("regenerate")
        self.assertEqual(self.read("var/lib/agentbox/vms/priv-data.qcow2"), '{"mine": true}')
        self.assertEqual(self.read("etc/agentbox/secrets/claude-priv"), "sk-ant-x\n")
        self.assertNotIn(self.path("var/lib/agentbox/vms/priv-data.qcow2"),
                         [c["argv"][-2] for c in self.calls("qemu-img") if c["argv"][0] == "create"])

    def test_existing_network_that_does_not_match_the_record_is_an_error(self):
        moved = NETWORK.replace("10.77.2.", "10.77.5.")
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}},
                      networks={"agentbox-priv": {"active": True, "autostart": True, "xml": moved}})
        code, _, err = self.run_agentbox("regenerate")
        self.assertEqual(code, 1)
        self.assertIn("priv: network agentbox-priv does not match the record", err)
        self.assertEqual([c["net"] for c in self.calls("net-define")], ["agentbox-work"])

    def old_network(self, state="running", active=True):
        """priv's network as libvirt dumps one without a resolver, its VM in the given state."""
        self.scenario(vms={"priv": {"uuid": PRIV_UUID, "state": state}},
                      networks={"agentbox-priv": {"active": active, "autostart": True, "xml": OLD_NETWORK}})

    def network_calls(self):
        return [(c["cmd"], c["net"]) for c in self.calls() if c["cmd"].startswith("net-") and c["net"] == "agentbox-priv"]

    def test_a_network_without_the_resolver_is_redefined_and_restarted(self):
        self.old_network()
        code, out, err = self.run_agentbox("regenerate")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([(c["net"], c["xml"]) for c in self.calls("net-define") if c["net"] == "agentbox-priv"],
                         [("agentbox-priv", NETWORK)])
        # dnsmasq reads its options when the network starts
        self.assertEqual(self.network_calls(), [("net-define", "agentbox-priv"), ("net-destroy", "agentbox-priv"),
                                                ("net-start", "agentbox-priv")])
        self.assertIn("priv: network brought to the current definition and restarted; the running VM has no "
                      "network until its next Restart (libvirt does not reconnect a running VM to a restarted "
                      "network)\n", out)
        # once current, it is left alone
        self.forget_calls()
        self.assertNotIn("network", " ".join(self.json("regenerate")["vms"]["priv"]))
        self.assertEqual(self.network_calls(), [])

    def test_with_its_vm_off_the_network_is_restarted_without_a_word_about_the_vm(self):
        self.old_network(state="shut off")
        self.assertEqual(self.json("regenerate")["vms"]["priv"][:1], ["network-redefined"])
        self.assertEqual(self.network_calls(), [("net-define", "agentbox-priv"), ("net-destroy", "agentbox-priv"),
                                                ("net-start", "agentbox-priv")])

    def test_an_inactive_network_without_the_resolver_is_redefined_and_started(self):
        self.old_network(state="shut off", active=False)
        self.assertEqual(self.json("regenerate")["vms"]["priv"][:1], ["network-redefined"])
        self.assertEqual(self.network_calls(), [("net-define", "agentbox-priv"), ("net-start", "agentbox-priv")])

    def test_a_current_network_is_left_alone(self):
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}},
                      networks={"agentbox-priv": {"active": True, "autostart": True, "xml": NETWORK}})
        self.assertEqual(self.run_agentbox("regenerate")[0], 0)
        self.assertEqual(self.network_calls(), [])

    def test_the_zone_of_the_vm_networks_gets_the_ports_the_vms_reach(self):
        # firewalld's zone libvirt rejects what it does not list: regenerate opens them as setup does
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}}, firewall_zones={"libvirt": {"ports": ["9100/tcp"]}})
        code, out, err = self.run_agentbox("regenerate")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("firewall zone libvirt: port 53/udp open; port 53/tcp open; "
                                       "port 443/tcp open\n"), out)
        with open(os.path.join(self.dir, "firewall.json")) as f:
            zones = json.load(f)
        for side in ("permanent", "runtime"):
            self.assertEqual(sorted(zones[side]["libvirt"]["ports"]), ["443/tcp", "53/tcp", "53/udp", "9100/tcp"])
        # the zone opened before any VM network comes up
        cmds = [(c["cmd"], c.get("argv", [])) for c in self.calls()]
        opened = max(i for i, (cmd, argv) in enumerate(cmds) if cmd == "firewall-cmd" and "--reload" in argv)
        started = min(i for i, (cmd, _) in enumerate(cmds) if cmd in ("net-define", "net-start"))
        self.assertLess(opened, started)
        self.forget_calls()
        self.assertEqual(self.json("regenerate")["zone"], [])

    def test_firewall_rules_written_but_not_loaded_are_loaded_next_time(self):
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}}, systemctl_fails=["restart"])
        self.assertEqual(self.run_agentbox("regenerate")[0], 1)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})
        self.forget_calls()
        self.assertTrue(self.json("regenerate")["firewall"])
        self.assertIn(["restart", "agentbox-vmnet.service"], [c["argv"] for c in self.calls("systemctl")])
        self.forget_calls()
        self.assertFalse(self.json("regenerate")["firewall"])
        self.assertNotIn(["restart", "agentbox-vmnet.service"], [c["argv"] for c in self.calls("systemctl")])

    def test_units_written_before_a_failure_are_loaded_next_time(self):
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}}, virt_install_fails=["agentbox-priv"])
        code, _, err = self.run_agentbox("regenerate")
        self.assertEqual(code, 1)
        self.assertIn("priv: virt-install", err)
        self.assertNotIn(["daemon-reload"], [c["argv"] for c in self.calls("systemctl")])
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})
        self.forget_calls()
        self.run_agentbox("regenerate")
        self.assertIn(["daemon-reload"], [c["argv"] for c in self.calls("systemctl")])

    def test_storage_pool_for_the_disks_is_ours_and_made_once(self):
        """virt-install would otherwise make one up for the disk directory."""
        self.assertTrue(self.json("regenerate")["storage"])
        pools = [c for c in self.calls() if c["cmd"].startswith("pool-")]
        self.assertEqual([c["cmd"] for c in pools], ["pool-define", "pool-autostart", "pool-start"])
        self.assertIn("<name>agentbox-vms</name>", pools[0]["xml"])
        self.assertIn(f"<path>{self.path('var/lib/agentbox/vms')}</path>", pools[0]["xml"])
        self.forget_calls()
        self.assertFalse(self.json("regenerate")["storage"])
        self.assertEqual([c for c in self.calls() if c["cmd"].startswith("pool-")], [])

    def test_existing_pool_for_the_disks_is_kept(self):
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}},
                      pools={"vms": {"path": self.path("var/lib/agentbox/vms"), "active": True, "autostart": True}})
        self.assertFalse(self.json("regenerate")["storage"])
        self.assertEqual([c for c in self.calls() if c["cmd"].startswith("pool-")], [])

    def socket_calls(self):
        """What stops or restarts the VMs' own sockets and services."""
        return [c["argv"] for c in self.calls("systemctl") if c["argv"][0] in ("stop", "restart")
                and c["argv"][1].startswith(("agentbox-proxy-", "agentbox-ghvm-", "agentbox-gui-"))]

    def test_a_changed_proxy_socket_is_restarted_after_its_proxy_stopped(self):
        # systemd keeps an active socket where it was, and the proxy keeps the socket it was handed
        self.write("etc/systemd/system/agentbox-proxy-priv.socket", PROXY_SOCKET.replace(":443", ":8443"))
        code, out, err = self.run_agentbox("regenerate")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.read("etc/systemd/system/agentbox-proxy-priv.socket"), PROXY_SOCKET)
        # work's sockets are new: enable --now starts them
        self.assertEqual(self.socket_calls(), [["stop", "agentbox-proxy-priv.service"],
                                               ["restart", "agentbox-proxy-priv.socket"]])
        # after systemd read the new unit
        argv = [c["argv"] for c in self.calls("systemctl")]
        reloaded = max(i for i, a in enumerate(argv) if a == ["daemon-reload"])
        self.assertLess(reloaded, argv.index(["stop", "agentbox-proxy-priv.service"]))
        self.assertIn("priv: changed sockets restarted on their new addresses\n", out)
        self.assertNotIn("work: changed sockets", out)

    def test_a_changed_socket_of_its_own_connections_is_only_restarted(self):
        # Accept=yes: every connection has its own process, none holds the listening socket
        self.run_agentbox("regenerate")
        gui = "etc/systemd/system/agentbox-gui-priv.socket"
        self.write(gui, self.read(gui).replace("ListenStream=", "ListenStream=127.0.0.2:"))
        self.forget_calls()
        self.run_agentbox("regenerate")
        self.assertEqual(self.socket_calls(), [["restart", "agentbox-gui-priv.socket"]])

    def test_unchanged_sockets_are_left_alone(self):
        self.run_agentbox("regenerate")
        self.forget_calls()
        self.run_agentbox("regenerate")
        self.assertEqual(self.socket_calls(), [])

    def test_a_socket_left_to_rebind_by_a_failed_run_is_rebound_by_the_next(self):
        self.run_agentbox("regenerate")
        self.write("etc/systemd/system/agentbox-proxy-priv.socket", PROXY_SOCKET.replace(":443", ":8443"))
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}}, systemctl_fails=["stop"])
        self.assertEqual(self.run_agentbox("regenerate")[0], 1)
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})
        self.forget_calls()
        self.run_agentbox("regenerate")
        self.assertIn(["restart", "agentbox-proxy-priv.socket"], self.socket_calls())

    def test_reloads_systemd_and_firewall_and_enables_the_sockets(self):
        self.run_agentbox("regenerate")
        # firewall rules first: they hold before any bridge comes up; then the journal
        # namespace of Activity, before the units that log into it
        self.assertEqual([c["argv"] for c in self.calls("systemctl")], [
            ["restart", "agentbox-vmnet.service"],
            ["try-restart", "systemd-journald@agentbox.service"],
            ["enable", "--now", "systemd-journald@agentbox.socket"],
            ["stop", "agentbox-weekly.timer"], ["daemon-reload"], ["start", "agentbox-weekly.timer"],
            ["restart", "agentbox-feeds.timer"],
            ["enable", "--now", "agentbox-weekly.timer", "agentbox-feeds.timer"],
            ["stop", "agentbox-rebuild.timer"], ["daemon-reload"], ["start", "agentbox-rebuild.timer"],
            ["enable", "--now", "agentbox-rebuild.timer"],
            ["stop", "agentbox-versions.timer"], ["daemon-reload"], ["start", "agentbox-versions.timer"],
            ["daemon-reload"], ["start", "--no-block", "agentbox-versions.service"],
            ["enable", "--now", "agentbox-versions.timer"],
            ["daemon-reload"],
            ["enable", "--now", "agentbox-proxy-work.socket", "agentbox-ghvm-work.socket", "agentbox-gui-work.socket",
             "agentbox-proxy-priv.socket", "agentbox-ghvm-priv.socket", "agentbox-gui-priv.socket"]])

    def test_second_run_changes_nothing(self):
        self.run_agentbox("regenerate")
        before = self.snapshot()
        self.forget_calls()
        result = self.json("regenerate")
        self.assertEqual(result, {"vms": {"work": [], "priv": []}, "firewall": False, "storage": False,
                                  "retention": None, "timers": False, "zone": []})
        self.assertEqual(self.snapshot(), before)
        changing = [c for c in self.calls() if c["cmd"] in ("define", "net-define", "net-autostart", "net-start")
                    or c["cmd"] == "qemu-img" and c["argv"][0] == "create"
                    or c["cmd"] == "systemctl" and c["argv"][0] != "enable"]
        self.assertEqual(changing, [])
        self.assertEqual(self.run_agentbox("regenerate"), (0, "everything up to date\n", ""))

    def test_first_run_reports_what_it_did(self):
        result = self.json("regenerate")
        self.assertEqual(result, {"vms": {vm: ["network", "units", "data-disk", "claude-token", "observer", "domain"]
                                          for vm in ("work", "priv")}, "firewall": True, "storage": True,
                                  "retention": 90, "timers": True,
                                  "zone": ["port 53/udp open", "port 53/tcp open", "port 443/tcp open", "port 9100/tcp open",
                                           "service ssh removed"]})

    def test_new_vm_keeps_the_uuid_from_its_first_definition(self):
        vm_records.write(self.root, record("lab", 3))
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}, "lab": {"defined": False}})
        self.run_agentbox("regenerate")
        self.assertNotIn("--uuid", self.virt_install("lab"))
        first = [c["xml"] for c in self.calls("define") if c["dom"] == "agentbox-lab"]
        self.assertEqual(len(first), 1)
        self.forget_calls()
        self.assertEqual(self.json("regenerate")["vms"]["lab"], [])
        self.assertIn("--uuid", self.virt_install("lab"))
        self.assertEqual(self.calls("define"), [])

    def test_changed_record_redefines_only_that_vm(self):
        self.run_agentbox("regenerate")
        self.forget_calls()
        vm_records.write(self.root, record("priv", 2, memory_mib=16384))
        self.assertEqual(self.json("regenerate"), {"vms": {"work": [], "priv": ["domain"]}, "firewall": False,
                                                   "storage": False, "retention": None,
                                                   "timers": False, "zone": []})
        self.assertEqual([c["dom"] for c in self.calls("define")], ["agentbox-priv"])
        self.assertIn("16384", self.virt_install("priv"))

    def test_paused_vm_keeps_its_definition_until_it_runs_again(self):
        self.scenario(vms={"priv": {"uuid": PRIV_UUID, "managed_save": True, "state": "shut off"}})
        self.assertEqual(self.json("regenerate")["vms"]["priv"],
                         ["network", "units", "data-disk", "claude-token", "observer", "domain-postponed"])
        self.assertEqual([c["dom"] for c in self.calls("define")], ["agentbox-work"])
        self.scenario(vms={"priv": {"uuid": PRIV_UUID}})
        os.remove(os.path.join(self.dir, "domains.json"))
        self.assertEqual(self.json("regenerate")["vms"]["priv"], ["domain"])

    def test_long_name_gets_a_numbered_bridge(self):
        vm_records.write(self.root, record("research-project", 3))
        self.run_agentbox("regenerate")
        xml = [c["xml"] for c in self.calls("net-define") if c["net"] == "agentbox-research-project"][0]
        self.assertIn("<bridge name='abox-3' stp='on' delay='0'/>", xml)
        self.assertIn('    iifname "abox-3" ip daddr != 10.77.3.1 drop\n', self.read("etc/agentbox/vm-input.nft"))

    def test_broken_record_stops_before_changing_anything(self):
        self.write("etc/agentbox/vms/lab.json", '{"name": "lab", "network": "drei"}')
        code, out, err = self.run_agentbox("regenerate")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("lab", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.units(), [])


class RegistryLookupTest(RegistryTest):
    def test_unknown_and_invalid_names(self):
        vm_records.write(self.root)
        for name in ("nope", "../work", "Work", "work.json"):
            with self.subTest(name=name):
                self.assertEqual(self.run_agentbox("logs", name), (1, "", f"agentbox: No such VM: {name}\n"))
        self.assertEqual(self.run_agentbox("logs"), (1, "", "agentbox: no VM given\n"))

    def status_vms(self):
        code, out, err = self.run_agentbox("status", "--json")
        self.assertEqual(code, 0, err)
        return list(json.loads(out)), err

    def test_record_must_match_its_file_name(self):
        """status shows the VMs that are fine and names the broken record; regenerate refuses."""
        vm_records.write(self.root)
        self.write("etc/agentbox/vms/lab.json", json.dumps(record("work", 3)))
        vms, err = self.status_vms()
        self.assertEqual(vms, ["work", "priv"])
        self.assertIn("lab.json", err)
        code, _, err = self.run_agentbox("regenerate")
        self.assertEqual(code, 1)
        self.assertIn("lab.json", err)

    def test_two_vms_on_one_network_are_refused(self):
        vm_records.write(self.root, record("work", 1), record("lab", 1), record("priv", 2))
        vms, err = self.status_vms()
        self.assertEqual(vms, ["priv"])
        self.assertIn("network 1 given twice: lab and work", err)
        self.assertEqual(self.run_agentbox("regenerate")[0], 1)

    def test_off_is_not_a_vm_name(self):
        """`agentbox focus off` means: no VM in focus."""
        vm_records.write(self.root)
        self.write("etc/agentbox/vms/off.json", json.dumps(record("off", 3)))
        vms, err = self.status_vms()
        self.assertEqual(vms, ["work", "priv"])
        self.assertIn("the name off is reserved", err)

    def test_name_pattern_is_the_same_everywhere(self):
        sys.path.insert(0, REPO)
        from agentbox import registry
        for program in (TOKEND, os.path.join(REPO, "web", "agentbox-web")):
            with self.subTest(program=program), open(program) as f:
                self.assertIn(f're.compile(r"{registry.NAME.pattern}")', f.read())


class TokenServiceTest(unittest.TestCase):
    """agentbox-gh-tokend refuses before it talks to GitHub."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        vm_records.write(self.root, record("priv", 2, github_owners=["likt0r"]), record("work", 1))

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, vm, line):
        r = subprocess.run([TOKEND, vm], input=line + "\n", capture_output=True, text=True, timeout=30,
                           env={"PATH": os.environ["PATH"], "AGENTBOX_ROOT": self.root})
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout), r.stderr

    def test_refuses_owners_not_in_the_record(self):
        answer, log = self.request("priv", "someone/repo")
        self.assertEqual(answer, {"error": "someone is not a GitHub owner of the VM priv"})
        self.assertEqual(log, "[priv] refused: 'someone/repo': owner-not-allowed: someone is not a GitHub owner of "
                              "the VM priv\n")

    def test_vm_without_owners_gets_nothing(self):
        answer, _ = self.request("work", "likt0r/askos")
        self.assertEqual(answer, {"error": "likt0r is not a GitHub owner of the VM work"})

    def test_unknown_vm(self):
        for vm in ("lab", "../priv"):
            with self.subTest(vm=vm):
                answer, _ = self.request(vm, "likt0r/askos")
                self.assertEqual(answer, {"error": "the VM is not in the VM registry"})

    def test_what_it_logs_activity_reads_with_the_reason_and_its_parameters(self):
        """Both sides of the line (#69): the token service writes it, agentbox/activity.py reads
        it. GitHub's error status, with the App's key and GitHub stood in for in-process."""
        import contextlib
        import importlib.machinery
        import importlib.util
        import io
        import urllib.error
        from unittest import mock
        sys.path.insert(0, REPO)
        from agentbox import activity
        loader = importlib.machinery.SourceFileLoader("agentbox_gh_tokend", TOKEND)
        tokend = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(tokend)

        def refused_by_github(*_, **__):
            raise urllib.error.HTTPError("https://api.github.com/x", 502, "Bad Gateway", {}, None)

        cases = [("priv", "likt0r/askos", "github-status", {"status": "502", "app_id": "1"}),
                 ("priv", "someone/repo", "owner-not-allowed", {}),
                 ("priv", "no slash", "invalid-request", {}),
                 ("lab", "likt0r/askos", "vm-not-registered", {})]
        for vm, request, reason, params in cases:
            with self.subTest(reason=reason):
                err, out = io.StringIO(), io.StringIO()
                with mock.patch.object(tokend, "REGISTRY", self.root + "/etc/agentbox/vms"), \
                        mock.patch.object(tokend, "read_conf", lambda _: ["APP_ID=1"]), \
                        mock.patch.object(tokend, "app_jwt", lambda _: "jwt"), \
                        mock.patch.object(tokend.urllib.request, "urlopen", refused_by_github), \
                        mock.patch.object(sys, "argv", ["agentbox-gh-tokend", vm]), \
                        mock.patch.object(sys, "stdin", io.StringIO(request + "\n")), \
                        contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                    tokend.main()
                self.assertNotIn("token", json.loads(out.getvalue()))
                entry = activity.github_entry({"__REALTIME_TIMESTAMP": "1793700000000000",
                                               "SYSLOG_IDENTIFIER": f"agentbox-gh-{vm}", "MESSAGE": err.getvalue().strip()})
                self.assertEqual((entry["code"], entry["reason"], entry["params"]["request"]),
                                 ("github.refused", reason, request))
                self.assertEqual({k: v for k, v in entry["params"].items() if k not in ("request", "repository", "reason")},
                                 params)


class SeveralAppsTest(unittest.TestCase):
    """agentbox-gh-tokend with several Apps (askos#25): it asks them in ascending App ID for
    an installation on the repository, and the first one installed there issues the token.
    GitHub and the Apps' keys stand in-process: app_jwt names the App, a fake urlopen answers
    per App."""

    def setUp(self):
        import importlib.machinery
        import importlib.util
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        vm_records.write(self.root, record("priv", 2, github_owners=["likt0r", "correctiv"]))
        os.makedirs(self.root + "/etc/agentbox/github")
        # out of order on purpose, with a comment and an odd line
        self.conf("APP_ID=10\n# personal\nAPP_ID=9\nAPP_ID=0x1\n")
        loader = importlib.machinery.SourceFileLoader("agentbox_gh_tokend", TOKEND)
        self.tokend = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.tokend)
        self.installed = {}         # App ID -> {repository: status of /installation, 200 or an error}
        self.gone = set()           # Apps whose installation is gone by the time the token is asked for
        self.broken = set()         # Apps whose key cannot sign
        self.asked = []             # (App ID, method, path)

    def conf(self, text):
        with open(self.root + "/etc/agentbox/github/app.conf", "w") as f:
            f.write(text)

    def urlopen(self, req, timeout=None):
        import io
        import urllib.error
        app = req.get_header("Authorization").removeprefix("Bearer jwt-")
        path = req.full_url.removeprefix("https://api.github.com")
        self.asked.append((app, req.get_method(), path))
        if path.endswith("/installation"):
            repo = "/".join(path.split("/")[2:4])
            status = self.installed.get(app, {}).get(repo, 404)
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "x", {}, None)
            return io.BytesIO(json.dumps({"id": int(app) * 10}).encode())
        if app in self.gone:
            raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)
        return io.BytesIO(json.dumps({"token": f"ghs_from_{app}", "expires_at": "2026-11-10T12:00:00Z"}).encode())

    def jwt(self, app_id):
        if app_id in self.broken:
            raise subprocess.CalledProcessError(1, ["openssl"])
        return f"jwt-{app_id}"

    def request(self, line, vm="priv"):
        import contextlib
        import io
        from unittest import mock
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(self.tokend, "REGISTRY", self.root + "/etc/agentbox/vms"), \
                mock.patch.object(self.tokend, "CONF_DIR", self.root + "/etc/agentbox/github"), \
                mock.patch.object(self.tokend, "app_jwt", self.jwt), \
                mock.patch.object(self.tokend.urllib.request, "urlopen", self.urlopen), \
                mock.patch.object(sys, "argv", ["agentbox-gh-tokend", vm]), \
                mock.patch.object(sys, "stdin", io.StringIO(line + "\n")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            self.tokend.main()
        return json.loads(out.getvalue()), err.getvalue()

    def test_repository_installed_only_on_the_second_app(self):
        self.installed = {"10": {"correctiv/site": 200}}
        answer, log = self.request("correctiv/site")
        self.assertEqual(answer, {"token": "ghs_from_10", "expires_at": "2026-11-10T12:00:00Z"})
        self.assertEqual(self.asked, [("9", "GET", "/repos/correctiv/site/installation"),
                                      ("10", "GET", "/repos/correctiv/site/installation"),
                                      ("10", "POST", "/app/installations/100/access_tokens")])
        self.assertEqual(log, "[priv] token for correctiv/site until 2026-11-10T12:00:00Z by App 10\n")

    def test_the_first_app_installed_there_issues_the_token(self):
        self.installed = {"9": {"likt0r/askos": 200}, "10": {"likt0r/askos": 200}}
        answer, log = self.request("likt0r/askos")
        self.assertEqual(answer["token"], "ghs_from_9")
        self.assertEqual([a for a, _, _ in self.asked], ["9", "9"])
        self.assertTrue(log.endswith(" by App 9\n"), log)

    def test_no_app_installed(self):
        for conf in ("APP_ID=10\nAPP_ID=9\n", "", None):
            with self.subTest(conf=conf):
                if conf is None:
                    os.remove(self.root + "/etc/agentbox/github/app.conf")
                else:
                    self.conf(conf)
                answer, log = self.request("likt0r/askos")
                self.assertEqual(answer, {"error": "no GitHub App is installed on likt0r/askos"})
                self.assertEqual(log, "[priv] refused: 'likt0r/askos': not-installed: no GitHub App is installed "
                                      "on likt0r/askos\n")

    def test_another_error_from_one_app_stops_before_the_next_is_asked(self):
        for status in (401, 403, 500, 502):
            with self.subTest(status=status):
                self.asked = []
                self.installed = {"9": {"likt0r/askos": status}, "10": {"likt0r/askos": 200}}
                answer, log = self.request("likt0r/askos")
                self.assertEqual(answer, {"error": f"GitHub answered {status} (GitHub App 9)"})
                self.assertEqual([a for a, _, _ in self.asked], ["9"])
                self.assertEqual(log, f"[priv] refused: 'likt0r/askos': github-status status={status} app_id=9: "
                                      f"GitHub answered {status} (GitHub App 9)\n")

    def test_an_app_whose_key_cannot_sign_stops_and_is_named(self):
        # app.conf names it, its app-<id>.pem is missing or broken: refused, the next App not asked
        self.broken = {"9"}
        self.installed = {"10": {"likt0r/askos": 200}}
        answer, log = self.request("likt0r/askos")
        message = "the key of GitHub App 9 cannot sign (missing or broken: agentbox set-github-app 9)"
        self.assertEqual(answer, {"error": message})
        self.assertEqual(log, f"[priv] refused: 'likt0r/askos': app-key app_id=9: {message}\n")
        self.assertEqual(self.asked, [])

    def test_the_real_signer_with_a_missing_key_is_refused_by_app_id(self):
        from unittest import mock
        with mock.patch.object(self.tokend, "CONF_DIR", self.root + "/etc/agentbox/github"):
            with self.assertRaises(subprocess.CalledProcessError):
                self.tokend.app_jwt("9")

    def test_an_installation_gone_before_the_token_names_the_repository(self):
        self.installed = {"9": {"likt0r/askos": 200}, "10": {"likt0r/askos": 200}}
        self.gone = {"9"}
        answer, log = self.request("likt0r/askos")
        self.assertEqual(answer, {"error": "GitHub App 9 is no longer installed on likt0r/askos"})
        self.assertEqual(log, "[priv] refused: 'likt0r/askos': not-installed app_id=9: GitHub App 9 is no longer "
                              "installed on likt0r/askos\n")
        self.assertEqual([a for a, _, _ in self.asked], ["9", "9"])

    def test_owner_check_comes_before_any_app_is_asked(self):
        self.installed = {"9": {"someone/repo": 200}}
        answer, _ = self.request("someone/repo")
        self.assertEqual(answer, {"error": "someone is not a GitHub owner of the VM priv"})
        self.assertEqual(self.asked, [])

    def test_activity_reads_the_app_that_issued_the_token(self):
        sys.path.insert(0, REPO)
        from agentbox import activity
        self.installed = {"10": {"correctiv/site": 200}}
        _, log = self.request("correctiv/site")
        entry = activity.github_entry({"__REALTIME_TIMESTAMP": "1793700000000000",
                                       "SYSLOG_IDENTIFIER": "agentbox-gh-priv", "MESSAGE": log.strip()})
        self.assertEqual((entry["code"], entry["params"]), ("github.token", {
            "repository": "correctiv/site", "expires": "2026-11-10T12:00:00Z", "app_id": "10"}))
        self.assertEqual(entry["description"], "GitHub token for correctiv/site until 2026-11-10T12:00:00Z by App 10")
        # a line from before there were several Apps
        entry = activity.github_entry({"__REALTIME_TIMESTAMP": "1793700000000000", "SYSLOG_IDENTIFIER": "agentbox-gh-priv",
                                       "MESSAGE": "[priv] token for likt0r/askos until 2026-11-10T12:00:00Z"})
        self.assertEqual(entry["params"], {"repository": "likt0r/askos", "expires": "2026-11-10T12:00:00Z"})
        # a refusal names the App it stopped at
        self.broken = {"9"}
        _, log = self.request("likt0r/askos")
        entry = activity.github_entry({"__REALTIME_TIMESTAMP": "1793700000000000",
                                       "SYSLOG_IDENTIFIER": "agentbox-gh-priv", "MESSAGE": log.strip()})
        self.assertEqual((entry["code"], entry["reason"], entry["params"]["app_id"]), ("github.refused", "app-key", "9"))


if __name__ == "__main__":
    unittest.main()
