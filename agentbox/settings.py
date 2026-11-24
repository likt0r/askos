"""Settings (#22): the one root-owned record /etc/agentbox/settings.json.

Every value has a built-in default, so a missing file means all defaults; a file
that is not JSON, holds an unknown key or an invalid value is a clear error for
every consumer (load() is the only reader on the Host: the Control Room backend,
which runs unprivileged, imports it too; agentbox-feeds gets its values from the
generated unit; the proxies read only hold_seconds, with list_records.hold_seconds). The
file is world-readable and holds nothing secret.

`agentbox settings` shows the values, `agentbox set-settings KEY=VALUE...` validates
all of them, saves nothing when one is invalid, and otherwise saves and applies what
depends on them (generate.apply: timers, journald retention) without a restart by
hand. The Control Room password, the home network and the ports are not settings. The Control
Room changes every Setting, but turns build_vm only on (CONTROL_ROOM_OFF): off is root's on the
CLI at the Host.

maintenance_time (#49) replaced reset_time and rebuild_time: a file that still holds
reset_time reads as maintenance_time two hours before it (when the version check ran), and
rebuild_time is ignored; the next save writes the file without either (RETIRED).
"""
import json
import os
import re

from . import durations, list_records, paths, state_files
from .commands import declare
from .errors import Fail, die
from .sanitize import clean

SETTINGS = os.path.join(paths.CONF, "settings.json")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ENTRY_DURATIONS = tuple(durations.DURATIONS)       # the Control Room's matching API choices
LANGUAGES = ("en", "de")
BROWSER = "browser"            # default_language: each browser's own language (en or de, else en)
TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
# an Image name, shared by images.py, the VM registry, activity.py and the Control Room backend
IMAGE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# a Build of an Image (images.py; activity.py and the Control Room, which do not import images):
# YYYYMMDD-HHMMSS, before that YYYYMMDD-HHMM or YYYYMMDD
BUILD_ID = re.compile(r"[0-9]{8}(?:-[0-9]{4}(?:[0-9]{2})?)?")
# a release of Orca as its tags name it without the v (vm/build/orca-release takes the same)
ORCA_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
# a release of the npm package playwright, the same form (no prerelease)
PLAYWRIGHT_VERSION = ORCA_VERSION
MAX_MEMORY_MIB = 1048576       # as vms.py: when the Host's memory is unknown; create checks the real one
MAX_DATA_DISK_GIB = 16384
RESET = "default"              # as a value in set-settings: back to the built-in default
# Settings the Control Room may turn on but never off (ADR 0025): a stolen session that turned
# build_vm off could build a Recipe as root on the Host
CONTROL_ROOM_OFF = ("build_vm",)
ALERT_PAUSE, ALERT_NONE = "pause", "none"     # alert_action


class Setting:
    """One key: default, how to read a value (a JSON value or the CLI's text), what is
    allowed, and when a change takes effect if not at once."""

    def __init__(self, default, kind, later=None):
        self.default, (self.parse, self.allowed), self.later = default, kind, later


def _int(low, high):
    def parse(v):
        if isinstance(v, str) and re.fullmatch(r"[0-9]{1,9}", v):
            v = int(v)
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise ValueError
        return v
    return parse, f"whole number from {low} to {high}"


def _choice(*choices):
    def parse(v):
        if v not in choices:
            raise ValueError
        return v
    return parse, "one of " + ", ".join(choices)


def _pattern(regex, what):
    def parse(v):
        if not isinstance(v, str) or not regex.fullmatch(v):
            raise ValueError
        return v
    return parse, what


def _optional(regex, what):
    """Empty or a value of the pattern."""
    parse, _ = _pattern(regex, what)

    def optional(v):
        return v if v == "" else parse(v)
    return optional, what


def _bool():
    """true or false; the CLI's text, or a JSON boolean from the file and the API."""
    def parse(v):
        if isinstance(v, bool):
            return v
        if v in ("true", "false"):
            return v == "true"
        raise ValueError
    return parse, "true or false"


