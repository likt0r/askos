"""Small per-VM state files that several commands read, and the ways agentbox reads and
writes its files.

A file that is missing is the usual case and gives a default; any other failure ends the
command (action_failed, "<path>: <reason>", with the keyword arguments as its parameters,
path= among them if the caller wants it), or with fail=False is raised (OSError) for the
caller to handle. Owners are changed only as root: tests run as a normal user.
"""
import contextlib
import fcntl
import grp
import json
import os
import pwd
import re
import stat
import tempfile
import time

from . import paths
from .errors import die


def lines(data):
    """Lines of a file as grep sees them (bytes, without the newline)."""
    parts = data.split(b"\n")
    if parts[-1] == b"":
        parts.pop()
    return parts


def content_lines(path):
    """The lines of a list file without comment and empty lines (bytes); [] when missing."""
    return [l for l in lines(read(path, b"")) if l and not l.startswith(b"#")]


_FAIL = object()


def _failed(path, e, fail, params):
    if not fail:
        raise e
    die(f"{path}: {e.strerror}", error="action_failed", **params)


def read(path, default=_FAIL, /, **params):
    """The file's content (bytes); default when it is missing. Without a default a missing
    file fails like any other failure: the command ends (action_failed, with params)."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError as e:
        if default is _FAIL:
            _failed(path, e, True, params)
        return default
    except OSError as e:
        _failed(path, e, True, params)


def read_json(path):
    """The JSON value in the file (UTF-8); None when it is missing, unreadable or no JSON."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def json_object(path, default=None):
    """The JSON object (a dict) in the file; default when it is missing, unreadable, no
    JSON or no object."""
    value = read_json(path)
    return value if isinstance(value, dict) else default


def write_json(path, value, mode=0o644, *, indent=2, sort_keys=False, ensure_ascii=True):
    """value as JSON with a final newline, replacing the file atomically (write_atomic).
    indent, sort_keys and ensure_ascii as json.dumps takes them: each file keeps its own."""
    data = json.dumps(value, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii) + "\n"
    write_atomic(path, data.encode(), mode)


def write_if_changed(path, data, mode=0o644, gid=-1, /, *, before=None, fail=True, **params):
    """Replace the file (write_atomic) when its content (bytes), its mode or, when gid is
    given, its group differ; whether it did. before() runs first when it writes (a marker,
    so a run that dies before applying the file leaves it for the next one)."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            if fh.read() == data and stat.S_IMODE(st.st_mode) == mode and gid in (-1, st.st_gid):
                return False
    except FileNotFoundError:
        pass
    except OSError as e:
        _failed(path, e, fail, params)
    if before:
        before()
    write_atomic(path, data, mode, gid)
    return True


def remove(path, /, *, fail=True, **params):
    """Remove the file if it is there; whether it was."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        _failed(path, e, fail, params)


def listdir(path, /, *, fail=True, **params):
    """The names in the directory, sorted; [] when it is missing."""
    try:
        return sorted(os.listdir(path))
    except FileNotFoundError:
        return []
    except OSError as e:
        _failed(path, e, fail, params)


def directory(path, mode, gid=-1, *, only_new=False):
    """Make path a directory, with any missing parents; True when it was created. It gets
    mode whatever the umask and, when gid is given, owner root and that group; an existing
    one gets them again unless only_new. Failures are raised (OSError): each caller reports
    them its own way."""
    new = not os.path.isdir(path)
    if new:
        os.makedirs(path, mode=mode & 0o700, exist_ok=True)     # nobody else in it before the chmod
    if new or not only_new:
        os.chmod(path, mode)
        if gid != -1:
            os.chown(path, 0, gid)
    return new


def user_ids(name, missing):
    """(uid, gid) of the user as root, else (-1, -1). A missing user ends the command;
    missing says why it should be there ("the package creates it")."""
    if os.geteuid() != 0:
        return -1, -1
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        die(f"user {name} is missing ({missing})", error="action_failed", user=name)
    return pw.pw_uid, pw.pw_gid


def group_id(name, missing):
    """gid of the group as root, else -1. A missing group ends the command; missing says why
    it should be there."""
    if os.geteuid() != 0:
        return -1
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        die(f"the group {name} is missing ({missing})", error="action_failed", group=name)


def _lock_fd(path):
    locks = os.path.join(paths.STATE, "locks")
    lock = os.path.join(locks, f"{os.path.basename(path)}.lock")
    try:
        os.makedirs(locks, mode=0o700, exist_ok=True)
        return os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        die(f"{lock}: {e.strerror}", error="action_failed")


@contextlib.contextmanager
def locked(path):
    """Serialise read-modify-write of `path` between agentbox calls running at the
    same time (the Control Room may send several requests at once)."""
    fd = _lock_fd(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def held(path):
    """Whether another agentbox call holds the lock of `path` (locked()) at this moment;
    never waits for it."""
    fd = _lock_fd(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


@contextlib.contextmanager
def locked_unless_held(path):
    """The lock of `path` (locked()) for the block, taken only if nobody holds it: yields True
    then, else False at once, without waiting."""
    fd = _lock_fd(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def vm_lock(name):
    """The lifecycle lock of a VM (#52). Everything that stops the VM and needs it to
    stay off (Reset, Restart, Pause, delete) holds it for its whole length; everything
    that starts it (start, wake, pair) holds it around "check the state, start". Not
    reentrant: whoever holds it calls only code that does not take it again."""
    return locked(f"lifecycle-{name}")


def write_atomic(path, data, mode=0o644, gid=-1, uid=-1):
    """Replace a file atomically: a uniquely named temporary file next to it,
    flushed to disk, then renamed over it. Mode independent of the umask; owner
    and group changed only when given (root)."""
    d, base = os.path.split(path)
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{base}.")
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            if gid != -1 or uid != -1:
                os.fchown(fh.fileno(), uid, gid)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        die(f"{path}: {e.strerror}", error="action_failed")


def open_web_file(ctx):
    return os.path.join(paths.CONF, f"open-{ctx}")


def open_until(ctx):
    """End of the VM's Open web (epoch seconds), or 0 when the web is not open."""
    try:
        with open(open_web_file(ctx)) as f:
            u = f.read().strip().split(".")[0]
    except OSError:
        return 0
    return int(u) if re.fullmatch(r"\d+", u) and int(u) > time.time() else 0


def claude_token_file(ctx):
    return os.path.join(paths.CONF, "secrets", f"claude-{ctx}")


def vm_env_file(ctx):
    """The VM variables as QEMU passes them in (fw_cfg opt/agentbox/env, vmenv.py)."""
    return os.path.join(paths.STATE, "vm-env", f"{ctx}.env")


def claude_token_since(ctx):
    """When the VM's Claude token was stored (epoch seconds), or 0 when there is none."""
    f = claude_token_file(ctx)
    try:
        with open(f, "rb") as fh:
            if fh.read(7) != b"sk-ant-":
                return 0
        return int(os.stat(f).st_mtime)
    except OSError:
        return 0
