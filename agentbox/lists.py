"""The hand-maintained Watchlist and the Legitimate domains: records on the Host
(ADR 0009).

Storage. Two root-owned files that the classifier (agentbox-suspicious, which the
proxy loads) reads directly and re-reads whenever they change; for the Signals a
broken file leaves its last good state in effect there. A Watchlist that cannot be
read (the record broken, also in a single entry, or missing without the legacy file)
closes Open web in the proxy, whatever state it holds; the commands here and `open-web` refuse then with
UNREADABLE and the reason (`watchlist` shows it in place of the entries), except that
watch-add --force creates a missing record (which opens Open web again, so the
Control Room cannot):

    <CONF>/watchlist.json   {"entries": [{"domain": "webhook.site", "category": "exfil",
                              "source": "Shai-Hulud 1 (09/2025)", "note": "",
                              "added": 1793700000, "added_by": "bewr"}]}
    <CONF>/legitimate.json  {"entries": [{"domain": "pnpm.io", "note": "",
                              "added": 1793700000, "added_by": "bewr"}]}

An entry covers its domain and every subdomain; there is at most one per domain.
category is one of the classifier's CATEGORIES and becomes the Signal of a hit;
source names the incident or report behind an entry. Only agentbox writes these
files.

A Regex entry (ADR 0013, patterns.py) has a pattern, "regex", in place of the domain,
given with --regex PATTERN; a domain and a pattern of the same text are two entries.
Watchlist patterns skip the breadth check (a broad one only raises false Alerts). A
regex Legitimate domain lifts only weak Signals: which strong ones a pattern would
lift cannot be checked, so it needs no --force and takes none. The repository's proxy/suspicious-domains and proxy/legitimate-domains only
fill a new Host; later changes to them do not reach it.

Built in. The classifier's GENUINE domains (registries and vendors) are
Legitimate domains in code: they are listed as built in, cannot be removed, and
adding one changes nothing.

Before these records, both lists were text files (<CONF>/suspicious-domains,
<CONF>/legitimate-domains), and a new Host still starts with them, from the Release.
`agentbox migrate-lists` (agentbox setup) takes their entries over (added_by
"migration") and moves the files aside (<file>.migrated). Until then the
classifier and the read-only commands use them, and the first change through
agentbox migrates them, recorded in Activity as a migration of its own. Where
neither watchlist.json nor its text file exists, migrate-lists creates no
Watchlist, so Open web stays closed until the first watch-add --force creates it;
an empty record of the Legitimate domains is safe, it only adds Signals.

Strong Signals. Outside Open web nothing here opens anything: only the Allowlist
allows. In Open web a destination without strong Signals is reachable, so
removing a Watchlist entry of a strong category, lowering an entry to a weak one,
and adding a Legitimate domain that would lift strong Signals (its own, or Feed
entries at or below it) need --force, which the Control Room never passes, as for
allow. The Feeds are stored as fetched (Legitimate domains lift their hits only when
the classifier classifies), so the check sees every entry, also those a Legitimate
domain of the Host lifts now and the new one would keep lifted once that one goes. So
does an addition that cannot be checked: any without the threat-intelligence list. A
Legitimate domain never lifts the blockchain pattern, so that pattern needs no such
check.
"""
import contextlib
import json
import os
import time

from . import activity, paths, patterns, settings, state_files
from .allowlist import MAX_NOTE, _parse
from .detection import _classifier, domain_arg, feed_line
from .errors import Fail, die
from .network import DOMAIN, done
from .sanitize import clean

MAX_SOURCE = 200
# the fields of an entry besides domain or regex (patterns.py)
WATCHLIST = {"title": "Watchlist", "file": "watchlist.json", "legacy": "suspicious-domains",
             "fields": {"category": str, "source": str, "note": str, "added": int, "added_by": str}}
LEGITIMATE = {"title": "Legitimate domains", "file": "legitimate.json", "legacy": "legitimate-domains",
              "fields": {"note": str, "added": int, "added_by": str}}
