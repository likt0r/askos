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

scenario.json "build_vm" (the first build VM of a Build, <image>-<build>-build) and "disk_vm"
(the second, <image>-<build>-disk), each:
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
  tamper      after the driver: "symlink" puts a link to a Host file in place of the file it
              hands out (image.oci.tar, disk.qcow2), "hardlink" adds a second link to it; of
              image.oci.tar (fake_podman.edit_archive): "second_image" names a second image in its
              index, "layer_urls" drops its layer and names a URL to fetch it from instead,
              "many_layers" gives it 129 layers, "media_type" a layer type that is no OCI layer's,
              "link_member" adds a symbolic link to it, "sparse_member" a GNU sparse member,
              "pax_sparse" a PAX sparse member, "extra_member" a harmless file of no image's,
              "hidden_index" a V7 header Python's tarfile stops at (non-octal devmajor) and
              after it a second index.json naming another manifest, which Go's archive/tar reads
  names       after the driver: image.oci.tar names this image instead of the one podman saved
  disk_meta   after the driver: these keys of a fake disk (fake_qemu_img.py) added to disk.qcow2,
              e.g. {"backing-filename": "/etc/shadow"}
"""
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FAKE_DIR", os.environ["FAKE_VIRSH_DIR"])
import fake_podman  # noqa: E402
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


def v7_header(name, data, garbage):
    """A V7 tar header (no ustar magic) of a file, and its data; with garbage, non-octal bytes in
    the devmajor field, which Go's archive/tar ignores in a V7 header and Python's tarfile takes
    for an invalid header, and so for the end of the archive."""
    h = bytearray(512)
    h[0:len(name)] = name.encode()
    h[100:108], h[108:116], h[116:124] = b"0000644\0", b"0000000\0", b"0000000\0"
    h[124:136], h[136:148] = ("%011o\0" % len(data)).encode(), b"00000000000\0"
    h[148:156], h[156] = b"        ", ord("0")
    if garbage:
        h[329:337] = b"zzzzzzz\0"
    h[148:156] = ("%06o\0 " % sum(h)).encode()
    return bytes(h) + data + b"\0" * (-len(data) % 512)


def hide_index(archive):
    """After the archive's last entry: a header Python stops at, then a second index.json."""
    index, _ = fake_podman.read_oci(archive)
    index["manifests"][0]["digest"] = "sha256:" + "1" * 64
    with tarfile.open(archive) as tar:
        tar.getmembers()
        end = tar.offset                 # where its end-of-archive blocks begin
    with open(archive, "rb") as f:
        raw = f.read()[:end]
    raw += v7_header("junk", b"", True) + v7_header("index.json", json.dumps(index).encode(), False) + b"\0" * 1024
    with open(archive, "wb") as f:
        f.write(raw)


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
    disk = domain.endswith("-disk")
    conf = fake_virsh.load("scenario.json", {}).get("disk_vm" if disk else "build_vm") or {}
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
    handed = os.path.join(out, "disk.qcow2" if disk else "image.oci.tar")
    if conf.get("tamper") == "symlink":
        os.remove(handed)
        os.symlink(os.path.join(DIR, "scenario.json"), handed)
    elif conf.get("tamper") == "hardlink":
        os.link(handed, os.path.join(out, "second-link"))
    elif conf.get("tamper") == "second_image":
        fake_podman.edit_archive(handed, index=lambda i: i["manifests"].append(dict(i["manifests"][0])))
    elif conf.get("tamper") == "layer_urls":
        fake_podman.edit_archive(handed, drop_layers=True,
                                 manifest=lambda m: m["layers"][0].update(urls=["http://127.0.0.1:18765/layer"]))
    elif conf.get("tamper") == "many_layers":
        fake_podman.edit_archive(handed, manifest=lambda m: m.update(layers=m["layers"] * 129))
    elif conf.get("tamper") == "media_type":
        fake_podman.edit_archive(handed, manifest=lambda m: m["layers"][0].update(
            mediaType="application/vnd.oci.image.layer.nondistributable.v1.tar+gzip"))
    elif conf.get("tamper") == "link_member":
        link = tarfile.TarInfo("blobs/sha256/passwd")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        fake_podman.edit_archive(handed, extra=[link])
    elif conf.get("tamper") == "sparse_member":
        sparse = tarfile.TarInfo("blobs/sha256/sparse")
        sparse.type = tarfile.GNUTYPE_SPARSE
        fake_podman.edit_archive(handed, extra=[sparse])
    elif conf.get("tamper") == "pax_sparse":
        sparse = tarfile.TarInfo("blobs/sha256/pax-sparse")
        sparse.pax_headers = {"GNU.sparse.map": "0,0", "GNU.sparse.size": "0"}
        fake_podman.edit_archive(handed, extra=[sparse])
    elif conf.get("tamper") == "extra_member":
        extra = tarfile.TarInfo("evil.txt")
        fake_podman.edit_archive(handed, extra=[extra])
    elif conf.get("tamper") == "hidden_index":
        hide_index(handed)
    if conf.get("names"):
        fake_podman.edit_archive(handed, index=lambda i: i["manifests"][0]["annotations"].update(
            {fake_podman.REF_NAME: conf["names"]}))
    if conf.get("disk_meta"):
        with open(handed) as f:
            meta = json.load(f)
        with open(handed, "w") as f:
            json.dump({**meta, **conf["disk_meta"]}, f)
    # power-off: the transient domain is gone, then QEMU's end closes the channel
    if not conf.get("stays"):
        with fake_virsh.changing():
            fake_virsh.end_transient(domain)
    conn.close()
    listener.close()


if __name__ == "__main__":
    main(*sys.argv[1:])
