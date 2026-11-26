"""The complete virt-install argument lists of a VM (agentbox/generate.py) and of a build VM
(agentbox/build_vm_run.py), pinned element for element; their shared part is in
agentbox/domain_args.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentbox import build_vm_run, footprint, generate, hostsetup  # noqa: E402

DISK = "format=qcow2,bus=virtio,{},cache=none,io=native,discard=unmap"


def base(name, memory, vcpus, uuid=(), console_none=()):
    return [
        "--connect", "qemu:///system", "--print-xml", "--dry-run", "--check", "all=off",
        "--name", name, *uuid, "--osinfo", "linux2024", "--memory", str(memory), "--vcpus", str(vcpus),
        "--cpu", "host-passthrough", "--machine", "q35", "--boot", "hd", "--import", "--noautoconsole",
        "--graphics", "none", "--video", "none", "--sound", "none", "--input", "none", *console_none,
        "--controller", "usb,model=none",
        "--events", "on_poweroff=destroy,on_reboot=destroy,on_crash=destroy",
        "--features", "acpi=on,apic=on",
    ]


class DomainArgsTest(unittest.TestCase):
    def test_vm(self):
        rec = {"name": "dev", "memory_mib": 4096, "orca_port": 6768}
        fp = footprint
        for uuid in (None, "11111111-2222-3333-4444-555555555555"):
            with self.subTest(uuid=uuid), \
                    mock.patch.object(hostsetup, "load", lambda: {}), \
                    mock.patch.object(hostsetup, "pairing_address", lambda cfg: "10.0.0.1:8443"), \
                    mock.patch.object(generate, "mac", lambda r: "52:54:00:00:00:09"):
                expected = [
                    *base("agentbox-dev", 4096, generate.VCPUS, ["--uuid", uuid] if uuid else []),
                    "--sysinfo", "type=fwcfg,entry0.name=opt/agentbox/context,entry0=dev,"
                                 "entry1.name=opt/agentbox/orca_port,entry1=6768,"
                                 "entry2.name=opt/agentbox/claude_placeholder,"
                                 f"entry2.file={fp.claude_placeholder_fw_cfg_file('dev')},"
                                 "entry3.name=opt/agentbox/pairing_address,entry3=10.0.0.1:8443,"
                                 f"entry4.name=opt/agentbox/env,entry4.file={fp.vm_env_file('dev')},"
                                 f"entry5.name=opt/agentbox/ssh,entry5.file={fp.vm_ssh_file('dev')},"
                                 f"entry6.name=opt/agentbox/secrets,entry6.file={fp.vm_secrets_file('dev')}",
                    "--disk", f"path={fp.system_disk('dev')},size=1," + DISK.format("target.dev=vda"),
                    "--disk", f"path={fp.data_disk('dev')},size=1," + DISK.format("target.dev=vdb")
                    + ",serial=agentbox-data",
                    "--network", "network=agentbox-dev,mac=52:54:00:00:00:09,model=virtio",
                    "--serial", "pty", "--console", "pty",
                    "--channel", "unix,target.type=virtio,target.name=org.qemu.guest_agent.0",
                    "--memballoon", "model=virtio,freePageReporting=on",
                    "--rng", "/dev/urandom",
                ]
                self.assertEqual(generate.virt_install_args(rec, uuid), expected)

    def test_build_vm(self):
        for online in (True, False):
            with self.subTest(online=online), mock.patch.object(build_vm_run, "vcpus", lambda: 6):
                vm = {"domain": "agentbox-build-x", "system": "/w/system.qcow2", "scratch": "/w/scratch.qcow2",
                      "input": "/w/input.img", "seed": "/w/seed.iso", "fs_socket": "/w/fs.sock",
                      "log_socket": "/w/log.sock", "online": online}
                expected = [
                    *base("agentbox-build-x", 8192, 6, console_none=["--console", "none"]),
                    "--memorybacking", "source.type=memfd,access.mode=shared",
                    "--disk", "path=/w/system.qcow2," + DISK.format("target.dev=vda"),
                    "--disk", "path=/w/scratch.qcow2," + DISK.format("target.dev=vdb") + ",serial=agentbox-scratch",
                    "--disk", "path=/w/input.img,format=raw,bus=virtio,target.dev=vdc,readonly=on,"
                              "serial=agentbox-build-input",
                    "--disk", "path=/w/seed.iso,format=raw,bus=virtio,target.dev=vdd,readonly=on",
                    "--filesystem", "type=mount,driver.type=virtiofs,source.socket=/w/fs.sock,target.dir=agentbox-out",
                    "--network", "network=agentbox-build,mac=52:54:00:77:00:02,model=virtio" if online else "none",
                    "--channel", "unix,mode=bind,path=/w/log.sock,target.type=virtio,target.name=org.agentbox.build-log",
                    "--memballoon", "none",
                    "--rng", "/dev/urandom",
                ]
                self.assertEqual(build_vm_run.virt_install_args(vm), expected)


if __name__ == "__main__":
    unittest.main()
