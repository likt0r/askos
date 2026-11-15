"""Detection as the owner sees it: checking a domain, the harmless list, and
Alerts marked as seen.

The Signals themselves come from agentbox-suspicious, the classifier the proxy
loads; check-domain runs it with --json and shows each Signal with its reason.
The harmless list (<CONF>/harmless, root-owned, one domain per line, including
subdomains, or "regex <pattern>" for a Regex entry, ADR 0013) is read by the
classifier: a harmless domain raises no Alert, except for Signals that do not depend
on its name. Everything else stays as it is: with
strong Signals it is still blocked in Open web and allowed only with --force, so
marking a domain harmless never opens a way out. Seen marks are per VM in
<STATE>/alerts-seen-<vm>.json ({domain or "*": epoch seconds}); an Alert counts
as seen when it happened before its mark.
"""
import ipaddress
import json
import os
import re
import time

from . import list_records, patterns, paths, programs, scripts, state_files, times
from .allowlist import _parse
from .entry_store import given, label
from .errors import die
from .network import DOMAIN, done, valid_domain
from .registry import check
from .sanitize import clean

# Signals that do not depend on the name: they raise Alerts even for harmless domains (as in the classifier)
STRUCTURAL = {"raw-ip", "metadata"}
SEEN_KEEP = 2 * 86400                # Alerts are listed for 24 hours; older marks are dropped
# a name only a resolver reads as an address, before the classifier is asked (alert_host)
NUMERIC_HOST = re.compile(r"[0-9a-fx.]{1,253}")
HARMLESS_HEADER = b"# Domains with a Harmless mark, with their subdomains (agentbox mark-harmless/unmark-harmless)\n"


_module = None


def _classifier_module():
    """The classifier as a module, for is_address (loaded once; no bytecode is written next
    to it)."""
    global _module
    if _module is None:
        _module = scripts.load(paths.SUSPICIOUS, "agentbox_suspicious")
    return _module


def harmless_file():
    return os.path.join(paths.CONF, list_records.HARMLESS)


def seen_file(ctx):
    return os.path.join(paths.STATE, f"alerts-seen-{ctx}.json")


def harmless_marks():
    """The Harmless marks as {"domain": d} or {"regex": p}, as the classifier reads them."""
    return list_records.harmless_marks(state_files.read(harmless_file(), b"").decode("utf-8", "replace"))


def within(host, domains):
    """host is one of domains or below one of them."""
    return any(host == d or host.endswith("." + d) for d in domains if d)


def _mark_line(ref):
    """The line of a Harmless mark in the file, without its newline."""
    return (f"regex {ref[1]}" if ref[0] == "regex" else ref[1]).encode()


def _mark_target(args):
    """The mark asked for, ("domain", domain) or ("regex", pattern) from --regex, and whether
    --json was given. A pattern is taken as given; mark-harmless checks a new one."""
    positional, opts = _parse(args, {"regex": True, "json": False})
    if "regex" in opts:
        if positional:
            die("--regex takes the place of the domain; too many arguments: " + clean(positional[0], False)[:60],
                error="invalid_argument")
        return ("regex", opts["regex"]), "json" in opts
    domain = domain_arg(positional[0] if positional else "")
    if len(positional) > 1:
        die("too many arguments: " + clean(positional[1], False)[:60], error="invalid_argument")
    return ("domain", domain), "json" in opts


def flags(args, allowed):
    for a in args:
        if a not in allowed:
            die(f"unknown option: {a}" if a.startswith("-") else f"too many arguments: {a}",
                error="invalid_argument")
    return set(args)


def domain_arg(d):
    """A domain name, not an address."""
    valid_domain(d)
    try:
        ipaddress.ip_address(d)
    except ValueError:
        return d
    die(f"invalid domain: {d}", error="invalid_domain", domain=d)


# --- check-domain -------------------------------------------------------------

