"""libvirt through virsh, with a time limit on every call."""
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from . import footprint, paths, programs
from .registry import dom

# Each call has its own limit so a hanging guest agent or libvirtd cannot block
# agentbox (guest-agent commands get 10 s on top of that inside virsh).
TIMEOUT = paths.tunable("AGENTBOX_VIRSH_TIMEOUT", 15)
# a transient domain's start (create_transient) has this limit, not TIMEOUT
CREATE_TIMEOUT = 120
# state() of a domain libvirt does not know (or cannot tell)
MISSING = "missing"
_COMMAND = ("virsh", "-q", "-c", "qemu:///system")


def _virsh(args, timeout, **kw):
    limit = TIMEOUT if timeout is None else min(timeout, TIMEOUT)
    env = dict(os.environ, LC_ALL="C", **kw.pop("env", {}))
    try:
        return subprocess.run([*_COMMAND, *args], capture_output=True, text=True,
                              errors="replace", env=env, timeout=limit, **kw)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"virsh does not answer within {limit:g} s")
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "virsh is missing")


def run(*args, timeout=None):
    """virsh with these arguments; timeout: a shorter limit than TIMEOUT for this call."""
    return _virsh(args, timeout)


def quote(word):
    """One word for virsh's own command parser: in single quotes, which keep everything as it
    is; a single quote closes them, follows escaped and opens them again."""
    return "'" + word.replace("'", "'\\''") + "'"


def run_unseen(*args, timeout=None):
    """virsh with this command on its standard input instead of in its arguments, for a command
    that carries a secret: the arguments of every process are there for every local user to read
    (/proc/<pid>/cmdline), its standard input is not. Only for a command whose output is one
    line (qemu-agent-command), as run gives it.

    virsh without a command reads commands from its standard input as its interactive shell
    does, through readline: it shows the line it reads and its prompt on its standard output,
    keeps a history file and ends with 0 also when the command failed. So its terminal is a
    dumb one, without the user's readline settings, its history goes to a directory of its own
    that is removed afterwards and keeps no line (VIRSH_HISTSIZE=0), a failure is an error line
    on its standard error (exit code 1), and the output is the line before its last prompt."""
    line = " ".join(quote(a) for a in args)
    if any(c < " " or c == "\x7f" for c in line):          # readline would act on them
        raise ValueError("a command for virsh's standard input is one line of printable characters")
    with tempfile.TemporaryDirectory(prefix="agentbox-virsh-") as cache:
        r = _virsh((), timeout, input=line + "\n", env={
            "TERM": "dumb", "INPUTRC": "/dev/null", "XDG_CACHE_HOME": cache, "VIRSH_HISTSIZE": "0"})
    if r.returncode != 0:
        return r
    lines = r.stdout.split("\n")
    if any(e.startswith("error:") for e in r.stderr.splitlines()):
        return subprocess.CompletedProcess(args, 1, "", r.stderr)
    if len(lines) < 4 or lines[-1] or not lines[-2].startswith("virsh"):
        return subprocess.CompletedProcess(args, 1, "", r.stderr + "virsh gave no output for the command\n")
    return subprocess.CompletedProcess(args, 0, lines[-3] + "\n", r.stderr)


# --- lookups: what libvirt knows about a VM and its network -----------------------------------

def state(name):
    """First line of `virsh domstate`, e.g. "running" or "shut off"; MISSING when unknown."""
    r = run("domstate", dom(name))
    first = r.stdout.split("\n")[0].strip() if r.returncode == 0 else ""
    return first or MISSING


def domain_state(name):
    """libvirt's word for the VM's state, "unknown" when virsh says none or cannot tell; None
    when libvirt has no such domain."""
    r = run("domstate", dom(name))
    if r.returncode == 0:
        return r.stdout.split("\n")[0].strip() or "unknown"
    if "failed to get domain" in r.stderr:
        return None
    return "unknown"


def has_managed_save(name):
    """Whether libvirt holds a managed save of the VM's memory."""
    return bool(re.search(r"^Managed save: *yes", run("dominfo", dom(name)).stdout, re.MULTILINE))


def domain_uuid(name):
    """The UUID of the VM's domain as virsh gives it, None when libvirt has no such domain;
    raises Fail when virsh cannot tell."""
    r = run("domuuid", dom(name))
    if r.returncode != 0:
        if "failed to get domain" in r.stderr:
            return None
        programs.fail(r, f"{name}: virsh", program="virsh", vm=name)
    return r.stdout.strip()


