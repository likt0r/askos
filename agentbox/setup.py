"""agentbox setup: set a Host up (ADR 0009).

Writes the Host setup, sets the Control Room password, creates the certificate if there
is none, does the Host steps (hoststeps.py) and starts the Control Room.

  agentbox setup [--mode local] [--web-port N] [--cert-name NAME] [--owner-user USER]
                 [--password-file FILE | --password-stdin] [--force] [--json]
  agentbox setup --mode network --bind ADDRESS --home-network CIDR [...]
  agentbox setup [--json]            on a Host set up already: the Host steps only

Local-only (the default): the Control Room and the Orca ports are reachable only from the
Host itself and no firewall rule is added for any network. Network mode: they are also
reachable from the named home network, and only from it: the firewall accepts that network
on the Control Room and Orca ports and rejects everything else on them.

The owner user (--owner-user, else the user who ran sudo) is stored in the Host setup: they
go into the group libvirt. It has to be a person's account, and setup refuses to run through
sudo for a service or system user (the Control Room's sudo rule allows agentbox with any
arguments). Builds need nothing of theirs: Orca comes from its GitHub release (#113), and an
/etc/agentbox/build.conf an earlier setup wrote is no longer read.

The password never goes onto a command line: it comes from a file, from stdin, or is asked
for at a terminal (twice); one that is there already stays unless --force. Without --force
an existing Host setup is left alone, so running
setup twice cannot change how the Host is exposed: without options setup then does the Host
steps for the Host setup there is (after an update, or to finish a run that stopped), and
with any other option it refuses. Settings, the VM registry, an existing certificate and the
GitHub App are never touched; with --force the Orca ports of existing VMs and the firewall
rules follow the new mode.

The steps run in a fixed order (ARCHITECTURE.md, "Setting a Host up"); each changes
only what differs, so a second run changes nothing. One that fails stops setup with a message
naming it; every file is replaced whole, and the Host setup is written only after the steps
before it succeeded.
"""
import grp
import json
import os
import re
import subprocess
import sys
import tempfile
from getpass import getpass

from . import firewall, generate, hoststeps, hostsetup, paths, state_files
from .errors import Fail, die

MIN_PASSWORD = 16
WEB_GROUP = "agentbox-web"
WEB_UNIT = "agentbox-web.service"

# Orca port = 6800 + network number (1-254). The whole range is let through so a new VM
# (agentbox create) needs no firewall change; only what a VM in the registry listens on
# answers. Everything else on Orca ports, also the desktop runtime (6768-6799, 16768), is
# rejected, like the Control Room's port.
ORCA_ACCEPT = "6801-7054"
ORCA_REJECT = ("6768-7054", "16768")
NETWORK_OPTIONS = {"--bind": "bind", "--home-network": "home_network"}


def _usage(message, **params):
    die(message, 2, error="invalid_argument", **params)


def _parse(args):
    opts = {"mode": hostsetup.LOCAL, "web_port": None, "cert_name": None, "owner_user": None, "password": None,
            "force": False, "json": False, "bind": None, "home_network": None, "given": []}
    args = list(args)
    while args:
        a = args.pop(0)
        if a != "--json":
            opts["given"].append(a)
        value = lambda: args.pop(0) if args else _usage(f"{a} needs a value", option=a)      # noqa: E731
        if a == "--mode":
            opts["mode"] = value()
        elif a == "--web-port":
            v = value()
            if not v.isdigit():
                _usage(f"--web-port: not a port number: {v!r}", option="--web-port")
            opts["web_port"] = int(v)
        elif a == "--cert-name":
            opts["cert_name"] = value()
        elif a == "--owner-user":
            opts["owner_user"] = value()
        elif a in NETWORK_OPTIONS:
            opts[NETWORK_OPTIONS[a]] = value()
        elif a == "--password-file":
            opts["password"] = ("file", value())
        elif a == "--password-stdin":
            opts["password"] = ("stdin", None)
        elif a == "--force":
            opts["force"] = True
        elif a == "--json":
            opts["json"] = True
        else:
            _usage(f"unknown option: {a}", option=a)
    if opts["mode"] not in (hostsetup.LOCAL, hostsetup.NETWORK):
        _usage("--mode: local or network", option="--mode")
    given = [o for o, k in NETWORK_OPTIONS.items() if opts[k] is not None]
    if opts["mode"] == hostsetup.LOCAL and given:
        _usage(f"{', '.join(given)} exist only in Network mode (--mode network)", options=given)
    missing = [o for o, k in NETWORK_OPTIONS.items() if opts[k] is None]
    if opts["mode"] == hostsetup.NETWORK and missing:
        _usage(f"Network mode needs: {', '.join(missing)}", options=missing)
    return opts


