"""A command's arguments: positional arguments and options, the same rules for every command.

`--name value` or `--name=value` for an option that takes a value, `--name` for a flag. A
value is taken as given, also one that starts with `--` (`--note --json` is the note
"--json"), and an option is never guessed from its start (`--forc` is no `--force`:
cli.check_control_room looks for `--force` as written). Every failure ends the command with
error invalid_argument (ADR 0012), the option's name without its value as `option`, and the
exit code the command chose.

The global options (`--actor`, and `--json` for the failure's form) are taken by cli.py.
"""
from .errors import die
from .sanitize import clean

MANY = "many"


class Parsed:
    """The raw arguments and the one result of parsing them for a command."""

    def __init__(self, raw, positional, values):
        self.raw = tuple(raw)
        self.positional = tuple(positional)
        self.values = dict(values)

    def __iter__(self):
        return iter(self.raw)

    def __len__(self):
        return len(self.raw)

    def __getitem__(self, index):
        return self.raw[index]


def parse(args, known, *, exit=2, positional=True, prefix="", allow_unknown=False):
    """(positional arguments, options). known: option name (without --) -> False for a flag
    (True when given), True for one value, MANY for a value that may repeat (a list of them),
    or a function that checks the one value (true when it is valid). positional=False: a
    positional argument is an unknown option. prefix: put before every message."""
    if isinstance(args, Parsed):
        return list(args.positional), dict(args.values)

    found, opts, i = [], {}, 0

    def fail(message, name):
        die(prefix + message, exit, error="invalid_argument", option="--" + clean(name, False)[:60])

    def stray(a):
        die(prefix + "unknown option: " + clean(a, False)[:60], exit, error="invalid_argument",
            option=clean(a, False)[:60])

    while i < len(args):
        a = args[i]
        i += 1
        if not a.startswith("--"):
            if not positional:
                stray(a)
            found.append(a)
            continue
        name, eq, value = a[2:].partition("=")
        if name not in known:
            if allow_unknown and positional:
                found.append(a)
                continue
            fail("unknown option: " + clean(a, False)[:60], name)
        kind = known[name]
        if kind is False:
            if eq:
                fail(f"--{name} takes no value", name)
            opts[name] = True
            continue
        if not eq:
            if i >= len(args):
                fail(f"--{name} needs a value", name)
            value, i = args[i], i + 1
        if callable(kind) and not kind(value):
            fail(f"invalid value for --{name}: " + clean(value, False)[:60], name)
        if kind == MANY:
            opts.setdefault(name, []).append(value)
        elif name in opts:
            fail(f"--{name} given twice", name)
        else:
            opts[name] = value
    return found, opts
