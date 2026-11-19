"""The safeguard check after every Build (#17): reads the built Image's files from the
Host and never runs anything from the Image, so an Image cannot fake its own result.

Every Build, of the Base image or a derived one, must carry the safeguards the Base
image sets up (vm/Containerfile, vm/files, package-safeguards), with the values this
module expects, not the ones the repository happens to contain:

- npm: its global configuration (/etc/npmrc, named by npm's builtin configuration; without
  it npm would look for /usr/etc/npmrc) ignores install scripts and takes only versions at
  least package_min_age_days old; npm 12 or later, older versions ignore min-release-age
  without a word.
- pnpm, uv: the same minimum age through the environment of every service in the VM
  (systemd's DefaultEnvironment, all drop-ins merged as systemd does) and uv.toml.
- Bun: bunfig.toml with the same minimum age, put into the agent's home by guest-setup.
- Git: hooks only from /usr/lib/agentbox/hooks, whose pre-push hook refuses main and master.
- Network: guest-setup makes the Host's address on the VM's network the only resolver
  (NetworkManager keeps out of /etc/resolv.conf, dns=none; systemd-resolved's configuration
  names no DNS or FallbackDNS servers), and no service carries a proxy variable or option:
  every connection goes to the Host, where the proxy reads its destination.
- Guest agent: four of the wrappers the Host runs through it (agentbox-journal, agentbox-devices,
  agentbox-inbox, which unpacks `agentbox import` as the agent with tarfile's "data" filter,
  and agentbox-container-images, which removes Container images as the agent, with `podman
  rmi` only and never by force).
- Claude Code: fetches none of the owner's claude.ai connectors (#8). ENABLE_CLAUDEAI_MCP_SERVERS
  is false in the environment of every service, and /etc/profile.d/agentbox.sh exports it
  for shells. The Claude token cannot fetch connectors; this holds after a /login in the VM.
- sshd (#127): SSH only by key and only as agent. Its configuration is read from the files as
  sshd reads them (the main file, each Include where it stands, the first value winning, no
  Match block, only files in /etc and /usr) and must have the values of SSHD, AllowUsers only
  agent and host keys only under /run/agentbox/ssh; sshd.service and sshd.socket are not
  enabled, the presets disable both at first boot, sshd@.service and systemd's ssh generator
  are masked, and /etc/sysconfig/sshd adds no OPTIONS. `sshd -T` would run the Image. By ADR
  0019 this catches mistakes, not an Image crafted to get past it.
- Nothing in that environment switches a safeguard off again (npm_config_ignore_scripts,
  GIT_CONFIG_*, ...).

A derived Image is also compared with the Base Build it was built on (its manifest):
it must be built on that Build's layers, every safeguard file must be identical, the
Git hooks and guest-agent wrappers unchanged, and a file it adds or changes where
environment or configuration is read must not mention a safeguard at all (Claude Code's
JSON settings read as Claude Code reads them, escapes decoded; one that does not parse fails).

Paths inside the Image are resolved within its root: a symbolic link never leads out
of it (an absolute target counts from the Image's root).
"""
import fnmatch
import hashlib
import json
import os
import re
import shlex
import stat

HOOKS = "/usr/lib/agentbox/hooks"
BUNFIG = "/usr/lib/agentbox/bunfig.toml"
GUEST_SETUP = "/usr/lib/agentbox/guest-setup"
FWCFG_ENV = "/usr/lib/agentbox/fwcfg-env"     # guest-setup writes the VM variables with it
HOME_PATHS = "/usr/lib/agentbox/home-paths"   # guest-setup follows no link in the agent's home with it (#142)
FWCFG_SSH = "/usr/lib/agentbox/fwcfg-ssh"     # guest-setup writes the SSH keys and host key with it (ADR 0020)
NPM = "/usr/lib/node_modules/npm"
NPM_DEFAULT = "/usr/etc/npmrc"        # npm with prefix /usr reads this unless its builtin config names another
QGA = "/usr/libexec/qemu-ga/fsfreeze-hook.d"
JOURNAL_WRAPPER = f"{QGA}/agentbox-journal"
DEVICES_WRAPPER = f"{QGA}/agentbox-devices"
INBOX_WRAPPER = f"{QGA}/agentbox-inbox"
# what agentbox-inbox must do: drop root before reading the tar file, unpack with the "data" filter
INBOX_MUST = ("become(USER)", 'filter="data"', "os.setuid(pw.pw_uid)")
CONTAINER_IMAGES_WRAPPER = f"{QGA}/agentbox-container-images"
# what agentbox-container-images must do: drop root first, remove with podman rmi; and what it
# must not: force a removal, or touch containers, volumes or the whole storage
CONTAINER_IMAGES_MUST = ("become(USER)", "os.setuid(pw.pw_uid)", '"rmi"')
CONTAINER_IMAGES_NEVER = ("--force", '"-f"', '"rm"', '"volume"', '"system"', '"image", "prune"',
                          '"container", "prune"')
