"""Images and Builds (#17, ADR 0008, ADR 0010).

An Image is defined in the installed Release (paths.VM_SOURCE, laid out as the repository:
vm/ and the guest tools of github-app/): the Base image by vm/Containerfile (build context:
all of it), every other Image by vm/images/<name>/Containerfile (build context: its
directory), which starts from the Base image's current Build, handed in as the build
argument BASE (`ARG BASE` / `FROM ${BASE}`).

A Build runs as the systemd unit agentbox-build@<image>.service (agentbox-build@all
builds the Base image, then every other Image), whose journal is its log
(ADR 0004). `agentbox build` only starts the unit; `agentbox build-run` is what it runs:

  1. the Containerfiles and guest files of the installed Release are copied, so every
     Image of one run is built from the same files even if an update replaces them;
  2. podman builds the container image: the Base image with package_min_age_days and
     orca_version of the Settings (its Containerfile downloads Orca from its GitHub release,
     vm/build/orca-release; nothing of Orca comes from the Host), a derived Image on the
     Base image's current Build;
  3. safeguards.check reads the built filesystem (podman image mount) and discards the
     Build if a safeguard is missing or weakened; then the versions of its tools are read
     from the same files (versions.py);
  4. bootc-image-builder turns it into a disk, images/<image>-<build>.qcow2;
  5. the link images/<image>.qcow2 moves to it (one rename) and old Builds go.

Anything failing leaves the previous Build in use and is recorded in Activity (kind
build) with the reason. Builds run one at a time.

An Update (#104) is a Build of one Image followed by a Reset of the VMs the owner picked
(`agentbox build <image> --reset <vm>...`): when the Build became the current one, each
picked VM is reset, in the same unit after the Build, onto the Image's current Build at that
moment, and started again if it ran; a failed Build resets nothing. Each Reset, done, failed
or never begun, is an Activity entry (reset.update) naming the Build it used.

An update of everything (#114) runs as agentbox-build@update (build-run update): Update now
(`agentbox update-now [--reset <vm>]...`) first runs the version check (the fetch as
agentbox-feeds through its unit, then versions.check), and the version check of the Maintenance
window (maintenance.py) starts it itself when auto_update is on and an Image has an update
(start_automatic, never while a Build runs). Under the run lock it decides which Images to
build (update_images: those a new Build would update; with the Base image every Image, which
build on it), builds them, and then resets the VMs picked (Update now) onto their Image's current Build. A failed Build
resets nothing. Its Builds are recorded with the reason "update".

What a VM gets at Reset: its record's Image (`image`) at its current Build, or the
Build it is pinned to (`build`); `agentbox set-image` changes both, effective at the
next Reset. A Build stays as long as it is one of the newest builds_kept of its Image,
the current one, the backing file of any System disk or a VM's pin.

Builds made before this (images/base-<stamp>.qcow2 and the link base.qcow2) are Builds
of the Base image as they are: nothing is moved, System disks keep their backing files.

An Image's definition (#67): `agentbox containerfile <image>` shows its Containerfile from the
installed Release as text; `images` names its path and links it in the repository at the tag
of the Release (or the commit) its current Build came from. Nothing is fetched for either.
"""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

from . import activity, guest, options, paths, programs, registry, safeguards, settings, state_files, times, version
from . import versions
from .errors import Fail, die
from .sanitize import SECRET, clean, entry

BASE = "base"
NAME = settings.IMAGE                                  # also the names of default_image
BUILD_ID = settings.BUILD_ID
DISK = re.compile(rf"({NAME.pattern})-({BUILD_ID.pattern})\.qcow2")
RECORD = re.compile(rf"({NAME.pattern})-({BUILD_ID.pattern})\.json")
USABLE = ("ok", "migrated")
STATUS = {"ok": "ok", "migrated": "from before", "running": "running", "failed": "failed",
          "rejected": "rejected", "missing": "file missing"}
STEPS = ("source", "build", "check", "disk", "done")
TEMPLATE = "agentbox-build@.service"
CONTAINER = "localhost/agentbox/{}:{}"
BUILDER = "quay.io/centos-bootc/bootc-image-builder:latest"
STORAGE = "/var/lib/containers/storage"
RUN_LOCK = os.path.join(paths.STATE, "build-run")       # held for a whole Build: one at a time
REQUEST_LOCK = os.path.join(paths.STATE, "build-request")  # held while a request file is written
UPDATE = "update"           # the target of an update of everything: build-run update (#114)
TARGETS = ("all", UPDATE)   # targets of agentbox-build@ that are no Image
COMMIT = re.compile(r"[0-9a-f]{7,40}")      # of main, named by Builds from before ADR 0010
CONTAINERFILE_MAX = 64 * 1024     # bytes of a Containerfile `agentbox containerfile` shows at most
DISK_CONFIG = """\
# room for container images, node_modules, build caches (all disposable)
[[customizations.filesystem]]
mountpoint = "/"
minsize = "80 GiB"
"""


def unit(target):
    return f"agentbox-build@{target}.service"


def build_file(image, build):
    return os.path.join(paths.IMAGES, f"{image}-{build}.qcow2")


def current_link(image):
    return os.path.join(paths.IMAGES, f"{image}.qcow2")


def record_file(image, build):
    return os.path.join(paths.BUILDS, f"{image}-{build}.json")


def _order(build):
    """Sort key: Build ids of both forms in time order."""
    return re.sub(r"\D", "", build).ljust(14, "0")


def _image_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in TARGETS:
        die("invalid Image: " + clean(str(name), False)[:40], error="invalid_image",
            image=clean(str(name), False)[:40])
    return name


def _build_id(build):
    if not BUILD_ID.fullmatch(build or ""):
        die("invalid Build: " + clean(str(build), False)[:40], error="invalid_build",
            build=clean(str(build), False)[:40])
    return build


# --- what is on the Host --------------------------------------------------------------

def _records():
    out = []
    for n in state_files.listdir(paths.BUILDS, fail=False):
        m = RECORD.fullmatch(n)
        if not m:
            continue
        rec = state_files.json_object(os.path.join(paths.BUILDS, n))
        if rec is not None and rec.get("image") == m.group(1) and rec.get("build") == m.group(2):
            out.append(rec)
    return out


def _save(rec):
    state_files.directory(paths.BUILDS, 0o700, only_new=True)
    state_files.write_json(record_file(rec["image"], rec["build"]), rec, ensure_ascii=False)


def current(image):
    """The id of the Image's current Build (where images/<image>.qcow2 points), or None."""
    link = current_link(image)
    if not os.path.islink(link) or not os.path.isfile(link):
        return None
    m = DISK.fullmatch(os.path.basename(os.readlink(link)))
    return m.group(2) if m and m.group(1) == image else None


def available():
    """Images with a current Build, which a VM can use."""
    out = []
    for n in state_files.listdir(paths.IMAGES, fail=False):
        if not n.endswith(".qcow2"):
            continue
        name, path = n[:-len(".qcow2")], os.path.join(paths.IMAGES, n)
        # a link is an Image's current Build; a file named like a Build is a Build
        if NAME.fullmatch(name) and (os.path.islink(path) or not DISK.fullmatch(n)) and os.path.isfile(path):
            out.append(name)
    return out


