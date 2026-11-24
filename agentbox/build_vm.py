"""The build VMs (ADR 0025, #144): throwaway VMs that are to run the Builds. So far their image;
nothing boots it yet, and Builds run on the Host.

Their image is the Fedora Cloud Base Generic qcow2 of the Fedora release whose fedora-bootc the
Builds take (RELEASE, the tag in digests.UPSTREAM), as Fedora publishes and signs it with the
release under releases/<N>/Cloud/x86_64/images/ (DIRECTORY). Nothing is built or changed on the
Host to make it. Fedora does not update that image during the release, so it is renewed when
the release of fedora-bootc changes (a Release of Agent Box that moves the tag), not more often.
The version check (versions.py) finds and checks it, in its two parts:

  the fetch (agentbox fetch-versions, as agentbox-feeds) reads RELEASES for the entry of the
  release, Cloud_Base, x86_64, whose link is a Generic .qcow2 directly in DIRECTORY; when its
  name differs from the image in use (RECORD, which its unit binds read-only) it downloads the
  image and the directory's CHECKSUM into DOWNLOADS (its StateDirectory), over HTTPS only, at
  most MAX_IMAGE bytes, with time limits, and checks the download against the SHA-256
  releases.json lists (a broken download, not a check: releases.json is not signed). Once the
  image is in use, the next fetch deletes its download.

  check-versions (root) reads the fetch's files as untrusted input: through a directory opened
  without following a link, regular files only, bounded sizes, the name of the release's image
  and nothing else, the CHECKSUM's name built from it. gpgv runs as agentbox-feeds (setpriv),
  not root, with a keyring root makes from the Host's own RPM-GPG-KEY-fedora-<N>-primary
  (fedora-gpg-keys, updated with the Host); only its exit 0 with a VALIDSIG and none of
  REFUSED_STATUS counts, since gpgv writes the text of a bad signature too and exits 0 for one
  by an expired or revoked key, and --output - gives only the signed text. Root reads
  that text (at most MAX_TEXT), takes the one SHA-256 line of exactly the image's name, and
  copies the downloaded image into IMAGES while it hashes what it copies, so what it compares
  is what it keeps; then reads the copy's header with qemu-img info -f qcow2 under limits
  (qcow2_problem: no backing file, no external data file, no encryption). Only then it renames
  the copy to IMAGES/<name> (0644) and records it in RECORD. A failed step leaves the image in
  use as it is. The image it replaces goes once no Build runs (the run lock, not waited for),
  else at the next check.

A new image is no update: it starts no Build.
"""
import base64
import hashlib
import json
import os
import re
import resource
import shutil
import stat
import subprocess
import tempfile
import time

from . import digests, paths, state_files
from .sanitize import clean

RELEASE = digests.UPSTREAM["fedora-bootc"][1]
RELEASES = "https://fedoraproject.org/releases.json"
MIRROR = "https://download.fedoraproject.org/pub/fedora/linux/releases/"
IMAGES = os.path.join(paths.BUILD_VM, "images")             # root's checked image
RECORD = os.path.join(paths.BUILD_VM, "image.json")         # the image in use
# the fetch's download, in its StateDirectory (versions.AVAILABLE_DIR)
DOWNLOADS = os.path.join(paths.STATE, "available", "build-vm")
FEEDS_USER = "agentbox-feeds"
MAX_IMAGE = paths.tunable("AGENTBOX_BUILD_VM_IMAGE_BYTES", 2 << 30)    # Fedora 44's: 557 MiB
MAX_TEXT = 64 << 10             # the CHECKSUM and its signed text: a few hundred bytes
MAX_VIRTUAL = 20 << 30          # Fedora 44's image: 5 GiB; the system disk on it is 20 GiB
DOWNLOAD_SECONDS = paths.tunable("AGENTBOX_BUILD_VM_DOWNLOAD_SECONDS", 2400)
GPGV_SECONDS = 60
# qemu-img on a qcow2 the Host did not write: its address space and CPU time
QEMU_IMG_LIMITS = {resource.RLIMIT_AS: 1 << 30, resource.RLIMIT_CPU: 30}
QEMU_IMG_SECONDS = 60
CHUNK = 1 << 20
# the errors the fetch reports (versions.FETCH_ERROR takes them)
ERRORS = (r"fedoraproject\.org not reachable|fedoraproject\.org answered [1-5][0-9][0-9]|"
          r"no Cloud Base Generic qcow2 of the release in releases\.json|"
          r"more than one Cloud Base Generic qcow2 of the release in releases\.json|"
          r"download of the (image|CHECKSUM) failed|the download does not match releases\.json")
