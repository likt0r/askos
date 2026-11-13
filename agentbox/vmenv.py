"""VM variables (#29): environment variables of one VM, for `orca serve`, the agents and
terminals it starts, and login shells.

They live in the VM's record (`"env": {"NAME": "value"}`, registry.py) and reach the VM
at boot like its name, Claude token and OpenCode key: `agentbox regenerate` (and `set-env`) write them
into a file that QEMU passes as the fw_cfg entry opt/agentbox/env, one line
`NAME=<base64 of the value>` per variable, or `none` without any (QEMU refuses empty
fw_cfg files). Values never go into virt-install's --sysinfo string, which it splits at
commas. In the VM, /usr/lib/agentbox/fwcfg-env writes them into /run/agentbox/env and
/run/agentbox/agent-env, before the fixed values of Agent Box.

The values are not secret. What matters is that no VM variable switches a Safeguard off:
a variable set at runtime never passes through the safeguard check of the Builds (ADR
0008), and /run/agentbox/env overrides systemd's DefaultEnvironment for `orca serve`. So
the Host refuses every name a Safeguard or Agent Box itself reads: the patterns of the
safeguard check (safeguards.OVERRIDES, safeguards.MENTIONS) except its proxy names, the
names Agent Box sets for the VM (RESERVED, RESERVED_PREFIXES) and every variable the
Image's systemd DefaultEnvironment sets (DEFAULT_ENVIRONMENT; a test holds it to the files
of the Image).
Broad prefixes such as npm_config_* or UV_* stay allowed. A record keeps a variable whose
name a later release refuses (a broken record would stop regenerate and create for every VM):
it is never passed on (fw_cfg leaves it out, refused() names it), and only unset-env can
still name it.

Everything here works on a plain {name: value} dict, so another list of variables (secrets
per VM) can use the same name check, limits and file format.
"""
import base64
import re
import unicodedata

from . import safeguards
from .errors import Fail, die
from .sanitize import clean

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAX_VARIABLES = 64
MAX_NAME = 64
MAX_VALUE = 4096                     # bytes of the value in UTF-8
# Set by the unit of `orca serve` (HOME, XDG_RUNTIME_DIR: its Environment=, which the
# EnvironmentFile overrides), by guest-setup (the fixed values of /run/agentbox/env and
# agent-env), read by every process or shell before anything else, or run as code by shells.
RESERVED = frozenset({"PATH", "HOME", "XDG_RUNTIME_DIR", "NODE_OPTIONS", "BASH_ENV", "ENV",
                      "CLAUDE_CODE_OAUTH_TOKEN", "ENABLE_CLAUDEAI_MCP_SERVERS", "OPENCODE_API_KEY", "ORCA_PORT",
                      "PAIRING_ADDRESS"})
RESERVED_PREFIXES = ("LD_", "AGENTBOX_", "GIT_CONFIG_")
# What the patterns of the safeguard check miss, in either case (npm reads its variables so):
# npm going around the minimum release age (before)
RESERVED_ANY_CASE = frozenset({"npm_config_before"})
# The proxy names of safeguards.MENTIONS (HTTPS_PROXY, no_proxy, ...). The Build check refuses
# them in an Image, which carries no proxy variables; a VM variable may have one: the proxy is
# transparent and the VM reaches nothing but the Host, so no proxy variable switches a
# Safeguard off. reserved() blanks them out of a name and checks the rest with MENTIONS.
PROXY_MENTIONS = re.compile(r"_proxy\b|proxy=|" + safeguards.PROXY_VALUE.pattern, re.IGNORECASE)
# Set by bash itself: its readonly variables (`readonly -p` of a bare bash; export fails on
# them in every login shell that sources agent-env), GROUPS, which ignores assignments, and
# IFS, which would change the word splitting of every profile.d script after agent-env.
# Not a Safeguard: a variable that never arrives.
SHELL = frozenset({"SHELLOPTS", "BASHOPTS", "UID", "EUID", "PPID", "GROUPS", "BASH_VERSINFO", "IFS"})
# Every variable of a DefaultEnvironment in the Image: vm/files/usr/lib/systemd/system.conf.d/agentbox.conf
# and the agentbox-packages.conf that package-safeguards writes (tests/test_vm_env.py checks both).
DEFAULT_ENVIRONMENT = frozenset({
    "PATH", "AGENTBOX_GH_ADDR", "DO_NOT_TRACK", "ORCA_TELEMETRY_DISABLED", "DISABLE_AUTOUPDATER",
    "ENABLE_CLAUDEAI_MCP_SERVERS", "DOCKER_HOST", "RUSTUP_HOME", "JAVA_HOME",
    "UV_EXCLUDE_NEWER", "pnpm_config_minimum_release_age"})


