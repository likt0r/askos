"""Activity: who did what and when, recorded in the journal (ADR 0004, ADR 0007).

Recording. The dispatcher (cli.py) records every state-changing command in
one place: time, actor, VM, kind, action, outcome, the action's parameters, a
failure's error code and a short English description, as structured fields of a
journal entry under the identifier agentbox-activity. The action is the entry's
stable code (ADR 0012): the Control Room words the entry from it and the parameters
in the user's language, and shows the description only for an action it does not
know. A new command is recorded without doing anything; only
commands in READ_ONLY are not. Other code records events of its own with
record() (automatic Resets here, Allowlist expiries in allowlist.py, the outcome of
every Build in images.py, a VM that does not respond in responding.py, once per
incident by a wake or by `watch`); a command that recorded what is due itself says so
with own_entry() (a wake that gave up), and the dispatcher records nothing for it.

The actor is the global option --actor (the Control Room passes device and
address, timers pass "timer"), else the user at the terminal.

Where. Entries go to the journald namespace "agentbox" through the native
journal protocol; the proxy and the GitHub token service log there too
(LogNamespace= in their units, generate.py). The namespace keeps entries for
retention_days() (journald@agentbox.conf, written by `agentbox regenerate`).
While it is not set up, entries go to the default journal so they are not
lost (journalctl -t agentbox-activity), but they are not part of the timeline.
Only root and the group agentbox-journal may write into the namespace; anyone
else gets a warning and the command stands.

Reading. `agentbox activity` merges the recorded entries (from the namespace
only), Alerts from the proxy and GitHub tokens issued or refused (from the
namespace and the default journal, where they were logged before) into one
timeline (SOURCES), never older than the retention.

Who wrote it. Each writer may write only its own kinds of entries; readers
check that by the fields journald adds itself (trusted()) and count the others
without showing them (untrusted_warning()).

Secrets never reach the journal: descriptions come from validated arguments
and error messages, never from stdin or a command's output, and every value
passes through sanitize.redact on the way in and out.
"""
import ast
import contextlib
import datetime
import json
import os
import pwd
import re
import socket
import struct
import subprocess
import sys
import threading
import time

from . import paths, registry, settings, vmenv
from .errors import Fail, die
from .guest import TIME
from .sanitize import clean, field, message_text, redact

NAMESPACE = "agentbox"
IDENTIFIER = "agentbox-activity"
# kinds of entries in the timeline; `agentbox activity --kind` accepts these
KINDS = ("signin", "action", "allowlist", "alert", "github", "pairing", "build", "reset", "settings",
         "credentials")
# the running sockets of the namespace (stdout, socket, dev-log)
JOURNAL_RUN = paths.host(f"/run/systemd/journal.{NAMESPACE}")
SOCKETS = (f"{JOURNAL_RUN}/socket", paths.host("/run/systemd/journal/socket"))

# Commands that change nothing are not recorded; every other command is, so a
# new command is recorded unless it is listed here. (watch writes only bookkeeping,
# the wakes files, and records its own entries, responding.py.)
READ_ONLY = {"status", "logs", "blocked", "alerts", "allowlist", "owners", "activity", "check-domain", "check-regex",
             "watchlist", "legitimate", "harmless", "pairings", "settings", "github-app", "github-tokens", "images", "build-log",
             "containerfile", "describe", "watch", "version", "env"}
# Commands that record their own entries (one per VM, entry or step) instead of one for the
# call; fetch-versions records none, and check-versions only the start of an automatic update
# (build.automatic); otherwise they change only their records of what is available
# (versions.py). maintenance records each step of the Maintenance window (maintenance.<step>).
OWN_ENTRIES = {"weekly-check", "expire-allowlist", "build-run", "check-versions", "fetch-versions", "maintenance"}
# Kind of a command's entry; every other command is an "action".
COMMAND_KINDS = {"allow": "allowlist", "disallow": "allowlist", "open-web": "allowlist", "close-web": "allowlist",
                 "extend": "allowlist", "make-permanent": "allowlist", "set-note": "allowlist",
                 "migrate-allowlist": "allowlist", "pair": "pairing", "observe": "pairing", "unpair": "pairing",
                 "revoke-pairings": "pairing", "set-settings": "settings",
                 "set-claude-token": "credentials", "set-owners": "credentials", "set-github-app": "credentials",
                 "remove-github-app": "credentials",
                 "build": "build", "set-image": "build", "update-now": "build"}

MAX_ACTOR = 100
MAX_DESCRIPTION = 500                # also each parameter
FIELD_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,40}")
# Fields of an entry, after AGENTBOX_: the action's parameters are PARAM_<NAME>, a failure's
# error code ERROR and its parameters ERROR_PARAM_<NAME>; CODE (the action again) marks an
# entry recorded with them (#69). Every other field is an extra one that is no parameter
# (EXIT, EXPIRED, the Control Room's ADDRESS and DEVICE) and is never part of the timeline.
PARAM, ERROR_PARAM = "PARAM_", "ERROR_PARAM_"
FIXED = {"KIND", "ACTION", "CODE", "VM", "ACTOR", "OUTCOME", "DESCRIPTION", "ERROR"}
PARAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,27}")
ERROR_CODE = re.compile(r"[a-z][a-z_]{0,63}")

# Retention: activity_retention_days of the Settings (settings.py).
JOURNALD_CONF = paths.host(f"/etc/systemd/journald@{NAMESPACE}.conf")

_actor = None
_unchanged = False
_refused = False                     # the namespace did not let this user write (said once)
_own_entry = False
_result = {}                         # parameters a command adds once it ran (result())


# --- actor -------------------------------------------------------------------

def valid_actor(v):
    return (isinstance(v, str) and 0 < len(v) <= MAX_ACTOR and v.isprintable() and v == v.strip()
            and not v.startswith("-"))


def take_actor(args):
    """Split the global option --actor TEXT (or --actor=TEXT) off the front of
    the arguments, before the command: (value or None, the other arguments)."""
    if args and args[0] == "--actor":
        if len(args) < 2:
            die("--actor needs a value", 2, error="invalid_argument", option="--actor")
        return args[1], args[2:]
    if args and args[0].startswith("--actor="):
        return args[0][len("--actor="):], args[1:]
    return None, args