# what migrate-lists says when there is nothing to take over
NOTHING_TO_MIGRATE = "Watchlist and Legitimate domains: nothing to take over"
# what an unreadable Watchlist means (the Control Room answers watchlist_unreadable)
UNREADABLE = "Watchlist unreadable, Open web closed"
# and an unreadable record of the Legitimate domains (legitimate_unreadable); the classifier
# then counts only the built-in ones, which only adds Signals
LEGITIMATE_UNREADABLE = "Legitimate domains unreadable"


class Unreadable(Fail):
    """The Watchlist cannot be read; ends a command with UNREADABLE and why. error: the code,
    watchlist_unreadable, or watchlist_recreate_force_required where --force would create it
    again (which reopens Open web)."""

    def __init__(self, why, error="watchlist_unreadable"):
        super().__init__(f"{UNREADABLE}: {why}", error=error)
        self.why = why


def record_file(kind):
    return os.path.join(paths.CONF, kind["file"])


def legacy_file(kind):
    return os.path.join(paths.CONF, kind["legacy"])


_constants = None


def constants():
    """The classifier's categories ({category: strong}) and built-in Legitimate domains."""
    global _constants
    if _constants is None:
        c = _classifier("--constants")
        if not (isinstance(c, dict) and isinstance(c.get("categories"), dict) and isinstance(c.get("genuine"), list)
                and all(isinstance(k, str) and isinstance(v, bool) for k, v in c["categories"].items())
                and all(isinstance(d, str) for d in c["genuine"])):
            die("agentbox-suspicious: invalid answer", error="action_failed")
        _constants = c
    return _constants


def categories():
    return constants()["categories"]


def genuine():
    return set(constants()["genuine"])


# --- arguments -------------------------------------------------------------------

def _text(value, name, limit):
    """--source or --note: one line of printable characters (None when not given)."""
    if value is None:
        return None
    text = value.strip()
    if len(text) > limit or not text.isprintable():
        die(f"--{name}: at most {limit} printable characters on one line",
            error="invalid_source" if name == "source" else "invalid_note", option=f"--{name}")
    return text


def _target(positional, opts):
    """The entry asked for: ("domain", domain) from the positional argument, or ("regex",
    pattern) from --regex in its place, taken as given (a new one is checked by the caller)."""
    if "regex" in opts:
        if positional:
            die("--regex takes the place of the domain; too many arguments: " + clean(positional[0], False)[:60],
                error="invalid_argument")
        return "regex", opts["regex"]
    domain = domain_arg(positional[0] if positional else "")
    if len(positional) > 1:
        die("too many arguments: " + clean(positional[1], False)[:60], error="invalid_argument")
    return "domain", domain


def label(ref):
    """An entry for people: the domain, or "pattern <regex>"."""
    return f"pattern {ref[1]}" if ref[0] == "regex" else ref[1]


def _given(ref):
    """The entry asked for as a result's fields: domain and regex, the other null."""
    return patterns.both({ref[0]: ref[1]})


def _param(ref):
    """The entry asked for as a failure's parameter: domain or regex."""
    return {ref[0]: ref[1]}


def _category(value):
    if value not in categories():
        die(f"--category must be one of {', '.join(categories())}", error="invalid_category",
            category=clean(str(value), False)[:20])
    return value


# --- storage ---------------------------------------------------------------------

