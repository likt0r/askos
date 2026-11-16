"""Recipes: Images the owner defines on the Host as checked tar files (ADR 0018).

The current Recipe <name> is CONF/recipes/<name>/recipe.tar. Every save moves the
previous tar into versions/ and keeps the newest ten. A Recipe is never unpacked on
the Host: Builds give its tar to podman on stdin (images.py).
"""
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import tarfile
import time

from . import activity, images, options, paths, state_files, times
from .commands import declare
from .errors import Fail, die
from .sanitize import clean

RECIPES = os.path.join(paths.CONF, "recipes")
VERSION = re.compile(r"[0-9]{8}-[0-9]{6}")
VERSIONS_KEPT = 10
MEMBERS_MAX = 512
CONTENT_MAX = 64 * 1024 * 1024
INPUT_MAX = CONTENT_MAX + 1024 * 1024
CONTAINERFILE_MAX = 256 * 1024
BAD_MODE = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX


# --- checking and packing -----------------------------------------------------

def _member_name(name):
    return clean(str(name), False)[:200]


def _invalid(member, problem):
    where = f"member {_member_name(member)!r}" if member is not None else "the tar"
    params = {"member": _member_name(member)} if member is not None else {}
    die(f"invalid Recipe: {where}: {problem}", error="invalid_recipe", **params)