def max_memory_kib(name):
    """The VM's memory limit in KiB, None when virsh has no answer (no such domain), 0 when
    its answer has no number."""
    r = run("dominfo", dom(name))
    if r.returncode != 0:
        return None
    m = re.search(r"^Max memory:\s+(\d+) KiB", r.stdout, re.MULTILINE)
    return int(m.group(1)) if m else 0


def memory_rss_kib(name):
    """The resident memory of the VM's QEMU process in KiB, as the text of each rss line of
    `virsh dommemstat` ("" for a line without a number); empty when virsh has no answer."""
    r = run("dommemstat", dom(name))
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.split("\n"):
        if line.startswith("rss"):
            f = line.split()
            out.append(f[1] if len(f) > 1 else "")
    return out


def running_system_backing(name):
    """The backing file of the System disk as the running VM uses it, or None when
    libvirt does not say. libvirt fixes the chain when it starts the VM; a header
    written later does not change it (#52)."""
    r = run("dumpxml", dom(name))
    if r.returncode != 0:
        return None
    try:
        root = ET.fromstring(r.stdout)
    except ET.ParseError:
        return None
    want = os.path.realpath(footprint.system_disk(name))
    for disk in root.iter("disk"):
        source, target = disk.find("source"), disk.find("target")
        # the System disk: by its file, else by its device (generate: vda)
        if (source is not None and source.get("file") and os.path.realpath(source.get("file")) == want) \
                or (target is not None and target.get("dev") == "vda"):
            backing = disk.find("backingStore/source")
            return backing.get("file") if backing is not None else None
    return None


def network_info(name):
    """The VM's network as libvirt reports it (virsh net-info): (active, autostart), None when
    libvirt has no such network; raises Fail when virsh cannot tell."""
    r = run("net-info", dom(name))
    if r.returncode != 0:
        if "failed to get network" in r.stderr:
            return None
        programs.fail(r, f"{name}: virsh", program="virsh", vm=name)
    info = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
    info = {k.strip(): v.strip() for k, v in info.items()}
    return info.get("Active") == "yes", info.get("Autostart") == "yes"


# --- actions: what Agent Box does to a VM ------------------------------------------------------
# An action gives virsh's answer (returncode, stderr) and the caller decides what a failure means.

def start(name):
    """Start the VM; one with a Pause resumes from it."""
    return run("start", dom(name))


def shutdown(name):
    """Ask the VM's system to shut down."""
    return run("shutdown", dom(name))


def force_off(name):
    """Stop the VM at once, as pulling its plug would."""
    return run("destroy", dom(name))


def pause(name):
    """Pause the VM: its memory goes to a managed save and it stops."""
    return run("managedsave", dom(name))


def drop_pause(name):
    """Discard the VM's Pause (its managed save), so its next start is a cold one."""
    return run("managedsave-remove", dom(name))


def attach_readonly_disk(name, path, device, serial):
    """Attach the raw file at path to the running VM as a read-only virtio disk with this
    target device and serial number."""
    return run("attach-disk", dom(name), path, device, "--live", "--mode", "readonly", "--targetbus", "virtio",
               "--driver", "qemu", "--subdriver", "raw", "--serial", serial)


def detach_disk(name, device):
    """Detach the disk with this target device from the running VM."""
    return run("detach-disk", dom(name), device, "--live")


def disk_devices(name):
    """The target devices of the VM's disks (e.g. "vda"); empty when virsh has no answer."""
    r = run("domblklist", dom(name))
    if r.returncode != 0:
        return []
    return [line.split()[0] for line in r.stdout.splitlines() if line.split()]


def agent_command(name, command, carries_data, seconds, timeout=None):
    """The VM's guest agent runs this command (its JSON text) and has `seconds` for it; one that
    carries data for the VM reaches virsh on its standard input (run_unseen), never as an argument."""
    return (run_unseen if carries_data else run)("qemu-agent-command", dom(name), command,
                                                  "--timeout", str(seconds), timeout=timeout)


# --- definitions: a VM's domain, its network and the storage pool of the disk directory --------
# A definition virsh refuses raises Fail with `what` (the call in the caller's words) in its
# message. A network's without `what` gives virsh's answer, and the caller decides what a
# failure means.

