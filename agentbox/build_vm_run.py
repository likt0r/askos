"""The build VMs of a Build (ADR 0025, #144): two throwaway VMs, one after the other. The first,
online, runs the Image's `podman build` and hands out an image archive; the Host imports and
checks it (images.py); the second, offline (no network device at all), makes the disk from the
image the Host checked and hands out disk.qcow2. build_vm.py checks the image they boot.

Behind an internal switch, off: a Build runs in build VMs only when its run's environment says
AGENTBOX_BUILD_VM=1 (enabled(); for the Build unit `systemctl set-environment
AGENTBOX_BUILD_VM=1`); the Setting build_vm replaces it.

A build VM, named DOMAIN_PREFIX + its id (<image>-<build>-<pass>, the pass "build" or "disk",
PASSES):

  before   /dev/kvm; the checked image (build_vm.current()); MemAvailable of at least MEMORY_MIB;
           room for its output filesystem; for the online pass only: the build network's rules
           loaded in the kernel (as `nft list chain` shows them, rules_problem()) and the network
           NETWORK, defined and started when it is not, refused when it is not the one
           network_xml() describes
  its disks, in BUILD_VM/<id>/ (root; BUILD_VM and it 0711, so QEMU, as qemu, reaches the
           files libvirt hands it), 0600 when made, deleted after it, whatever happened:
             system.qcow2   a copy of the checked image grown to SYSTEM_GIB, no backing file;
                            cloud-init grows the root filesystem into it
             scratch.qcow2  new, empty, sparse, SCRATCH_GIB: the VM's /var/lib/containers
             input.tar      the input disk, raw and read-only (serial INPUT_SERIAL): job.json,
                            for the build pass context.tar (a Recipe's tar as it is, else a tar
                            of the Release's build context), and images/<n>.oci.tar, the images
                            it gets as `skopeo copy` writes them from the Host's storage
             seed.iso       the NoCloud seed (volume cidata, xorrisofs as virt-install writes
                            it): user-data and build-vm-run of the run's copy of the Release
                            (vm/build-vm/), meta-data naming the VM
  its output directory BUILD_VM/out-<id>: the root of a filesystem of its own, so what the VM
           writes there is bounded however fast it writes: an ext4 of the pass's out_bytes in a
           sparse image file BUILD_VM/out-<id>.img (root, 0600), loop-mounted nosuid,nodev,noexec,
           its root USER's (0700) while the VM runs; served as the virtiofs tag OUT_TAG by a
           virtiofsd Agent Box starts, not libvirt's (which runs as root): a transient unit
           (systemd-run) of USER, whose listening socket systemd makes (root:qemu 0660, in
           RUN/<id>, root:qemu 0770) and hands it as fd 3, so root never touches a file USER
           could replace; in its own sandbox (namespace) as the root of a user namespace that
           maps only USER, every guest uid and gid squashed onto it, so every file the VM
           writes is USER's on the Host whatever uid or setuid bit the VM gives it
  the domain: virt-install --print-xml (as every VM, ADR 0004) of MEMORY_MIB, as many vCPUs as
           the Host has but at most MAX_VCPUS, memfd shared memory (virtiofs), no balloon, no
           graphics, no console, no guest agent, the log channel (LOG_CHANNEL, a Unix socket
           QEMU makes in RUN/<id>), the network NETWORK for the online pass and no network
           device at all for the offline one, started with `virsh create`: transient, destroyed
           on power-off, reboot and crash
  its log: the Build run reads the channel line by line, each line cleaned like any text
           from a VM (control characters out, at most LINE_MAX characters) and written with
           "vm: " in front to the run's output, the Build unit's journal, up to MAX_LOG bytes
           (each line counting LINE_OVERHEAD more); podman's STEP lines among them go to the
           caller (seen), which keeps the Build's progress. The Host reads at its own pace: a VM
           that writes faster waits.
  limits:  no line within BOOT_SECONDS of the start (it did not boot), the pass's seconds in
           all, more than its out_bytes, MAX_OUT_ENTRIES files and directories or MAX_OUT_DEPTH
           levels in the output directory, or less than MIN_FREE bytes free under BUILD_VM
           (checked every WATCH_SECONDS): the VM is destroyed and the Build fails with that
           reason
  after:   the domain destroyed if it is still there, virtiofsd's units stopped, the disks
           and RUN/<id> deleted, the output directory taken back by root (0700); the domain
           must be gone and virtiofsd's units inactive, else the Build fails. Then the output
           filesystem is mounted read-only, and root opens exactly result.json and the pass's
           file in it (image.oci.tar or disk.qcow2), never through a link: regular files of USER
           with one link, result.json at most MAX_RESULT, the pass's file at most its cap. The
           caller takes that file (take: the import, or the disk's check and rewrite) while
           nothing can change it; then the output filesystem is unmounted and deleted, whatever
           happened.

An interruption (the run's SIGTERM handler, Ctrl+C) runs the same teardown; after a crash of
the run, the next run removes whatever a build VM left (leftovers()).
"""
import ipaddress
import json
import os
import pwd
import re
import select
import shutil
import socket
import stat
import tarfile
import time

from . import build_vm, paths, programs, settings, state_files, states, virsh
from .errors import Fail
from .registry import dom
from .sanitize import clean

SWITCH = "AGENTBOX_BUILD_VM"
NETWORK_NAME = "build"              # no VM can be named so (list_records.RESERVED_NAMES)
NETWORK = dom(NETWORK_NAME)         # agentbox-build
BRIDGE = "aboxbuild"                # outside the abox-* the VM networks' rules match
ADDRESS = "10.77.0.1"               # no VM network has the number 0
GUEST = "10.77.0.2"
MAC = "52:54:00:77:00:02"
# where a build VM's packets never go: the home network, the VMs, the Host's own and reserved ranges
BLOCKED = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
           "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4")
