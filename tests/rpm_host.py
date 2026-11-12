"""A Host in use, for the update and removal test of the RPM (rpm/upgrade-test.sh, #82). Runs
as root in its throwaway container, never elsewhere.

  python3 -I -B tests/rpm_host.py prepare <state-dir>
      After the older Release is installed: what a Host in use has. Settings, the VM registry
      (work and priv), the GitHub App, a Build of the Base image and the VMs' System disks;
      then `agentbox setup` of that Release in Network mode, as on titan, for the owner user
      `owner`: the Host setup, the Control Room's password and certificate, the lists, the
      other Host steps and what `agentbox regenerate` writes (units, network rules, Data
      disks, domain definitions). systemd, firewalld and libvirt do not run in a container:
      systemctl, firewall-cmd, virsh, virt-install and qemu-img are the fakes of tests/, as in
      tests/test_rpm.py (their calls stay in <state-dir>/fakes). Then the units setup and
      regenerate enable are enabled with the real systemctl, which can do that without systemd.

  python3 -I -B tests/rpm_host.py snapshot <file>
      Every path of the root file system as JSON, {path: [type, mode, uid, gid, size, mtime,
      link target, sha256]}, the hash for files in /etc and /var, where the Host's records and
      state are. Left out like in rpm/install-test.sh: the kernel's file systems, /run, /tmp,
      caches, logs, the package databases, /src and the test's own directory.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import vm_records  # noqa: E402

SKIP = ("/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/var/cache", "/var/log", "/usr/lib/sysimage",
        "/var/lib/rpm", "/var/lib/dnf", "/src")
HASHED = ("/etc/", "/var/")
FAKES = {"systemctl": "fake_systemctl.py", "firewall-cmd": "fake_firewall_cmd.py", "virsh": "fake_virsh.py",
         "virt-install": "fake_virt_install.py", "qemu-img": "fake_qemu_img.py"}
OWNER = "owner"
BUILD = "base-20261101-0300"
VMS = ("work", "priv")
# what agentbox setup enables (agentbox/hoststeps.py, setup.py) and agentbox regenerate for
# the Activity journal, the timers and the VMs
ENABLED = ["agentbox-web.service", "agentbox-vmnet.service", "agentbox-allowlist-expire.timer",
           "libvirt-guests.service", "systemd-journald@agentbox.socket", "agentbox-maintenance.timer",
           "agentbox-feeds.timer"]
ENABLED += [f"agentbox-{kind}-{vm}.socket" for vm in VMS for kind in ("proxy", "ghvm", "gui")]


def write(path, text, mode=0o644, group="root"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)
    shutil.chown(path, "root", group)


def prepare(state):
    subprocess.run(["useradd", "--create-home", OWNER], check=True)
    write("/etc/agentbox/settings.json", json.dumps({"reset_time": "05:30", "rebuild_day": "sat",
                                                     "activity_retention_days": 30}, indent=2) + "\n")
    vm_records.write("/", vm_records.record("work", 1, github_owners=["likt0r"]), vm_records.record("priv", 2))
    write("/etc/agentbox/github/app.conf", "APP_ID=123456\n", 0o640, "agentbox-gh")
    write("/etc/agentbox/github/app-123456.pem", "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
          0o640, "agentbox-gh")
    # a Build of the Base image and the System disks on it (fake disks: qemu-img metadata as JSON)
    image = f"/var/lib/agentbox/images/{BUILD}.qcow2"
    write(image, "{}")
    os.symlink(os.path.basename(image), "/var/lib/agentbox/images/base.qcow2")
    write(f"/var/lib/agentbox/builds/{BUILD}.json", json.dumps({"image": "base", "build": BUILD.removeprefix("base-"),
                                                               "state": "done"}) + "\n")
    for vm in VMS:
        write(f"/var/lib/agentbox/vms/{vm}-system.qcow2", json.dumps({"backing-filename": image}))

    fakes = os.path.join(state, "fakes")
    os.makedirs(os.path.join(fakes, "bin"))
    for name, program in FAKES.items():
        write(os.path.join(fakes, "bin", name), f'#!/bin/sh\nexec python3 -B "{os.path.join(TESTS, program)}" "$@"\n',
              0o755)
    with open(os.path.join(fakes, "scenario.json"), "w") as f:
        json.dump({"state": "shut off"}, f)
    env = {"PATH": os.path.join(fakes, "bin") + ":/usr/bin:/usr/sbin", "LANG": "C.UTF-8", "SUDO_USER": OWNER,
           "FAKE_DIR": fakes, "FAKE_VIRSH_DIR": fakes}
    r = subprocess.run(["/usr/bin/agentbox", "setup", "--mode", "network", "--bind", "192.0.2.8", "--home-network",
                        "192.0.2.0/24", "--cert-name", "titan", "--password-stdin"],
                       input="a password of twenty characters\n", capture_output=True, text=True, env=env, timeout=600)
    if r.returncode != 0:
        sys.exit(f"agentbox setup: exit {r.returncode}\n{r.stdout}{r.stderr}")
    r = subprocess.run(["systemctl", "enable", *ENABLED], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"systemctl enable: exit {r.returncode}\n{r.stderr}")


def entry(path):
    st = os.lstat(path)
    kind = "l" if stat.S_ISLNK(st.st_mode) else "d" if stat.S_ISDIR(st.st_mode) else \
        "f" if stat.S_ISREG(st.st_mode) else "o"
    digest = ""
    if kind == "f" and path.startswith(HASHED):
        with open(path, "rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
    return [kind, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, st.st_size if kind != "d" else 0, st.st_mtime_ns,
            os.readlink(path) if kind == "l" else "", digest]


def snapshot(out):
    skip = SKIP + (os.path.dirname(os.path.abspath(out)),)
    device = os.lstat("/").st_dev
    paths = {}
    for top, dirs, names in os.walk("/"):
        dirs[:] = [d for d in dirs
                   if os.path.join(top, d) not in skip and os.lstat(os.path.join(top, d)).st_dev == device]
        for name in dirs + names:
            path = os.path.join(top, name)
            paths[path] = entry(path)
    with open(out, "w", encoding="utf-8", errors="surrogateescape") as f:
        json.dump(paths, f, sort_keys=True)


if __name__ == "__main__":
    if os.geteuid() != 0 or not (os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")):
        sys.exit("tests/rpm_host.py changes the system: only as root in rpm/upgrade-test.sh's container.")
    command, target = sys.argv[1:3]
    {"prepare": prepare, "snapshot": snapshot}[command](target)
