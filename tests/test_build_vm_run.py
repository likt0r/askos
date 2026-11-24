"""The first build VM of a Build (#144, ADR 0025, agentbox/build_vm_run.py): `podman build` in a
throwaway VM behind the internal switch AGENTBOX_BUILD_VM=1, its image archive in an output
directory, its log in the Build's journal.

The real CLI runs build-run as test_images.py does, with fakes on PATH as well for what a
build VM needs: systemd-run (virtiofsd's unit, fake_systemd_run.py), xorrisofs
(fake_xorrisofs.py), nft (the kernel's ruleset, fake_nft.py: by default KERNEL, a real nft's
listing of the rules agentbox regenerate writes), mkfs.ext4, mount and umount of the output
filesystem (fake_mount.py: the output directory stays a plain directory) and virsh's `create`,
which starts
fake_build_vm.py as the VM: it runs the real driver from the seed (vm/build-vm/build-vm-run of the Release) in a root of its own, with
the fake podman as the VM's podman, and its log reaches the Host through the log channel's
socket as QEMU's would. No test boots a VM.

Run from the repo root: python3 -m unittest discover -s tests
"""
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_images import FEDORA_REF, ImagesTest  # noqa: E402
from test_recipes import RecipeTest, archive, file  # noqa: E402
from test_registry import AGENTBOX, REPO, fake  # noqa: E402

sys.path.insert(0, REPO)
from agentbox import build_vm_run, generate  # noqa: E402

BUILD_VM = "var/lib/agentbox/build-vm"
IMAGE_44 = "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
RUN = "run/agentbox/build-vm"
# `nft list chain inet agentbox_vm <chain>` of a real nft 1.1 with generate.nft_rules([]) loaded
# (in a user and network namespace of its own): its priority words, its merged ranges
KERNEL = {
    "input": "table inet agentbox_vm {\n\tchain input {\n"
             "\t\ttype filter hook input priority filter - 5; policy accept;\n"
             '\t\tiifname "abox-*" jump from_vm\n\t\tiifname "aboxbuild" jump from_build\n\t}\n}\n',
    "from_build": "table inet agentbox_vm {\n\tchain from_build {\n\t\tmeta nfproto ipv6 drop\n"
                  "\t\tip daddr { 10.77.0.1, 255.255.255.255 } udp dport 67 accept\n"
                  "\t\tip daddr 10.77.0.1 udp dport 53 accept\n"
                  "\t\tip daddr 10.77.0.1 tcp dport 53 accept\n\t\tdrop\n\t}\n}\n",
    "forward": "table inet agentbox_vm {\n\tchain forward {\n"
               "\t\ttype filter hook forward priority filter - 5; policy accept;\n"
               '\t\tiifname "aboxbuild" meta nfproto ipv6 drop\n'
               '\t\tiifname "aboxbuild" ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, '
               '169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/3 } drop\n'
               '\t\toifname "aboxbuild" ct state established,related accept\n\t\toifname "aboxbuild" drop\n'
               "\t}\n}\n",
}


class BuildVmTest(ImagesTest):
    """A Host that can run a build VM: KVM, the checked image, 16 GiB available, the VM network
    rules loaded; the switch on."""

    def setUp(self):
        self.env = {"AGENTBOX_BUILD_VM": "1", "AGENTBOX_BUILD_VM_MIN_FREE_BYTES": "0",
                    "AGENTBOX_BUILD_VM_OUT_BYTES": str(1 << 30),
                    "AGENTBOX_BUILD_VM_WATCH_SECONDS": "1", "AGENTBOX_BUILD_VM_END_SECONDS": "10"}
        super().setUp()
        fake(os.path.join(self.dir, "bin", "systemd-run"), "fake_systemd_run.py")
        fake(os.path.join(self.dir, "bin", "xorrisofs"), "fake_xorrisofs.py")
        fake(os.path.join(self.dir, "bin", "nft"), "fake_nft.py")
        for program in ("mkfs.ext4", "mount", "umount"):
            with open(os.path.join(self.dir, "bin", program), "w") as f:
                f.write(f'#!/bin/sh\nexec python3 "{os.path.join(REPO, "tests", "fake_mount.py")}" {program} "$@"\n')
            os.chmod(os.path.join(self.dir, "bin", program), 0o755)
        self.write(f"{BUILD_VM}/image.json", json.dumps({"name": IMAGE_44, "release": "44", "sha256": "0" * 64,
                                                         "checked": 1}))
        self.write(f"{BUILD_VM}/images/{IMAGE_44}", "{}")
        self.write("dev/kvm", "")
        self.write("proc/meminfo", "MemTotal:       33554432 kB\nMemAvailable:   16777216 kB\n")
        self.write("etc/agentbox/vm-input.nft", generate.nft_rules([]))

    def scenario(self, **s):
        s.setdefault("systemctl_active", ["agentbox-vmnet.service"])
        s.setdefault("nft_chains", KERNEL)
        super().scenario(**s)

    def environment(self):
        return {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
                "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC", **self.env}

    def run_agentbox(self, *args):
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=self.environment(), timeout=120)
        return r.returncode, r.stdout, r.stderr

    def build_in_background(self, target="base"):
        p = subprocess.Popen([AGENTBOX, "build-run", target], env=self.environment(), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def in_vm(self, target="base"):
        """A Build whose podman build ran in the build VM: (its record, the output)."""
        code, out = self.build(target)
        self.assertNotEqual(code, 0, out)
        return self.newest(target), out

    def ident(self, image="base", build=None):
        return f"{image}-{build or self.newest(image)['build']}-build"

    def guest(self, image="base", build=None):
        """The fake VM's own root (fake_build_vm.py)."""
        return os.path.join(self.dir, "guest-buildvm-" + self.ident(image, build))

    def guest_podman(self, image="base", build=None, verb=None):
        try:
            with open(os.path.join(self.guest(image, build), "calls.jsonl")) as f:
                calls = [json.loads(line) for line in f]
        except FileNotFoundError:
            return []
        return [c["argv"] for c in calls if c["cmd"] == "podman" and (verb is None or c["argv"][0] == verb)]

    def input_disk(self, image="base", build=None):
        """{member: bytes} of the input disk the VM got."""
        with tarfile.open(os.path.join(self.guest(image, build), "input.tar")) as tar:
            return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}

    def vm_args(self):
        calls = [c["argv"] for c in self.calls("virt-install")
                 if c["argv"][c["argv"].index("--name") + 1].startswith("agentbox-buildvm-")]
        self.assertEqual(len(calls), 1)
        return calls[0]

    def options(self, args, flag):
        return [args[i + 1] for i, a in enumerate(args) if a == flag]

    def leftover(self, ident):
        """What of the build VM `ident` is still on the Host."""
        return [p for p in (f"{BUILD_VM}/{ident}", f"{BUILD_VM}/out-{ident}", f"{BUILD_VM}/out-{ident}.img",
                            f"{RUN}/{ident}") if os.path.lexists(self.path(p))]

    def out(self, ident):
        return self.path(f"{BUILD_VM}/out-{ident}")