def builds():
    """Every Build on the Host: {image: {build: info}} from the records and the disk files.
    A disk without a record was built before Builds were recorded ("migrated")."""
    out = {}
    for rec in _records():
        out.setdefault(rec["image"], {})[rec["build"]] = dict(rec, file=None, size=None)
    for n in state_files.listdir(paths.IMAGES, fail=False):
        m = DISK.fullmatch(n)
        path = os.path.join(paths.IMAGES, n)
        if not m or os.path.islink(path) or not os.path.isfile(path):
            continue
        st = os.stat(path)
        info = out.setdefault(m.group(1), {}).setdefault(m.group(2), {
            "image": m.group(1), "build": m.group(2), "status": "migrated", "started": int(st.st_mtime),
            "finished": int(st.st_mtime)})
        info["file"], info["size"] = n, st.st_size
    for image in out.values():
        for info in image.values():
            if info["status"] == "ok" and not info["file"]:
                info["status"] = "missing"
    return out


def _backing(disk):
    """Base name of the backing file of a disk; "" without one; None when unreadable."""
    info = programs.disk_info(disk)
    backing = info.get("backing-filename", "") if info is not None else None
    return os.path.basename(backing) if isinstance(backing, str) else None


def in_use():
    """{disk file name: [VMs]} of the Builds System disks are backed by; None when a
    System disk cannot be read (then nothing may be deleted)."""
    used = {}
    for n in state_files.listdir(paths.VMS, fail=False):
        m = re.fullmatch(r"(.+)-system\.qcow2", n)
        if not m:
            continue
        backing = _backing(os.path.join(paths.VMS, n))
        if backing is None:
            return None
        if backing:
            used.setdefault(backing, []).append(m.group(1))
    return used


def pinned():
    """{(image, build): [VMs]} of the VMs pinned to a Build, and {image: [VMs]} of every VM."""
    pins, users = {}, {}
    records, _ = registry.scan()
    for rec in records:
        users.setdefault(rec["image"], []).append(rec["name"])
        if rec["build"]:
            pins.setdefault((rec["image"], rec["build"]), []).append(rec["name"])
    return pins, users


def behind(rec, file, cur):
    """(Image, Build) of the current Build `cur` of the VM's Image when the VM takes it at its
    next Reset (no Build pin) and its System disk came from an earlier Build of that Image
    (`file`: the disk file of the Build it came from); else None, also when either is unknown.
    With auto_update, the Reset check of the Maintenance window moves such a VM (#114)."""
    m = DISK.fullmatch(file or "")
    if rec["build"] or not cur or not m or m.group(1) != rec["image"]:
        return None
    return (rec["image"], cur) if _order(cur) > _order(m.group(2)) else None


def disk_for(rec):
    """The Build a VM's System disk is created from at its Reset: its pin, else the
    current Build of its Image."""
    image, build = rec["image"], rec["build"]
    if build:
        path = build_file(image, build)
        info = builds().get(image, {}).get(build)
        if not info or not info["file"] or info["status"] not in USABLE:
            die(f"{rec['name']}: Build {build} of Image {image} does not exist (agentbox images)",
                error="build_not_found", vm=rec["name"], image=image, build=build)
        return path
    path = os.path.realpath(current_link(image))
    if not os.path.isfile(path):
        die(f"no Build of Image {image}, run agentbox build {image} first", error="image_has_no_build",
            image=image)
    return path


def prune(image, say):
    """Keep the newest builds_kept Builds of the Image, its current one and every Build
    a System disk is backed by or a VM is pinned to; delete the others, and the records
    of failed Builds beyond builds_kept. Returns the deleted Builds."""
    kept = settings.load()["builds_kept"]
    with state_files.locked(paths.IMAGES):
        used = in_use()
        if used is None:
            say("Old Builds stay: not every System disk is readable")
            return []
        pins, _ = pinned()
        found = builds().get(image, {})
        disks = sorted((b for b, i in found.items() if i["file"]), key=_order, reverse=True)
        keep = set(disks[:kept]) | {current(image)}
        removed = []
        for b in disks:
            if b in keep or found[b]["file"] in used or (image, b) in pins:
                continue
            state_files.remove(build_file(image, b), fail=False)
            state_files.remove(record_file(image, b), fail=False)
            removed.append(b)
            say(f"Old Build removed: {image}-{b}")
        failed = sorted((b for b, i in found.items() if i["status"] in ("failed", "rejected")), key=_order,
                        reverse=True)
        for b in failed[kept:]:
            state_files.remove(record_file(image, b), fail=False)
    return removed


# --- running podman --------------------------------------------------------------------

class _Failed(Exception):
    """A Build that does not become a Build: status "failed" or "rejected"."""

    def __init__(self, message, status="failed", problems=()):
        super().__init__(message)
        self.message, self.status, self.problems = message, status, list(problems)


def _podman(*args, capture=False, timeout=600, what=None):
    """podman with its output in the Build's journal (or captured); _Failed when it fails."""
    sys.stdout.flush()
    try:
        r = subprocess.run(["podman", *args], stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None, text=True, errors="replace",
                           timeout=timeout)
    except FileNotFoundError:
        raise _Failed("podman is missing")
    except subprocess.TimeoutExpired:
        raise _Failed(f"{what or 'podman ' + args[0]}: time limit exceeded")
    if r.returncode != 0:
        detail = clean((r.stderr or "").strip(), False)[-200:] if capture else ""
        raise _Failed(f"{what or 'podman ' + args[0]} failed (exit {r.returncode})" + (f": {detail}" if detail else ""))
    return r.stdout if capture else None


def _rmi(tag):
    """Remove a container image; --force also ends a container of it that still runs (the
    version check's count, versions.py), so a Build never waits for one or leaves its
    image behind."""
    programs.quietly(["podman", "rmi", "--force", "--ignore", tag], 300)


def _image_exists(tag):
    try:
        return subprocess.run(["podman", "image", "exists", tag], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _layers(tag):
    try:
        info = json.loads(_podman("image", "inspect", tag, capture=True, timeout=120))
        layers = info[0]["RootFS"]["Layers"]
    except (ValueError, LookupError, TypeError):
        raise _Failed(f"podman image inspect {tag}: unexpected answer")
    if not isinstance(layers, list) or not all(isinstance(l, str) for l in layers):
        raise _Failed(f"podman image inspect {tag}: unexpected answer")
    return layers


# --- source ------------------------------------------------------------------------------

def definition(image):
    """The Containerfile that defines the Image, as a path in the repository, which is also its
    path in the installed Release (paths.VM_SOURCE is laid out as the repository)."""
    return "vm/Containerfile" if image == BASE else f"vm/images/{image}/Containerfile"


def derived(src):
    """The Images other than the Base image defined in this source tree."""
    folder = os.path.join(src, "vm", "images")
    return [n for n in state_files.listdir(folder, fail=False)
            if NAME.fullmatch(n) and n != BASE and n not in TARGETS
            and os.path.isfile(os.path.join(folder, n, "Containerfile"))]


def _defined():
    """The Images the installed Release defines, or None when its files are not there."""
    if not os.path.isfile(os.path.join(paths.VM_SOURCE, "vm", "Containerfile")):
        return None
    return [BASE, *derived(paths.VM_SOURCE)]


def _release():
    """The installed Release, or None when its VERSION cannot be read."""
    try:
        return version.release()
    except Fail:
        return None


def take_source(work, say):
    """The Containerfiles and guest files of the installed Release (ADR 0010), copied to
    work/src: (source directory, Release). One copy serves every Image of the run, so a
    package update between two Images of `build --all` cannot mix two Releases; the copy itself
    is not guarded against an update while it runs."""
    release = version.release()
    say(f"== Sources: Release {release} ({paths.VM_SOURCE})")
    if not os.path.isfile(os.path.join(paths.VM_SOURCE, "vm", "Containerfile")):
        die(f"{paths.VM_SOURCE}/vm/Containerfile is missing: the Release is not fully installed",
            error="action_failed")
    src = os.path.join(work, "src")
    try:
        shutil.copytree(paths.VM_SOURCE, src, symlinks=True)
    except OSError as e:          # shutil.Error included
        die(f"{paths.VM_SOURCE} not readable ({type(e).__name__})", error="action_failed")
    say(f"Images: {', '.join([BASE, *derived(src)])}")
    return src, release


# --- one Build ---------------------------------------------------------------------------

def _base_for_derived():
    """The Base image's current Build a derived Image builds on: checked, with its
    container image still there. (build, record)."""
    build = current(BASE)
    rec = builds().get(BASE, {}).get(build) if build else None
    if not rec or rec["status"] != "ok" or not rec.get("safeguards"):
        raise _Failed("the Base image has no checked Build, run agentbox build base first")
    if not _image_exists(CONTAINER.format(BASE, build)):
        raise _Failed(f"the container image of the Base Build {build} is missing, run agentbox build base first")
    return build, rec


def _check(image, tag, days, base):
    """The safeguard check; _Failed("rejected") with every problem found. Returns the
    manifest, the agents' versions and the versions of the tools (versions.in_image),
    which are None when they cannot be read: they never fail a Build."""
    problems = []
    if base is not None:
        mine, theirs = _layers(tag), _layers(CONTAINER.format(BASE, base["build"]))
        if mine[:len(theirs)] != theirs:
            problems.append(f"not built on the Base Build {base['build']} (FROM ${{BASE}})")
    root = _podman("image", "mount", tag, capture=True, timeout=300).strip()
    try:
        if not os.path.isabs(root) or not os.path.isdir(root):
            raise _Failed("podman image mount: unexpected answer")
        found, manifest, agents = safeguards.check(root, days, base["safeguards"] if base else None)
        try:
            tools = versions.in_image(root) if not found else None
        except Exception:                # what cannot be read is unknown
            tools = None
    finally:
        programs.quietly(["podman", "image", "unmount", tag], 120)
    problems += found
    if problems:
        raise _Failed(problems[0] + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""),
                      "rejected", problems)
    return manifest, agents, tools


