"""Times and sizes as agentbox writes and reads them.

Records, JSON output and the VM's journal take UTC in ISO 8601 with a final Z; people at
the terminal read the Host's local time. A leaf module: it imports nothing of agentbox, so
every module (and the Control Room's backend) can use it.
"""
import datetime
import re
import time

# --since and --until of `agentbox logs` and `agentbox activity`: words, an offset back from
# now or a date with an optional time (Host local time)
TIME = re.compile(r"(today|yesterday|now|-\d{1,6}(s|min|h|d|w)|\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?)")
DATE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}(?::\d{2})?))?")


def iso(epoch):
    """Epoch seconds as UTC to the second, "2026-11-13T09:30:00Z"; None when epoch is no
    number."""
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(stamp):
    """The datetime of an ISO 8601 time (a final Z for UTC allowed); None when stamp is no
    such text."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def epoch(stamp):
    """Epoch seconds of an ISO 8601 time (parse); None when stamp is no such text."""
    t = parse(stamp)
    return None if t is None else t.timestamp()


def local(epoch):
    """Epoch seconds in the Host's local time to the minute, "2026-11-13 10:30"; "?" when
    epoch is no number."""
    if not isinstance(epoch, (int, float)):
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def date_time(v):
    """A date with an optional time, YYYY-MM-DD[ HH:MM[:SS]] (or T in place of the space), as
    a naive datetime (no time: midnight); None when v is not written that way. ValueError
    when it names no real day or time."""
    m = DATE_TIME.fullmatch(v)
    if not m:
        return None
    t = m.group(2) or "00:00"
    fmt = "%Y-%m-%d %H:%M:%S" if t.count(":") == 2 else "%Y-%m-%d %H:%M"
    return datetime.datetime.strptime(f"{m.group(1)} {t}", fmt)


def journal_usec(e):
    """The time of a journal record (journalctl --output=json) in microseconds since the
    epoch; None when it has none."""
    try:
        return int(e["__REALTIME_TIMESTAMP"])
    except (KeyError, TypeError, ValueError):
        return None


def journal_time(e):
    """(microseconds, UTC to the microsecond "2026-11-13T09:30:00.123456Z") of a journal
    record; None when it has no usable time."""
    usec = journal_usec(e)
    try:
        when = datetime.datetime.fromtimestamp(usec / 1e6, datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return usec, when.isoformat(timespec="microseconds").replace("+00:00", "Z")


def size(n, largest="TiB"):
    """A number of bytes for people: "512 bytes", "1.5 KiB", up to the unit largest."""
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == largest:
            return f"{n} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