def _young_days():
    # HaGeZi publishes the newly-registered list in bands of seven days, up to 35
    bands = _int(7, 35)[0]

    def parse(v):
        if bands(v) % 7:
            raise ValueError
        return bands(v)
    return parse, "7, 14, 21, 28 or 35"


BUILD = "from the next Build"
NEW_VMS = "for new VMs"
SPEC = {
    "reset_age_days": Setting(7, _int(1, 365)),
    # the Maintenance window (maintenance.py, #49): Feeds, version check, the weekly rebuild on
    # rebuild_day and the Reset check, one after another, daily at this time
    "maintenance_time": Setting("02:00", _pattern(TIME, "time HH:MM")),
    "rebuild_day": Setting("sun", _choice(*DAYS)),
    "builds_kept": Setting(3, _int(1, 20), BUILD),
    "default_memory_mib": Setting(26624, _int(1024, MAX_MEMORY_MIB), NEW_VMS),
    "default_data_disk_gib": Setting(300, _int(1, MAX_DATA_DISK_GIB), NEW_VMS),
    "default_image": Setting("base", _pattern(IMAGE, "name of an Image"), NEW_VMS),
    "default_container_image_cleanup": Setting(True, _bool(), NEW_VMS),
    "default_container_image_cleanup_days": Setting(7, _int(1, 365), NEW_VMS),
    "open_web_max_minutes": Setting(240, _int(1, 1440)),
    # how long the proxy holds a connection to a destination that is not allowed, so the owner
    # can allow it meanwhile (0: refused at once); each proxy reads it itself as it changes,
    # with list_records.hold_seconds, which owns the default and the range
    "hold_seconds": Setting(list_records.HOLD_SECONDS, _int(0, list_records.MAX_HOLD_SECONDS)),
    # what an Alert with a strong Signal does to its VM (ADR 0022): pause it, or nothing (none);
    # agentbox alert-watch reads it at every such Alert
    "alert_action": Setting(ALERT_PAUSE, _choice(ALERT_PAUSE, ALERT_NONE)),
    "temporary_entry_default": Setting("1h", _choice(*durations.DURATIONS), "for new Temporary allowlist entries"),
    "young_domain_days": Setting(14, _young_days(), "at the next fetch of the Feeds"),
    "feed_interval_hours": Setting(12, _int(1, 168)),
    "package_min_age_days": Setting(2, _int(1, 30), BUILD),
    # ADR 0025: Builds and the version check's package count run in throwaway build VMs
    # (build_vm_run.py); off, as root on the Host. agentbox setup writes false on a Host without
    # the memory or KVM for them (hoststeps.build_vms); only root on the CLI turns it off
    # (CONTROL_ROOM_OFF), never the Control Room
    "build_vm": Setting(True, _bool(), BUILD),
    # #114: the version check of the Maintenance window starts a Build of every Image with an
    # update, and its Reset check also resets idle VMs whose Image has a newer Build
    "auto_update": Setting(True, _bool()),
    # the Orca release every Build installs (#113); empty: the newest at least
    # package_min_age_days old (vm/build/orca-release)
    "orca_version": Setting("", _optional(ORCA_VERSION, "empty (automatic: the newest release at least "
                                                        "package_min_age_days old) or a version like 1.4.250"),
                            BUILD),
    # the version of the npm package playwright every Base Build installs, with its browsers
    # (#162); empty: the newest at least package_min_age_days old, as npm --before picks it. A
    # pin must be that old too: npm in the VM refuses a younger @playwright/test
    "playwright_version": Setting("", _optional(PLAYWRIGHT_VERSION, "empty (automatic: the newest version at "
                                                                    "least package_min_age_days old) or a version "
                                                                    "like 1.65.1"),
                                  BUILD),
    "activity_retention_days": Setting(90, _int(1, 3650)),
    "session_hours": Setting(12, _int(1, 168), "at the next sign-in"),
    # en or de wins over the browser's language after sign-in; a language chosen in the
    # browser wins over both, and it does not apply on the sign-in page (web/ui/app/utils/
    # language.ts). The Control Room backend reads it for GET/POST /session
    "default_language": Setting(BROWSER, _choice(BROWSER, *LANGUAGES),
                                "from the next sign-in or the next load of the Control Room"),
}
DEFAULTS = {k: s.default for k, s in SPEC.items()}
# keys of before the Maintenance window (#49): read from an existing file, never written again
RETIRED = ("reset_time", "rebuild_time")
VERSION_CHECK_LEAD = 120     # minutes the version check ran before reset_time, until #49


