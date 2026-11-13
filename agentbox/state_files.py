"""Small per-VM state files that several commands read."""
import contextlib
import fcntl
import os
import re
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
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")
    return [l for l in lines(data) if l and not l.startswith(b"#")]


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


def opencode_key_file(ctx):
    return os.path.join(paths.CONF, "secrets", f"opencode-{ctx}")


def vm_env_file(ctx):
    """The VM variables as QEMU passes them in (fw_cfg opt/agentbox/env, vmenv.py)."""
    return os.path.join(paths.STATE, "vm-env", f"{ctx}.env")


def _stored_since(f, prefix):
    try:
        with open(f, "rb") as fh:
            if fh.read(len(prefix)) != prefix:
                return 0
        return int(os.stat(f).st_mtime)
    except OSError:
        return 0


def claude_token_since(ctx):
    """When the VM's Claude token was stored (epoch seconds), or 0 when there is none."""
    return _stored_since(claude_token_file(ctx), b"sk-ant-")


def opencode_key_since(ctx):
    """When the VM's OpenCode key was stored (epoch seconds), or 0 when there is none."""
    return _stored_since(opencode_key_file(ctx), b"sk-")
