"""Versions of the tools in the Images (#104): what a Build contains, and what is available.

What a Build contains is recorded at every Build (images.py), after the Safeguard check and
from the same mounted filesystem, by reading files; nothing from the Image runs (ADR 0008):

  orca                          /usr/lib/orca/resources/app.asar, the version in Orca's
                                package.json (the Build installs the RPM of Orca's GitHub
                                release there, vm/build/orca-release)
  claude-code, codex, opencode  /usr/lib/node_modules/<package>/package.json
  fedora                        VERSION_ID of /usr/lib/os-release

Builds made before have only the versions of the npm tools (their `agents`); the others
are unknown.

What is available is determined once a day on the Host (agentbox-versions.timer, written by
`agentbox regenerate`), in two parts, so that nothing from the internet is parsed as root:

  `agentbox fetch-versions` (agentbox-versions-fetch.service) runs as the service user
  agentbox-feeds, with no access to /etc/agentbox, the VMs or the Builds, and asks the npm
  registry and GitHub; it writes AVAILABLE (its StateDirectory).
  `agentbox check-versions` (agentbox-versions.service, root, after the fetch) reads that
  file as untrusted input (FETCHED), adds what only root can see (the package count below)
  and writes /var/lib/agentbox/versions.json, which everything else reads:

  npm tools  the version a Build would install now: from the registry's metadata
             (registry.npmjs.org), the newest version published at least
             package_min_age_days ago, the `latest` tag when it is old enough, as npm
             picks it with --before (vm/Containerfile)
  orca       the release a Build would install now (#113): from GitHub's list of Orca's
             releases (api.github.com, ORCA_REPO), the highest version that is no draft and
             no prerelease and was published at least package_min_age_days ago, or exactly
             orca_version of the Settings when it is set; by the rule of the Build's
             vm/build/orca-release, and only one whose x86_64 RPM GitHub lists with a SHA-256.
             Nothing is downloaded here.
  packages   per Image whose current Build's container image is kept (the Base image),
             how many Fedora package updates are available for that Build: `dnf
             check-update` in a container from it (podman as root, like the Build), named
             agentbox-check-<image>, without capabilities and with a read-only root, for
             at most CHECK_UPDATE_SECONDS. It never waits for a Build, and a Build never
             waits for it: a Build that removes the container image meanwhile (podman rmi
             --force, images._rmi) ends the count, which is then unknown.

With auto_update on (#114), `agentbox check-versions` then starts an update of every Image
a new Build would update (images.start_automatic: agentbox-build@update, never while a Build
runs). The timer runs it CHECK_BEFORE_RESET_HOURS before reset_time (generate.py), so the
Build is done when the daily Reset check moves idle VMs onto it.

Whatever cannot be determined is null, "unknown"; a failed check never blocks a Build or
a Reset. The fetch needs HTTPS to registry.npmjs.org and api.github.com, the count's
container the Fedora mirrors. versions.json from before #113 named the Host's Orca and the
newest release instead (orca.host, orca.release): Orca is unknown in it.
"""
import datetime
import json
import os
import re
import stat
import struct
import subprocess
import sys
import time

from . import paths, safeguards, settings, state_files
from .errors import Fail, die
from .sanitize import clean

TOOLS = ("orca", "claude-code", "codex", "opencode", "fedora")
NAMES = {"orca": "Orca", "claude-code": "Claude Code", "codex": "codex", "opencode": "opencode",
         "fedora": "Fedora"}