def _checked(what, *args):
    r = run(*args)
    if r.returncode != 0 and what is not None:
        programs.fail(r, what, program="virsh")
    return r


def _define(verb, xml, what):
    """virsh reads a definition from a file: the XML goes to a temporary one."""
    with tempfile.NamedTemporaryFile("w", suffix=".xml") as f:
        f.write(xml)
        f.flush()
        return _checked(what, verb, f.name)


def define_domain(xml, what):
    """Define the VM's domain from its XML, anew when it exists."""
    _define("define", xml, what)


def undefine_domain(name, what):
    """Remove the VM's domain together with its Pause (managed save)."""
    _checked(what, "undefine", dom(name), "--managed-save")


def network_definition(name):
    """The VM's network as libvirt defines it (virsh net-dumpxml), None when virsh has none: its
    "bridge", "address" and "mac" (each None when the definition has none), its "uuid" (or None),
    whether DNS is switched off ("dns_off"), whether it sends anything on ("forwarding": a
    <forward> of packets or a DNS <forwarder>) and the values of its "dnsmasq_options"; the
    "forward_mode" of its <forward>, the "family", "netmask" and "prefix" of its <ip> (each
    None when the definition has none, "" when it is empty) and how many <bridge>, <ip> and
    <forward> elements it has ("elements", by name)."""
    r = run("net-dumpxml", dom(name))
    if r.returncode != 0:
        return None
    xml = r.stdout

    def value(tag, attr, chars="+"):
        m = re.search(rf"<{tag}\b[^>]*\b{attr}=['\"]([^'\"]{chars})", xml)
        return m.group(1) if m else None
    uuid = re.search(r"<uuid>([^<]+)</uuid>", xml)
    return {"bridge": value("bridge", "name"), "address": value("ip", "address"), "mac": value("host", "mac"),
            "uuid": uuid.group(1) if uuid else None,
            "dns_off": bool(re.search(r"<dns\b[^>]*\benable=['\"]no['\"]", xml)),
            "forwarding": bool(re.search(r"<forwarder\b|<forward\b", xml)),
            "dnsmasq_options": re.findall(r"<dnsmasq:option\s+value=['\"]([^'\"]*)['\"]", xml),
            "forward_mode": value("forward", "mode", "*"), "family": value("ip", "family", "*"),
            "netmask": value("ip", "netmask", "*"), "prefix": value("ip", "prefix", "*"),
            "elements": {tag: len(re.findall(rf"<{tag}\b[^>]*>", xml)) for tag in ("bridge", "ip", "forward")}}


def define_network(xml, what=None):
    """Define the VM's network from its XML, anew when it exists."""
    return _define("net-define", xml, what)


def start_network(name, what=None):
    return _checked(what, "net-start", dom(name))


def stop_network(name, what=None):
    return _checked(what, "net-destroy", dom(name))


def autostart_network(name, what):
    """Have libvirt start the VM's network whenever libvirt starts."""
    _checked(what, "net-autostart", dom(name))


def undefine_network(name, what=None):
    return _checked(what, "net-undefine", dom(name))


def pool_on(directory):
    """Whether any storage pool, active or not, has this directory as its target."""
    r = run("pool-list", "--all", "--name")
    for name in r.stdout.split() if r.returncode == 0 else []:
        m = re.search(r"<path>([^<]+)</path>", run("pool-dumpxml", name).stdout)
        if m and m.group(1) == directory:
            return True
    return False


def define_pool(name, directory):
    """Define a directory storage pool of this name on the directory, autostarted and started."""
    xml = f"<pool type='dir'>\n  <name>{name}</name>\n  <target>\n    <path>{directory}</path>\n  </target>\n</pool>\n"
    _define("pool-define", xml, "create storage pool")
    _checked("autostart storage pool", "pool-autostart", name)
    _checked("start storage pool", "pool-start", name)


# --- a build VM: its transient domain -----------------------------------------------------------

def create_transient(name, xml_path):
    """Start the VM as a transient domain from the XML in this file: libvirt keeps no definition
    of it. Raises Fail when virsh refuses it or does not answer within CREATE_TIMEOUT."""
    programs.run([*_COMMAND, "create", xml_path], CREATE_TIMEOUT, what=f"virsh create {dom(name)}", c_locale=True)