USER = "agentbox-buildvm"           # rpm/agentbox.sysusers: runs nothing but virtiofsd
NAME_PREFIX = "buildvm-"            # longer than any VM's name with the rest of an id
DOMAIN_PREFIX = dom(NAME_PREFIX)    # agentbox-buildvm-
# a build VM's id: <image>-<build>-<pass>; its work directory, out-<id>, out-<id>.img, RUN/<id>
ID = re.compile(rf"(?:{settings.IMAGE.pattern})-(?:{settings.BUILD_ID.pattern})-(?:build|disk)")
MEMORY_MIB = 8192
MAX_VCPUS = 8
SYSTEM_GIB = 20
SCRATCH_GIB = 40
LOG_CHANNEL = "org.agentbox.build-log"
INPUT_SERIAL = "agentbox-build-input"     # virtio-blk serials: at most 20 characters
SCRATCH_SERIAL = "agentbox-scratch"
OUT_TAG = "agentbox-out"
SEED_LABEL = "cidata"
VIRTIOFSD = "/usr/libexec/virtiofsd"
RUN = paths.host("/run/agentbox/build-vm")
SOURCE = os.path.join("vm", "build-vm")   # the seed's files in the Release
FEDORA_BOOTC = "localhost/agentbox-input/fedora-bootc:{}"   # its name in a build VM, by Build
BUILDER = "localhost/agentbox-input/bootc-image-builder:{}"
BOOT_SECONDS = paths.tunable("AGENTBOX_BUILD_VM_BOOT_SECONDS", 600)
BUILD_SECONDS = paths.tunable("AGENTBOX_BUILD_VM_SECONDS", 4 * 3600)
DISK_SECONDS = paths.tunable("AGENTBOX_BUILD_VM_DISK_SECONDS", 2 * 3600)
WATCH_SECONDS = paths.tunable("AGENTBOX_BUILD_VM_WATCH_SECONDS", 2)
END_SECONDS = paths.tunable("AGENTBOX_BUILD_VM_END_SECONDS", 60)   # from its log's end to its domain's
# the teardown's waits, each bounded; their sum, with every virsh call it makes (virsh.TIMEOUT
# each), is TEARDOWN_SECONDS, which the Build unit's TimeoutStopSec covers (generate.py), so a
# stopped Build destroys its VM before systemd would kill the run
DESTROY_WAIT = 30                   # from destroy to the domain's end
DESTROY_SECONDS = paths.tunable("AGENTBOX_BUILD_VM_DESTROY_SECONDS", DESTROY_WAIT)
STOP_SECONDS = 60                   # systemctl stop of virtiofsd's units
CHECK_SECONDS = 15                  # systemctl is-active of each of them
UMOUNT_SECONDS = 30
VIRSH_SECONDS = 15                  # virsh.TIMEOUT
VIRSH_CALLS = 4                     # in the teardown: state, destroy, state after the wait, state again
TEARDOWN_SECONDS = VIRSH_CALLS * VIRSH_SECONDS + DESTROY_WAIT + STOP_SECONDS + 2 * CHECK_SECONDS + UMOUNT_SECONDS
MAX_OUT_ENTRIES = paths.tunable("AGENTBOX_BUILD_VM_OUT_ENTRIES", 10000)   # files and directories in it
MAX_OUT_DEPTH = 32                  # directories within directories in it
MAX_LOG = paths.tunable("AGENTBOX_BUILD_VM_LOG_BYTES", 64 << 20)          # of its log, kept in the journal
MIN_FREE = paths.tunable("AGENTBOX_BUILD_VM_MIN_FREE_BYTES", 10 << 30)
MAX_ARCHIVE = 64 << 30
MAX_DISK = 160 << 30                # of the disk.qcow2 it hands out, and of its virtual size
# their output filesystems: ext4 with one inode per MiB and a 64 MiB journal leaves 65.86 GiB of
# 66 GiB and 161.83 GiB of 162 GiB free (mkfs.ext4 1.47), so each cap fits with almost 2 GiB to
# spare
OUT_BYTES = paths.tunable("AGENTBOX_BUILD_VM_OUT_BYTES", MAX_ARCHIVE + (2 << 30))
DISK_OUT_BYTES = paths.tunable("AGENTBOX_BUILD_VM_DISK_OUT_BYTES", MAX_DISK + (2 << 30))
OUT_MKFS = ("-m", "0", "-i", "1048576", "-J", "size=64")
LINE_OVERHEAD = 64                  # what a line of its log counts on top of its bytes
MAX_RESULT = 64 << 10
LINE_MAX = 4096
SKOPEO_SECONDS = 3600
# the two passes of a Build: whether the VM has a network device, its time limit, the size of
# its output filesystem, the one file it hands out and that file's cap
PASSES = {
    "build": {"online": True, "seconds": BUILD_SECONDS, "out_bytes": OUT_BYTES, "file": "image.oci.tar",
              "cap": MAX_ARCHIVE, "what": "image archive", "doing": "the podman build runs in the build VM"},
    "disk": {"online": False, "seconds": DISK_SECONDS, "out_bytes": DISK_OUT_BYTES, "file": "disk.qcow2",
             "cap": MAX_DISK, "what": "disk", "doing": "the disk is made in the offline build VM"},
}