def set_actor(value):
    """The actor of this call: the --actor value, else the user at the terminal."""
    global _actor
    if value is not None and not valid_actor(value):
        die(f"invalid value for --actor (1 to {MAX_ACTOR} printable characters)", 2, error="invalid_argument",
            option="--actor")
    _actor = value if value is not None else terminal_user()


def actor():
    """The actor of this call (who adds an Allowlist entry, for instance)."""
    return _actor or terminal_user()


def terminal_user():
    user = os.environ.get("SUDO_USER", "")
    if valid_actor(user):
        return user
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return f"uid {os.getuid()}"


# --- recording -----------------------------------------------------------------

def _encode(fields):
    """One entry in the native journal protocol (systemd's journal-native(7))."""
    out = bytearray()
    for key, value in fields.items():
        data = value.encode("utf-8", "replace")
        if b"\n" in data:
            out += key.encode() + b"\n" + struct.pack("<Q", len(data)) + data + b"\n"
        else:
            out += key.encode() + b"=" + data + b"\n"
    return bytes(out)


def _send(data):
    """To the namespace's journal, else (not set up) the default journal. Nothing
    when no journal is there at all (tests without one); a warning when it refuses,
    and when the namespace does not let this user write: then nowhere else either."""
    error = None
    for path in SOCKETS:
        if not os.path.exists(path):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.sendto(data, path)
            return
        except PermissionError as e:
            if path != SOCKETS[0]:
                error = e
                continue
            global _refused
            if not _refused:          # once per call, however many entries it records
                _refused = True
                print(f"agentbox: Activity not recorded: only root and the group {JOURNAL_GROUP} write into "
                      f"the journal (the members of libvirt, after agentbox setup and logging in again)",
                      file=sys.stderr)
            return
        except OSError as e:
            error = e
    if error:
        print(f"agentbox: Activity not recorded in the journal ({error.strerror or error})", file=sys.stderr)


def _value(v):
    """A field's value as text: a flag is "true" when set and left out otherwise, a list
    is joined with ", ", nothing (None, "", False) is left out."""
    if v is None or v is False or v == "":
        return None
    if v is True:
        return "true"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or None
    return str(v)


def record(kind, action, description, vm=None, outcome="ok", actor=None, params=None, error=None,
           error_params=None, **fields):
    """Record one Activity entry in the journal.

    kind: one of KINDS. action: the entry's stable code, the command ("allow") or
    "<kind>.<event>" ("reset.automatic", "allowlist.expired"). description: short
    English text for people, the fallback where the Control Room does not know the
    action. vm: the VM it concerns, if any. outcome: "ok" or "failed". actor:
    defaults to the actor of this call. params: the action's parameters ({name:
    value}: a domain, a scope, a Build, a flag such as no_start), from which the
    Control Room words the entry. error: a failure's error code (Fail.error), with
    error_params (Fail.params). Extra fields become AGENTBOX_<NAME> (letters,
    digits, underscore) and are no parameters.

    Never pass secrets, in parameters either. Every value is reduced to one clean
    line and known token, key and Pairing-code formats are hidden, but that is a
    safety net."""
    actor = actor or _actor or terminal_user()
    extra = {k.upper(): v for k, v in fields.items()}
    if any(k in FIXED or k.startswith((PARAM, ERROR_PARAM)) for k in extra):
        raise ValueError(f"Activity field {sorted(extra)} taken by the entry itself")
    # a parameter the timeline would not read back (recorded_entry) is a mistake here
    for name in params or {}:
        if not PARAM_NAME.fullmatch(name):
            raise ValueError(f"invalid Activity parameter {name!r}")
    entry = {"KIND": kind, "ACTION": action, "CODE": action, "VM": vm, "ACTOR": actor, "OUTCOME": outcome,
             "DESCRIPTION": description, "ERROR": error, **extra,
             **{PARAM + k.upper(): v for k, v in (params or {}).items()},
             # a failure's parameters come from wherever it was raised: names that fit only
             **{ERROR_PARAM + k.upper(): v for k, v in (error_params or {}).items() if PARAM_NAME.fullmatch(k)}}
    out = {}
    for key, value in entry.items():
        if not FIELD_NAME.fullmatch(key):
            raise ValueError(f"invalid Activity field {key!r}")
        value = _value(value)
        if value is None:
            continue
        limit = MAX_DESCRIPTION if key == "DESCRIPTION" or key.startswith((PARAM, ERROR_PARAM)) else MAX_ACTOR
        out[f"AGENTBOX_{key}"] = redact(value)[:limit]
    message = (f"{out.get('AGENTBOX_VM', '-')} {out['AGENTBOX_ACTION']} {out['AGENTBOX_OUTCOME']} "
               f"({out['AGENTBOX_ACTOR']}): {out.get('AGENTBOX_DESCRIPTION', '')}")
    _send(_encode({"MESSAGE": message, "PRIORITY": "5" if outcome == "ok" else "4",
                   "SYSLOG_IDENTIFIER": IDENTIFIER, **out}))


def unchanged():
    """For a command that turned out to change nothing (waking a VM that runs):
    the dispatcher records no entry for it."""
    global _unchanged
    _unchanged = True


def result(**params):
    """Parameters of the command's entry that are known only once it ran (an import's files
    and bytes); the dispatcher adds them to those DESCRIBE gives."""
    _result.update(params)


def own_entry():
    """For a command that recorded what is due itself, or found that nothing is (a wake
    giving up on a VM that does not answer: one entry per incident, #54): the dispatcher
    records no entry for it, whatever its outcome."""
    global _own_entry
    _own_entry = True


def _arg(args, i):
    return args[i] if len(args) > i else ""


# Allowlist and list commands: options that take a value, and the durations of --for
_VALUED = ("--for", "--note", "--category", "--source", "--regex")
_DURATIONS = {"15min": "15 minutes", "1h": "1 hour", "8h": "8 hours", "7d": "7 days"}
# The Watchlist's categories, as the classifier's CATEGORIES (tests/test_lists.py checks
# that): a description names only one of these, never what a request passed as --category.
WATCH_CATEGORIES = ("exfil", "c2", "tunnel", "paste", "phishing", "blockchain", "payload")
# a Build's name, as images.BUILD_ID (images imports this module)
_BUILD = re.compile(r"\d{8}(?:-\d{4}(?:\d{2})?)?")