NOT_FETCHED = "not fetched yet; the next fetch asks fedoraproject.org"
# gpgv's status lines that refuse a signature even when it exits 0 with VALIDSIG
REFUSED_STATUS = ("BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG")
SIGNED_LINE = re.compile(r"SHA256 \(([^()\s]{1,255})\) = ([0-9a-f]{64})")


def directory(release=RELEASE):
    """Where Fedora keeps the release's signed Cloud images."""
    return f"{MIRROR}{release}/Cloud/x86_64/images/"


def image_name(release=RELEASE):
    """The pattern of the release's image's file name; its group is the compose (1.7)."""
    return re.compile(rf"Fedora-Cloud-Base-Generic-{re.escape(release)}-([0-9]{{1,4}}\.[0-9]{{1,4}})\.x86_64\.qcow2")


def checksum_name(name, release=RELEASE):
    """The CHECKSUM file that signs the image `name` (of the release's pattern)."""
    return f"Fedora-Cloud-{release}-{image_name(release).fullmatch(name).group(1)}-x86_64-CHECKSUM"


def key_file(release=RELEASE):
    """The Host's own key of the release, which signs its CHECKSUM files."""
    return os.path.join(paths.RPM_GPG, f"RPM-GPG-KEY-fedora-{release}-primary")


def load():
    """The record of the image in use ({name, release, sha256, checked}), or None."""
    rec = state_files.json_object(RECORD)
    if not rec or not isinstance(rec.get("release"), str) or not re.fullmatch(r"[0-9]{1,3}", rec["release"]):
        return None
    name = rec.get("name")
    if not isinstance(name, str) or not image_name(rec["release"]).fullmatch(name):
        return None
    return rec


def current():
    """The checked image of RELEASE a build VM boots: its path, or None when there is none (no
    check took one yet, or the one in use is of another release)."""
    rec = load()
    if not rec or rec["release"] != RELEASE:
        return None
    path = os.path.join(IMAGES, rec["name"])
    return path if os.path.isfile(path) else None


def line(got):
    """One line of the version check about the build VMs' image."""
    text = f"Build VM image (Fedora Cloud {RELEASE}): {got.get('image') or 'unknown'}"
    if got.get("error"):
        text += f" ({got['error']})"
    elif got.get("state"):
        text += f", {got['state']}"
    return text


# --- the fetch (agentbox-feeds) ---------------------------------------------------------------

def listed(listing, release=RELEASE):
    """[(name, sha256, size)] of the release's Generic qcow2 in releases.json (Fedora's list of
    everything it releases), only one whose link lies directly in DIRECTORY."""
    found = set()
    pattern = image_name(release)
    for entry in listing if isinstance(listing, list) else []:
        if not isinstance(entry, dict) or (entry.get("version"), entry.get("arch"), entry.get("variant"),
                                           entry.get("subvariant")) != (release, "x86_64", "Cloud", "Cloud_Base"):
            continue
        link, sha, size = entry.get("link"), entry.get("sha256"), entry.get("size")
        if not isinstance(link, str) or not link.startswith(directory(release)):
            continue
        name = link[len(directory(release)):]
        if not pattern.fullmatch(name) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) \
                or not isinstance(size, str) or not re.fullmatch(r"[0-9]{1,12}", size):
            continue
        found.add((name, sha, int(size)))
    return sorted(found)


def _hash(path):
    """(SHA-256, size) of a file, or (None, None)."""
    h, size = hashlib.sha256(), 0
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
                size += len(chunk)
    except OSError:
        return None, None
    return h.hexdigest(), size