class Failed(Exception):
    """A build VM that did not hand out what it was to: the Build fails with this reason."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def enabled():
    """The internal switch: whether a Build runs in build VMs (AGENTBOX_BUILD_VM=1 in the run's
    environment). Off by default; turning it on only moves a Build off the Host."""
    return os.environ.get(SWITCH) == "1"


# --- the network ---------------------------------------------------------------------------------

def network_xml():
    """NAT, IPv4 only, DHCP for the one build VM; libvirt's dnsmasq resolves through the Host's
    resolver. What it may reach is up to the VM network rules (rule_lines)."""
    return f"""<network>
  <name>{NETWORK}</name>
  <forward mode='nat'/>
  <bridge name='{BRIDGE}' stp='on' delay='0'/>
  <ip address='{ADDRESS}' netmask='255.255.255.0'>
    <dhcp>
      <range start='{GUEST}' end='{GUEST}'/>
      <host mac='{MAC}' ip='{GUEST}'/>
    </dhcp>
  </ip>
</network>
"""


def input_jump():
    """The line of the VM network rules' input chain that sends the build network to from_build."""
    return f'    iifname "{BRIDGE}" jump from_build'


def rule_lines():
    """The chains of the build network in the VM network rules (generate.nft_rules): to the
    Host only DHCP and DNS to ADDRESS; nothing forwarded to BLOCKED or over IPv6; nothing
    forwarded to a build VM that it did not ask for. A drop in this table is final whatever
    libvirt's own rules accept."""
    return [
        "  chain from_build {",
        "    meta nfproto ipv6 drop",
        f"    ip daddr {{ {ADDRESS}, 255.255.255.255 }} udp dport 67 accept",
        f"    ip daddr {ADDRESS} udp dport 53 accept",
        f"    ip daddr {ADDRESS} tcp dport 53 accept",
        "    drop",
        "  }",
        "  chain forward {",
        "    type filter hook forward priority -5; policy accept",
        f'    iifname "{BRIDGE}" meta nfproto ipv6 drop',
        f'    iifname "{BRIDGE}" ip daddr {{ {", ".join(BLOCKED)} }} drop',
        f'    oifname "{BRIDGE}" ct state established,related accept',
        f'    oifname "{BRIDGE}" drop',
        "  }",
    ]


def _words(line):
    return " ".join(line.split()).rstrip(";").strip()


def _expected():
    """{chain: (hook or None, [rule])} of the build network's rules as rule_lines() and
    input_jump() give them, in nft's words."""
    chains, name = {"input": ("input", [_words(input_jump())])}, None
    for line in rule_lines():
        words = _words(line)
        if words.startswith("chain "):
            name = words.split()[1]
            chains[name] = (None, [])
        elif words.startswith("type filter hook "):
            chains[name] = (words.split()[3], chains[name][1])
        elif words != "}":
            chains[name][1].append(words)
    return chains


def _interval(element):
    """(first, last) address of an element of an nft set: a.b.c.d, a.b.c.d/n or a-b."""
    if "-" in element:
        first, last = element.split("-", 1)
        return int(ipaddress.IPv4Address(first.strip())), int(ipaddress.IPv4Address(last.strip()))
    net = ipaddress.IPv4Network(element.strip(), strict=False)
    return int(net.network_address), int(net.broadcast_address)


def _same_set(found, wanted):
    """Whether the set rule `found` (as nft lists it, which may merge adjacent ranges) is the
    rule `wanted`, its set holding exactly the same addresses: no fewer (a drop that lets some
    through) and no more (an accept that lets more in)."""
    head, _, rest = found.partition("{")
    elements, _, tail = rest.partition("}")
    w_head, _, w_rest = wanted.partition("{")
    w_elements, _, w_tail = w_rest.partition("}")
    if head.strip() != w_head.strip() or tail.strip() != w_tail.strip():
        return False
    try:
        have = sorted(_interval(e) for e in elements.split(",") if e.strip())
    except ValueError:
        return False
    return _merged(have) == _merged(sorted(_interval(e) for e in w_elements.split(",")))


def _merged(intervals):
    merged = []
    for first, last in intervals:
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def rules_problem(listings):
    """What is missing from the build network's rules in the kernel, from `nft list chain inet
    agentbox_vm <chain>` of each chain ({chain: its output, None when nft found no such chain});
    None when every one is there as it should be. The input chain must hold its jump; from_build
    and forward exactly their rules in their order, forward hooked into forwarding."""
    for chain, (hook, rules) in _expected().items():
        text = listings.get(chain)
        if text is None:
            return f"the chain {chain} is not loaded"
        lines = [_words(l) for l in text.splitlines()]
        lines = [l for l in lines if l and l != "}" and not l.startswith(("table ", "chain "))]
        if hook and not any(l.startswith(f"type filter hook {hook} ") for l in lines):
            return f"the chain {chain} is not hooked into {hook}"
        got = [l for l in lines if not l.startswith("type ")]
        if chain == "input":
            if rules[0] not in got:
                return "the input chain does not send the build network to from_build"
            continue
        if len(got) != len(rules) or not all(g == w or "{" in w and _same_set(g, w) for g, w in zip(got, rules)):
            return f"the chain {chain} is not the build network's"
    return None


def _rules():
    """The build network's rules are loaded in the kernel (not only written): nft lists them."""
    listings = {}
    for chain in _expected():
        try:
            r = programs.run(["nft", "list", "chain", "inet", "agentbox_vm", chain], 30, c_locale=True, ok=None)
        except Fail as f:
            raise Failed(f"the build network's rules cannot be read: {f.message}")
        listings[chain] = r.stdout if r.returncode == 0 else None
    why = rules_problem(listings)
    if why:
        raise Failed(f"the build network's rules are not loaded in the kernel: {why} "
                     "(sudo agentbox regenerate; systemctl restart agentbox-vmnet)")


