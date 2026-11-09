"""Who writes into the journald namespace agentbox, as journald stamps it, for the
entries of the fake journal (journal.json of fake_journalctl.py).

journald adds trusted fields to every entry (_UID from the kernel, _SYSTEMD_UNIT from
the sender's cgroup, _COMM, _EXE, _TRANSPORT) that no client can set. Only root and
the group agentbox-journal may write into the namespace (its sockets, the package's drop-in);
on top of that each writer may write only its own kinds of entries (activity.trusted).
Tests give legitimate entries the fields of their writer and forge entries with the
fields of someone else:

    write_passwd(root)                          the users, as etc/passwd under the test root
    write_group(root)                           the group agentbox-journal, as etc/group there
    writer("agentbox-proxy-work")               the proxy of work
    writer("agentbox-activity")                 the agentbox CLI (root)
    writer("agentbox-activity", kind="signin")  the Control Room's sign-ins
    MEMBER                                      the agentbox CLI of a member of agentbox-journal,
                                                without root (agentbox pair)
    written_by(entry, FORGED)                   the entry as a local user's process sends it: anyone
                                                can write the default journal, and could write the
                                                namespace before its sockets were restricted
"""
import os

TRUSTED = ("_UID", "_GID", "_SYSTEMD_UNIT", "_COMM", "_EXE", "_TRANSPORT")

PROXY, GH, WEB = "962", "963", "959"
PASSWD = f"""root:x:0:0:root:/root:/bin/bash
agentbox-proxy:x:{PROXY}:{PROXY}::/home/agentbox-proxy:/usr/sbin/nologin
agentbox-gh:x:{GH}:{GH}::/home/agentbox-gh:/usr/sbin/nologin
agentbox-web:x:{WEB}:{WEB}::/home/agentbox-web:/usr/sbin/nologin
bewr:x:1000:1000::/home/bewr:/bin/bash
gast:x:1002:1002::/home/gast:/bin/bash
"""
GROUP = """root:x:0:
libvirt:x:985:bewr
agentbox-journal:x:970:bewr
"""
CLI = {"_UID": "0", "_GID": "0", "_SYSTEMD_UNIT": "session-3.scope", "_COMM": "agentbox",
       "_EXE": "/usr/bin/python3.14", "_TRANSPORT": "journal"}
MEMBER = {"_UID": "1000", "_GID": "1000", "_SYSTEMD_UNIT": "session-4.scope", "_COMM": "agentbox",
          "_EXE": "/usr/bin/python3.14", "_TRANSPORT": "journal"}
CONTROL_ROOM = {"_UID": WEB, "_GID": WEB, "_SYSTEMD_UNIT": "agentbox-web.service", "_COMM": "agentbox-web",
                "_EXE": "/usr/bin/python3.14", "_TRANSPORT": "journal"}
FORGED = {"_UID": "1002", "_GID": "1002", "_SYSTEMD_UNIT": "user@1002.service", "_COMM": "python3",
          "_EXE": "/usr/bin/python3.14", "_TRANSPORT": "journal"}


def write_passwd(root):
    os.makedirs(os.path.join(root, "etc"), exist_ok=True)
    with open(os.path.join(root, "etc", "passwd"), "w") as f:
        f.write(PASSWD)


def write_group(root, text=GROUP):
    os.makedirs(os.path.join(root, "etc"), exist_ok=True)
    with open(os.path.join(root, "etc", "group"), "w") as f:
        f.write(text)


def proxy(vm):
    return {"_UID": PROXY, "_GID": "970", "_SYSTEMD_UNIT": f"agentbox-proxy-{vm}.service", "_COMM": "agentbox-proxy",
            "_EXE": "/usr/bin/python3.14", "_TRANSPORT": "stdout"}


def token_service(vm):
    return {"_UID": GH, "_GID": "970", "_SYSTEMD_UNIT": f"agentbox-ghvm-{vm}@7-4242-10.77.1.1:9100-10.77.1.2:51666.service",
            "_COMM": "agentbox-gh-tok", "_EXE": "/usr/bin/python3.14", "_TRANSPORT": "stdout"}


def writer(ident, kind=None):
    """The trusted fields of the legitimate writer of this identifier (none for others)."""
    if ident == "agentbox-activity":
        return dict(CONTROL_ROOM if kind == "signin" else CLI)
    if ident.startswith("agentbox-proxy-"):
        return proxy(ident[len("agentbox-proxy-"):])
    if ident.startswith("agentbox-gh-"):
        return token_service(ident[len("agentbox-gh-"):])
    return {}


def written_by(entry, fields):
    """The entry as journald stamps it when someone else (these trusted fields) sent it."""
    return {**{k: v for k, v in entry.items() if k not in TRUSTED}, **fields}