def _classifier(*args):
    """agentbox-suspicious with the files of this Host (or the test root): its JSON answer."""
    r = programs.run([paths.SUSPICIOUS, *args], paths.CLASSIFIER_SECONDS, what="agentbox-suspicious",
                     env=paths.classifier_env())
    try:
        return json.loads(r.stdout)
    except ValueError:
        die("agentbox-suspicious: invalid answer", error="action_failed")


def _signal(s):
    """A Signal of the classifier's verdict: its name, whether it is strong or suppressed, the
    reason's English text, code and parameters (text)."""
    return (isinstance(s, dict) and isinstance(s.get("signal"), str) and isinstance(s.get("reason"), str)
            and isinstance(s.get("code"), str) and isinstance(s.get("params"), dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in s["params"].items())
            and isinstance(s.get("strong"), bool) and isinstance(s.get("suppressed"), bool))


def report(domain):
    """The classifier's verdict on a domain."""
    rep = _classifier("--json", domain)
    ok = (isinstance(rep, dict) and isinstance(rep.get("signals"), list) and isinstance(rep.get("feeds"), dict)
          and all(_signal(s) for s in rep["signals"]))
    if not ok:
        die("agentbox-suspicious: invalid answer", error="action_failed")
    return rep


def verdict(rep):
    blocked = "blocked in Open web, allowed only with --force"
    if rep.get("harmless"):
        mark = f"pattern {rep['harmless']}" if rep.get("harmless_regex") else rep["harmless"]
        quiet = f"marked harmless ({mark}): " + ("Alert" if rep.get("alert") else "no Alert")
        return quiet + (f", stays {blocked}" if rep.get("strong") else "")
    if rep.get("strong"):
        return f"strongly suspicious: Alert, {blocked}"
    if rep.get("alert"):
        return "suspicious: Alert"
    if rep.get("legitimate_regex"):
        return f"unremarkable (Legitimate pattern {rep['legitimate']}, which lifts only weak Signals)"
    if rep.get("legitimate"):
        return f"unremarkable (known Legitimate domain {rep['legitimate']})"
    return "unremarkable"


def feed_line(feeds):
    parts = []
    for name, f in sorted(feeds.items()):
        f = f if isinstance(f, dict) else {}
        if not f.get("present"):
            parts.append(f"{name} missing")
            continue
        text = f"{name} {f.get('entries', '?')} entries"
        if isinstance(f.get("updated"), int):
            text += f", as of {times.local(f['updated'])}"
        if f.get("error"):
            text += f", last fetch failed ({str(f['error'])[:120]})"
        parts.append(text)
    return "Feeds: " + "; ".join(parts)


def check_domain(args):
    """agentbox check-domain <domain> [--json]: which Signals a domain has, and why"""
    domain = domain_arg(args[0] if args else "")
    opts = flags(args[1:], {"--json"})
    rep = report(domain)
    if "--json" in opts:
        print(json.dumps(rep))
        return
    print(f"{domain}: {verdict(rep)}")
    for s in rep["signals"]:
        print(f"  {s['signal']:<13} {s['reason']}" + ("  [suppressed]" if s["suppressed"] else ""))
    print(feed_line(rep["feeds"]))


# --- harmless list ------------------------------------------------------------

def harmless(args):
    """agentbox harmless [--json]: the Harmless marks, domains and (with --json as
    {"domain": null, "regex": p}) Regex entries; refused: why the rules refuse a stored pattern
    (it silences nothing until removed), else null"""
    opts = flags(args, {"--json"})
    marks = harmless_marks()
    if "--json" in opts:
        print(json.dumps([{**patterns.both(m), "refused": patterns.refused(m)} for m in marks]))
    else:
        for m in marks:
            refused = patterns.refused(m)
            print(m.get("domain") or f"regex {clean(m['regex'], False)}"
                  + (f"  [{refused}; it silences nothing]" if refused else ""))