def _problem(kind, e):
    """What is wrong with an entry, or None. The classifier checks the entries of both
    records the same way, with the same texts byte for byte (agentbox-suspicious, _problem)."""
    if not isinstance(e, dict):
        return "not a JSON object"
    name = "regex" if "regex" in e else "domain"
    for key, typ in ((name, str), *kind["fields"].items()):
        if key not in e or isinstance(e[key], bool) or not isinstance(e[key], typ):
            return f"field {key} missing or of the wrong type"
    if name == "regex":
        # a pattern the rules refuse is no broken entry: it counts as nothing (_refused)
        if "domain" in e:
            return "both domain and regex"
    elif not DOMAIN.fullmatch(e["domain"]):
        return f"invalid domain {clean(e['domain'], False)[:60]}"
    if "category" in kind["fields"] and e["category"] not in categories():
        return f"unknown category {clean(e['category'], False)[:20]}"
    if len(e["note"]) > MAX_NOTE or len(e.get("source", "")) > MAX_SOURCE:
        return "text too long"
    return None


def _valid(kind, entries, path):
    """The entries; ends the command at a broken or duplicate one. Such a record is
    unreadable for the classifier as well: for the Watchlist the proxy keeps Open web
    closed, for the Legitimate domains it counts only the built-in ones."""
    seen = set()
    for e in entries:
        why = _problem(kind, e)
        # the classifier's reasons, byte for byte (see _problem)
        why = f"broken entry ({why})" if why else f"{patterns.value(e)} listed twice" \
            if patterns.key(e) in seen else None
        if why and kind is WATCHLIST:
            raise Unreadable(f"{path}: {why}")
        if why:
            die(f"{LEGITIMATE_UNREADABLE}: {path}: {why}", error="legitimate_unreadable")
        seen.add(patterns.key(e))
    return entries


def _entry(kind, domain, now, added_by, category="", source="", note="", ref=None):
    """A new entry of the domain, or of ref, ("domain", domain) or ("regex", pattern)."""
    ref = ref or ("domain", domain)
    e = {"category": category, "source": source, "note": note, "added": now, "added_by": added_by}
    return {ref[0]: ref[1], **{k: e[k] for k in kind["fields"]}}


def _sorted(entries):
    """Domains first, then Regex entries, each by their value."""
    return sorted(entries, key=patterns.key)


def _lines(n):
    return "no line" if n == 0 else "1 line" if n == 1 else f"{n} lines"


def _from_legacy(kind, path, now):
    """(entries, the lines that cannot be carried over) of a text list from before the
    records, read as the classifier read it; built-in Legitimate domains are not taken
    over. A Watchlist domain listed twice keeps its strongest category (of equals the
    last line, which the classifier took), so the migration never weakens an entry."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return [], []
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")
    entries, skipped, at = [], [], {}
    builtin = genuine() if kind is LEGITIMATE else set()
    strong = categories() if kind is WATCHLIST else {}
    for line in lines:
        body, _, comment = line.partition("#")
        parts = body.split()
        if not parts:
            continue
        domain = parts[0].lower().rstrip(".")
        comment = " ".join(comment.split())[:MAX_SOURCE]
        if domain in builtin:
            continue
        if kind is WATCHLIST and len(parts) < 2:
            continue            # the classifier ignored lines without a category
        if not DOMAIN.fullmatch(domain) or kind is WATCHLIST and parts[1] not in strong:
            skipped.append(clean(" ".join(parts[:2]), False)[:60])
            continue
        if kind is WATCHLIST:
            e = _entry(kind, domain, now, "migration", category=parts[1], source=comment)
        else:
            e = _entry(kind, domain, now, "migration", note=comment[:MAX_NOTE])
        if domain not in at:
            at[domain] = len(entries)
            entries.append(e)
        elif kind is WATCHLIST and not (strong[entries[at[domain]]["category"]] and not strong[e["category"]]):
            entries[at[domain]] = e
    return entries, skipped


def _migrated(kind, n, skipped):
    """What a migration did, as an Activity entry of migrate-lists: the description and the
    parameters list (watchlist, legitimate), legacy (the old file), migrated and skipped."""
    lines = {0: "no line", 1: "1 line"}.get(len(skipped), f"{len(skipped)} lines")
    return {"description": f"{kind['title']}: {n} {'entry' if n == 1 else 'entries'} taken over from "
                           f"{kind['legacy']}, {lines} skipped",
            "params": {"list": _list_name(kind), "legacy": kind["legacy"], "migrated": n, "skipped": len(skipped)}}


def _list_name(kind):
    return "watchlist" if kind is WATCHLIST else "legitimate"


def _load(kind, create=False):
    """(the record's entries as stored, None), or while there is no record (None, the
    legacy file or None when that is missing too). Ends the command when the record
    cannot be read. For the Watchlist that, and a missing record without a legacy file,
    is what the classifier calls unreadable (watchlist_unreadable): the proxy lets
    nothing through Open web then, and the message says so. Only a command that
    creates the record (create, watch-add) goes on without one. The reasons are the
    classifier's, byte for byte (see _problem)."""
    path = record_file(kind)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        legacy = legacy_file(kind)
        if os.path.exists(legacy):
            return None, legacy
        if kind is WATCHLIST and not create:
            raise Unreadable(f"{path} missing")
        return None, None
    except OSError as e:
        why = f"{path}: {e.strerror}"
    except ValueError:
        why = f"{path}: not valid JSON"
    else:
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data["entries"], None
        why = f"{path}: no list entries"
    if kind is WATCHLIST:
        raise Unreadable(why)
    die(f"{LEGITIMATE_UNREADABLE}: {why}", error="legitimate_unreadable")