NM_CONF = "/etc/NetworkManager/conf.d/agentbox.conf"
RESOLVER = 'echo "nameserver $gw" > /etc/resolv.conf'   # guest-setup: the Host on the VM's network
PROFILE = "/etc/profile.d/agentbox.sh"
# systemd-resolved's configuration: the main file and its drop-in directories, lowest first
RESOLVED_MAIN = ("/etc/systemd/resolved.conf", "/usr/local/lib/systemd/resolved.conf", "/usr/lib/systemd/resolved.conf")
RESOLVED_DROPINS = ("/usr/lib/systemd/resolved.conf.d", "/usr/local/lib/systemd/resolved.conf.d",
                    "/etc/systemd/resolved.conf.d")
# sshd (#127): its configuration as sshd reads it, from the main file and what it includes
SSHD_CONFIG = "/etc/ssh/sshd_config"
SSHD_ETC = "/etc/ssh"                 # where a relative Include points
SSHD_DROPIN = "/etc/ssh/sshd_config.d/10-agentbox.conf"
SSHD_PRESET = "/usr/lib/systemd/system-preset/10-agentbox.preset"
SSHD_SYSCONFIG = "/etc/sysconfig/sshd"    # OPTIONS: added to sshd's command line by its units
SSHD_RUN = "/run/agentbox/ssh"         # root-owned, written at boot: the SSH keys and the host key
SSHD_INCLUDE_DEPTH = 16                # sshd's own limit (READCONF_MAX_DEPTH)
# where sshd's configuration may come from: directories the agent cannot write at runtime
SSHD_INCLUDE_ROOTS = ("/etc/", "/usr/")
# The value sshd must take for each (keywords lower-cased, as sshd reads them, which keeps the
# first value it reads); compared lower-cased, except the paths and AuthenticationMethods.
SSHD = {"passwordauthentication": "no", "kbdinteractiveauthentication": "no", "pubkeyauthentication": "yes",
        "authenticationmethods": "publickey", "hostbasedauthentication": "no", "permitrootlogin": "no",
        "authorizedkeysfile": f"{SSHD_RUN}/authorized_keys", "authorizedkeyscommand": "none",
        "trustedusercakeys": "none", "allowtcpforwarding": "local", "allowagentforwarding": "no",
        "allowstreamlocalforwarding": "no", "x11forwarding": "no", "permittunnel": "no", "gatewayports": "no",
        "permituserenvironment": "no"}
SSHD_PATHS = ("authorizedkeysfile", "authorizedkeyscommand", "trustedusercakeys")
SSHD_CASED = SSHD_PATHS + ("authenticationmethods",)    # sshd reads these values case-sensitively
SSHD_ALIASES = {"challengeresponseauthentication": "kbdinteractiveauthentication",
                "skeyauthentication": "kbdinteractiveauthentication", "dsaauthentication": "pubkeyauthentication",
                "hostdsakey": "hostkey"}
SSHD_NAMES = {n.lower(): n for n in ("PasswordAuthentication", "KbdInteractiveAuthentication", "PubkeyAuthentication",
                                     "AuthenticationMethods", "HostbasedAuthentication", "PermitRootLogin",
                                     "AuthorizedKeysFile", "AuthorizedKeysCommand", "TrustedUserCAKeys",
                                     "AllowTcpForwarding", "AllowAgentForwarding", "AllowStreamLocalForwarding",
                                     "X11Forwarding", "PermitTunnel", "GatewayPorts", "PermitUserEnvironment")}
SSHD_UNITS = ("sshd.service", "sshd.socket")
# masked in the Base image: Fedora's sshd@.service adds ~/.ssh/authorized_keys on its command
# line, and systemd's generator would start it on a local socket
SSHD_MASKS = (("/etc/systemd/system", "sshd@.service"), ("/etc/systemd/system-generators", "systemd-ssh-generator"))
# systemd's unit directories in the Image, highest first (systemd-analyze unit-paths)
UNIT_DIRS = ("/etc/systemd/system.control", "/etc/systemd/system", "/etc/systemd/system.attached",
             "/usr/local/lib/systemd/system", "/usr/lib/systemd/system")