def _positional(args):
    """The arguments that are neither an option nor an option's value."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in _VALUED:
            skip = True
        elif not a.startswith("--"):
            out.append(a)
    return out


def _opt(args, name):
    """The value of --name V or --name=V, else None."""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return None


def _resets(args):
    """The VMs of `build --reset <vm>...` (an Update) that are VM names."""
    found = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--reset"]
    found += [a[len("--reset="):] for a in args if a.startswith("--reset=")]
    return [v for v in dict.fromkeys(found) if registry.NAME.fullmatch(v)]


def _name(v, pattern=registry.NAME):
    """v if it is a name of that form (a VM by default, an Image, a Build), else None: a
    parameter carries a name only when it is one."""
    return v if isinstance(v, str) and pattern.fullmatch(v) else None


def _said(text, **params):
    """A command's description and its parameters, as record() takes them."""
    return text, params


def _forced(a, text, params):
    """With --force: said, and the flag force."""
    return (f"{text} with --force", {**params, "force": True}) if "--force" in a else (text, params)


def _entry(a, p, i):
    """The entry a list command names: (its text, its parameter), the domain at position i, or
    the pattern of --regex (a Regex entry, ADR 0013)."""
    pattern = _opt(a, "--regex")
    if pattern is not None:
        return f"pattern {pattern}", {"regex": pattern}
    return _arg(p, i), {"domain": _arg(p, i)}


def _listed(what, duration=None, temporary=False, force=False):
    """Description of an Allowlist command `<vm|all> <domain>`: `what` with the domain (and
    "for all VMs"), then the duration of --for after `duration` ("for", "by") or "temporarily"
    (--temporary), and --force where the command takes it. Parameters: domain, or regex for a
    Regex entry (--regex), scope (a VM or all), duration (15min, 1h, 8h, 7d), the flags
    temporary and force."""
    def describe(a):
        p = _positional(a)
        scope, (shown, given) = _arg(p, 0), _entry(a, p, 1)
        text = what.format(shown + (" for all VMs" if scope == "all" else ""))
        params = {**given, "scope": "all" if scope == "all" else _name(scope)}
        d = _opt(a, "--for")
        if duration and d in _DURATIONS:
            text, params["duration"] = f"{text} {duration} {_DURATIONS[d]}", d
        elif temporary and "--temporary" in a:
            text, params["temporary"] = f"{text} temporarily", True
        return _forced(a, text, params) if force else (text, params)
    return describe


def _domain(what, force=False):
    """Description of a command `<domain>` (Watchlist, Legitimate domains, Harmless marks):
    `what` with the domain (or the pattern of --regex), and --force where the command takes it."""
    def describe(a):
        shown, params = _entry(a, _positional(a), 0)
        text = what.format(shown)
        return _forced(a, text, params) if force else (text, params)
    return describe


def _watch_add(a):
    (shown, given), category = _entry(a, _positional(a), 0), _opt(a, "--category")
    category = category if category in WATCH_CATEGORIES else None
    return _forced(a, f"Watchlist: add {shown}" + (f" ({category})" if category else ""),
                   {**given, "category": category})


def _mark_seen(a):
    domain = _arg(_positional(a), 1)
    return _said(f"Mark the Alerts for {domain} as seen", domain=domain) if domain else _said("Mark Alerts as seen")


# Room for the list of VM variables in a description, so a failure's message fits behind it
ENV_LISTED = 360


def _fit(items, room, more):
    """items joined with ", ", as many whole ones as fit into room characters together with
    more(n) for the n left out (a first item longer than that is cut, ending in "…"): a cut
    list says so, where record() would cut it silently."""
    shown, used = [], 0
    for i, item in enumerate(items):
        rest = len(items) - i - 1
        needed = used + (2 if shown else 0) + len(item) + (len(more(rest)) if rest else 0)
        if needed > room:
            break
        shown.append(item)
        used += (2 if len(shown) > 1 else 0) + len(item)
    if not shown and items:
        tail = more(len(items) - 1) if len(items) > 1 else ""
        return items[0][:max(0, room - len(tail) - 1)] + "…" + tail
    left = len(items) - len(shown)
    return ", ".join(shown) + (more(left) if left else "")


def _env_param(items):
    """A list of VM variables or names as a parameter, cut where it would not fit (", … +n")."""
    return _fit(items, MAX_DESCRIPTION, lambda n: f", … +{n}") if items else None


def _env_listed(items):
    return _fit(items, ENV_LISTED, lambda n: f" and {n} more")


def _set_env(a):
    """The variables as NAME=value (their values are not secret, vmenv.py), only those whose
    name has the form of one, and the flag replace. The description lists the names before
    the values: the values only when all fit."""
    pairs = [p for p in _positional(a)[1:] if vmenv.NAME.fullmatch(p.partition("=")[0]) and "=" in p]
    listed = ", ".join(pairs)
    if len(listed) > ENV_LISTED:
        listed = _env_listed([p.partition("=")[0] for p in pairs]) + " (values left out, too long)"
    if "--replace" in a:
        return _said("Replace the VM variables" + (": " + listed if pairs else " with none"),
                     env=_env_param(pairs), replace=True)
    return _said("Set VM variables" + (": " + listed if pairs else ""), env=_env_param(pairs))


def _unset_env(a):
    names = [n for n in _positional(a)[1:] if vmenv.NAME.fullmatch(n)]
    return _said("Remove VM variables" + (": " + _env_listed(names) if names else ""), names=_env_param(names))


def _create(a):
    image = _name(_opt(a, "--image"), settings.IMAGE)
    return _said("Create the VM" + (f" with Image {image}" if image else ""), image=image)


def _owners(a):
    owners = [o for o in a[1:] if o != "--json"]
    return _said("Set GitHub owners: " + ", ".join(owners), owners=owners)