def read(kind, create=False):
    """(entries, the legacy file they come from or None): the record, or while it
    does not exist the text list from before, which the classifier uses then as well
    (none at all, with create, when that is missing too)."""
    entries, legacy = _load(kind, create)
    if entries is not None:
        return _valid(kind, entries, record_file(kind)), None
    return (_from_legacy(kind, legacy, int(time.time()))[0] if legacy else []), legacy


def _refused(kind, e):
    """Why the rules refuse a stored Regex entry, else None (patterns.refused; the Watchlist
    does without the breadth check)."""
    return patterns.refused(e, broad=kind is not WATCHLIST)


def refused_watchlist(entries):
    """Why the Watchlist counts as unreadable for a Regex entry the rules refuse, as the
    classifier says it (its _watchlist_record), else None: it raises no Signal, so the proxy
    keeps Open web closed until it is removed."""
    why = next((w for w in (_refused(WATCHLIST, e) for e in entries) if w), None)
    return why and f"{record_file(WATCHLIST)}: {why}; Open web stays closed until it is removed " \
                   f"(agentbox watch-remove --regex)"


def readable_watchlist():
    """Ends the command while the proxy keeps Open web closed for an unreadable Watchlist
    (agentbox open-web), also for a broken entry or a Regex entry the rules refuse."""
    why = refused_watchlist(read(WATCHLIST)[0])
    if why:
        raise Unreadable(why)


def _write(kind, entries, legacy=None):
    """The record (readable by the proxy's and the Feeds' users), then the legacy file moved aside."""
    data = json.dumps({"entries": _sorted(entries)}, indent=2, ensure_ascii=False) + "\n"
    state_files.write_atomic(record_file(kind), data.encode(), 0o644)
    if legacy:
        try:
            os.replace(legacy, legacy + ".migrated")
        except FileNotFoundError:
            pass
        except OSError as e:
            die(f"{legacy}: {e.strerror}", error="action_failed")


@contextlib.contextmanager
def changing(kind, create=False):
    """Read-modify-write of a record under its lock. The list may be changed in
    place; it is written when it changed or still came from the legacy file. Such a
    migration is an Activity entry of its own, also when the command changes nothing.
    A legacy file with lines that cannot be carried over is left to migrate-lists on the
    Host, which names them: a change from the Control Room must not drop them unseen.
    With create a missing Watchlist without a legacy file starts empty (watch-add)."""
    with state_files.locked(record_file(kind)):
        entries, legacy = read(kind, create)
        if legacy:
            skipped = _from_legacy(kind, legacy, 0)[1]
            if skipped:
                die(f"{legacy}: {_lines(len(skipped))} cannot be taken over ({', '.join(skipped)}); "
                    f"run agentbox migrate-lists on the Host first", error="migration_required")
        migrated = len(entries)
        before = json.dumps(_sorted(entries), sort_keys=True)
        yield entries
        if legacy or json.dumps(_sorted(entries), sort_keys=True) != before:
            _write(kind, entries, legacy)
        if legacy:
            activity.record("action", "migrate-lists", **_migrated(kind, migrated, []))


