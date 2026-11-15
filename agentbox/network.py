"""What a VM may reach: blocked destinations and Alerts from the proxy log, and
Open web. The Allowlist itself is in allowlist.py.

The proxy writes one journal line per event under the identifier
agentbox-proxy-<vm>, into the journald namespace of Activity, with the
keywords ALLOWED, BLOCKED and ALERT (and CLOSED, ERROR). Lines the VM's proxy
did not write are ignored (activity.trusted).
"""
import json
import re
import time

from . import activity, list_records, settings, state_files, times
from .registry import check
from .errors import die
from .sanitize import field
from .state_files import open_web_file

# lowercase labels of letters, digits and inner hyphens, at least two of them
DOMAIN = list_records.DOMAIN
# proxy lines: "[<vm>] <event>"
BLOCKED = re.compile(r"\[[a-z0-9-]+\] BLOCKED ([a-z0-9.-]+) \((.*)\)")
ALERT = re.compile(r"\[[a-z0-9-]+\] ALERT (\S+) \[(.*)\]")
LOG_WINDOW = 86400
# why the proxy blocked a destination (the reasons of its BLOCKED lines, proxy/agentbox-proxy, part
# of the log format) as a stable code the Control Room words (docs/control-room-api.md)
BLOCK_REASONS = {"not on the Allowlist": "not-allowed", "Watchlist unreadable, Open web closed": "watchlist-unreadable",
                 # a Regex entry matched, but allows nothing while the Watchlist cannot be read
                 "Watchlist unreadable, regex entries closed": "watchlist-unreadable",
                 "does not resolve to a public address": "not-public",
                 # a connection the proxy took no destination from: the line names the client
                 "not a TLS handshake": "not-tls", "no server name": "no-server-name"}
# the reasons of BLOCKED lines that name the client's address, not a destination
CLIENT_REASONS = ("not a TLS handshake", "no server name")
BLOCK_SUSPICIOUS = re.compile(r"suspicious: ([a-z0-9-]+(?:,[a-z0-9-]+)*)")


def valid_domain(d):
    if not DOMAIN.fullmatch(d):
        die(f"invalid domain: {d}", error="invalid_domain", domain=d)
    return d


def write_file(path, data, mode=0o644):
    state_files.write_atomic(path, data, mode)


def done(as_json, message, **fields):
    """Result of a changing command: the message, or with --json an object around it."""
    if as_json:
        print(json.dumps({**fields, "message": message}))
    elif message is not None:
        print(message)


# --- proxy log ---------------------------------------------------------------

def proxy_journal(ctx):
    """The VM's proxy lines with Alerts and blocked destinations of the last 24 hours,
    oldest first, as (epoch seconds, message): only those its proxy wrote, from the
    Activity namespace it logs into and the default journal it logged into before."""
    entries = activity._journal((time.time() - LOG_WINDOW, None), [f"agentbox-proxy-{ctx}"], (activity.PROXY_USER,),
                                grep=r"^\[[a-z0-9-]+\] (ALERT|BLOCKED) ")
    lines = []
    for e in reversed(entries):
        usec = times.journal_usec(e)
        if usec is not None:
            lines.append((usec // 10**6, field(e, "MESSAGE", limit=None) or ""))
    return lines


def block_reason(why):
    """The code and parameters of a BLOCKED line's reason: the Signals that kept it blocked in
    Open web for `suspicious`; no code for a reason this does not know."""
    m = BLOCK_SUSPICIOUS.fullmatch(why)
    if m:
        return {"code": "suspicious", "params": {"signals": ", ".join(m.group(1).split(","))}}
    return {"code": BLOCK_REASONS.get(why), "params": {}}


def blocked_rows(log):
    """Blocked domains, newest first, with the Signals of their last Alert."""
    signals = {}
    for _, message in log:
        v = ALERT.fullmatch(message)
        if v:
            signals[v.group(1)] = v.group(2).split(",")
    seen = {}
    for t, message in log:
        m = BLOCKED.fullmatch(message)
        if not m:
            continue
        host, why = m.group(1), m.group(2)
        # the reason stays the one of the first block; a line naming the client's address is a
        # row apart from a destination of the same text
        e = seen.setdefault((host, why in CLIENT_REASONS),
                            {"domain": host, "count": 0, "last": 0, "reason": why, **block_reason(why)})
        e["count"] += 1
        e["last"] = max(e["last"], t)
    rows = sorted(seen.values(), key=lambda e: -e["last"])
    for e in rows:
        e["suspicious"] = [] if e["reason"] in CLIENT_REASONS else signals.get(e["domain"], [])
    return rows


def alert_rows(log):
    """Alerts grouped by destination, newest first, with the Signals of the first one."""
    seen = {}
    for t, message in log:
        m = ALERT.fullmatch(message)
        if not m:
            continue
        host, tags = m.group(1), m.group(2).split(",")
        e = seen.setdefault(host, {"domain": host, "tags": tags, "count": 0, "first": t, "last": 0})
        e["count"] += 1
        e["last"] = max(e["last"], t)
    return sorted(seen.values(), key=lambda e: -e["last"])


def blocked(args):
    """agentbox blocked <vm> [--json]: blocked domains of the last 24 hours"""
    ctx = check(args[0] if args else "")
    rows = blocked_rows(proxy_journal(ctx))
    if "--json" in args[1:]:
        print(json.dumps(rows))
        return
    for e in rows:
        flag = ("  ALERT: " + ",".join(e["suspicious"])) if e["suspicious"] else ""
        print("%-40s %4dx  last %s  (%s)%s" % (e["domain"], e["count"], times.local(e["last"]), e["reason"], flag))


def alerts(args):
    """agentbox alerts <vm> [--json]: Alerts of the last 24 hours, without harmless
    destinations, each marked seen or not (agentbox mark-seen)"""
    from .detection import alert_view
    ctx = check(args[0] if args else "")
    rows = alert_view(ctx, alert_rows(proxy_journal(ctx)))
    if "--json" in args[1:]:
        print(json.dumps(rows))
        return
    for e in rows:
        print("%-40s %4dx  last %s  %s%s" % (e["domain"], e["count"], times.local(e["last"]), ",".join(e["tags"]),
                                              "  (seen)" if e["seen"] else ""))


# --- Open web ------------------------------------------------------------------

def open_web(args):
    """agentbox open-web <vm> <minutes> [--json]: any public domain, for a while; not
    while the Watchlist is unreadable, because the proxy keeps Open web closed then"""
    from .lists import readable_watchlist
    ctx = check(args[0] if args else "")
    arg = args[1] if len(args) > 1 else ""
    minutes = int(arg) if re.fullmatch(r"[0-9]{1,9}", arg) else 0
    longest = settings.load()["open_web_max_minutes"]
    if not 1 <= minutes <= longest:
        die(f"minutes must be from 1 to {longest}", error="invalid_minutes", value=arg, longest=longest)
    readable_watchlist()
    until = int(time.time()) + minutes * 60
    write_file(open_web_file(ctx), b"%d\n" % until)
    done("--json" in args[2:], f"{ctx}: Open web until {time.strftime('%H:%M', time.localtime(until))}",
         vm=ctx, open_until=until)


def close_web(args):
    """agentbox close-web <vm> [--json]"""
    ctx = check(args[0] if args else "")
    f = open_web_file(ctx)
    state_files.remove(f)
    done("--json" in args[1:], f"{ctx}: Open web closed", vm=ctx)


COMMANDS = {"blocked": blocked, "alerts": alerts, "open-web": open_web, "close-web": close_web}