def _settings(a):
    """The Settings' keys only, never what was passed instead of one."""
    keys = [k for k in dict.fromkeys(x.partition("=")[0] for x in a if x != "--json") if k in settings.SPEC]
    return _said("Change Settings" + (": " + ", ".join(keys) if keys else ""), settings=keys)


def _then_reset(a, text, **params):
    """build and update-now: with the VMs an Update resets (--reset) after its Builds."""
    resets = _resets(a)
    return _said(text + (", then a Reset of " + ", ".join(resets) if resets else ""), **params, reset=resets)


def _build(a):
    if "--all" in a:
        return _then_reset(a, "Start a Build of all Images", all=True)
    return _then_reset(a, f"Start a Build of {_arg(a, 0)}", image=_name(_arg(a, 0), settings.IMAGE))


def _import(a):
    """The folder as an absolute path; the target only when it is one."""
    from . import transfer               # transfer imports this module
    p = _positional(a)
    folder = transfer.folder_path(_arg(p, 1)) if _arg(p, 1) else None
    target = transfer.target_for(_arg(p, 1), _arg(p, 2)) if folder else None
    target = target if target and transfer.target_problem(target) is None else None
    if folder and target:
        return _said(f"Import {folder} into ~/code/{target}", folder=folder, target=target)
    return _said("Import a folder", folder=folder)


def _set_image(a):
    image, build = _arg(a, 1), _opt(a, "--build")
    return _said(f"Image {image}, " + (f"pinned to Build {build}" if build else "always its current Build")
                 + ", from the next Reset", image=_name(image, settings.IMAGE), build=_name(build, _BUILD))


# What a command was asked to do, for its entry (works for either outcome): the English
# description and the parameters the Control Room words it from (docs/control-room-api.md).
# Commands not listed are described by their command line, without parameters.
DESCRIBE = {
    "start": lambda a: _said("Start"),
    "wake": lambda a: _said("Wake"),
    "pause": lambda a: _said("Pause"),
    "restart": lambda a: _said("Restart"),
    "reset": lambda a: _said("Reset without start", no_start=True) if "--no-start" in a[1:] else _said("Reset"),
    "focus": lambda a: _said("Focus off", off=True) if _arg(a, 0) == "off"
                       else _said(f"Focus on {_arg(a, 0)}", target=_name(_arg(a, 0))),
    "create": _create,
    "delete": lambda a: _said("Delete the VM and its Data disk", delete_data_disk=True)
                        if "--delete-data-disk" in a else _said("Delete the VM"),
    "set-memory": lambda a: _said(f"Memory limit {_arg(a, 1)} MiB", mib=_arg(a, 1))
                            if re.fullmatch(r"[0-9]{1,9}", _arg(a, 1)) else _said("Change the memory limit"),
    "set-env": _set_env,
    "unset-env": _unset_env,
    "allow": _listed("Allow {}", "for", temporary=True, force=True),
    "disallow": _listed("Remove the Allowlist entry {}"),
    "extend": _listed("Extend the Allowlist entry {}", "by", force=True),
    "make-permanent": _listed("Make the Allowlist entry {} permanent", force=True),
    "set-note": _listed("Note on the Allowlist entry {}"),
    "migrate-allowlist": lambda a: _said("Take over the old Allowlist files"),
    "open-web": lambda a: _said(f"Open web for {_arg(a, 1)} minutes",
                                minutes=_arg(a, 1) if re.fullmatch(r"[0-9]{1,6}", _arg(a, 1)) else None),
    "close-web": lambda a: _said("Close Open web"),
    "mark-seen": _mark_seen,
    "mark-harmless": _domain("Harmless mark for {}"),
    "unmark-harmless": _domain("Remove the Harmless mark for {}"),
    "watch-add": _watch_add,
    "watch-remove": _domain("Watchlist: remove {}", force=True),
    "watch-set-note": _domain("Watchlist: note on {}"),
    "legitimate-add": _domain("Add the Legitimate domain {}", force=True),
    "legitimate-remove": _domain("Remove the Legitimate domain {}"),
    "legitimate-set-note": _domain("Note on the Legitimate domain {}"),
    "migrate-lists": lambda a: _said("Take over the Watchlist and the Legitimate domains from the text files"),
    "set-owners": _owners,
    "set-claude-token": lambda a: _said("Set the Claude token"),
    # the App ID only when it is one: never echo what might be a pasted key
    "set-github-app": lambda a: _said(f"Add or replace the GitHub App (App ID {_arg(a, 0)})", app_id=_arg(a, 0))
                                if re.fullmatch(r"[1-9][0-9]{0,11}", _arg(a, 0))
                                else _said("Add or replace the GitHub App"),
    "remove-github-app": lambda a: _said(f"Remove the GitHub App (App ID {_arg(a, 0)})", app_id=_arg(a, 0))
                                   if re.fullmatch(r"[1-9][0-9]{0,11}", _arg(a, 0))
                                   else _said("Remove a GitHub App"),
    "pair": lambda a: _said("Show the Pairing code"),
    "observe": lambda a: _said("Pair the Host as Observer"),
    "unpair": lambda a: _said("Remove a single device"),
    "revoke-pairings": lambda a: _said("Revoke all pairings"),
    "migrate": lambda a: _said("Take over work and priv into the VM registry"),
    "regenerate": lambda a: _said("Generate the configuration from the VM registry"),
    "setup": lambda a: _said("Set up the Host"),
    "set-settings": _settings,
    "build": _build,
    "update-now": lambda a: _then_reset(a, "Update now: check the versions, build every Image with an update"),
    "set-image": _set_image,
    "import": _import,
}


def _failure_params(params):
    """A failure's parameters as Activity keeps them, with the care of the command's own: a
    category or a Setting only when it is a known one (invalid_category and unknown_setting
    name what was passed, which could be anything pasted, a token too)."""
    known = {"category": WATCH_CATEGORIES, "setting": settings.SPEC}
    return {k: v for k, v in params.items() if k not in known or isinstance(v, str) and v in known[k]}


def _known_vm(args):
    """The first argument, if a VM of that name is registered."""
    name = _arg(args, 0)
    if registry.NAME.fullmatch(name) and name not in registry.RESERVED and os.path.exists(registry.record_file(name)):
        return name
    return None