def find(entries, ref):
    """The entry of ref, ("domain", domain) or ("regex", pattern), else None."""
    return next((e for e in entries if patterns.key(e) == tuple(ref)), None)


# --- the Watchlist ------------------------------------------------------------------

def _shown(e):
    """A Watchlist entry as the commands show it: domain and regex (one of them null), strong,
    and refused (why the rules refuse a stored pattern, else null)."""
    return {**patterns.both(e), "strong": categories()[e["category"]], "refused": _refused(WATCHLIST, e)}


def watchlist(args):
    """agentbox watchlist [--json]: the hand-maintained Watchlist and the state of the
    Feeds (when each was last fetched, entries, the last error). While the Watchlist
    cannot be read, why (unreadable) in place of its entries; the Feeds still show. A Regex
    entry the rules refuse is listed (refused) and makes it unreadable for Open web, as for
    the proxy, until it is removed."""
    positional, opts = _parse(args, {"json": False})
    if positional:
        die("too many arguments: " + clean(positional[0], False)[:60], error="invalid_argument")
    try:
        stored = read(WATCHLIST)[0]
        entries, unreadable = [_shown(e) for e in _sorted(stored)], refused_watchlist(stored)
    except Unreadable as e:
        entries, unreadable = [], e.why
    rep = _classifier("--watchlist")
    if not (isinstance(rep, dict) and isinstance(rep.get("feeds"), dict)):
        die("agentbox-suspicious: invalid answer", error="action_failed")
    conf = settings.load()
    result = {"entries": entries, "unreadable": unreadable, "feeds": rep["feeds"],
              "feed_interval_hours": conf["feed_interval_hours"], "young_domain_days": conf["young_domain_days"]}
    if "json" in opts:
        print(json.dumps(result, ensure_ascii=False))
        return
    if unreadable:
        print(f"{UNREADABLE}: {unreadable}")
    for e in entries:
        what = e["domain"] or "regex " + clean(e["regex"], False)
        print(f"{what:<32} {e['category']:<11} {'[refused] ' if e['refused'] else ''}{e['source']}".rstrip())
    print(feed_line(result["feeds"]))


def watch_add(args):
    """agentbox watch-add <domain>|--regex PATTERN --category C [--source S] [--note N] [--force]
    [--json]: a new Watchlist entry, or an existing one changed (lowering a strong category to a
    weak one only with --force). On a Host without any Watchlist (no record, no text
    file from before) it creates the record, which opens Open web again: only with
    --force, so the Control Room can never recreate a deleted Watchlist. A pattern needs
    no breadth check here."""
    positional, opts = _parse(args, {"category": True, "source": True, "note": True, "force": False,
                                     "regex": True, "json": False})
    ref = _target(positional, opts)
    if ref[0] == "regex":
        patterns.checked(ref[1], broad=False)
    if "category" not in opts:
        die("watch-add needs --category", error="invalid_argument", option="--category")
    category = _category(opts["category"])
    source = _text(opts.get("source"), "source", MAX_SOURCE)
    note = _text(opts.get("note"), "note", MAX_NOTE)
    strong = categories()
    with changing(WATCHLIST, create=True) as entries:
        path = record_file(WATCHLIST)
        if "force" not in opts and not os.path.exists(path) and not os.path.exists(legacy_file(WATCHLIST)):
            raise Unreadable(f"{path} is missing; agentbox watch-add with --force on the Host creates it again "
                             f"and so opens Open web again. Creating it again only with --force",
                             error="watchlist_recreate_force_required")
        e = find(entries, ref)
        if e is None:
            e = _entry(WATCHLIST, None, int(time.time()), activity.actor(), category=category,
                       source=source or "", note=note or "", ref=ref)
            entries.append(e)
            message = f"{label(ref)} on the Watchlist ({category})"
        else:
            if strong[e["category"]] and not strong[category] and "force" not in opts:
                die(f"{label(ref)} is on the Watchlist as {e['category']}, a strong Signal. "
                    f"Downgrading to {category} only with --force", error="force_required", **_param(ref),
                    category=e["category"])
            before = dict(e)
            e["category"] = category
            if source is not None:
                e["source"] = source
            if note is not None:
                e["note"] = note
            if e == before:
                activity.unchanged()
                message = f"{label(ref)} is on the Watchlist like this already ({category})"
            else:
                message = f"{label(ref)} changed on the Watchlist ({category})"
    done("json" in opts, message, **_shown(e))