def _switch(image, build):
    """Make the Build the Image's current one: the link images/<image>.qcow2, in one rename."""
    link = current_link(image)
    if os.path.lexists(link) and not os.path.islink(link):
        raise _Failed(f"{link} is a file, not a link; sort it out by hand first")
    tmp = os.path.join(paths.IMAGES, f".{image}.qcow2.new")
    state_files.remove(tmp, fail=False)
    os.symlink(os.path.basename(build_file(image, build)), tmp)
    os.replace(tmp, link)


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _build_one(image, src, release, days, orca, actor, unit_name, say, reason=None):
    """Build one Image from the Release's files in the unit unit_name, the Base image with
    the package minimum age `days` and Orca's release `orca` (orca_version, "" for the
    newest old enough); the Build's id when it became the current Build, else False. The
    Activity entries of an update's Builds have the reason "update"."""
    what = "Update: " if reason == "update" else ""
    build = _stamp()
    while os.path.exists(record_file(image, build)) or os.path.exists(build_file(image, build)):
        time.sleep(1)
        build = _stamp()
    rec = {"image": image, "build": build, "status": "running", "step": "build", "started": int(time.time()),
           "finished": None, "release": release, "base_build": None,
           "package_min_age_days": days, "orca_version": orca or None, "unit": unit_name,
           "actor": actor, "agents": None, "tools": None, "problems": [], "error": None, "safeguards": None,
           "container": False}
    _save(rec)
    tag = CONTAINER.format(image, build)
    out = os.path.join(paths.IMAGES, f".build-{image}-{build}")
    os.makedirs(out)
    try:
        try:
            if image == BASE:
                say(f"== {image}: building the container image (package minimum age: {days} days; Orca: "
                    + (f"{orca}, pinned in orca_version)" if orca else "the newest release at least that old)"))
                _podman("build", "--pull=newer", "--no-cache", "--build-arg", f"PACKAGE_MIN_AGE_DAYS={days}",
                        "--build-arg", f"ORCA_VERSION={orca}",
                        "-t", tag, "-f", os.path.join(src, "vm", "Containerfile"), src,
                        timeout=4 * 3600, what="podman build")
                base = None
            else:
                context = os.path.join(src, "vm", "images", image)
                if not os.path.isfile(os.path.join(context, "Containerfile")):
                    raise _Failed(f"Image {image} does not exist in Release {release} (vm/images/{image}/Containerfile)")
                base_build, base = _base_for_derived()
                # a derived Image keeps the Base Build's safeguards, its package age included,
                # and its Orca
                days = base["package_min_age_days"]
                rec.update(base_build=base_build, package_min_age_days=days, orca_version=base.get("orca_version"))
                _save(rec)
                say(f"== {image}: building the container image on the Base Build {base_build}")
                _podman("build", "--pull=missing", "--no-cache", "--build-arg",
                        f"BASE={CONTAINER.format(BASE, base_build)}", "-t", tag,
                        "-f", os.path.join(context, "Containerfile"), context, timeout=4 * 3600, what="podman build")
            rec.update(step="check", container=True)
            _save(rec)
            say(f"== {image}: checking the Safeguards")
            manifest, agents, tools = _check(image, tag, days, base)
            rec.update(step="disk", agents=agents, tools=tools, safeguards=manifest if image == BASE else None)
            _save(rec)
            say(f"== {image}: all Safeguards present. Creating the disk (bootc-image-builder)")
            with open(os.path.join(out, "config.toml"), "w") as f:
                f.write(DISK_CONFIG)
            _podman("run", "--rm", "--privileged", "--pull=newer", "--security-opt", "label=type:unconfined_t",
                    "-v", f"{out}:/output", "-v", f"{os.path.join(out, 'config.toml')}:/config.toml:ro",
                    "-v", f"{STORAGE}:{STORAGE}", BUILDER, "--type", "qcow2", "--rootfs", "xfs",
                    "--config", "/config.toml", tag, timeout=2 * 3600, what="bootc-image-builder")
            disk = os.path.join(out, "qcow2", "disk.qcow2")
            if not os.path.isfile(disk):
                raise _Failed("bootc-image-builder created no disk")
            os.chmod(disk, 0o644)
            os.replace(disk, build_file(image, build))
            rec.update(status="ok", step="done", finished=int(time.time()), container=image == BASE)
            _save(rec)
            with state_files.locked(paths.IMAGES):
                _switch(image, build)
            say(f"== {image}: Build {build} is the current one (applies to VMs from their next Reset)")
        except _Failed as f:
            rec.update(status=f.status, finished=int(time.time()), error=f.message, problems=f.problems,
                       container=False)
            _save(rec)
            _rmi(tag)
            state_files.remove(build_file(image, build), fail=False)
            if f.status == "rejected":
                say(f"== {image}: Build {build} rejected, a Safeguard is missing or weakened:")
                for p in f.problems:
                    say(f"   {p}")
            else:
                say(f"== {image}: Build {build} failed: {f.message}")
            say(f"The previous Build stays in use ({current(image) or 'none'})")
            activity.record("build", f"build.{f.status}",
                            f"{what}Build {image}-{build} " + ("rejected: " if f.status == "rejected" else "failed: ")
                            + f.message, outcome="failed", actor=actor,
                            # no error code: what failed, in English, for the Control Room to show
                            params={"image": image, "build": build, "reason": reason, "message": f.message})
            _save({**rec, "ended": int(time.time())})
            return False
        except (Fail, OSError, KeyboardInterrupt) as e:
            message = "interrupted" if isinstance(e, KeyboardInterrupt) else \
                e.message.strip() if isinstance(e, Fail) else f"{e.strerror or e}"
            rec.update(status="failed", finished=int(time.time()), error=message, container=False)
            _save(rec)
            _rmi(tag)
            state_files.remove(build_file(image, build), fail=False)
            say(f"== {image}: Build {build} failed: {message}")
            error = "interrupted" if isinstance(e, KeyboardInterrupt) else e.error if isinstance(e, Fail) else None
            activity.record("build", "build.failed", f"{what}Build {image}-{build} failed: {message}",
                            outcome="failed", actor=actor,
                            params={"image": image, "build": build, "reason": reason,
                                    "message": None if error else message},
                            error=error, error_params=e.params if isinstance(e, Fail) else None)
            _save({**rec, "ended": int(time.time())})
            if isinstance(e, KeyboardInterrupt):
                raise
            return False
    finally:
        shutil.rmtree(out, ignore_errors=True)
    # a derived Image's container image is not needed any more; the Base image keeps
    # only its current one (derived Images build on it)
    if image == BASE:
        for old in _records():
            if old["image"] == BASE and old["build"] != build and old.get("container"):
                _rmi(CONTAINER.format(BASE, old["build"]))
                _save({**old, "container": False})
    else:
        _rmi(tag)
    removed = prune(image, say)
    note = f", {len(removed)} old Build{'s' if len(removed) != 1 else ''} removed" if removed else ""
    activity.record("build", "build.ok", f"{what}Build {image}-{build} done (Release {release}){note}",
                    actor=actor, params={"image": image, "build": build, "release": release,
                                         "removed": len(removed) or None, "reason": reason})
    if os.path.exists(record_file(image, build)):
        _save({**rec, "ended": int(time.time())})        # the end of its log
    return build


