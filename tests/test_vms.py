"""agentbox create and delete: VMs come and go through the registry.

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and fake
virsh, qemu-img, systemctl and virt-install on PATH (the harness of
test_registry.py), with work (network 1) and priv (network 2) in the registry
and a Build of the Base image.

Run from the repo root: python3 -m unittest discover -s tests
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import unittest
from xml.sax.saxutils import unescape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from test_registry import AGENTBOX, UNITS, RegistryTest  # noqa: E402
from vm_records import record  # noqa: E402

# VMs the tests create: libvirt does not know them beforehand
NEW = ("lab", "lab2", "lab3", "lab4", "lab5", "research-project")
ARCHIVED = re.compile(r"lab-data-\d{8}T\d{6}Z(-\d+)?\.qcow2")


class VmsTest(RegistryTest):
    def setUp(self):
        super().setUp()
        os.makedirs(self.path("var/lib/agentbox/images"))
        self.write("var/lib/agentbox/images/base-20261101.qcow2", "")
        os.symlink("base-20261101.qcow2", self.path("var/lib/agentbox/images/base.qcow2"))
        vm_records.write(self.root)

    def scenario(self, **s):
        vms = {name: {"defined": False} for name in NEW}
        vms.update(s.pop("vms", {}))
        super().scenario(vms=vms, **s)

    def ok(self, *args):
        code, out, err = self.run_agentbox(*args)
        self.assertEqual(code, 0, err)
        return out

    def refused(self, *args):
        """The command fails with this message and changes nothing."""
        before = self.files()
        calls = len(self.calls())
        code, out, err = self.run_agentbox(*args)
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.calls()[calls:], [])
        return err

    def files(self):
        return {k: v for k, v in self.snapshot().items() if "/locks/" not in k}

    def exists(self, rel):
        return os.path.lexists(self.path(rel))

    def archive(self):
        try:
            return sorted(os.listdir(self.path("var/lib/agentbox/archive")))
        except FileNotFoundError:
            return []

    def virsh(self, *cmds):
        return [c for c in self.calls() if c["cmd"] in cmds]

    def systemctl(self):
        return [c["argv"] for c in self.calls("systemctl")]

    def store(self, *entries):
        """Write the Allowlist (allowlist.json) with these entries (domain, scope, expires, note)."""
        self.write("etc/agentbox/allowlist.json", json.dumps({"entries": [
            {"domain": d, "scope": s, "expires": e, "note": n, "added": 1793700000, "added_by": "bewr"}
            for d, s, e, n in entries]}))

    def entries(self, scope):
        """(domain, expires, note) of the scope's entries in allowlist.json."""
        with open(self.path("etc/agentbox/allowlist.json")) as f:
            return sorted((e["domain"], e["expires"], e["note"]) for e in json.load(f)["entries"] if e["scope"] == scope)