def _shown(name):
    return clean(name, False)[:MAX_NAME + 8]


def reserved(name):
    """Whether a variable of this name could switch a Safeguard off or change what Agent Box
    or bash itself sets: the patterns of the safeguard check without its proxy names, the
    reserved names and prefixes, the Image's DefaultEnvironment and the variables of bash. The
    patterns and RESERVED_ANY_CASE ignore case (npm_config_ignore_scripts counts as
    NPM_CONFIG_IGNORE_SCRIPTS)."""
    return bool(safeguards.OVERRIDES.fullmatch(name) or safeguards.MENTIONS.search(PROXY_MENTIONS.sub(" ", name))
                or name in RESERVED or name.startswith(RESERVED_PREFIXES) or name.lower() in RESERVED_ANY_CASE
                or name in DEFAULT_ENVIRONMENT or name in SHELL)


def valid_name(name):
    """The name if it has the form of one (removing a variable needs no more); dies otherwise."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or len(name) > MAX_NAME:
        die(f"invalid name of a VM variable: {_shown(name) if isinstance(name, str) else '?'} "
            f"(up to {MAX_NAME} characters from A-Z, a-z, 0-9 and _, not starting with a digit)",
            error="invalid_env_name")
    return name


def check_name(name):
    """The name if a VM variable may have it; dies otherwise (invalid_env_name, env_name_reserved)."""
    valid_name(name)
    if reserved(name):
        why = "bash sets it itself" if name in SHELL else "a Safeguard or Agent Box itself sets or reads it"
        die(f"{name} cannot be a VM variable: {why}", error="env_name_reserved", name=name)
    return name


def check_value(name, value):
    """The value if it may be one: valid UTF-8 text of at most MAX_VALUE bytes without control
    characters other than tab (so no NUL, newline or carriage return); dies otherwise
    (invalid_env_value, env_too_large)."""
    if not isinstance(value, str):
        die(f"the value of {name} is no text", error="invalid_env_value", name=name)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        die(f"the value of {name} is not valid UTF-8", error="invalid_env_value", name=name)
    if any(c != "\t" and unicodedata.category(c) == "Cc" for c in value):
        die(f"the value of {name} contains a control character (a line break, NUL, ...); only tabs are allowed",
            error="invalid_env_value", name=name)
    if size > MAX_VALUE:
        die(f"the value of {name} is {size} bytes long, at most {MAX_VALUE} are allowed", error="env_too_large",
            name=name, max_bytes=MAX_VALUE)
    return value


def check(env, kept=()):
    """The variables if every name and value may be one and there are at most MAX_VARIABLES;
    dies at the first that may not, so a caller changes nothing. Names in kept (a record has
    them already) may be refused ones: they stay, but are never passed on."""
    if not isinstance(env, dict):
        die("the VM variables are no list of names and values", error="invalid_argument")
    for name, value in env.items():
        check_value(valid_name(name) if name in kept else check_name(name), value)
    if len(env) > MAX_VARIABLES:
        die(f"{len(env)} VM variables, at most {MAX_VARIABLES} are allowed", error="env_too_large",
            max_variables=MAX_VARIABLES)
    return env


def refused(env):
    """The names of these variables that a VM variable may not have (any more): never passed on."""
    return [name for name in env if reserved(name)]


def problem(env):
    """What is wrong with the variables of a record, or None (registry.problem): their form
    and the limits. A refused name is no problem of the record (refused())."""
    try:
        check(env, kept=env if isinstance(env, dict) else ())
    except Fail as f:
        return f.message
    return None


def pairs(args):
    """NAME=VALUE arguments as {name: value}, each checked; a name given twice is refused."""
    env = {}
    for a in args:
        name, eq, value = a.partition("=")
        if not eq:
            die(f"expected NAME=VALUE, not {_shown(a)}", error="invalid_argument")
        check_value(check_name(name), value)
        if name in env:
            die(f"{name} given twice", error="invalid_argument", name=name)
        env[name] = value
    return env


def fw_cfg(env):
    """The content of the fw_cfg file of these variables: one line NAME=<base64(value)> each,
    in their order, or "none" without any (QEMU refuses empty fw_cfg files). A refused name
    never goes in."""
    passed = {name: value for name, value in env.items() if not reserved(name)}
    if not passed:
        return b"none\n"
    return b"".join(name.encode() + b"=" + base64.b64encode(value.encode("utf-8")) + b"\n"
                    for name, value in passed.items())
