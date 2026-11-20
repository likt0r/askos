"""The proxy's log lines (proxy/agentbox-proxy), in one place: the proxy writes them with the
functions here, and agentbox reads them back with parse(), for the network view, the Allowlist's
uses and previews, the Alerts of Activity and the held destinations the Control Room notifies
about. One line per connection, English (ADR 0012), and one per held destination:
  [<vm>] ALLOWED <host> (allowlist <domain> <scope>)   or (allowlist regex <pattern> <scope>),
                                                        or (open web)
  [<vm>] BLOCKED <host> (<reason>)      a client reason names the client's address instead
  [<vm>] ALERT <host> [<signal>,...]
  [<vm>] HELD <host> (up to <seconds> s) a destination that is not allowed waits for an entry;
                                        once per host while connections to it are held, each
                                        of which ends with its own ALLOWED or BLOCKED line
  [<vm>] CLOSED <host> (<reason>)       tunnel closed by the proxy (ADR 0005, ADR 0021)
  [<vm>] ERROR <host> (<error>)         destination unreachable
Other lines (message()) are no events: parse() returns None for them.

A leaf module of the package: the proxy imports it, so it imports only the standard library
(tests/test_list_records.py), and the RPM restarts the proxies when it changes."""
import collections
import re

# why the proxy blocked a connection (BLOCKED), and why it closed a tunnel (CLOSED)
NOT_ALLOWED = "not on the Allowlist"
# which Signals are strong is unknown then: nothing passes, and every tunnel closes (ADR 0021)
WATCHLIST_UNREADABLE = "Watchlist unreadable"
NOT_PUBLIC = "does not resolve to a public address"
NOT_TLS = "not a TLS handshake"
NO_SERVER_NAME = "no server name"
# followed by the Signals that keep the destination blocked, whatever would allow it (ADR 0021)
SUSPICIOUS = "suspicious: "
OPEN_WEB_ENDED = "Open web ended"
REMOVED = "removed from the Allowlist"
# a Forbid of the VM covers the destination: refused at once, never held, also in Open web; and
# why a tunnel only Open web allowed closes when a Forbid comes to cover it
FORBIDDEN = "forbidden"
# the reasons of BLOCKED as stable codes the Control Room words (docs/control-room-api.md)
BLOCK_REASONS = {NOT_ALLOWED: "not-allowed", WATCHLIST_UNREADABLE: "watchlist-unreadable", NOT_PUBLIC: "not-public",
                 FORBIDDEN: "forbidden",
                 # a connection the proxy took no destination from: the line names the client
                 NOT_TLS: "not-tls", NO_SERVER_NAME: "no-server-name"}
CLIENT_REASONS = (NOT_TLS, NO_SERVER_NAME)

# A parsed line. kind: ALLOWED, BLOCKED, ALERT, CLOSED, ERROR or HELD; reason: the text in
# parentheses of BLOCKED, CLOSED, ERROR and HELD; signals: of an ALERT, or of a BLOCKED for
# `suspicious`; entry: of an ALLOWED, ("domain" or "regex", value, scope), None for Open web;
# seconds: of a HELD, how long the proxy holds the connection at most.
Event = collections.namedtuple("Event", "vm kind host reason signals entry seconds",
                               defaults=(None, (), None, None))

LINE = re.compile(r"\[([a-z0-9-]+)\] (?:(ALLOWED|BLOCKED|CLOSED|ERROR|HELD) (\S+) \((.*)\)|(ALERT) (\S+) \[(.*)\])")
ENTRY = re.compile(r"allowlist (regex )?(\S+) (\S+)")
HOLD = re.compile(r"up to ([1-9][0-9]{0,3}) s")


def allowed(vm, host, entry=None):
    """entry: ("domain" or "regex", value, scope) of the Allowlist entry, None for Open web."""
    if entry is None:
        return f"[{vm}] ALLOWED {host} (open web)"
    kind, value, scope = entry
    return f"[{vm}] ALLOWED {host} (allowlist {'regex ' if kind == 'regex' else ''}{value} {scope})"


def blocked(vm, host, reason):
    return f"[{vm}] BLOCKED {host} ({reason})"


def suspicious(signals):
    """The reason of a destination its strong Signals keep blocked, or of a tunnel they close."""
    return SUSPICIOUS + ",".join(signals)


def alert(vm, host, signals):
    return f"[{vm}] ALERT {host} [{','.join(signals)}]"


def held(vm, host, seconds):
    return f"[{vm}] HELD {host} (up to {seconds} s)"


def closed(vm, host, reason):
    return f"[{vm}] CLOSED {host} ({reason})"


def error(vm, host, error):
    return f"[{vm}] ERROR {host} ({error})"


def message(vm, text):
    """A line that is no event: that the proxy is ready, errors about its files or its checks."""
    return f"[{vm}] {text}"


def parse(line):
    """The Event of a proxy line, or None for a line that is none."""
    m = LINE.fullmatch(line)
    if not m:
        return None
    vm, kind, host, reason, alerted, alert_host, signals = m.groups()
    if alerted:
        return Event(vm, "ALERT", alert_host, signals=_signals(signals))
    if kind == "ALLOWED":
        e = ENTRY.fullmatch(reason)
        if reason != "open web" and not e:
            return None
        return Event(vm, kind, host, entry=e and ("regex" if e.group(1) else "domain", e.group(2), e.group(3)))
    if kind == "HELD":
        m = HOLD.fullmatch(reason)
        return Event(vm, kind, host, reason, seconds=int(m.group(1))) if m else None
    if kind == "BLOCKED" and reason.startswith(SUSPICIOUS):
        return Event(vm, kind, host, reason, _signals(reason[len(SUSPICIOUS):]))
    return Event(vm, kind, host, reason)


def _signals(text):
    return tuple(text.split(",")) if text else ()


def block_code(reason):
    """The stable code of a BLOCKED line's reason; None for a reason this does not know."""
    return "suspicious" if reason.startswith(SUSPICIOUS) else BLOCK_REASONS.get(reason)


def grep(*kinds):
    """journalctl --grep for the lines of these kinds."""
    return r"^\[[a-z0-9-]+\] " + (kinds[0] if len(kinds) == 1 else f"({'|'.join(kinds)})") + " "