def _cleanup(say):
    """Before a Build (holding the run lock): leftovers of an interrupted one."""
    for n in state_files.listdir(paths.IMAGES, fail=False):
        if re.fullmatch(r"\.build-.+|build-\d{8}-\d{4}", n) and os.path.isdir(os.path.join(paths.IMAGES, n)):
            shutil.rmtree(os.path.join(paths.IMAGES, n), ignore_errors=True)
    for rec in _records():
        if rec.get("status") == "running":
            rec.update(status="failed", error="interrupted", finished=rec.get("finished") or int(time.time()))
            _save(rec)
            _rmi(CONTAINER.format(rec["image"], rec["build"]))
            say(f"Interrupted Build {rec['image']}-{rec['build']} marked as failed")


def _request_file(target):
    return os.path.join(paths.BUILDS, f"request-{target}.json")


def _take_request(target):
    """Who started the Build, the VMs to reset after it, each with the Image it used when it was
    picked, and whether an update checks the versions first (agentbox build and update-now
    write them before they start the unit): (actor, [(VM, Image)], check)."""
    path = _request_file(target)
    request = state_files.json_object(path, {})
    actor, resets = request.get("actor"), request.get("reset")
    picked, check = request.get("images"), request.get("check")
    state_files.remove(path, fail=False)
    resets = [v for v in resets if isinstance(v, str) and registry.NAME.fullmatch(v)] \
        if isinstance(resets, list) and target != "all" else []
    resets = list(dict.fromkeys(resets))
    if target == UPDATE:
        picked = picked if isinstance(picked, dict) else {}
        picks = [(v, picked[v]) for v in resets if isinstance(picked.get(v), str) and NAME.fullmatch(picked[v])]
    else:
        picks = [(v, target) for v in resets]
    return (actor if activity.valid_actor(actor) else "systemd"), picks, check is True and target == UPDATE


def _reset_after(picks, built, actor, say):
    """The Resets of an Update after its Builds (`built`: {Image: the Build made}): each picked
    VM, if it still uses the Image it used when it was picked, without a pin, onto the Image's
    current Build (normally the one just made), started again when it ran
    (lifecycle.reset_as_found). One Activity entry each, naming the Build used; a VM that fails
    does not keep the others from their Reset. Interrupted (systemctl stop), the VMs not reset
    yet are recorded as such."""
    from . import lifecycle, virsh           # lifecycle imports this module
    failed = False
    for n, (vm, image) in enumerate(picks):
        try:
            rec = registry.load(registry.check(vm))
            if rec["image"] != image or rec["build"]:
                die(f"{vm} now uses " + (f"Build {rec['build']} of {rec['image']}" if rec["build"]
                                         else f"Image {rec['image']}") + ", no Reset",
                    error="vm_pinned" if rec["image"] == image else "vm_uses_other_image", vm=vm,
                    image=rec["image"], build=rec["build"])
            build = built.get(image)
            say(f"== {vm}: Reset after the Update of {image}" + (f" (Build {build})" if build else ""))
            ran, used = lifecycle.reset_as_found(vm, say)
        except KeyboardInterrupt:
            for rest, rest_image in picks[n:]:
                running = virsh.state(rest) == "running"
                activity.record("reset", "reset.update", f"Reset after the Update of {rest_image} interrupted, the VM "
                                + ("is running" if running else "is off"), rest, "failed", actor=actor,
                                params={"image": rest_image, "running": running}, error="interrupted")
            raise
        except Fail as f:
            failed = True
            message = " ".join(f.message.strip().removeprefix("agentbox: ").split()) or f"Exit {f.exit}"
            running = virsh.state(vm) == "running"
            state = "is running" if running else "stays off"
            say(f"== {vm}: Reset failed: {message}; the VM {state}")
            activity.record("reset", "reset.update", f"Reset after the Update of {image} failed: {message}; "
                            f"the VM {state}", vm, "failed", actor=actor, params={"image": image, "running": running},
                            error=f.error, error_params=f.params)
            continue
        m = DISK.fullmatch(used)
        activity.record("reset", "reset.update", f"Reset after the Update onto Build {used.removesuffix('.qcow2')}"
                        + (", started again" if ran else ""), vm, actor=actor,
                        params={"image": image, "build": m.group(2) if m else None, "started": bool(ran)})
    if failed:
        raise Fail("", 1, raw=True, error="action_failed")


def _no_resets(picks, why, actor, say, failed=(), error=None):
    """A failed Build resets nothing: said in its log and for each picked VM in Activity, with
    the Images whose Build failed or the error code of what failed instead (the flag skipped:
    no Reset began)."""
    say(f"== No Reset of {', '.join(vm for vm, _ in picks)}: {why}")
    for vm, image in picks:
        activity.record("reset", "reset.update", f"No Reset after the Update: {why}", vm, "failed", actor=actor,
                        params={"image": image, "skipped": True, "failed_builds": list(failed)}, error=error)


def _failed_builds(images):
    return ("the Build of " if len(images) == 1 else "the Builds of ") + ", ".join(images) + " failed"


def _terminate(signum, frame):
    raise KeyboardInterrupt


def build_run(args):
    """agentbox build-run <image>|all|update: what agentbox-build@<target>.service runs."""
    if len(args) != 1:
        die("build-run <image>|all|update", error="invalid_argument")
    target = args[0] if args[0] in TARGETS else _image_name(args[0])
    say = lambda line: print(line, flush=True)      # noqa: E731  (the unit's journal)
    signal.signal(signal.SIGTERM, _terminate)        # systemctl stop: clean up like Ctrl+C
    actor, picks, check = _take_request(target)
    state_files.directory(paths.IMAGES, 0o711, only_new=True)
    if target == UPDATE:
        _update_run(actor, picks, check, say)
        return
    try:
        results = _run(target, actor, say)
    except (Fail, KeyboardInterrupt):
        if picks:
            _no_resets(picks, _failed_builds([target]), actor, say, failed=[target])
        raise
    if not all(build for _, build in results):
        if picks:
            _no_resets(picks, _failed_builds([target]), actor, say, failed=[target])
        raise Fail("", 1, raw=True, error="action_failed")
    if picks:
        _reset_after(picks, dict(results), actor, say)