# systemd's preset files, highest first: one of the same name further down is ignored
PRESET_DIRS = ("/etc/systemd/system-preset", "/usr/local/lib/systemd/system-preset", "/usr/lib/systemd/system-preset")
CLAUDEAI = "ENABLE_CLAUDEAI_MCP_SERVERS"
CLAUDE_CODE = "/etc/claude-code/"     # Claude Code's managed settings (JSON) on Linux
MIN_NPM = 12

# The files that make up the safeguards: a derived Image must leave each as the Base has it.
FILES = ("/etc/npmrc", NPM_DEFAULT, f"{NPM}/npmrc", "/etc/uv/uv.toml", "/etc/gitconfig", BUNFIG, GUEST_SETUP, FWCFG_ENV,
         HOME_PATHS, FWCFG_SSH,
         "/usr/lib/agentbox/package-safeguards", "/usr/lib/systemd/system/agentbox-guest-setup.service",
         "/usr/lib/systemd/system.conf.d/agentbox.conf", "/usr/lib/systemd/system.conf.d/agentbox-packages.conf",
         NM_CONF, PROFILE, SSHD_DROPIN, SSHD_PRESET)
# Directories a derived Image must leave exactly as they are.
SEALED = (HOOKS, QGA)
# Where environment and configuration are read: a derived Image may add or change files
# there, but none that mentions a safeguard (MENTIONS).
WATCHED_FILES = ("/etc/environment", "/etc/profile", "/etc/bashrc", "/etc/security/pam_env.conf",
                 "/etc/systemd/system.conf", "/usr/lib/systemd/system.conf", "/etc/systemd/user.conf",
                 "/etc/NetworkManager/NetworkManager.conf", "/etc/xdg/uv/uv.toml", "/etc/gitconfig",
                 f"{CLAUDE_CODE}managed-settings.json") + RESOLVED_MAIN
WATCHED_DIRS = ("/etc/profile.d", "/etc/environment.d", "/usr/lib/environment.d",
                "/etc/systemd/system.conf.d", "/usr/lib/systemd/system.conf.d", "/usr/local/lib/systemd/system.conf.d",
                "/etc/systemd/user.conf.d", "/usr/lib/systemd/user.conf.d",
                "/etc/NetworkManager/conf.d", "/usr/lib/NetworkManager/conf.d") + RESOLVED_DROPINS + (
                f"{CLAUDE_CODE}managed-settings.d",)
MENTIONS = re.compile(r"ignore[-_]?scripts|release[-_]?age|exclude[-_]?newer|hooks[-_]?path|_proxy\b|proxy=|"
                      r"proxy(host|port)|use-env-proxy|usesystemproxies|git_config|uv_no_config|"
                      r"\b(fallback)?dns\s*=|claudeai", re.IGNORECASE)
# Variables that would switch a safeguard off again for every service of the VM
OVERRIDES = re.compile(r"(npm_config_(ignore_scripts|min_release_age)|pnpm_config_minimum_release_age_exclude|"
                       r"git_config_(global|system|nosystem|count|parameters)|uv_no_config|uv_config_file)",
                       re.IGNORECASE)
# Proxy variables (HTTPS_PROXY, no_proxy, NODE_USE_ENV_PROXY, ...), Node's --use-env-proxy and
# Java's proxy properties
PROXY_NAME = re.compile(r"\w*_proxy", re.IGNORECASE)
PROXY_VALUE = re.compile(r"proxy(host|port)|use-env-proxy|usesystemproxies", re.IGNORECASE)
# systemd's main configuration (the first one found) and its drop-in directories, lowest first
SYSTEM_CONF = ("/etc/systemd/system.conf", "/run/systemd/system.conf", "/usr/local/lib/systemd/system.conf",
               "/usr/lib/systemd/system.conf")
SYSTEM_DROPINS = ("/usr/lib/systemd/system.conf.d", "/usr/local/lib/systemd/system.conf.d",
                  "/run/systemd/system.conf.d", "/etc/systemd/system.conf.d")
AGENTS = {"claude-code": "@anthropic-ai/claude-code", "codex": "@openai/codex", "opencode": "opencode-ai",
          "npm": "npm", "pnpm": "pnpm", "bun": "bun"}
VERSION = re.compile(r"[0-9A-Za-z.+-]{1,40}")
MAX_READ = 1 << 20
# files read() refused as larger than MAX_READ during the current check: each fails it, since
# what lies beyond the limit could change what the check sees. check() clears it first and turns
# it into problems after the checks; a read after that (agents(), whose versions never fail a
# Build) is noted but fails nothing.
_too_large = []


# --- reading the Image ------------------------------------------------------------

