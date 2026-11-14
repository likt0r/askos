"""Folders from the Host into a VM: `agentbox import` (#47, ADR 0015).

The Host packs the folder into a tar file under /var/lib/agentbox/transfers with tarfile
(ADR 0006), attaches it to the running VM as a read-only raw disk (no filesystem; the Host
mounts nothing and the VM gets no lasting access to the Host's files, ADR 0001, ADR 0004)
and has the guest agent run agentbox-inbox in the Image, which unpacks it as the user agent
under ~/code (the Data disk). Then the disk is detached and the tar file deleted, also when
anything failed.

What is packed: of a Git repository its whole .git and what `git ls-files --cached --others
--exclude-standard` lists, so ignored files stay out (a tracked file deleted in the working
tree is skipped, its deletion is in .git); of a folder without Git everything. A .git that is
a file (a linked worktree) stops the import: its repository lies elsewhere. Repositories
inside the folder (untracked ones, submodules with a checkout) are packed by the same rule.
Symbolic links stay links; links the VM would refuse (absolute, or leading out of the
folder) and special files (sockets, FIFOs, devices) are skipped and named. Packing runs in
a child process with the rights of the user who ran `sudo agentbox import` (a root shell:
of the folder's owner), so root reads nothing for it that this user could not read.

Every import holds an exclusive lock on its tar file for its whole life and, before it makes
its file, deletes the tar files of the directory nobody holds a lock on any more (an import
that was killed hard leaves its file behind).

The VM: one that is off is started as `pair` starts it, a paused one is refused. The VM's
lifecycle lock is held from the attach to the deletion of the tar file, so no Reset, Restart,
Pause or delete comes in between. The wrapper's answer is untrusted: only two whole numbers
are taken from it, and its error output is cleaned.
"""
import fcntl
import json
import os
import pwd
import secrets
import signal
import stat
import subprocess
import sys
import tarfile
import time

from . import activity, guest, lifecycle, paths, programs, responding, state_files, times, virsh
from .errors import Fail, die
from .registry import check, dom
from .sanitize import clean

INBOX = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-inbox"
DEVICE = "/dev/disk/by-id/virtio-agentbox-inbox"     # the serial below, as the VM names it
SERIAL = "agentbox-inbox"
TARGET_DEV = "vdc"            # vda is the System disk, vdb the Data disk (generate.py)
CODE = "/var/home/agent/code"
VM_LIMIT = 1800               # the wrapper's own time limit (agentbox-inbox)
MAX_TARGET = 1024
MAX_SKIPPED = 20              # skipped paths named in the output
MAX_NUMBER = 1 << 62          # files and bytes the wrapper may report
NESTED = 16                   # repositories inside repositories, at most this deep


# --- arguments ---------------------------------------------------------------------

def target_problem(target):
    """Why `target` cannot be a path under ~/code, or None (agentbox-inbox checks the same)."""
    if not target or len(target.encode("utf-8", "surrogateescape")) > MAX_TARGET:
        return "empty or too long"
    if target.startswith("/"):
        return "an absolute path"
    parts = target.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "empty, . or .. parts"
    if any(len(p.encode("utf-8", "surrogateescape")) > 255 for p in parts):
        return "a part longer than 255 bytes"
    if clean(target, False) != target:
        return "control or invisible characters"
    return None


def folder_path(given):
    """The folder as an absolute path (as given when the current directory is gone)."""
    try:
        return os.path.abspath(given)
    except OSError:
        return given


def target_for(folder, given):
    """The target as given (without a trailing slash), else the folder's name; not checked."""
    if given:
        return given.rstrip("/") or given
    return os.path.basename(folder_path(folder).rstrip("/"))


