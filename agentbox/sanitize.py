"""Everything that comes out of a VM is untrusted.

Control, format and line-separator characters could steer a terminal or fake
log lines; colour sequences are noise; pairing codes grant full access to a VM
and must never show up in log output.
"""
import datetime
import re
import unicodedata

MAX_MESSAGE = 8192
MAX_FIELD = 256                      # every field except the message
PAIR_CODE = re.compile(r"orca://pair\?code=[A-Za-z0-9_-]+(?![A-Za-z0-9_-])")
# also percent-encoded: Orca repeats the code inside its web client URL
PAIR_ANY = re.compile(r"orca(?:://|%3A%2F%2F)pair\S*", re.IGNORECASE)
BIDI = set("‪‫‬‭‮⁦⁧⁨⁩")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")      # colour and cursor sequences
# Claude and GitHub tokens (GitHub's are up to ~400 characters with "-", ".", "_"),
# JWTs and private keys, should one slip into a message
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]+|gh[opsur]_[A-Za-z0-9_.-]{8,}|github_pat_[A-Za-z0-9_]{8,}"
                    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def clean(s, multiline=True):
    keep = "\t\n" if multiline else ""
    out = []
    for c in s:
        cat = unicodedata.category(c)
        if c in keep:
            out.append(c)
        elif cat in ("Zl", "Zp"):        # line/paragraph separators: treat as a space
            out.append(" ")
        elif cat not in ("Cc", "Cs", "Cf") and c not in BIDI:
            out.append(c)
    return PAIR_ANY.sub("orca://pair?[hidden]", "".join(out))


def redact(s):
    """Text for the journal: one line, without control characters, Pairing codes
    and anything that looks like a token or key. A safety net: nothing that
    records Activity passes secrets on purpose."""
    return SECRET.sub("[hidden]", clean(" ".join(ANSI.sub("", s).split()), False))


def message_text(v):
    """A journal MESSAGE as text: journald hands messages with control characters
    over as a list of bytes. None if it is neither."""
    if isinstance(v, list) and v and isinstance(v[0], str):   # field present more than once
        return v[0]
    if isinstance(v, list):
        try:
            v = bytes(v).decode("utf-8")
        except (ValueError, TypeError):
            return "[binary data]"
        return "[binary data]" if "\x00" in v else v
    return v if isinstance(v, str) else None


def field(e, key, multiline=False, limit=MAX_FIELD):
    v = message_text(e.get(key))
    if v is None:
        return None
    v = clean(ANSI.sub("", v), multiline)
    return v[:limit] + " ... [truncated]" if limit and len(v) > limit else v


def entry(e):
    """One journal record (journalctl --output=json) as the public log entry."""
    msg = field(e, "MESSAGE", True, limit=None)
    if msg is not None and len(msg) > MAX_MESSAGE:
        msg = msg[:MAX_MESSAGE] + " ... [truncated]"
    try:
        t = datetime.datetime.fromtimestamp(int(e["__REALTIME_TIMESTAMP"]) / 1e6, datetime.timezone.utc)
        when = t.isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        when = None
    prio = field(e, "PRIORITY")
    return {
        "time": when,
        "unit": field(e, "_SYSTEMD_UNIT") or field(e, "_SYSTEMD_USER_UNIT"),
        "identifier": field(e, "SYSLOG_IDENTIFIER") or field(e, "_COMM"),
        "priority": int(prio) if prio and re.fullmatch(r"[0-7]", prio) else None,
        "message": msg,
        "boot": field(e, "_BOOT_ID"),
        "cursor": field(e, "__CURSOR"),
        # text output only
        "_host": field(e, "_HOSTNAME"),
        "_pid": field(e, "_PID") or field(e, "SYSLOG_PID"),
    }


def pairing_codes(records):
    """Every pairing code in these journal records, in their order (colour removed;
    guest.records puts them oldest first)."""
    codes = []
    for e in records:
        v = e.get("MESSAGE")
        if isinstance(v, list) and not (v and isinstance(v[0], str)):
            try:
                v = bytes(v).decode("utf-8", "replace")
            except (ValueError, TypeError):
                continue
        elif isinstance(v, list):
            v = v[0]
        if isinstance(v, str):
            codes += PAIR_CODE.findall(ANSI.sub("", v))
    return codes
