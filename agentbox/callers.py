"""Who may run the commands that need no root but hand out access: root and the members of
libvirt, who control every VM anyway (`sign-in-link`, `ssh-config`); and whose rights a
command run through sudo reads the owner's own files with (sudo_user(), become()): `import`
packs a folder, `add-ssh-key` reads FILE or, with --mine, ~/.ssh/*.pub, so root reads nothing
for them that the user who ran sudo could not read.

A leaf module: the Control Room imports sign_in_links, which uses it.
"""
import os
import pwd

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


def sudo_user():
    """(uid, gid) of the user who ran `sudo` (SUDO_UID and SUDO_GID, which sudo sets itself);
    None without them, for root, or for a uid the Host does not know."""
    try:
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
        if uid > 0 and gid >= 0:
            pwd.getpwuid(uid)
            return uid, gid
    except (KeyError, ValueError, OverflowError):
        pass
    return None


def become(uid, gid):
    """The rights of this user, for good (a child process): their groups, and their home as
    HOME."""
    try:
        pw = pwd.getpwuid(uid)
        groups, home = os.getgrouplist(pw.pw_name, gid), pw.pw_dir
    except KeyError:
        groups, home = [gid], "/"
    os.setgroups(groups)
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = home


def check(command):
    """Root, or a member of libvirt in this login (a new membership counts from the next
    one); anyone else is refused before anything is read or written."""
    if os.geteuid() == 0:
        return
    gid = _libvirt_gid()
    if gid is None or gid not in {os.getegid(), *os.getgroups()}:
        die(f"{command} is for root and the members of libvirt, who control every VM anyway: "
            f"sudo agentbox {command}", error="sudo_failed", command=command)