NPM = {"claude-code": "@anthropic-ai/claude-code", "codex": "@openai/codex", "opencode": "opencode-ai"}
VERSION = safeguards.VERSION
SEMVER = re.compile(r"v?(\d{1,9})\.(\d{1,9})\.(\d{1,9})(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")
IMAGE_ORCA = "/usr/lib/orca"                     # where vm/Containerfile installs it
ORCA_REPO = "stablyai/orca"                      # Orca's releases on GitHub (vm/build/orca-release)
ORCA_PAGES, ORCA_PER_PAGE = 3, 100               # as vm/build/orca-release
ORCA_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ORCA_MAX_RPM = 1 << 30
ASAR = "resources/app.asar"
RECORD = os.path.join(paths.STATE, "versions.json")
AVAILABLE_DIR = os.path.join(paths.STATE, "available")      # agentbox-feeds' (StateDirectory)
AVAILABLE = os.path.join(AVAILABLE_DIR, "available.json")
FETCH_UNIT = "agentbox-versions-fetch.service"          # the fetch as agentbox-feeds (generate.py)
MAX_AVAILABLE = 64 << 10            # the fetch's file is a few hundred bytes
STALE = 3 * 86400                   # a fetch older than this counts as none
MIN_AGE_DAYS = os.environ.get("AGENTBOX_VERSIONS_MIN_AGE_DAYS")    # set by the fetch's unit
ORCA_PIN = os.environ.get("AGENTBOX_VERSIONS_ORCA_VERSION")         # likewise; empty: none
# the errors the fetch reports (FETCHED takes no other text from its file)
FETCH_ERROR = re.compile(r"npm registry not reachable|npm registry answered [1-5][0-9][0-9]|"
                         r"no matching version found|GitHub not reachable|GitHub answered "
                         r"[1-5][0-9][0-9]|no release of the version in orca_version|"
                         r"no x86_64 RPM with a SHA-256 in the release|"
                         r"RPM of the version in orca_version replaced only recently")
REGISTRY = "https://registry.npmjs.org/"
GITHUB_API = "https://api.github.com/repos/"
MAX_ASAR_HEADER = 64 << 20          # Orca 1.4: 1.1 MiB
MAX_PACKAGE_JSON = 1 << 20
# the registry's full metadata of a package with all its versions (the abbreviated form
# has no publish times); today a few MiB
MAX_METADATA = 64 << 20
CHECK_UPDATE_SECONDS = paths.tunable("AGENTBOX_CHECK_UPDATE_SECONDS", 5 * 60)
# what dnf may write in the read-only container
CHECK_TMPFS = ("/var/cache", "/var/lib/dnf", "/var/log")


# --- what a Build contains ------------------------------------------------------------------

def _asar_package(fd):
    """Orca's package.json from an open app.asar (Electron's archive: a header with the
    file table as JSON, then the files), or None."""
    head = os.pread(fd, 16, 0)
    if len(head) != 16:
        return None
    one, size, _, length = struct.unpack("<4I", head)
    if one != 4 or not 8 < size <= MAX_ASAR_HEADER or length > size - 8:
        return None
    try:
        table = json.loads(os.pread(fd, length, 16))
        entry = table["files"]["package.json"]
        offset, count = int(entry["offset"]), entry["size"]
    except (ValueError, LookupError, TypeError, OverflowError, RecursionError):
        return None
    if entry.get("unpacked") or not isinstance(count, int) or not 0 < count <= MAX_PACKAGE_JSON \
            or not 0 <= offset < 1 << 40:
        return None
    try:
        package = json.loads(os.pread(fd, count, 8 + size + offset))
    except (ValueError, RecursionError):
        return None
    return package if isinstance(package, dict) else None


