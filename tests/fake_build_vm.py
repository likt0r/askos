#!/usr/bin/env python3
"""Stand-in for a build VM in tests (agentbox/build_vm_run.py): what QEMU and the VM do once
`virsh create` (fake_virsh.py) started the transient domain.

argv: the domain, the fd of its log channel's listening socket (QEMU's, mode=bind), the
domain's XML file (fake_virt_install.py's, which carries virt-install's arguments).

The VM has a root of its own, FAKE_VIRSH_DIR/guest-<domain without "agentbox-">:
  dev/virtio-ports/org.agentbox.build-log  a FIFO whose other end goes to the log channel; the
                                 fake stops reading it while the Host does not read
  dev/disk/by-id/virtio-agentbox-build-input  the input disk, a copy kept in input.tar there
  dev/disk/by-id/virtio-agentbox-scratch      the scratch disk
  run/agentbox-out               a link to the output directory virtiofsd serves (the
                                 --shared-dir of the systemd-run whose socket the domain names)
  seed/                          the files of the seed (fake_xorrisofs.py's JSON), kept
  disk-modes.json                {target: mode} of the disk files the domain names
  usr/bin/                       mkfs.btrfs, mount, umount and restorecon that only record (in calls.log
                                 there) and podman, the Host's fake_podman.py with the VM's own
                                 store and calls (FAKE_DIR is the root); run.json comes from the
                                 Host's test directory, scenario.json from "build_vm" "podman"
It waits for the Host to connect to the log channel, then runs the seed's build-vm-run there as
the VM would (AGENTBOX_BUILD_VM_ROOT), and ends the domain when it ends.

scenario.json "build_vm":
  boots       false: the VM says nothing and runs until destroyed (default true)
  driver      false: no driver runs; the VM writes "lines" and ends (default true)
  lines       lines the VM writes to its log before the driver (bytes as latin-1 text)
  out_files   {name: bytes}: files the VM writes into its output directory first
  out_deep    {"depth": n, "bytes": m}: directories n deep in its output directory, made through
              directory file descriptors as a VM can, with a file of m bytes at the bottom
  out_many    n: empty files in its output directory
  out_space   bytes it allocates (fallocate) in its output directory once the Host reads its log
  stays       true: the domain does not end when the driver ends (as if QEMU hung)
  podman      the scenario of the VM's podman (fake_podman.py), e.g. build_waits, podman_fails
  tamper      after the driver: "symlink" puts a link to a Host file in place of
              image.oci.tar, "hardlink" adds a second link to it
"""
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_virsh  # noqa: E402

DIR = os.environ["FAKE_VIRSH_DIR"]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = "dev/virtio-ports/org.agentbox.build-log"


def option(args, flag, key, where=lambda v: True):
    """key=... of the first `flag` argument where `where` holds (args: one value alone, too)."""
    if len(args) == 1:
        args = [flag, args[0]]
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args) and where(args[i + 1]):
            for part in args[i + 1].split(","):
                k, _, v = part.partition("=")
                if k == key:
                    return v
    return None


def shared_dir(fs_socket):
    with open(os.path.join(DIR, "calls.jsonl")) as f:
        calls = [json.loads(line) for line in f]
    for call in reversed(calls):
        if call["cmd"] == "systemd-run" and f"--socket-property=ListenStream={fs_socket}" in call["argv"]:
            return next(a.split("=", 1)[1] for a in call["argv"] if a.startswith("--shared-dir="))
    raise SystemExit(f"no virtiofsd serves {fs_socket}")


def tool(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)


