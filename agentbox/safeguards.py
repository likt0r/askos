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
  (NetworkManager keeps out of /etc/resolv.conf, dns=none), and no service carries a proxy
  variable: every connection goes to the Host, where the proxy reads its destination.
- Guest agent: the two wrappers the Host runs through it (agentbox-journal, agentbox-devices).
- Claude Code: fetches none of the owner's claude.ai connectors (#8). ENABLE_CLAUDEAI_MCP_SERVERS
  is false in the environment of every service, and /etc/profile.d/agentbox.sh exports it
  for shells. The Claude token cannot fetch connectors; this holds after a /login in the VM.
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
import hashlib
import json
import os
import re
import shlex
import stat

HOOKS = "/usr/lib/agentbox/hooks"
BUNFIG = "/usr/lib/agentbox/bunfig.toml"
GUEST_SETUP = "/usr/lib/agentbox/guest-setup"
NPM = "/usr/lib/node_modules/npm"
NPM_DEFAULT = "/usr/etc/npmrc"        # npm with prefix /usr reads this unless its builtin config names another
QGA = "/usr/libexec/qemu-ga/fsfreeze-hook.d"
JOURNAL_WRAPPER = f"{QGA}/agentbox-journal"
DEVICES_WRAPPER = f"{QGA}/agentbox-devices"
NM_CONF = "/etc/NetworkManager/conf.d/agentbox.conf"
RESOLVER = 'echo "nameserver $gw" > /etc/resolv.conf'   # guest-setup: the Host on the VM's network
PROFILE = "/etc/profile.d/agentbox.sh"
CLAUDEAI = "ENABLE_CLAUDEAI_MCP_SERVERS"
CLAUDE_CODE = "/etc/claude-code/"     # Claude Code's managed settings (JSON) on Linux
MIN_NPM = 12

# The files that make up the safeguards: a derived Image must leave each as the Base has it.
FILES = ("/etc/npmrc", NPM_DEFAULT, f"{NPM}/npmrc", "/etc/uv/uv.toml", "/etc/gitconfig", BUNFIG, GUEST_SETUP,
         "/usr/lib/agentbox/package-safeguards", "/usr/lib/systemd/system/agentbox-guest-setup.service",
         "/usr/lib/systemd/system.conf.d/agentbox.conf", "/usr/lib/systemd/system.conf.d/agentbox-packages.conf",
         NM_CONF, PROFILE)
# Directories a derived Image must leave exactly as they are.
SEALED = (HOOKS, QGA)
# Where environment and configuration are read: a derived Image may add or change files
# there, but none that mentions a safeguard (MENTIONS).
WATCHED_FILES = ("/etc/environment", "/etc/profile", "/etc/bashrc", "/etc/security/pam_env.conf",
                 "/etc/systemd/system.conf", "/usr/lib/systemd/system.conf", "/etc/systemd/user.conf",
                 "/etc/NetworkManager/NetworkManager.conf", "/etc/xdg/uv/uv.toml", "/etc/gitconfig",
                 f"{CLAUDE_CODE}managed-settings.json")
WATCHED_DIRS = ("/etc/profile.d", "/etc/environment.d", "/usr/lib/environment.d",
                "/etc/systemd/system.conf.d", "/usr/lib/systemd/system.conf.d", "/usr/local/lib/systemd/system.conf.d",
                "/etc/systemd/user.conf.d", "/usr/lib/systemd/user.conf.d",
                "/etc/NetworkManager/conf.d", "/usr/lib/NetworkManager/conf.d", f"{CLAUDE_CODE}managed-settings.d")
MENTIONS = re.compile(r"ignore[-_]?scripts|release[-_]?age|exclude[-_]?newer|hooks[-_]?path|_proxy\b|proxy=|"
                      r"proxy(host|port)|git_config|uv_no_config|\bdns\s*=|claudeai", re.IGNORECASE)
# Variables that would switch a safeguard off again for every service of the VM
OVERRIDES = re.compile(r"(npm_config_(ignore_scripts|min_release_age)|pnpm_config_minimum_release_age_exclude|"
                       r"git_config_(global|system|nosystem|count|parameters)|uv_no_config|uv_config_file)",
                       re.IGNORECASE)
# Proxy variables (HTTPS_PROXY, no_proxy, NODE_USE_ENV_PROXY, ...) and Java's proxy properties
PROXY_NAME = re.compile(r"\w*_proxy", re.IGNORECASE)
PROXY_VALUE = re.compile(r"proxy(host|port)", re.IGNORECASE)
# systemd's main configuration (the first one found) and its drop-in directories, lowest first
SYSTEM_CONF = ("/etc/systemd/system.conf", "/run/systemd/system.conf", "/usr/local/lib/systemd/system.conf",
               "/usr/lib/systemd/system.conf")
SYSTEM_DROPINS = ("/usr/lib/systemd/system.conf.d", "/usr/local/lib/systemd/system.conf.d",
                  "/run/systemd/system.conf.d", "/etc/systemd/system.conf.d")
AGENTS = {"claude-code": "@anthropic-ai/claude-code", "codex": "@openai/codex", "opencode": "opencode-ai",
          "npm": "npm", "pnpm": "pnpm", "bun": "bun"}
VERSION = re.compile(r"[0-9A-Za-z.+-]{1,40}")
MAX_READ = 1 << 20


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
    """The content of a regular file in the Image (at most MAX_READ bytes), or None."""
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
            return f.read(MAX_READ)
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


def _version(root, package):
    try:
        info = json.loads(text(root, f"/usr/lib/node_modules/{package}/package.json") or "")
    except ValueError:
        return None
    v = info.get("version") if isinstance(info, dict) else None
    return v if isinstance(v, str) and VERSION.fullmatch(v) else None


def agents(root):
    """Versions of the agents and package managers in the Image (None: not found)."""
    return {name: _version(root, package) for name, package in AGENTS.items()}


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
    version = _version(root, "npm")
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
    for name in sorted(env):
        if PROXY_NAME.fullmatch(name) or PROXY_VALUE.search(env[name]):
            problems.append(f"Proxy: {name} is set; the VM reaches the outside without proxy variables")
    return problems


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
    env = systemd_environment(root)
    problems = (_packages(root, env, days) + _git(root) + _network(root, env) + _claude(root, env) + _overrides(env)
                + _guest_agent(root))
    if base is not None:
        problems += _compare(root, base)
    return list(dict.fromkeys(problems)), manifest(root), agents(root)