class SwitchTest(BuildVmTest):
    def test_without_the_switch_builds_run_on_the_host(self):
        del self.env["AGENTBOX_BUILD_VM"]
        self.built()
        self.assertEqual(self.newest()["status"], "ok")
        self.assertEqual(len(self.podman("build")), 1)
        self.assertEqual([c for c in self.calls() if c["cmd"] in ("create", "systemd-run", "xorrisofs")], [])
        self.assertEqual([c for c in self.calls("skopeo") if c["argv"][0] == "copy"], [])

    def test_the_switch_counts_only_as_1(self):
        self.env["AGENTBOX_BUILD_VM"] = "yes"
        self.built()
        self.assertEqual(self.calls("create"), [])


class BuildInVmTest(BuildVmTest):
    def test_the_podman_build_runs_in_the_build_vm_with_the_hosts_arguments(self):
        self.ok("set-settings", "package_min_age_days=5", "orca_version=1.4.250")
        new, out = self.in_vm()
        # nothing of the Build runs on the Host: no podman build there
        self.assertEqual(self.podman("build"), [])
        builds = self.guest_podman(verb="build")
        self.assertEqual(len(builds), 1)
        argv = builds[0]
        local = f"localhost/agentbox-input/fedora-bootc:{new['build']}"
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--build-arg"],
                         [f"FEDORA_BOOTC={local}", "PACKAGE_MIN_AGE_DAYS=5", "ORCA_VERSION=1.4.250",
                          "PLAYWRIGHT_VERSION="])
        self.assertIn("--no-cache", argv)
        self.assertIn("--pull=never", argv)
        self.assertEqual(argv[argv.index("-t") + 1], f"localhost/agentbox/base:{new['build']}")
        self.assertEqual(argv[argv.index("-f") + 1], os.path.join(argv[-1], "vm", "Containerfile"))
        # the pinned fedora-bootc went in from the Host's storage, loaded under its local name
        self.assertEqual([c["argv"] for c in self.calls("skopeo") if c["argv"][0] == "copy"][0][:3],
                         ["copy", "--quiet", f"containers-storage:{FEDORA_REF}"])
        self.assertEqual(self.guest_podman(verb="load")[0][:2], ["load", "-i"])
        self.assertEqual(self.guest_podman(verb="pull"), [])
        # the record keeps the Host's values, not what the VM used
        self.assertEqual((new["package_min_age_days"], new["orca_version"]), (5, "1.4.250"))
        rec = self.build_record()
        self.assertEqual(rec["fedora_bootc_digest"], FEDORA_REF.split("@")[1])

    def test_the_build_vm_hands_out_an_image_archive_the_host_does_not_import_yet(self):
        new, out = self.in_vm()
        ident = self.ident()
        archive_path = os.path.join(self.out(ident), "image.oci.tar")
        with tarfile.open(archive_path) as tar:
            self.assertEqual(tar.extractfile("name").read().decode(), f"localhost/agentbox/base:{new['build']}")
        self.assertEqual(self.guest_podman(verb="save")[0][:4], ["save", "--format", "oci-archive", "-o"])
        with open(os.path.join(self.out(ident), "result.json")) as f:
            self.assertEqual(json.load(f), {"status": "ok"})
        self.assertEqual(os.stat(self.out(ident)).st_mode & 0o777, 0o700)
        self.assertEqual((new["status"], new["step"]), ("failed", "build"))
        self.assertIn(f"the build VM handed out {archive_path}; the Host does not import", new["error"])
        self.assertIn(f"== base: the build VM handed out its image archive ({archive_path})", out)
        # nothing of the archive reached the Host's storage, and the previous Build stays
        self.assertFalse(os.path.isdir(self.image_store(f"localhost/agentbox/base:{new['build']}")))
        self.assertEqual(self.current(), "base-20261101.qcow2")
        # the VM's disks and run directory are gone; only the output filesystem stays
        self.assertEqual(self.leftover(ident), [f"{BUILD_VM}/out-{ident}", f"{BUILD_VM}/out-{ident}.img"])

    def test_the_output_directory_is_a_filesystem_of_its_own_mounted_read_only_once_the_vm_ended(self):
        """virtiofs has no quota: a filesystem of a fixed size bounds what the VM writes there,
        however fast; nothing changes it once the VM ended."""
        self.in_vm()
        ident = self.ident()
        image, out = self.out(ident) + ".img", self.out(ident)
        st = os.stat(image)
        self.assertEqual((st.st_size, st.st_mode & 0o777), (1 << 30, 0o600))
        self.assertLess(st.st_blocks * 512, 1 << 20)            # sparse
        uid, gid = os.getuid(), os.getgid()
        self.assertEqual(self.calls("mkfs.ext4")[0]["argv"], ["-q", "-F", "-m", "0", "-E", f"root_owner={uid}:{gid}",
                                                              image])
        self.assertEqual([c["argv"] for c in self.calls("mount")], [
            ["-t", "ext4", "-o", "loop,nosuid,nodev,noexec", image, out],
            ["-o", "remount,ro,nosuid,nodev,noexec", out]])
        # the read-only mount comes once virtiofsd's units were stopped and found inactive
        calls = self.calls()
        stop = next(i for i, c in enumerate(calls) if c["cmd"] == "systemctl" and c["argv"][0] == "stop")
        checked = max(i for i, c in enumerate(calls) if c["cmd"] == "systemctl" and c["argv"][0] == "is-active"
                      and c["argv"][-1].startswith("agentbox-buildvm-"))
        remount = next(i for i, c in enumerate(calls) if c["cmd"] == "mount" and "remount" in c["argv"][1])
        self.assertLess(stop, checked)
        self.assertLess(checked, remount)
        self.assertEqual(self.calls("umount"), [])
        # the next run unmounts and deletes it
        self.forget_calls()
        del self.env["AGENTBOX_BUILD_VM"]
        self.built()
        self.assertEqual([c["argv"] for c in self.calls("umount")], [[out]])
        self.assertEqual(self.leftover(ident), [])

    def test_without_room_for_the_output_filesystem_no_build_vm_starts(self):
        self.env["AGENTBOX_BUILD_VM_OUT_BYTES"] = str(1 << 60)
        new, _ = self.in_vm()
        self.assertEqual(new["error"], f"not enough free space under {self.path(BUILD_VM)} for a build VM's output: "
                                       f"it needs {(1 << 60) >> 30} GiB")
        self.assertEqual(self.calls("create") + self.calls("mkfs.ext4"), [])
        self.assertEqual(self.leftover(self.ident()), [])

    def test_the_vm_is_a_transient_domain_of_fixed_size_that_nobody_logs_into(self):
        self.in_vm()
        ident = self.ident()
        args = self.vm_args()
        self.assertEqual(args[args.index("--name") + 1], f"agentbox-buildvm-{ident}")
        self.assertEqual(args[args.index("--memory") + 1], "8192")
        self.assertEqual(args[args.index("--vcpus") + 1], str(min(os.cpu_count(), 8)))
        self.assertEqual(self.options(args, "--events"), ["on_poweroff=destroy,on_reboot=destroy,on_crash=destroy"])
        self.assertEqual(self.options(args, "--memorybacking"), ["source.type=memfd,access.mode=shared"])
        self.assertEqual(self.options(args, "--memballoon"), ["none"])
        self.assertEqual(self.options(args, "--console"), ["none"])
        # no guest agent channel, no serial console, no fw_cfg, no graphics: only the log channel
        run = self.path(f"{RUN}/{ident}")
        self.assertEqual(self.options(args, "--channel"),
                         [f"unix,mode=bind,path={run}/log.sock,target.type=virtio,target.name=org.agentbox.build-log"])
        self.assertEqual(self.options(args, "--serial"), [])
        self.assertEqual(self.options(args, "--sysinfo"), [])
        self.assertEqual(self.options(args, "--graphics"), ["none"])
        self.assertEqual(self.options(args, "--network"), ["network=agentbox-build,mac=52:54:00:77:00:02,model=virtio"])
        self.assertEqual(self.options(args, "--filesystem"),
                         [f"type=mount,driver.type=virtiofs,source.socket={run}/virtiofs.sock,target.dir=agentbox-out"])
        work = self.path(f"{BUILD_VM}/{ident}")
        self.assertEqual(self.options(args, "--disk"), [
            f"path={work}/system.qcow2,format=qcow2,bus=virtio,target.dev=vda,cache=none,io=native,discard=unmap",
            f"path={work}/scratch.qcow2,format=qcow2,bus=virtio,target.dev=vdb,cache=none,io=native,discard=unmap,"
            "serial=agentbox-scratch",
            f"path={work}/input.tar,format=raw,bus=virtio,target.dev=vdc,readonly=on,serial=agentbox-build-input",
            f"path={work}/seed.iso,format=raw,bus=virtio,target.dev=vdd,readonly=on"])
        # started transient (create), never defined, and gone once it ended
        self.assertEqual([c["dom"] for c in self.calls("create")], [f"agentbox-buildvm-{ident}"])
        self.assertEqual([c for c in self.calls("define") if "buildvm" in c["dom"]], [])
        self.assertEqual(self.run_virsh("domstate", f"agentbox-buildvm-{ident}")[0], 1)

    def run_virsh(self, *args):
        r = subprocess.run(["python3", os.path.join(REPO, "tests", "fake_virsh.py"), *args], capture_output=True,
                           text=True, env=self.environment())
        return r.returncode, r.stdout, r.stderr

    def test_its_disks_are_new_a_copy_of_the_checked_image_and_an_empty_scratch_disk(self):
        self.in_vm()
        ident = self.ident()
        work = self.path(f"{BUILD_VM}/{ident}")
        # the system disk is the VM's own copy, grown, with no backing file: libvirt never hands the
        # checked image to QEMU's user
        self.assertEqual([c["argv"] for c in self.calls("qemu-img") if c["argv"][0] in ("create", "resize")], [
            ["resize", "-q", "-f", "qcow2", f"{work}/system.qcow2", "20G"],
            ["create", "-q", "-f", "qcow2", f"{work}/scratch.qcow2", "40G"]])
        self.assertFalse([c for c in self.calls("qemu-img") if "-b" in c["argv"]])
        self.assertEqual(self.mode(f"{BUILD_VM}/images/{IMAGE_44}"), 0o644)
        with open(self.path(f"{BUILD_VM}/images/{IMAGE_44}")) as f:
            self.assertEqual(f.read(), "{}")
        # the driver formats the scratch disk and makes it the VM's container storage
        with open(os.path.join(self.guest(), "calls.log")) as f:
            said = f.read().splitlines()
        scratch = os.path.join(self.guest(), "dev/disk/by-id/virtio-agentbox-scratch")
        storage = os.path.join(self.guest(), "var/lib/containers")
        # the new filesystem gets the SELinux labels of /var/lib/containers before podman uses it
        self.assertEqual(said[:3], [f"mkfs.btrfs -q -f {scratch}", f"mount {scratch} {storage}",
                                    f"restorecon -R {storage}"])
        self.assertIn(f"mount -t virtiofs agentbox-out {os.path.join(self.guest(), 'run/agentbox-out')}", said)
        self.assertFalse(os.path.exists(work))
        # root's alone while they were there (the input disk holds a Recipe); libvirt hands them to QEMU
        with open(os.path.join(self.guest(), "disk-modes.json")) as f:
            self.assertEqual(json.load(f), {"vda": 0o600, "vdb": 0o600, "vdc": 0o600, "vdd": 0o600})

    def test_the_seed_masks_sshd_creates_no_user_and_runs_the_releases_driver(self):
        self.in_vm()
        seed = os.path.join(self.guest(), "seed")
        call = self.calls("xorrisofs")[0]["argv"]
        self.assertEqual(call[call.index("-V") + 1], "cidata")
        with open(os.path.join(seed, "volume")) as f:
            self.assertEqual(f.read(), "cidata")
        for name in ("user-data", "build-vm-run"):
            with open(os.path.join(self.source, "vm", "build-vm", name)) as release, \
                    open(os.path.join(seed, name)) as got:
                self.assertEqual(got.read(), release.read())
        with open(os.path.join(seed, "meta-data")) as f:
            self.assertEqual(f.read(),
                             f"instance-id: agentbox-buildvm-{self.ident()}\nlocal-hostname: agentbox-build\n")
        with open(os.path.join(REPO, "vm", "build-vm", "user-data")) as f:
            lines = [l for l in f.read().splitlines() if l and not l.startswith("#")]
        self.assertEqual(lines[0], "users: []")
        self.assertIn("ssh_pwauth: false", lines)
        self.assertIn("package_upgrade: false", lines)
        self.assertIn("  - [systemctl, mask, --now, sshd.service, sshd.socket, qemu-guest-agent.service]", lines)
        self.assertIn("python3 -I /run/agentbox-seed/build-vm-run", "\n".join(lines))
        self.assertEqual(lines[lines.index("power_state:") + 1:], ["  mode: poweroff", "  delay: now",
                                                                    "  condition: true", "  timeout: 60"])
        # the driver ran from the seed, as root would
        with open(os.path.join(self.guest(), "driver.exit")) as f:
            self.assertEqual(f.read(), "0")

    def test_the_input_disk_holds_the_job_the_context_and_the_images_from_the_hosts_storage(self):
        new, _ = self.in_vm()
        disk = self.input_disk()
        self.assertEqual(sorted(disk), ["context.tar", "images/0.oci.tar", "job.json"])
        local = f"localhost/agentbox-input/fedora-bootc:{new['build']}"
        self.assertEqual(json.loads(disk["job.json"]), {
            "job": "build", "image": "base", "build": new["build"], "tag": f"localhost/agentbox/base:{new['build']}",
            "build_args": [["FEDORA_BOOTC", local], ["PACKAGE_MIN_AGE_DAYS", "2"], ["ORCA_VERSION", ""],
                           ["PLAYWRIGHT_VERSION", ""]],
            "containerfile": "vm/Containerfile", "images": [{"archive": "images/0.oci.tar", "name": local}]})
        with tarfile.open(fileobj=io.BytesIO(disk["context.tar"])) as tar:
            names = tar.getnames()
            with open(os.path.join(self.source, "vm", "Containerfile"), "rb") as f:
                self.assertEqual(tar.extractfile("vm/Containerfile").read(), f.read())
        self.assertIn("github-app/gh", names)
        with tarfile.open(fileobj=io.BytesIO(disk["images/0.oci.tar"])) as tar:
            self.assertEqual(tar.extractfile("name").read().decode(), local)
        copy = [c["argv"] for c in self.calls("skopeo") if c["argv"][0] == "copy"]
        self.assertEqual(len(copy), 1)
        self.assertEqual(copy[0][3].rsplit(":", 2)[1:], [local.rsplit(":", 1)[0], new["build"]])
        # the VM loaded it under that name, no other, and built FROM it
        self.assertEqual([a[:2] for a in self.guest_podman()][:1], [["load", "-i"]])
        self.assertEqual(self.guest_podman(verb="tag"), [])
        self.assertTrue(os.path.isdir(os.path.join(self.guest(), "podman", re.sub(r"[^A-Za-z0-9_.-]", "_", local))))

    def test_virtiofsd_runs_as_its_own_user_with_every_guest_id_squashed(self):
        self.in_vm()
        ident = self.ident()
        argv = self.calls("systemd-run")[0]["argv"]
        run, out = self.path(f"{RUN}/{ident}"), self.out(ident)
        self.assertIn(f"--unit=agentbox-buildvm-{ident}", argv)
        self.assertIn("--property=User=agentbox-buildvm", argv)
        self.assertIn("--property=Group=agentbox-buildvm", argv)
        self.assertIn("--property=NoNewPrivileges=yes", argv)
        # systemd (root) makes the socket QEMU connects to; virtiofsd gets it as fd 3
        self.assertIn(f"--socket-property=ListenStream={run}/virtiofs.sock", argv)
        self.assertIn("--socket-property=SocketGroup=qemu", argv)
        self.assertIn("--socket-property=SocketMode=0660", argv)
        uid, gid = os.getuid(), os.getgid()    # agentbox-buildvm's, as a test can give no other
        # root of a user namespace that maps only agentbox-buildvm, every guest id squashed onto it
        self.assertEqual(argv[argv.index("--") + 1:], [
            "/usr/libexec/virtiofsd", "--fd=3", f"--shared-dir={out}", "--sandbox=namespace",
            f"--uid-map=:0:{uid}:1:", f"--gid-map=:0:{gid}:1:",
            "--translate-uid=squash-guest:0:0:4294967295", "--translate-gid=squash-guest:0:0:4294967295"])
        # its units stopped once the VM ended
        stops = [c["argv"] for c in self.calls("systemctl") if c["argv"][0] == "stop"]
        self.assertIn(["stop", f"agentbox-buildvm-{ident}.socket", f"agentbox-buildvm-{ident}.service"], stops)

    def test_the_log_reaches_the_builds_journal_cleaned_with_vm_in_front(self):
        self.scenario(build_vm={"lines": ["boot \x1b[31mred\x07 done\r\n", "x" * 5000 + "\n"]})
        new, out = self.in_vm()
        lines = out.splitlines()
        self.assertIn("vm: boot [31mred done", lines)
        self.assertIn("vm: " + "x" * 4096, lines)
        self.assertIn("vm: " + "x" * (5000 - 4096), lines)
        self.assertIn("vm: build-vm: running podman build", lines)
        self.assertIn("vm: STEP 1/", out)
        self.assertIn("vm: build-vm: done", lines)

    def test_the_steps_of_the_podman_build_in_the_vm_are_the_builds_progress(self):
        self.scenario(build_vm={"podman": {"build_waits": {"step": 2, "file": "go"}}})
        p = self.build_in_background()
        self.wait_for(lambda: self.running_step() == 2, "progress of step 2")
        rec = self.build_record()
        progress = rec["progress"]
        self.assertGreater(progress["steps"], 2)
        # what `agentbox images` shows of a running Build
        self.assertEqual(self.newest()["progress"], progress)
        with open(os.path.join(self.guest(build=rec["build"]), "go"), "w"):
            pass
        p.wait(timeout=60)
        self.assertEqual(self.build_record()["progress"]["step"], progress["steps"])

    def running_step(self):
        """The step of the newest Build's progress while it runs, else None."""
        if not os.path.isdir(self.path("var/lib/agentbox/builds")) or not any(
                n.startswith("base-") for n in os.listdir(self.path("var/lib/agentbox/builds"))):
            return None
        rec = self.build_record()
        return (rec.get("progress") or {}).get("step") if rec["status"] == "running" else None

    def test_a_failure_in_the_vm_fails_the_build_with_its_reason(self):
        self.scenario(build_vm={"podman": {"podman_fails": {"build": ["localhost/agentbox/base:"]}}})
        new, out = self.in_vm()
        self.assertEqual(new["status"], "failed")
        self.assertEqual(new["error"], "in the build VM: podman build failed (exit 1)")
        self.assertIn("vm: build-vm: failed: podman build failed (exit 1)", out)
        self.assertEqual(self.leftover(self.ident()), [])
        entry = self.activity()[-1]
        self.assertEqual(entry["AGENTBOX_ACTION"], "build.failed")

    def test_a_vm_without_a_result_fails_the_build(self):
        self.scenario(build_vm={"driver": False, "lines": ["kernel panic\n"]})
        new, out = self.in_vm()
        self.assertEqual(new["error"], "the build VM ended without a result (result.json; see its lines in this log)")
        self.assertIn("vm: kernel panic", out)
        self.assertEqual(self.leftover(self.ident()), [])

    def test_a_link_or_a_second_link_instead_of_the_archive_is_refused(self):
        for tamper in ("symlink", "hardlink"):
            with self.subTest(tamper=tamper):
                self.scenario(build_vm={"tamper": tamper})
                new, _ = self.in_vm()
                self.assertTrue(new["error"].startswith("the build VM handed out no image archive"), new["error"])
                self.assertEqual(self.leftover(self.ident()), [])
                time.sleep(1.1)        # the next Build gets another id