def _orca_package(path):
    """package.json of the Orca whose app.asar is at the Host path `path` (a regular file,
    not followed if it is a link), or None."""
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return _asar_package(fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def _version(v):
    return v if isinstance(v, str) and VERSION.fullmatch(v) else None


def _os_release(root):
    values = {}
    for line in (safeguards.text(root, "/usr/lib/os-release") or safeguards.text(root, "/etc/os-release")
                 or "").splitlines():
        key, eq, value = line.partition("=")
        if eq:
            values[key.strip()] = value.strip().strip("\"'")
    return values


def in_image(root):
    """{tool: version or None} of the Build whose files are at `root` (mounted)."""
    found = {"orca": None, "fedora": None}
    try:
        package = _orca_package(safeguards.resolve(root, f"{IMAGE_ORCA}/{ASAR}"))
        found["orca"] = _version(package.get("version")) if package else None
        release = _os_release(root)
        if release.get("ID") == "fedora":
            found["fedora"] = _version(release.get("VERSION_ID"))
    except (OSError, ValueError):
        pass
    agents = safeguards.agents(root)
    return {tool: found[tool] if tool in found else agents.get(tool) for tool in TOOLS}


def of_build(rec):
    """{tool: version or None} of a Build's record: what it recorded, and for a Build from
    before, the npm tools from its `agents`."""
    tools = rec.get("tools")
    if not isinstance(tools, dict):
        agents = rec.get("agents") if isinstance(rec.get("agents"), dict) else {}
        tools = {t: agents.get(t) for t in NPM}
    return {t: _version(tools.get(t)) for t in TOOLS}


# --- comparing versions ---------------------------------------------------------------------

def _key(v):
    """Sort key of a semantic version (a release after its prereleases), None for others."""
    m = SEMVER.fullmatch(v or "")
    if not m:
        return None
    pre = m.group(4)
    rank = (1,) if pre is None else (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), rank


def newer(available, installed):
    """Whether `available` is a newer version than `installed`; None when one is unknown or
    not a semantic version."""
    a, b = _key(available), _key(installed)
    return None if a is None or b is None else a > b


# --- what is available ------------------------------------------------------------------------

def _get(url, accept):
    """(HTTP status, JSON or None) of GET url over HTTPS; status 0 when it was not reached."""
    argv = ["curl", "-q", "-s", "-m", "120", "--proto", "=https", "--proto-redir", "=https",
            "--max-filesize", str(MAX_METADATA),
            "-H", f"Accept: {accept}", "-H", "User-Agent: agentbox", "-w", "\\n%{http_code}", url]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        die("curl is missing", 127, error="action_failed")
    except OSError:
        return 0, None
    # --max-filesize does not bind a body of unknown length (chunked): cap what is read
    try:
        out = proc.stdout.read(MAX_METADATA + 64)
        too_big = len(out) > MAX_METADATA + 32
        if too_big:
            proc.kill()
        proc.stdout.close()
        proc.wait(timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        proc.wait()
        return 0, None
    if too_big:
        return 0, None
    body, _, status = out.rpartition(b"\n")
    if proc.returncode != 0 or not re.fullmatch(rb"[1-5][0-9][0-9]", status):
        return 0, None
    try:
        return int(status), json.loads(body)
    except (ValueError, RecursionError):
        return int(status), None


def _epoch(stamp):
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pick(metadata, cutoff):
    """The version npm installs from a package's metadata when it may take only versions
    published up to `cutoff` (epoch seconds; npm's --before, npm-pick-manifest): the
    `latest` tag when it is that old and not deprecated, else the newest such release,
    deprecated ones last. Engines are not considered. None when there is none."""
    versions, times = metadata.get("versions"), metadata.get("time")
    tags = metadata.get("dist-tags")
    if not isinstance(versions, dict):
        return None
    times = times if isinstance(times, dict) else {}

    def old_enough(v):
        published = _epoch(times.get(v))
        return published is None or published <= cutoff         # npm takes one without a time

    def deprecated(v):
        manifest = versions.get(v)
        return isinstance(manifest, dict) and bool(manifest.get("deprecated"))

    latest = tags.get("latest") if isinstance(tags, dict) else None
    if isinstance(latest, str) and latest in versions and old_enough(latest) and not deprecated(latest):
        return _version(latest)
    releases = [v for v in versions if _key(v) and _key(v)[3] == (1,) and old_enough(v)]
    if not releases:
        return None
    return _version(max(releases, key=lambda v: (not deprecated(v), _key(v))))


def npm_newest(package, cutoff):
    """(version, error) of an npm package as a Build would install it now."""
    status, metadata = _get(REGISTRY + package.replace("/", "%2f"), "application/json")
    if status == 0:
        return None, "npm registry not reachable"
    if status != 200 or not isinstance(metadata, dict):
        return None, f"npm registry answered {status}"
    version = pick(metadata, cutoff)
    return (version, None) if version else (None, "no matching version found")


def _orca_version(v):
    """A version as Orca's releases are named and orca_version takes it (X.Y.Z), else None."""
    return v if isinstance(v, str) and settings.ORCA_VERSION.fullmatch(v) else None


def orca_version_of(release):
    """The version of a published release in GitHub's list (tag vX.Y.Z, no draft, no
    prerelease), else None."""
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        return None
    tag = release.get("tag_name")
    return _orca_version(tag[1:]) if isinstance(tag, str) and tag.startswith("v") else None


def _orca_published(release):
    """When the release was published (epoch seconds), None when unknown."""
    return _orca_stamp(release.get("published_at"))


def _orca_stamp(stamp):
    """A timestamp from GitHub (ISO 8601 with a time zone) as epoch seconds, else None."""
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.timestamp() if when.tzinfo is not None else None


def orca_asset_ok(release, version, cutoff, pinned=False):
    """Whether the release's RPM was not created or replaced recently (vm/build/orca-release,
    asset_ok): created_at and updated_at of the asset up to `cutoff`; when pinned (orca_version
    takes a young release too) at most an hour after the release was published if that is
    later. A release without the asset passes (orca_rpm says); a missing or unreadable
    timestamp counts as too young."""
    name = f"orca-ide-{version}.x86_64.rpm"
    assets = release.get("assets")
    found = [a for a in assets if isinstance(a, dict) and a.get("name") == name] if isinstance(assets, list) else []
    if len(found) != 1:
        return True
    if pinned:
        when = _orca_published(release)
        cutoff = max(cutoff, when + 3600) if when is not None else float("-inf")
    for key in ("created_at", "updated_at"):
        when = _orca_stamp(found[0].get(key))
        if when is None or when > cutoff:
            return False
    return True


def orca_choose(releases, cutoff, pin=None):
    """The release a Build takes from GitHub's list (vm/build/orca-release, choose): with pin
    the published release of that version, else the highest version among the published
    releases that came out up to `cutoff` (epoch seconds). None when there is none."""
    found = []
    for release in releases if isinstance(releases, list) else []:
        version = orca_version_of(release)
        if version is None:
            continue
        if pin is not None:
            if version == pin:
                return release
            continue
        when = _orca_published(release)
        if when is not None and when <= cutoff and orca_asset_ok(release, version, cutoff):
            found.append((tuple(int(p) for p in version.split(".")), len(found), release))
    return max(found)[2] if found else None


def orca_rpm(release, version):
    """Whether the release has the x86_64 RPM a Build downloads, with a SHA-256 to check it
    by (vm/build/orca-release, rpm_asset)."""
    name = f"orca-ide-{version}.x86_64.rpm"
    assets = release.get("assets")
    found = [a for a in assets if isinstance(a, dict) and a.get("name") == name] if isinstance(assets, list) else []
    if len(found) != 1:
        return False
    asset = found[0]
    size, digest = asset.get("size"), asset.get("digest")
    return asset.get("state") == "uploaded" and isinstance(digest, str) and bool(ORCA_DIGEST.fullmatch(digest)) \
        and isinstance(size, int) and not isinstance(size, bool) and 0 < size <= ORCA_MAX_RPM


def orca_newest(cutoff, pin=None):
    """(version, error) of Orca as a Build would install it now: the release of orca_version
    (pin), else the newest published at least as old as cutoff (orca_choose)."""
    api = f"{GITHUB_API}{ORCA_REPO}/releases"
    if pin is not None:
        status, release = _get(f"{api}/tags/v{pin}", "application/vnd.github+json")
        if status == 0:
            return None, "GitHub not reachable"
        if status not in (200, 404) or (status == 200 and not isinstance(release, dict)):
            return None, f"GitHub answered {status}"
        release = orca_choose([release], cutoff, pin) if status == 200 else None
        if release is None:
            return None, "no release of the version in orca_version"
        if not orca_asset_ok(release, pin, cutoff, pinned=True):
            return None, "RPM of the version in orca_version replaced only recently"
    else:
        everything = []
        for page in range(1, ORCA_PAGES + 1):
            status, releases = _get(f"{api}?per_page={ORCA_PER_PAGE}&page={page}", "application/vnd.github+json")
            if status == 0:
                return None, "GitHub not reachable"
            if status != 200 or not isinstance(releases, list):
                return None, f"GitHub answered {status}"
            everything += releases
            if len(releases) < ORCA_PER_PAGE:
                break
        release = orca_choose(everything, cutoff)      # only after all pages: the highest version
        if release is None:
            return None, "no matching version found"
    version = orca_version_of(release)
    return (version, None) if orca_rpm(release, version) else (None, "no x86_64 RPM with a SHA-256 in the release")


def _remove_container(name):
    try:
        subprocess.run(["podman", "rm", "--force", "--ignore", name], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        pass


def count_updates(image, tag):
    """(number of Fedora package updates, error) for the container image `tag` of the
    Image: `dnf check-update` in a container from it, which exits 100 and lists one
    package per line (name.arch, version, repository) when there are updates."""
    name = f"agentbox-check-{image}"
    _remove_container(name)               # left over from a check that was killed
    argv = ["podman", "run", "--rm", f"--name={name}", "--pull=never", "--cap-drop=all",
            "--security-opt=no-new-privileges", "--read-only", *(f"--tmpfs={d}" for d in CHECK_TMPFS),
            tag, "dnf", "-q", "check-update"]
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                           timeout=CHECK_UPDATE_SECONDS)
    except FileNotFoundError:
        return None, "podman is missing"
    except (OSError, subprocess.TimeoutExpired):
        _remove_container(name)
        return None, "dnf check-update: time limit exceeded"
    if r.returncode == 0:
        return 0, None
    if r.returncode != 100:
        tail = clean((r.stderr or "")[-300:], multiline=False).strip()
        return None, f"dnf check-update failed (exit {r.returncode})" + (f": {tail}" if tail else "")
    names = set()
    for line in r.stdout.splitlines():
        if line.lower().startswith("obsolet"):
            break
        f = line.split()
        if len(f) == 3 and "." in f[0]:
            names.add(f[0])
    return len(names), None


# --- the record -------------------------------------------------------------------------------

def load():
    """What the last check found (versions.json), or None when there was none or it is unreadable."""
    try:
        with open(RECORD, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _part(rec, *keys):
    for k in keys:
        rec = rec.get(k) if isinstance(rec, dict) else None
    return rec


def available(rec=None, pin=None):
    """What the Control Room shows as available, from the record: the versions a new Build
    would bring ({tool: version or None}), when it was checked, with which package minimum
    age, and the Orca version pinned in the Settings (orca_version, `pin`; None: automatic).
    Orca is unknown when the check took it with another pin than the Settings have now (the
    next check, which a changed pin starts at once, takes the new one) and in a record from
    before #113."""
    rec = load() if rec is None else rec
    tools = {t: None for t in TOOLS}
    for t in NPM:
        tools[t] = _version(_part(rec, "npm", t, "version"))
    if _orca_version(_part(rec, "orca", "pin")) == pin:
        tools["orca"] = _orca_version(_part(rec, "orca", "version"))
    checked = _part(rec, "checked")
    days = _part(rec, "package_min_age_days")
    return {"checked": _iso(checked), "package_min_age_days": days if isinstance(days, int) else None,
            "tools": tools, "orca_version": pin}


def updates(tools, offered):
    """The tools of a Build ({tool: version}) a new Build would change, by what is offered
    (available()["tools"]): an npm tool with a newer version, Orca with another one (a
    pinned orca_version may be older)."""
    out = [t for t in NPM if newer(offered.get(t), tools.get(t))]
    if offered.get("orca") and tools.get("orca") and offered["orca"] != tools["orca"]:
        out.insert(0, "orca")
    return out


def package_updates(image, build, rec=None):
    """The number of Fedora package updates the last check counted for this Build of the
    Image, None when it did not count them (for this Build)."""
    found = _part(load() if rec is None else rec, "packages", image)
    if not isinstance(found, dict) or found.get("build") != build or not build:
        return None
    n = found.get("updates")
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None


def _iso(epoch):
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text(tools):
    """One line for people: "Orca 1.4.241, Claude Code 2.1.5, ..."; "?" for unknown."""
    return ", ".join(f"{NAMES[t]} {tools.get(t) or '?'}" for t in TOOLS)


# --- agentbox fetch-versions (agentbox-feeds) -------------------------------------------------

def _min_age_days():
    """package_min_age_days: from the unit (regenerate writes the Settings' value into it,
    so the fetch needs nothing of /etc/agentbox), else from the Settings."""
    if MIN_AGE_DAYS and MIN_AGE_DAYS.isdigit() and 1 <= int(MIN_AGE_DAYS) <= 30:
        return int(MIN_AGE_DAYS)
    return settings.load()["package_min_age_days"]


def _orca_pin():
    """orca_version (None: automatic): from the unit like the minimum age, else from the
    Settings; a unit written before orca_version, whose fetch cannot read them, has none."""
    if ORCA_PIN is not None and (ORCA_PIN == "" or _orca_version(ORCA_PIN)):
        return ORCA_PIN or None
    try:
        return settings.load()["orca_version"] or None
    except Fail:
        return None


def _orca_line(got, days):
    return (f"Orca: {got['version'] or 'unknown'}" + (f" ({got['error']})" if got["error"] else "")
            + (f", pinned in orca_version ({got['pin']})" if got["pin"] else
               f", the newest release at least {days} days old" if days else ""))


def fetch(say):
    """Ask the npm registry and GitHub (as agentbox-feeds) and write AVAILABLE; returns it."""
    days = _min_age_days()
    pin = _orca_pin()
    now = time.time()
    rec = {"fetched": int(now), "package_min_age_days": days, "npm": {}, "orca": {}}
    for tool, package in NPM.items():
        version, error = npm_newest(package, now - days * 86400)
        rec["npm"][tool] = {"version": version, "error": error}
        say(f"{NAMES[tool]}: {version or 'unknown'}" + (f" ({error})" if error else "")
            + f", the newest version at least {days} days old")
    version, error = orca_newest(now - days * 86400, pin)
    rec["orca"] = {"version": version, "error": error, "pin": pin}
    say(_orca_line(rec["orca"], days))
    os.makedirs(AVAILABLE_DIR, exist_ok=True)
    state_files.write_atomic(AVAILABLE, (json.dumps(rec, indent=2) + "\n").encode(), 0o644)
    return rec


def fetch_versions(args):
    """agentbox fetch-versions [--json] (agentbox-versions-fetch.service, as agentbox-feeds):
    what the npm registry and GitHub offer, for agentbox check-versions."""
    if any(a != "--json" for a in args):
        die("fetch-versions [--json]", error="invalid_argument")
    as_json = "--json" in args
    rec = fetch((lambda line: None) if as_json else (lambda line: print(clean(line, False), flush=True)))
    if as_json:
        print(json.dumps(rec))


# --- agentbox check-versions (root) -------------------------------------------------------------

def _read_untrusted(path):
    """The content of the regular file `path`, at most MAX_AVAILABLE bytes, never through a
    link; None otherwise. Its directory belongs to agentbox-feeds."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, MAX_AVAILABLE + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= MAX_AVAILABLE else None


def _error(v):
    return v if isinstance(v, str) and FETCH_ERROR.fullmatch(v) else "unknown error"


def fetched(now):
    """What agentbox-feeds' fetch found (AVAILABLE), checked as untrusted input: the shape,
    every version against VERSION (Orca's and the pin it was taken with against
    settings.ORCA_VERSION), errors against FETCH_ERROR, the time; anything else is unknown.
    A missing, unreadable or stale file makes everything unknown."""
    npm = {t: {"version": None, "error": "not fetched (agentbox-versions-fetch.service)"} for t in NPM}
    orca = {"version": None, "error": "not fetched (agentbox-versions-fetch.service)", "pin": None}
    out = {"fetched": None, "package_min_age_days": None, "npm": npm, "orca": orca}
    data = _read_untrusted(AVAILABLE)
    try:
        rec = json.loads(data) if data is not None else None
    except (ValueError, RecursionError):
        rec = None
    when = rec.get("fetched") if isinstance(rec, dict) else None
    if not isinstance(when, int) or isinstance(when, bool) or not now - STALE <= when <= now + 300:
        if os.path.lexists(AVAILABLE):
            for part in (*npm.values(), orca):
                part["error"] = "fetch stale or unreadable"
        return out
    days = rec.get("package_min_age_days")
    out["fetched"] = when
    out["package_min_age_days"] = days if isinstance(days, int) and not isinstance(days, bool) \
        and 1 <= days <= 30 else None
    for tool in NPM:
        found = _part(rec, "npm", tool)
        found = found if isinstance(found, dict) else {}
        version = _version(found.get("version"))
        npm[tool] = {"version": version, "error": None if version else _error(found.get("error"))}
    found = rec.get("orca") if isinstance(rec.get("orca"), dict) else {}
    version = _orca_version(found.get("version"))
    orca.update(version=version, error=None if version else _error(found.get("error")),
                pin=_orca_version(found.get("pin")))
    return out


def check(say):
    """Determine what is available and save it (versions.json); returns the record. One
    check at a time (the record's lock, which no Build takes)."""
    with state_files.locked(RECORD):
        return _check(say)


def _check(say):
    from . import images            # images records the versions of a Build with this module
    now = time.time()
    found = fetched(now)
    days = found["package_min_age_days"]
    rec = {"checked": int(now), "fetched": found["fetched"], "package_min_age_days": days, "npm": found["npm"],
           "orca": found["orca"], "packages": {}}
    for tool, got in rec["npm"].items():
        say(f"{NAMES[tool]}: {got['version'] or 'unknown'}" + (f" ({got['error']})" if got["error"] else "")
            + (f", the newest version at least {days} days old" if days else ""))
    say(_orca_line(rec["orca"], days))
    for image in images.available():
        build = images.current(image)
        tag = images.CONTAINER.format(image, build)
        # only the Base image keeps the container image of its current Build
        if not build or not images._image_exists(tag):
            continue
        n, error = count_updates(image, tag)
        rec["packages"][image] = {"build": build, "updates": n, "error": error}
        say(f"Package updates for {image}-{build}: " + (str(n) if n is not None else "unknown")
            + (f" ({error})" if error else ""))
    os.makedirs(paths.STATE, exist_ok=True)
    state_files.write_atomic(RECORD, (json.dumps(rec, indent=2, ensure_ascii=False) + "\n").encode(), 0o644)
    return rec


def check_versions(args):
    """agentbox check-versions [--json] (daily timer, root, after fetch-versions): what a new
    Build would bring and the package updates of each Image's current Build; with auto_update
    on, then an update of the Images a new Build would update (images.start_automatic)."""
    if any(a != "--json" for a in args):
        die("check-versions [--json]", error="invalid_argument")
    as_json = "--json" in args
    say = (lambda line: None) if as_json else (lambda line: print(clean(line, False), flush=True))
    rec = check(say)
    if as_json:
        print(json.dumps(rec, ensure_ascii=False))
    failed = [t for t in NPM if rec["npm"][t]["error"]] + (["orca"] if rec["orca"]["error"] else []) + \
        [i for i, p in rec["packages"].items() if p["error"]]
    if failed:
        print(f"agentbox: not everything determined ({', '.join(failed)}), stays unknown", file=sys.stderr)
    if settings.load()["auto_update"]:
        from . import images        # images records the versions of a Build with this module
        images.start_automatic(say)


COMMANDS = {"check-versions": check_versions, "fetch-versions": fetch_versions}
NO_ROOT = {"fetch-versions"}        # runs as agentbox-feeds
