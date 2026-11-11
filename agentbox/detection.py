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
import subprocess
import time

from . import patterns, paths, state_files
from .allowlist import _parse
from .errors import die
from .network import DOMAIN, done, valid_domain, when
from .registry import check
from .sanitize import clean

# Signals that do not depend on the name: they raise Alerts even for harmless domains (as in the classifier)
STRUCTURAL = {"raw-ip", "metadata"}
SEEN_KEEP = 2 * 86400                # Alerts are listed for 24 hours; older marks are dropped
HARMLESS_HEADER = b"# Domains with a Harmless mark, with their subdomains (agentbox mark-harmless/unmark-harmless)\n"


def harmless_file():
    return os.path.join(paths.CONF, "harmless")


def seen_file(ctx):
    return os.path.join(paths.STATE, f"alerts-seen-{ctx}.json")


def harmless_marks():
    """The Harmless marks as {"domain": d} or {"regex": p} (a line "regex <pattern>"), as the
    classifier reads them."""
    out = []
    for line in state_files.content_lines(harmless_file()):
        text = line.decode("utf-8", "replace")
        parts = text.split()
        if len(parts) >= 2 and parts[0] == "regex":
            out.append({"regex": parts[1]})
            continue
        domain = text.split("#", 1)[0].strip().lower().rstrip(".")
        if domain:
            out.append({"domain": domain})
    return out


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
    try:
        r = subprocess.run([paths.SUSPICIOUS, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=paths.classifier_env(), timeout=30)
    except OSError:
        die(f"{paths.SUSPICIOUS} is missing", 127, error="action_failed")
    except subprocess.TimeoutExpired:
        die("agentbox-suspicious does not answer", error="action_failed")
    if r.returncode != 0:
        die("agentbox-suspicious failed: " + (r.stderr.decode("utf-8", "replace").strip()[-200:]
                                              or f"Exit {r.returncode}"), error="action_failed")
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
            text += f", as of {when(f['updated'])}"
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


def _shown(ref):
    return f"pattern {ref[1]}" if ref[0] == "regex" else ref[1]


def mark_harmless(args):
    """agentbox mark-harmless <domain>|--regex PATTERN [--json]: no more Alerts for it (a
    false positive); blocking in Open web and allow --force stay as they are"""
    ref, as_json = _mark_target(args)
    if ref[0] == "regex":
        patterns.checked(ref[1])
    f = harmless_file()
    line = _mark_line(ref)
    with state_files.locked(f):
        try:
            with open(f, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            data = HARMLESS_HEADER
        except OSError as e:
            die(f"{f}: {e.strerror}", error="action_failed")
        if line not in state_files.lines(data):
            if data and not data.endswith(b"\n"):
                data += b"\n"
            state_files.write_atomic(f, data + line + b"\n", 0o644)
    done(as_json, f"{_shown(ref)} marked harmless", **patterns.both({ref[0]: ref[1]}))


def unmark_harmless(args):
    """agentbox unmark-harmless <domain>|--regex PATTERN [--json]"""
    ref, as_json = _mark_target(args)
    f = harmless_file()
    if os.path.isfile(f):
        with state_files.locked(f):
            try:
                with open(f, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                die(f"{f}: {e.strerror}", error="action_failed")
            entry = _mark_line(ref)
            if entry in state_files.lines(data):
                state_files.write_atomic(f, b"".join(l + b"\n" for l in state_files.lines(data) if l != entry), 0o644)
    done(as_json, f"{_shown(ref)} no longer marked harmless", **patterns.both({ref[0]: ref[1]}))


# --- Alerts marked as seen ----------------------------------------------------

def seen_marks(ctx):
    try:
        with open(seen_file(ctx), encoding="utf-8") as f:
            marks = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(marks, dict):
        return {}
    return {k: v for k, v in marks.items() if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}


def alert_host(d):
    """What an Alert can name: a domain or an IP address (as the proxy logs it)."""
    try:
        return str(ipaddress.ip_address(d))
    except ValueError:
        pass
    if not DOMAIN.fullmatch(d):
        die(f"invalid domain: {d}", error="invalid_domain", domain=d)
    return d


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
        state_files.write_atomic(f, (json.dumps(marks, sort_keys=True) + "\n").encode(), 0o644)
    done("--json" in opts, f"{ctx}: Alerts {'for ' + domain + ' ' if domain else ''}marked as seen",
         vm=ctx, domain=domain, seen=now)


def alert_view(ctx, rows):
    """Alert rows as `agentbox alerts` shows them: without harmless destinations (a domain or
    a pattern that matches; unless a Signal remains that does not depend on the name), each
    marked seen or not."""
    harmless_ = harmless_marks()
    quiet = [m["domain"] for m in harmless_ if "domain" in m]
    compiled = [c for c in (patterns.compiled(m["regex"]) for m in harmless_ if "regex" in m) if c is not None]
    marks = seen_marks(ctx)
    out = []
    for e in rows:
        harmless = within(e["domain"], quiet) or any(patterns.matches(c, e["domain"]) for c in compiled)
        if harmless and not STRUCTURAL & set(e["tags"]):
            continue
        e["seen"] = e["last"] <= max(marks.get("*", 0), marks.get(e["domain"], 0))
        out.append(e)
    return out


COMMANDS = {"check-domain": check_domain, "harmless": harmless,
            "mark-harmless": mark_harmless, "unmark-harmless": unmark_harmless, "mark-seen": mark_seen}