def mark_harmless(args):
    """agentbox mark-harmless <domain>|--regex PATTERN [--json]: no more Alerts for it (a
    false positive); blocking in Open web and allow --force stay as they are"""
    ref, as_json = _mark_target(args)
    if ref[0] == "regex":
        patterns.checked(ref[1])
    f = harmless_file()
    line = _mark_line(ref)
    with state_files.locked(f):
        data = state_files.read(f, HARMLESS_HEADER)
        if line not in state_files.lines(data):
            if data and not data.endswith(b"\n"):
                data += b"\n"
            state_files.write_atomic(f, data + line + b"\n", 0o644)
    done(as_json, f"{label(ref)} marked harmless", **given(ref))


def unmark_harmless(args):
    """agentbox unmark-harmless <domain>|--regex PATTERN [--json]"""
    ref, as_json = _mark_target(args)
    f = harmless_file()
    if os.path.isfile(f):
        with state_files.locked(f):
            data = state_files.lines(state_files.read(f))
            mark = list_records.param(ref)
            kept = [l for l in data if list_records.harmless_marks(l.decode("utf-8", "replace")) != [mark]]
            if kept != data:            # every line that reads as the mark, as the classifier reads it
                state_files.write_atomic(f, b"".join(l + b"\n" for l in kept), 0o644)
    done(as_json, f"{label(ref)} no longer marked harmless", **given(ref))


# --- Alerts marked as seen ----------------------------------------------------

def seen_marks(ctx):
    marks = state_files.json_object(seen_file(ctx), {})
    return {k: v for k, v in marks.items() if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}


def alert_host(d):
    """What an Alert can name: a domain or an IP address (as the proxy logs it), also one in a
    form only a resolver reads as an address (16909060, 0x01020304; the classifier's
    is_address, which raises raw-ip for it)."""
    try:
        return str(ipaddress.ip_address(d))
    except ValueError:
        pass
    if DOMAIN.fullmatch(d):
        return d
    if NUMERIC_HOST.fullmatch(d) and _classifier_module().is_address(d):
        return d
    die(f"invalid domain: {d}", error="invalid_domain", domain=d)


def mark_seen(args):
    """agentbox mark-seen <vm> [<domain>] [--json]: the VM's Alerts until now
    (all, or those of one destination) count as seen"""
    ctx = check(args[0] if args else "")
    names = [a for a in args[1:] if not a.startswith("-")]
    opts = flags([a for a in args[1:] if a.startswith("-")], {"--json"})
    if len(names) > 1:
        die(f"too many arguments: {names[1]}", error="invalid_argument")
    domain = alert_host(names[0]) if names else None
    now = int(time.time())
    f = seen_file(ctx)
    with state_files.locked(f):
        marks = {k: v for k, v in seen_marks(ctx).items() if k == "*" or v > now - SEEN_KEEP}
        marks[domain or "*"] = now
        state_files.write_json(f, marks, indent=None, sort_keys=True)
    done("--json" in opts, f"{ctx}: Alerts {'for ' + domain + ' ' if domain else ''}marked as seen",
         vm=ctx, domain=domain, seen=now)


def alert_view(ctx, rows):
    """Alert rows as `agentbox alerts` shows them: without harmless destinations (a domain or
    a pattern that matches; unless a Signal remains that does not depend on the name), each
    marked seen or not."""
    harmless_ = harmless_marks()
    quiet = [m["domain"] for m in harmless_ if "domain" in m]
    compiled = [c for c in (list_records.compile_pattern(m["regex"]) for m in harmless_ if "regex" in m) if c is not None]
    marks = seen_marks(ctx)
    out = []
    for e in rows:
        harmless = within(e["domain"], quiet) or any(list_records.pattern_matches(c, e["domain"]) for c in compiled)
        if harmless and not STRUCTURAL & set(e["tags"]):
            continue
        e["seen"] = e["last"] <= max(marks.get("*", 0), marks.get(e["domain"], 0))
        out.append(e)
    return out


COMMANDS = {"check-domain": check_domain, "harmless": harmless,
            "mark-harmless": mark_harmless, "unmark-harmless": unmark_harmless, "mark-seen": mark_seen}
