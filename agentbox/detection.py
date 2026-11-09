"""Detection as the owner sees it: checking a domain, the harmless list, and
Alerts marked as seen.

The Signals themselves come from agentbox-suspicious, the classifier the proxy
loads; check-domain runs it with --json and shows each Signal with its reason.
The harmless list (<CONF>/harmless, root-owned, one domain per line, including
subdomains) is read by the classifier: a harmless domain raises no Alert, except
for Signals that do not depend on its name. Everything else stays as it is: with
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

from . import paths, state_files
from .errors import die
from .network import DOMAIN, done, valid_domain, when
from .registry import check

# Signals that do not depend on the name: they raise Alerts even for harmless domains (as in the classifier)
STRUCTURAL = {"port", "raw-ip", "metadata"}
SEEN_KEEP = 2 * 86400                # Alerts are listed for 24 hours; older marks are dropped
HARMLESS_HEADER = b"# Domains with a Harmless mark, with their subdomains (agentbox mark-harmless/unmark-harmless)\n"


def harmless_file():
    return os.path.join(paths.CONF, "harmless")


def seen_file(ctx):
    return os.path.join(paths.STATE, f"alerts-seen-{ctx}.json")


def harmless_domains():
    return [line.decode("utf-8", "replace").split("#", 1)[0].strip().lower().rstrip(".")
            for line in state_files.content_lines(harmless_file())]


def within(host, domains):
    """host is one of domains or below one of them."""
    return any(host == d or host.endswith("." + d) for d in domains if d)


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
        quiet = f"marked harmless ({rep['harmless']}): " + ("Alert" if rep.get("alert") else "no Alert")
        return quiet + (f", stays {blocked}" if rep.get("strong") else "")
    if rep.get("strong"):
        return f"strongly suspicious: Alert, {blocked}"
    if rep.get("alert"):
        return "suspicious: Alert"
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
    """agentbox harmless [--json]: domains marked as harmless"""
    opts = flags(args, {"--json"})
    entries = harmless_domains()
    if "--json" in opts:
        print(json.dumps(entries))
    else:
        print("".join(d + "\n" for d in entries), end="")


def mark_harmless(args):
    """agentbox mark-harmless <domain> [--json]: no more Alerts for it (a false
    positive); blocking in Open web and allow --force stay as they are"""
    domain = domain_arg(args[0] if args else "")
    opts = flags(args[1:], {"--json"})
    f = harmless_file()
    with state_files.locked(f):
        try:
            with open(f, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            data = HARMLESS_HEADER
        except OSError as e:
            die(f"{f}: {e.strerror}", error="action_failed")
        if domain.encode() not in state_files.lines(data):
            if data and not data.endswith(b"\n"):
                data += b"\n"
            state_files.write_atomic(f, data + domain.encode() + b"\n", 0o644)
    done("--json" in opts, f"{domain} marked harmless", domain=domain)


def unmark_harmless(args):
    """agentbox unmark-harmless <domain> [--json]"""
    domain = domain_arg(args[0] if args else "")
    opts = flags(args[1:], {"--json"})
    f = harmless_file()
    if os.path.isfile(f):
        with state_files.locked(f):
            try:
                with open(f, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                die(f"{f}: {e.strerror}", error="action_failed")
            entry = domain.encode()
            if entry in state_files.lines(data):
                state_files.write_atomic(f, b"".join(l + b"\n" for l in state_files.lines(data) if l != entry), 0o644)
    done("--json" in opts, f"{domain} no longer marked harmless", domain=domain)


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
    """Alert rows as `agentbox alerts` shows them: without harmless destinations
    (unless a Signal remains that does not depend on the name), each marked seen or not."""
    quiet = harmless_domains()
    marks = seen_marks(ctx)
    out = []
    for e in rows:
        if within(e["domain"], quiet) and not STRUCTURAL & set(e["tags"]):
            continue
        e["seen"] = e["last"] <= max(marks.get("*", 0), marks.get(e["domain"], 0))
        out.append(e)
    return out


COMMANDS = {"check-domain": check_domain, "harmless": harmless,
            "mark-harmless": mark_harmless, "unmark-harmless": unmark_harmless, "mark-seen": mark_seen}