class NetworkTest(BuildVmTest):
    def test_the_build_network_is_nat_ipv4_with_one_dhcp_host_started_but_not_autostarted(self):
        self.in_vm()
        xml = [c["xml"] for c in self.calls("net-define") if c["net"] == "agentbox-build"][0]
        self.assertIn("<forward mode='nat'/>", xml)
        self.assertIn("<bridge name='aboxbuild' stp='on' delay='0'/>", xml)
        self.assertIn("<ip address='10.77.0.1' netmask='255.255.255.0'>", xml)
        self.assertIn("<host mac='52:54:00:77:00:02' ip='10.77.0.2'/>", xml)
        self.assertNotIn("ipv6", xml)
        self.assertNotIn("dnsmasq:option", xml)      # no address=/#/: names resolve as on the Host
        self.assertEqual([c["net"] for c in self.calls("net-start")], ["agentbox-build"])
        self.assertEqual([c for c in self.calls("net-autostart") if c["net"] == "agentbox-build"], [])
        # the next build VM finds it running and leaves it
        self.forget_calls()
        time.sleep(1.1)
        self.in_vm()
        self.assertEqual(self.calls("net-define") + self.calls("net-start"), [])

    def test_an_existing_build_network_that_is_not_running_is_started(self):
        self.scenario(networks={"agentbox-build": {"active": False, "autostart": False,
                                                   "xml": build_vm_run.network_xml()}})
        self.in_vm()
        self.assertEqual(self.calls("net-define"), [])
        self.assertEqual([c["net"] for c in self.calls("net-start")], ["agentbox-build"])
        self.assertEqual(len(self.calls("create")), 1)

    def test_an_existing_network_of_that_name_must_be_the_build_network(self):
        ours = build_vm_run.network_xml()
        for what, xml in (
                ("another network", "<network>\n  <name>agentbox-build</name>\n  <bridge name='virbr9'/>\n"
                                    "  <ip address='192.168.9.1' netmask='255.255.255.0'/>\n</network>"),
                ("a wider netmask", ours.replace("255.255.255.0", "255.255.0.0")),
                ("a second address", ours.replace("</network>", "  <ip address='192.168.9.1' prefix='24'/>\n"
                                                                "</network>")),
                ("IPv6", ours.replace("</network>", "  <ip family='ipv6' address='fd00::1' prefix='64'/>\n"
                                                    "</network>")),
                ("routed", ours.replace("mode='nat'", "mode='route'"))):
            with self.subTest(what=what):
                self.forget_calls()
                self.scenario(networks={"agentbox-build": {"active": True, "autostart": True, "xml": xml}})
                new, _ = self.in_vm()
                self.assertTrue(new["error"].startswith("the network agentbox-build is not the build network"),
                                new["error"])
                self.assertEqual(self.calls("create"), [])
                time.sleep(1.1)

    def test_the_rules_let_the_build_network_reach_only_dhcp_and_dns_of_the_host(self):
        rules = generate.nft_rules([]).splitlines()
        self.assertIn('    iifname "aboxbuild" jump from_build', rules[rules.index("  chain input {"):])
        start = rules.index("  chain from_build {") + 1
        chain = rules[start:rules.index("  }", start)]
        self.assertEqual(chain, ["    meta nfproto ipv6 drop",
                                 "    ip daddr { 10.77.0.1, 255.255.255.255 } udp dport 67 accept",
                                 "    ip daddr 10.77.0.1 udp dport 53 accept",
                                 "    ip daddr 10.77.0.1 tcp dport 53 accept", "    drop"])
        start = rules.index("  chain forward {") + 1
        chain = rules[start:rules.index("  }", start)]
        self.assertEqual(chain[0], "    type filter hook forward priority -5; policy accept")
        self.assertIn('    iifname "aboxbuild" meta nfproto ipv6 drop', chain)
        blocked = next(l for l in chain if "ip daddr {" in l)
        for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
                    "127.0.0.0/8", "224.0.0.0/4", "240.0.0.0/4", "0.0.0.0/8"):
            self.assertIn(net, blocked)
        self.assertTrue(blocked.startswith('    iifname "aboxbuild" ') and blocked.endswith(" drop"))
        # nothing reaches a build VM that it did not ask for
        self.assertEqual(chain[-2:], ['    oifname "aboxbuild" ct state established,related accept',
                                      '    oifname "aboxbuild" drop'])
        # the VM networks' own rules do not match the build network's bridge
        self.assertIn('    iifname "abox-*" jump from_vm', rules)
        self.assertNotRegex("aboxbuild", r"^abox-")

    def test_without_the_rules_loaded_in_the_kernel_no_build_vm_starts(self):
        """Written is not loaded: the check asks nft for the chains in the kernel."""
        forward = KERNEL["forward"]
        for broken, chains, said in (
                ("no chains", {}, "the chain input is not loaded"),
                ("no jump",
                 {**KERNEL, "input": KERNEL["input"].replace('\t\tiifname "aboxbuild" jump from_build\n', "")},
                 "the input chain does not send the build network to from_build"),
                ("a rule missing", {**KERNEL, "from_build": KERNEL["from_build"].replace("\t\tdrop\n", "")},
                 "the chain from_build is not the build network's"),
                ("a range missing", {**KERNEL, "forward": forward.replace(" 192.168.0.0/16,", "")},
                 "the chain forward is not the build network's"),
                ("a wider accept", {**KERNEL, "from_build": KERNEL["from_build"].replace(
                    "10.77.0.1, 255.255.255.255", "10.77.0.0/24, 255.255.255.255")},
                 "the chain from_build is not the build network's"),
                ("not hooked", {**KERNEL, "forward": forward.replace("hook forward", "hook output")},
                 "the chain forward is not hooked into forward")):
            with self.subTest(broken=broken):
                self.forget_calls()
                self.scenario(nft_chains=chains)
                new, _ = self.in_vm()
                self.assertEqual(new["error"], f"the build network's rules are not loaded in the kernel: {said} "
                                               "(sudo agentbox regenerate; systemctl restart agentbox-vmnet)")
                self.assertEqual(self.calls("create"), [])
                time.sleep(1.1)
        self.assertEqual(self.calls("nft")[0]["argv"], ["list", "chain", "inet", "agentbox_vm", "input"])

    @unittest.skipUnless(shutil.which("nft") and shutil.which("unshare") and subprocess.run(
        ["unshare", "-rn", "true"], capture_output=True).returncode == 0, "needs nft and user namespaces")
    def test_the_rules_regenerate_writes_pass_the_check_as_a_real_nft_lists_them(self):
        """Not in the claims: it skips itself where unprivileged namespaces are not allowed."""
        rules = os.path.join(self.dir, "rules.nft")
        with open(rules, "w") as f:
            f.write(generate.nft_rules([]))
        script = f"nft -f {rules} && " + " && ".join(
            f"nft list chain inet agentbox_vm {c} > {os.path.join(self.dir, c)}.txt" for c in KERNEL)
        self.assertEqual(subprocess.run(["unshare", "-rn", "sh", "-c", script]).returncode, 0)
        listings = {}
        for chain in KERNEL:
            with open(os.path.join(self.dir, f"{chain}.txt")) as f:
                listings[chain] = f.read()
        self.assertIsNone(build_vm_run.rules_problem(listings))
        self.assertIsNotNone(build_vm_run.rules_problem({**listings, "forward": listings["forward"].replace(
            "10.0.0.0/8, ", "")}))