def _password(source):
    """The password from its source; asked for twice at a terminal, never echoed."""
    kind, where = source or (None, None)
    if kind == "file":
        try:
            with open(where, encoding="utf-8") as f:
                pw = f.readline()
        except (OSError, UnicodeDecodeError) as e:
            die(f"{where}: {getattr(e, 'strerror', None) or e}", error="action_failed", path=where)
    elif kind == "stdin":
        pw = sys.stdin.readline()
    else:
        if not sys.stdin.isatty():
            die("no terminal: give the password with --password-file or --password-stdin", error="invalid_argument")
        pw = getpass(f"Password for the Control Room (at least {MIN_PASSWORD} characters): ")
        if getpass("Repeat: ") != pw:
            die("the passwords do not match", error="invalid_argument")
        return _check_password(pw)
    return _check_password(pw.rstrip("\n"))


def _check_password(pw):
    if len(pw) < MIN_PASSWORD:
        die(f"the password needs at least {MIN_PASSWORD} characters", error="invalid_argument", minimum=MIN_PASSWORD)
    return pw


def _hash(pw):
    """The scrypt hash the Control Room checks; computed by the backend itself so both agree."""
    try:
        r = subprocess.run([sys.executable, "-I", paths.WEB_BACKEND, "--hash-password"], input=pw + "\n",
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        die(f"{paths.WEB_BACKEND}: {e}", error="action_failed")
    if r.returncode != 0 or not r.stdout.strip():
        die("the password could not be hashed: " + (r.stderr.strip()[-200:] or f"exit {r.returncode}"),
            error="action_failed")
    return r.stdout.strip()


def _web_gid():
    """Group of the Control Room's files (root only; tests run as a normal user)."""
    if os.geteuid() != 0:
        return -1
    try:
        return grp.getgrnam(WEB_GROUP).gr_gid
    except KeyError:
        return die(f"the group {WEB_GROUP} is missing (the package creates it)", error="action_failed", group=WEB_GROUP)


def _certificate(setup, gid):
    """Create the certificate unless one exists; True if created. An existing one is never replaced."""
    cert, key = os.path.join(paths.WEB_CONF, "cert.pem"), os.path.join(paths.WEB_CONF, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return False
    name = setup["cert_name"]
    names = ["DNS:localhost", "IP:127.0.0.1"]
    if setup["mode"] == hostsetup.NETWORK:
        names.append(f"IP:{setup['bind']}")
    if name:
        names.append(f"DNS:{name}")
    subject = name or (setup["bind"] if setup["mode"] == hostsetup.NETWORK else "localhost")
    with tempfile.TemporaryDirectory(dir=paths.WEB_CONF, prefix=".cert.") as tmp:
        t_cert, t_key = os.path.join(tmp, "cert.pem"), os.path.join(tmp, "key.pem")
        try:
            r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                                "-nodes", "-days", "3650", "-subj", f"/CN={subject}",
                                "-addext", "subjectAltName=" + ",".join(names), "-keyout", t_key, "-out", t_cert],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            die(f"openssl: {e}", error="action_failed")
        if r.returncode != 0:
            die("the certificate could not be created: " + (r.stderr.strip()[-200:] or f"exit {r.returncode}"),
                error="action_failed")
        for src, dst, mode in ((t_key, key, 0o640), (t_cert, cert, 0o644)):
            os.chmod(src, mode)
            if gid != -1:
                os.chown(src, -1, gid)
            os.replace(src, dst)
    return True


def _fingerprint():
    try:
        r = subprocess.run(["openssl", "x509", "-in", os.path.join(paths.WEB_CONF, "cert.pem"), "-noout",
                            "-fingerprint", "-sha256"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip().partition("=")[2] or None


def _run(argv, what):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return die(f"{what}: {e}", error="action_failed")


def _systemctl(*args):
    r = _run(["systemctl", *args], f"systemctl {' '.join(args)}")
    if r.returncode != 0:
        die(f"systemctl {' '.join(args)} fails: " + (r.stderr.strip()[-200:] or f"exit {r.returncode}"),
            error="action_failed")


def firewall_rules(setup):
    """(accept, reject) rich rules of a Host setup; none in Local-only."""
    if setup["mode"] != hostsetup.NETWORK:
        return [], []
    net, web = setup["home_network"], str(setup["web_port"])
    accept = [f'rule priority="-110" family="ipv4" source address="{net}" port port="{p}" protocol="tcp" accept'
              for p in (web, ORCA_ACCEPT)]
    reject = [f'rule priority="-100" family="{fam}" port port="{p}" protocol="tcp" reject'
              for p in (web, *ORCA_REJECT) for fam in ("ipv4", "ipv6")]
    return accept, reject


def _firewall(new, old):
    """Make the firewall match `new` (a Host setup): add its rules, remove those of `old`
    that `new` no longer has. Nothing is called when there is nothing to do."""
    add = [r for rules in firewall_rules(new) for r in rules]
    drop = [r for r in (x for rules in firewall_rules(old) for x in rules) if r not in add] if old else []
    if not add and not drop:
        return []
    changed = firewall.ensure(firewall.default_zone(), add=[("rich-rule", r) for r in add],
                              remove=[("rich-rule", r) for r in drop])
    counts = {verb: sum(1 for v, _, _ in changed if v == verb) for verb in ("add", "remove")}
    return [f"{n} rules {word}" for verb, word in (("add", "added"), ("remove", "removed"))
            for n in [counts[verb]] if n]


def _sudo_user():
    """Who ran `sudo agentbox setup`; None for root itself."""
    user = os.environ.get("SUDO_USER", "")
    return user if user != "root" and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user) else None


class _Steps:
    """The steps of one run: each said as it is done (text) or collected (--json); one that
    fails stops setup with its name and how to go on."""

    def __init__(self, as_json):
        self.as_json = as_json
        self.done, self.warnings = [], []
        self.written = False        # the Host setup setup goes on from is on disk

    def warn(self, text):
        self.warnings.append(text)
        print(text, file=sys.stderr)

    def run(self, name, step, *args):
        try:
            changes = step(*args) or []
        except Fail as f:
            again = ("run agentbox setup again without options, it goes on from there" if self.written
                     else "run setup again, what is done already stays")
            raise Fail(f"setup stopped at {name}: {f.message} (once that is fixed, {again})", f.exit,
                       error=f.error, **f.params) from None
        self.done.append({"step": name, "changes": changes})
        if not self.as_json:
            print(f"{name}: {'; '.join(changes) or 'up to date'}", flush=True)
        return changes


def setup(args):
    opts = _parse(args)
    hoststeps.check_caller()        # not the Control Room or another service through its sudo rule
    for field, value in (("--cert-name", opts["cert_name"]), ("--owner-user", opts["owner_user"])):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
            _usage(f"{field}: not a valid name: {value!r}", option=field)
    existing = os.path.exists(hostsetup.PATH)
    # set up already and no --force: only the Host steps, for the Host setup there is
    again = existing and not opts["force"]
    if again:
        new, old, record = hostsetup.load(), None, None     # a record that makes no sense stops here
        # a Host setup without owner_user (written by setup before it kept one) gets it once,
        # from --owner-user or the sudo user; a recorded one changes only with --force
        if [o for o in opts["given"] if o != "--owner-user" or new["owner_user"]]:
            die(f"{hostsetup.PATH} exists already: the Host is set up. Without options agentbox setup brings it "
                "up to date and leaves the Host setup as it is; replace it with --force (Settings, VMs and the "
                "certificate stay)", error="invalid_argument")
        user = new["owner_user"]
        if not user:
            user = opts["owner_user"] or _sudo_user()
            if user:
                record = {**{k: v for k, v in new.items() if v is not None}, "owner_user": user}
                new = hostsetup.parse(record)
    else:
        record = {"mode": opts["mode"], "bind": hostsetup.LOOPBACK,
                  "web_port": opts["web_port"] or hostsetup.DEFAULT_WEB_PORT}
        record.update({k: opts[k] for k in ("bind", "home_network", "cert_name") if opts[k]})
        user = opts["owner_user"] or _sudo_user()
        if user:
            record["owner_user"] = user
        new = hostsetup.parse(record)       # refuses addresses and networks that make no sense
        old = None
        if existing:
            try:
                old = hostsetup.load()      # to take back its firewall rules; a broken record just has none
            except Fail:
                old = None
    if not user:
        _usage("--owner-user is missing: who uses the Host (goes into the group libvirt); without the option "
               "the user who ran sudo", option="--owner-user")
    owner = hoststeps.owner(user)
    gid = _web_gid()

    steps = _Steps(opts["json"])
    steps.written = again
    # before anything changes: the detection of this Release with the lists that will apply
    steps.run("Self-test of the detection", hoststeps.self_test)
    generate.ports_free()           # a service on the VMs' resolver or proxy ports stops setup here
    # a password there stays, unless --force replaces the Host setup
    keep_password = again or (not opts["force"] and os.path.exists(os.path.join(paths.WEB_CONF, "password")))
    password_hash = None if keep_password else _hash(_password(opts["password"]))

    steps.run("Directories", hoststeps.directories)
    steps.run("Groups", hoststeps.groups, owner)
    steps.run("Lists", hoststeps.seed_lists, steps.warn)
    # the VM networks closed before regenerate (below) can bring up a VM
    steps.run("Firewall zone libvirt", hoststeps.libvirt_zone)
    steps.run("Network rules of the VMs", hoststeps.vm_rules)
    steps.run("libvirt-guests", hoststeps.libvirt_guests)
    steps.run("Expiry of Temporary allowlist entries", hoststeps.expiry_timer)
    steps.run("migrate", hoststeps.migrate)
    steps.run("migrate-allowlist", hoststeps.migrate_allowlist)

    created = False

    def certificate_and_password():
        nonlocal created
        try:
            os.makedirs(paths.WEB_CONF, exist_ok=True)
            os.chmod(paths.WEB_CONF, 0o750)
            if gid != -1:
                os.chown(paths.WEB_CONF, 0, gid)
        except OSError as e:
            die(f"{paths.WEB_CONF}: {e.strerror}", error="action_failed", path=paths.WEB_CONF)
        created = _certificate(new, gid)
        said = ["certificate created"] if created else []
        if password_hash:
            state_files.write_atomic(os.path.join(paths.WEB_CONF, "password"), (password_hash + "\n").encode(),
                                     0o640, gid)
            said.append("password set")
        elif not again:
            said.append("the existing password stays (a new one with --force)")
        return said

    def host_setup_and_regenerate():
        with state_files.locked(paths.REGISTRY):
            if record is not None:
                state_files.write_atomic(hostsetup.PATH, (json.dumps(record, indent=2) + "\n").encode(), 0o644)
                steps.written = True
            # the Orca ports of existing VMs follow the mode
            result = generate.apply()
        said = [] if record is None else [f"owner_user {record['owner_user']} added to the Host setup" if again
                                         else "Host setup written"]
        return said + generate.report(result)

    def control_room():
        if again:
            return hoststeps.enable(WEB_UNIT)
        _systemctl("enable", WEB_UNIT)
        _systemctl("restart", WEB_UNIT)
        return [f"{WEB_UNIT} enabled and restarted"]

    steps.run("Certificate and password", certificate_and_password)
    steps.run("Firewall for the home network", _firewall, new, old)
    steps.run("regenerate", host_setup_and_regenerate)
    # proxies with code from before the records read only the text lists migrate-lists moves aside
    steps.run("Proxies", hoststeps.proxies_before_lists)
    steps.run("migrate-lists", hoststeps.migrate_lists)
    steps.run("Activity journal", hoststeps.journal_sockets)
    steps.run("Feeds", hoststeps.first_feeds)
    steps.run("Control Room", control_room)

    url = f"https://{new['bind']}:{new['web_port']}"
    fingerprint = _fingerprint()
    if opts["json"]:
        kept = "owner_user_added" if record is not None else "kept"
        print(json.dumps({"mode": new["mode"], "url": url, "host_setup": kept if again else "written",
                          "certificate_created": created, "fingerprint": fingerprint, "steps": steps.done,
                          "warnings": steps.warnings}))
        return
    reach = (f"Network mode, reachable from {new['home_network']}." if new["mode"] == hostsetup.NETWORK
             else "Local-only, reachable from this machine only.")
    if again:
        print(f"Host setup completed with the owner {new['owner_user']}: {reach}" if record is not None
              else f"Host setup unchanged: {reach}")
    else:
        print(f"Host set up: {reach}")
    print(f"Control Room: {url}")
    if new["mode"] == hostsetup.NETWORK and not created and not again:
        print("The existing certificate stays. If it does not fit this address, delete cert.pem and key.pem "
              f"in {paths.WEB_CONF} and run setup --force again.")
    if fingerprint:
        print(f"Certificate fingerprint (compare it in the browser on the first visit): {fingerprint}")


COMMANDS = {"setup": setup}