def parse(args):
    positional, as_json = [], False
    for a in args:
        if a == "--json":
            as_json = True
        elif a.startswith("--"):
            die("unknown option: " + clean(a, False)[:60], 2, error="invalid_argument", option=clean(a, False)[:60])
        else:
            positional.append(a)
    if not positional:
        check("")
    if not 2 <= len(positional) <= 3:
        die("import <vm> <folder> [<target>] [--json]", 2, error="invalid_argument")
    ctx = check(positional[0])
    source = folder_path(positional[1])
    target = target_for(positional[1], positional[2] if len(positional) == 3 else "")
    why = target_problem(target)
    if why:
        die(f"invalid target ({why}): a relative path under ~/code such as foo or clients/foo", 2,
            error="invalid_argument", argument="target")
    return ctx, source, target, as_json


def check_source(source):
    """The folder exists and is one; whether it is a Git repository. A .git that is not a
    folder (a linked worktree, a submodule's checkout) stops the import."""
    shown = clean(source, False)
    try:
        st = os.stat(source)
    except FileNotFoundError:
        die(f"{shown}: no such folder", error="folder_not_found", folder=shown)
    except OSError as e:
        die(f"{shown}: {e.strerror}", error="folder_not_found", folder=shown)
    if not stat.S_ISDIR(st.st_mode):
        die(f"{shown} is not a folder", error="not_a_folder", folder=shown)
    git = os.path.join(source, ".git")
    try:
        st = os.lstat(git)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(st.st_mode):
        return True
    if stat.S_ISLNK(st.st_mode):
        die(f"{shown}: .git is a symbolic link; import the folder it points to, or replace the link by the "
            "repository", error="not_a_folder", folder=shown)
    main = _main_repository(source, git)
    die(f"{shown} is a linked worktree (.git is no folder); import its main repository instead"
        + (f": {clean(main, False)}" if main else ""), error="linked_worktree", folder=shown)