class HostTest(BuildVmTest):
    def test_without_kvm_a_checked_image_or_enough_memory_no_build_vm_starts(self):
        for missing, said in (("kvm", "the Host has no KVM (/dev/kvm): no build VM can run"),
                              ("image", "there is no checked build VM image yet"),
                              ("memory", "not enough free memory for a build VM: it needs 8 GiB, the Host has "
                                         "8191 MiB available")):
            with self.subTest(missing=missing):
                if missing == "kvm":
                    os.remove(self.path("dev/kvm"))
                elif missing == "image":
                    self.write("dev/kvm", "")
                    os.remove(self.path(f"{BUILD_VM}/image.json"))
                else:
                    self.write(f"{BUILD_VM}/image.json", json.dumps({"name": IMAGE_44, "release": "44",
                                                                     "sha256": "0" * 64, "checked": 1}))
                    self.write("proc/meminfo", "MemTotal:       33554432 kB\nMemAvailable:    8387584 kB\n")
                new, _ = self.in_vm()
                self.assertTrue(new["error"].startswith(said), new["error"])
                self.assertEqual(self.calls("create") + self.calls("systemd-run") + self.calls("net-define"), [])
                self.assertEqual(self.podman("build"), [])      # no fallback to the Host
                time.sleep(1.1)