def _network():
    """NETWORK defined and started when it is not; an existing one must be network_xml()'s."""
    try:
        found = states.network(NETWORK_NAME)
    except Fail as f:
        raise Failed(f.message)
    if found is None:
        _virsh_file("net-define", network_xml(), f"define the network {NETWORK}")
        active = False
    else:
        xml = virsh.run("net-dumpxml", NETWORK).stdout
        tags = {tag: re.findall(rf"<{tag}\b[^>]*>", xml) for tag in ("bridge", "ip", "forward")}

        def attr(tag, name):
            m = re.search(rf"\b{name}=['\"]([^'\"]*)", tags[tag][0])
            return m.group(1) if m else None
        if any(len(found) != 1 for found in tags.values()) or (attr("bridge", "name"), attr("ip", "address"),
                                                                 attr("forward", "mode")) != (BRIDGE, ADDRESS, "nat") \
                or attr("ip", "family") not in (None, "ipv4") \
                or (attr("ip", "netmask"), attr("ip", "prefix")) not in (("255.255.255.0", None), (None, "24")):
            raise Failed(f"the network {NETWORK} is not the build network (bridge {BRIDGE}, one address, "
                         f"{ADDRESS}/24, NAT, IPv4 only): remove it (virsh net-destroy, net-undefine)")
        active = found[0]
    if not active:
        r = virsh.run("net-start", NETWORK)
        if r.returncode != 0:
            raise Failed(f"cannot start the network {NETWORK}: {programs.summary(r)}")


def _virsh_file(cmd, xml, what):
    """virsh <cmd> <file> with xml in a temporary file."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".xml") as f:
        f.write(xml)
        f.flush()
        r = virsh.run(cmd, f.name)
    if r.returncode != 0:
        raise Failed(f"cannot {what}: {programs.summary(r)}")


# --- before it starts ----------------------------------------------------------------------------

def _memory_available():
    """MemAvailable in MiB, or None."""
    try:
        with open(paths.host("/proc/meminfo")) as f:
            m = re.search(r"^MemAvailable:\s+(\d+) kB$", f.read(), re.MULTILINE)
    except OSError:
        m = None
    return int(m.group(1)) // 1024 if m else None


def _host_ready(online):
    """The checked image a build VM boots, once the Host can run one; an online one only with the
    build network's rules loaded and the network there."""
    if not os.path.exists(paths.host("/dev/kvm")):
        raise Failed("the Host has no KVM (/dev/kvm): no build VM can run")
    image = build_vm.current()
    if not image:
        raise Failed("there is no checked build VM image yet (the version check fetches and checks it: "
                     "systemctl start agentbox-versions)")
    available = _memory_available()
    if available is None or available < MEMORY_MIB:
        raise Failed(f"not enough free memory for a build VM: it needs {MEMORY_MIB >> 10} GiB, "
                     f"the Host has {'an unknown amount' if available is None else f'{available} MiB'} available")
    if online:
        _rules()
        _network()
    return image


def vcpus():
    return max(1, min(os.cpu_count() or 1, MAX_VCPUS))


def ids():
    """(uid, gid) of USER; without root (tests) the caller's own, the only ones it can give."""
    if os.geteuid() != 0:
        return os.getuid(), os.getgid()
    try:
        pw = pwd.getpwnam(USER)
    except KeyError:
        raise Failed(f"the user {USER} is missing (the package creates it)")
    return pw.pw_uid, pw.pw_gid


def _owned(tarinfo):
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = "root"
    return tarinfo


