"""Where agentbox finds its files and programs: the one place that names them.

Host paths: AGENTBOX_ROOT moves every Host path under a test directory, so the CLI can
run as a normal user against fake state (the test seam of ADR 0006). External programs
(virsh, qemu-img, journalctl, ss, curl) are looked up on PATH.

Files of Agent Box (ARCHITECTURE.md, "Where the files live"): what a Release installs
lies under the prefix /usr, where the RPM puts it (ADR 0009):

  bin/agentbox                    CLI starter (python3 -I)      CLI
  lib/agentbox/agentbox/          this package
  lib/agentbox/VERSION            the Release                   VERSION
  libexec/agentbox-proxy          services and helpers          PROXY
  libexec/agentbox-suspicious                                   SUSPICIOUS
  libexec/agentbox-feeds                                        FEEDS_PROGRAM
  libexec/agentbox-gh-tokend                                    TOKEND
  libexec/agentbox-web                                          WEB_BACKEND
  libexec/agentbox-readings       (loaded by agentbox-web)
  share/agentbox/web/ui/          the Control Room UI           WEB_UI
  share/agentbox/vm/              Containerfile and guest files  VM_SOURCE
                                  (vm/..., github-app/..., the Base image's build context)
  share/agentbox/lists/           lists a new Host starts with   SEED_LISTS

A checkout names the same files, not its own, and, outside tests, changes nothing on a
Host (cli.py). Paths named for others (units, the sudoers rule, the Control Room's sudo
call) are the Host's even in tests; files agentbox opens or runs itself are under
AGENTBOX_ROOT in tests, like the Host paths. The programs in libexec that load one
another look next to themselves and need nothing from here; only the starters
(bin/agentbox, agentbox-web) find this package on their own.
"""
import os

ROOT = os.environ.get("AGENTBOX_ROOT", "")


def host(path):
    return ROOT + path if ROOT else path


CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))    # the directory this package is in
VERSION = os.path.join(CODE, "VERSION")      # the Release (agentbox version, the Control Room)
PREFIX = "/usr"
# A checkout is not installed: the files it names (in units, for sudo) need not exist on
# this Host, so its CLI changes nothing there (cli.py), except under AGENTBOX_ROOT.
INSTALLED = os.path.realpath(CODE) == os.path.realpath(host(PREFIX + "/lib/agentbox"))
CLI = PREFIX + "/bin/agentbox"
LIBEXEC = PREFIX + "/libexec"
SHARE = PREFIX + "/share/agentbox"
# named in units (agentbox regenerate)
PROXY = LIBEXEC + "/agentbox-proxy"
TOKEND = LIBEXEC + "/agentbox-gh-tokend"
FEEDS_PROGRAM = LIBEXEC + "/agentbox-feeds"
# run or read by agentbox and the Control Room
SUSPICIOUS = host(LIBEXEC + "/agentbox-suspicious")
WEB_BACKEND = host(LIBEXEC + "/agentbox-web")
WEB_UI = host(SHARE + "/web/ui")
VM_SOURCE = host(SHARE + "/vm")              # Builds from the installed Release (ADR 0010, #96)
SEED_LISTS = host(SHARE + "/lists")

CONF = host("/etc/agentbox")
REGISTRY = host("/etc/agentbox/vms")
WEB_CONF = host("/etc/agentbox/web")        # Control Room: certificate, key, password hash
SYSTEMD = host("/etc/systemd/system")
STATE = host("/var/lib/agentbox")
VMS = host("/var/lib/agentbox/vms")
IMAGES = host("/var/lib/agentbox/images")    # Builds: <image>-<build>.qcow2, <image>.qcow2 the current one
BUILDS = host("/var/lib/agentbox/builds")    # a record per Build
ARCHIVE = host("/var/lib/agentbox/archive")      # Data disks of deleted VMs
FEEDS = host("/var/lib/agentbox/feeds")         # HaGeZi lists, written by agentbox-feeds
TRANSFERS = host("/var/lib/agentbox/transfers")  # tar files of `agentbox import` while attached to a VM
ORCA = host("/opt/Orca/resources/bin/orca-ide")  # installed by the owner, not part of a Release
# UID_MIN: the lowest uid of a person (agentbox setup, the owner user)
LOGIN_DEFS = host("/etc/login.defs")
# libvirt-guests.service: the VMs when the Host shuts down (agentbox setup writes it)
LIBVIRT_GUESTS = host("/etc/sysconfig/libvirt-guests")


def tunable(name, default):
    """A time limit or count that tests shorten through the environment. Only read
    when AGENTBOX_ROOT is set, so a stray variable cannot change a real run;
    malformed values fall back to the default."""
    if not ROOT:
        return default
    try:
        return max(0, int(os.environ.get(name, "")))
    except ValueError:
        return default


# how long agentbox waits for agentbox-suspicious to classify a domain
CLASSIFIER_SECONDS = tunable("AGENTBOX_CLASSIFIER_SECONDS", 30)


def test_address(name):
    """host:port from the environment, only in tests (see tunable), else None."""
    if not ROOT:
        return None
    host, _, port = os.environ.get(name, "").rpartition(":")
    return (host, int(port)) if host and port.isdigit() else None


def classifier_env():
    """Environment for agentbox-suspicious: where the lists of this Host (or of the
    test root) are."""
    env = dict(os.environ, AGENTBOX_CONF_DIR=CONF, AGENTBOX_FEEDS_DIR=FEEDS)
    env.pop("AGENTBOX_SUSPICIOUS", None)
    return env