class LimitTest(BuildVmTest):
    def test_a_vm_that_says_nothing_is_destroyed_after_the_boot_limit(self):
        self.env["AGENTBOX_BUILD_VM_BOOT_SECONDS"] = "2"
        self.scenario(build_vm={"boots": False})
        new, _ = self.in_vm()
        ident = self.ident()
        self.assertEqual(new["error"], "the build VM did not boot: no line on its log channel within 2 s")
        self.assertEqual([c["dom"] for c in self.calls("destroy")], [f"agentbox-buildvm-{ident}"])
        self.assertEqual(self.leftover(ident), [])

    def test_a_vm_beyond_its_time_limit_is_destroyed(self):
        self.env["AGENTBOX_BUILD_VM_SECONDS"] = "3"
        self.scenario(build_vm={"podman": {"build_waits": {"step": 1, "file": "never"}}})
        new, out = self.in_vm()
        self.assertEqual(new["error"], "the build VM's time limit of 3 s is exceeded")
        self.assertIn("vm: STEP 1/", out)
        self.assertEqual([c["dom"] for c in self.calls("destroy")], [f"agentbox-buildvm-{self.ident()}"])
        self.assertEqual(self.leftover(self.ident()), [])

    def test_a_vm_that_fills_its_output_directory_is_destroyed(self):
        self.env["AGENTBOX_BUILD_VM_OUT_BYTES"] = str(64 << 10)
        self.scenario(build_vm={"out_files": {"big": 1 << 20}, "podman": {"build_waits": {"step": 1, "file": "never"}}})
        new, _ = self.in_vm()
        self.assertEqual(new["error"], f"the build VM wrote more than {64 << 10} bytes into its output directory")
        self.assertEqual(len(self.calls("destroy")), 1)
        self.assertEqual(self.leftover(self.ident()), [])

    def test_what_lies_deep_in_the_output_directory_counts(self):
        self.env["AGENTBOX_BUILD_VM_OUT_BYTES"] = str(64 << 10)
        self.scenario(build_vm={"out_deep": {"depth": 20, "bytes": 1 << 20},
                                "podman": {"build_waits": {"step": 1, "file": "never"}}})
        new, _ = self.in_vm()
        self.assertEqual(new["error"], f"the build VM wrote more than {64 << 10} bytes into its output directory")
        self.assertEqual(self.leftover(self.ident()), [])

    def test_directories_nested_too_deep_end_the_vm(self):
        """3000 deep: deeper than a path can name, which a walk by paths would skip."""
        self.scenario(build_vm={"out_deep": {"depth": 3000, "bytes": 100 << 10},
                                "podman": {"build_waits": {"step": 1, "file": "never"}}})
        new, _ = self.in_vm()
        self.assertEqual(new["error"], "the build VM nested directories deeper than 32 in its output directory")
        self.assertEqual(len(self.calls("destroy")), 1)
        self.assertEqual(self.leftover(self.ident()), [])

    def test_too_many_files_in_the_output_directory_end_the_vm(self):
        self.env["AGENTBOX_BUILD_VM_OUT_ENTRIES"] = "50"
        self.scenario(build_vm={"out_many": 51, "podman": {"build_waits": {"step": 1, "file": "never"}}})
        new, _ = self.in_vm()
        self.assertEqual(new["error"], "the build VM made more than 50 files and directories in its output directory")

    def test_empty_lines_count_toward_the_log_cap(self):
        self.env["AGENTBOX_BUILD_VM_LOG_BYTES"] = "2000"
        self.scenario(build_vm={"lines": ["\n"] * 100})
        new, out = self.in_vm()
        self.assertEqual(out.count("the build VM wrote more than 2000 bytes to its log; its further lines are "
                                   "not kept"), 1)
        self.assertEqual(out.splitlines().count("vm: "), 2000 // 64)

    def test_a_log_beyond_its_cap_is_cut_with_one_note(self):
        self.env["AGENTBOX_BUILD_VM_LOG_BYTES"] = "2000"
        self.scenario(build_vm={"lines": [f"line {n:03} " + "y" * 40 + "\n" for n in range(100)]})
        new, out = self.in_vm()
        lines = out.splitlines()
        self.assertIn("vm: line 000 " + "y" * 40, lines)
        self.assertNotIn("vm: line 099 " + "y" * 40, lines)
        self.assertNotIn("vm: build-vm: done", lines)
        self.assertEqual(out.count("the build VM wrote more than 2000 bytes to its log; its further lines are "
                                   "not kept"), 1)
        # the Build itself goes on, its progress too
        self.assertIn("the Host does not import", new["error"])
        rec = self.build_record()
        self.assertEqual(rec["progress"]["step"], rec["progress"]["steps"])

    def test_too_little_free_space_on_the_host_ends_the_vm(self):
        """Room at the start (for the output filesystem and the rest), then the VM takes it."""
        st = os.statvfs(self.root)
        free = st.f_bavail * st.f_frsize
        self.env["AGENTBOX_BUILD_VM_MIN_FREE_BYTES"] = str(free - (100 << 20))
        self.env["AGENTBOX_BUILD_VM_OUT_BYTES"] = str(1 << 20)
        self.scenario(build_vm={"out_space": 256 << 20, "podman": {"build_waits": {"step": 1, "file": "never"}}})
        new, _ = self.in_vm()
        self.assertEqual(new["error"], f"less than {free - (100 << 20)} bytes free under "
                                       f"{self.path('var/lib/agentbox')}")
        self.assertEqual(len(self.calls("destroy")), 1)
        self.assertEqual(self.leftover(self.ident()), [])


class InterruptionTest(BuildVmTest):
    def test_stopping_the_build_destroys_the_vm_and_stops_virtiofsd(self):
        self.scenario(build_vm={"podman": {"build_waits": {"step": 1, "file": "never"}}})
        p = self.build_in_background()
        self.wait_for(lambda: BuildInVmTest.running_step(self) == 1, "the VM's first step")
        rec = self.build_record()
        ident = self.ident(build=rec["build"])
        p.send_signal(signal.SIGTERM)          # systemctl stop of the Build's unit
        p.wait(timeout=60)
        self.assertNotEqual(p.returncode, 0)
        rec = self.build_record(build=rec["build"])
        self.assertEqual((rec["status"], rec["error"]), ("failed", "interrupted"))
        self.assertEqual([c["dom"] for c in self.calls("destroy")], [f"agentbox-buildvm-{ident}"])
        stops = [c["argv"] for c in self.calls("systemctl") if c["argv"][0] == "stop"]
        self.assertIn(["stop", f"agentbox-buildvm-{ident}.socket", f"agentbox-buildvm-{ident}.service"], stops)
        self.assertEqual(self.leftover(ident), [])

    def test_a_virtiofsd_that_still_runs_after_the_teardown_fails_the_build(self):
        self.scenario(systemctl_active=["agentbox-vmnet.service", "agentbox-buildvm-*.service"])
        new, out = self.in_vm()
        unit = f"agentbox-buildvm-{self.ident()}.service"
        self.assertEqual(new["error"], f"{unit} still runs after the build VM's teardown; its output is not taken")
        self.assertIn(f"Still running after the build VM's teardown: {unit}", out)
        self.assertEqual(self.leftover(self.ident()), [])          # no archive handed on

    def test_a_domain_that_survives_the_teardown_fails_the_build(self):
        self.env["AGENTBOX_BUILD_VM_END_SECONDS"] = "2"
        self.scenario(build_vm={"stays": True}, build_vm_survives_destroy=True)
        new, _ = self.in_vm()
        domain = f"agentbox-buildvm-{self.ident()}"
        self.assertEqual(new["error"], f"the domain {domain} still runs after the build VM's teardown; its output is "
                                       "not taken")
        self.assertEqual([c["dom"] for c in self.calls("destroy")], [domain])
        self.assertEqual(self.leftover(self.ident()), [])

    def test_the_next_run_removes_what_a_killed_run_left(self):
        ident = "base-20260101-000000-build"
        for rel in (f"{BUILD_VM}/{ident}/system.qcow2", f"{BUILD_VM}/out-{ident}/image.oci.tar",
                    f"{RUN}/{ident}/log.sock"):
            self.write(rel, "")
        self.scenario(vms={f"buildvm-{ident}": {"defined": True, "state": "running"}})
        del self.env["AGENTBOX_BUILD_VM"]       # whatever the next run does
        out = self.built()
        self.assertEqual(self.leftover(ident), [])
        self.assertIn(f"Removed what the build VM agentbox-buildvm-{ident} left", out)
        self.assertEqual([c["dom"] for c in self.calls("destroy")], [f"agentbox-buildvm-{ident}"])
        stops = [c["argv"] for c in self.calls("systemctl") if c["argv"][0] == "stop"]
        self.assertIn(["stop", f"agentbox-buildvm-{ident}.socket", f"agentbox-buildvm-{ident}.service"], stops)
        # the checked image stays
        self.assertTrue(os.path.isfile(self.path(f"{BUILD_VM}/images/{IMAGE_44}")))
        self.assertTrue(os.path.isfile(self.path(f"{BUILD_VM}/image.json")))


class DerivedTest(RecipeTest, BuildVmTest):
    def setUp(self):
        super().setUp()
        del self.env["AGENTBOX_BUILD_VM"]
        self.built()
        self.base = self.newest()["build"]
        self.env["AGENTBOX_BUILD_VM"] = "1"
        self.forget_calls()

    def test_a_recipe_goes_in_as_its_tar_unchanged_and_builds_on_the_base_build(self):
        data = archive([file("Containerfile", b"ARG BASE\nFROM ${BASE}\nCOPY tool /usr/bin/tool\n"),
                        file("tool", "#!/bin/sh\necho from-recipe\n")])
        self.set_recipe(data=data)
        new, out = self.in_vm("dms")
        disk = self.input_disk("dms")
        self.assertEqual(disk["context.tar"], data)
        base = f"localhost/agentbox/base:{self.base}"
        job = json.loads(disk["job.json"])
        self.assertEqual((job["containerfile"], job["build_args"], job["images"]),
                         (None, [["BASE", base]], [{"archive": "images/0.oci.tar", "name": base}]))
        copy = [c["argv"] for c in self.calls("skopeo") if c["argv"][0] == "copy"]
        work = self.path(f"{BUILD_VM}/dms-{new['build']}-build")
        self.assertEqual(copy[0][2:], [f"containers-storage:{base}", f"oci-archive:{work}/image-0.oci.tar:{base}"])
        argv = self.guest_podman("dms", verb="build")[0]
        self.assertEqual(argv[-1], "-")
        self.assertNotIn("-f", argv)
        self.assertEqual(self.guest_podman("dms", verb="tag"), [])     # loaded under its own name
        self.assertIn("vm: STEP 2/2: COPY tool /usr/bin/tool", out)
        self.assertEqual(self.build_record("dms")["progress"],
                         {"step": 2, "steps": 2, "command": "COPY tool /usr/bin/tool"})
        self.assertEqual(new["recipe_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.podman("build"), [])

    def test_a_derived_release_image_builds_on_the_base_build_handed_in(self):
        self.image("tools", "ARG BASE\nFROM ${BASE}\nCOPY extra /usr/share/extra\n", extra="x\n")
        new, _ = self.in_vm("tools")
        job = json.loads(self.input_disk("tools")["job.json"])
        self.assertEqual((job["containerfile"], job["build_args"]),
                         ("Containerfile", [["BASE", f"localhost/agentbox/base:{self.base}"]]))
        with tarfile.open(fileobj=io.BytesIO(self.input_disk("tools")["context.tar"])) as tar:
            self.assertEqual(sorted(tar.getnames()), ["Containerfile", "extra"])
        argv = self.guest_podman("tools", verb="build")[0]
        self.assertEqual(argv[argv.index("-f") + 1], os.path.join(argv[-1], "Containerfile"))
        self.assertIn("the Host does not import", new["error"])


if __name__ == "__main__":
    unittest.main()