def _containerfile_instruction(data, member):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _invalid(member, "Containerfile is not UTF-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        instruction = line.split(None, 1)[0].upper()
        if instruction == "ARG":
            continue
        if not re.fullmatch(r"(?i:FROM)[ \t]+(?:\$\{BASE\}|\$BASE)(?:[ \t]+(?i:AS)[ \t]+\S+)?", line):
            _invalid(member, "its first instruction other than ARG must be FROM ${BASE}"
                             " (or FROM $BASE, with an optional AS stage)")
        return
    _invalid(member, "it has no FROM ${BASE}")


def _hash_member(archive, member, keep=False):
    """Hash one regular file; only Containerfile content is kept and interpreted."""
    stream = archive.extractfile(member)
    if stream is None:
        _invalid(member.name, "regular file has no content")
    digest, chunks, size = hashlib.sha256(), [], 0
    try:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            if keep:
                chunks.append(chunk)
    except (tarfile.TarError, EOFError, OSError):
        _invalid(member.name, "file content is truncated")
    finally:
        stream.close()
    if size != member.size:
        _invalid(member.name, "file content is truncated")
    return digest.hexdigest(), b"".join(chunks) if keep else None


def check(data):
    """Check one Recipe tar and return its members and SHA-256.

    Only regular files and directories are accepted. File contents are read solely to
    hash them; only the Containerfile's bounded content is decoded or interpreted.
    Nothing is extracted to the filesystem.
    """
    if not isinstance(data, bytes):
        data = bytes(data)
    if len(data) > INPUT_MAX:
        _invalid(None, f"more than {INPUT_MAX} bytes (64 MiB of content plus tar overhead)")
    tar_sha256 = hashlib.sha256(data).hexdigest()
    members, seen, total, containerfile = [], set(), 0, None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                if len(members) >= MEMBERS_MAX:
                    _invalid(member.name, f"more than {MEMBERS_MAX} members")
                name = member.name
                norm = posixpath.normpath(name)
                if not name:
                    _invalid(name, "empty name")
                if name.startswith("/"):
                    _invalid(name, "absolute name")
                if "\\" in name or "\0" in name:
                    _invalid(name, "backslash or NUL in its name")
                if ".." in name.split("/"):
                    _invalid(name, "'..' component in its name")
                if norm in seen:
                    _invalid(name, f"duplicate normalized name {norm!r}")
                seen.add(norm)
                if not member.isfile() and not member.isdir():
                    _invalid(name, "not a regular file or directory (links, devices and fifos are refused)")
                if member.mode & BAD_MODE:
                    _invalid(name, "setuid, setgid or sticky mode bits")
                total += member.size
                if total > CONTENT_MAX:
                    _invalid(name, "total member sizes exceed 64 MiB")
                if norm == "Containerfile":
                    if not member.isfile():
                        _invalid(name, "Containerfile is not a top-level regular file")
                    if member.size > CONTAINERFILE_MAX:
                        _invalid(name, "Containerfile exceeds 256 KiB")
                    containerfile = member
                digest, content = _hash_member(archive, member, member is containerfile) \
                    if member.isfile() else (None, None)
                members.append({"name": norm, "size": member.size, "sha256": digest,
                                "mode": member.mode, "kind": "file" if member.isfile() else "directory"})
                if member is containerfile:
                    _containerfile_instruction(content, norm)
    except (tarfile.TarError, EOFError, UnicodeError, ValueError) as e:
        _invalid(None, f"not a readable plain uncompressed tar ({type(e).__name__})")
    if containerfile is None:
        _invalid("Containerfile", "no top-level regular file named Containerfile")
    return {"members": members, "sha256": tar_sha256}


def pack(directory):
    """A reproducible, normalized plain tar of a directory, checked again."""
    if os.path.islink(directory) or not os.path.isdir(directory):
        _invalid(directory, "not a directory (or itself a symbolic link)")
    root = os.path.abspath(directory)
    entries = []

    def walk(folder, prefix=""):
        try:
            found = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError as e:
            die(f"{folder}: {e.strerror}", error="action_failed")
        for entry in found:
            rel = prefix + entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as e:
                die(f"{entry.path}: {e.strerror}", error="action_failed")
            if stat.S_ISLNK(info.st_mode):
                _invalid(rel, "symbolic link")
            if stat.S_ISDIR(info.st_mode):
                entries.append((rel + "/", info, entry.path))
                walk(entry.path, rel + "/")
            elif stat.S_ISREG(info.st_mode):
                entries.append((rel, info, entry.path))
            else:
                _invalid(rel, "not a regular file or directory")
            if len(entries) > MEMBERS_MAX:
                _invalid(rel, f"more than {MEMBERS_MAX} members")

    walk(root)
    out = io.BytesIO()
    try:
        with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for rel, info, full in sorted(entries, key=lambda item: item[0]):
                member = tarfile.TarInfo(rel)
                member.uid = member.gid = 0
                member.uname = member.gname = "root"
                member.mtime = 0
                if stat.S_ISDIR(info.st_mode):
                    member.type = tarfile.DIRTYPE
                    member.mode = 0o755
                    member.size = 0
                    archive.addfile(member)
                    continue
                member.type = tarfile.REGTYPE
                member.mode = 0o755 if info.st_mode & 0o111 else 0o644
                member.size = info.st_size
                with open(full, "rb") as source:
                    archive.addfile(member, source)
    except (OSError, ValueError) as e:
        _invalid(directory, f"cannot be packed ({e})")
    data = out.getvalue()
    check(data)
    return data


# --- storage ------------------------------------------------------------------

def _name(name):
    if not isinstance(name, str) or not images.NAME.fullmatch(name) \
            or name == images.BASE or name in images.TARGETS:
        die("invalid Recipe name (an Image name other than base, all or update): "
            + clean(str(name), False)[:60], error="invalid_argument", name=clean(str(name), False)[:60])
    return name


def _new_name(name):
    _name(name)
    if name in (images.release_defined() or []):
        die(f"{name}: an Image of the installed Release has this name", error="image_exists", image=name)
    return name


def _folder(name):
    return os.path.join(RECIPES, name)


def _tar(name):
    return os.path.join(_folder(name), "recipe.tar")


def _version_tar(name, version):
    return os.path.join(_folder(name), "versions", version + ".tar")


def names():
    """The Recipes stored on the Host, sorted, including one a later Release shadows."""
    return [name for name in state_files.listdir(RECIPES, fail=False)
            if images.NAME.fullmatch(name) and name != images.BASE and name not in images.TARGETS
            and os.path.isdir(_folder(name)) and os.path.lexists(_tar(name))]


def _version(version):
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        die("invalid Recipe version (YYYYmmdd-HHMMSS): " + clean(str(version), False)[:60],
            error="invalid_argument", version=clean(str(version), False)[:60])
    return version


def _stored_names(name):
    folder = os.path.join(_folder(name), "versions")
    return [n[:-4] for n in state_files.listdir(folder, fail=False)
            if n.endswith(".tar") and VERSION.fullmatch(n[:-4])]


def _read_bytes(path, not_found):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NOCTTY", 0))
    except FileNotFoundError:
        if not_found is None:
            return None
        die(not_found[0], error=not_found[1], **not_found[2])
    except OSError as e:
        die(f"{path}: {e.strerror or type(e).__name__}", error="action_failed")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            die(f"{path}: not a regular file", error="invalid_recipe")
        chunks, size = [], 0
        while size <= INPUT_MAX:
            chunk = os.read(fd, INPUT_MAX + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as e:
        die(f"{path}: {e.strerror or type(e).__name__}", error="action_failed")
    finally:
        os.close(fd)
    return b"".join(chunks)


def _load(name, version=None):
    """A stored tar and its checked summary, for managing a Recipe (even a shadowed one)."""
    path = _tar(name) if version is None else _version_tar(name, version)
    missing = (f"Recipe {name} has no kept version {version}", "version_not_found",
               {"name": name, "version": version}) if version is not None else \
        (f"No Recipe {name}", "recipe_not_found", {"name": name})
    data = _read_bytes(path, missing)
    return data, check(data)


def read(name):
    """The current checked Recipe tar; refuse a missing, broken or Release-shadowed one."""
    _new_name(name)
    with state_files.locked(_folder(name)):
        return _load(name)[0]


def sha256(name):
    """The SHA-256 of the current checked Recipe tar."""
    return hashlib.sha256(read(name)).hexdigest()


def _save(name, data, summary, always=False):
    """Save a checked tar; (created, changed), changed False for an identical recipe-set."""
    with state_files.locked(_folder(name)):
        current = _read_bytes(_tar(name), None)
        if not always and current is not None and hashlib.sha256(current).hexdigest() == summary["sha256"]:
            return False, False
        created = current is None
        archived = None
        state_files.directory(RECIPES, 0o700)
        state_files.directory(_folder(name), 0o700)
        if current is not None:
            versions = os.path.join(_folder(name), "versions")
            state_files.directory(versions, 0o700)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            while os.path.lexists(_version_tar(name, stamp)):
                time.sleep(1)
                stamp = time.strftime("%Y%m%d-%H%M%S")
            os.replace(_tar(name), _version_tar(name, stamp))
            archived = stamp
            os.chmod(_version_tar(name, stamp), 0o600)
            for old in sorted(_stored_names(name), reverse=True)[VERSIONS_KEPT:]:
                state_files.remove(_version_tar(name, old), fail=False)
        try:
            state_files.write_atomic(_tar(name), data, 0o600)
        except BaseException:
            if archived is not None and not os.path.lexists(_tar(name)):
                os.replace(_version_tar(name, archived), _tar(name))
            raise
        return created, True


def _member_bytes(data, summary, wanted):
    """The bytes of one checked member, at most CONTAINERFILE_MAX + 1 bytes."""
    wanted = posixpath.normpath(wanted)
    found = next((m for m in summary["members"] if m["name"] == wanted), None)
    if found is None:
        die(f"Recipe has no member {clean(wanted, False)}", error="invalid_argument", file=wanted)
    if found["kind"] != "file":
        die(f"{clean(wanted, False)} is not a regular file", error="invalid_argument", file=wanted)
    if found["size"] > CONTAINERFILE_MAX:
        die(f"{clean(wanted, False)} is over 256 KiB", error="invalid_argument", file=wanted)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            member = next(m for m in archive if posixpath.normpath(m.name) == wanted)
            stream = archive.extractfile(member)
            content = stream.read(CONTAINERFILE_MAX + 1)
            stream.close()
    except (tarfile.TarError, StopIteration, EOFError, OSError):
        die(f"{clean(wanted, False)} cannot be read", error="invalid_recipe", member=wanted)
    if len(content) != found["size"]:
        die(f"{clean(wanted, False)} is truncated", error="invalid_recipe", member=wanted)
    return wanted, content


def containerfile(name):
    """The current Recipe's Containerfile, read from its checked current tar."""
    data = read(name)
    return _member_bytes(data, check(data), "Containerfile")[1]


# --- command helpers ----------------------------------------------------------

def _recipe_summary(name, data, summary):
    versions = len(_stored_names(name))
    _, users = images.pinned()
    return {"name": name, "sha256": summary["sha256"], "size": len(data),
            "member_count": len(summary["members"]), "saved": times.iso(os.stat(_tar(name)).st_mtime),
            "versions": versions, "vms": sorted(users.get(name, [])), "current": images.current(name)}


def _activity_params(name, digest=None, version=None, **extra):
    params = {k: v for k, v in {"name": name if isinstance(name, str) and images.NAME.fullmatch(name) else None,
                                "sha256": digest, "version": version}.items() if v}
    params.update(extra)
    return params


def _record(action, name, digest, description, *, version=None, outcome="ok", failure=None, **extra):
    fields = {"outcome": outcome, "params": _activity_params(name, digest, version, **extra),
              "error": failure.error if failure else None,
              "error_params": failure.params if failure else None}
    # Constant actions keep every recorded action visible to the Control Room's contract test.
    if action == "recipe.set":
        activity.record("build", "recipe.set", description, **fields)
    elif action == "recipe.restore":
        activity.record("build", "recipe.restore", description, **fields)
    else:
        activity.record("build", "recipe.remove", description, **fields)


def _failed(action, name, digest, text, failure, version=None):
    _record(action, name, digest, f"{text}: {failure.message.strip()}", version=version,
            outcome="failed", failure=failure)


def _version_listing(name, version, data, summary, newer=None, newer_summary=None):
    when = int(time.mktime(time.strptime(version, "%Y%m%d-%H%M%S")))
    item = {"version": version, "time": times.iso(when), "sha256": summary["sha256"], "size": len(data),
            "against": newer or "current"}
    if newer_summary is not None:
        before = {m["name"]: m for m in newer_summary["members"] if m["kind"] == "file"}
        mine = {m["name"]: m for m in summary["members"] if m["kind"] == "file"}
        item.update(added=sorted(set(before) - set(mine)), removed=sorted(set(mine) - set(before)),
                    changed=sorted(n for n in set(mine) & set(before)
                                   if mine[n]["sha256"] != before[n]["sha256"]))
    return item


# --- commands -----------------------------------------------------------------

def recipe_pack_cmd(args):
    """agentbox recipe-pack <directory>: a reproducible Recipe tar on stdout."""
    positional, _ = options.parse(args)
    if len(positional) != 1:
        die("recipe-pack <directory>", error="invalid_argument")
    if sys.stdout.isatty():
        die("recipe-pack writes a binary tar; redirect the tar to a file or pipe it into "
            "sudo agentbox recipe-set <name>", error="invalid_argument")
    data = pack(positional[0])
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def recipe_set_cmd(args):
    """agentbox recipe-set <name> [--json]: create or replace a Recipe from stdin."""
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("recipe-set <name> [--json]", error="invalid_argument")
    name, digest = positional[0], None
    try:
        _new_name(name)
        data = sys.stdin.buffer.read(INPUT_MAX + 1)
        digest = hashlib.sha256(data).hexdigest()
        summary = check(data)
        created, changed = _save(name, data, summary)
        if not changed:
            message = f"Recipe {name} unchanged ({digest}); no new version"
            result = {"name": name, "sha256": digest, "created": False, "changed": False,
                      "unchanged": True, "message": message}
        else:
            what = "created" if created else "changed"
            message = f"Recipe {name} {what} ({digest})"
            result = {"name": name, "sha256": digest, "created": created, "changed": True,
                      "unchanged": False, "message": message}
            _record("recipe.set", name, digest, f"Recipe {name} {what}", created=created)
    except Fail as failure:
        _failed("recipe.set", name, digest, f"Set Recipe {name}", failure)
        raise
    if "json" in opts:
        print(json.dumps(result))
    else:
        print(message)


def recipes_cmd(args):
    """agentbox recipes [--json]: every Recipe, its use and current Build."""
    positional, opts = options.parse(args)
    if positional:
        die("recipes [--json]", error="invalid_argument")
    result = []
    for name in names():
        with state_files.locked(_folder(name)):
            data, summary = _load(name)
            result.append(_recipe_summary(name, data, summary))
    if "json" in opts:
        print(json.dumps({"recipes": result}, ensure_ascii=False))
        return
    if not result:
        print("No Recipes.")
    for recipe in result:
        print(f"Recipe {recipe['name']}: sha256 {recipe['sha256']}, {times.size(recipe['size'])}, "
              f"{recipe['member_count']} members, saved {recipe['saved']}, {recipe['versions']} kept versions, "
              f"VMs: {', '.join(recipe['vms']) or '-'}, current Build: {recipe['current'] or '-'}")


def recipe_show_cmd(args):
    """agentbox recipe-show <name> [--version V] [--file PATH] [--json]."""
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("recipe-show <name> [--version V] [--file PATH] [--json]", error="invalid_argument")
    name = _name(positional[0])
    version = _version(opts["version"]) if "version" in opts else None
    with state_files.locked(_folder(name)):
        data, summary = _load(name, version)
        if "file" not in opts:
            result = {"name": name, "version": version, "sha256": summary["sha256"],
                      "size": len(data), "members": summary["members"]}
        else:
            wanted, content = _member_bytes(data, summary, opts["file"])
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                die(f"{clean(wanted, False)} is not UTF-8 text", error="invalid_argument", file=wanted)
            info = next(m for m in summary["members"] if m["name"] == wanted)
            result = {"name": name, "version": version, "file": wanted, "sha256": info["sha256"],
                      "size": len(content), "text": text}
    if "json" in opts:
        print(json.dumps(result, ensure_ascii=False))
    elif "file" in opts:
        sys.stdout.write(result["text"] if not result["text"] or result["text"].endswith("\n")
                         else result["text"] + "\n")
    else:
        print(f"Recipe {name}" + (f" version {version}" if version else "") + f": {summary['sha256']}")
        for member in summary["members"]:
            print(f"{member['kind']:<9} {member['mode']:04o} {member['size']:>10} "
                  f"{member['sha256'] or '-':<64} {member['name']}")


def recipe_versions_cmd(args):
    """agentbox recipe-versions <name> [--json]: kept versions newest first and their diffs."""
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("recipe-versions <name> [--json]", error="invalid_argument")
    name = _name(positional[0])
    result = []
    with state_files.locked(_folder(name)):
        _, current_summary = _load(name)
        previous_summary, previous_version = current_summary, None
        for version in sorted(_stored_names(name), reverse=True):
            data, summary = _load(name, version)
            result.append(_version_listing(name, version, data, summary, previous_version, previous_summary))
            previous_summary, previous_version = summary, version
    if "json" in opts:
        print(json.dumps({"name": name, "current_sha256": current_summary["sha256"],
                          "versions": result}, ensure_ascii=False))
        return
    if not result:
        print(f"Recipe {name}: no kept versions.")
    for item in result:
        print(f"{item['version']}  {item['time']}  {item['sha256']}  against {item['against']}")
        for field, mark in (("added", "+"), ("removed", "-"), ("changed", "~")):
            for member in item[field]:
                print(f"  {mark} {member}")


def recipe_restore_cmd(args):
    """agentbox recipe-restore <name> <version> [--json]: save a kept version as current."""
    positional, opts = options.parse(args)
    if len(positional) != 2:
        die("recipe-restore <name> <version> [--json]", error="invalid_argument")
    name, version, digest = positional[0], positional[1], None
    try:
        _name(name)
        _version(version)
        data, summary = _load(name, version)
        digest = summary["sha256"]
        _save(name, data, summary, always=True)
        message = f"Recipe {name} restored to {version} ({digest})"
        result = {"name": name, "version": version, "sha256": digest, "restored": True, "message": message}
        _record("recipe.restore", name, digest, f"Recipe {name} restored to {version}", version=version)
    except Fail as failure:
        _failed("recipe.restore", name, digest, f"Restore Recipe {name} version {version}", failure, version)
        raise
    if "json" in opts:
        print(json.dumps(result))
    else:
        print(message)


def recipe_remove_cmd(args):
    """agentbox recipe-remove <name> [--json]: remove a Recipe, not its Builds."""
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("recipe-remove <name> [--json]", error="invalid_argument")
    name, digest = positional[0], None
    try:
        _name(name)
        # set-image checks and saves a VM's Image holding the lock of the Images directory:
        # holding it too, no VM can take the Image between this check and the removal
        with state_files.locked(_folder(name)), state_files.locked(paths.IMAGES):
            _, summary = _load(name)
            digest = summary["sha256"]
            _, users = images.pinned()
            vms = sorted(users.get(name, []))
            if vms:
                die(f"Recipe {name}: its Image is used by {', '.join(vms)}", error="image_in_use",
                    image=name, vms=vms)
            # build --all and an Update take every Recipe at their start, before this Image's
            # Build has a record: while any Build runs or is about to, nothing is removed
            running = any(rec.get("image") == name and rec.get("status") == "running"
                          for rec in images.build_records()) or images.building()
            if running:
                die(f"Recipe {name}: a Build is running, which may build this Recipe; remove it once the Build "
                    "has ended", error="build_running", image=name)
            shutil.rmtree(_folder(name))
        message = f"Recipe {name} removed ({digest}); its Builds stay"
        result = {"name": name, "sha256": digest, "removed": True, "message": message}
        _record("recipe.remove", name, digest, f"Recipe {name} removed; its Builds stay")
    except Fail as failure:
        _failed("recipe.remove", name, digest, f"Remove Recipe {name}", failure)
        raise
    if "json" in opts:
        print(json.dumps(result))
    else:
        print(message)


DECLARATIONS = {
    "recipe-pack": declare(recipe_pack_cmd, "agentbox recipe-pack <directory>",
                            options={}, needs_root=False, read_only=True),
    "recipe-set": declare(recipe_set_cmd, "agentbox recipe-set <name> [--json]",
                           options={"json": False}, own_entries=True),
    "recipes": declare(recipes_cmd, "agentbox recipes [--json]", options={"json": False}, read_only=True),
    "recipe-show": declare(recipe_show_cmd, "agentbox recipe-show <name> [--version V] [--file PATH] [--json]",
                            options={"version": True, "file": True, "json": False}, read_only=True),
    "recipe-versions": declare(recipe_versions_cmd, "agentbox recipe-versions <name> [--json]",
                                options={"json": False}, read_only=True),
    "recipe-restore": declare(recipe_restore_cmd, "agentbox recipe-restore <name> <version> [--json]",
                               options={"json": False}, own_entries=True),
    "recipe-remove": declare(recipe_remove_cmd, "agentbox recipe-remove <name> [--json]",
                              options={"json": False}, own_entries=True),
}
