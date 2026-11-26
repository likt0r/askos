"""The two images a Build takes from quay.io, pinned by digest (#144).

  fedora-bootc          quay.io/fedora/fedora-bootc:44: what the Base image starts from
                        (vm/Containerfile: ARG FEDORA_BOOTC, FROM ${FEDORA_BOOTC}) and the
                        buildroot of the disk step (bootc-image-builder --build-container)
  bootc-image-builder   quay.io/centos-bootc/bootc-image-builder:latest: the disk step

A Build never pulls either by its tag: it uses the digests pinned in RECORD, which only the
version check moves (versions.py):

  the fetch (agentbox fetch-versions, as agentbox-feeds) asks quay.io's API for the tag's
  history (every digest the tag pointed to, since when and until when; pages of 100, at most
  HISTORY_PAGES of them) and takes
    bootc-image-builder  the digest the tag pointed to package_min_age_days ago, as npm
                         --before takes a version
    fedora-bootc         the digest the tag points to now: quay.io keeps a digest of
                         fedora/fedora-bootc only while a tag points to it, and the tag moves
                         every day, so no older digest can still be pulled
  check-versions (root) reads that answer as untrusted input: the repository is fixed here, the
  digest must have the form of one, and the builder's minimum age is root's own
  package_min_age_days (Settings). The time the tag moved to a digest (`since`) is the fetch's
  claim, which root cannot verify. Root pulls a new digest by its digest (podman pull
  <repository>@<digest>, which checks the content against it) and only then pins it; a
  fedora-bootc only when the image's os-release names Fedora of the release its tag names
  (UPSTREAM), since the repository holds every release (43, 45, rawhide...) and the fetch, not
  root, chose the digest: one of another release is removed again. A failed fetch or pull leaves
  the pin as it is, and so does a fetch file without the image's entry, which asked quay.io
  nothing (a fetch from before the pins): "not fetched yet", no error of quay.io. A pinned
  image missing from the Host's container storage is pulled again by the next check, which a
  Build runs first when it misses one it needs (images._pins).

A new digest is no update by itself: no Build starts for it, the next one (the weekly rebuild
on rebuild_day, or an update of Orca, the agents or the packages) takes it.

The digest a pin replaces is retired: the version check right away, and every Build run
before its Builds, removes its image (podman rmi, under the run lock, so no Build is using it;
the check does not wait for the lock and leaves them while a Build runs), unless it is pinned
again or the current Base Build is built on it, whose derived Images' disk steps still use it
as their buildroot. An image podman could not remove stays retired, and the next check or run
tries again. So the Host keeps at most two fedora-bootc images (the pin and the current Base
Build's), 1-2 GB each, and one bootc-image-builder.
"""
import json
import os
import re
import shutil
import subprocess
import time

from . import hostfacts, paths, programs, safeguards, state_files, times
from .sanitize import clean

REGISTRY = "quay.io"
# name: (repository on quay.io, tag, whether package_min_age_days applies)
UPSTREAM = {
    "fedora-bootc": ("fedora/fedora-bootc", "44", False),
    "bootc-image-builder": ("centos-bootc/bootc-image-builder", "latest", True),
}
NAMES = tuple(UPSTREAM)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
RECORD = os.path.join(paths.STATE, "digests.json")
API = "https://quay.io/api/v1/repository/"
HISTORY = 100                       # entries of a tag's history per page (quay.io's most)
HISTORY_PAGES = 5                   # pages read at most
PULL_SECONDS = paths.tunable("AGENTBOX_DIGEST_PULL_SECONDS", 3600)
# the errors the fetch reports for a digest (versions.FETCH_ERROR takes them)
ERRORS = (r"quay\.io not reachable|quay\.io answered [1-5][0-9][0-9]|no digest of the tag found|"
          r"no digest of the tag at least package_min_age_days old|"
          r"no digest of the tag (found|at least package_min_age_days old) "
          r"in the first [0-9]{1,4} entries of its history")
# the fetch file has no entry for the image: that fetch never asked quay.io for it (fetched())
NOT_FETCHED = "not fetched yet; the next fetch asks quay.io"


def tagged(name):
    """The image by its tag, for people: quay.io/fedora/fedora-bootc:44."""
    repository, tag, _ = UPSTREAM[name]
    return f"{REGISTRY}/{repository}:{tag}"


def reference(name, digest):
    """The image by its digest, as podman takes it: quay.io/fedora/fedora-bootc@sha256:..."""
    return f"{REGISTRY}/{UPSTREAM[name][0]}@{digest}"