def watch_remove(args):
    """agentbox watch-remove <domain>|--regex PATTERN [--force] [--json]: the Watchlist entry
    goes; one of a strong category only with --force"""
    positional, opts = _parse(args, {"force": False, "regex": True, "json": False})
    ref = _target(positional, opts)
    with changing(WATCHLIST) as entries:
        e = find(entries, ref)
        if e is not None:
            if categories()[e["category"]] and "force" not in opts:
                die(f"{label(ref)} is on the Watchlist as {e['category']}, a strong Signal. "
                    f"Removing it only with --force", error="force_required", **_param(ref),
                    category=e["category"])
            entries.remove(e)
    if e is None:
        activity.unchanged()
        done("json" in opts, f"{label(ref)} is not on the Watchlist", **_given(ref), removed=False)
        return
    done("json" in opts, f"{label(ref)} removed from the Watchlist", **_given(ref), removed=True)


def watch_set_note(args):
    """agentbox watch-set-note <domain>|--regex PATTERN --note TEXT [--json]: the entry's note
    (empty to clear it)"""
    positional, opts = _parse(args, {"note": True, "regex": True, "json": False})
    ref = _target(positional, opts)
    if "note" not in opts:
        die("watch-set-note needs --note TEXT", error="invalid_argument", option="--note")
    note = _text(opts["note"], "note", MAX_NOTE)
    with changing(WATCHLIST) as entries:
        e = find(entries, ref)
        if e is None:
            die(f"{label(ref)} is not on the Watchlist", error="watchlist_entry_not_found", **_param(ref))
        e["note"] = note
    done("json" in opts, f"Note on {label(ref)} saved", **_shown(e))


# --- the Legitimate domains ---------------------------------------------------------

def legitimate(args):
    """agentbox legitimate [--json]: the Legitimate domains, the Host's and the built-in ones"""
    positional, opts = _parse(args, {"json": False})
    if positional:
        die("too many arguments: " + clean(positional[0], False)[:60], error="invalid_argument")
    own = read(LEGITIMATE)[0]
    builtin = genuine()
    rows = [{**patterns.both(e), "builtin": False, "refused": _refused(LEGITIMATE, e)} for e in own
            if e.get("domain") not in builtin]
    rows += [{"domain": d, "regex": None, "note": "", "added": None, "added_by": None, "builtin": True,
              "refused": None} for d in builtin]
    rows.sort(key=lambda e: (e["domain"] is None, e["domain"] or e["regex"]))
    if "json" in opts:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        what = r["domain"] or "regex " + clean(r["regex"], False)
        said = "built-in" if r["builtin"] else (f"[{r['refused']}; it lifts nothing] " if r["refused"] else "") + r["note"]
        print(f"{what:<32} {said}".rstrip())