def _check_versions(say):
    """The version check of Update now, as the Maintenance window runs it: the fetch as
    agentbox-feeds through its unit (systemctl start waits for it), then the root part
    (versions.check). What fails stays unknown and never stops the update."""
    say(f"== Checking versions: asking npm and GitHub ({versions.FETCH_UNIT}, as agentbox-feeds)")
    try:
        r = subprocess.run(["systemctl", "start", versions.FETCH_UNIT], capture_output=True, text=True,
                           errors="replace", timeout=20 * 60)
        if r.returncode != 0:
            say("Fetch failed: " + programs.summary(r))
    except (OSError, subprocess.TimeoutExpired) as e:
        say(f"Fetch failed: {type(e).__name__}")
    say("== Checking versions: package updates and what a new Build would bring")
    try:
        versions.check(say)
    except Fail as f:
        say(f"Check failed: {f.message}")


def _update_run(actor, picks, check, say):
    """build-run update: Update now (check: the version check first) and the automatic update
    the version check of the Maintenance window starts. Under the run lock it builds the Images
    a new Build would update (_run), then resets the VMs picked onto their Image's current
    Build. A failed Build resets nothing."""
    try:
        if check:
            _check_versions(say)
        results = _run(UPDATE, actor, say)
    except KeyboardInterrupt:
        if picks:
            _no_resets(picks, "the update was interrupted", actor, say, error="interrupted")
        raise
    except Fail as f:
        if picks:
            _no_resets(picks, "the update failed", actor, say, error=f.error)
        raise
    failed = [image for image, build in results if not build]
    if failed:
        if picks:
            _no_resets(picks, _failed_builds(failed), actor, say, failed=failed)
        raise Fail("", 1, raw=True, error="action_failed")
    if picks:
        _reset_after(picks, dict(results), actor, say)


def update_images(view=None):
    """The Images an update builds: those a new Build would update (`update` of the overview: a
    tool it would change, or package updates) that the installed Release defines; with the
    Base image every Image of the Release, since they build on it (as build --all)."""
    view = overview() if view is None else view
    wanted = [i["name"] for i in view["images"] if i["update"] and i["in_release"]]
    if BASE in wanted:
        return [BASE, *(i["name"] for i in view["images"] if i["name"] != BASE and i["in_release"])]
    return wanted


def _run(target, actor, say):
    """The Builds of build-run, one at a time (the run lock): [(Image, Build id or False)]. An
    update decides under the lock which Images it builds (update_images), so a Build that
    ended while it waited for the lock counts; when none needs one, it builds nothing."""
    reason = "update" if target == UPDATE else None
    with state_files.locked(RUN_LOCK):
        started = int(time.time())
        _cleanup(say)
        wanted = None
        if target == UPDATE:
            wanted = update_images()
            if not wanted:
                say("== Update: no Image needs a new Build")
                activity.record("build", "build.unneeded", "Update: no Image needs a new Build",
                                actor=actor, params={"reason": reason})
                return []
            say(f"== Update: Build of {', '.join(wanted)}")
        work = os.path.join(paths.IMAGES, f".build-source-{_stamp()}")
        os.makedirs(work)
        try:
            try:
                values = settings.load()
                days, orca = values["package_min_age_days"], values["orca_version"]
                src, release = take_source(work, say)
            except Fail as f:
                # nothing to build from: one failed Build of the Image (the Base image for all)
                image = BASE if target == "all" else wanted[0] if wanted else target
                build = _stamp()
                _save({"image": image, "build": build, "status": "failed", "step": "source", "started": started,
                       "finished": int(time.time()), "release": None, "base_build": None,
                       "package_min_age_days": None, "orca_version": None, "unit": unit(target), "actor": actor,
                       "agents": None,
                       "tools": None, "problems": [], "error": f.message, "safeguards": None, "container": False})
                say(f"== No sources: {f.message}")
                say(f"The previous Build stays in use ({current(image) or 'none'})")
                activity.record("build", "build.failed", ("Update: " if reason else "")
                                + f"{'Build of all Images' if target == 'all' else f'Build {image}-{build}'} failed: "
                                + f.message, outcome="failed", actor=actor,
                                params={"image": image, "build": build, "all": target == "all", "reason": reason},
                                error=f.error, error_params=f.params)
                raise Fail("", 1, raw=True, error="action_failed")
            if target == "all" or BASE in (wanted or []):
                images = [BASE, *derived(src)]
            else:
                images = wanted or [target]
            return [(image, _build_one(image, src, release, days, orca, actor, unit(target), say, reason))
                    for image in images]
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _changes(image, offered):
    """What a new Build of the Image would bring, for people: "Claude Code 2.1.5 -> 2.1.7, 12
    package updates"."""
    now = next((b for b in image["builds"] if b["current"]), None)
    parts = [f"{versions.NAMES.get(t, t)} {now['tools'].get(t) or '?'} -> {offered['tools'].get(t) or '?'}"
             for t in (now["updates"] if now else [])]
    if image["package_updates"]:
        parts.append(f"{image['package_updates']} package updates")
    return ", ".join(parts)


def start_automatic(say):
    """With auto_update on, after the version check of the Maintenance window
    (versions.check_versions): an update of the Images a new Build would update, started at once
    (agentbox-build@update), unless a Build or an update runs (the run lock is held, or the
    update's unit runs): it never waits and never runs two Builds at once; the next check tries
    again. Its start is recorded in Activity (build.automatic, reason update). The window waits
    for its Builds (building())."""
    view = overview()
    wanted = update_images(view)
    if not wanted:
        say("Automatic update: no Image needs a new Build")
        return
    changes = "; ".join(f"{i['name']}: {_changes(i, view['available'])}" for i in view["images"]
                        if i["name"] in wanted and i["update"])
    targets = ", ".join(wanted)
    # a unit that was started but has not taken the run lock yet counts too
    builds = (UPDATE, "all", *(i["name"] for i in view["images"]))
    if state_files.held(RUN_LOCK) or any(_active(unit(t)) for t in builds):
        say(f"Automatic update of {targets} not started: a Build is running; the next check tries again")
        activity.record("build", "build.skipped", f"Automatic update of {targets} not started: a Build is "
                        "running; the next check tries again", params={"images": wanted, "reason": "update"})
        return
    try:
        name = start(UPDATE, check=False)
    except Fail as f:
        say(f"Automatic update of {targets} not started: {f.message}")
        activity.record("build", "build.automatic", f"Automatic update of {targets} not started: {f.message}",
                        outcome="failed", params={"images": wanted, "reason": "update"}, error=f.error,
                        error_params=f.params)
        return
    say(f"Automatic update started ({name}): Build of {targets} ({changes})")
    # what the Builds would bring stays in the description: free English text, no parameter
    activity.record("build", "build.automatic", f"Automatic update started: Build of {targets} ({changes})",
                    params={"images": wanted, "reason": "update"})


def building():
    """Whether a Build runs or is about to (the Maintenance window waits for it, maintenance.py):
    the run lock is held, a unit of a Build is active (update, all, or an Image the Host or the
    installed Release knows), or a request file younger than a minute waits for the unit that
    takes it at its start (start() wrote it, then started the unit without waiting)."""
    if state_files.held(RUN_LOCK):
        return True
    names = dict.fromkeys([*available(), *builds(), *(_defined() or [])])
    if any(_active(unit(t)) for t in (UPDATE, "all", *names)):
        return True
    for n in state_files.listdir(paths.BUILDS, fail=False):
        if not re.fullmatch(r"request-.+\.json", n):
            continue
        try:
            if time.time() - os.stat(os.path.join(paths.BUILDS, n)).st_mtime < 60:
                return True
        except FileNotFoundError:
            continue
    return False


