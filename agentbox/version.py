"""agentbox version: the Release this Host runs (GLOSSARY.md, ADR 0009).

One semantic version (`MAJOR.MINOR.PATCH`, no `v`), stored in the file VERSION next to this
package (paths.VERSION): release-it writes it when a Release is cut, the RPM installs it beside the code,
and the Control Room backend reads the same file. A checkout that is not a Release says
0.0.0.
"""
import json
import re

from . import paths
from .errors import die

VERSION_FILE = paths.VERSION
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?")
# The repository the Releases are cut from, each tagged v<release> (docs/releasing.md; the URL
# of rpm/agentbox.spec). Only named in links: nothing is fetched from it.
REPOSITORY = "likt0r/askos"
GITHUB = "https://github.com/"
REPOSITORY_NAME = re.compile(r"[A-Za-z0-9-]{1,39}/(?!\.\.?$)[A-Za-z0-9._-]{1,100}")


def tag(release):
    """The Git tag of a Release (v<release>); None for a checkout's 0.0.0, which is no Release,
    and for anything that is not a version."""
    return f"v{release}" if isinstance(release, str) and SEMVER.fullmatch(release) and release != "0.0.0" else None


def repository_url(repository=REPOSITORY):
    """The repository's page on GitHub; None for a name that is not owner/repository."""
    return GITHUB + repository if isinstance(repository, str) and REPOSITORY_NAME.fullmatch(repository) else None


def file_url(repository, ref, path):
    """The page of a file of the repository at a tag or commit; None when either is unknown."""
    base = repository_url(repository)
    return f"{base}/blob/{ref}/{path}" if base and ref else None


def release():
    """The Release as text; a missing or malformed VERSION file is a clear error."""
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError as e:
        die(f"{VERSION_FILE}: {e.strerror}", error="action_failed")
    if not SEMVER.fullmatch(text):
        die(f"{VERSION_FILE}: not a version of the form MAJOR.MINOR.PATCH: {text!r}", error="action_failed")
    return text


def version(args):
    """agentbox version [--json]  (also: agentbox --version)"""
    unknown = [a for a in args if a != "--json"]
    if unknown:
        die(f"unknown option: {unknown[0]}", 2, error="invalid_argument", option=unknown[0])
    if "--json" in args:
        print(json.dumps({"release": release()}))
    else:
        print(f"agentbox {release()}")


COMMANDS = {"version": version}