def prepare(root, args, conf):
    os.makedirs(root, exist_ok=True)
    by_id = os.path.join(root, "dev/disk/by-id")
    os.makedirs(by_id, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.join(root, LOG)), exist_ok=True)
    os.mkfifo(os.path.join(root, LOG))
    serial = lambda s: lambda v: f"serial={s}" in v  # noqa: E731
    disks = {option([a], "--disk", "target.dev"): option([a], "--disk", "path")
             for a in (args[i + 1] for i, a in enumerate(args) if a == "--disk")}
    with open(os.path.join(root, "disk-modes.json"), "w") as f:
        json.dump({t: os.stat(p).st_mode & 0o777 for t, p in disks.items()}, f)
    shutil.copyfile(option(args, "--disk", "path", serial("agentbox-build-input")), os.path.join(root, "input.tar"))
    os.symlink(os.path.join(root, "input.tar"), os.path.join(by_id, "virtio-agentbox-build-input"))
    os.symlink(option(args, "--disk", "path", serial("agentbox-scratch")),
               os.path.join(by_id, "virtio-agentbox-scratch"))
    seed = option(args, "--disk", "path", lambda v: "target.dev=vdd" in v)
    with open(seed) as f:
        iso = json.load(f)
    os.makedirs(os.path.join(root, "seed"))
    for name, content in iso["files"].items():
        with open(os.path.join(root, "seed", name), "w") as f:
            f.write(content)
    with open(os.path.join(root, "seed", "volume"), "w") as f:
        f.write(iso["volume"])
    out = shared_dir(option(args, "--filesystem", "source.socket"))
    os.makedirs(os.path.join(root, "run"), exist_ok=True)
    os.symlink(out, os.path.join(root, "run", "agentbox-out"))
    calls = os.path.join(root, "calls.log")
    for name in ("mkfs.btrfs", "mount", "umount", "restorecon"):
        tool(os.path.join(root, "usr/bin", name), f'echo "{name} $*" >> "{calls}"\n')
    tool(os.path.join(root, "usr/bin/podman"),
         f'FAKE_DIR="{root}" exec "{sys.executable}" "{os.path.join(REPO, "tests", "fake_podman.py")}" "$@"\n')
    if os.path.exists(os.path.join(DIR, "run.json")):
        shutil.copyfile(os.path.join(DIR, "run.json"), os.path.join(root, "run.json"))
    with open(os.path.join(root, "scenario.json"), "w") as f:
        json.dump(conf.get("podman") or {}, f)
    for name, size in (conf.get("out_files") or {}).items():
        with open(os.path.join(out, name), "wb") as f:
            f.write(b"x" * size)
    if conf.get("out_deep"):
        fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
        for _ in range(conf["out_deep"]["depth"]):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        data = os.open("data", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
        os.write(data, b"x" * conf["out_deep"]["bytes"])
        os.close(data)
        os.close(fd)
    for n in range(conf.get("out_many") or 0):
        open(os.path.join(out, f"f{n}"), "w").close()
    return out


def forward(port, conn, driver):
    """The FIFO into the log channel until the driver has ended and said all."""
    while True:
        ready, _, _ = select.select([port], [], [], 0.2)
        if ready:
            data = os.read(port, 65536)
            if data:
                conn.sendall(data)
                continue
        if driver.poll() is not None and not ready:
            return


def main(domain, fd, xml_file):
    listener = socket.socket(fileno=int(fd))
    with open(xml_file) as f:
        xml = f.read()
    from xml.sax.saxutils import unescape
    import re
    args = json.loads(unescape(re.search(r"<args>(.*)</args>", xml, re.S).group(1)))
    conf = fake_virsh.load("scenario.json", {}).get("build_vm") or {}
    root = os.path.join(DIR, "guest-" + domain.removeprefix("agentbox-"))
    out = prepare(root, args, conf)
    conn, _ = listener.accept()
    if conf.get("boots") is False:
        end = time.monotonic() + 120      # until destroyed; never past its test
        while time.monotonic() < end and os.path.isdir(DIR):
            time.sleep(0.5)
        return
    if conf.get("out_space"):
        fd = os.open(os.path.join(out, "space"), os.O_WRONLY | os.O_CREAT, 0o644)
        os.posix_fallocate(fd, 0, conf["out_space"])
        os.close(fd)
    for line in conf.get("lines") or []:
        conn.sendall(line.encode("latin-1"))
    if conf.get("driver", True):
        # O_RDWR: the FIFO never reads as ended before the driver opens it
        port = os.open(os.path.join(root, LOG), os.O_RDWR | os.O_NONBLOCK)
        driver = subprocess.Popen([sys.executable, "-I", os.path.join(root, "seed", "build-vm-run")],
                                  env={"PATH": os.path.join(root, "usr/bin") + ":/usr/bin:/bin", "LANG": "C.UTF-8",
                                       "AGENTBOX_BUILD_VM_ROOT": root}, stdin=subprocess.DEVNULL)
        forward(port, conn, driver)
        with open(os.path.join(root, "driver.exit"), "w") as f:
            f.write(str(driver.returncode))
    archive = os.path.join(out, "image.oci.tar")
    if conf.get("tamper") == "symlink":
        os.remove(archive)
        os.symlink(os.path.join(DIR, "scenario.json"), archive)
    elif conf.get("tamper") == "hardlink":
        os.link(archive, os.path.join(out, "second-link"))
    # power-off: the transient domain is gone, then QEMU's end closes the channel
    if not conf.get("stays"):
        with fake_virsh.changing():
            fake_virsh.end_transient(domain)
    conn.close()
    listener.close()


if __name__ == "__main__":
    main(*sys.argv[1:])
