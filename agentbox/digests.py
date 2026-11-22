"""The two images a Build takes from quay.io, pinned by digest (#144).

  fedora-bootc          quay.io/fedora/fedora-bootc:44: what the Base image starts from
                        (vm/Containerfile: ARG FEDORA_BOOTC, FROM ${FEDORA_BOOTC}) and the
                        buildroot of the disk step (bootc-image-builder --build-container)
  bootc-image-builder   quay.io/centos-bootc/bootc-image-builder:latest: the disk step

A Build never pulls either by its tag: it uses the digests pinned in RECORD, which only the
version check moves (versions.py):

  the fetch (agentbox fetch-versions, as agentbox-feeds) asks quay.io's API for the tag's
  history (every digest the tag pointed to, since when and until when) and takes
    bootc-image-builder  the digest the tag pointed to package_min_age_days ago, as npm
                         --before takes a version
    fedora-bootc         the digest the tag points to now: quay.io keeps a digest of
                         fedora/fedora-bootc only while a tag points to it, and the tag moves
                         every day, so no older digest can still be pulled
  check-versions (root) reads that answer as untrusted input, pulls a new digest by its digest
  (podman pull <repository>@<digest>, which checks the content against it) and only then pins
  it. A failed fetch or pull leaves the pin as it is. A pinned image missing from the Host's
  container storage is pulled again by the next check.

The digest a pin replaces is retired: the next Build run removes its image (podman rmi, under
the run lock, so no Build is using it), unless it is pinned again or the current Base Build is
built on it, whose derived Images' disk steps still use it as their buildroot.
"""
import os
import re
import subprocess
import time

from . import paths, programs, state_files, times
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
HISTORY = 100                       # entries of a tag's history asked for (one page)
PULL_SECONDS = paths.tunable("AGENTBOX_DIGEST_PULL_SECONDS", 3600)
# the errors the fetch reports for a digest (versions.FETCH_ERROR takes them)
ERRORS = (r"quay\.io not reachable|quay\.io answered [1-5][0-9][0-9]|no digest of the tag found|"
          r"no digest of the tag at least package_min_age_days old")


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
    (its "tags": start_ts, end_ts when it left the digest, manifest_digest), or (None, None)."""
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
    history on quay.io, asked with get(url, accept) -> (status, JSON) (versions._get)."""
    repository, tag, aged = UPSTREAM[name]
    status, answer = get(f"{API}{repository}/tag/?specificTag={tag}&onlyActiveTags=false&limit={HISTORY}",
                         "application/json")
    if status == 0:
        return None, None, "quay.io not reachable"
    if status != 200 or not isinstance(answer, dict):
        return None, None, f"quay.io answered {status}"
    digest, since = choose(answer.get("tags"), tag, now - days * 86400 if aged else now)
    if digest is None:
        return None, None, ("no digest of the tag at least package_min_age_days old" if aged
                            else "no digest of the tag found")
    return digest, since, None


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
    image with a minimum age, at least package_min_age_days (`days`) before it; `error`
    (versions._error) turns an error text into one the fetch may report."""
    out = {}
    for name in NAMES:
        got = found.get(name) if isinstance(found, dict) else None
        got = got if isinstance(got, dict) else {}
        digest, since = valid(got.get("digest")), got.get("since")
        problem = None
        if digest is None:
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
    from the Host, then pin it and retire the digest it replaces. A failed pull is the
    image's error (in `found`) and leaves its pin. Pulls run without the record's lock, so a
    Build reading the pins never waits for one."""
    pins = pinned()
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
        if error:
            got["error"] = error
            say(f"{tagged(name)}: {error}; pin stays " + (short(pin["digest"]) if pin else "unset"))
            continue
        pulled[name] = got
    if not pulled:
        return
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


def _ours(ref):
    """Whether `ref` is one of the two images by a digest: nothing else is ever removed."""
    digest = ref.rpartition("@")[2]
    return valid(digest) is not None and any(ref == reference(n, digest) for n in NAMES)


def remove_retired(base_digest, say):
    """Before the Builds of a run (holding its lock): remove the images of retired digests,
    except one pinned again and the fedora-bootc the current Base Build is built on
    (`base_digest`), which stays retired until a later run."""
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
            say(f"Removed the retired image {ref}")
        rec["retired"] = stay
        state_files.write_json(RECORD, rec)
