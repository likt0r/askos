"""What a VM may reach: blocked destinations and Alerts from the proxy log, and
Open web. The Allowlist itself is in allowlist.py.

The proxy writes one journal line per event under the identifier
agentbox-proxy-<vm>, into the journald namespace of Activity, in the format of
proxy_log (ALLOWED, BLOCKED, ALERT, CLOSED, ERROR). Lines the VM's proxy did not
write are ignored (activity.trusted).
"""
import json
import re
import time

from . import activity, list_records, proxy_log, settings, state_files, times
from .commands import declare
from .registry import check
from .errors import die
from .state_files import open_web_file

# lowercase labels of letters, digits and inner hyphens, at least two of them
DOMAIN = list_records.DOMAIN
LOG_WINDOW = 86400


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
    """The VM's Alerts and blocked destinations of the last 24 hours, oldest first, as
    (epoch seconds, proxy_log.Event)."""
    lines = activity.proxy_lines((time.time() - LOG_WINDOW, None), [ctx], ("ALERT", "BLOCKED"))
    return [(usec // 10**6, event) for usec, event in ((times.journal_usec(e), event) for e, event in reversed(lines))
            if usec is not None]


def block_reason(why):
    """The code and parameters of a BLOCKED line's reason: the Signals that kept it blocked in
    Open web for `suspicious`; no code for a reason this does not know."""
    code = proxy_log.block_code(why)
    signals = why[len(proxy_log.SUSPICIOUS):].split(",") if code == "suspicious" else None
    return {"code": code, "params": {"signals": ", ".join(signals)} if signals else {}}


def blocked_rows(log):
    """Blocked domains, newest first, with the Signals of their last Alert."""
    signals = {}
    for _, event in log:
        if event.kind == "ALERT":
            signals[event.host] = list(event.signals)
    seen = {}
    for t, event in log:
        if event.kind != "BLOCKED":
            continue
        host, why = event.host, event.reason
        # the reason stays the one of the first block; a line naming the client's address is a
        # row apart from a destination of the same text
        e = seen.setdefault((host, why in proxy_log.CLIENT_REASONS),
                            {"domain": host, "count": 0, "last": 0, "reason": why, **block_reason(why)})
        e["count"] += 1
        e["last"] = max(e["last"], t)
    rows = sorted(seen.values(), key=lambda e: -e["last"])
    for e in rows:
        e["suspicious"] = [] if e["reason"] in proxy_log.CLIENT_REASONS else signals.get(e["domain"], [])
    return rows


def alert_rows(log):
    """Alerts grouped by destination, newest first, with the Signals of the first one."""
    seen = {}
    for t, event in log:
        if event.kind != "ALERT":
            continue
        host, tags = event.host, list(event.signals)
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


DECLARATIONS = {
    "blocked": declare(blocked, "agentbox blocked <vm> [--json]", options={"json": False},
                       read_only=True, control_room=True),
    "alerts": declare(alerts, "agentbox alerts <vm> [--json]", options={"json": False},
                      read_only=True, control_room=True),
    "open-web": declare(open_web, "agentbox open-web <vm> <minutes> [--json]", options={"json": False},
                        control_room=True, kind="allowlist",
                         describe=lambda a: activity.said(
                            f"Open web for {a.positional[1] if len(a.positional) > 1 else ''} minutes",
                            minutes=(a.positional[1] if len(a.positional) > 1 and
                                     re.fullmatch(r"[0-9]{1,6}", a.positional[1]) else None))),
    "close-web": declare(close_web, "agentbox close-web <vm> [--json]", options={"json": False},
                         control_room=True, kind="allowlist",
                          describe=lambda a: activity.said("Close Open web")),
}