def check(key, value):
    """The valid value, or a Fail saying what is wrong with it."""
    spec = SPEC.get(key)
    if spec is None and key in RETIRED:
        die(f"{key} is no longer a setting: the Maintenance window (maintenance_time) runs the version check, "
            "the weekly rebuild and the Reset check one after another", error="unknown_setting", setting=key)
    if spec is None:
        shown = clean(str(key), False)[:40]
        die(f"unknown setting: {shown} (known: {', '.join(SPEC)})", error="unknown_setting", setting=shown)
    try:
        return spec.parse(value)
    except (ValueError, TypeError):
        die(f"{key}: invalid value {clean(str(value), False)[:40]} (allowed: {spec.allowed})",
            error="invalid_setting", setting=key)


def stored():
    """The values in the file, validated; {} when there is no file."""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        die(f"{SETTINGS}: unreadable or not JSON", error="action_failed")
    if not isinstance(data, dict):
        die(f"{SETTINGS}: a JSON object expected", error="action_failed")
    data = _migrated(data)
    out = {}
    for key, value in data.items():
        try:
            # the file holds typed values; only the CLI's text is converted (a "90" is an error here)
            if key in SPEC and type(value) is not type(SPEC[key].default):
                die(f"{key}: invalid value {clean(str(value), False)[:40]} (allowed: {SPEC[key].allowed})",
                    error="invalid_setting", setting=key)
            out[key] = check(key, value)
        except Fail as f:
            die(f"{SETTINGS}: {f.message}", error="action_failed")
    return out


def _migrated(data):
    """The file's values with the keys of before the Maintenance window taken over (#49):
    maintenance_time from reset_time, VERSION_CHECK_LEAD earlier (the day before when that
    is before midnight), unless the file names maintenance_time itself; reset_time and
    rebuild_time dropped. A reset_time that is no time is the error it always was."""
    data = dict(data)
    old = data.pop("reset_time", None)
    data.pop("rebuild_time", None)
    if old is not None and "maintenance_time" not in data:
        if not isinstance(old, str) or not TIME.fullmatch(old):
            die(f"{SETTINGS}: reset_time: invalid value {clean(str(old), False)[:40]} (allowed: time HH:MM)",
                error="action_failed")
        hours, minutes = map(int, old.split(":"))
        at = (hours * 60 + minutes - VERSION_CHECK_LEAD) % (24 * 60)
        data["maintenance_time"] = f"{at // 60:02d}:{at % 60:02d}"
    return data


def load():
    """Every setting: the file's value, else the default."""
    return {**DEFAULTS, **stored()}


def describe():
    """For show and the API: value, default, whether it is the default, when a change
    takes effect (None: at once) and what is allowed."""
    values = load()
    return {k: {"value": values[k], "default": s.default, "is_default": values[k] == s.default,
                "applies_later": s.later, "allowed": s.allowed}
            for k, s in SPEC.items()}


def save(changes):
    """Validate every change, then save; nothing is saved when one is invalid. The value
    RESET removes the key, also one stored with the default value, so a later change of the
    built-in default applies. Returns {key: (old, new)} for the values that differ."""
    new = {k: (RESET if v == RESET and k in SPEC else check(k, v)) for k, v in changes.items()}
    with state_files.locked(SETTINGS):
        old = load()
        kept = stored()
        removed = False
        for k, v in new.items():
            if v == RESET:
                removed |= k in kept
                kept.pop(k, None)
            else:
                kept[k] = v
        now = {**DEFAULTS, **kept}
        diff = {k: (old[k], now[k]) for k in new if old[k] != now[k]}
        if diff or removed:
            state_files.write_json(SETTINGS, dict(sorted(kept.items())))
        return diff