def _since(started, target):
    return [r for r in _records() if r.get("unit") == unit(target) and isinstance(r.get("started"), int)
            and r["started"] >= started]


def failed_since(started, target, actor):
    """The Builds that agentbox-build@<target> began at or after `started` (epoch seconds) on the
    request of `actor` (the actor of the request file, which the Build record keeps) and that did
    not become usable, as "<image>-<build>" (the Maintenance window: Builds it waited for that
    failed; a Build of another actor's request, Update now while the window waited, is not the
    window's)."""
    return [f"{r['image']}-{r['build']}" for r in _since(started, target)
            if r.get("actor") == actor and r.get("status") not in USABLE]


def begun_since(started, target):
    """Whether agentbox-build@<target> began a Build at or after `started`, on anyone's request."""
    return bool(_since(started, target))


def unit_failed_since(started, target):
    """Whether agentbox-build@<target> began a run at or after `started` (epoch seconds) that
    failed (systemd's Result of its latest run, which left the inactive state then): a run that
    fails before it begins a Build leaves no Build record. False when systemd cannot say."""
    try:
        r = subprocess.run(["systemctl", "show", "--timestamp=unix", "--property=Result,InactiveExitTimestamp",
                            "--", unit(target)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        return False
    props = dict(line.partition("=")[::2] for line in r.stdout.splitlines())
    began = re.fullmatch(r"@(\d+)", props.get("InactiveExitTimestamp", "").strip())
    result = props.get("Result", "").strip()
    return began is not None and int(began.group(1)) >= started and result not in ("", "success")


# --- commands ------------------------------------------------------------------------------

def _active(name):
    """Whether a unit of a Build runs, or is about to: deactivating counts, a Build that is
    stopping may still hold the run lock and write its Image (describe.ACTIVE leaves it out,
    a connection being closed is no connection)."""
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.strip() in ("active", "activating", "deactivating", "reloading")


def _update_vms(image, names):
    """The VMs an Update resets after its Build, with their Image: each must use the Image
    (image None, Update now: any) without a pin, else its Reset would not bring the new Build.
    [(VM, Image)]."""
    if names and image == "all":
        die("--reset only with one Image, not with --all", error="invalid_argument", option="--reset")
    picks = {}
    for name in names:
        vm = registry.check(name)
        rec = registry.load(vm)
        if image is not None and rec["image"] != image:
            die(f"{vm} uses Image {rec['image']}, not {image}", error="vm_uses_other_image", vm=vm,
                image=rec["image"], wanted=image)
        if rec["build"]:
            die(f"{vm} is pinned to Build {rec['build']}, a Reset would not bring the new Build "
                f"(agentbox set-image {vm} {rec['image']})", error="vm_pinned", vm=vm, image=rec["image"],
                build=rec["build"])
        picks.setdefault(vm, rec["image"])
    return list(picks.items())


def start(target, resets=(), **request):
    """Start the unit of a Build (an Image, all, or update), naming in its request file who
    asked (the actor of this call), the VMs to reset after it and what else its run needs;
    returns the unit. A unit of that target that runs already is refused."""
    if not os.path.exists(os.path.join(paths.SYSTEMD, TEMPLATE)):
        die(f"{TEMPLATE} is missing, run agentbox regenerate first", error="not_set_up")
    name = unit(target)
    if _active(name):
        if target == UPDATE:
            die(f"an update is running already (journalctl -fu {name})", error="update_running")
        what = "of all Images" if target == "all" else f"of {target}"
        die(f"a Build {what} is running already (agentbox build-log {BASE if target == 'all' else target})",
            error="build_running", image=target)
    state_files.directory(paths.BUILDS, 0o700, only_new=True)
    # two callers at the same time (an automatic start, Update now) must not overwrite each
    # other's request: a request file the unit has not taken yet (younger than a minute) refuses
    with state_files.locked(REQUEST_LOCK):
        try:
            waiting = time.time() - os.stat(_request_file(target)).st_mtime < 60
        except FileNotFoundError:
            waiting = False
        if waiting:
            if target == UPDATE:
                die(f"an update is running already (journalctl -fu {name})", error="update_running")
            die(f"a Build of {target} is being started already", error="build_running", image=target)
        mine = json.dumps({"actor": activity.actor(), "reset": list(resets), **request}).encode()
        state_files.write_atomic(_request_file(target), mine, 0o600)
    # a start that fails in any way (an exit code, no answer in time, not startable) takes its
    # request file back, or the next start within a minute would be refused as waiting; unless
    # the unit runs after all (systemd queued it before the time limit) and takes it, or it is
    # another caller's by now
    try:
        r = programs.systemctl("start", "--no-block", name, timeout=60, ok=None)
        if r.returncode != 0:
            programs.fail(r, f"systemctl start {name}", program="systemctl")
    except BaseException:
        _take_back(target, name, mine)
        raise
    return name


def _take_back(target, name, mine):
    """Remove the request file of a start that failed, if it is still this call's and no unit
    of the target runs to take it."""
    with state_files.locked(REQUEST_LOCK):
        if _active(name) or state_files.read(_request_file(target), None) != mine:
            return
        state_files.remove(_request_file(target), fail=False)


def build_cmd(args):
    """agentbox build <image>|--all [--reset <vm>]... [--json]: start a Build as a systemd
    unit; with --reset an Update: when the Build becomes the current one, these VMs are
    reset onto it."""
    positional, opts = options.parse(args, {"all": False, "json": False, "reset": options.MANY})
    if bool(positional) == bool(opts.get("all")) or len(positional) > 1:
        die("build <image>|--all [--reset <vm>]... [--json]", error="invalid_argument")
    target = "all" if opts.get("all") else _image_name(positional[0])
    resets = [vm for vm, _ in _update_vms(target, opts.get("reset", []))]
    name = start(target, resets)
    what = "of all Images" if target == "all" else f"of {target}"
    message = (f"Build {what} started ({name}); "
               + (f"if it succeeds, then a Reset of {', '.join(resets)}; " if resets else "")
               + f"progress: agentbox build-log {BASE if target == 'all' else target} or journalctl -fu {name}")
    if "json" in opts:
        print(json.dumps({"image": target, "unit": name, "started": True, "reset": resets, "message": message}))
    else:
        print(message)


def update_now_cmd(args):
    """agentbox update-now [--reset <vm>]... [--json]: Update now (#114), in
    agentbox-build@update: the version check at once, a Build of every Image a new Build would
    update (with the Base image every Image), then, when every Build succeeded, a Reset of these
    VMs onto their Image's current Build (sessions in them end, their Data disks stay)."""
    positional, opts = options.parse(args, {"json": False, "reset": options.MANY})
    if positional:
        die("update-now [--reset <vm>]... [--json]", error="invalid_argument")
    picks = _update_vms(None, opts.get("reset", []))
    resets = [vm for vm, _ in picks]
    name = start(UPDATE, resets, images=dict(picks), check=True)
    message = (f"Update started ({name}): checking versions, then building every Image with an update; "
               + (f"if every Build succeeds, then a Reset of {', '.join(resets)}; " if resets else "")
               + f"progress: journalctl -fu {name}")
    if "json" in opts:
        print(json.dumps({"unit": name, "started": True, "reset": resets, "message": message}))
    else:
        print(message)


def _public(info, cur, used, pins, offered):
    file = info.get("file")
    tools = versions.of_build(info)
    return {"build": info["build"], "status": info["status"], "step": info.get("step"),
            "started": times.iso(info.get("started")), "finished": times.iso(info.get("finished")),
            "release": info.get("release"), "commit": info.get("commit"), "base_build": info.get("base_build"),
            "package_min_age_days": info.get("package_min_age_days"), "orca_version": info.get("orca_version"),
            "agents": info.get("agents"),
            "tools": tools, "updates": versions.updates(tools, offered["tools"]) if info["status"] in USABLE else [],
            "error": info.get("error"), "problems": info.get("problems") or [], "actor": info.get("actor"),
            "current": info["build"] == cur, "file": file, "size": info.get("size"),
            "used_by": sorted(used.get(file, [])) if file and used is not None else ([] if used is not None else None),
            "pinned_by": sorted(pins.get((info["image"], info["build"]), []))}


def _source_ref(info):
    """(repository, tag or commit) of the files a Build came from: its Release's tag, or for a
    Build from before ADR 0010 the commit of main and the repository its record names; (None,
    None) when neither is known (a checkout's Release 0.0.0 has no tag)."""
    tag = version.tag(info.get("release"))
    if tag:
        return version.REPOSITORY, tag
    commit = info.get("commit")
    if isinstance(commit, str) and COMMIT.fullmatch(commit):
        return info.get("repository"), commit
    return None, None


def overview():
    """Every Image (defined in the installed Release, with Builds on the Host or used by a
    VM) and its Builds. A Build's `release` is the Release it was built from; Builds from
    before ADR 0010 name the commit of main they came from instead (`commit`). The versions
    of its tools (`tools`) and those a new Build would change (`updates`), by what the last
    version check found (`available`, versions.py); an Image's `update`: a new Build would
    change a tool or bring package updates (None: nothing to tell it by). An Image's
    Containerfile (`containerfile`, its path in the repository) links to the repository at the
    tag or commit its current Build came from (`containerfile_url`, None when that is not
    known); `repository` is where the Releases come from. Links only: nothing is fetched."""
    found, defined = builds(), _defined()
    used = in_use()
    pins, users = pinned()
    values = settings.load()
    checked = versions.load()
    offered = versions.available(checked or {}, values["orca_version"] or None)
    names = set(found) | set(users) | set(available()) | set(defined or [])
    names = sorted((n for n in names if NAME.fullmatch(n)), key=lambda n: (n != BASE, n))
    images = []
    for image in names:
        cur = current(image)
        mine = sorted(found.get(image, {}).values(), key=lambda i: _order(i["build"]), reverse=True)
        running = next((i for i in mine if i["status"] == "running"), None)
        public = [_public(i, cur, used, pins, offered) for i in mine]
        now = next((b for b in public if b["current"]), None)
        packages = versions.package_updates(image, cur, checked or {})
        changes = (now["updates"] if now else []) + (["packages"] if packages else [])
        known = now is not None and (packages is not None or any(
            offered["tools"][t] and now["tools"][t] for t in ("orca", *versions.NPM)))
        repository, ref = _source_ref(found.get(image, {}).get(cur) or {})
        images.append({
            "name": image, "base": image == BASE,
            "in_release": image in defined if defined is not None else None,
            "current": cur, "vms": sorted(users.get(image, [])),
            "running": {"build": running["build"], "step": running.get("step"),
                        "started": times.iso(running.get("started"))} if running else None,
            "package_updates": packages, "updates": changes, "update": bool(changes) if known else None,
            "containerfile": definition(image),
            "containerfile_url": version.file_url(repository, ref, definition(image)),
            "builds": public})
    return {"images": images, "builds_kept": values["builds_kept"], "release": _release(),
            "repository": version.REPOSITORY, "repository_url": version.repository_url(),
            "disks_readable": used is not None, "available": offered}


def _update_line(image):
    """What a new Build of the Image would bring, for people."""
    if image["update"] is None:
        return "Update: unknown (checked daily; now: systemctl start agentbox-versions)"
    changes = [versions.NAMES.get(u, u) for u in image["updates"] if u != "packages"]
    packages = image["package_updates"]
    if packages:
        changes.append(f"{packages} package updates")
    if changes:
        return "Update available: a new Build would bring " + ", ".join(changes)
    return "Update: a new Build would bring nothing new" + ("" if packages is not None else
                                                            " (package updates unknown)")


def images_cmd(args):
    """agentbox images [--json]: Images and their Builds, which VMs use and pin which."""
    if any(a != "--json" for a in args):
        die("images [--json]", error="invalid_argument")
    result = overview()
    # an update of everything (Update now, or the automatic one) runs: it may be checking the
    # versions, before any Build of it shows
    result["updating"] = _active(unit(UPDATE))
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False))
        return
    offered = result["available"]
    if result["updating"]:
        print(f"Update running ({unit(UPDATE)}): checking versions, building the Images with an update, "
              "then the Resets")
    for image in result["images"]:
        head = f"Image {image['name']}" + (" (Base image)" if image["base"] else "")
        head += f", current: {image['current'] or '-'}, VMs: {', '.join(image['vms']) or '-'}"
        if image["in_release"] is False:
            head += " (not in the installed Release)"
        print(head)
        print(f"  Containerfile: {image['containerfile']}"
              + (f", as in the current Build: {image['containerfile_url']}" if image["containerfile_url"] else ""))
        if image["current"]:
            print("  " + _update_line(image))
        for b in image["builds"]:
            notes = [n for n in ("current" if b["current"] else "",
                                 f"used by {', '.join(b['used_by'])}" if b["used_by"] else "",
                                 f"pinned for {', '.join(b['pinned_by'])}" if b["pinned_by"] else "") if n]
            when = times.local(times.epoch(b["started"])) if b["started"] else "?"
            line = f"  {b['build']:<16} {STATUS.get(b['status'], b['status']):<12} {when}"
            if b["release"]:
                line += f"  Release {b['release']}"
            elif b["commit"]:
                line += f"  Commit {b['commit'][:12]}"
            if b["status"] == "running":
                line += f"  step {b['step']}"
            if b["error"]:
                line += f"  {b['error']}"
            if notes:
                line += "  (" + "; ".join(notes) + ")"
            print(line)
            if b["status"] in USABLE and any(b["tools"].values()):
                print(" " * 19 + versions.text(b["tools"]))
    tools = offered["tools"]
    parts = [f"{versions.NAMES[t]} {tools[t] or '?'}" for t in ("orca", *versions.NPM)]
    if offered["checked"]:
        when = times.local(times.epoch(offered["checked"]))
        pin = f"; Orca pinned to {offered['orca_version']} (orca_version)" if offered["orca_version"] else ""
        print(f"Available (checked {when}, at least {offered['package_min_age_days']} days old{pin}): "
              + ", ".join(parts))
    else:
        print("Available: not checked yet (daily; now: systemctl start agentbox-versions)")
    if not result["disks_readable"]:
        print("agentbox: not every System disk is readable, Builds in use unknown", file=sys.stderr)


