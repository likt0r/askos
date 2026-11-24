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
  2. podman builds the container image: the Base image from the pinned digest of
     quay.io/fedora/fedora-bootc:44 (digests.py, build argument FEDORA_BOOTC) with
     package_min_age_days, orca_version and playwright_version of the Settings (its
     Containerfile downloads Orca from its GitHub release, vm/build/orca-release; nothing of
     Orca comes from the Host), a derived Image on the Base image's current Build; its output
     passes through build-run into the journal, and the latest `STEP n/m: <command>` line goes
     into the running Build's record (`progress`, cleaned like the log); with the Setting
     build_vm on (ADR 0025, read once at the start of the run, the record's `build_vm`), this
     podman build runs in the Build's first build VM instead (online, build_vm_run.py), and the
     Host imports the image archive it hands out with skopeo copy under the name it chose for
     the Build, never the archive's (the step import);
  3. safeguards.check reads the built filesystem (podman image mount) and discards the
     Build if a safeguard is missing or weakened; then the versions of its tools are read
     from the same files (versions.py);
  4. bootc-image-builder, by its pinned digest, turns it into a disk,
     images/<image>-<build>.qcow2, with the fedora-bootc the Image is built on as its
     buildroot (--build-container): bootc, osbuild's stages and the tools that make the
     filesystems come from that pristine image, not from the Image (bootc still runs the
     Image's own bootupctl to install the bootloader, see ARCHITECTURE.md); with build_vm on,
     in the Build's second build VM (offline: no network device) from the image the Host
     checked, and the Host takes its disk.qcow2 only once qemu-img read its header (no backing
     file, no data file, not encrypted) and only as a new qcow2 its own qemu-img writes from it;
  5. the link images/<image>.qcow2 moves to it (one rename) and old Builds go.

The pinned digests are read once at the start of a run; with one it needs not pinned yet
(fedora-bootc only for a run that builds the Base image), or with build_vm on and no checked
build VM image yet (build_vm.py), the run first runs the version check, as Update now does,
and fails at the step pins when there is still none. With build_vm on, a Build never falls
back to the Host: when a build VM cannot run (no KVM, not enough memory, libvirt refuses it)
the Build fails with that reason.

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
import resource
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

from . import activity, build_vm, build_vm_run, digests, footprint, guest, options, paths, programs, registry, safeguards
from . import settings, state_files, times, version
from . import states, versions
from .commands import declare
from .errors import Fail, die
from .sanitize import SECRET, clean, entry, redact

BASE = "base"
NAME = settings.IMAGE                                  # also the names of default_image
BUILD_ID = settings.BUILD_ID
DISK = re.compile(rf"({NAME.pattern})-({BUILD_ID.pattern})\.qcow2")
RECORD = re.compile(rf"({NAME.pattern})-({BUILD_ID.pattern})\.json")
USABLE = ("ok", "migrated")
STATUS = {"ok": "ok", "migrated": "from before", "running": "running", "failed": "failed",
          "rejected": "rejected", "missing": "file missing"}
# a run that fails before its Builds records one failed Build at "pins" (the pinned images and the build VMs' image it
# needs, _pins) or "source" (the Release, the Settings); a Build runs "build" to "done"
STEPS = ("pins", "source", "build", "import", "check", "disk", "done")    # import: in build VMs only
TEMPLATE = "agentbox-build@.service"
CONTAINER = "localhost/agentbox/{}:{}"
STORAGE = "/var/lib/containers/storage"
RUN_LOCK = build_vm_run.RUN_LOCK                       # held for a whole Build: one at a time
REQUEST_LOCK = os.path.join(paths.STATE, "build-request")  # held while a request file is written
UPDATE = "update"           # the target of an update of everything: build-run update (#114)
TARGETS = ("all", UPDATE)   # targets of agentbox-build@ that are no Image
COMMIT = re.compile(r"[0-9a-f]{7,40}")      # of main, named by Builds from before ADR 0010
# the line podman build writes as it begins an instruction of the Containerfile ("[1/2] " in
# front in a multi-stage one, whose stages count on their own)
STEP_LINE = re.compile(r"(?:\[(\d{1,4})/(\d{1,4})\] )?STEP (\d{1,4})/(\d{1,4}): ?(.*)")
COMMAND_MAX = 200                 # characters of a step's command a running Build carries
CONTAINERFILE_MAX = 64 * 1024     # bytes of a Containerfile `agentbox containerfile` shows at most
IMPORT_SECONDS = 3600            # skopeo copy of a build VM's image archive into the Host's storage
# the room an import must find before it starts: in the Host's storage IMPORT_FACTOR times the
# size of the image's layers (a filesystem compresses to about a third), and IMPORT_RESERVE on
# each filesystem it writes to, below which the import is stopped while it runs
IMPORT_FACTOR = 3
IMPORT_RESERVE = paths.tunable("AGENTBOX_BUILD_IMPORT_RESERVE_BYTES", 10 << 30)
# a build VM's image archive, which root checks and takes the image's files out of (_unpack):
# its tar's entries, the NUL bytes after its end, the size of index.json, its manifest and its
# config, its layers; in a child process under these limits (copying 64 GiB costs CPU time)
ARCHIVE_ENTRIES = 10000
ARCHIVE_TRAIL = 1 << 20
ARCHIVE_JSON = 4 << 20
ARCHIVE_LAYERS = 128
ARCHIVE_LIMITS = {resource.RLIMIT_AS: 1 << 30, resource.RLIMIT_CPU: 1800}
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYERS = ("application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip",
              "application/vnd.oci.image.layer.v1.tar+zstd")
SHA256 = re.compile(r"sha256:([0-9a-f]{64})")
CONVERT_SECONDS = 3600           # qemu-img convert of a build VM's disk
DISK_RESERVE = paths.tunable("AGENTBOX_BUILD_DISK_RESERVE_BYTES", 10 << 30)       # left free by the rewrite
# qemu-img convert as root on a disk a VM wrote: at most 4 GiB of address space (it aborts below
# 2 GiB once its thread pool grows, qemu-img 10.2) and a file no larger than the disk's virtual
# size and its metadata, nor than the room under images/ less DISK_RESERVE
CONVERT_AS = 4 << 30
CONVERT_MARGIN = 1 << 30         # qcow2 metadata beyond the guest's blocks: 20 MiB at 160 GiB
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

def build_records():
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
    for rec in build_records():
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


def backing(disk):
    """Base name of the backing file of a disk; "" without one; None when unreadable."""
    info = programs.disk_info(disk)
    backing = info.get("backing-filename", "") if info is not None else None
    return os.path.basename(backing) if isinstance(backing, str) else None


def in_use():
    """{disk file name: [VMs]} of the Builds System disks are backed by; None when a
    System disk cannot be read (then nothing may be deleted)."""
    used = {}
    for n in state_files.listdir(paths.VMS, fail=False):
        m = footprint.SYSTEM_DISK.fullmatch(n)
        if not m:
            continue
        backing_file = backing(footprint.system_disk(m.group(1)))
        if backing_file is None:
            return None
        if backing_file:
            used.setdefault(backing_file, []).append(m.group(1))
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


def _podman(*args, capture=False, timeout=600, what=None, input=None):
    """podman with its output in the Build's journal (or captured); _Failed when it fails."""
    sys.stdout.flush()
    binary = isinstance(input, (bytes, bytearray))
    try:
        r = subprocess.run(["podman", *args], input=input, stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None, text=not binary,
                           errors=None if binary else "replace", timeout=timeout)
    except FileNotFoundError:
        raise _Failed("podman is missing")
    except subprocess.TimeoutExpired:
        raise _Failed(f"{what or 'podman ' + args[0]}: time limit exceeded")
    if r.returncode != 0:
        detail = clean((r.stderr or "").strip(), False)[-200:] if capture else ""
        raise _Failed(f"{what or 'podman ' + args[0]} failed (exit {r.returncode})" + (f": {detail}" if detail else ""))
    return r.stdout if capture else None


def step_line(line):
    """{"step": n, "steps": m, "command"} of a line of podman build that begins step n of m of
    the Containerfile ("STEP 7/19: RUN dnf ..."), else None. The command is the Build's output,
    untrusted: one line without control characters, redacted as the build log is (tokens, keys
    and Pairing codes hidden), at most COMMAND_MAX characters."""
    m = STEP_LINE.fullmatch(line.rstrip("\r\n"))
    if not m or not 1 <= int(m[3]) <= int(m[4]):
        return None
    command = redact(m[5])
    if len(command) > COMMAND_MAX:
        command = command[:COMMAND_MAX - 1] + "\u2026"
    return {"step": int(m[3]), "steps": int(m[4]), "command": command}


class Steps:
    """podman's own STEP lines of one podman build. A RUN step's output can print anything,
    a line like "STEP 9999/9999: ..." too, so a STEP line counts only as the next one podman
    writes: step 1 first, then step n+1 of the same count in the same stage, or step 1 of the
    next stage of a multi-stage Containerfile ("[i/k] STEP n/m: ...", the same k). Any other
    STEP line is output of a step; at most it names the step that comes next."""

    def __init__(self):
        self.at = None               # (stage, stages, step, steps) of the last one taken

    def take(self, line):
        """The progress (step_line) when `line` is podman's next STEP line, else None."""
        found = step_line(line)
        if found is None:
            return None
        m = STEP_LINE.fullmatch(line.rstrip("\r\n"))
        stage, stages = (int(m[1]), int(m[2])) if m[1] else (None, None)
        if stage is not None and not 1 <= stage <= stages:
            return None
        n, total = found["step"], found["steps"]
        if self.at is None:
            ok = n == 1 and stage in (None, 1)
        else:
            last_stage, last_stages, last, last_total = self.at
            ok = (stage, stages) == (last_stage, last_stages) and total == last_total and n == last + 1 \
                or stage is not None and stages == last_stages and stage == last_stage + 1 and n == 1
        if not ok:
            return None
        self.at = (stage, stages, n, total)
        return found


def progress(rec):
    """Where the podman build of a Build record is (step_line of its latest STEP line) while the
    Build runs its step build; None otherwise."""
    p = rec.get("progress")
    if rec.get("status") != "running" or rec.get("step") != "build" or not isinstance(p, dict):
        return None
    if not all(isinstance(p.get(k), int) for k in ("step", "steps")) or not isinstance(p.get("command"), str):
        return None
    return {"step": p["step"], "steps": p["steps"], "command": p["command"]}


def _podman_build(*args, rec, input=None):
    """podman build with its output passed on to the Build's journal line by line, the latest
    STEP line of it in the running Build's record (`progress`); _Failed when it fails."""
    sys.stdout.flush()
    try:
        proc = subprocess.Popen(["podman", "build", *args], stdin=subprocess.PIPE if input is not None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise _Failed("podman is missing")

    def relay():
        # reads to the end whatever happens: podman would wait on a full pipe
        begins, steps, failed = True, Steps(), False
        for chunk in iter(lambda: proc.stdout.readline(65536), b""):
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            except (OSError, ValueError):
                pass
            line, begins = begins, chunk.endswith(b"\n")     # whether the chunk begins a line
            try:
                found = steps.take(chunk.decode("utf-8", "replace")) if line else None
                if found:
                    rec["progress"] = found
                    _save(rec)
            except Exception as e:    # the progress is lost, never the output: keep draining
                if not failed:
                    failed = True
                    print(f"agentbox: the Build's progress could not be saved: {e}", file=sys.stderr, flush=True)

    def feed():
        try:
            proc.stdin.write(input)
            proc.stdin.close()
        except OSError:
            pass

    threads = [threading.Thread(target=relay, daemon=True)]
    if input is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    try:
        code = proc.wait(timeout=4 * 3600)
    except subprocess.TimeoutExpired:
        code = None
    finally:                      # also on an interruption (KeyboardInterrupt, _terminate)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for t in threads:         # a process podman left behind may hold the pipe open
            t.join(timeout=30)
    if code is None:
        raise _Failed("podman build: time limit exceeded")
    if code != 0:
        raise _Failed(f"podman build failed (exit {code})")


def _rmi(tag):
    """Remove a container image; --force also ends a container of it that still runs (the
    version check's count, versions.py), so a Build never waits for one or leaves its
    image behind."""
    programs.quietly(["podman", "rmi", "--force", "--ignore", tag], 300)


def image_exists(tag):
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


def release_defined():
    """The Images the installed Release defines, or None when its files are not there."""
    if not os.path.isfile(os.path.join(paths.VM_SOURCE, "vm", "Containerfile")):
        return None
    return [BASE, *derived(paths.VM_SOURCE)]


def _recipe_image(image):
    """Whether a stored Recipe defines this Image now (a same-name Release Image wins)."""
    from . import recipes
    release = release_defined()
    return image in recipes.names() and (release is None or image not in release)


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
    container image still there and the digest of the fedora-bootc it is built on, which is
    the buildroot of the derived Image's disk step too. (build, record)."""
    build = current(BASE)
    rec = builds().get(BASE, {}).get(build) if build else None
    if not rec or rec["status"] != "ok" or not rec.get("safeguards"):
        raise _Failed("the Base image has no checked Build, run agentbox build base first")
    if not image_exists(CONTAINER.format(BASE, build)):
        raise _Failed(f"the container image of the Base Build {build} is missing, run agentbox build base first")
    if not digests.valid(rec.get("fedora_bootc_digest")):
        raise _Failed(f"the Base Build {build} records no digest of fedora-bootc, run agentbox build base first")
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


def _new_build(image, release, days, orca, playwright, pins, actor, unit_name, in_vm, recipe_sha256=None):
    """A new Build id and its first, running record, with the pinned digests of the run
    (`pins`: {name: digest}) and whether it runs in build VMs (`in_vm`, the Setting build_vm
    of the run); a derived Image's fedora-bootc is its Base Build's."""
    build = _stamp()
    while os.path.exists(record_file(image, build)) or os.path.exists(build_file(image, build)):
        time.sleep(1)
        build = _stamp()
    rec = {"image": image, "build": build, "status": "running", "step": "build", "started": int(time.time()),
           "finished": None, "release": release, "base_build": None, "recipe_sha256": recipe_sha256,
           "package_min_age_days": days, "orca_version": orca or None,
           "playwright_version": playwright or None,
           "fedora_bootc_digest": pins["fedora-bootc"] if image == BASE else None,
           "builder_digest": pins["bootc-image-builder"], "unit": unit_name,
           "actor": actor, "agents": None, "tools": None, "problems": [], "error": None, "safeguards": None,
           "container": False, "build_vm": in_vm}
    _save(rec)
    return build, rec


def _build_container(image, src, release, days, orca, playwright, rec, tag, say, recipe_data=None):
    """Build the Image's container image and return its Base Build record, or None for the Base image."""
    if image == BASE:
        fedora = digests.reference("fedora-bootc", rec["fedora_bootc_digest"])
        say(f"== {image}: building the container image from {fedora} (package minimum age: {days} days; Orca: "
            + (f"{orca}, pinned in orca_version" if orca else "the newest release at least that old")
            + "; Playwright: " + (f"{playwright}, pinned in playwright_version)" if playwright
                                  else "the newest version at least that old)"))
        if rec["build_vm"]:
            # the VM gets the platform image of the pin from the Host's storage, under a local name
            platform, why = digests.instance("fedora-bootc", rec["fedora_bootc_digest"])
            if why:
                raise _Failed(why)
            local = build_vm_run.FEDORA_BOOTC.format(rec["build"])
            _in_build_vm(image, rec, tag, [("FEDORA_BOOTC", local), ("PACKAGE_MIN_AGE_DAYS", days),
                                           ("ORCA_VERSION", orca), ("PLAYWRIGHT_VERSION", playwright)],
                         "vm/Containerfile", src, [(platform, local)], src, say)
            return None
        # the version check pulled the pinned digest; never anything by its tag
        _podman_build("--pull=missing", "--no-cache", "--build-arg", f"FEDORA_BOOTC={fedora}",
                      "--build-arg", f"PACKAGE_MIN_AGE_DAYS={days}",
                      "--build-arg", f"ORCA_VERSION={orca}", "--build-arg", f"PLAYWRIGHT_VERSION={playwright}",
                      "-t", tag, "-f", os.path.join(src, "vm", "Containerfile"), src, rec=rec)
        return None
    if isinstance(recipe_data, Fail):
        raise recipe_data             # the Recipe could not be taken at the start of the run
    context = os.path.join(src, "vm", "images", image)
    if recipe_data is None and not os.path.isfile(os.path.join(context, "Containerfile")):
        raise _Failed(f"Image {image} does not exist in Release {release} (vm/images/{image}/Containerfile)")
    base_build, base = _base_for_derived()
    # a derived Image keeps the Base Build's safeguards, its package age included, its Orca, its
    # Playwright and its fedora-bootc
    days = base["package_min_age_days"]
    rec.update(base_build=base_build, package_min_age_days=days, orca_version=base.get("orca_version"),
               playwright_version=base.get("playwright_version"), fedora_bootc_digest=base["fedora_bootc_digest"])
    _save(rec)
    say(f"== {image}: building the container image on the Base Build {base_build}")
    base_tag = CONTAINER.format(BASE, base_build)
    if recipe_data is not None:
        # One owned snapshot of the Recipe is handed over by a single call, never as a path.
        from . import recipes
        recipes.check(recipe_data)
        if rec["build_vm"]:
            _in_build_vm(image, rec, tag, [("BASE", base_tag)], None, recipe_data, [(base_tag, base_tag)], src, say)
            return base
        _podman_build("--pull=missing", "--no-cache", "--build-arg", f"BASE={CONTAINER.format(BASE, base_build)}",
                      "-t", tag, "-", input=recipe_data, rec=rec)
    else:
        if rec["build_vm"]:
            _in_build_vm(image, rec, tag, [("BASE", base_tag)], "Containerfile", context, [(base_tag, base_tag)],
                         src, say)
            return base
        _podman_build("--pull=missing", "--no-cache", "--build-arg", f"BASE={CONTAINER.format(BASE, base_build)}",
                      "-t", tag, "-f", os.path.join(context, "Containerfile"), context, rec=rec)
    return base


def _vm_failed(f):
    """A build VM's failure as the Build's: with its stable code when the Host lacks what a build
    VM needs (no KVM, no checked image, not enough memory), recorded with that code; else its
    reason as text."""
    return Fail(f.message, error=f.error, **f.params) if f.error else _Failed(f.message)


def _in_build_vm(image, rec, tag, build_args, containerfile, context, hand_in, src, say):
    """The podman build of _build_container in the Build's first build VM (build_vm_run.py, the
    Setting build_vm on): the same build arguments, the same context (a Recipe's tar as it is),
    the images it builds on handed in from the Host's storage; its log in the Build's journal,
    its STEP lines the Build's progress. Then the image archive it hands out goes into the
    Host's storage as `tag` (_import, the step import), and is deleted with its output."""
    steps = Steps()

    def seen(line):
        found = steps.take(line)
        if found:
            rec["progress"] = found
            _save(rec)

    def take(archive):
        rec.update(step="import")
        _save(rec)
        _import(image, rec["build"], archive, tag, say)

    job = {"tag": tag, "build_args": [[k, str(v)] for k, v in build_args], "containerfile": containerfile}
    try:
        build_vm_run.build_image(image, rec["build"], job, context, hand_in, src, say, seen, take)
    except build_vm_run.Failed as f:
        raise _vm_failed(f) from None


def _import(image, build, archive, tag, say):
    """The image archive of a first build VM into the Host's storage under the name the Host
    chose for the Build (`tag`), never one the archive names: not podman load, which takes the
    name from the archive and so could retag the current Base Build or any other image there.

    skopeo never reads the archive. Root reads it (_unpack, in a child process under
    ARCHIVE_LIMITS) and writes the image's files itself into a new directory, the OCI layout
    `layout` in build_vm_run.import_dir: its own oci-layout and index.json, which names the one
    manifest it checked and nothing else, and the manifest, the config and the layers copied out
    of the archive. Then `skopeo copy oci:<layout> containers-storage:<tag>`: skopeo reads only
    files root wrote and checked, so no difference between how Python and Go read a tar
    reaches it. skopeo and containers/storage check every blob against the manifest's digest;
    nothing from the image runs. skopeo runs in a network namespace of its own (unshare --net),
    so nothing an archive names makes root on the Host send a request anywhere.

    The import starts only with room for the blobs on the filesystem of the build VMs (as
    root extracts them), IMPORT_FACTOR times the layers' size in the Host's storage, and
    IMPORT_RESERVE left on each; while skopeo runs, the free space of both is read every
    build_vm_run.WATCH_SECONDS, and skopeo is killed and the import fails when either falls
    below IMPORT_RESERVE (a layer that unpacks to far more than its size). What a killed skopeo
    left of the image goes with the failed Build (podman rmi of tag); containers/storage deletes
    a layer it left incomplete when it next opens the store. The directory goes afterwards,
    whatever happened, and with leftovers() after a killed run."""
    work = build_vm_run.import_dir(image, build)
    layout, tmp = os.path.join(work, "layout"), os.path.join(work, "tmp")
    storage = paths.host(STORAGE)
    shutil.rmtree(work, ignore_errors=True)
    os.mkdir(work, 0o700)
    try:
        say(f"== {image}: checking the build VM's image archive and taking its image's files out of it")
        answer = _in_child(_unpack, archive, layout, storage)
        if answer:
            raise _Failed(answer["failed"] if "failed" in answer
                          else f"the build VM's image archive is refused: {answer['refused']}")
        os.mkdir(tmp, 0o700)
        say(f"== {image}: importing the image as {tag}")
        _watched(["unshare", "--net", "--", "skopeo", "--tmpdir", tmp, "copy", "--quiet", f"oci:{layout}",
                  f"containers-storage:{tag}"], (storage, paths.BUILD_VM))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _watched(argv, places):
    """The import's skopeo to its end, within IMPORT_SECONDS and while every filesystem of
    `places` keeps IMPORT_RESERVE free; _Failed otherwise, skopeo killed."""
    import tempfile
    with tempfile.TemporaryFile() as err:
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                 env=dict(os.environ, LC_ALL="C"))
        except OSError as e:
            raise _Failed(f"the build VM's image archive cannot be imported: {argv[0]}: {e.strerror or e}")
        deadline = time.monotonic() + IMPORT_SECONDS
        try:
            while True:
                try:
                    code = p.wait(timeout=build_vm_run.WATCH_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                short = [path for path in places if _free_at(path)[1] < IMPORT_RESERVE]
                if short:
                    raise _Failed(f"the import of the build VM's image archive was stopped: less than "
                                  f"{IMPORT_RESERVE} bytes free under {short[0]}")
                if time.monotonic() > deadline:
                    raise _Failed(f"the import of the build VM's image archive does not end within {IMPORT_SECONDS} s")
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
        if code != 0:
            err.seek(0)
            said = clean(" ".join(err.read(1 << 16).decode("utf-8", "replace").split())[-200:], False)
            raise _Failed(f"the build VM's image archive cannot be imported: skopeo copy failed (exit {code})"
                          + (f": {said}" if said else ""))


def _in_child(fn, *args):
    """fn(*args) in a child process under ARCHIVE_LIMITS: its answer (JSON), which a _Failed
    there turns into {"failed": its message}; {"refused": ...} when the child dies or answers
    nothing (Python's tarfile holds a member's extended header in memory, whatever its size).
    The child never outlives the run: the kernel kills it when the run ends (_die_with), and
    the run kills it when it is interrupted while it waits."""
    sys.stdout.flush()
    sys.stderr.flush()
    parent = os.getpid()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:                         # the child: its answer as JSON on the pipe, then gone
        _die_with(parent)
        try:
            os.close(r)
            build_vm.lower(ARCHIVE_LIMITS)
            answer = fn(*args)
        except _Failed as f:
            answer = {"failed": f.message}
        except BaseException as e:       # MemoryError among them: whatever it is, the archive is refused
            answer = {"refused": f"it cannot be read within its limits ({type(e).__name__})"}
        try:
            os.write(w, json.dumps(answer).encode()[:1 << 12])
        finally:
            os._exit(0)
    os.close(w)
    answered = False
    try:
        with os.fdopen(r, "rb") as f:
            data = f.read(1 << 12)
        answered = True
    finally:
        if not answered:                 # interrupted (the run's SIGTERM handler): no child copies on
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.waitpid(pid, 0)
    try:
        answer = json.loads(data)
    except ValueError:                   # no answer: the child was ended (its CPU time, say)
        answer = {"refused": "it cannot be read within its limits"}
    return answer if answer is None or isinstance(answer, dict) else {"refused": "it cannot be read"}


PR_SET_PDEATHSIG = 1                 # prctl(2), linux/prctl.h


def _die_with(parent):
    """In a child just forked: SIGKILL from the kernel when the process that forked it ends
    (prctl PR_SET_PDEATHSIG), so a run killed mid-import leaves no root process copying an
    archive; gone at once when that process ended before this was set, or when it cannot be
    set."""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        failed = libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0
    except (OSError, AttributeError):
        failed = True
    if failed or os.getppid() != parent:
        os._exit(1)


def _unpack(path, layout, storage):
    """In the child of _in_child: the OCI archive at `path` checked, then the files of its one
    image written into the new directory `layout` (root's, 0700; files 0600, never through a
    link): None, or {"refused": why}. _Failed without room for them.

    The tar: at most ARCHIVE_ENTRIES entries, each a regular file or a directory (no link,
    device, FIFO, sparse member or continuation) under a name with no empty, . or .. part,
    none twice, and nothing but NUL bytes after its end, at most ARCHIVE_TRAIL of them, so that
    no reader of the tar finds entries this one did not see. Its image: index.json names exactly
    one manifest; the manifest (at most ARCHIVE_JSON, matching its digest) an OCI config (at
    most ARCHIVE_JSON, matching its digest) and 1 to ARCHIVE_LAYERS OCI layers; every
    descriptor names its blob by a sha256 digest and its size, no URL to fetch it from, and the
    blob is in the archive with that size. Root writes oci-layout and index.json itself (the
    one manifest, its media type, digest and size) and copies the manifest, config and layer
    blobs out of the archive; nothing else."""
    import hashlib
    import tarfile
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        return {"refused": f"it cannot be opened: {e.strerror or e}"}
    with os.fdopen(fd, "rb") as raw:
        try:
            tar = tarfile.open(fileobj=raw, mode="r:")
            files = {}
            for count, m in enumerate(iter(tar.next, None), 1):
                if count > ARCHIVE_ENTRIES:
                    return {"refused": f"it holds more than {ARCHIVE_ENTRIES} entries"}
                name = m.name[2:] if m.name.startswith("./") else m.name
                name = name.rstrip("/") if m.isdir() else name
                shown = repr(clean(name[:100], False))
                if name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/")):
                    return {"refused": f"it holds an entry named {shown}"}
                if m.sparse is not None or any(k.startswith("GNU.sparse.") for k in m.pax_headers):
                    return {"refused": f"it holds {shown}, which is sparse"}
                if m.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
                    return {"refused": f"it holds {shown}, which is no regular file or directory"}
                if name in files:
                    return {"refused": f"it holds {shown} twice"}
                if not m.isdir():
                    files[name] = m
            end = tar.offset
        except tarfile.TarError:
            return {"refused": "it is no tar"}
        raw.seek(end)
        trail = raw.read(ARCHIVE_TRAIL + 1)
        if len(trail) > ARCHIVE_TRAIL or trail.strip(b"\0"):
            return {"refused": "it holds more after the end of its tar"}

        def read(name):
            m = files.get(name)
            return tar.extractfile(m).read(ARCHIVE_JSON + 1) if m is not None and m.size <= ARCHIVE_JSON else None

        def blob(d, types, what):
            """The blob's name in the archive of the descriptor d; _Refused otherwise."""
            if not isinstance(d, dict):
                raise _Refused(f"its {what} is no descriptor")
            if "urls" in d:
                raise _Refused(f"its {what} names URLs to fetch it from")
            if d.get("mediaType") not in types:
                raise _Refused(f"its {what} has the media type {clean(str(d.get('mediaType'))[:100], False)!r}")
            m = SHA256.fullmatch(d["digest"]) if isinstance(d.get("digest"), str) else None
            size = d.get("size")
            if not m or not isinstance(size, int) or isinstance(size, bool):
                raise _Refused(f"its {what} names no sha256 digest and size")
            name = f"blobs/sha256/{m.group(1)}"
            if name not in files or files[name].size != size:
                raise _Refused(f"its {what} is not in the archive")
            return name

        def checked_json(name, digest, what):
            data = read(name)
            if data is None or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                raise _Refused(f"its {what} does not match its digest, or is larger than {ARCHIVE_JSON} bytes")
            try:
                found = json.loads(data.decode("utf-8"))
            except (ValueError, RecursionError):
                found = None
            if not isinstance(found, dict):
                raise _Refused(f"its {what} is no JSON object")
            return found

        try:
            index = read("index.json")
            try:
                index = json.loads(index.decode("utf-8")) if index is not None else None
            except (ValueError, RecursionError):
                index = None
            if not isinstance(index, dict):
                raise _Refused(f"its index.json is missing, larger than {ARCHIVE_JSON} bytes or no JSON object")
            manifests = index.get("manifests")
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise _Refused(f"it holds {len(manifests) if isinstance(manifests, list) else 'no list of'} "
                               "images, not one")
            entry = manifests[0]
            name = blob(entry, (OCI_MANIFEST,), "image's manifest")
            manifest = checked_json(name, entry["digest"], "manifest")
            if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") not in (None, OCI_MANIFEST):
                raise _Refused("its manifest is no OCI image manifest")
            config = blob(manifest.get("config"), (OCI_CONFIG,), "config")
            checked_json(config, manifest["config"]["digest"], "config")
            layers = manifest.get("layers")
            if not isinstance(layers, list) or not layers:
                raise _Refused("its manifest names no layers")
            if len(layers) > ARCHIVE_LAYERS:
                raise _Refused(f"it has {len(layers)} layers, more than {ARCHIVE_LAYERS}")
            layer_blobs = [blob(layer, OCI_LAYERS, f"layer {n + 1}") for n, layer in enumerate(layers)]
        except _Refused as r:
            return {"refused": r.why}
        wanted = list(dict.fromkeys([name, config, *layer_blobs]))
        total = sum(files[n].size for n in wanted)
        layer_bytes = sum(files[n].size for n in dict.fromkeys(layer_blobs))
        _room_for(f"to import the build VM's image archive ({total} bytes, {layer_bytes} of them layers)",
                  [(paths.BUILD_VM, total), (storage, IMPORT_FACTOR * layer_bytes)], IMPORT_RESERVE)
        for folder in (layout, os.path.join(layout, "blobs"), os.path.join(layout, "blobs", "sha256")):
            os.mkdir(folder, 0o700)
        _write_new(os.path.join(layout, "oci-layout"), b'{"imageLayoutVersion": "1.0.0"}')
        _write_new(os.path.join(layout, "index.json"), json.dumps(
            {"schemaVersion": 2, "manifests": [{"mediaType": OCI_MANIFEST, "digest": entry["digest"],
                                                "size": entry["size"]}]}).encode())
        for n in wanted:
            out = os.open(os.path.join(layout, n), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                _copy_range(raw.fileno(), out, files[n].offset_data, files[n].size)
            finally:
                os.close(out)
    return None


class _Refused(Exception):
    def __init__(self, why):
        super().__init__(why)
        self.why = why


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _copy_range(src, dst, offset, size):
    """size bytes of the file src from offset to the file dst, in the kernel where it can."""
    done = 0
    while done < size:
        try:
            n = os.copy_file_range(src, dst, min(size - done, 1 << 30), offset + done)
        except OSError:                  # an older kernel across filesystems
            data = os.pread(src, min(size - done, 1 << 20), offset + done)
            n = os.write(dst, data) if data else 0
        if n <= 0:
            raise OSError(f"the archive ended {size - done} bytes early")
        done += n


def _free_at(path):
    """(device, bytes free) of the filesystem of path, or of its nearest parent that exists."""
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    st = os.statvfs(path)
    return os.stat(path).st_dev, st.f_bavail * st.f_frsize


def _room_for(what, needs, reserve):
    """_Failed unless each filesystem has room for what it is to take ([(path, bytes)], summed
    where two paths share one) and `reserve` besides."""
    wanted, free, where = {}, {}, {}
    for path, need in needs:
        dev, free[dev] = _free_at(path)
        wanted[dev] = wanted.get(dev, 0) + need
        where.setdefault(dev, path)
    short = [f"{where[dev]} needs {wanted[dev] + reserve} bytes free, has {free[dev]}"
             for dev in wanted if free[dev] < wanted[dev] + reserve]
    if short:
        raise _Failed(f"not enough free space {what}: " + "; ".join(short))


def _make_disk(image, build, tag, out, rec, src, say):
    """Create the Image's System disk from its checked container image: bootc-image-builder by
    its pinned digest, with the pristine fedora-bootc the Image is built on as the buildroot
    (--build-container), so the bootc that installs the Image, osbuild's stages and the tools
    that make the filesystems do not come from the Image. With build_vm on, in the
    Build's second build VM (_disk_in_build_vm)."""
    if rec["build_vm"]:
        return _disk_in_build_vm(image, build, tag, out, rec, src, say)
    builder = digests.reference("bootc-image-builder", rec["builder_digest"])
    buildroot, why = digests.instance("fedora-bootc", rec["fedora_bootc_digest"])
    if why:
        raise _Failed(why)
    say(f"== {image}: all Safeguards present. Creating the disk ({builder}, buildroot {buildroot})")
    with open(os.path.join(out, "config.toml"), "w") as f:
        f.write(DISK_CONFIG)
    _podman("run", "--rm", "--privileged", "--pull=missing", "--security-opt", "label=type:unconfined_t",
            "-v", f"{out}:/output", "-v", f"{os.path.join(out, 'config.toml')}:/config.toml:ro",
            "-v", f"{STORAGE}:{STORAGE}", builder, "--type", "qcow2", "--rootfs", "xfs",
            "--build-container", buildroot, "--config", "/config.toml", tag,
            timeout=2 * 3600, what="bootc-image-builder")
    disk = os.path.join(out, "qcow2", "disk.qcow2")
    if not os.path.isfile(disk):
        raise _Failed("bootc-image-builder created no disk")
    os.chmod(disk, 0o644)
    os.replace(disk, build_file(image, build))


def _disk_in_build_vm(image, build, tag, out, rec, src, say):
    """The disk step in the Build's second build VM, offline (build_vm_run.make_disk): the image
    the Host checked, bootc-image-builder and the buildroot go in from the Host's storage, each
    by its platform image as the Host's own disk step names the buildroot, under local names;
    the same arguments and config.toml as on the Host. Its disk.qcow2 becomes the Build's disk
    only as a new file the Host's qemu-img writes (_take_disk)."""
    builder, why = digests.instance("bootc-image-builder", rec["builder_digest"])
    if why:
        raise _Failed(why)
    buildroot, why = digests.instance("fedora-bootc", rec["fedora_bootc_digest"])
    if why:
        raise _Failed(why)
    say(f"== {image}: all Safeguards present. Creating the disk in a build VM ({builder}, buildroot {buildroot})")
    local_builder, local_root = build_vm_run.BUILDER.format(build), build_vm_run.FEDORA_BOOTC.format(build)
    job = {"tag": tag, "builder": local_builder, "buildroot": local_root, "config": DISK_CONFIG}
    hand_in = [(tag, tag), (builder, local_builder), (buildroot, local_root)]
    try:
        build_vm_run.make_disk(image, build, job, hand_in, src, say,
                               lambda disk: _take_disk(image, build, disk, out, say))
    except build_vm_run.Failed as f:
        raise _vm_failed(f) from None


def _take_disk(image, build, disk, out, say):
    """A build VM's disk.qcow2 as the Build's disk: its header read with the format named, never
    guessed (build_vm.qcow2_check: a qcow2 naming no backing file and no external data file,
    not encrypted, of at most build_vm_run.MAX_DISK), then rewritten into a new qcow2 in `out`
    (rewrite), which moves into place in one rename. Only with room under images/ for as much
    as the VM's file and DISK_RESERVE besides; the new file may grow to the disk's virtual size
    (and CONVERT_MARGIN), never beyond the room there less DISK_RESERVE."""
    problem, virtual = build_vm.qcow2_check(disk, build_vm_run.MAX_DISK)
    if problem:
        raise _Failed(f"the build VM's disk is refused: {problem}")
    _room_for("to write the Build's disk", [(paths.IMAGES, os.stat(disk).st_size)], DISK_RESERVE)
    say(f"== {image}: the disk's header is checked; writing the Build's disk from it")
    part = os.path.join(out, "disk.qcow2.part")
    problem = rewrite(disk, part, min(virtual + CONVERT_MARGIN, _free_at(paths.IMAGES)[1] - DISK_RESERVE))
    if problem:
        raise _Failed(f"the build VM's disk cannot be rewritten: {problem}")
    # root's alone before it is read: the unprivileged qemu-img that wrote it can change it no more
    if os.geteuid() == 0:
        os.chown(part, 0, 0)
    os.chmod(part, 0o600)
    # what that qemu-img wrote is checked as the VM's disk was before it becomes the Build's
    problem = build_vm.qcow2_problem(part, build_vm_run.MAX_DISK)
    if problem:
        raise _Failed(f"the rewritten disk is refused: {problem}")
    os.chmod(part, 0o644)
    os.replace(part, build_file(image, build))


def rewrite(disk, part, max_size):
    """qemu-img convert of the qcow2 `disk` into a new qcow2 `part`: None, or what went wrong.

    qemu-img parses bytes a VM chose, so it runs as build_vm_run.USER (setpriv), in a network
    namespace of its own (root's unshare --net before that), with at most CONVERT_AS of address
    space and files of at most max_size bytes. Root opens both files and hands them over as
    file descriptors (/dev/fd/N): the VM's disk read-only, never through a link, and `part`,
    which root makes (0600) and gives USER; USER needs no access to the directories they lie
    in. The json: filename opens the disk with "backing": null, so no backing file is opened
    even if one were named (an external data file it would still open: the caller's check
    refuses one, and nothing changes the read-only output in between); the new file has
    neither, and holds the guest's blocks and no header field the VM chose."""
    uid, gid = build_vm_run.ids()
    try:
        src = os.open(disk, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        return f"{disk} cannot be opened: {e.strerror or e}"
    try:
        dst = os.open(part, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        os.close(src)
        return f"{part} cannot be made: {e.strerror or e}"
    try:
        if os.geteuid() == 0:
            os.fchown(dst, uid, gid)
        source = "json:" + json.dumps({"driver": "qcow2", "file": {"driver": "file", "filename": f"/dev/fd/{src}"},
                                       "backing": None})
        argv = ["unshare", "--net", "--", "setpriv", f"--reuid={build_vm_run.USER}", f"--regid={build_vm_run.USER}",
                "--clear-groups", "--no-new-privs", "--", "qemu-img", "convert", "-q", "-f", "qcow2", "-O", "qcow2",
                source, f"/dev/fd/{dst}"]

        def limits():
            build_vm.lower({resource.RLIMIT_AS: CONVERT_AS, resource.RLIMIT_FSIZE: max(0, max_size)})
        try:
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                               timeout=CONVERT_SECONDS, preexec_fn=limits, pass_fds=(src, dst), cwd="/",
                               env=dict(os.environ, LC_ALL="C"))
        except FileNotFoundError:
            return "unshare is missing"
        except subprocess.TimeoutExpired:
            return f"qemu-img convert does not end within {CONVERT_SECONDS} s"
        except (OSError, subprocess.SubprocessError) as e:
            return f"qemu-img convert cannot be started: {e}"
        written = os.fstat(dst).st_size
    finally:
        os.close(src)
        os.close(dst)
    # at its file size limit qemu-img's write fails with EFBIG ("File too large"): it ignores SIGXFSZ
    if r.returncode != 0 and ("File too large" in r.stderr or written >= max_size):
        return f"it would be larger than {max_size} bytes"
    if r.returncode != 0:
        return f"qemu-img convert failed (exit {r.returncode}): " + clean(" ".join(r.stderr.split())[-200:], False)
    return None


def _failed(image, build, rec, tag, f, what, actor, reason, say):
    """Record and report a Build rejection or failure from the Build's own steps."""
    rec.update(status=f.status, finished=int(time.time()), error=f.message, problems=f.problems, container=False)
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
                    params={"image": image, "build": build, "reason": reason, "message": f.message,
                            "recipe_sha256": rec.get("recipe_sha256")})
    _save({**rec, "ended": int(time.time())})
    return False


def _error(image, build, rec, tag, e, what, actor, reason, say):
    """Record and report a command failure, Host error or interruption of a Build."""
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
                            "message": None if error else message, "recipe_sha256": rec.get("recipe_sha256")},
                    error=error, error_params=e.params if isinstance(e, Fail) else None)
    _save({**rec, "ended": int(time.time())})


def _done(image, build, rec, tag, release, what, actor, reason, say):
    """Finish a successful Build, its container cleanup, pruning and Activity entry."""
    # a derived Image's container image is not needed any more; the Base image keeps
    # only its current one (derived Images build on it)
    if image == BASE:
        for old in build_records():
            if old["image"] == BASE and old["build"] != build and old.get("container"):
                _rmi(CONTAINER.format(BASE, old["build"]))
                _save({**old, "container": False})
    else:
        _rmi(tag)
    removed = prune(image, say)
    note = f", {len(removed)} old Build{'s' if len(removed) != 1 else ''} removed" if removed else ""
    activity.record("build", "build.ok", f"{what}Build {image}-{build} done (Release {release}){note}",
                    actor=actor, params={"image": image, "build": build, "release": release,
                                         "recipe_sha256": rec.get("recipe_sha256"),
                                         "removed": len(removed) or None, "reason": reason})
    if os.path.exists(record_file(image, build)):
        _save({**rec, "ended": int(time.time())})        # the end of its log
    return build


def _build_one(image, src, release, days, orca, playwright, pins, actor, unit_name, say, in_vm, reason=None,
               recipe_data=None, recipe_sha256=None):
    """Build one Image from the Release's files or its Recipe in the unit unit_name, the Base image with
    the package minimum age `days`, Orca's release `orca` (orca_version, "" for the newest old
    enough) and Playwright's version `playwright` (playwright_version, likewise), with the
    pinned digests `pins` ({name: digest}, digests.py), in build VMs when `in_vm` (the Setting
    build_vm of the run); the Build's id when it became the current Build, else False. The
    Activity entries of an update's Builds have the reason "update"."""
    what = "Update: " if reason == "update" else ""
    build, rec = _new_build(image, release, days, orca, playwright, pins, actor, unit_name, in_vm, recipe_sha256)
    tag = CONTAINER.format(image, build)
    out = os.path.join(paths.IMAGES, f".build-{image}-{build}")
    os.makedirs(out)
    try:
        try:
            base = _build_container(image, src, release, days, orca, playwright, rec, tag, say, recipe_data)
            rec.update(step="check", container=True)
            _save(rec)
            say(f"== {image}: checking the Safeguards")
            manifest, agents, tools = _check(image, tag, rec["package_min_age_days"], base)
            rec.update(step="disk", agents=agents, tools=tools, safeguards=manifest if image == BASE else None)
            _save(rec)
            _make_disk(image, build, tag, out, rec, src, say)
            rec.update(status="ok", step="done", finished=int(time.time()), container=image == BASE)
            _save(rec)
            with state_files.locked(paths.IMAGES):
                _switch(image, build)
            say(f"== {image}: Build {build} is the current one (applies to VMs from their next Reset)")
        except _Failed as f:
            return _failed(image, build, rec, tag, f, what, actor, reason, say)
        except (Fail, OSError, KeyboardInterrupt) as e:
            _error(image, build, rec, tag, e, what, actor, reason, say)
            if isinstance(e, KeyboardInterrupt):
                raise
            return False
    finally:
        shutil.rmtree(out, ignore_errors=True)
    return _done(image, build, rec, tag, release, what, actor, reason, say)


def _cleanup(say):
    """Before a Build (holding the run lock): leftovers of an interrupted one."""
    for n in state_files.listdir(paths.IMAGES, fail=False):
        if re.fullmatch(r"\.build-.+|build-\d{8}-\d{4}", n) and os.path.isdir(os.path.join(paths.IMAGES, n)):
            shutil.rmtree(os.path.join(paths.IMAGES, n), ignore_errors=True)
    for rec in build_records():
        if rec.get("status") == "running":
            rec.update(status="failed", error="interrupted", finished=rec.get("finished") or int(time.time()))
            _save(rec)
            _rmi(CONTAINER.format(rec["image"], rec["build"]))
            say(f"Interrupted Build {rec['image']}-{rec['build']} marked as failed")
    build_vm_run.leftovers(say)
    _remove_retired(say)


def _remove_retired(say):
    """Holding the run lock: the images of digests the version check no longer pins; the
    current Base Build's fedora-bootc stays, its derived Images' disk steps use it as their
    buildroot (digests.remove_retired)."""
    base = builds().get(BASE, {}).get(current(BASE)) or {}
    digests.remove_retired(base.get("fedora_bootc_digest"), say)


def remove_retired(say):
    """The version check's, right after it pinned (versions.py): what _remove_retired removes,
    only when no Build runs, without waiting for one: a running run's Builds may still use a
    retired image, and the next check or run removes it."""
    if not digests.load().get("retired"):
        return
    with state_files.locked_unless_held(RUN_LOCK) as mine:
        if not mine:
            say("A Build is running: the retired images stay until the next check or Build run")
            return
        _remove_retired(say)


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
    from . import lifecycle                  # lifecycle imports this module
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
                running = states.running(rest)
                activity.record("reset", "reset.update", f"Reset after the Update of {rest_image} interrupted, the VM "
                                + ("is running" if running else "is off"), rest, "failed", actor=actor,
                                params={"image": rest_image, "running": running}, error="interrupted")
            raise
        except Fail as f:
            failed = True
            message = " ".join(f.message.strip().removeprefix("agentbox: ").split()) or f"Exit {f.exit}"
            running = states.running(vm)
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


def _check_versions(say, count=True):
    """The version check of Update now, as the Maintenance window runs it: the fetch as
    agentbox-feeds through its unit (systemctl start waits for it), then the root part
    (versions.check). What fails stays unknown and never stops the update. Without `count`
    (a run that holds the run lock, _pins) it counts no package updates and keeps the counts of
    the check before."""
    say(f"== Checking versions: asking npm, GitHub, quay.io and fedoraproject.org ({versions.FETCH_UNIT}, "
        "as agentbox-feeds)")
    try:
        # longer than the unit's own limit: systemd ends the fetch before the root part reads its files
        r = programs.systemctl("start", versions.FETCH_UNIT, timeout=versions.FETCH_SECONDS + 60, ok=None)
        if r.returncode != 0:
            say("Fetch failed: " + programs.summary(r))
    except Fail as f:
        say(f"Fetch failed: {f.message}")
    say("== Checking versions: package updates and what a new Build would bring")
    try:
        versions.check(say, count=count)
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
    tool it would change, or package updates) that the installed Release or a Recipe defines;
    with the Base image every such Image, since the others build on it (as build --all)."""
    view = overview() if view is None else view
    wanted = [i["name"] for i in view["images"] if i["update"] and (i["in_release"] or i["recipe"])]
    if BASE in wanted:
        return [BASE, *(i["name"] for i in view["images"]
                        if i["name"] != BASE and (i["in_release"] or i["recipe"]))]
    return wanted


def _recipe_snapshot(name):
    """(tar, SHA-256) of a Recipe, taken once for the whole run like the Release copy, so a save
    during the run changes no Build of it; (Fail, None) when it cannot be read or is refused,
    which fails only that Image's Build."""
    from . import recipes
    try:
        data = recipes.read(name)
    except Fail as f:
        return f, None
    return data, recipes.check(data)["sha256"]


def _pins(base, in_vm, say):
    """The pinned digests a run's Builds use ({name: digest or None}, digests.py), read once:
    bootc-image-builder always, fedora-bootc only for a run that builds the Base image (`base`;
    a derived Image takes its Base Build's). With one it needs not pinned yet (no version check
    has pulled it), or whose image is not in the Host's container storage (podman system prune;
    quay.io drops an old fedora-bootc digest, so podman could not pull it by itself), or, for
    a run in build VMs (`in_vm`), without a checked build VM image (build_vm.current()), the
    version check runs first, as Update now runs it: it pulls the pinned digest again or pins
    the tag's current one, and fetches and checks the build VMs' image. Still none, or still not
    there, fails the run (no build VM image: build_vm_no_image). A run without the
    Base image fails at once when the fedora-bootc its Base Build is built on, its disk steps'
    buildroot, is not there: quay.io no longer serves that digest once the tag moved on, so
    only a new Base Build helps."""
    needed = [n for n in digests.NAMES if base or n != "fedora-bootc"]

    def absent(pins):
        return [digests.tagged(n) for n in needed
                if pins[n] and not digests.present(digests.reference(n, pins[n]["digest"]))]

    pins = digests.pinned()
    unpinned, gone = [digests.tagged(n) for n in needed if not pins[n]], absent(pins)
    if unpinned:
        say(f"== No pinned digest of {', '.join(unpinned)} yet: running the version check first")
    if gone:
        say(f"== The image of the pinned digest of {', '.join(gone)} is not on the Host: "
            "running the version check first")
    no_image = in_vm and not build_vm.current()
    if no_image:
        say("== No checked build VM image yet: running the version check first")
    if unpinned or gone or no_image:
        _check_versions(say, count=False)        # it holds the run lock
        pins = digests.pinned()
    if in_vm and not build_vm.current():
        die("there is no checked build VM image: the version check could not fetch and check one "
            "(systemctl start agentbox-versions; journalctl -u agentbox-versions)", error="build_vm_no_image")
    missing = [digests.tagged(n) for n in needed if not pins[n]]
    if missing:
        die(f"no pinned digest of {', '.join(missing)}: the version check could not pin one "
            "(systemctl start agentbox-versions; journalctl -u agentbox-versions)", error="action_failed")
    gone = absent(pins)
    if gone:
        die(f"the image of the pinned digest of {', '.join(gone)} is not on the Host and the version check "
            "could not pull it (systemctl start agentbox-versions; journalctl -u agentbox-versions)",
            error="action_failed")
    if not base:
        build = current(BASE)
        digest = digests.valid((builds().get(BASE, {}).get(build) or {}).get("fedora_bootc_digest"))
        ref = digests.reference("fedora-bootc", digest) if digest else None
        if ref and not digests.present(ref):
            die(f"the fedora-bootc the Base Build {build} is built on ({ref}) is not on the Host, and quay.io "
                "serves a digest of it only while its tag points there: build the Base image again "
                "(agentbox build base)", error="action_failed")
    for name in needed:
        say(f"{digests.tagged(name)}: pinned {pins[name]['digest']}")
    return {name: pins[name]["digest"] if name in needed else None for name in digests.NAMES}


def _run(target, actor, say):
    """The Builds of build-run, one at a time (the run lock): [(Image, Build id or False)]. An
    update decides under the lock which Images it builds (update_images), so a Build that
    ended while it waited for the lock counts; when none needs one, it builds nothing. The
    version check's count in a build VM, which holds the lock too, gives way to it: the run
    says it wants the lock before it blocks on it, whoever holds it (build_vm_run.want_lock)."""
    reason = "update" if target == UPDATE else None
    build_vm_run.want_lock()
    with state_files.locked(RUN_LOCK):
        build_vm_run.lock_taken()
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
            step, in_vm = "source", None       # None: the Settings could not be read
            try:
                values = settings.load()
                in_vm = values["build_vm"]
                step = "pins"
                pins = _pins(target in ("all", BASE) or BASE in (wanted or []), in_vm, say)
                step = "source"
                days, orca = values["package_min_age_days"], values["orca_version"]
                playwright = values["playwright_version"]
                src, release = take_source(work, say)
            except Fail as f:
                # nothing to build with or from: one failed Build of the Image (the Base image for all)
                image = BASE if target == "all" else wanted[0] if wanted else target
                build = _stamp()
                _save({"image": image, "build": build, "status": "failed", "step": step, "started": started,
                       "finished": int(time.time()), "release": None, "base_build": None, "recipe_sha256": None,
                       "package_min_age_days": None, "orca_version": None, "playwright_version": None,
                       "fedora_bootc_digest": None, "builder_digest": None, "unit": unit(target), "actor": actor,
                       "agents": None,
                       "tools": None, "problems": [], "error": f.message, "safeguards": None, "container": False,
                       "build_vm": in_vm})
                head = "No checked build VM image" if f.error == "build_vm_no_image" else \
                    "Pinned images missing" if step == "pins" else "No sources"
                say(f"== {head}: {f.message}")
                say(f"The previous Build stays in use ({current(image) or 'none'})")
                activity.record("build", "build.failed", ("Update: " if reason else "")
                                + f"{'Build of all Images' if target == 'all' else f'Build {image}-{build}'} failed: "
                                + f.message, outcome="failed", actor=actor,
                                params={"image": image, "build": build, "all": target == "all", "reason": reason},
                                error=f.error, error_params=f.params)
                raise Fail("", 1, raw=True, error="action_failed")
            from . import recipes
            released = derived(src)
            if target == "all" or BASE in (wanted or []):
                build_images = [BASE, *released, *(name for name in recipes.names() if name not in released)]
            else:
                build_images = wanted or [target]
            snapshots = {name: _recipe_snapshot(name) for name in build_images
                         if name != BASE and name not in released and name in recipes.names()}
            return [(image, _build_one(image, src, release, days, orca, playwright, pins, actor, unit(target), say,
                                       in_vm, reason, recipe_data=snapshots.get(image, (None, None))[0],
                                       recipe_sha256=snapshots.get(image, (None, None))[1]))
                    for image in build_images]
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
    if state_files.held(RUN_LOCK) or any(states.build_running(unit(t)) for t in builds):
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
    from . import recipes
    names = dict.fromkeys([*available(), *builds(), *(release_defined() or []), *recipes.names()])
    if any(states.build_running(unit(t)) for t in (UPDATE, "all", *names)):
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
    return [r for r in build_records() if r.get("unit") == unit(target) and isinstance(r.get("started"), int)
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
    failed: a run that fails before it begins a Build leaves no Build record (states.failed_since)."""
    return states.failed_since(unit(target), started)


# --- commands ------------------------------------------------------------------------------

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
    if states.build_running(name):
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
        if states.build_running(name) or state_files.read(_request_file(target), None) != mine:
            return
        state_files.remove(_request_file(target), fail=False)


def build_cmd(args):
    """agentbox build <image>|--all [--reset <vm>]... [--json]: start a Build as a systemd
    unit; with --reset an Update: when the Build becomes the current one, these VMs are
    reset onto it."""
    positional, opts = options.parse(args)
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
    positional, opts = options.parse(args)
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
    fedora, builder = digests.valid(info.get("fedora_bootc_digest")), digests.valid(info.get("builder_digest"))
    updates = versions.updates(tools, offered) if info["status"] in USABLE else []
    return {"build": info["build"], "status": info["status"], "step": info.get("step"),
            "progress": progress(info), "started": times.iso(info.get("started")), "finished": times.iso(info.get("finished")),
            "release": info.get("release"), "commit": info.get("commit"), "base_build": info.get("base_build"),
            "recipe_sha256": info.get("recipe_sha256"), "build_vm": info.get("build_vm"),
            "package_min_age_days": info.get("package_min_age_days"), "orca_version": info.get("orca_version"),
            "playwright_version": info.get("playwright_version"), "fedora_bootc_digest": fedora,
            "builder_digest": builder, "agents": info.get("agents"), "tools": tools, "updates": updates,
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
    version check found (`available`, versions.py), with the pinned digests a new Build would
    use (`available.digests`, digests.py); a Build's `fedora_bootc_digest` and `builder_digest`
    are those it was built with. A digest pinned since is no update: the next Build takes it.
    An Image's `update`: a new Build would change a tool or bring package updates (None:
    nothing to tell it by). An Image's
    Containerfile (`containerfile`, its path in the repository) links to the repository at the
    tag or commit its current Build came from (`containerfile_url`, None when that is not
    known); `repository` is where the Releases come from. Links only: nothing is fetched."""
    from . import recipes
    found, defined = builds(), release_defined()
    stored_recipes = recipes.names()
    active_recipes = {name for name in stored_recipes if defined is None or name not in defined}
    used = in_use()
    pins, users = pinned()
    values = settings.load()
    checked = versions.load()
    offered = versions.available(checked or {}, values["orca_version"] or None, values["playwright_version"] or None)
    offered["digests"] = {name: {**pin, "since": times.iso(pin["since"]), "pinned": times.iso(pin["pinned"]),
                                 "image": digests.tagged(name)} if pin else None
                          for name, pin in digests.pinned().items()}
    names = set(found) | set(users) | set(available()) | set(defined or []) | active_recipes
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
        is_recipe = image in active_recipes
        source = definition(image) if not is_recipe else f"Recipe {image}: Containerfile (current recipe.tar)"
        problem = f"the installed Release also defines Image {image}; the Release wins and its Recipe is not built" \
            if defined is not None and image in defined and image in stored_recipes else None
        images.append({
            "name": image, "base": image == BASE, "recipe": is_recipe,
            "in_release": False if is_recipe else (image in defined if defined is not None else None),
            "current": cur, "vms": sorted(users.get(image, [])), "problem": problem,
            "running": {"build": running["build"], "step": running.get("step"), "progress": progress(running),
                        "build_vm": running.get("build_vm"),
                        "started": times.iso(running.get("started"))} if running else None,
            "package_updates": packages, "updates": changes, "update": bool(changes) if known else None,
            "containerfile": source,
            "containerfile_url": None if is_recipe else version.file_url(repository, ref, definition(image)),
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


def step_text(step, at):
    """A running Build's step for people: "step check", or in the step build where its podman
    build is ("build: step 7/19: RUN dnf ...", `at` from progress())."""
    return f"{step}: step {at['step']}/{at['steps']}: {at['command']}" if at else f"step {step}"


def images_cmd(args):
    """agentbox images [--json]: Images and their Builds, which VMs use and pin which."""
    if any(a != "--json" for a in args):
        die("images [--json]", error="invalid_argument")
    result = overview()
    # an update of everything (Update now, or the automatic one) runs: it may be checking the
    # versions, before any Build of it shows
    result["updating"] = states.build_running(unit(UPDATE))
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False))
        return
    offered = result["available"]
    if result["updating"]:
        print(f"Update running ({unit(UPDATE)}): checking versions, building the Images with an update, "
              "then the Resets")
    for image in result["images"]:
        head = f"Image {image['name']}" + (" (Base image)" if image["base"] else "")
        if image["recipe"]:
            head += " (Recipe)"
        head += f", current: {image['current'] or '-'}, VMs: {', '.join(image['vms']) or '-'}"
        if image["in_release"] is False and not image["recipe"]:
            head += " (not in the installed Release)"
        print(head)
        if image["problem"]:
            print(f"  Problem: {image['problem']}")
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
                line += "  " + step_text(b["step"], b["progress"])
            if b["error"]:
                line += f"  {b['error']}"
            if notes:
                line += "  (" + "; ".join(notes) + ")"
            print(line)
            if b["status"] in USABLE and any(b["tools"].values()):
                print(" " * 19 + versions.text(b["tools"]))
            if b["fedora_bootc_digest"] or b["builder_digest"]:
                print(" " * 19 + f"fedora-bootc {digests.short(b['fedora_bootc_digest'])}, "
                      f"bootc-image-builder {digests.short(b['builder_digest'])}")
    tools = offered["tools"]
    parts = [f"{versions.NAMES[t]} {tools[t] or '?'}" for t in ("orca", *versions.NPM)]
    if offered["checked"]:
        when = times.local(times.epoch(offered["checked"]))
        pin = (f"; Orca pinned to {offered['orca_version']} (orca_version)" if offered["orca_version"] else "") \
            + (f"; Playwright pinned to {offered['playwright_version']} (playwright_version)"
               if offered["playwright_version"] else "")
        print(f"Available (checked {when}, at least {offered['package_min_age_days']} days old{pin}): "
              + ", ".join(parts))
    else:
        print("Available: not checked yet (daily; now: systemctl start agentbox-versions)")
    pins = [f"{p['image']} {digests.short(p['digest'])} (since {times.local(times.epoch(p['since']))})"
            if p else f"{digests.tagged(n)} none yet" for n, p in offered["digests"].items()]
    print("Pinned for the next Build: " + ", ".join(pins))
    if not result["disks_readable"]:
        print("agentbox: not every System disk is readable, Builds in use unknown", file=sys.stderr)


def _read_definition(image):
    """(bytes, size) of the Image's Containerfile in the installed Release or current Recipe,
    cut at CONTAINERFILE_MAX bytes after its last whole line."""
    if _recipe_image(image):
        from . import recipes
        data = recipes.containerfile(image)
        size = len(data)
        if len(data) > CONTAINERFILE_MAX:
            cut = data.rfind(b"\n", 0, CONTAINERFILE_MAX)
            data = data[:cut + 1] if cut >= 0 else data[:CONTAINERFILE_MAX]
        return data, size
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
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("containerfile <image> [--json]", error="invalid_argument")
    image = _image_name(positional[0])
    data, size = _read_definition(image)
    text = SECRET.sub("[hidden]", clean(data.decode("utf-8", "replace")))
    truncated = len(data) < size
    release = _release()
    is_recipe = _recipe_image(image)
    source = f"Recipe {image}: Containerfile (current recipe.tar)" if is_recipe else definition(image)
    url = None if is_recipe else version.file_url(version.REPOSITORY, version.tag(release), definition(image))
    if "json" in opts:
        from . import recipes
        print(json.dumps({"image": image, "path": source, "release": release, "url": url,
                          "recipe_sha256": recipes.sha256(image) if is_recipe else None,
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
    out = activity.journalctl(argv)
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
    positional, opts = options.parse(args)
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
                          "progress": progress(info), "error": info.get("error"), "problems": info.get("problems") or [], "entries": entries,
                          "cursor": entries[-1]["cursor"] if entries else opts.get("after-cursor"),
                          "truncated": truncated}, ensure_ascii=False))
        return
    at = progress(info)
    print(f"Build {image}-{info['build']}: {STATUS.get(info['status'], info['status'])}"
          + (f" ({step_text(info['step'], at)})" if at else "")
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
    positional, opts = options.parse(args)
    if len(positional) != 2:
        die("set-image <vm> <image> [--build B] [--json]", error="invalid_argument")
    vm = registry.check(positional[0])
    image = _image_name(positional[1])
    build = _build_id(opts["build"]) if "build" in opts else None
    with state_files.locked(footprint.record_file(vm)):
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


DECLARATIONS = {
    "images": declare(images_cmd, "agentbox images [--json]", options={"json": False}, read_only=True,
                       control_room=True, allow_unknown=True),
    "build": declare(build_cmd, "agentbox build <image>|--all [--reset <vm>]... [--json]",
                      options={"all": False, "reset": options.MANY, "json": False}, control_room=True,
                      kind="build",
                      describe=lambda a: activity.describe_then_reset(
                          a, "Start a Build of all Images", all=True) if "all" in a.values else
                          activity.describe_then_reset(
                              a, f"Start a Build of {a.positional[0] if a.positional else ''}",
                              image=activity.name(a.positional[0] if a.positional else "", settings.IMAGE))),
    "build-run": declare(build_run, "agentbox build-run <image>|all", options={}, own_entries=True),
    "build-log": declare(build_log, "agentbox build-log <image> [<build>] [--after-cursor C] [--lines N] [--json]",
                         options={**LOG_CHECKS, "json": False}, read_only=True, control_room=True),
    "set-image": declare(set_image, "agentbox set-image <vm> <image> [--build B] [--json]",
                         options={"build": True, "json": False}, control_room=True, kind="build",
                         describe=activity.describe_set_image),
    "update-now": declare(update_now_cmd, "agentbox update-now [--reset <vm>]... [--json]",
                           options={"reset": options.MANY, "json": False}, control_room=True, kind="build",
                           describe=lambda a: activity.describe_then_reset(
                               a, "Update now: check the versions, build every Image with an update")),
    "containerfile": declare(containerfile_cmd, "agentbox containerfile <image> [--json]",
                              options={"json": False}, read_only=True, control_room=True),
}