class CreateTest(VmsTest):
    def test_new_vm_gets_the_next_network_and_its_orca_port(self):
        vm_records.write_host_setup(self.root)    # Network mode: the Orca port listens on every address
        self.ok("create", "lab")
        rec = vm_records.read(self.root, "lab")
        self.assertRegex(rec.pop("created"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(rec, {"name": "lab", "network": 3, "orca_port": 6803, "image": "base", "build": None,
                               "memory_mib": 26624, "data_disk_gib": 300, "github_owners": []})
        self.assertIn("ListenStream=6803\n", self.read("etc/systemd/system/agentbox-gui-lab.socket"))
        self.assertIn("ListenStream=10.77.3.1:443\n", self.read("etc/systemd/system/agentbox-proxy-lab.socket"))
        self.assertIn('    iifname "abox-lab" ip daddr != 10.77.3.1 drop\n', self.read("etc/agentbox/vm-input.nft"))

    def test_new_vm_is_configured_through_regenerate(self):
        self.ok("create", "lab")
        for unit in UNITS:
            self.assertTrue(self.exists(f"etc/systemd/system/{unit.format('lab')}"), unit)
        self.assertEqual([c["net"] for c in self.virsh("net-define")], ["agentbox-work", "agentbox-priv", "agentbox-lab"])
        self.assertIn("agentbox-lab", [c["dom"] for c in self.virsh("define")])
        self.assertIn(["enable", "--now", "agentbox-proxy-work.socket", "agentbox-ghvm-work.socket",
                       "agentbox-gui-work.socket", "agentbox-proxy-priv.socket", "agentbox-ghvm-priv.socket",
                       "agentbox-gui-priv.socket", "agentbox-proxy-lab.socket", "agentbox-ghvm-lab.socket",
                       "agentbox-gui-lab.socket"], self.systemctl())
        self.assertEqual(self.read("etc/agentbox/secrets/claude-lab"), "none\n")
        self.assertTrue(os.path.isdir(self.path("var/lib/agentbox/observer-lab")))

    def test_new_vm_has_disks_but_is_not_started(self):
        """The first `agentbox pair` or Orca connection starts it."""
        self.ok("create", "lab")
        creates = [c["argv"] for c in self.calls("qemu-img") if c["argv"][0] == "create"]
        system = self.path("var/lib/agentbox/vms/lab-system.qcow2")
        self.assertIn(["create", "-q", "-f", "qcow2", "-F", "qcow2", "-b",
                       self.path("var/lib/agentbox/images/base-20261101.qcow2"), system], creates)
        self.assertIn(["create", "-q", "-f", "qcow2", self.path("var/lib/agentbox/vms/lab-data.qcow2"), "300G"],
                      creates)
        self.assertEqual(self.virsh("start"), [])
        self.assertEqual(json.loads(self.ok("status", "--json"))["lab"]["state"], "shut off")

    def test_text_output(self):
        out = self.ok("create", "lab").splitlines()
        self.assertEqual(out[0], "lab: entered into the registry (network 3, Orca port 6803)")
        self.assertIn("lab: fresh System disk (base-20261101.qcow2)", out)
        self.assertIn("lab: VM defined again (applies from the next start)", out)
        self.assertEqual(out[-1], "lab created. Pair with: agentbox pair lab (starts the VM)")

    def test_json(self):
        result = self.json("create", "lab", "--owner", "likt0r")
        message = result.pop("message")
        self.assertEqual(result, {"vm": "lab", "network": 3, "orca_port": 6803, "image": "base", "memory_mib": 26624,
                                  "data_disk_gib": 300, "github_owners": ["likt0r"], "allowlist_from": None})
        self.assertTrue(message.startswith("lab: entered into the registry"))

    def test_options(self):
        self.ok("create", "lab", "--image", "base", "--memory-mib", "8192", "--data-disk-gib=50",
                "--owner", "likt0r", "--owner=acme-corp", "--owner", "likt0r")
        rec = vm_records.read(self.root, "lab")
        self.assertEqual((rec["image"], rec["memory_mib"], rec["data_disk_gib"], rec["github_owners"]),
                         ("base", 8192, 50, ["likt0r", "acme-corp"]))
        argv = [c["argv"] for c in self.calls("virt-install") if "agentbox-lab" in c["argv"]][-1]
        self.assertEqual(argv[argv.index("--memory") + 1], "8192")
        self.assertIn(["create", "-q", "-f", "qcow2", self.path("var/lib/agentbox/vms/lab-data.qcow2"), "50G"],
                      [c["argv"] for c in self.calls("qemu-img")])

    def test_networks_fill_gaps_first(self):
        vm_records.write(self.root, record("work", 1), record("lab3", 3), record("lab5", 5))
        os.remove(self.path("etc/agentbox/vms/priv.json"))
        self.assertEqual(self.json("create", "lab")["network"], 2)
        self.assertEqual(self.json("create", "lab2")["network"], 4)
        self.assertEqual(self.json("create", "lab4")["orca_port"], 6806)

    def test_no_free_network_left(self):
        for vm in ("work", "priv"):
            os.remove(self.path(f"etc/agentbox/vms/{vm}.json"))
        vm_records.write(self.root, *(record(f"vm{n}", n) for n in range(1, 255)))
        err = self.refused("create", "lab")
        self.assertEqual(err, "agentbox: no free network left (at most 254 VMs)\n")

    def test_concurrent_creates_never_share_a_network(self):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        procs = [subprocess.Popen([AGENTBOX, "create", name, "--json"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env) for name in ("lab", "lab2", "lab3", "lab4")]
        results = []
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
            results.append(json.loads(out))
        self.assertEqual(sorted(r["network"] for r in results), [3, 4, 5, 6])
        self.assertEqual(sorted(r["orca_port"] for r in results), [6803, 6804, 6805, 6806])
        self.assertEqual(self.registry(), ["lab.json", "lab2.json", "lab3.json", "lab4.json", "priv.json", "work.json"])

    def test_names_follow_the_pattern(self):
        for name in ("a", "Lab", "1lab", "lab_x", "lab.x", "-lab", "a" * 17, "../lab", "lab/x", "lä"):
            with self.subTest(name=name):
                err = self.refused("create", name)
                self.assertTrue(err.startswith("agentbox: invalid name: "), err)
        self.ok("create", "research-project")       # 16 characters
        self.assertEqual(vm_records.read(self.root, "research-project")["network"], 3)

    def test_reserved_names_are_refused(self):
        for name in ("off", "all", "new", "status", "login", "logout", "settings", "activity", "v1", "v2"):
            with self.subTest(name=name):
                self.assertEqual(self.refused("create", name), f"agentbox: the name {name} is reserved\n")

    def test_existing_name_is_refused(self):
        self.assertEqual(self.refused("create", "priv"), "agentbox: priv exists already\n")
        self.ok("create", "lab")
        self.assertEqual(self.refused("create", "lab"), "agentbox: lab exists already\n")

    def test_there_is_no_rename(self):
        self.ok("create", "lab")
        code, _, err = self.run_agentbox("rename", "lab", "lab2")
        self.assertEqual(code, 1)
        self.assertIn("agentbox create", err)
        self.assertNotIn("rename", err)
        self.assertEqual(self.registry(), ["lab.json", "priv.json", "work.json"])

    def test_copies_the_source_vms_own_entries_once(self):
        """Temporary entries keep their expiry and notes are copied; expired entries,
        entries of other VMs and those for all VMs (which apply anyway) are not."""
        later = int(time.time()) + 3600
        self.store(("docs.rs", "priv", None, "Rust docs"), ("pypi.org", "priv", later, ""),
                   ("old.example", "priv", 1000, ""), ("crates.io", "work", None, ""), ("github.com", "all", None, ""))
        result = self.json("create", "lab", "--copy-allowlist-from", "priv")
        self.assertEqual(result["allowlist_from"], "priv")
        self.assertIn("lab: 2 Allowlist entries copied from priv", result["message"])
        self.assertEqual(self.entries("lab"), [("docs.rs", None, "Rust docs"), ("pypi.org", later, "")])
        with open(self.path("etc/agentbox/allowlist.json")) as f:
            copied = [e for e in json.load(f)["entries"] if e["scope"] == "lab"]
        self.assertAlmostEqual(copied[0]["added"], time.time(), delta=60)
        self.assertEqual(self.mode("etc/agentbox/allowlist.json"), 0o644)
        # what applies to lab: the entries for all VMs and its own copies
        self.assertEqual([(e["domain"], e["scope"]) for e in json.loads(self.ok("allowlist", "lab", "--json"))],
                         [("github.com", "all"), ("docs.rs", "lab"), ("pypi.org", "lab")])

    def test_copies_from_the_files_before_the_migration(self):
        self.write("etc/agentbox/allowlist-priv",
                   "# Freigaben nur fuer priv (agentbox allow/disallow)\ndocs.rs\n\n# Notiz\npypi.org\n")
        self.assertIn("lab: 2 Allowlist entries copied from priv", self.json("create", "lab", "--copy-allowlist-from",
                                                                          "priv")["message"])
        self.assertEqual(self.entries("lab"), [("docs.rs", None, ""), ("pypi.org", None, "")])
        self.assertEqual(self.entries("priv"), [("docs.rs", None, ""), ("pypi.org", None, "")])
        self.assertFalse(self.exists("etc/agentbox/allowlist-priv"))

    def test_without_copy_nothing_of_an_earlier_vm_of_the_name_is_left(self):
        self.store(("evil.example", "lab", None, ""), ("docs.rs", "priv", None, ""))
        self.write("etc/agentbox/allowlist-lab", "evil.example\n")
        self.write("etc/agentbox/open-lab", "9999999999\n")
        self.ok("create", "lab")
        self.assertEqual(self.entries("lab"), [])
        self.assertEqual(self.entries("priv"), [("docs.rs", None, "")])
        self.assertFalse(self.exists("etc/agentbox/allowlist-lab"))
        self.assertFalse(self.exists("etc/agentbox/open-lab"))

    def test_copy_from_unknown_vm_is_refused(self):
        for source in ("nope", "lab", "../priv"):
            with self.subTest(source=source):
                self.assertEqual(self.refused("create", "lab", "--copy-allowlist-from", source),
                                 f"agentbox: No such VM: {source}\n")

    def test_invalid_options_are_refused(self):
        cases = {
            ("--memory-mib", "abc"): "--memory-mib must be a whole number from 1024 to",
            ("--memory-mib", "512"): "--memory-mib must be a whole number from 1024 to",
            ("--memory-mib", "-1"): "--memory-mib must be a whole number from 1024 to",
            ("--memory-mib", "99999999999"): "--memory-mib must be a whole number from 1024 to",
            ("--data-disk-gib", "0"): "--data-disk-gib must be a whole number from 1 to 16384",
            ("--data-disk-gib", "16385"): "--data-disk-gib must be a whole number from 1 to 16384",
            ("--owner", "two words"): "invalid GitHub name: two words",
            ("--owner", "-"*40): "invalid GitHub name",
            ("--image", "fancy"): "No such Image: fancy (available: base)",
            ("--image", "base", "--image", "base"): "--image given twice",
            ("--image",): "--image needs a value",
            ("--json=yes",): "--json takes no value",
            ("--start",): "unknown option: --start",
            ("lab2",): "create <vm>",
        }
        for extra, message in cases.items():
            with self.subTest(args=extra):
                self.assertIn(message, self.refused("create", "lab", *extra))
        self.assertIn("create <vm>", self.refused("create"))

    def test_memory_limit_is_at_most_the_hosts_memory(self):
        os.makedirs(self.path("proc"))
        self.write("proc/meminfo", "MemTotal:       16777216 kB\nMemFree:         1000000 kB\n")
        self.assertEqual(self.refused("create", "lab"),
                         "agentbox: --memory-mib must be a whole number from 1024 to 16384\n")
        self.ok("create", "lab", "--memory-mib", "16384")

    def test_image_must_exist(self):
        os.remove(self.path("var/lib/agentbox/images/base.qcow2"))
        self.assertEqual(self.refused("create", "lab"), "agentbox: no Image yet, first agentbox build base\n")

    def test_leftovers_of_an_earlier_vm_are_not_taken_over(self):
        self.write("var/lib/agentbox/vms/lab-data.qcow2", "{}")
        self.write("etc/agentbox/secrets/claude-lab", "sk-ant-old\n")
        self.assertEqual(self.refused("create", "lab"),
                         "agentbox: lab: leftovers of an earlier VM exist (Data disk, Claude token), remove them first\n")
        os.remove(self.path("var/lib/agentbox/vms/lab-data.qcow2"))
        os.remove(self.path("etc/agentbox/secrets/claude-lab"))
        # each file alone stops it, and is named (a directory ends in /)
        for what, rel in (("Data disk", "var/lib/agentbox/vms/lab-data.qcow2"),
                          ("System disk", "var/lib/agentbox/vms/lab-system.qcow2"),
                          ("Claude token", "etc/agentbox/secrets/claude-lab"),
                          ("Observer", "var/lib/agentbox/observer-lab/"),
                          ("units", "etc/systemd/system/agentbox-proxy-lab.socket")):
            with self.subTest(what=what):
                if rel.endswith("/"):
                    os.makedirs(self.path(rel))
                else:
                    self.write(rel, "{}")
                self.assertEqual(self.refused("create", "lab"),
                                 f"agentbox: lab: leftovers of an earlier VM exist ({what}), remove them first\n")
                (os.rmdir if rel.endswith("/") else os.remove)(self.path(rel))
        self.scenario(vms={"lab": {"state": "shut off"}},
                      networks={"agentbox-lab": {"active": False, "autostart": False, "xml": "<network/>"}})
        self.assertEqual(self.refused("create", "lab"), "agentbox: lab: leftovers of an earlier VM exist "
                                                        "(VM agentbox-lab, network agentbox-lab), remove them first\n")

    def test_broken_registry_stops_create(self):
        self.write("etc/agentbox/vms/lab2.json", '{"name": "lab2"}')
        err = self.refused("create", "lab")
        self.assertIn("lab2.json: broken record", err)

    def test_failing_configuration_says_how_to_finish(self):
        self.scenario(virt_install_fails=["agentbox-lab"])
        code, _, err = self.run_agentbox("create", "lab")
        self.assertEqual(code, 1)
        self.assertIn("lab: virt-install fails", err)
        self.assertIn("agentbox: lab is registered, but not completely set up: agentbox regenerate finishes it",
                      err)
        # with --json the same lines, without their "agentbox: "
        self.scenario(virt_install_fails=["agentbox-lab", "agentbox-lab2"])
        code, out, err = self.run_agentbox("create", "lab2", "--json")
        self.assertEqual((code, out), (1, ""))
        said = json.loads(err)
        self.assertEqual((said["error"], said["params"]["vm"]), ("action_failed", "lab2"))
        self.assertIn("virt-install fails", said["message"])
        self.assertIn("\nlab2 is registered, but not completely set up: agentbox regenerate finishes it",
                      said["message"])
        self.assertNotIn("agentbox: ", said["message"])
        self.scenario()
        self.assertEqual(self.json("regenerate")["vms"]["lab"], ["domain"])


class DeleteTest(VmsTest):
    def setUp(self):
        super().setUp()
        self.store(("docs.rs", "priv", None, ""), ("github.com", "all", None, ""))
        self.ok("create", "lab", "--copy-allowlist-from", "priv")
        self.write("var/lib/agentbox/vms/lab-data.qcow2", '{"repos": "unpushed work"}')
        self.forget_calls()

    def others(self):
        """Every file that belongs to work, priv or all VMs (the Allowlist: test_allowlist_entries_go)."""
        return {k: v for k, v in self.files().items()
                if "lab" not in k and not k.endswith(("vm-input.nft", "allowlist.json")) and "/pending/" not in k}

    def test_allowlist_entries_go_with_the_vm(self):
        self.assertEqual(self.entries("lab"), [("docs.rs", None, "")])
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertEqual(self.entries("lab"), [])
        self.assertEqual(self.entries("priv"), [("docs.rs", None, "")])
        self.assertEqual(self.entries("all"), [("github.com", None, "")])

    def test_seen_alerts_go_with_the_vm(self):
        self.write("var/lib/agentbox/alerts-seen-lab.json", '{"*": 1761732000}\n')
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertFalse(self.exists("var/lib/agentbox/alerts-seen-lab.json"))

    def test_failed_wakes_go_with_the_vm(self):
        self.write("var/lib/agentbox/wakes-lab.json", '{"start": null, "count": 3}\n')
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertFalse(self.exists("var/lib/agentbox/wakes-lab.json"))

    def test_refused_without_the_matching_name(self):
        for extra in ((), ("--confirm", "priv"), ("--confirm", "LAB"), ("--confirm=lab2",), ("--confirm", "")):
            with self.subTest(args=extra):
                self.assertEqual(self.refused("delete", "lab", *extra), "agentbox: deleting only with --confirm lab\n")
        self.assertIn("--confirm needs a value", self.refused("delete", "lab", "--confirm"))
        self.assertIn("unknown option: --force", self.refused("delete", "lab", "--confirm", "lab", "--force"))
        self.assertTrue(self.exists("etc/agentbox/vms/lab.json"))

    def test_unknown_vm(self):
        self.assertEqual(self.refused("delete", "nope", "--confirm", "nope"), "agentbox: No such VM: nope\n")
        self.assertEqual(self.refused("delete", "../lab", "--confirm", "../lab"), "agentbox: No such VM: ../lab\n")

    def test_waits_for_a_reset_or_wake_of_the_vm(self):
        # #52: no wake starts the VM between its stop and its undefine
        lock = self.path("var/lib/agentbox/locks/lifecycle-lab.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        p = subprocess.Popen([AGENTBOX, "delete", "lab", "--confirm", "lab"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
        try:
            time.sleep(1)
            self.assertIsNone(p.poll())
            self.assertEqual(self.virsh("shutdown", "undefine"), [])
        finally:
            os.close(fd)
        _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual([c["dom"] for c in self.virsh("undefine")], ["agentbox-lab"])

    def test_takes_down_everything_and_archives_the_data_disk(self):
        others = self.others()
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertEqual(self.registry(), ["priv.json", "work.json"])
        self.assertEqual([k for k in self.files() if "lab" in k and "/archive/" not in k], [])
        self.assertEqual(len(self.archive()), 1)
        self.assertRegex(self.archive()[0], ARCHIVED)
        self.assertEqual(self.read(f"var/lib/agentbox/archive/{self.archive()[0]}"), '{"repos": "unpushed work"}')
        self.assertEqual(self.mode("var/lib/agentbox/archive"), 0o700)
        self.assertNotIn("abox-lab", self.read("etc/agentbox/vm-input.nft"))
        self.assertEqual(self.others(), others)
        self.assertEqual([(c["cmd"], c.get("dom") or c.get("net")) for c in self.virsh("undefine", "net-destroy",
                                                                                       "net-undefine")],
                         [("undefine", "agentbox-lab"), ("net-destroy", "agentbox-lab"), ("net-undefine", "agentbox-lab")])
        self.assertEqual(self.virsh("undefine")[0]["flags"], ["--managed-save"])
        self.assertEqual(self.systemctl()[:3], [
            ["disable", "--now", "agentbox-proxy-lab.socket", "agentbox-ghvm-lab.socket", "agentbox-gui-lab.socket"],
            ["stop", "agentbox-gui-lab@*.service", "agentbox-ghvm-lab@*.service"],
            ["daemon-reload"]])
        self.assertIn(["restart", "agentbox-vmnet.service"], self.systemctl())
        self.assertNotIn("lab", json.loads(self.ok("status", "--json")))

    def test_text_output(self):
        out = self.ok("delete", "lab", "--confirm", "lab").splitlines()
        self.assertEqual(out[:5], ["lab: removed from the registry", "lab: Orca port, proxy and token service turned off",
                                   "lab: VM stopped and removed", "lab: network agentbox-lab removed",
                                   "lab: System disk deleted"])
        self.assertRegex(out[5], r"^lab: Data disk archived: .*/var/lib/agentbox/archive/lab-data-")
        self.assertEqual(out[6:], ["lab: Claude token, Allowlist entries and Observer removed", "network rules reloaded",
                                   "lab deleted"])

    def test_json(self):
        result = self.json("delete", "lab", "--confirm", "lab")
        self.assertTrue(result.pop("message").endswith("lab deleted"))
        self.assertEqual(result, {"vm": "lab", "data_disk": "archived", "resumed": False,
                                  "archive": self.path(f"var/lib/agentbox/archive/{self.archive()[0]}")})

    def test_data_disk_is_deleted_only_on_request(self):
        result = self.json("delete", "lab", "--confirm=lab", "--delete-data-disk")
        self.assertEqual((result["data_disk"], result["archive"]), ("deleted", None))
        self.assertFalse(self.exists("var/lib/agentbox/vms/lab-data.qcow2"))
        self.assertEqual(self.archive(), [])

    def test_running_vm_is_shut_down_first(self):
        self.ok("start", "lab")
        self.forget_calls()
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertEqual([c["cmd"] for c in self.virsh("shutdown", "destroy", "undefine")], ["shutdown", "undefine"])

    def test_paused_vm_loses_its_saved_memory(self):
        self.ok("start", "lab")
        self.ok("pause", "lab")
        self.forget_calls()
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertEqual([c["cmd"] for c in self.virsh("managedsave-remove", "undefine")],
                         ["managedsave-remove", "undefine"])
        code, _, err = self.run_agentbox("start", "lab")
        self.assertEqual((code, err), (1, "agentbox: No such VM: lab\n"))

    def test_name_can_be_used_again_and_starts_fresh(self):
        self.ok("delete", "lab", "--confirm", "lab")
        self.ok("create", "lab")
        self.assertEqual(vm_records.read(self.root, "lab")["network"], 3)
        self.assertEqual(self.entries("lab"), [])
        self.assertEqual(self.read("etc/agentbox/secrets/claude-lab"), "none\n")
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertEqual(len(self.archive()), 2)
        for name in self.archive():
            self.assertRegex(name, ARCHIVED)

    def test_interrupted_delete_is_finished_by_the_next(self):
        self.scenario(systemctl_fails=["daemon-reload"])
        code, _, err = self.run_agentbox("delete", "lab", "--confirm", "lab")
        self.assertEqual(code, 1, err)
        self.scenario()
        # gone from the registry, but its name and network stay taken until the delete is finished
        self.assertNotIn("lab", json.loads(self.ok("status", "--json")))
        self.assertEqual(self.refused("create", "lab"),
                         "agentbox: lab is being deleted, first finish agentbox delete lab --confirm lab\n")
        self.assertEqual(self.json("create", "lab2")["network"], 4)
        # its network still exists, so regenerate keeps it isolated
        self.ok("regenerate")
        self.assertIn('iifname "abox-lab" ip daddr != 10.77.3.1 drop', self.read("etc/agentbox/vm-input.nft"))
        self.assertTrue(self.exists("var/lib/agentbox/vms/lab-data.qcow2"))
        self.forget_calls()
        result = self.json("delete", "lab", "--confirm", "lab")
        self.assertTrue(result["resumed"])
        self.assertTrue(result["message"].startswith("lab: resuming the delete\n"))
        self.assertEqual(result["data_disk"], "archived")
        self.assertEqual([c["cmd"] for c in self.virsh("undefine", "net-undefine")], ["undefine", "net-undefine"])
        self.assertEqual([k for k in self.files() if "lab" in k and "/archive/" not in k and "lab2" not in k], [])
        self.assertNotIn("abox-lab\"", self.read("etc/agentbox/vm-input.nft"))
        self.assertIn("abox-lab2", self.read("etc/agentbox/vm-input.nft"))
        self.ok("create", "lab")

    def test_interrupted_after_archiving_reports_the_same_archive(self):
        self.scenario(systemctl_fails=["restart"])
        self.assertEqual(self.run_agentbox("delete", "lab", "--confirm", "lab")[0], 1)
        archived = self.archive()
        self.assertEqual(len(archived), 1)
        self.scenario()
        result = self.json("delete", "lab", "--confirm", "lab", "--delete-data-disk")
        self.assertEqual((result["data_disk"], result["archive"]),
                         ("archived", self.path(f"var/lib/agentbox/archive/{archived[0]}")))
        self.assertEqual(self.archive(), archived)
        self.assertNotIn("abox-lab", self.read("etc/agentbox/vm-input.nft"))

    def test_nothing_left_to_resume(self):
        self.ok("delete", "lab", "--confirm", "lab")
        self.assertEqual(self.refused("delete", "lab", "--confirm", "lab"), "agentbox: No such VM: lab\n")


class SetMemoryTest(VmsTest):
    """set-memory changes the record and the libvirt definition. A running VM keeps its
    memory until its next Restart; a paused one is refused, its saved memory belongs to
    the old definition."""

    def setUp(self):
        super().setUp()
        self.ok("regenerate")
        self.forget_calls()

    def defined_memory(self, vm):
        """--memory of the VM's last definition (fake virt-install puts its arguments into the XML)."""
        xml = [c["xml"] for c in self.virsh("define") if c["dom"] == f"agentbox-{vm}"][-1]
        args = json.loads(unescape(re.search(r"<args>(.*)</args>", xml).group(1)))
        return args[args.index("--memory") + 1]

    def turned_off(self, vm):
        """The VM is shut off (the fake's state after regenerate's define is forgotten)."""
        os.remove(os.path.join(self.dir, "domains.json"))
        self.scenario(vms={vm: {"state": "shut off"}})

    def test_running_vm_keeps_running_and_gets_the_limit_at_its_next_restart(self):
        result = self.json("set-memory", "work", "16384")
        self.assertEqual(result.pop("message"), "work: memory limit 26624 -> 16384 MiB, applies from the next "
                                                "Restart (agentbox restart work) or Reset; until then the "
                                                "running VM keeps its memory")
        self.assertEqual(result, {"vm": "work", "memory_mib": 16384, "previous_mib": 26624, "changed": True,
                                  "state": "running", "applies": "next_restart"})
        self.assertEqual(vm_records.read(self.root, "work")["memory_mib"], 16384)
        self.assertEqual([c["dom"] for c in self.virsh("define")], ["agentbox-work"])
        self.assertEqual(self.defined_memory("work"), "16384")
        # nothing stops, starts or pauses it now
        self.assertEqual(self.virsh("shutdown", "destroy", "start", "managedsave", "managedsave-remove"), [])
        self.assertEqual(vm_records.read(self.root, "priv")["memory_mib"], 26624)
        # the definition matches the record: regenerate has nothing left to do
        self.assertEqual(self.json("regenerate")["vms"], {"work": [], "priv": []})

    def test_text_output(self):
        self.assertEqual(self.ok("set-memory", "work", "32768"),
                         "work: memory limit 26624 -> 32768 MiB, applies from the next Restart "
                         "(agentbox restart work) or Reset; until then the running VM keeps its memory\n")

    def test_vm_that_is_off_gets_it_at_its_next_start(self):
        self.turned_off("priv")
        result = self.json("set-memory", "priv", "8192")
        self.assertEqual((result["state"], result["applies"], result["message"]),
                         ("shut off", "next_start",
                          "priv: memory limit 26624 -> 8192 MiB, applies from the next start"))
        self.assertEqual(self.defined_memory("priv"), "8192")
        self.assertEqual(self.virsh("start"), [])

    def test_same_limit_changes_nothing(self):
        self.assertEqual(self.ok("set-memory", "work", "26624"), "work: memory limit stays 26624 MiB\n")
        result = self.json("set-memory", "work", "26624")
        self.assertEqual((result["changed"], result["applies"]), (False, None))
        self.assertEqual(self.virsh("define"), [])

    def test_paused_vm_is_refused_and_keeps_its_saved_memory(self):
        self.ok("pause", "work")
        self.forget_calls()
        self.assertEqual(self.refused("set-memory", "work", "16384"),
                         "agentbox: work is paused, its saved memory belongs to the previous memory limit: "
                         "first resume it (agentbox start work), then try again\n")
        self.assertEqual(vm_records.read(self.root, "work")["memory_mib"], 26624)
        # resumed, it can be changed, for its next Restart
        self.ok("start", "work")
        self.assertEqual(self.json("set-memory", "work", "16384")["applies"], "next_restart")

    def test_limits_are_those_of_create(self):
        os.makedirs(self.path("proc"))
        self.write("proc/meminfo", "MemTotal:       16777216 kB\nMemFree:         1000000 kB\n")
        for value in ("1023", "0", "16385", "99999999999", "abc", "-1", "8192.0", "1e4", " 8192", ""):
            with self.subTest(value=value):
                self.assertEqual(self.refused("set-memory", "work", value),
                                 "agentbox: memory limit (MiB) must be a whole number from 1024 to 16384\n")
        self.ok("set-memory", "work", "16384")
        self.ok("set-memory", "priv", "1024")
        self.assertEqual([vm_records.read(self.root, vm)["memory_mib"] for vm in ("work", "priv")], [16384, 1024])

    def test_usage(self):
        cases = {(): "agentbox: set-memory <vm> <mib> [--json]\n",
                 ("work",): "agentbox: set-memory <vm> <mib> [--json]\n",
                 ("work", "8192", "9000"): "agentbox: set-memory <vm> <mib> [--json]\n",
                 ("work", "8192", "--force"): "agentbox: unknown option: --force\n",
                 ("work", "8192", "--json=yes"): "agentbox: --json takes no value\n",
                 ("nope", "8192"): "agentbox: No such VM: nope\n",
                 ("../work", "8192"): "agentbox: No such VM: ../work\n"}
        for args, message in cases.items():
            with self.subTest(args=args):
                self.assertEqual(self.refused("set-memory", *args), message)

    def test_failing_definition_keeps_the_record_and_says_how_to_finish(self):
        self.scenario(virt_install_fails=["agentbox-work"])
        code, out, err = self.run_agentbox("set-memory", "work", "16384")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("agentbox: work: memory limit 16384 MiB saved, but the VM was not defined again: "
                      "work: virt-install fails", err)
        self.assertTrue(err.endswith("; agentbox regenerate finishes it\n"), err)
        self.assertEqual(vm_records.read(self.root, "work")["memory_mib"], 16384)
        self.scenario()
        self.assertEqual(self.json("regenerate")["vms"]["work"], ["domain"])
        self.assertEqual(self.defined_memory("work"), "16384")

    def test_waits_for_a_reset_or_pause_of_the_vm(self):
        # no Pause saves the VM's memory between the check and the new definition
        lock = self.path("var/lib/agentbox/locks/lifecycle-work.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        p = subprocess.Popen([AGENTBOX, "set-memory", "work", "16384"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
        try:
            time.sleep(1)
            self.assertIsNone(p.poll())
            self.assertEqual(self.virsh("define"), [])
        finally:
            os.close(fd)
        _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual([c["dom"] for c in self.virsh("define")], ["agentbox-work"])


if __name__ == "__main__":
    unittest.main()