@contextlib.contextmanager
def command(cmd, args):
    """Record the command that runs inside this block, with its outcome. The VM
    is the first argument if such a VM exists before or after the command (so
    creating and deleting one name it). A failure adds its message to the
    description and records its error code and parameters (Fail); an interrupt
    is `interrupted`, any other exception `unexpected_error`."""
    global _unchanged, _own_entry
    if cmd in READ_ONLY or cmd in OWN_ENTRIES:
        yield
        return
    _unchanged = _own_entry = False
    _result.clear()
    vm_before = _known_vm(args)
    what, params = DESCRIBE[cmd](args) if cmd in DESCRIBE else (None, {})
    kind = COMMAND_KINDS.get(cmd, "action")

    def entry(outcome, message=None, error=None, error_params=None, **fields):
        vm = vm_before or _known_vm(args)
        text = what
        if text is None:          # new commands: the command line without the VM and --json
            text = " ".join([cmd, *(a for a in (args[1:] if vm else args) if a != "--json")])
        record(kind, cmd, f"{text}: {message}" if message else text, vm, outcome, params={**params, **_result},
               error=error, error_params=error_params, **fields)

    try:
        yield
    except Fail as f:
        if not _own_entry:
            entry("failed", f.message.strip(), f.error, _failure_params(f.params), exit=f.exit)
        raise
    except KeyboardInterrupt:
        entry("failed", "interrupted", "interrupted")
        raise
    except Exception as e:
        entry("failed", f"internal error ({type(e).__name__})", "unexpected_error", {"type": type(e).__name__})
        raise
    if not _unchanged and not _own_entry:
        entry("ok")


# --- retention -------------------------------------------------------------------

def retention_days():
    """How many days Activity is kept: activity_retention_days of the Settings
    (default 90); `agentbox regenerate` applies it."""
    return settings.load()["activity_retention_days"]


def journald_conf(days):
    """Configuration of the namespace's journald. Files are rotated daily, so
    journald deletes entries within a day of their expiry."""
    return (f"# Generated by agentbox regenerate (activity_retention_days in /etc/agentbox/settings.json)\n"
            f"[Journal]\nStorage=persistent\nMaxRetentionSec={days}day\nMaxFileSec=1day\n")


# --- who wrote an entry --------------------------------------------------------------

# Who may write into the namespace is decided when writing: its sockets belong to
# root and the group JOURNAL_GROUP, mode 0660 (the package's drop-in). Members are the
# members of libvirt (they manage every VM anyway; agentbox setup) and the services that
# log there (generated units, agentbox-web.service). Readers never ask about groups, so
# an entry that was allowed when it was written stays in the history.
#
# Each writer may still write only its own kinds of entries, so that a compromised
# service, above all a proxy, cannot fake others. journald adds fields that no client
# can set, _UID from the kernel and _SYSTEMD_UNIT from the sender's cgroup:
#   agentbox-activity     the agentbox CLI (root, or a member of the group): its
#                         commands and events (CLI_KINDS), from any _UID; journald
#                         misses the unit of a CLI that has already ended.
#                         The Control Room (agentbox-web.service as agentbox-web): its
#                         sign-ins and sign-outs, without a VM, and nothing else.
#                         The proxy and token service users: nothing.
#   agentbox-proxy-<vm>   that VM's proxy, agentbox-proxy-<vm>.service as agentbox-proxy
#   agentbox-gh-<vm>      that VM's token service, agentbox-ghvm-<vm>@<connection>.service
#                         as agentbox-gh
# The services' logs are also read from the default journal (before the namespace),
# which anyone can write, so their readers ask journalctl only for entries of their
# user (_uids()); `agentbox watch` counts what it leaves out.
JOURNAL_GROUP = "agentbox-journal"
CONTROL_ROOM = ("agentbox-web", "agentbox-web.service")
SIGN_INS = {"signin", "signin.failed", "signin.locked", "signout"}      # web/agentbox-web
PROXY_USER, TOKEN_USER = "agentbox-proxy", "agentbox-gh"                # rpm/agentbox.sysusers
CLI_KINDS = set(KINDS) - {"signin", "alert", "github"}                  # what record() is given
SERVICE = re.compile(r"agentbox-(proxy|gh)-(" + registry.NAME.pattern + ")")
SERVICE_UNIT = re.compile(r"agentbox-(proxy-[^@/]+|ghvm-[^@/]+@[^/]*)\.service")
PASSWD, GROUP = paths.host("/etc/passwd"), paths.host("/etc/group")

_users = (None, {})
_untrusted = 0
_untrusted_lock = threading.Lock()


def _uid(user):
    """The uid of a service user as journald writes _UID, None without one. From
    /etc/passwd, where useradd --system put them (tests: under AGENTBOX_ROOT, not
    the Host's users), read again when it changed (watch runs for long)."""
    global _users
    try:
        st = os.stat(PASSWD)
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        if stamp != _users[0]:
            users = {}
            with open(PASSWD, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) > 2 and re.fullmatch(r"[0-9]{1,10}", parts[2]):
                        users.setdefault(parts[0], str(int(parts[2])))
            _users = (stamp, users)
    except OSError:
        return None
    return _users[1].get(user)


def _uids(*users):
    """journalctl matches for entries of these users; a user that does not exist
    writes nothing. Values of one field are alternatives."""
    return [f"_UID={u}" for u in (_uid(n) for n in users) if u is not None]


def group_exists(name):
    """Whether /etc/group (tests: under AGENTBOX_ROOT) has this group."""
    try:
        with open(GROUP, encoding="utf-8", errors="replace") as f:
            return any(line.split(":", 1)[0] == name for line in f)
    except OSError:
        return False


