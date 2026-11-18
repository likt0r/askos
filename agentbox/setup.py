"""agentbox setup: set a Host up (ADR 0009).

Writes the Host setup, sets the Control Room password, creates the certificate if there
is none, does the Host steps (hoststeps.py) and starts the Control Room.

  agentbox setup [--mode local] [--web-port N] [--cert-name NAME] [--owner-user USER]
                 [--password-file FILE | --password-stdin] [--new-password] [--force] [--json]
  agentbox setup --mode network --bind ADDRESS --home-network CIDR [...]
  agentbox setup [--json]            on a Host set up already: the Host steps only

Local-only (the default): the Control Room, the Orca ports and the SSH ports are reachable
only from the Host itself and no firewall rule is added for any network. Network mode: they
are also reachable from the named home network, and only from it: the firewall accepts that
network on the Control Room's, the Orca and the SSH ports and rejects everything else on them
(firewall.py).

The owner user (--owner-user, else the user who ran sudo) is stored in the Host setup: they
go into the group libvirt. It has to be a person's account, and setup refuses to run through
sudo for a service or system user (the Control Room's sudo rule allows agentbox with any
arguments). Builds need nothing of theirs: Orca comes from its GitHub release (#113), and an
/etc/agentbox/build.conf an earlier setup wrote is no longer read.

The password never goes onto a command line: it comes from a file, from stdin, or is asked
for at a terminal (twice); one that is there already stays, also with --force, which replaces
the Host setup only: a new one is set with --new-password, with the source or the prompt as
on a first run, and a password source without it is refused on a Host that has one. Without
--force an existing Host setup is left alone, so running
setup twice cannot change how the Host is exposed: without options setup then does the Host
steps for the Host setup there is (after an update, or to finish a run that stopped; with
--new-password it sets a new password besides), and with any other option it refuses.
Activity records a new password, never its value. Settings, the VM registry, an existing
certificate and the GitHub App are never touched; with --force the Orca and SSH ports of
existing VMs and the firewall rules follow the new mode.

The steps run in a fixed order (ARCHITECTURE.md, "Setting a Host up"); each changes
only what differs, so a second run changes nothing. One that fails stops setup with a message
naming it; every file is replaced whole, and the Host setup is written only after the steps
before it succeeded.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from getpass import getpass

from . import firewall, footprint, generate, hoststeps, hostsetup, paths, programs, state_files
from .commands import declare
from .errors import Fail, die

MIN_PASSWORD = 16
WEB_GROUP = "agentbox-web"
WEB_UNIT = "agentbox-web.service"
CONTROL_ROOM_PENDING = "control-room"      # a new Host setup the Control Room has not restarted onto

NETWORK_OPTIONS = {"--bind": "bind", "--home-network": "home_network"}


def _usage(message, **params):
    die(message, 2, error="invalid_argument", **params)


def _parse(args):
    values = args.values
    opts = {"mode": values.get("mode", hostsetup.LOCAL), "web_port": values.get("web-port"),
            "cert_name": values.get("cert-name"), "owner_user": values.get("owner-user"),
            "password": ("file", values["password-file"]) if "password-file" in values else
                        ("stdin", None) if "password-stdin" in values else None,
            "new_password": values.get("new-password", False),
            "force": values.get("force", False), "json": values.get("json", False),
            "bind": values.get("bind"), "home_network": values.get("home-network"),
            "given": ["--" + name for name in values if name != "json"]}
    if opts["web_port"] is not None:
        if not opts["web_port"].isdigit():
            _usage(f"--web-port: not a port number: {opts['web_port']!r}", option="--web-port")
        opts["web_port"] = int(opts["web_port"])
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
    return state_files.group_id(WEB_GROUP, "the package creates it")


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


# the home-network rules a run of setup added, or found in the Host setup it replaces, until
# the last step (_take_back) leaves only those the recorded Host setup needs
NOTED = os.path.join(paths.STATE, "pending", "home-network-rules.json")


def _noted():
    rules = state_files.read_json(NOTED)
    return [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else []


def _note(rules):
    if rules:
        state_files.directory(os.path.dirname(NOTED), 0o700, only_new=True)
        state_files.write_json(NOTED, rules, 0o600)
    else:
        state_files.remove(NOTED)


def _flat(setup):
    return [r for rules in firewall.home_network_rules(setup) for r in rules] if setup else []


def _firewall(new, old):
    """Add the rules of `new` (a Host setup), before any port listens on every address. Every
    rule it adds and every rule of `old` that `new` does not have is noted first (NOTED), so
    _take_back can take back what the recorded Host setup does not need, also when this run
    stops before `new` is recorded. Nothing is called when there is nothing to add."""
    add, drop = _flat(new), _flat(old)
    noted = _noted()
    wanted = list(dict.fromkeys(noted + [r for r in add if r not in drop] + [r for r in drop if r not in add]))
    if wanted != noted:
        _note(wanted)
    if not add:
        return []
    changed = firewall.ensure(firewall.default_zone(), add=[("rich-rule", r) for r in add])
    # a rule that was there already is not agentbox's to take back
    added = {value for _, _, value in changed}
    kept = [r for r in wanted if r in noted or r in drop or r in added]
    if kept != wanted:
        _note(kept)
    return [f"{len(changed)} rules added"] if changed else []


def _still_listening():
    """The VMs being deleted whose Orca or SSH port unit is still there: their sockets may
    listen on every address, and regenerate rebinds only the VMs in the registry."""
    return [m["record"]["name"] for m in footprint.being_deleted()
            if any(os.path.exists(footprint.unit_file(footprint.unit_name(m["record"]["name"], kind)))
                   for kind in ("gui.socket", "ssh.socket"))]


def _take_back():
    """The firewall reconciled with the recorded Host setup, once regenerate moved the Orca and
    SSH port sockets and the Control Room was restarted onto it: every noted rule (NOTED) it
    does not need is removed. Without any rule needed (Local-only) none goes while a VM being
    deleted may still listen on every address."""
    needed = _flat(hostsetup.load())
    rules = [r for r in _noted() if r not in needed]
    if not rules:
        state_files.remove(NOTED)
        return []
    if not needed:
        with state_files.locked(paths.REGISTRY):
            names = _still_listening()
        if names:
            finish = ", ".join(f"agentbox delete {n} --confirm {n}" for n in names)
            one = len(names) == 1
            die(f"the rules of Network mode stay while {', '.join(names)} {'is' if one else 'are'} being deleted: "
                f"{'its' if one else 'their'} Orca and SSH ports may still listen on every address. Finish the "
                f"delete with {finish}",
                error="action_failed", vms=names)
    changed = firewall.ensure(firewall.default_zone(), remove=[("rich-rule", r) for r in rules])
    state_files.remove(NOTED)
    return [f"{len(changed)} rules removed"] if changed else []


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
    # a password there stays, also with --force: a password source sets a new one only
    # together with --new-password
    password_there = os.path.exists(os.path.join(paths.WEB_CONF, "password"))
    if opts["password"] is not None and not opts["new_password"] and password_there:
        # with options that a Host set up already takes only with --force, that one is named too
        needs_force = existing and not opts["force"] and any(
            o not in ("--password-file", "--password-stdin") for o in opts["given"])
        die("the Control Room has a password already: a new one is set with "
            + ("--force --new-password" if needs_force else "--new-password")
            + " (with --password-file, --password-stdin or the prompt)", error="invalid_argument",
            option="--new-password")
    if again:
        new, record = hostsetup.load(), None     # a record that makes no sense stops here
        old = new           # the recorded Host setup stays (owner_user aside, which no rule names)
        # a Host setup without owner_user (written by setup before it kept one) gets it once,
        # from --owner-user or the sudo user; a recorded one changes only with --force.
        # --new-password (with --password-file/--password-stdin or the prompt) sets a new
        # Control Room password besides the Host steps; every other option needs --force
        allowed = {"--owner-user"} if not new["owner_user"] else set()
        if opts["new_password"]:
            allowed |= {"--new-password", "--password-file", "--password-stdin"}
        if [o for o in opts["given"] if o not in allowed]:
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
    # a password there stays, also with --force; a new one only with --new-password. On a
    # Host set up already without one (a run that stopped) a plain run asks for none and
    # leaves the Host without a password: only --new-password sets one.
    keep_password = not opts["new_password"] and (again or password_there)
    password_hash = None if keep_password else _hash(_password(opts["password"]))

    steps.run("Directories", hoststeps.directories)
    steps.run("Groups", hoststeps.groups, owner)
    steps.run("Lists", hoststeps.seed_lists, steps.warn)
    # the VM networks closed before regenerate (below) can bring up a VM
    steps.run("Firewall zone libvirt", hoststeps.libvirt_zone)
    steps.run("Network rules of the VMs", hoststeps.vm_rules)
    steps.run("libvirt-guests", hoststeps.libvirt_guests)
    steps.run("Expiry of Temporary allowlist entries", hoststeps.expiry_timer)
    steps.run("Container image cleanup", hoststeps.container_image_cleanup_timer)
    steps.run("migrate", hoststeps.migrate)
    steps.run("migrate-allowlist", hoststeps.migrate_allowlist)

    created = False

    def certificate_and_password():
        nonlocal created
        try:
            state_files.directory(paths.WEB_CONF, 0o750, gid)
        except OSError as e:
            die(f"{paths.WEB_CONF}: {e.strerror}", error="action_failed", path=paths.WEB_CONF)
        created = _certificate(new, gid)
        said = ["certificate created"] if created else []
        if password_hash:
            state_files.write_atomic(os.path.join(paths.WEB_CONF, "password"), (password_hash + "\n").encode(),
                                     0o640, gid)
            said.append("password set")
        elif password_there and not again:
            said.append("the existing password stays (a new one with --new-password)")
        elif not password_there:
            said.append("no password is set (set one with --new-password)")
        return said

    def host_setup_and_regenerate():
        with state_files.locked(paths.REGISTRY):
            if record is not None:
                if not again:       # the Control Room restarts onto it, also in a run that goes on
                    generate.mark_pending(CONTROL_ROOM_PENDING)
                state_files.write_json(hostsetup.PATH, record)
                steps.written = True
            # the Orca ports of existing VMs follow the mode
            result = generate.apply()
        said = [] if record is None else [f"owner_user {record['owner_user']} added to the Host setup" if again
                                         else "Host setup written"]
        return said + generate.report(result)

    def control_room():
        if not generate.is_pending(CONTROL_ROOM_PENDING):
            return hoststeps.enable(WEB_UNIT)
        programs.systemctl("enable", WEB_UNIT)
        generate.apply_pending(CONTROL_ROOM_PENDING, "restart", WEB_UNIT)
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
    # only now no port listens on every address any more: the rules a switch replaced go
    steps.run("Firewall rules taken back", _take_back)

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


DECLARATIONS = {
    "setup": declare(setup, "agentbox setup [--mode local|network] [--bind A --home-network N] [--web-port P] [--cert-name N] [--owner-user U] [--password-file F|--password-stdin] [--new-password] [--force] [--json]",
                     options={"mode": True, "bind": True, "home-network": True, "web-port": True,
                              "cert-name": True, "owner-user": True, "password-file": True,
                              "password-stdin": False, "new-password": False, "force": False, "json": False},
                     positional=False,
                     describe=lambda a: ("Set up the Host with a new Control Room password"
                                         if "new-password" in a.values else "Set up the Host", {})),
}