def short(digest):
    """sha256: and the first 12 hex digits, for people; "?" when unknown."""
    return digest[:19] if isinstance(digest, str) and DIGEST.fullmatch(digest) else "?"


def valid(digest):
    return digest if isinstance(digest, str) and DIGEST.fullmatch(digest) else None


# --- the fetch (agentbox-feeds) ---------------------------------------------------------------

def choose(history, tag, cutoff):
    """(digest, since) the tag pointed to at `cutoff` (epoch seconds) by quay.io's tag history
    (its "tags": start_ts, end_ts when it left the digest, manifest_digest), or (None, None).
    By the entries' own times, in whatever order they come: the one that began last of those
    that cover `cutoff`."""
    found = None
    for entry in history if isinstance(history, list) else []:
        if not isinstance(entry, dict) or entry.get("name") != tag:
            continue
        digest, start, end = valid(entry.get("manifest_digest")), entry.get("start_ts"), entry.get("end_ts")
        if digest is None or not isinstance(start, int) or isinstance(start, bool):
            continue
        if end is not None and (not isinstance(end, int) or isinstance(end, bool)):
            continue
        if start <= cutoff and (end is None or end > cutoff) and (found is None or start > found[1]):
            found = (digest, start)
    return found or (None, None)


def newest(name, now, days, get):
    """(digest, since, error) of the image as the next Build would use it: by the tag's
    history on quay.io, asked with get(url, accept) -> (status, JSON) (versions._get), page by
    page until an entry covers the time asked for, quay.io has no more, or HISTORY_PAGES were
    read; a history longer than that names how much of it was read in the error."""
    repository, tag, aged = UPSTREAM[name]
    cutoff = now - days * 86400 if aged else now
    history, more = [], False
    for page in range(1, HISTORY_PAGES + 1):
        status, answer = get(f"{API}{repository}/tag/?specificTag={tag}&onlyActiveTags=false"
                             f"&limit={HISTORY}&page={page}", "application/json")
        if status == 0:
            return None, None, "quay.io not reachable"
        if status != 200 or not isinstance(answer, dict):
            return None, None, f"quay.io answered {status}"
        entries = answer.get("tags")
        history += entries if isinstance(entries, list) else []
        more = answer.get("has_additional") is True
        digest, since = choose(history, tag, cutoff)
        if digest is not None:
            return digest, since, None
        if not more:
            break
    wanted = "at least package_min_age_days old" if aged else "found"
    if more:
        return None, None, f"no digest of the tag {wanted} in the first {len(history)} entries of its history"
    return None, None, f"no digest of the tag {wanted}"


def line(name, got, days):
    """One line of the version check about an image."""
    aged = UPSTREAM[name][2]
    return (f"{tagged(name)}: {short(got['digest']) if got['digest'] else 'unknown'}"
            + (f" ({got['error']})" if got.get("error") else "")
            + (f", since {times.local(got['since'])}" if got.get("since") else "")
            + (f", the digest of the tag at least {days} days ago" if aged and days else
               ", the tag's current digest" if not aged else ""))


# --- check-versions (root) --------------------------------------------------------------------

def fetched(found, when, days, error):
    """What the fetch found for the images ({name: {digest, since, error}} of its file), checked
    as untrusted input: the digest's form, `since` no later than the fetch (`when`), and for an
    image with a minimum age, at least `days` before it: root's own package_min_age_days, never
    the file's. `since` itself is the fetch's claim, which root cannot verify; the repository
    is fixed (UPSTREAM), and the pull checks the content against the digest. `error`
    (versions._error) turns an error text into one the fetch may report. A file without an
    entry for the image (no `digests`, or not this one) did not ask quay.io for it: written by
    a fetch of a Release before the pins, or one that ran before this Release was installed;
    that is NOT_FETCHED, not a failed fetch."""
    out = {}
    for name in NAMES:
        # found: the file's `digests`, None without one
        asked = name in found if isinstance(found, dict) else found is not None
        got = found.get(name) if isinstance(found, dict) else None
        got = got if isinstance(got, dict) else {}
        digest, since = valid(got.get("digest")), got.get("since")
        problem = None
        if not asked:
            problem = NOT_FETCHED
        elif digest is None:
            problem = error(got.get("error"))
        elif not isinstance(since, int) or isinstance(since, bool) or not 0 < since <= when:
            problem = "unknown error"
        elif UPSTREAM[name][2] and (days is None or since > when - days * 86400):
            problem = "no digest of the tag at least package_min_age_days old"
        out[name] = {"digest": None, "since": None, "error": problem} if problem else \
            {"digest": digest, "since": since, "error": None}
    return out