def trusted(e):
    """Whether journald says the entry comes from a writer that may write it
    (above). Entries of any other identifier never do."""
    ident, uid, unit = e.get("SYSLOG_IDENTIFIER"), e.get("_UID"), e.get("_SYSTEMD_UNIT")
    if not isinstance(ident, str) or not isinstance(uid, str):
        return False
    if ident == IDENTIFIER:
        kind, action = e.get("AGENTBOX_KIND"), e.get("AGENTBOX_ACTION")
        if uid == _uid(CONTROL_ROOM[0]):
            return (unit == CONTROL_ROOM[1] and kind == "signin" and isinstance(action, str)
                    and action in SIGN_INS and "AGENTBOX_VM" not in e)
        # never the services, by user and (should /etc/passwd not tell) by unit
        if uid in (_uid(PROXY_USER), _uid(TOKEN_USER)) or isinstance(unit, str) and SERVICE_UNIT.fullmatch(unit):
            return False
        return isinstance(kind, str) and kind in CLI_KINDS
    m = SERVICE.fullmatch(ident)
    if not m or not isinstance(unit, str):
        return False
    service, vm = m.groups()
    if service == "proxy":
        return uid == _uid(PROXY_USER) and unit == f"agentbox-proxy-{vm}.service"
    return uid == _uid(TOKEN_USER) and unit.startswith(f"agentbox-ghvm-{vm}@") and unit.endswith(".service")


def count_untrusted(n=1):
    global _untrusted
    with _untrusted_lock:
        _untrusted += n


def untrusted_warning():
    """How many entries were left out as untrusted since the last call, as one
    warning that says nothing they said; None if none were."""
    global _untrusted
    with _untrusted_lock:
        n, _untrusted = _untrusted, 0
    return f"journal entries without a known writer left out: {n}" if n else None


# --- reading -------------------------------------------------------------------------

UNITS = {"s": 1, "min": 60, "h": 3600, "d": 86400, "w": 604800}
CHECKS = {
    "kind": lambda v: all(k in KINDS for k in v.split(",")),
    "vm": registry.NAME.fullmatch,
    "since": TIME.fullmatch,
    "until": TIME.fullmatch,
    "search": lambda v: 0 < len(v) <= 200 and clean(v, False) == v,
    "lines": lambda v: re.fullmatch(r"\d{1,4}", v) and 1 <= int(v) <= 5000,
}
# Alerts as the proxy logs them (lines with the older German VERDACHT are ignored),
# and the token service's lines (github-app/agentbox-gh-tokend; its older German lines
# are ignored as well)
ALERT = re.compile(r"\[[a-z0-9-]+\] ALERT (\S+) \[([^\]]*)\]")
TOKEN = re.compile(r"\[[a-z0-9-]+\] (token for|refused:|error:) (.*)")
GITHUB = {"token for": ("github.token", "ok"), "refused:": ("github.refused", "failed"),
          "error:": ("github.error", "failed")}
# the parts of a token service line: "<owner>/<repo> until <expiry>[ by App <app-id>]" (without the
# App before there were several), or "<request as repr()>: <rest>",
# the rest of a refusal "<reason>[ <name>=<value>...]: <text>"
ISSUED = re.compile(r"(\S+) until (\S+?)(?: by App ([1-9][0-9]{0,11}))?")
REFUSAL = re.compile(r"([a-z][a-z-]{0,31})((?: [a-z][a-z0-9_]{0,27}=[A-Za-z0-9._/-]{1,64}){0,8}): (.*)")
TOKEN_PARAMS = {"request", "repository", "reason", "expires", "message"}        # the entry's own
REQUEST = re.compile(r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"): (.*)""")
REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")      # what the token service accepts


def to_epoch(v, now):
    """A --since/--until value (guest.TIME) as epoch seconds, Host local time."""
    if v == "now":
        return now
    if v in ("today", "yesterday"):
        day = datetime.date.fromtimestamp(now) - datetime.timedelta(days=1 if v == "yesterday" else 0)
        return datetime.datetime.combine(day, datetime.time()).timestamp()
    m = re.fullmatch(r"-(\d{1,6})(s|min|h|d|w)", v)
    if m:
        return now - int(m.group(1)) * UNITS[m.group(2)]
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}(?::\d{2})?))?", v)
    t = m.group(2) or "00:00"
    fmt = "%Y-%m-%d %H:%M:%S" if t.count(":") == 2 else "%Y-%m-%d %H:%M"
    try:
        return datetime.datetime.strptime(f"{m.group(1)} {t}", fmt).timestamp()
    except ValueError:
        die(f"invalid time: {v}", 2, error="invalid_argument", value=v)


def _options(args):
    opts, as_json, i = {}, False, 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--json":
            as_json = True
            continue
        name, eq, val = a.partition("=")
        if not name.startswith("--") or name[2:] not in CHECKS:
            die("unknown option: " + clean(a, False)[:60], 2, error="invalid_argument",
                option=clean(name, False)[:60])
        if not eq:
            if i >= len(args):
                die(f"{name} needs a value", 2, error="invalid_argument", option=name)
            val, i = args[i], i + 1
        if not CHECKS[name[2:]](val):
            die(f"invalid value for {name}: " + clean(val, False)[:60], 2, error="invalid_argument", option=name)
        opts[name[2:]] = val
    return opts, as_json


def _journal(window, identifiers, writers, matches=(), grep=None, cap=None, namespace=f"+{NAMESPACE}",
             prefix=None):
    """Entries in the time window, newest first, from the Activity namespace and (by
    default, "+") the default journal: of these identifiers (None: any starting with
    `prefix`) and written by these users (None: anyone the socket let write), so
    nobody else's entries are even read; of those only the ones their writer may
    write (trusted()), the others are counted."""
    uids = None if writers is None else _uids(*writers)
    # nothing to read (no such identifier, no such user), and never every entry on the Host
    if identifiers == [] or uids == [] or identifiers is None and uids is None:
        return []
    since, until = window
    argv = ["journalctl", f"--namespace={namespace}", "--output=json", "--no-pager", "--reverse",
            f"--since=@{int(since)}", *([f"--until=@{int(until)}"] if until is not None else []),
            *[f"--identifier={i}" for i in identifiers or ()], *([f"--grep={grep}"] if grep else []),
            *([f"--lines={cap}"] if cap else []), *matches, *(uids or ())]
    out = _journalctl(argv)
    read = [e for e in map(_json, out.splitlines()) if isinstance(e, dict)]
    # read by user (identifiers None): another family's identifier is its own reader's
    entries = [e for e in read if not (prefix and isinstance(e.get("SYSLOG_IDENTIFIER"), str)
                                       and not e["SYSLOG_IDENTIFIER"].startswith(prefix))]
    kept = [e for e in entries if trusted(e)]
    if cap and len(read) >= cap and len(kept) < len(read):
        # entries left out must not push real ones out of the newest `cap`
        return _journal(window, identifiers, writers, matches, grep, None, namespace, prefix)
    count_untrusted(len(entries) - len(kept))
    return kept


