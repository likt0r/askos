"""firewalld, as agentbox setup changes it: the libvirt zone of the VM networks and the rules of
Network mode (ADR 0009).

ensure() makes a zone's permanent configuration hold what it should and reloads only when it
changed something or the running firewall still differs from it, so running setup again calls
firewall-cmd only to ask.
"""
from . import programs


def _cmd(*args, ok=(0,)):
    return programs.run(["firewall-cmd", *args], 120, ok=ok)


def default_zone():
    return _cmd("--get-default-zone").stdout.strip() or "public"


def _has(zone, kind, value, permanent):
    """Whether the zone has it, permanently or in the running firewall. firewall-cmd answers
    0 (yes) or 1 (no); anything else (no such zone, firewalld not running) stops."""
    args = [*(["--permanent"] if permanent else []), f"--zone={zone}", f"--query-{kind}={value}"]
    return _cmd(*args, ok=(0, 1)).returncode == 0


def ensure(zone, add=(), remove=()):
    """The zone with every (kind, value) of add and none of remove; kind is rich-rule, port or
    service. Returns what it changed as (verb, kind, value)."""
    changed, behind = [], False
    for wanted, items in ((True, add), (False, remove)):
        for kind, value in items:
            if _has(zone, kind, value, True) != wanted:
                verb = "add" if wanted else "remove"
                args = ["--permanent", f"--zone={zone}", f"--{verb}-{kind}={value}"]
                _cmd(*args)
                changed.append((verb, kind, value))
            elif _has(zone, kind, value, False) != wanted:
                behind = True       # permanent already, but not loaded (an earlier run stopped)
    if changed or behind:
        _cmd("--reload")
    return changed