def load():
    """The record of the pins and the retired digests; {} when there is none."""
    rec = state_files.json_object(RECORD, {})
    return rec if isinstance(rec, dict) else {}


def pinned(rec=None):
    """{name: {"digest", "since", "pinned"} or None}: the digests a new Build uses."""
    rec = load() if rec is None else rec
    out = {}
    for name in NAMES:
        pin = rec.get(name) if isinstance(rec.get(name), dict) else {}
        digest = valid(pin.get("digest"))
        out[name] = {"digest": digest, "since": pin.get("since") if isinstance(pin.get("since"), int) else None,
                     "pinned": pin.get("pinned") if isinstance(pin.get("pinned"), int) else None} if digest else None
    return out


def present(ref):
    try:
        return subprocess.run(["podman", "image", "exists", ref], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _is_list(ref):
    """Whether the Host's container storage holds a manifest list under ref (skopeo reads the
    raw manifest from the store, nothing is fetched); None when skopeo cannot tell."""
    try:
        r = subprocess.run(["skopeo", "inspect", "--raw", f"containers-storage:{ref}"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors="replace", timeout=60)
        raw = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raw = None
    return isinstance(raw.get("manifests"), list) if isinstance(raw, dict) else None


def instance(name, digest):
    """The reference to hand bootc-image-builder for the pinned image: by the digest of the
    Host's own platform image, not of the manifest list a tag's digest names. image-builder looks
    a list's entry up in the local store by the image's Digest field, which podman sets to the
    list's digest when it pulled by it, and finds nothing (osbuild/image-builder
    pkg/container/client.go getLocalImageID). podman records every digest it pulled the image by
    in RepoDigests; of those of the same repository, the ones that are no list (skopeo reads them
    from the store) name the platform image, all the same image, so any of them does, the pinned
    one first. (the reference, None), or (None, why)."""
    ref = reference(name, digest)
    try:
        r = subprocess.run(["podman", "image", "inspect", "--format", "{{json .RepoDigests}}", ref],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=60)
        found = json.loads(r.stdout) if r.returncode == 0 else None
        why = clean((r.stderr or "")[-200:], multiline=False).strip()
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        found, why = None, type(e).__name__
    if not isinstance(found, list):
        return None, f"{ref}: podman does not say the image's digests" + (f" ({why})" if why else "")
    repo = reference(name, "")
    digests = sorted({d for d in (valid(x[len(repo):]) for x in found if isinstance(x, str) and x.startswith(repo))
                      if d} | {digest})
    platform = [d for d in sorted(digests, key=lambda d: d != digest) if _is_list(reference(name, d)) is False]
    if not platform and not shutil.which("skopeo"):
        return None, (f"{ref}: skopeo is missing, which tells the platform image from the list "
                      f"({hostfacts.INSTALL} skopeo)")
    if not platform:
        return None, (f"{ref}: the Host's container storage holds no platform image of it that skopeo can "
                      f"read; pull it again (sudo podman rmi {ref}, then sudo agentbox check-versions)")
    return reference(name, platform[0]), None


def gone(ref):
    """Whether podman says the image is not there (podman image exists: 1); not when it
    cannot tell."""
    try:
        return subprocess.run(["podman", "image", "exists", ref], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=60).returncode == 1
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pull(ref):
    """podman pull by digest into the Host's container storage (root, as a Build pulls); the
    error, or None."""
    try:
        r = subprocess.run(["podman", "pull", "--quiet", ref], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, errors="replace", timeout=PULL_SECONDS)
    except FileNotFoundError:
        return "podman is missing"
    except (OSError, subprocess.TimeoutExpired):
        return "podman pull: time limit exceeded"
    if r.returncode != 0:
        tail = clean((r.stderr or "")[-200:], multiline=False).strip()
        return f"podman pull failed (exit {r.returncode})" + (f": {tail}" if tail else "")
    return None


def take(found, say):
    """Pin what the check found (fetched()): pull each new digest, and one pinned but missing
    from the Host, check a fedora-bootc's release (_release), then pin it and retire the
    digest it replaces. A failed pull or check is the image's error (in `found`) and leaves
    its pin; an image of another release is removed again unless pinned or retired. Pulls run
    without the record's lock, so a Build reading the pins never waits for one. Whether it
    pinned anything new."""
    rec = load()
    pins = pinned(rec)
    retired = rec.get("retired") if isinstance(rec.get("retired"), list) else []
    kept = {reference(n, p["digest"]) for n, p in pins.items() if p} | {r for r in retired if isinstance(r, str)}
    pulled = {}
    for name in NAMES:
        got, pin = found[name], pins[name]
        if not got["digest"]:
            say(f"{tagged(name)}: pin stays " + (short(pin["digest"]) if pin else "unset"))
            continue
        ref = reference(name, got["digest"])
        if pin and pin["digest"] == got["digest"] and present(ref):
            continue
        say(f"{tagged(name)}: pulling {short(got['digest'])}")
        error = _pull(ref)
        if not error and name == "fedora-bootc":
            error = _release(ref)
            if error and ref not in kept:
                programs.quietly(["podman", "rmi", "--ignore", ref], 300)
        if error:
            got["error"] = error
            say(f"{tagged(name)}: {error}; pin stays " + (short(pin["digest"]) if pin else "unset"))
            continue
        pulled[name] = got
    if not pulled:
        return False
    with state_files.locked(RECORD):
        rec = load()
        retired = [r for r in rec.get("retired", []) if isinstance(r, str)] \
            if isinstance(rec.get("retired"), list) else []
        for name, got in pulled.items():
            before = pinned(rec)[name]
            if before and before["digest"] != got["digest"]:
                retired.append(reference(name, before["digest"]))
            rec[name] = {"digest": got["digest"], "since": got["since"], "pinned": int(time.time())}
            say(f"{tagged(name)}: pinned {short(got['digest'])}")
        now_pinned = {reference(n, p["digest"]) for n, p in pinned(rec).items() if p}
        rec["retired"] = [r for r in dict.fromkeys(retired) if r not in now_pinned]
        os.makedirs(paths.STATE, exist_ok=True)
        state_files.write_json(RECORD, rec)
    return True


def _release(ref):
    """None when the image `ref` (pulled) is Fedora of the release fedora-bootc's tag names
    (UPSTREAM), by its os-release, read from the mounted image as the Safeguard check reads an
    Image (links only inside it, regular files of bounded size); else the error."""
    wanted = UPSTREAM["fedora-bootc"][1]
    try:
        r = subprocess.run(["podman", "image", "mount", ref], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return "podman image mount failed"
    root = r.stdout.strip()
    try:
        if r.returncode != 0 or not os.path.isabs(root) or not os.path.isdir(root):
            return "podman image mount failed"
        try:
            release = safeguards.os_release(root)
        except (OSError, ValueError):
            release = {}
    finally:
        programs.quietly(["podman", "image", "unmount", ref], 120)
    if release.get("ID") == "fedora" and release.get("VERSION_ID") == wanted:
        return None
    return f"the pulled image is not Fedora {wanted} by its os-release"


def _ours(ref):
    """Whether `ref` is one of the two images by a digest: nothing else is ever removed."""
    digest = ref.rpartition("@")[2]
    return valid(digest) is not None and any(ref == reference(n, digest) for n in NAMES)


def remove_retired(base_digest, say):
    """Holding the run lock (images.remove_retired): remove the images of retired digests,
    except one pinned again and the fedora-bootc the current Base Build is built on
    (`base_digest`), which stays retired until a later run; so does one podman did not remove
    (podman image exists does not say it is gone)."""
    with state_files.locked(RECORD):
        rec = load()
        retired = rec.get("retired")
        if not isinstance(retired, list) or not retired:
            return
        now_pinned = {reference(n, p["digest"]) for n, p in pinned(rec).items() if p}
        base = reference("fedora-bootc", base_digest) if valid(base_digest) else None
        stay = []
        for ref in dict.fromkeys(r for r in retired if isinstance(r, str)):
            if ref in now_pinned or not _ours(ref):
                continue
            if ref == base:
                stay.append(ref)
                continue
            programs.quietly(["podman", "rmi", "--ignore", ref], 300)
            if not gone(ref):
                # e.g. an image still built on it: stays retired, the next check or run tries again
                stay.append(ref)
                say(f"Could not remove the retired image {ref} yet; the next check or Build run tries again")
                continue
            say(f"Removed the retired image {ref}")
        rec["retired"] = stay
        state_files.write_json(RECORD, rec)
