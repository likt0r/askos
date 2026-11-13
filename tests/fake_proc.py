"""The VMs' QEMU processes under AGENTBOX_ROOT, as agentbox reads them (responding.starts):
libvirt's pid file per running domain (/run/libvirt/qemu/agentbox-<vm>.pid), and in /proc
the boot time and boot id, and per process comm, cmdline and stat with its start in clock
ticks after boot.

fake_virsh adds a VM's process on `start` and removes it when the VM stops; tests add one
with a start of their choosing (qemu) where the scenario has the VM running already.
"""
import os
import time

# boot time of the fake Host (epoch seconds): the real one, so that a process's start in
# clock ticks after boot is on the clock agentbox reads (CLOCK_BOOTTIME), as on a Host
BOOT = round(time.time() - time.clock_gettime(time.CLOCK_BOOTTIME))
BOOT_ID = "6f1c1a6e-2b0e-4c8e-9d0e-0a3f1b2c4d5e"
FIRST_PID = 5000


def _proc(root):
    return os.path.join(root, "proc")


def _pid_file(root, vm):
    return os.path.join(root, "run", "libvirt", "qemu", f"agentbox-{vm}.pid")


def _host(root, btime=BOOT, overwrite=False):
    """/proc/stat with the boot time and the boot id (written once, unless overwrite)."""
    proc = _proc(root)
    os.makedirs(os.path.join(proc, "sys", "kernel", "random"), exist_ok=True)
    stat = os.path.join(proc, "stat")
    if overwrite or not os.path.exists(stat):
        with open(stat + ".new", "w") as f:
            f.write(f"cpu  1 2 3 4 5 6 7 0 0 0\nbtime {btime}\nprocesses 100\n")
        os.replace(stat + ".new", stat)
    boot_id = os.path.join(proc, "sys", "kernel", "random", "boot_id")
    if not os.path.exists(boot_id):      # read while VMs start: never half written
        with open(boot_id + ".new", "w") as f:
            f.write(BOOT_ID + "\n")
        os.replace(boot_id + ".new", boot_id)


def _process(root, vm, started):
    """A process that looks like the VM's QEMU process, started at `started`; its pid."""
    _host(root)
    proc = _proc(root)
    pids = [int(p) for p in os.listdir(proc) if p.isdigit()]
    pid = max([FIRST_PID - 1, *pids]) + 1
    d = os.path.join(proc, str(pid))
    os.makedirs(d)
    ticks = int((started - BOOT) * os.sysconf("SC_CLK_TCK"))
    with open(os.path.join(d, "comm"), "w") as f:
        f.write("qemu-system-x86\n")
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\0".join([b"/usr/bin/qemu-system-x86_64", b"-name", f"guest=agentbox-{vm},debug-threads=on".encode(),
                            b"-S", b""]))
    # fields 3 to 21, then 22: the start in clock ticks after boot
    with open(os.path.join(d, "stat"), "w") as f:
        f.write(f"{pid} (qemu-system-x86) S " + " ".join(["0"] * 18) + f" {ticks} 0 0\n")
    return pid


def qemu(root, vm, started):
    """The VM's QEMU process as libvirt started it, at `started` (epoch seconds); replaces
    an earlier one."""
    gone(root, vm)
    pid = _process(root, vm, started)
    os.makedirs(os.path.dirname(_pid_file(root, vm)), exist_ok=True)
    with open(_pid_file(root, vm), "w") as f:
        f.write(f"{pid}\n")
    return pid


def impostor(root, vm, started):
    """Another process that names itself like the VM's QEMU process (any local user can
    choose its comm and command line), without libvirt's pid file."""
    return _process(root, vm, started)


def step_clock(root, seconds):
    """The wall clock is set by `seconds`: the boot time /proc/stat reports moves with it."""
    _host(root, BOOT + seconds, overwrite=True)


def gone(root, vm):
    """The VM's QEMU process ended (and libvirt removed its pid file)."""
    try:
        with open(_pid_file(root, vm)) as f:
            pid = f.read().strip()
        os.unlink(_pid_file(root, vm))
    except OSError:
        return
    if pid.isdigit():
        import shutil                    # not for every call of fake_virsh, which imports this module
        shutil.rmtree(os.path.join(_proc(root), pid), ignore_errors=True)