# --- commands ----------------------------------------------------------------

def _shown(value):
    """A value for people; an empty one (orca_version, playwright_version: automatic) as "", a
    boolean as true or false."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return '""' if value == "" else value


def _show(args):
    """agentbox settings [--json]"""
    if any(a != "--json" for a in args):
        die("settings [--json]", error="invalid_argument")
    info = describe()
    if "--json" in args:
        print(json.dumps({"settings": info}, ensure_ascii=False))
        return
    for key, i in info.items():
        notes = ["default" if i["is_default"] else f"changed, default: {_shown(i['default'])}"]
        if i["applies_later"]:
            notes.append(f"applies {i['applies_later']}")
        print(f"{key:26} {_shown(i['value'])!s:8} ({'; '.join(notes)})")


def refuse_off_from_control_room(changes):
    """Called through sudo by the Control Room (SUDO_USER, as cli.check_control_room reads it):
    no change of {key: text} turns a CONTROL_ROOM_OFF Setting off; `default` turns it on. Before
    anything is saved."""
    from .cli import CONTROL_ROOM_USER      # cli imports every command's module, this one too
    if os.environ.get("SUDO_USER") != CONTROL_ROOM_USER:
        return
    for key in CONTROL_ROOM_OFF:
        if key in changes and changes[key] != RESET and check(key, changes[key]) is False:
            die(f"{key}=false is not for the Control Room: only root on the CLI at the Host turns it off "
                f"(sudo agentbox set-settings {key}=false)", error="setting_cli_only", setting=key)


def _set(args):
    """agentbox set-settings KEY=VALUE... [--json]  (VALUE `default`: back to the default)"""
    usage = "set-settings KEY=VALUE... [--json] (VALUE default: the default value); each key only once"
    changes = {}
    for a in args:
        if a == "--json":
            continue
        key, eq, value = a.partition("=")
        if not eq or key in changes:
            die(usage, error="invalid_argument")
        changes[key] = value
    if not changes:
        die(usage, error="invalid_argument")
    refuse_off_from_control_room(changes)
    from . import generate     # generate imports activity, which imports this module
    with state_files.locked(paths.REGISTRY):
        generate.ports_free()      # what apply() would stop on stops the change before it is saved
        diff = save(changes)
        if "rebuild_day" in diff:
            from . import maintenance     # it imports this module
            maintenance.rebuild_day_changed()
        try:
            result = generate.apply() if diff else None
        except Fail as f:
            die(f"saved, but not applied: {f.message}; `agentbox regenerate` repeats it", error="action_failed")
    later = {k: SPEC[k].later for k in diff if SPEC[k].later}
    if "--json" in args:
        print(json.dumps({"changed": {k: {"from": o, "to": n} for k, (o, n) in diff.items()},
                          "applies_later": later, "regenerated": result}, ensure_ascii=False))
        return
    if not diff:
        print("unchanged")
        return
    for k, (o, n) in diff.items():
        print(f"{k}: {_shown(o)} -> {_shown(n)}" + (f" (applies {later[k]})" if k in later else ""))
    for line in generate.report(result):
        print(line)


def _describe_settings(args):
    keys = [key for key in dict.fromkeys(value.partition("=")[0] for value in args.positional) if key in SPEC]
    return "Change Settings" + (": " + ", ".join(keys) if keys else ""), {"settings": keys}


DECLARATIONS = {
    "settings": declare(_show, "agentbox settings [--json]", options={"json": False}, read_only=True,
                         control_room=True),
    "set-settings": declare(_set, "agentbox set-settings KEY=VALUE... [--json]", options={"json": False},
                             control_room=True, kind="settings", describe=_describe_settings,
                             allow_unknown=True),
}