def _json(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def _journalctl(argv):
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except OSError:
        die("journalctl is missing", 127, error="journal_failed")
    except subprocess.TimeoutExpired:
        die("journalctl does not answer, narrow the filters", 5, error="journal_failed")
    out = r.stdout.decode("utf-8", "replace")
    # --grep without a match ends with 1, without output or message
    if r.returncode != 0 and not (r.returncode == 1 and not out.strip() and not r.stderr.strip()):
        die("journalctl failed: " + (clean(r.stderr.decode("utf-8", "replace").strip(), False)[:300]
                                     or f"Exit {r.returncode}"), error="journal_failed")
    return out


def _text(e, key, limit=MAX_DESCRIPTION):
    v = field(e, key, limit=limit)
    return redact(v) if v is not None else None


def _base(e):
    try:
        usec = int(e["__REALTIME_TIMESTAMP"])
        when = datetime.datetime.fromtimestamp(usec / 1e6, datetime.timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    return {"time": when.isoformat(timespec="microseconds").replace("+00:00", "Z"), "_usec": usec}


def _recorded(window, kinds, vm, cap):
    """Entries recorded with record(), by agentbox and the Control Room. Only
    the namespace counts, not entries under the same identifier in the Host's
    default journal (written before the namespace existed, or by tests)."""
    matches = [f"AGENTBOX_KIND={k}" for k in kinds or ()] + ([f"AGENTBOX_VM={vm}"] if vm else [])
    for e in _journal(window, [IDENTIFIER], None, matches, cap=cap, namespace=NAMESPACE):
        entry = recorded_entry(e)
        if entry:
            yield entry


def _params(e, prefix):
    """The parameters recorded as AGENTBOX_<prefix><NAME>, as {name: text}."""
    prefix = f"AGENTBOX_{prefix}"
    out = {}
    for key in sorted(k for k in e if isinstance(k, str) and k.startswith(prefix)):
        name, value = key[len(prefix):].lower(), _text(e, key)
        if PARAM_NAME.fullmatch(name) and value is not None and len(out) < 32:
            out[name] = value
    return out


def recorded_entry(e):
    """A journal record written by record() as a timeline entry, or None. One recorded
    with a code (AGENTBOX_CODE, the action) also has its code and parameters, and a failure
    its error code and that error's parameters; older ones have only their description."""
    base, kind = _base(e), _text(e, "AGENTBOX_KIND", 32)
    if base is None or kind is None:
        return None
    action, outcome = _text(e, "AGENTBOX_ACTION", 64) or kind, _text(e, "AGENTBOX_OUTCOME", 16)
    entry = {**base, "vm": _text(e, "AGENTBOX_VM", 32), "actor": _text(e, "AGENTBOX_ACTOR", MAX_ACTOR),
             "kind": kind, "action": action, "outcome": "failed" if outcome == "failed" else "ok",
             "description": _text(e, "AGENTBOX_DESCRIPTION") or ""}
    if _text(e, "AGENTBOX_CODE", 64) == action:
        entry.update(code=action, params=_params(e, PARAM))
        error = _text(e, "AGENTBOX_ERROR", 64)
        if entry["outcome"] == "failed" and error and ERROR_CODE.fullmatch(error):
            entry.update(error=error, error_params=_params(e, ERROR_PARAM))
    # a stable code for why (automatic Resets: idle, gui-connected, agents-working, ...),
    # so the Control Room can word it; only where one was recorded (before #69 a field of its own)
    reason = entry.get("params", {}).get("reason") or _text(e, "AGENTBOX_REASON", 32)
    if reason and re.fullmatch(r"[a-z][a-z-]{0,31}", reason):
        entry["reason"] = reason
    return entry


def _per_vm(e, prefix):
    ident = field(e, "SYSLOG_IDENTIFIER") or ""
    return ident[len(prefix) + 1:] if ident.startswith(prefix + "-") else None


def _alerts(window, kinds, vm, cap):
    """Alerts from the proxy log (agentbox-proxy-<vm>): of one VM, or of every VM
    that ever logged, deleted ones included."""
    for e in _journal(window, [f"agentbox-proxy-{vm}"] if vm else None, (PROXY_USER,),
                      grep=r"^\[[a-z0-9-]+\] ALERT ", cap=cap, prefix="agentbox-proxy-"):
        entry = alert_entry(e)
        if entry:
            yield entry


def alert_entry(e):
    """A proxy line with an Alert as a timeline entry, with the code `alert` and the
    destination and its Signals as parameters (also as _domain and _signals), or None."""
    m, base, name = ALERT.fullmatch(_text(e, "MESSAGE") or ""), _base(e), _per_vm(e, "agentbox-proxy")
    if not (m and base and name):
        return None
    signals = [s for s in m.group(2).replace(",", " ").split() if s]
    return {**base, "vm": name, "actor": f"VM {name}", "kind": "alert", "action": "alert", "outcome": "ok",
            "description": f"Alert: {m.group(1)} ({m.group(2)})", "code": "alert",
            "params": {"domain": m.group(1), **({"signals": ", ".join(signals)} if signals else {})},
            "_domain": m.group(1), "_signals": signals}


def _token_request(action, rest):
    """What a token service line says, as entry fields that the timeline does not
    show (leading "_"), for `agentbox github-tokens`: the repository (owner/repo)
    if the request named one, the request as the VM sent it, why it was refused (the
    English text, and its stable code in _code), and when an issued token expires.
    `rest` is the line after its keyword, as logged: split first, then every part is
    cleaned like any description."""
    if action == "github.token":
        m = ISSUED.fullmatch(rest)
        repo = m.group(1) if m and REPOSITORY.fullmatch(m.group(1)) else None
        return {"_repository": repo, "_request": repo, "_reason": None, "_code": None, "_code_params": {},
                "_expires": redact(m.group(2))[:40] if m else None, "_app_id": m.group(3) if m else None}
    m = REQUEST.fullmatch(rest)
    if not m:
        return {"_repository": None, "_request": None, "_reason": redact(rest)[:MAX_DESCRIPTION], "_code": None,
                "_code_params": {}, "_expires": None, "_app_id": None}
    try:
        # the token service logs the request with repr(); a string literal, nothing else
        request = str(ast.literal_eval(m.group(1)))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        request = m.group(1)[1:-1]
    request = redact(request)[:300]
    why, code, params = m.group(2), None, {}
    refusal = REFUSAL.fullmatch(why) if action == "github.refused" else None
    if refusal:
        code, named, why = refusal.groups()
        for pair in named.split():
            name, _, value = pair.partition("=")
            if name not in TOKEN_PARAMS:
                params[name] = redact(value)
    return {"_repository": request if REPOSITORY.fullmatch(request) else None, "_request": request,
            "_reason": redact(why)[:MAX_DESCRIPTION], "_code": code, "_code_params": params, "_expires": None,
            "_app_id": None}


def _token_entry(action, fields):
    """The English description and the parameters of a token service line (its code is
    the action): the repository and when the token expires, or the request, the
    repository it names, and the reason's code and its parameters (refused: `status` of
    github-status) or the error as the token service said it (`message`, English; error).
    Never the token, which is never logged."""
    request, repo, reason = fields["_request"], fields["_repository"], fields["_reason"]
    asked = request if request is not None else "?"
    if action == "github.token":
        params = {"repository": repo, "expires": fields["_expires"], "app_id": fields["_app_id"]}
        text = f"GitHub token for {repo or '?'} until {fields['_expires'] or '?'}" + \
            (f" by App {fields['_app_id']}" if fields["_app_id"] else "")
    elif action == "github.refused":
        params = {"request": request, "repository": repo, "reason": fields["_code"], **fields["_code_params"]}
        text = f"GitHub token for {asked} refused: {reason}"
    else:
        params = {"request": request, "repository": repo, "message": reason}
        text = f"GitHub token for {asked} failed: {reason}"
    return text, {k: v for k, v in params.items() if v}


def _github(window, kinds, vm, cap):
    """GitHub tokens issued and refused (agentbox-gh-<vm>), of one VM or every VM; the
    token itself is never logged."""
    for e in _journal(window, [f"agentbox-gh-{vm}"] if vm else None, (TOKEN_USER,), cap=cap, prefix="agentbox-gh-"):
        entry = github_entry(e)
        if entry:
            yield entry


def github_entry(e):
    """A token service line as a timeline entry with the code `github.token`, `github.refused`
    or `github.error` (its action) and its parameters (_token_entry), plus the fields of
    _token_request; or None."""
    m, base, name = TOKEN.fullmatch(_text(e, "MESSAGE") or ""), _base(e), _per_vm(e, "agentbox-gh")
    if not (m and base and name):
        return None
    action, outcome = GITHUB[m.group(1)]
    raw = TOKEN.fullmatch(message_text(e.get("MESSAGE")) or "")
    fields = _token_request(action, (raw or m).group(2))
    text, params = _token_entry(action, fields)
    entry = {**base, "vm": name, "actor": f"VM {name}", "kind": "github", "action": action, "outcome": outcome,
             "description": redact(text)[:MAX_DESCRIPTION], "code": action, "params": params, **fields}
    if params.get("reason"):
        entry["reason"] = params["reason"]
    return entry


# Where the timeline comes from: (kinds a source can contain, None for any; reader).
# A reader gets ((since, until), kinds or None, vm or None, cap or None) and yields
# entries. Builds record their outcome with record() (images.py).
SOURCES = [(None, _recorded), ({"alert"}, _alerts), ({"github"}, _github)]


def timeline(opts, now=None):
    """(entries newest first, more than --lines matched, retention in days)."""
    now = time.time() if now is None else now
    days = retention_days()
    since = now - days * 86400
    if "since" in opts:
        since = max(since, to_epoch(opts["since"], now))
    until = to_epoch(opts["until"], now) if "until" in opts else None
    kinds = opts["kind"].split(",") if "kind" in opts else None
    vm, search, limit = opts.get("vm"), opts.get("search", "").casefold(), int(opts.get("lines", 200))
    # without a text search journalctl may stop after limit + 1 entries per source
    cap = None if search else limit + 1
    entries = []
    for source_kinds, read in SOURCES:
        if kinds and source_kinds is not None and not source_kinds & set(kinds):
            continue
        # sources narrow their journalctl query as far as they can; every filter
        # still applies here to every entry, whatever a source returned
        for e in read((since, until), kinds, vm, cap):
            if e["_usec"] < since * 1e6 or until is not None and e["_usec"] > until * 1e6:
                continue
            if kinds and e["kind"] not in kinds or vm and e["vm"] != vm:
                continue
            if search and search not in " ".join(str(e[k] or "") for k in
                                                 ("vm", "actor", "kind", "action", "description")).casefold():
                continue
            entries.append(e)
    entries.sort(key=lambda e: -e["_usec"])
    return entries[:limit], len(entries) > limit, days


def activity(args):
    """agentbox activity [--kind K[,K]] [--vm VM] [--since T] [--until T] [--search TEXT]
    [--lines N] [--json]: the Activity timeline, newest first."""
    opts, as_json = _options(args)
    entries, truncated, days = timeline(opts)
    if as_json:
        print(json.dumps({"entries": [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries],
                          "truncated": truncated, "retention_days": days}, ensure_ascii=False))
        return
    for e in entries:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["_usec"] / 1e6))
        failed = "  [failed]" if e["outcome"] == "failed" else ""
        print(f"{when}  {e['vm'] or '-':<16} {e['kind']:<9}  {e['description']}  by {e['actor'] or '?'}{failed}")
    if truncated:
        print("agentbox: there are more entries (raise --lines or narrow the filters)", file=sys.stderr)


COMMANDS = {"activity": activity}