def _read_definition(image):
    """(bytes, size) of the Image's Containerfile in the installed Release: a regular file that is
    not itself a link (the directories above it are the package's, owned by root), at most
    CONTAINERFILE_MAX bytes, cut after its last whole line; the size is the whole file's."""
    rel = definition(image)
    path = os.path.join(paths.VM_SOURCE, rel)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    except (FileNotFoundError, NotADirectoryError):
        die(f"no Containerfile for Image {image} in the installed Release ({rel} is missing)",
            error="containerfile_not_found", image=image)
    except OSError as e:
        die(f"{path} not readable ({e.strerror or type(e).__name__})", error="action_failed")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            die(f"{path} is not a file", error="action_failed")
        data = b""
        while len(data) <= CONTAINERFILE_MAX:
            chunk = os.read(fd, CONTAINERFILE_MAX + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as e:
        die(f"{path} not readable ({e.strerror or type(e).__name__})", error="action_failed")
    finally:
        os.close(fd)
    if len(data) > CONTAINERFILE_MAX:
        cut = data.rfind(b"\n", 0, CONTAINERFILE_MAX)
        data = data[:cut + 1] if cut >= 0 else data[:CONTAINERFILE_MAX]
    return data, max(st.st_size, len(data))


def containerfile_cmd(args):
    """agentbox containerfile <image> [--json]: the Containerfile that defines the Image in the
    installed Release (paths.VM_SOURCE), which its next Build takes; the Control Room shows it.
    Read from the Host only, never fetched; shown as text: at most CONTAINERFILE_MAX bytes,
    without control characters, and anything that looks like a token or key hidden. `url`: the
    file in the repository at the installed Release's tag (None for a checkout's 0.0.0)."""
    positional, opts = options.parse(args, {"json": False})
    if len(positional) != 1:
        die("containerfile <image> [--json]", error="invalid_argument")
    image = _image_name(positional[0])
    data, size = _read_definition(image)
    text = SECRET.sub("[hidden]", clean(data.decode("utf-8", "replace")))
    truncated = len(data) < size
    release = _release()
    if "json" in opts:
        print(json.dumps({"image": image, "path": definition(image), "release": release,
                          "url": version.file_url(version.REPOSITORY, version.tag(release), definition(image)),
                          "text": text, "size": size, "truncated": truncated}, ensure_ascii=False))
        return
    sys.stdout.write(text if not text or text.endswith("\n") else text + "\n")
    if truncated:
        print(f"agentbox: truncated, the file has {size} bytes (shown: at most {CONTAINERFILE_MAX})",
              file=sys.stderr)


LOG_CHECKS = {"after-cursor": guest.CHECKS["after-cursor"], "lines": guest.CHECKS["lines"]}


def _log_entries(info, opts):
    """The journal lines of a Build: its unit, from its start to its end."""
    name = info.get("unit")
    if not isinstance(name, str) or not re.fullmatch(r"agentbox-build@[a-z0-9-]{1,32}\.service", name) \
            or not isinstance(info.get("started"), int):
        return [], False
    limit = int(opts.get("lines", "200"))
    argv = ["journalctl", "--output=json", "--no-pager", f"_SYSTEMD_UNIT={name}", f"--since=@{info['started']}"]
    end = info.get("ended", info.get("finished"))
    if isinstance(end, int):
        argv.append(f"--until=@{end + 1}")
    argv.append(f"--after-cursor={opts['after-cursor']}" if "after-cursor" in opts else f"--lines={limit}")
    out = activity._journalctl(argv)
    entries = []
    for e in guest.records(out):
        e = entry(e)
        if e["message"] is not None:
            e["message"] = SECRET.sub("[hidden]", e["message"])
        entries.append({k: v for k, v in e.items() if not k.startswith("_")})
    truncated = len(entries) > limit
    return entries[:limit], truncated


def build_log(args):
    """agentbox build-log <image> [<build>] [--after-cursor C] [--lines N] [--json]: the
    journal of a Build (default: the running one, else the newest)."""
    positional, opts = options.parse(args, {**LOG_CHECKS, "json": False})
    if not 1 <= len(positional) <= 2:
        die("build-log <image> [<build>] [--after-cursor C] [--lines N] [--json]", error="invalid_argument")
    image = _image_name(positional[0])
    found = builds().get(image, {})
    if len(positional) == 2:
        info = found.get(_build_id(positional[1]))
        if info is None:
            die(f"Build {positional[1]} of Image {image} does not exist", error="build_not_found", image=image,
                build=positional[1])
    else:
        mine = sorted(found.values(), key=lambda i: _order(i["build"]), reverse=True)
        info = next((i for i in mine if i["status"] == "running"), mine[0] if mine else None)
        if info is None:
            die(f"Image {image} has no Builds", error="image_has_no_build", image=image)
    entries, truncated = _log_entries(info, opts)
    if "json" in opts:
        print(json.dumps({"image": image, "build": info["build"], "status": info["status"], "step": info.get("step"),
                          "error": info.get("error"), "problems": info.get("problems") or [], "entries": entries,
                          "cursor": entries[-1]["cursor"] if entries else opts.get("after-cursor"),
                          "truncated": truncated}, ensure_ascii=False))
        return
    print(f"Build {image}-{info['build']}: {STATUS.get(info['status'], info['status'])}"
          + (f" ({info['error']})" if info.get("error") else ""))
    for e in entries:
        t = times.parse(e["time"])
        when = t.astimezone().strftime("%H:%M:%S") if t else "?"
        print(f"{when} {e['message'] or ''}")
    if truncated:
        print("agentbox: output truncated (raise --lines)", file=sys.stderr)


def set_image(args):
    """agentbox set-image <vm> <image> [--build B] [--json]: the Image of a VM, at its newest
    Build or pinned to one (going back to an earlier Build); applies at the next Reset."""
    positional, opts = options.parse(args, {"build": True, "json": False})
    if len(positional) != 2:
        die("set-image <vm> <image> [--build B] [--json]", error="invalid_argument")
    vm = registry.check(positional[0])
    image = _image_name(positional[1])
    build = _build_id(opts["build"]) if "build" in opts else None
    with state_files.locked(registry.record_file(vm)):
        with state_files.locked(paths.IMAGES):            # no prune between this check and the save
            if build:
                info = builds().get(image, {}).get(build)
                if not info or not info["file"] or info["status"] not in USABLE:
                    die(f"Build {build} of Image {image} does not exist (agentbox images)", error="build_not_found",
                        image=image, build=build)
            elif not os.path.isfile(current_link(image)):
                die(f"Image {image} has no Build, run agentbox build {image} first", error="image_has_no_build",
                    image=image)
            rec = registry.load(vm)
            if (rec["image"], rec["build"]) == (image, build):
                activity.unchanged()
            else:
                registry.save({**rec, "image": image, "build": build})
    what = f"pinned to Build {build}" if build else "always its current Build"
    message = f"{vm}: Image {image}, {what} (applies from the next Reset: agentbox reset {vm})"
    if "json" in opts:
        print(json.dumps({"vm": vm, "image": image, "build": build, "message": message}))
    else:
        print(message)


from .commands import declare

DECLARATIONS = {
    "images": declare(images_cmd, "agentbox images [--json]", options={"json": False}, read_only=True,
                       control_room=True, order=133),
    "build": declare(build_cmd, "agentbox build <image>|--all [--reset <vm>]... [--json]",
                      options={"all": False, "reset": options.MANY, "json": False}, control_room=True,
                      kind="build",
                      describe=lambda a: activity._then_reset(
                          a, "Start a Build of all Images", all=True) if "all" in a.values else
                          activity._then_reset(a, f"Start a Build of {a.positional[0] if a.positional else ''}",
                                               image=activity._name(a.positional[0] if a.positional else "",
                                                                    settings.IMAGE)), order=138),
    "build-run": declare(build_run, "agentbox build-run <image>|all", options={}, own_entries=True, order=148),
    "build-log": declare(build_log, "agentbox build-log <image> [<build>] [--after-cursor C] [--lines N] [--json]",
                         options={**LOG_CHECKS, "json": False}, read_only=True, control_room=True, order=144),
    "set-image": declare(set_image, "agentbox set-image <vm> <image> [--build B] [--json]",
                         options={"build": True, "json": False}, control_room=True, kind="build",
                         describe=activity._set_image, order=146),
    "update-now": declare(update_now_cmd, "agentbox update-now [--reset <vm>]... [--json]",
                           options={"reset": options.MANY, "json": False}, control_room=True, kind="build",
                           describe=lambda a: activity._then_reset(
                               a, "Update now: check the versions, build every Image with an update"), order=142),
    "containerfile": declare(containerfile_cmd, "agentbox containerfile <image> [--json]",
                              options={"json": False}, read_only=True, control_room=True, order=136),
}