def _refuse_lifts(domain):
    """Ends the command when making the domain legitimate would lift strong Signals (the
    classifier's --lifts), or when that cannot be checked without the threat-intelligence list."""
    rep = _classifier("--lifts", domain)
    if not (isinstance(rep, dict) and isinstance(rep.get("signals"), list) and isinstance(rep.get("feed_entries"), list)
            and all(isinstance(s, str) for s in rep["signals"] + rep["feed_entries"])
            and isinstance(rep.get("feed_present"), bool)):
        die("agentbox-suspicious: invalid answer", error="action_failed")
    why = []
    if rep["signals"]:
        why.append(", ".join(rep["signals"]))
    if rep["feed_entries"]:
        why.append("threat-intelligence list: " + ", ".join(rep["feed_entries"]))
    if why:
        die(f"{domain} has strong Signals ({'; '.join(why)}). Legitimate only with --force",
            error="force_required", domain=domain)
    if not rep["feed_present"]:
        die(f"{domain}: cannot be checked without the threat-intelligence list (not fetched yet). "
            f"Legitimate only with --force", error="force_required", domain=domain)


def legitimate_add(args):
    """agentbox legitimate-add <domain>|--regex PATTERN [--note N] [--force] [--json]: a new
    Legitimate domain (with its subdomains); one that would lift strong Signals only with
    --force. A pattern lifts only weak Signals, so it needs no --force and takes none."""
    positional, opts = _parse(args, {"note": True, "force": False, "regex": True, "json": False})
    ref = _target(positional, opts)
    note = _text(opts.get("note"), "note", MAX_NOTE)
    if ref[0] == "regex":
        if "force" in opts:
            die("--force does not apply to a regex Legitimate domain: it lifts no strong Signals",
                error="invalid_argument", option="--force")
        patterns.checked(ref[1])
    elif ref[1] in genuine():
        activity.unchanged()
        done("json" in opts, f"{ref[1]} is built in and legitimate already", **_given(ref), builtin=True)
        return
    with changing(LEGITIMATE) as entries:
        e = find(entries, ref)
        if e is None:
            # under the lock: an entry removed in the meantime is checked like a new one
            if "force" not in opts and ref[0] == "domain":
                _refuse_lifts(ref[1])
            e = _entry(LEGITIMATE, None, int(time.time()), activity.actor(), note=note or "", ref=ref)
            entries.append(e)
            message = f"{label(ref)} added as a Legitimate domain"
        else:
            if note is None or note == e["note"]:
                activity.unchanged()
            else:
                e["note"] = note
            message = f"{label(ref)} is legitimate already"
    done("json" in opts, message, **patterns.both(e), builtin=False)


def legitimate_remove(args):
    """agentbox legitimate-remove <domain>|--regex PATTERN [--json]: the Host's entry goes;
    built-in domains stay (a Host's leftover entry for one goes)"""
    positional, opts = _parse(args, {"regex": True, "json": False})
    ref = _target(positional, opts)
    builtin = ref[0] == "domain" and ref[1] in genuine()
    with changing(LEGITIMATE) as entries:
        e = find(entries, ref)
        if e is not None:
            entries.remove(e)
    if builtin:
        # a record of the Host for a built-in domain (the list a new Host started with brought
        # claudeusercontent.com) can go; the domain stays built in
        if e is None:
            die(f"{ref[1]} is built in and stays legitimate", error="builtin_entry", domain=ref[1])
        done("json" in opts, f"{ref[1]}: the Host's entry is removed, the domain stays built in and legitimate",
             **_given(ref), removed=True)
        return
    if e is None:
        activity.unchanged()
        done("json" in opts, f"{label(ref)} is not an added Legitimate domain", **_given(ref), removed=False)
        return
    done("json" in opts, f"{label(ref)} no longer legitimate", **_given(ref), removed=True)


