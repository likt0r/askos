"""What a VM and a build VM have in common in the virt-install arguments that print their libvirt
domain (generate.virt_install_args, build_vm_run.virt_install_args): the base options and the
text of a disk option. Everything else is the business of those two functions. A leaf: it
imports only hostfacts.
"""
from . import hostfacts


def base(name, memory_mib, vcpus, *, uuid=None, console_none=False):
    """From the connection to the features, in the order both lists have them. console_none: the
    build VM has no console at all."""
    return [
        "--connect", "qemu:///system", "--print-xml", "--dry-run", "--check", "all=off",
        "--name", name, *(["--uuid", uuid] if uuid else []),
        "--osinfo", "linux2024", "--memory", str(memory_mib), "--vcpus", str(vcpus),
        "--cpu", "host-passthrough", "--machine", hostfacts.MACHINE, "--boot", "hd", "--import", "--noautoconsole",
        "--graphics", "none", "--video", "none", "--sound", "none", "--input", "none",
        *(["--console", "none"] if console_none else []),
        "--controller", "usb,model=none",
        # a reboot inside the VM ends QEMU, which reads the fw_cfg files only when it starts:
        # the VM is off, and its next start boots with the current SSH keys, VM variables,
        # secrets and placeholder, never with those its last start read (#160)
        "--events", "on_poweroff=destroy,on_reboot=destroy,on_crash=destroy",
        "--features", hostfacts.FEATURES,
    ]


def disk(path, dev, size=None):
    """A qcow2 virtio disk as `dev` (vda, ...). size (GiB): virt-install insists on one for a
    disk that does not exist yet; size=1 only satisfies that check."""
    return (f"path={path}," + (f"size={size}," if size else "")
            + f"format=qcow2,bus=virtio,target.dev={dev},cache=none,io=native,discard=unmap")