def _tar_of(folder, path):
    """A tar of folder's content (its entries at the top, links as links) at path."""
    with tarfile.open(path, "x", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(os.listdir(folder)):
            tar.add(os.path.join(folder, name), arcname=name, filter=_owned)


def _add_bytes(tar, name, data):
    import io
    info = _owned(tarfile.TarInfo(name))
    info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _input_disk(vm, job, context, hand_in, say):
    """The input disk: job.json, context.tar (none when context is None) and the images handed
    in, from the Host's storage by skopeo (`hand_in`: [(reference in the Host's storage, name in
    the VM)])."""
    images = []
    for n, (ref, name) in enumerate(hand_in):
        archive = os.path.join(vm["work"], f"image-{n}.oci.tar")
        say(f"Handing in {ref} as {name}")
        try:
            programs.run(["skopeo", "copy", "--quiet", f"containers-storage:{ref}", f"oci-archive:{archive}:{name}"],
                         SKOPEO_SECONDS, what=f"skopeo copy of {ref}")
        except Fail as f:
            raise Failed(f.message)
        images.append((archive, {"archive": f"images/{n}.oci.tar", "name": name}))
        _room(vm)
    job = {**job, "images": [entry for _, entry in images]}
    with tarfile.open(vm["input"], "x", format=tarfile.PAX_FORMAT) as tar:
        _add_bytes(tar, "job.json", json.dumps(job, indent=2).encode())
        if isinstance(context, (bytes, bytearray)):
            _add_bytes(tar, "context.tar", bytes(context))
        elif context is not None:
            nested = os.path.join(vm["work"], "context.tar")
            _tar_of(context, nested)
            tar.add(nested, arcname="context.tar", filter=_owned)
            os.remove(nested)
        for archive, entry in images:
            tar.add(archive, arcname=entry["archive"], filter=_owned)
            os.remove(archive)


def _seed(vm, src):
    """The NoCloud seed: the Release's user-data and driver, and meta-data naming the VM."""
    folder = os.path.join(vm["work"], "seed")
    os.mkdir(folder, 0o700)
    for name in ("user-data", "build-vm-run"):
        try:
            shutil.copyfile(os.path.join(src, SOURCE, name), os.path.join(folder, name))
        except OSError as e:
            raise Failed(f"{os.path.join(SOURCE, name)} of the Release cannot be read: {e.strerror or e}")
    with open(os.path.join(folder, "meta-data"), "w") as f:
        f.write(f"instance-id: {vm['domain']}\nlocal-hostname: agentbox-build\n")
    _run(["xorrisofs", "-quiet", "-o", vm["seed"], "-J", "-input-charset", "utf8", "-rational-rock",
          "-V", SEED_LABEL, folder], 120, "xorrisofs of the seed")
    shutil.rmtree(folder)


def _run(argv, timeout, what):
    try:
        return programs.run(argv, timeout, what=what, c_locale=True)
    except Fail as f:
        raise Failed(f.message)


def _prepare(vm, image, job, context, hand_in, src, ids, say):
    """The VM's disks, its output directory and its run directory, all new. The disks are root's
    alone (0600, libvirt hands them to QEMU while the VM runs): the input disk holds a Recipe."""
    state_files.directory(paths.BUILD_VM, 0o711)
    _drop_out(vm, say)
    _room(vm)
    for path in (vm["work"], vm["run"]):
        shutil.rmtree(path, ignore_errors=True)
    state_files.directory(vm["work"], 0o711)
    umask = os.umask(0o077)
    try:
        _disks(vm, image, job, context, hand_in, src, say)
    finally:
        os.umask(umask)
    _out_fs(vm, ids, say)
    qemu_gid = state_files.group_id("qemu", "is libvirt installed?")
    state_files.directory(os.path.dirname(RUN), 0o755)
    state_files.directory(RUN, 0o755)
    state_files.directory(vm["run"], 0o770, qemu_gid)


def _free():
    """Bytes free on the filesystem of BUILD_VM, where a build VM's files grow."""
    st = os.statvfs(paths.BUILD_VM)
    return st.f_bavail * st.f_frsize


def _room(vm):
    """Room left for the build VM's output filesystem, all of it, and MIN_FREE besides: before
    anything is written, after each thing handed in, and before the filesystem is made."""
    if _free() < vm["out_bytes"] + MIN_FREE:
        raise Failed(f"not enough free space under {paths.BUILD_VM} for a build VM and its output: it needs "
                     f"{_size(vm['out_bytes'] + MIN_FREE)} more")


def _out_fs(vm, ids, say):
    """The output directory, the root of a filesystem of its own of the pass's out_bytes (ext4 in
    a sparse image file, loop-mounted nosuid,nodev,noexec), USER's: virtiofs has no quota, this
    bounds what the VM writes whatever it does between two checks. There must be room for all of
    it."""
    _room(vm)
    fd = os.open(vm["out_image"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.ftruncate(fd, vm["out_bytes"])
    finally:
        os.close(fd)
    uid, gid = ids
    _run(["mkfs.ext4", "-q", "-F", *OUT_MKFS, "-E", f"root_owner={uid}:{gid}", vm["out_image"]], 300,
         "mkfs.ext4 of the output filesystem")
    os.mkdir(vm["out"], 0o700)
    _run(["mount", "-t", "ext4", "-o", "loop,nosuid,nodev,noexec", vm["out_image"], vm["out"]], 60,
         "mount of the output filesystem")
    os.chown(vm["out"], uid, gid)
    os.chmod(vm["out"], 0o700)


def _drop_out(vm, say):
    """The output filesystem unmounted, its directory and image file deleted. One that cannot be
    unmounted is said so and kept, for the next Build run to try again (leftovers)."""
    if os.path.isdir(vm["out"]) and not os.path.islink(vm["out"]):
        try:
            r = programs.run(["umount", vm["out"]], UMOUNT_SECONDS, ok=None, c_locale=True)
            failed = programs.summary(r) if r.returncode != 0 else None
        except Fail as f:
            failed = f.message
        if failed:
            say(f"umount of {vm['out']} failed: {failed}")
        if os.path.ismount(vm["out"]):
            say(f"The output filesystem {vm['out']} stays mounted; the next Build run tries again")
            return
        shutil.rmtree(vm["out"], ignore_errors=True)
    state_files.remove(vm["out_image"], fail=False)


def _disks(vm, image, job, context, hand_in, src, say):
    # a copy of its own, not an overlay: libvirt hands every file of a disk's chain to QEMU's
    # user and leaves a backing file so, which would be the checked image every build VM boots
    shutil.copyfile(image, vm["system"])
    _run(["qemu-img", "resize", "-q", "-f", "qcow2", vm["system"], f"{SYSTEM_GIB}G"], 120,
         "qemu-img resize of the system disk")
    _run(["qemu-img", "create", "-q", "-f", "qcow2", vm["scratch"], f"{SCRATCH_GIB}G"], 120,
         "qemu-img create of the scratch disk")
    _input_disk(vm, job, context, hand_in, say)
    _room(vm)
    _seed(vm, src)


def virtiofsd_argv(vm, ids):
    """systemd-run of virtiofsd as USER, socket-activated: systemd makes the socket (root:qemu
    0660) and hands it over as fd 3.

    Unprivileged, virtiofsd's namespace sandbox makes a user namespace of its own; --uid-map and
    --gid-map map USER, and only it, to root there (virtiofsd README, "--uid-map ... map your own
    UID to a single UID in the namespace: --uid-map=:0:1000:1: would map UID 1000 to root's UID in
    the namespace"; src/sandbox.rs, setup_id_mappings writes such a single own-id map itself, no
    newuidmap, and enter_namespace then becomes root in the namespace). Its translation squashes
    every guest uid and gid onto the namespace's 0, which is USER on the Host. Without the maps it
    maps USER to itself and cannot become root in the namespace ("Couldn't set the process uid as
    root", virtiofsd 1.14)."""
    uid, gid = ids
    return ["systemd-run", "--quiet", "--collect", f"--unit={vm['unit']}",
            f"--socket-property=ListenStream={vm['fs_socket']}", "--socket-property=SocketUser=root",
            "--socket-property=SocketGroup=qemu", "--socket-property=SocketMode=0660",
            f"--property=User={USER}", f"--property=Group={USER}", "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict", f"--property=ReadWritePaths={vm['out']}", "--property=ProtectHome=yes",
            "--property=PrivateTmp=yes", "--property=RestrictAddressFamilies=AF_UNIX", "--property=MemoryMax=2G",
            "--", VIRTIOFSD, "--fd=3", f"--shared-dir={vm['out']}", "--sandbox=namespace",
            f"--uid-map=:0:{uid}:1:", f"--gid-map=:0:{gid}:1:",
            "--translate-uid=squash-guest:0:0:4294967295", "--translate-gid=squash-guest:0:0:4294967295"]


def virt_install_args(vm):
    disk = "path={},format=qcow2,bus=virtio,target.dev={},cache=none,io=native,discard=unmap"
    return [
        "--connect", "qemu:///system", "--print-xml", "--dry-run", "--check", "all=off",
        "--name", vm["domain"], "--osinfo", "linux2024", "--memory", str(MEMORY_MIB), "--vcpus", str(vcpus()),
        "--cpu", "host-passthrough", "--machine", "q35", "--boot", "hd", "--import", "--noautoconsole",
        "--graphics", "none", "--video", "none", "--sound", "none", "--input", "none", "--console", "none",
        "--controller", "usb,model=none",
        "--events", "on_poweroff=destroy,on_reboot=destroy,on_crash=destroy",
        "--features", "acpi=on,apic=on",
        "--memorybacking", "source.type=memfd,access.mode=shared",
        "--disk", disk.format(vm["system"], "vda"),
        "--disk", disk.format(vm["scratch"], "vdb") + f",serial={SCRATCH_SERIAL}",
        "--disk", f"path={vm['input']},format=raw,bus=virtio,target.dev=vdc,readonly=on,serial={INPUT_SERIAL}",
        "--disk", f"path={vm['seed']},format=raw,bus=virtio,target.dev=vdd,readonly=on",
        "--filesystem", f"type=mount,driver.type=virtiofs,source.socket={vm['fs_socket']},target.dir={OUT_TAG}",
        # the offline pass has no network device at all (virt-install adds one unless told none)
        "--network", f"network={NETWORK},mac={MAC},model=virtio" if vm["online"] else "none",
        "--channel", f"unix,mode=bind,path={vm['log_socket']},target.type=virtio,target.name={LOG_CHANNEL}",
        "--memballoon", "none",
        "--rng", "/dev/urandom",
    ]


def _start(vm, ids, say):
    """virtiofsd, then the transient domain."""
    _run(virtiofsd_argv(vm, ids), 60, "systemd-run of virtiofsd")
    xml = _run(["virt-install", *virt_install_args(vm)], 120, "virt-install").stdout
    path = os.path.join(vm["work"], "domain.xml")
    with open(path, "w") as f:
        f.write(xml)
    _run(["virsh", "-q", "-c", "qemu:///system", "create", path], 120, f"virsh create {vm['domain']}")
    say(f"== Build VM {vm['domain']} started ({MEMORY_MIB >> 10} GiB, {vcpus()} vCPUs, "
        + (f"network {NETWORK})" if vm["online"] else "no network device)"))


# --- while it runs -------------------------------------------------------------------------------

def _gone(vm):
    """Whether libvirt no longer has the (transient) domain."""
    return states.gone(vm["name"])


def _held(folder):
    """Bytes allocated under folder, through directory file descriptors (no path grows with the
    depth), links not followed; Failed when it holds more than MAX_OUT_ENTRIES files and
    directories, directories deeper than MAX_OUT_DEPTH, or cannot be measured. An entry that
    goes while it is read counts no more."""
    total, count = 0, 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def walk(fd, depth):
        nonlocal total, count
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > MAX_OUT_ENTRIES:
                    raise Failed(f"the build VM made more than {MAX_OUT_ENTRIES} files and directories in its "
                                 "output directory")
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                total += st.st_blocks * 512
                if not stat.S_ISDIR(st.st_mode):
                    continue
                if depth >= MAX_OUT_DEPTH:
                    raise Failed(f"the build VM nested directories deeper than {MAX_OUT_DEPTH} in its output "
                                 "directory")
                try:
                    child = os.open(entry.name, flags, dir_fd=fd)
                except FileNotFoundError:
                    continue
                try:
                    walk(child, depth + 1)
                finally:
                    os.close(child)

    try:
        top = os.open(folder, flags)
        try:
            walk(top, 1)
        finally:
            os.close(top)
    except OSError as e:
        raise Failed(f"the build VM's output directory cannot be measured: {e.strerror or e}")
    return total


def _size(n):
    return f"{n >> 30} GiB" if n >= 1 << 30 and not n % (1 << 30) else f"{n} bytes"


def _watch(vm):
    if _free() < MIN_FREE:
        raise Failed(f"less than {_size(MIN_FREE)} free under {paths.BUILD_VM}")
    if _held(vm["out"]) > vm["out_bytes"]:
        raise Failed(f"the build VM wrote more than {_size(vm['out_bytes'])} into its output directory")


def _connect(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError:
        s.close()
        return None
    return s


def _emit(raw, say, seen, keep=True):
    text = clean(raw.decode("utf-8", "replace").rstrip("\r"), False)[:LINE_MAX]
    if keep:
        say("vm: " + text)
    try:
        seen(text)
    except Exception as e:           # the progress is lost, never the log
        say(f"agentbox: the Build's progress could not be saved: {e}")


def _log(line, logged, say, seen):
    """One line of the VM's log into the journal while its log is within MAX_LOG bytes, each line
    counting LINE_OVERHEAD more (so empty lines count), then one note and no more lines (their
    STEP lines still count); the bytes so far."""
    logged += len(line) + LINE_OVERHEAD
    if MAX_LOG < logged <= MAX_LOG + len(line) + LINE_OVERHEAD:
        say(f"agentbox: the build VM wrote more than {_size(MAX_LOG)} to its log; its further lines are not kept")
    _emit(line, say, seen, keep=logged <= MAX_LOG)
    return logged


def _relay(vm, say, seen):
    """The log channel into the run's output until QEMU closes it (the VM ended), within the
    limits; Failed when one is reached."""
    started, limit = time.monotonic(), vm["seconds"]
    conn, heard, pending, watched, logged = None, False, b"", 0.0, 0
    try:
        while True:
            now = time.monotonic()
            if not heard and now - started > BOOT_SECONDS:
                raise Failed(f"the build VM did not boot: no line on its log channel within {BOOT_SECONDS} s")
            if now - started > limit:
                raise Failed(f"the build VM's time limit of {limit} s is exceeded")
            if now - watched >= WATCH_SECONDS:
                watched = now
                _watch(vm)
                if conn is None and _gone(vm):
                    break
            if conn is None:
                conn = _connect(vm["log_socket"])
                if conn is None:
                    time.sleep(0.2)
                    continue
            ready, _, _ = select.select([conn], [], [], 1)
            if not ready:
                continue
            data = conn.recv(65536)
            if not data:
                break
            heard = True
            pending += data
            while True:
                cut = pending.find(b"\n")
                if cut < 0 and len(pending) < LINE_MAX:
                    break
                cut = cut if 0 <= cut < LINE_MAX else LINE_MAX
                line, pending = pending[:cut], pending[cut + 1 if pending[cut:cut + 1] == b"\n" else cut:]
                logged = _log(line, logged, say, seen)
        if pending:
            _log(pending, logged, say, seen)
    finally:
        if conn is not None:
            conn.close()
    deadline = time.monotonic() + END_SECONDS
    while not _gone(vm):
        if time.monotonic() > deadline:
            say(f"The build VM's log ended but its domain is still there: destroying {vm['domain']}")
            break
        time.sleep(0.5)


# --- afterwards ----------------------------------------------------------------------------------

def _stop(unit):
    """virtiofsd's units stopped; whatever goes wrong is ignored (they may be gone already)."""
    try:
        programs.systemctl("stop", f"{unit}.socket", f"{unit}.service", timeout=STOP_SECONDS, ok=None)
    except Fail:
        pass


def _end(vm):
    """The domain destroyed, virtiofsd stopped, the disks and the run directory deleted, the
    output directory taken back by root: whatever happened. What still runs afterwards (the
    domain, whose QEMU libvirt reports gone only once it ended, or virtiofsd's units, as systemd
    says, or cannot say): [what], for the caller to fail the Build on."""
    if not _gone(vm):
        virsh.run("destroy", vm["domain"])
        deadline = time.monotonic() + DESTROY_SECONDS
        while not _gone(vm) and time.monotonic() < deadline:
            time.sleep(0.5)
    _stop(vm["unit"])
    left = [] if _gone(vm) else [f"the domain {vm['domain']}"]
    left += [unit for unit in (f"{vm['unit']}.service", f"{vm['unit']}.socket")
             if not states.unit_stopped(unit, CHECK_SECONDS)]
    shutil.rmtree(vm["work"], ignore_errors=True)
    shutil.rmtree(vm["run"], ignore_errors=True)
    if os.path.isdir(vm["out"]) and not os.path.islink(vm["out"]):
        if os.geteuid() == 0:
            os.chown(vm["out"], 0, 0)
        os.chmod(vm["out"], 0o700)
    return left


def _handed_out(dir_fd, name, cap, uid):
    """The open file `name` of the output directory: a regular file of uid with one link, at most
    cap bytes, never through a link; None otherwise."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=dir_fd)
    except OSError:
        return None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1 or st.st_size > cap:
        os.close(fd)
        return None
    return fd


def _outcome(vm, uid):
    """The file the VM handed out (the pass's file): its path; Failed with the VM's reason
    otherwise."""
    try:
        dir_fd = os.open(vm["out"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise Failed(f"the build VM's output directory cannot be opened: {e.strerror or e}")
    try:
        fd = _handed_out(dir_fd, "result.json", MAX_RESULT, uid)
        if fd is None:
            raise Failed("the build VM ended without a result (result.json; see its lines in this log)")
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_RESULT + 1)
        try:
            result = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            result = None
        if not isinstance(result, dict) or result.get("status") not in ("ok", "failed"):
            raise Failed("the build VM's result.json is not one")
        if result["status"] != "ok":
            why = result.get("error") if isinstance(result.get("error"), str) else "no reason given"
            raise Failed("in the build VM: " + clean(" ".join(why.split()), False)[:300])
        fd = _handed_out(dir_fd, vm["file"], vm["cap"], uid)
        if fd is None:
            raise Failed(f"the build VM handed out no {vm['what']} ({vm['file']}: a regular file of {USER} "
                         f"with one link, at most {vm['cap'] >> 30} GiB)")
        os.close(fd)
    finally:
        os.close(dir_fd)
    return os.path.join(vm["out"], vm["file"])


def names(image, build, step="build"):
    """The paths and names of the build VM of a Build's pass (PASSES), and the pass's values."""
    vm = _named(f"{image}-{build}-{step}")
    work, run = vm["work"], vm["run"]
    return {**vm, **PASSES[step], "system": os.path.join(work, "system.qcow2"),
            "scratch": os.path.join(work, "scratch.qcow2"), "input": os.path.join(work, "input.tar"),
            "seed": os.path.join(work, "seed.iso"), "log_socket": os.path.join(run, "log.sock"),
            "fs_socket": os.path.join(run, "virtiofs.sock")}


def _named(ident):
    """The domain, virtiofsd's unit and the directories of the build VM `ident`."""
    return {"id": ident, "name": NAME_PREFIX + ident, "domain": DOMAIN_PREFIX + ident, "unit": DOMAIN_PREFIX + ident,
            "work": os.path.join(paths.BUILD_VM, ident), "out": os.path.join(paths.BUILD_VM, f"out-{ident}"),
            "out_image": os.path.join(paths.BUILD_VM, f"out-{ident}.img"), "run": os.path.join(RUN, ident)}


def _pass(image, build, step, job, context, hand_in, src, say, seen, take):
    """One build VM of the Build, its pass `step`, to its end: take(path) of the file it handed
    out, while its output filesystem is mounted read-only and nothing runs that could change it;
    take's answer. The output filesystem is unmounted and deleted afterwards, whatever happened."""
    vm = names(image, build, step)
    image_file = _host_ready(vm["online"])
    uids = ids()
    say(f"== {image}: {vm['doing']} {vm['domain']} (image {os.path.basename(image_file)})")
    try:
        try:
            _prepare(vm, image_file, {"job": step, "image": image, "build": build, **job}, context, hand_in,
                     src, uids, say)
            _start(vm, uids, say)
            _relay(vm, say, seen)
        except OSError as e:
            raise Failed(f"build VM: {e.strerror or e}" + (f" ({e.filename})" if e.filename else ""))
        finally:
            left = _end(vm)
            if left:
                say(f"Still running after the build VM's teardown: {', '.join(left)}")
        if left:
            # the VM or virtiofsd could still change what it handed out: nothing of it is taken
            raise Failed(f"{', '.join(left)} still runs after the build VM's teardown; its output is not taken")
        # nothing changes what it handed out from here on
        _run(["mount", "-o", "remount,ro,nosuid,nodev,noexec", vm["out"]], 60,
             "read-only mount of the output filesystem")
        path = _outcome(vm, uids[0])
        say(f"== {image}: the build VM handed out its {vm['what']} ({path})")
        return take(path)
    finally:
        _drop_out(vm, say)


def import_dir(image, build):
    """Where skopeo unpacks the first build VM's image archive while the Host imports it: on the
    filesystem of BUILD_VM, root's (0700), deleted after the import and by leftovers()."""
    return os.path.join(paths.BUILD_VM, f"import-{image}-{build}")


def build_image(image, build, job, context, hand_in, src, say, seen, take):
    """podman build in the first build VM of the Build, online: take(path) of the image archive
    it handed out (the import), whose answer this returns; the archive and its output directory
    are deleted after it. job: tag, build_args ([[name, value]]) and containerfile (in the
    context; None: the context on stdin). context: the tar of a Recipe (bytes) or a directory.
    hand_in: [(reference in the Host's storage, name in the VM)]. src: the run's copy of the
    Release. seen(line): each line of the VM's log. Failed when the build VM cannot run, or ends
    without the archive."""
    return _pass(image, build, "build", job, context, hand_in, src, say, seen, take)


def make_disk(image, build, job, hand_in, src, say, take):
    """bootc-image-builder in the second build VM of the Build, offline (no network device):
    take(path) of the disk.qcow2 it handed out (the Host's check and rewrite), whose answer this
    returns; the disk and its output directory are deleted after it. job: tag (the image the
    Host checked), builder and buildroot (bootc-image-builder and fedora-bootc), by their names
    in the VM, and config (bootc-image-builder's config.toml). hand_in: those three images, as
    for build_image. Failed when the build VM cannot run, or ends without the disk."""
    return _pass(image, build, "disk", job, None, hand_in, src, say, lambda line: None, take)


def leftovers(say):
    """Holding the run lock, before a Build: what a build VM left when its run ended without its
    teardown (killed, the Host's crash), or an output filesystem that could not be unmounted:
    its domain destroyed, its virtiofsd stopped, its output filesystem unmounted, its disks,
    output and run directories deleted; the directory of an import (import_dir) deleted. The
    checked image and its record stay."""
    imports = re.compile(rf"import-(?:{settings.IMAGE.pattern})-(?:{settings.BUILD_ID.pattern})")
    for name in state_files.listdir(paths.BUILD_VM, fail=False):
        if imports.fullmatch(name):
            shutil.rmtree(os.path.join(paths.BUILD_VM, name), ignore_errors=True)
            say(f"Removed what the import {name} left")
    found = set()
    # each form of a name read on its own: a work directory of the Image out-x is also the name
    # of an output directory of the Image x; both are taken, what does not exist costs nothing
    for name in state_files.listdir(paths.BUILD_VM, fail=False):
        for pattern in (ID, re.compile(rf"out-({ID.pattern})"), re.compile(rf"out-({ID.pattern})\.img")):
            m = pattern.fullmatch(name)
            if m:
                found.add(m.group(1) if pattern.groups else name)
    found |= {name for name in state_files.listdir(RUN, fail=False) if ID.fullmatch(name)}
    for ident in sorted(found):
        vm = _named(ident)
        there = [p for p in (vm["work"], vm["out"], vm["out_image"], vm["run"]) if os.path.lexists(p)]
        if not _gone(vm):
            there.append(vm["domain"])
            virsh.run("destroy", vm["domain"])
        if not there:
            continue
        _stop(vm["unit"])     # what still runs is removed by the run after it
        _drop_out(vm, say)
        for path in (vm["work"], vm["run"]):
            shutil.rmtree(path, ignore_errors=True)
        say(f"Removed what the build VM {vm['domain']} left")