def legitimate_set_note(args):
    """agentbox legitimate-set-note <domain>|--regex PATTERN --note TEXT [--json]"""
    positional, opts = _parse(args, {"note": True, "regex": True, "json": False})
    ref = _target(positional, opts)
    if "note" not in opts:
        die("legitimate-set-note needs --note TEXT", error="invalid_argument", option="--note")
    note = _text(opts["note"], "note", MAX_NOTE)
    if ref[0] == "domain" and ref[1] in genuine():
        die(f"{ref[1]} is built in and stays legitimate", error="builtin_entry", domain=ref[1])
    with changing(LEGITIMATE) as entries:
        e = find(entries, ref)
        if e is None:
            die(f"{label(ref)} is not an added Legitimate domain", error="legitimate_domain_not_found",
                **_param(ref))
        e["note"] = note
    done("json" in opts, f"Note on {label(ref)} saved", **patterns.both(e), builtin=False)


# --- migration ------------------------------------------------------------------------

def migrate(args):
    """agentbox migrate-lists [--json]: the Watchlist and the Legitimate domains from
    the text files of before the records, every entry; the files are moved aside
    (<file>.migrated), and the lines that cannot be carried over are named. Running it
    again changes nothing. Without a text file and a record of the Watchlist it creates
    none and says so: Open web stays closed. Each migration, and an empty record of the
    Legitimate domains it creates, is an Activity entry of its own. With nothing to
    migrate, a record that cannot be read is a warning (agentbox setup goes on)."""
    positional, opts = _parse(args, {"json": False})
    if positional:
        die("too many arguments: " + clean(positional[0], False)[:60], error="invalid_argument")
    result, said, notes = {}, [], []
    for name, kind in (("watchlist", WATCHLIST), ("legitimate", LEGITIMATE)):
        path, legacy = record_file(kind), legacy_file(kind)
        with state_files.locked(path):
            exists, had_legacy = os.path.exists(path), os.path.exists(legacy)
            if exists and not had_legacy:
                try:
                    read(kind)
                except Fail as e:
                    notes.append(f"WARNING: {e.message}")
                result[name] = {"migrated": 0, "skipped": [], "file": None}
                continue
            entries = read(kind)[0] if exists else []
            found, skipped = _from_legacy(kind, legacy, int(time.time()))
            added = [e for e in found if not find(entries, patterns.key(e))]
            # without a source an empty Watchlist would open Open web with nothing on it:
            # it stays missing, and Open web closed, until watch-add creates it
            if kind is WATCHLIST and not exists and not had_legacy:
                notes.append(f"{kind['title']}: neither {kind['file']} nor {kind['legacy']}, nothing created "
                             f"(Open web stays closed until the first agentbox watch-add --force)")
            elif had_legacy or not exists:
                _write(kind, entries + added, legacy if had_legacy else None)
                if not had_legacy:
                    # only the Legitimate domains: an empty record, which only adds Signals
                    notes.append(f"{kind['title']}: {kind['file']} created empty, no {kind['legacy']}")
                    activity.record("action", "migrate-lists", notes[-1],
                                    params={"list": _list_name(kind), "legacy": kind["legacy"], "created": True})
        result[name] = {"migrated": len(added), "skipped": skipped, "file": kind["legacy"] if had_legacy else None}
        if had_legacy:
            activity.record("action", "migrate-lists", **_migrated(kind, len(added), skipped))
            said.append(f"{kind['title']}: {len(added)} entries taken over from {kind['legacy']}"
                        + (f"; skipped: {', '.join(skipped)}" if skipped else ""))
    # every migration is an entry of its own (above), the command none besides
    activity.unchanged()
    done("json" in opts, "; ".join(said + notes) or NOTHING_TO_MIGRATE, **result)


COMMANDS = {"watchlist": watchlist, "watch-add": watch_add, "watch-remove": watch_remove,
            "watch-set-note": watch_set_note, "legitimate": legitimate, "legitimate-add": legitimate_add,
            "legitimate-remove": legitimate_remove, "legitimate-set-note": legitimate_set_note,
            "migrate-lists": migrate}
