"""Sign-in links of the Control Room: `agentbox sign-in-link` (#137).

Root and the members of libvirt, who control every VM anyway, get a link that signs a browser
in to the Control Room once, without the password. The link names the Control Room's address
for the Host setup's mode (the bind address in Network mode, 127.0.0.1 in Local-only) and
carries a random token in its fragment (`/sign-in#link=<token>`), which a browser never sends
to a server, so it reaches no log. The sign-in page reads it and sends it in the body of
`POST /api/v1/session` (web/agentbox-web).

The Host keeps only the token's SHA-256: one file per link in SIGN_IN_LINKS, named by that
hash and holding when it expires, LIFETIME seconds after it was made. The directory is the
Control Room's (agentbox-web), group libvirt, mode 1730, made by the package's tmpfiles entry:
the members of libvirt can add a file but not list the directory, and remove or replace only their own (sticky bit); nobody else can reach it.
The Control Room takes a link once (take()): it removes the file before it signs anyone in,
so a second use, an expired link or an unknown token is refused like a wrong password.

A leaf module: the Control Room imports it (tests/test_control_room_v1.py).
"""
import datetime
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time

from . import hostsetup, paths, times
from .commands import declare
from .errors import die

LIFETIME = 120                   # seconds a link is valid
FRAGMENT = "link"                # /sign-in#link=<token>
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")       # secrets.token_urlsafe(32)
HASH = re.compile(r"[0-9a-f]{64}")
MAX_FILE = 256
LIBVIRT = "libvirt"
# the Host's groups (tests: under AGENTBOX_ROOT); libvirt is a local group (systemd-sysusers)
GROUP = paths.host("/etc/group")


def digest(token):
    """What the Host keeps of a token: its SHA-256, in hex (the name of its file)."""
    return hashlib.sha256(token.encode()).hexdigest()


def _path(token):
    return os.path.join(paths.SIGN_IN_LINKS, digest(token))


# --- who may make one -------------------------------------------------------------

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


def check_caller():
    """Root, or a member of libvirt in this login (a new membership counts from the next
    one); anyone else is refused before anything is written. The directory's permissions say
    the same."""
    if os.geteuid() == 0:
        return
    gid = _libvirt_gid()
    if gid is None or gid not in {os.getegid(), *os.getgroups()}:
        die("sign-in-link is for root and the members of libvirt, who control every VM anyway: "
            "sudo agentbox sign-in-link", error="sudo_failed", command="sign-in-link")


# --- the links ----------------------------------------------------------------------

def create(now=None):
    """A new link: (token, epoch seconds it expires at). Only its hash is written."""
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    expires = int(now) + LIFETIME
    path = _path(token)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    except FileNotFoundError:
        die(f"{paths.SIGN_IN_LINKS} is missing: the package makes it at every boot "
            "(sudo systemd-tmpfiles --create agentbox.conf makes it now)", error="action_failed",
            path=paths.SIGN_IN_LINKS)
    except OSError as e:
        die(f"{paths.SIGN_IN_LINKS}: {e.strerror}", error="action_failed", path=paths.SIGN_IN_LINKS)
    try:
        os.fchmod(fd, 0o644)         # the Control Room reads it; nobody else reaches the directory
        os.write(fd, json.dumps({"expires": expires}).encode() + b"\n")
    except OSError as e:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        die(f"{paths.SIGN_IN_LINKS}: {e.strerror}", error="action_failed", path=paths.SIGN_IN_LINKS)
    os.close(fd)
    return token, expires


def _read(path):
    """(expires or None, mtime) of a link's file; None when it cannot be read as one."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, st.st_mtime
        data = os.read(fd, MAX_FILE)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        expires = json.loads(data)["expires"]
    except (ValueError, TypeError, KeyError):
        return None, st.st_mtime
    if isinstance(expires, bool) or not isinstance(expires, int):
        return None, st.st_mtime
    return expires, st.st_mtime


def sweep(now):
    """Remove the links that expired, and files no link can be any longer (a broken one older
    than LIFETIME: one just being written is left alone). Only the directory's owner (the
    Control Room) and root can list it."""
    try:
        names = os.listdir(paths.SIGN_IN_LINKS)
    except OSError:
        return
    for name in names:
        if not HASH.fullmatch(name):
            continue
        path = os.path.join(paths.SIGN_IN_LINKS, name)
        found = _read(path)
        if found is None:
            continue
        expires, mtime = found
        if expires is not None and expires <= now or expires is None and mtime + LIFETIME < now:
            try:
                os.unlink(path)
            except OSError:
                pass


def take(token, now=None):
    """Whether token is a link that is neither used nor expired. A link is used up by this,
    whatever the answer: its file is removed before it counts, so of two requests with the same
    token at most one gets True. Expired links go with it (sweep)."""
    now = time.time() if now is None else now
    sweep(now)
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return False
    path = _path(token)
    found = _read(path)
    if found is None:
        return False
    try:
        os.unlink(path)
    except OSError:
        return False                 # another request took it first
    expires, _ = found
    return expires is not None and now < expires


def url(setup, token):
    """The link: the Control Room's address for the Host setup's mode, the token in the fragment."""
    return f"https://{setup['bind']}:{setup['web_port']}/sign-in#{FRAGMENT}={token}"


# --- the command ----------------------------------------------------------------------

def sign_in_link(args):
    """agentbox sign-in-link [--json]"""
    check_caller()
    setup = hostsetup.load()
    token, expires = create()
    link = url(setup, token)
    if args.values.get("json"):
        print(json.dumps({"url": link, "expires": times.iso(expires), "mode": setup["mode"]}))
        return
    until = datetime.datetime.fromtimestamp(expires).strftime("%H:%M:%S")
    print(link)
    print(f"agentbox: valid once, for {LIFETIME // 60} minutes (until {until}): open it in the browser to sign in",
          file=sys.stderr)


DECLARATIONS = {
    "sign-in-link": declare(sign_in_link, "agentbox sign-in-link [--json]", options={"json": False},
                            needs_root=False, positional=False, kind="credentials",
                            describe=lambda a: ("Create a sign-in link for the Control Room", {})),
}