def resolve(root, path, hops=40):
    """The Host path of `path` inside the Image at `root`, following symbolic links
    within the Image; None when it does not exist or links go round in circles."""
    todo = [p for p in path.split("/") if p]
    done = []
    while todo:
        part = todo.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if done:
                done.pop()
            continue
        real = os.path.join(root, *done, part)
        try:
            st = os.lstat(real)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            hops -= 1
            if hops < 0:
                return None
            target = os.readlink(real)
            if target.startswith("/"):
                done = []
            todo = [p for p in target.split("/") if p] + todo
            continue
        if todo and not stat.S_ISDIR(st.st_mode):
            return None
        done.append(part)
    return os.path.join(root, *done)


def read(root, path):
    """The content of a regular file in the Image, or None; a file larger than MAX_READ is
    not read but noted, and fails the check."""
    real = resolve(root, path)
    if real is None:
        return None
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(MAX_READ + 1)
        if len(data) > MAX_READ:
            _too_large.append(path)
            return None
        return data
    finally:
        os.close(fd)


def text(root, path):
    data = read(root, path)
    return None if data is None else data.decode("utf-8", "replace")


def describe(root, path):
    """What is at `path` in the Image, for the manifest: a file with its hash and mode,
    a directory, something else, or nothing."""
    real = resolve(root, path)
    if real is None:
        return {"type": "missing"}
    try:
        st = os.stat(real, follow_symlinks=False)
    except OSError:
        return {"type": "missing"}
    if stat.S_ISDIR(st.st_mode):
        return {"type": "dir"}
    if not stat.S_ISREG(st.st_mode):
        return {"type": "other"}
    h = hashlib.sha256()
    try:
        with open(real, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return {"type": "other"}
    return {"type": "file", "sha256": h.hexdigest(), "mode": stat.S_IMODE(st.st_mode)}


def listing(root, path):
    """{name: describe()} of every entry of a directory in the Image; {} when there is none."""
    real = resolve(root, path)
    if real is None or not os.path.isdir(real):
        return {}
    try:
        names = sorted(os.listdir(real))
    except OSError:
        return {}
    return {n: describe(root, f"{path}/{n}") for n in names}


def executable(root, path):
    real = resolve(root, path)
    try:
        st = os.stat(real, follow_symlinks=False) if real else None
    except OSError:
        st = None
    return bool(st and stat.S_ISREG(st.st_mode) and st.st_mode & 0o111)


# --- configuration formats -----------------------------------------------------------

def ini(content):
    """Key-value configuration (npmrc, gitconfig, TOML without tables of tables, NetworkManager)
    as {(section, key): value}, sections and keys lower-cased, the last value winning;
    quotes around a value are removed."""
    out, section = {}, ""
    for line in (content or "").splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        m = re.fullmatch(r"\[\s*([^\]\s\"]+)(?:\s+\"[^\"]*\")?\s*\]", line)
        if m:
            section = m.group(1).lower() if '"' not in line else line.lower()
            continue
        key, eq, value = line.partition("=")
        if not eq:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[(section, key.strip().lower())] = value
    return out


def _logical_lines(content):
    """systemd configuration lines with backslash continuations joined."""
    lines, current = [], ""
    for raw in (content or "").splitlines():
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        lines.append(current + stripped)
        current = ""
    if current:
        lines.append(current)
    return lines


def systemd_environment(root):
    """DefaultEnvironment of the VM's service manager: the main configuration, then every
    drop-in in name order (one in /etc replaces one of the same name further down), the
    assignments of each adding to or (empty) clearing the earlier ones."""
    files = []
    for path in SYSTEM_CONF:
        if read(root, path) is not None:
            files.append(path)
            break
    dropins = {}
    for d in SYSTEM_DROPINS:
        for name, info in listing(root, d).items():
            if name.endswith(".conf") and info["type"] == "file":
                dropins[name] = f"{d}/{name}"
    files += [dropins[n] for n in sorted(dropins)]
    env = {}
    for path in files:
        section = ""
        for line in _logical_lines(text(root, path)):
            line = line.strip()
            if not line or line[0] in "#;":
                continue
            if line.startswith("["):
                section = line.strip("[] ")
                continue
            key, eq, value = line.partition("=")
            if section != "Manager" or key.strip() != "DefaultEnvironment" or not eq:
                continue
            if not value.strip():
                env = {}
                continue
            try:
                words = shlex.split(value)
            except ValueError:
                continue
            for w in words:
                name, eq, v = w.partition("=")
                if eq and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    env[name] = v
    return env


def npm_version(root, package):
    """Version of the npm package installed globally in the Image (None: not found)."""
    try:
        info = json.loads(text(root, f"/usr/lib/node_modules/{package}/package.json") or "")
    except ValueError:
        return None
    v = info.get("version") if isinstance(info, dict) else None
    return v if isinstance(v, str) and VERSION.fullmatch(v) else None


def agents(root):
    """Versions of the agents and package managers in the Image (None: not found)."""
    return {name: npm_version(root, package) for name, package in AGENTS.items()}


# --- the checks ----------------------------------------------------------------------

def _npm(root, days):
    problems = []
    builtin = ini(text(root, f"{NPM}/npmrc"))
    config = builtin.get(("", "globalconfig"), NPM_DEFAULT)
    npmrc = text(root, config)
    if npmrc is None:
        return [f"npm: global configuration {config} is missing"]
    values = ini(npmrc)
    if values.get(("", "ignore-scripts")) != "true":
        problems.append(f"npm: ignore-scripts is not true ({config})")
    age = values.get(("", "min-release-age"))
    if age != str(days):
        problems.append(f"npm: min-release-age is {age or 'not set'} instead of {days} ({config})")
    version = npm_version(root, "npm")
    major = re.match(r"\d+", version or "")
    if not major or int(major.group(0)) < MIN_NPM:
        problems.append(f"npm: version {version or 'unknown'}, min-release-age needs npm {MIN_NPM}")
    return problems


def _env_value(env, name, wanted, what):
    v = env.get(name)
    if v != wanted:
        return [f"{what}: {name} is {v if v is not None else 'not set'} instead of {wanted}"]
    return []


def _packages(root, env, days):
    problems = _npm(root, days)
    problems += _env_value(env, "pnpm_config_minimum_release_age", str(days * 1440), "pnpm")
    uv = ini(text(root, "/etc/uv/uv.toml")).get(("", "exclude-newer"))
    if uv != f"{days} days":
        problems.append(f"uv: exclude-newer in /etc/uv/uv.toml is {uv or 'not set'} instead of {days} days")
    problems += _env_value(env, "UV_EXCLUDE_NEWER", f"{days} days", "uv")
    bun = ini(text(root, BUNFIG)).get(("install", "minimumreleaseage"))
    if bun != str(days * 86400):
        problems.append(f"Bun: minimumReleaseAge in {BUNFIG} is {bun or 'not set'} instead of {days * 86400}")
    if BUNFIG not in (text(root, GUEST_SETUP) or ""):
        problems.append(f"Bun: guest-setup does not put {BUNFIG} into the agent's home")
    return problems


def _git(root):
    problems = []
    hooks = ini(text(root, "/etc/gitconfig")).get(("core", "hookspath"))
    if hooks != HOOKS:
        problems.append(f"Git: core.hooksPath in /etc/gitconfig is {hooks or 'not set'} instead of {HOOKS}")
    hook = text(root, f"{HOOKS}/pre-push") or ""
    if not executable(root, f"{HOOKS}/pre-push") or not all(
            ref in hook for ref in ("refs/heads/main", "refs/heads/master", "exit 1")):
        problems.append(f"Git: {HOOKS}/pre-push is missing or does not block pushes to main and master")
    return problems


def _network(root, env):
    """The Host is the VM's only resolver, and nothing sends connections to a proxy."""
    problems = []
    lines = [line.strip() for line in (text(root, GUEST_SETUP) or "").splitlines()]
    named = [line for line in lines if "nameserver" in line and not line.startswith("#")]
    if named != [RESOLVER]:
        problems.append(f"DNS: guest-setup does not make the Host the only resolver ({RESOLVER})")
    if ini(text(root, NM_CONF)).get(("main", "dns")) != "none":
        problems.append(f"DNS: dns=none is missing in {NM_CONF}")
    for path in _resolved_files(root):
        values = ini(text(root, path))
        for key, name in (("dns", "DNS"), ("fallbackdns", "FallbackDNS")):
            if values.get(("resolve", key)):
                problems.append(f"DNS: {path} sets {name}= (the Host is the only resolver)")
    for name in sorted(env):
        if PROXY_NAME.fullmatch(name) or PROXY_VALUE.search(env[name]):
            problems.append(f"Proxy: {name} is set; the VM reaches the outside without proxy variables")
    return problems


def _resolved_files(root):
    """systemd-resolved's configuration files in the Image: every main file and drop-in."""
    files = [p for p in RESOLVED_MAIN if read(root, p) is not None]
    for d in RESOLVED_DROPINS:
        files += [f"{d}/{n}" for n, info in listing(root, d).items() if n.endswith(".conf") and info["type"] == "file"]
    return files


def _claude(root, env):
    """Claude Code fetches no claude.ai connectors: the variable is false for every service,
    and the last line of the shells' profile naming it exports it as false."""
    problems = _env_value(env, CLAUDEAI, "false", "Claude Code")
    lines = [line.strip() for line in (text(root, PROFILE) or "").splitlines()]
    named = [line for line in lines if CLAUDEAI in line and not line.startswith("#")]
    if not named or named[-1] != f"export {CLAUDEAI}=false":
        problems.append(f"Claude Code: {PROFILE} does not export {CLAUDEAI}=false for shells")
    return problems


def _overrides(env):
    return [f"Environment: {name} overrides a Safeguard" for name in sorted(env) if OVERRIDES.fullmatch(name)]


def _guest_agent(root):
    problems = []
    journal = text(root, JOURNAL_WRAPPER) or ""
    if not executable(root, JOURNAL_WRAPPER) or \
            'exec /usr/bin/timeout 20 /usr/bin/journalctl --no-pager "$@"' not in journal:
        problems.append(f"Guest agent: {JOURNAL_WRAPPER} is missing or starts more than journalctl")
    devices = text(root, DEVICES_WRAPPER) or ""
    if not executable(root, DEVICES_WRAPPER) or not devices.startswith("#!/usr/bin/python3"):
        problems.append(f"Guest agent: {DEVICES_WRAPPER} is missing")
    inbox = text(root, INBOX_WRAPPER) or ""
    if not executable(root, INBOX_WRAPPER) or not inbox.startswith("#!/usr/bin/python3") or \
            not all(m in inbox for m in INBOX_MUST):
        problems.append(f"Guest agent: {INBOX_WRAPPER} is missing or does not unpack as the agent with the "
                        f"data filter")
    cleanup = text(root, CONTAINER_IMAGES_WRAPPER) or ""
    if not executable(root, CONTAINER_IMAGES_WRAPPER) or not cleanup.startswith("#!/usr/bin/python3") or \
            not all(m in cleanup for m in CONTAINER_IMAGES_MUST) or \
            any(m in cleanup for m in CONTAINER_IMAGES_NEVER):
        problems.append(f"Guest agent: {CONTAINER_IMAGES_WRAPPER} is missing or does not remove only Container "
                        f"images as the agent, never by force")
    return problems


# --- sshd (#127) ---------------------------------------------------------------------

def sshd_config(root):
    """sshd's configuration in the Image, read as sshd reads it: the main file, each Include
    where it stands (its patterns in name order, relative ones under /etc/ssh, hidden files
    left out), words split as sshd splits them, the first value of a keyword winning,
    AllowUsers and HostKey adding up. Returns ({keyword: (value, file)},
    {"allowusers": [...], "hostkey": [...]}, problems). A Match block is a problem: it changes
    values for some connections only."""
    first, lists, problems = {}, {"allowusers": [], "hostkey": []}, []

    def load(path, depth):
        if depth > SSHD_INCLUDE_DEPTH:
            problems.append(f"sshd: Include nested deeper than {SSHD_INCLUDE_DEPTH} ({path})")
            return
        if not _where(root, path).startswith(SSHD_INCLUDE_ROOTS):
            problems.append(f"sshd: {path} leads out of /etc and /usr")
            return
        content = text(root, path) or ""
        for number, line in enumerate(content.split("\n"), 1):
            m = re.fullmatch(r"[ \t\r]*([A-Za-z0-9]+)(?:[ \t\r]*=[ \t\r]*|[ \t\r]+|$)(.*)", line)
            if not line.strip(" \t\r") or line.lstrip(" \t\r").startswith("#"):
                continue
            args = _sshd_words(m.group(2)) if m else None
            if args is None:
                problems.append(f"sshd: line {number} of {path} cannot be read")
                continue
            keyword = SSHD_ALIASES.get(m.group(1).lower(), m.group(1).lower())
            if keyword == "match":
                problems.append(f"sshd: {path} has a Match block (line {number}); the Image allows none")
            elif keyword == "include":
                for pattern in args:
                    files, problem = _sshd_glob(root, pattern)
                    if problem:
                        problems.append(f"sshd: Include {pattern} {problem} ({path})")
                    for included in files:
                        load(included, depth + 1)
            elif keyword in lists:
                lists[keyword] += args
            else:
                first.setdefault(keyword, (" ".join(args), path))

    if read(root, SSHD_CONFIG) is None:
        problems.append(f"sshd: {SSHD_CONFIG} is missing")
    load(SSHD_CONFIG, 0)
    return first, lists, problems


def _sshd_words(rest):
    """The words of a configuration line after its keyword, split as OpenSSH's argv_split
    does: at blanks outside quotes, a # only at the start of a word ending the line, a
    backslash escaping a quote, a backslash or (outside quotes) a blank. None: a quote is
    not closed."""
    words, i = [], 0
    while True:
        while i < len(rest) and rest[i] in " \t\r":
            i += 1
        if i >= len(rest) or rest[i] == "#":
            return words
        word, quote = "", None
        while i < len(rest):
            c = rest[i]
            if c == "\\" and i + 1 < len(rest) and (rest[i + 1] in "'\"\\" or (quote is None and rest[i + 1] == " ")):
                word += rest[i + 1]
                i += 2
                continue
            if quote is None and c in " \t\r":
                break
            if quote is None and c in "'\"":
                quote = c
            elif quote is not None and c == quote:
                quote = None
            else:
                word += c
            i += 1
        if quote is not None:
            return None
        words.append(word)


def _sshd_glob(root, pattern):
    """The files an Include pattern names in the Image, in name order, and why the check
    cannot follow it, or None: ~ (the home of whoever runs sshd), a wildcard outside the last
    part, or a path that leads out of /etc and /usr, where the agent could write at runtime."""
    if pattern.startswith("~"):
        return [], "is a pattern the check does not read"
    path = pattern if pattern.startswith("/") else f"{SSHD_ETC}/{pattern}"
    folder, name = path.rsplit("/", 1)
    if re.search(r"[*?\[]", folder):
        return [], "has a wildcard in a directory"
    if not re.search(r"[*?\[]", name):
        files = [path]
    else:
        files = [f"{folder}/{n}" for n in sorted(_names(root, folder))
                 if fnmatch.fnmatchcase(n, name) and (name.startswith(".") or not n.startswith("."))]
    if any(not _where(root, f).startswith(SSHD_INCLUDE_ROOTS) for f in [folder + "/", *files]):
        return [], "leads out of /etc and /usr"
    return [f for f in files if read(root, f) is not None], None


def _names(root, folder):
    """Every name in a directory of the Image."""
    real = resolve(root, folder)
    try:
        return os.listdir(real) if real and os.path.isdir(real) else []
    except OSError:
        return []


def _where(root, path, hops=40):
    """Where `path` leads in the Image, links followed within it as far as they exist; the
    rest of a path that does not exist is kept as it stands."""
    todo, done = [p for p in path.split("/") if p], []
    while todo:
        part = todo.pop(0)
        if part == ".":
            continue
        if part == "..":
            if done:
                done.pop()
            continue
        real = os.path.join(root, *done, part)
        if os.path.islink(real) and hops > 0:
            hops -= 1
            target = os.readlink(real)
            if target.startswith("/"):
                done = []
            todo = [p for p in target.split("/") if p] + todo
            continue
        done.append(part)
    return "/" + "/".join(done) + ("/" if path.endswith("/") else "")


def _masked(root, folder, name):
    """Whether `name` in that directory of the Image is a link to /dev/null."""
    real = resolve(root, folder)
    try:
        return real is not None and os.readlink(os.path.join(real, name)) == "/dev/null"
    except OSError:
        return False


def _enabled(root, unit):
    """Where the Image enables a unit: a link in a .wants, .requires or .upholds directory,
    named after the unit or leading to it."""
    found = []
    for d in UNIT_DIRS:
        for name, info in listing(root, d).items():
            if not (name.endswith((".wants", ".requires", ".upholds")) and info["type"] == "dir"):
                continue
            for link in _names(root, f"{d}/{name}"):
                if unit in (link, os.path.basename(_where(root, f"{d}/{name}/{link}"))):
                    found.append(f"{d}/{name}/{link}")
    return found


def preset(root, unit):
    """What systemd's presets say for a unit, as at a VM's first boot: the first line that
    matches, files in name order (one in a higher directory replaces one of the same name
    below, a link to /dev/null there removes it); enable when none matches. (action, file)."""
    files = {}
    for d in reversed(PRESET_DIRS):
        for name, info in listing(root, d).items():
            if name.endswith(".preset"):
                if info["type"] == "file":
                    files[name] = f"{d}/{name}"
                else:
                    files.pop(name, None)
    for name in sorted(files):
        for line in (text(root, files[name]) or "").splitlines():
            words = line.split()
            if len(words) >= 2 and words[0] in ("enable", "disable", "ignore") and fnmatch.fnmatchcase(unit, words[1]):
                return words[0], files[name]
    return "enable", None


def _sshd(root):
    """SSH only by key and only as agent (#127): sshd's configuration has the values of SSHD,
    lets only agent in and takes host keys only from the root-owned directory under /run;
    sshd starts by itself in no VM (not enabled, the presets disable it at first boot,
    sshd@.service and systemd's ssh generator masked), and /etc/sysconfig/sshd adds nothing
    to its command line."""
    first, lists, problems = sshd_config(root)
    for keyword, wanted in SSHD.items():
        value, where = first.get(keyword, (None, None))
        same = value == wanted if keyword in SSHD_CASED else (value or "").lower() == wanted
        if not same:
            problems.append(f"sshd: {SSHD_NAMES[keyword]} is {value or 'not set'} instead of {wanted}"
                            + (f" ({where})" if where else ""))
    if lists["allowusers"] != ["agent"]:
        problems.append(f"sshd: AllowUsers is {' '.join(lists['allowusers']) or 'not set'} instead of agent")
    keys = lists["hostkey"]
    if not keys or any(k != os.path.normpath(k) or not k.startswith(SSHD_RUN + "/") for k in keys):
        problems.append(f"sshd: HostKey is {' '.join(keys) or 'not set'}, not only in {SSHD_RUN}")
    for unit in SSHD_UNITS:
        for link in _enabled(root, unit):
            problems.append(f"sshd: {unit} is enabled ({link})")
        action, where = preset(root, unit)
        if action != "disable":
            problems.append(f"sshd: the presets {action} {unit} at first boot" + (f" ({where})" if where else ""))
    for folder, name in SSHD_MASKS:
        if not _masked(root, folder, name):
            problems.append(f"sshd: {folder}/{name} is not masked")
    for line in (text(root, SSHD_SYSCONFIG) or "").splitlines():
        key, eq, value = line.strip().partition("=")
        if eq and key.strip() == "OPTIONS" and value.strip().strip("\"'").strip():
            problems.append(f"sshd: {SSHD_SYSCONFIG} adds OPTIONS to sshd's command line")
    return problems


def manifest(root):
    """What a derived Image is compared with: the safeguard files, the sealed directories
    and where environment and configuration are read, as the Base Build has them."""
    return {"files": {p: describe(root, p) for p in FILES + WATCHED_FILES},
            "dirs": {d: listing(root, d) for d in SEALED + WATCHED_DIRS}}


def _concern(root, path):
    """Why a file a derived Image added or changed where environment or configuration is
    read fails the check, or None. Claude Code's JSON settings are compared as Claude Code
    reads them, so the check sees decoded escapes ("\\u0041" for "A"). A settings file that
    does not parse fails as well."""
    content = text(root, path)
    if content is None:
        folder, name = path.rsplit("/", 1)
        real = resolve(root, folder or "/")          # the folder's links only within the Image
        if real is not None and os.path.lexists(os.path.join(real, name)):
            # a link to nothing in the Image (the agent's home, /tmp) or no regular file: what it
            # holds at runtime is never checked
            return f"{path} is new or changed and is no file the check can read"
        return None
    if path.startswith(CLAUDE_CODE) and path.endswith(".json"):
        try:
            content = json.dumps(json.loads(content), ensure_ascii=False)
        except (ValueError, RecursionError):
            return f"{path} is new or changed and is not valid JSON"
    if MENTIONS.search(content):
        return f"{path} is new or changed and concerns a Safeguard"
    return None


def _compare(root, base):
    """What a derived Image changed compared with its Base Build."""
    problems = []
    files, dirs = base.get("files", {}), base.get("dirs", {})
    for path in FILES:
        if describe(root, path) != files.get(path, {"type": "missing"}):
            problems.append(f"{path} differs from the Base image")
    for d in SEALED:
        now, then = listing(root, d), dirs.get(d, {})
        for name in sorted(set(now) | set(then)):
            if now.get(name) != then.get(name):
                what = "is missing" if name not in now else "is new" if name not in then else "differs"
                problems.append(f"{d}/{name} {what} (compared with the Base image)")
    changed = [p for p in WATCHED_FILES if p not in FILES and describe(root, p) != files.get(p, {"type": "missing"})]
    for d in WATCHED_DIRS:
        now, then = listing(root, d), dirs.get(d, {})
        changed += [f"{d}/{n}" for n in sorted(now) if now[n] != then.get(n)]
    for path in changed:
        problem = _concern(root, path)
        if problem:
            problems.append(problem)
    return problems


def check(root, days, base=None):
    """Check the Image whose files are at `root` (a mounted Build) against the package
    minimum age `days`; `base` is the manifest of the Base Build a derived Image was
    built on (None for the Base image). Returns (problems, manifest, agent versions)."""
    _too_large.clear()
    env = systemd_environment(root)
    problems = (_packages(root, env, days) + _git(root) + _network(root, env) + _claude(root, env) + _overrides(env)
                + _guest_agent(root) + _sshd(root))
    if base is not None:
        problems += _compare(root, base)
    problems += [f"{path} is larger than {MAX_READ >> 20} MiB and cannot be checked" for path in _too_large]
    return list(dict.fromkeys(problems)), manifest(root), agents(root)