def download(url, dest, cap, seconds):
    """curl url into the file dest (replaced), HTTPS only, also its redirects, at most `cap`
    bytes, for at most `seconds`: whether it got all of it. curl writes to a pipe and this reads
    it, so a body without a length is bound too."""
    argv = ["curl", "-q", "-s", "-f", "-L", "--max-redirs", "5", "--proto", "=https", "--proto-redir", "=https",
            "--connect-timeout", "30", "-m", str(seconds), "--max-filesize", str(cap),
            "-H", "User-Agent: agentbox", "-o", "-", url]
    part = dest + ".part"
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    size = 0
    try:
        with open(part, "wb") as out:
            while chunk := proc.stdout.read(CHUNK):
                size += len(chunk)
                if size > cap:
                    break
                out.write(chunk)
        if size > cap:
            proc.kill()
        proc.stdout.close()
        proc.wait(timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        proc.wait()
        size = cap + 1
    if size > cap or proc.returncode != 0:
        state_files.remove(part, fail=False)
        return False
    os.replace(part, dest)
    return True


def _in_use_name():
    """The name of the image in use, from RECORD (bound read-only into the fetch's unit), or None."""
    rec = load()
    return rec["name"] if rec else None


def _clear(keep=()):
    """Delete the fetch's downloads but `keep`."""
    for name in state_files.listdir(DOWNLOADS, fail=False):
        if name not in keep:
            path = os.path.join(DOWNLOADS, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                state_files.remove(path, fail=False)


def fetch(get, say):
    """As agentbox-feeds: the release's image as Fedora lists it, downloaded with its CHECKSUM
    into DOWNLOADS unless it is the image in use. get(url, accept) -> (status, JSON)
    (versions._get). The entry of available.json: {release, image, error}."""
    got = {"release": RELEASE, "image": None, "error": None}
    status, listing = get(RELEASES, "application/json")
    found = listed(listing) if status == 200 else []
    if status == 0:
        got["error"] = "fedoraproject.org not reachable"
    elif status != 200 or not isinstance(listing, list):
        got["error"] = f"fedoraproject.org answered {status}"
    elif not found:
        got["error"] = "no Cloud Base Generic qcow2 of the release in releases.json"
    elif len(found) > 1:
        got["error"] = "more than one Cloud Base Generic qcow2 of the release in releases.json"
    if got["error"]:
        say(line(got))
        return got
    name, sha, size = found[0]
    got["image"] = name
    if name == _in_use_name():
        _clear()
        say(line({**got, "state": "in use"}))
        return got
    checksum = checksum_name(name)
    os.makedirs(DOWNLOADS, exist_ok=True)
    _clear(keep=(name,))
    path = os.path.join(DOWNLOADS, name)
    # an image downloaded before (that check-versions refused, or whose CHECKSUM failed) is kept
    if _hash(path) != (sha, size):
        if not download(directory() + name, path, MAX_IMAGE, DOWNLOAD_SECONDS):
            got["error"] = "download of the image failed"
        elif _hash(path) != (sha, size):
            got["error"] = "the download does not match releases.json"
            state_files.remove(path, fail=False)
    if not got["error"] and not download(directory() + checksum, os.path.join(DOWNLOADS, checksum), MAX_TEXT, 120):
        got["error"] = "download of the CHECKSUM failed"
    say(line({**got, "state": "downloaded, for check-versions to check"}))
    return got


# --- check-versions (root) --------------------------------------------------------------------

def fetched(found, error):
    """What the fetch found (its file's `build_vm_image`), checked as untrusted input: an image
    name of RELEASE's pattern, else the fetch's error (`error`, versions._error). A file
    without the entry is NOT_FETCHED."""
    if not isinstance(found, dict):
        return {"image": None, "error": NOT_FETCHED if found is None else "unknown error"}
    name = found.get("image")
    if found.get("error") is None and isinstance(name, str) and image_name().fullmatch(name):
        return {"image": name, "error": None}
    return {"image": None, "error": error(found.get("error")) if found.get("error") is not None
            else "unknown error"}


def dearmor(armored):
    """The binary key of an ASCII-armored OpenPGP public key block (gpgv takes no armored
    keyring), its CRC-24 checked; None when it is none."""
    lines = armored.decode("ascii", "replace").splitlines()
    try:
        start = lines.index("-----BEGIN PGP PUBLIC KEY BLOCK-----")
        end = lines.index("-----END PGP PUBLIC KEY BLOCK-----", start)
    except ValueError:
        return None
    body = lines[start + 1:end]
    while body and body[0].strip():          # armor headers (Version: ...) up to the empty line
        body = body[1:]
    body = [l.strip() for l in body if l.strip()]
    crc = body.pop()[1:] if body and body[-1].startswith("=") else None
    try:
        data = base64.b64decode("".join(body), validate=True)
        if not data or crc is not None and base64.b64decode(crc, validate=True) != _crc24(data).to_bytes(3, "big"):
            return None
    except ValueError:
        return None
    return data


def _crc24(data):
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def _read_at(dir_fd, name, cap):
    """The content of the regular file `name` in the directory dir_fd, at most `cap` bytes,
    never through a link; None otherwise."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= cap else None


def signed_text(checksum):
    """(the signed text of the clearsigned CHECKSUM, None) by gpgv as agentbox-feeds with the
    Host's key of RELEASE as the only key, or (None, why)."""
    try:
        with open(key_file(), "rb") as f:
            key = dearmor(f.read(MAX_TEXT))
    except OSError:
        key = None
    if not key:
        return None, f"the Host has no key of Fedora {RELEASE} ({key_file()}, package fedora-gpg-keys)"
    work = tempfile.mkdtemp(prefix="agentbox-gpgv-")
    try:
        os.chmod(work, 0o755)            # agentbox-feeds reads, nobody but root writes
        for name, data in (("fedora.gpg", key), ("CHECKSUM", checksum)):
            with open(os.open(os.path.join(work, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "wb") as f:
                f.write(data)
        argv = ["setpriv", f"--reuid={FEEDS_USER}", f"--regid={FEEDS_USER}", "--clear-groups", "--no-new-privs",
                "--", "gpgv", "--homedir", work, "--status-fd", "2", "--keyring", os.path.join(work, "fedora.gpg"),
                "--output", "-", os.path.join(work, "CHECKSUM")]
        env = dict(os.environ, LC_ALL="C", HOME=work)        # --homedir: no GNUPGHOME of root's
        try:
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=GPGV_SECONDS)
        except FileNotFoundError:
            return None, "setpriv is missing"
        except (OSError, subprocess.TimeoutExpired):
            return None, "gpgv: time limit exceeded"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    status = r.stderr.decode("utf-8", "replace")
    # gpgv exits 0 with VALIDSIG also for a signature by an expired or revoked key
    refused = re.findall(rf"(?m)^\[GNUPG:\] ({'|'.join(REFUSED_STATUS)}) ", status)
    if r.returncode != 0 or refused or not re.search(r"(?m)^\[GNUPG:\] VALIDSIG ", status):
        said = [l for l in status.splitlines() if l.strip() and not l.startswith("[GNUPG:]")]
        return None, (f"the CHECKSUM's signature is not good (gpgv exit {r.returncode}"
                      + (f", {refused[0]}" if refused else "")
                      + (f": {clean(said[-1][:200], False)}" if said else "") + ")")
    if len(r.stdout) > MAX_TEXT:
        return None, "the CHECKSUM's signed text is too long"
    return r.stdout.decode("utf-8", "replace"), None


def signed_sha256(text, name):
    """The SHA-256 the signed text names for the file `name`, or None (none, or two)."""
    found = {m.group(2) for m in map(SIGNED_LINE.fullmatch, text.splitlines()) if m and m.group(1) == name}
    return found.pop() if len(found) == 1 else None


def _limits():
    for which, value in QEMU_IMG_LIMITS.items():
        resource.setrlimit(which, (value, value))


def qcow2_problem(path, max_virtual):
    """None when the file at `path` is a qcow2 that names no other file and is not encrypted,
    of a virtual size of at most max_virtual bytes, by qemu-img info with the format named
    (never guessed) under QEMU_IMG_LIMITS; else what is wrong. qemu-img opens neither a backing
    file nor a data file for info."""
    try:
        r = subprocess.run(["qemu-img", "info", "-f", "qcow2", "--output=json", path], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors="replace", timeout=QEMU_IMG_SECONDS,
                           preexec_fn=_limits)
    except FileNotFoundError:
        return "qemu-img is missing"
    except (OSError, subprocess.TimeoutExpired):
        return "qemu-img info: time limit exceeded"
    if r.returncode != 0:
        return "not a qcow2 by qemu-img info: " + clean(" ".join(r.stderr.split())[-200:], False)
    try:
        info = json.loads(r.stdout[:1 << 20])
    except (ValueError, RecursionError):
        info = None
    if not isinstance(info, dict) or info.get("format") != "qcow2":
        return "not a qcow2 by qemu-img info"
    specific = info.get("format-specific")
    data = specific.get("data") if isinstance(specific, dict) and specific.get("type") == "qcow2" else None
    if not isinstance(data, dict):
        return "not a qcow2 by qemu-img info"
    if any(k.startswith("backing") or k.startswith("full-backing") for k in info):
        return "the qcow2 names a backing file"
    if "data-file" in data or "data-file-raw" in data:
        return "the qcow2 names an external data file"
    if info.get("encrypted") or "encrypt" in data:
        return "the qcow2 is encrypted"
    if data.get("corrupt") is not False:
        return "the qcow2 is marked corrupt"
    size = info.get("virtual-size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= max_virtual:
        return f"the qcow2's virtual size is not between 1 byte and {max_virtual >> 30} GiB"
    return None


def _copy(dir_fd, name, part):
    """Copy the fetch's regular file `name` (never through a link, at most MAX_IMAGE bytes) to
    root's new file `part`, hashing what it copies: its SHA-256, or None."""
    try:
        src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=dir_fd)
    except OSError:
        return None
    h, size = hashlib.sha256(), 0
    try:
        st = os.fstat(src)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_IMAGE:
            return None
        with open(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as out:
            while chunk := os.read(src, CHUNK):
                size += len(chunk)
                if size > MAX_IMAGE:
                    return None
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except OSError:
        return None
    finally:
        os.close(src)
    return h.hexdigest()


def _take(name, say):
    """Check the fetch's download of the image `name` and keep it: None, or why it was refused."""
    try:
        dir_fd = os.open(DOWNLOADS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return "the fetch's download is missing"
    try:
        checksum = _read_at(dir_fd, checksum_name(name), MAX_TEXT)
        if checksum is None:
            return "the fetch's CHECKSUM is missing or no regular file of at most 64 KiB"
        text, why = signed_text(checksum)
        if why:
            return why
        wanted = signed_sha256(text, name)
        if wanted is None:
            return f"the signed CHECKSUM names no single SHA-256 of {name}"
        state_files.directory(paths.BUILD_VM, 0o700)
        state_files.directory(IMAGES, 0o755)
        part = os.path.join(IMAGES, f".{name}.part")
        state_files.remove(part, fail=False)
        try:
            sha = _copy(dir_fd, name, part)
            if sha is None:
                return f"the fetch's image is missing or no regular file of at most {MAX_IMAGE} bytes"
            if sha != wanted:
                return "the image does not match the signed CHECKSUM"
            problem = qcow2_problem(part, MAX_VIRTUAL)
            if problem:
                return problem
            os.chmod(part, 0o644)
            os.replace(part, os.path.join(IMAGES, name))
        finally:
            state_files.remove(part, fail=False)
    finally:
        os.close(dir_fd)
    state_files.write_json(RECORD, {"name": name, "release": RELEASE, "sha256": wanted, "checked": int(time.time())})
    say(f"Build VM image: checked and took {name}")
    return None


def remove_replaced(say):
    """Remove the images IMAGES holds besides the one in use, only when no Build runs, without
    waiting for one: a build VM may still boot a replaced image, and the next check removes it.
    A record that is there but cannot be read removes nothing: the image in use is not known."""
    from . import images            # the run lock; images uses versions, which uses this module
    rec = load()
    if rec is None and os.path.lexists(RECORD):
        say(f"{RECORD} cannot be read: no build VM image is removed")
        return
    stale = [n for n in state_files.listdir(IMAGES, fail=False) if not rec or n != rec["name"]]
    if not stale:
        return
    with state_files.locked_unless_held(images.RUN_LOCK) as mine:
        if not mine:
            say("A Build is running: the replaced build VM image stays until the next check")
            return
        for name in stale:
            state_files.remove(os.path.join(IMAGES, name), fail=False)
            say(f"Removed the replaced build VM image {name}")


def check(found, say):
    """Root's part (versions._check): take the image the fetch found (fetched()) when it is not
    the one in use, then remove a replaced one. Its entry in versions.json: {release, image (the
    name in use), offered (the fetch's), state, error}."""
    rec = load()
    if rec and not os.path.isfile(os.path.join(IMAGES, rec["name"])):
        # a record without its image: dropped, so the next fetch downloads the image again
        state_files.remove(RECORD, fail=False)
        rec = None
    in_use = rec["name"] if rec and rec["release"] == RELEASE else None
    got = {"release": RELEASE, "image": in_use, "offered": found["image"], "state": None, "error": found["error"]}
    if found["image"] and found["image"] != in_use:
        got["error"] = _take(found["image"], say)
        if got["error"]:
            say(f"Build VM image: {found['image']} refused: {got['error']}; "
                + (f"{rec['name']} stays in use" if rec else "there is none yet"))
        else:
            got.update(image=found["image"], state="checked")
    elif found["image"]:
        got["state"] = "in use"
    remove_replaced(say)
    say(line(got))
    return got
