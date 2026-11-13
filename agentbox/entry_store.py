"""The storage the Allowlist (allowlist.py) and the Watchlist and the Legitimate domains
(lists.py) share: records of entries that each name a domain or, as a Regex entry (ADR 0013,
patterns.py), a pattern, with fields of their own besides.

An entry asked for is a ref, ("domain", domain) or ("regex", pattern). A record is the file
{"entries": [...]}, written sorted and readable by the proxy's user; the text lists from
before the records are read line by line, a domain per line and a comment after #. Each
list keeps its own commands, files and rules: what tells its entries apart, its own
checks, how a broken record is reported, its order.
"""
import contextlib
import json
import os

from . import patterns, state_files
from .errors import die
from .network import DOMAIN
from .sanitize import clean


# --- arguments ---------------------------------------------------------------------

def target(positional, opts, domain):
    """The entry asked for: ("domain", d) from the first positional argument, checked by
    domain (a function that ends the command at an invalid one), or ("regex", pattern) from
    --regex in its place, taken as given (a new one is checked by the caller with
    patterns.checked, so an entry the rules no longer accept can still be removed)."""
    if "regex" in opts:
        if positional:
            die("--regex takes the place of the domain; too many arguments: " + clean(positional[0], False)[:60],
                error="invalid_argument")
        return "regex", opts["regex"]
    d = domain(positional[0] if positional else "")
    if len(positional) > 1:
        die("too many arguments: " + clean(positional[1], False)[:60], error="invalid_argument")
    return "domain", d


def text(value, name, limit):
    """--note or --source: one line of at most limit printable characters, stripped (None when
    not given); else the command ends with invalid_<name>."""
    if value is None:
        return None
    t = value.strip()
    if len(t) > limit or not t.isprintable():
        die(f"--{name}: at most {limit} printable characters on one line", error=f"invalid_{name}",
            option=f"--{name}")
    return t


def label(ref):
    """An entry for people: the domain, or "pattern <regex>"."""
    return f"pattern {ref[1]}" if ref[0] == "regex" else ref[1]


def param(ref):
    """The entry asked for as an entry's field or a failure's parameter: {"domain": d} or
    {"regex": p}."""
    return {ref[0]: ref[1]}


def given(ref):
    """The entry asked for as a result's fields: domain and regex, the other null."""
    return patterns.both(param(ref))


# --- records -------------------------------------------------------------------------

def problem(e, fields, shown, extra=None):
    """What is wrong with a stored entry, else None: no JSON object, a field missing or of the
    wrong type (domain or regex, then fields: {name: type}), both domain and regex, an
    invalid domain (shown(text, limit) shows it), then extra(e), the list's own checks. A
    pattern the rules refuse is no broken entry: it counts as nothing (patterns.refused)."""
    if not isinstance(e, dict):
        return "not a JSON object"
    name = "regex" if "regex" in e else "domain"
    for key, kind in ((name, str), *fields.items()):
        if key not in e or isinstance(e[key], bool) or not isinstance(e[key], kind):
            return f"field {key} missing or of the wrong type"
    if name == "regex":
        if "domain" in e:
            return "both domain and regex"
    elif not DOMAIN.fullmatch(e["domain"]):
        return f"invalid domain {shown(e['domain'], 60)}"
    return extra(e) if extra else None


def valid(entries, check, broken, key=patterns.key, twice=lambda e: f"{patterns.value(e)} listed twice"):
    """The entries; at the first broken one (check(e) says why, see problem) or one whose key(e) came
    before (twice(e) says so) broken(why) ends the command, why "broken entry (...)" or the
    text of twice."""
    seen = set()
    for e in entries:
        why = check(e)
        why = f"broken entry ({why})" if why else twice(e) if key(e) in seen else None
        if why:
            broken(why)
        seen.add(key(e))
    return entries


def write(path, entries, order, legacy=()):
    """The record at path with the entries in order(entries) (readable by the proxy's user),
    then each of the legacy files moved aside (<file>.migrated)."""
    state_files.write_json(path, {"entries": order(entries)}, ensure_ascii=False)
    for old in legacy:
        try:
            os.replace(old, old + ".migrated")
        except FileNotFoundError:
            pass
        except OSError as e:
            die(f"{old}: {e.strerror}", error="action_failed")


@contextlib.contextmanager
def changing(path, read, write, order):
    """Read-modify-write of a record under its lock: read() gives (entries, legacy), the
    entries may be changed in place, and write(entries, legacy) writes them when they changed
    (compared in order(entries)) or still came from the legacy files (the first change
    migrates them)."""
    with state_files.locked(path):
        entries, legacy = read()
        before = json.dumps(order(entries), sort_keys=True)
        yield entries
        if legacy or json.dumps(order(entries), sort_keys=True) != before:
            write(entries, legacy)


def find(entries, ref, **fields):
    """The entry of ref, ("domain", domain) or ("regex", pattern), with these values of
    fields (an Allowlist entry's scope), else None."""
    return next((e for e in entries if patterns.key(e) == tuple(ref)
                 and all(e[k] == v for k, v in fields.items())), None)


# --- the text lists from before the records --------------------------------------------

def legacy_lines(path):
    """The lines of a text list as (what comes before #, the comment), None when the file is
    missing."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return None
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")
    return [(body, comment) for body, _, comment in (line.partition("#") for line in lines)]


def legacy_domain(word):
    """A domain as a text list has it: lowercased, without its trailing dot."""
    return word.lower().rstrip(".")