def _main_repository(source, git):
    """The main repository a .git file points into (…/.git/worktrees/<name>, …/.git/modules/<name>), or None."""
    try:
        with open(git, "rb") as f:
            line = f.read(4096).decode("utf-8", "replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = os.path.normpath(os.path.join(source, line[len("gitdir:"):].strip()))
    for marker in ("/.git/worktrees/", "/.git/modules/"):
        if marker in gitdir:
            return gitdir.split(marker)[0]
    return None


# --- packing (in a child process with the owner's rights) --------------------------

def pack(source, fd):
    """Write the archive of `source` to fd with the rights of `_identity`:
    {"files", "bytes", "skipped": [{"path", "reason"}], "skipped_count"}."""
    who = _identity(source)
    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:                                   # the child: never returns
        status = 1
        try:
            os.close(r)
            if os.geteuid() == 0 and who[0] != 0:
                _become(*who)
            result, status = {"packed": _archive(source, fd)}, 0
        except Fail as f:
            result = {"error": f.error, "message": f.message}
        except BaseException as e:                 # the child reports every failure
            result = {"error": "import_failed", "message": f"packing fails ({type(e).__name__}: {e})"}
        try:
            with os.fdopen(w, "w") as pipe:
                json.dump(result, pipe)
        finally:
            os._exit(status)
    os.close(w)
    try:
        with os.fdopen(r) as pipe:
            data = pipe.read()
    except BaseException:                          # an interrupted parent leaves no child behind
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise
    os.waitpid(pid, 0)
    try:
        result = json.loads(data)
    except ValueError:
        die("packing the folder fails", error="import_failed")
    if "error" in result:
        raise Fail(result["message"], error=result["error"])
    return result["packed"]


def _identity(source):
    """(uid, gid) packing runs as: the user who ran `sudo` (SUDO_UID, SUDO_GID), so they can
    send only what they could read themselves; in a root shell the owner of the folder."""
    try:
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
        if uid > 0 and gid >= 0:
            pwd.getpwuid(uid)
            return uid, gid
    except (KeyError, ValueError, OverflowError):
        pass
    owner = os.stat(source)
    return owner.st_uid, owner.st_gid


def _become(uid, gid):
    """The rights of this user: their groups and home (git reads its configuration there)."""
    try:
        pw = pwd.getpwuid(uid)
        groups, home = os.getgrouplist(pw.pw_name, gid), pw.pw_dir
    except KeyError:
        groups, home = [gid], "/"
    os.setgroups(groups)
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = home


def _archive(source, fd):
    skipped, counts = [], {"files": 0, "bytes": 0}
    with os.fdopen(fd, "wb", closefd=False) as out, \
            tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(_with_parents(_members(source, ""))):
            _add(tar, source, rel, skipped, counts)
    return {**counts, "skipped": skipped[:MAX_SKIPPED], "skipped_count": len(skipped)}


def _with_parents(paths_):
    out = set(paths_)
    for p in paths_:
        parent = os.path.dirname(p)
        while parent and parent not in out:
            out.add(parent)
            parent = os.path.dirname(parent)
    return out


def _members(source, rel, depth=0):
    """The relative paths below `rel` to pack: a repository's .git and what Git lists,
    else everything."""
    here = os.path.join(source, rel)
    git = os.path.join(here, ".git")
    try:
        st = os.lstat(git)
    except FileNotFoundError:
        return _everything(source, rel)
    if depth > NESTED:
        die(f"{clean(here, False)}: repositories nested too deep", error="import_failed")
    found = {os.path.join(rel, ".git")}
    if stat.S_ISDIR(st.st_mode):
        found |= _everything(source, os.path.join(rel, ".git"))
    for listed in _git_listed(here):
        if listed.endswith("/"):                 # a repository Git does not descend into
            found |= _members(source, os.path.join(rel, listed.rstrip("/")), depth + 1)
            continue
        path = os.path.join(rel, listed)
        full = os.path.join(source, path)
        if os.path.isdir(full) and not os.path.islink(full):
            if os.path.lexists(os.path.join(full, ".git")):   # a submodule with its checkout
                found |= _members(source, path, depth + 1)
                continue
        found.add(path)
    return found


def _everything(source, rel):
    found = set()

    def fail(e):
        raise e
    top = os.path.join(source, rel)
    try:
        for d, dirs, files in os.walk(top, onerror=fail):
            below = os.path.relpath(d, source)
            for name in dirs + files:
                found.add(os.path.normpath(os.path.join(below, name)))
    except OSError as e:
        die(f"{clean(e.filename or top, False)}: {e.strerror}", error="import_failed")
    return found


def _git_listed(folder):
    """What `git ls-files --cached --others --exclude-standard` lists in this repository."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(["git", "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard"], cwd=folder, capture_output=True, env=dict(env, LC_ALL="C"),
                           timeout=600)
    except FileNotFoundError:
        die("git is missing on the Host", error="import_failed")
    except subprocess.TimeoutExpired:
        die(f"{clean(folder, False)}: git ls-files takes too long", error="import_failed")
    if r.returncode != 0:
        why = clean(r.stderr.decode("utf-8", "replace").strip(), False)[:300]
        die(f"{clean(folder, False)}: git ls-files fails: {why or f'exit {r.returncode}'}", error="import_failed")
    return list(dict.fromkeys(os.fsdecode(p) for p in r.stdout.split(b"\0") if p))


def _add(tar, source, rel, skipped, counts):
    path = os.path.join(source, rel)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return                                   # deleted in the working tree
    try:
        if stat.S_ISLNK(st.st_mode):
            link = os.readlink(path)
            inside = os.path.normpath(os.path.join(os.path.dirname(rel), link))
            if os.path.isabs(link) or inside == ".." or inside.startswith("../"):
                skipped.append({"path": rel, "reason": "link_outside"})
                return
        elif not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            skipped.append({"path": rel, "reason": "special_file"})
            return
        info = tar.gettarinfo(path, arcname=rel)
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode &= 0o777
        if info.isreg():
            with open(path, "rb") as f:
                tar.addfile(info, f)
            counts["files"] += 1
            counts["bytes"] += info.size
        elif info.islnk():                       # a further hard link to a file already added
            tar.addfile(info)
            counts["files"] += 1
        else:
            tar.addfile(info)
    except OSError as e:
        die(f"{clean(path, False)}: {e.strerror or e}", error="import_failed")


# --- the VM ------------------------------------------------------------------------

def _ready(ctx):
    """The VM runs and its guest agent answers; started if it was off. The caller holds
    the VM's lifecycle lock."""
    if lifecycle.has_save(ctx):
        die(f"{ctx}: the VM is paused; start it first (agentbox start {ctx})", error="vm_paused", vm=ctx)
    if virsh.state(ctx) == "running":
        return
    responding.clear(ctx)                     # the failed wakes of its last start are over
    lifecycle._must("start", dom(ctx))
    deadline = time.monotonic() + paths.tunable("AGENTBOX_BOOT_SECONDS", 180)
    while not guest.ping(ctx):
        if time.monotonic() > deadline:
            die(f"{ctx}: the guest agent does not answer after the start", 4, error="guest_agent_unavailable", vm=ctx)
        time.sleep(2)


def _attach(ctx, tar):
    r = virsh.run("attach-disk", dom(ctx), tar, TARGET_DEV, "--live", "--mode", "readonly", "--targetbus", "virtio",
                  "--driver", "qemu", "--subdriver", "raw", "--serial", SERIAL)
    if r.returncode != 0:
        die(f"{ctx}: the folder cannot be attached to the VM: {programs.summary(r)}", error="import_failed", vm=ctx)


def _detach(ctx):
    """Detach the disk; the reason when that fails, else None."""
    r = virsh.run("detach-disk", dom(ctx), TARGET_DEV, "--live")
    if r.returncode == 0:
        return None
    return programs.summary(r)


def _missing(ctx):
    die(f"{ctx}: the VM's Build has no agentbox-inbox; build its Image and reset the VM first "
        f"(agentbox build <image> --reset {ctx})", error="inbox_missing", vm=ctx)


def _receive(ctx, target):
    """Run agentbox-inbox in the VM; (exit code, stdout, stderr) once it ended, 124 when it
    did not within the limit."""
    limit = paths.tunable("AGENTBOX_IMPORT_SECONDS", VM_LIMIT + 60)
    code, out, err, _ = guest.execute(ctx, INBOX, ["receive", DEVICE, target], limit,
                                      missing=lambda: _missing(ctx))
    return code, out, err


def _result(ctx, target, code, out, err):
    """{"files", "bytes"} from the wrapper's answer, which comes from the VM: untrusted."""
    why = clean(err.strip().removeprefix("agentbox-inbox: "), False)[:300] or f"exit {code}"
    if code == 3:
        die(f"{ctx}: ~/code/{target} exists already in the VM; nothing was changed ({why})", error="target_exists",
            vm=ctx, target=target)
    if code == 2:
        die(f"{ctx}: the VM refuses the target ~/code/{target}: {why}", 2, error="invalid_argument", vm=ctx,
            argument="target")
    if code == 124:
        die(f"{ctx}: unpacking in the VM did not end within its time limit", error="import_failed", vm=ctx)
    if code != 0:
        die(f"{ctx}: unpacking in the VM fails: {why}", error="import_failed", vm=ctx)
    numbers = guest.numbers(out, ("files", "bytes"), MAX_NUMBER)
    if not numbers:
        die(f"{ctx}: unexpected answer from agentbox-inbox in the VM", error="import_failed", vm=ctx)
    return {"files": numbers[0], "bytes": numbers[1]}


def _clear_leftover(ctx):
    """Detach a disk with the target of the inbox that an interrupted import left behind."""
    r = virsh.run("domblklist", dom(ctx))
    if r.returncode == 0 and TARGET_DEV in (line.split()[0] for line in r.stdout.splitlines() if line.split()):
        problem = _detach(ctx)
        if problem:
            die(f"{ctx}: the disk {TARGET_DEV} of an earlier import could not be detached: {problem}",
                error="import_failed", vm=ctx)


def _transfer(ctx, tar, target):
    """Attach the tar file, have the VM unpack it, detach it: {"files", "bytes"}."""
    _clear_leftover(ctx)
    try:
        _attach(ctx, tar)
        answer = _receive(ctx, target)
    except BaseException:
        problem = _detach(ctx)          # also after an interrupted attach; no such disk is no problem
        if problem and "No disk found" not in problem:
            print(f"agentbox: {ctx}: the disk {TARGET_DEV} could not be detached: {problem}", file=sys.stderr)
        raise
    problem = _detach(ctx)
    try:
        result = _result(ctx, target, *answer)
    except Fail:
        if problem:
            print(f"agentbox: {ctx}: the disk {TARGET_DEV} could not be detached: {problem}", file=sys.stderr)
        raise
    if problem:
        activity.result(**result)       # the folder arrived: the entry says what
        die(f"{ctx}: the folder is in ~/code/{target}, but the disk {TARGET_DEV} could not be detached: {problem}",
            error="import_failed", vm=ctx)
    return result


def _sweep(own=None):
    """Delete the tar files nobody holds a lock on any more (their import was killed hard)."""
    try:
        names = os.listdir(paths.TRANSFERS)
    except OSError:
        return
    for name in names:
        full = os.path.join(paths.TRANSFERS, name)
        if not name.endswith(".tar") or full == own:
            continue
        try:
            fd = os.open(full, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.unlink(full)
        except OSError:
            pass                                 # locked by a running import, or gone
        finally:
            os.close(fd)


def _create_locked(ctx):
    """A new tar file, held under an exclusive lock: (path, fd). A sweep that locked the file
    between its creation and our lock removed it: then another file."""
    while True:
        tar = os.path.join(paths.TRANSFERS, f"{ctx}-{secrets.token_hex(6)}.tar")
        fd = os.open(tar, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.path.samestat(os.fstat(fd), os.stat(tar)):
                return tar, fd
        except FileNotFoundError:
            pass
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)


def _delete(tar):
    """Remove the tar file if it is there; a failure is only a warning."""
    try:
        state_files.remove(tar, fail=False)
    except OSError as e:
        print(f"agentbox: {tar}: {e.strerror}", file=sys.stderr)


def _interrupted(signum, frame):
    raise KeyboardInterrupt


def import_cmd(args):
    """agentbox import <vm> <folder> [<target>] [--json]"""
    ctx, source, target, as_json = parse(args)
    check_source(source)
    if lifecycle.has_save(ctx):
        die(f"{ctx}: the VM is paused; start it first (agentbox start {ctx})", error="vm_paused", vm=ctx)
    try:
        state_files.directory(paths.TRANSFERS, 0o711)      # QEMU must reach the file (it runs as qemu)
        _sweep()
        tar, fd = _create_locked(ctx)
    except OSError as e:
        die(f"{paths.TRANSFERS}: {e.strerror}", error="import_failed", vm=ctx)
    # a stopped command still detaches the disk and deletes the file
    handlers = {s: signal.signal(s, _interrupted) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        packed = pack(source, fd)
        # from the attach to the deletion: no Reset, Restart, Pause or delete in between
        with state_files.vm_lock(ctx):
            try:
                _ready(ctx)
                result = _transfer(ctx, tar, target)
            finally:
                _delete(tar)
    finally:
        for s, h in handlers.items():
            signal.signal(s, h)
        _delete(tar)
        os.close(fd)                              # the lock ends with the file gone
    activity.result(**result)
    shown = clean(source, False)
    skipped = [{"path": clean(s["path"], False), "reason": s["reason"]} for s in packed["skipped"]]
    if as_json:
        print(json.dumps({"vm": ctx, "folder": shown, "target": target, "path": f"{CODE}/{target}", **result,
                          "skipped": skipped, "skipped_count": packed["skipped_count"]}, ensure_ascii=False))
        return
    print(f"{ctx}: {shown} is in ~/code/{target} ({result['files']} files, {times.size(result['bytes'], 'GiB')})")
    if packed["skipped_count"]:
        named = ", ".join(s["path"] for s in skipped)
        more = packed["skipped_count"] - len(skipped)
        print(f"skipped {packed['skipped_count']} (links leading out of the folder, special files): {named}"
              + (f" and {more} more" if more else ""))


from .commands import declare

DECLARATIONS = {
    "import": declare(import_cmd, "agentbox import <vm> <folder> [<target>] [--json]",
                       options={"json": False}, describe=activity._import, order=60),
}
