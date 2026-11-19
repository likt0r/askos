"""Who may run the commands that need no root but hand out access: root and the members of
libvirt, who control every VM anyway (`sign-in-link`, `ssh-config`).

A leaf module: the Control Room imports sign_in_links, which uses it.
"""
import os

from . import paths
from .errors import die

LIBVIRT = "libvirt"
# the Host's groups (tests: under AGENTBOX_ROOT); libvirt is a local group (systemd-sysusers)
GROUP = paths.host("/etc/group")


def _libvirt_gid():
    try:
        with open(GROUP, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split(":")
                if len(parts) > 2 and parts[0] == LIBVIRT and parts[2].isdigit():
                    return int(parts[2])
    except OSError:
        pass
    return None


def check(command):
    """Root, or a member of libvirt in this login (a new membership counts from the next
    one); anyone else is refused before anything is read or written."""
    if os.geteuid() == 0:
        return
    gid = _libvirt_gid()
    if gid is None or gid not in {os.getegid(), *os.getgroups()}:
        die(f"{command} is for root and the members of libvirt, who control every VM anyway: "
            f"sudo agentbox {command}", error="sudo_failed", command=command)
