"""The facts of the Host that are not file locations (those are in paths.py): its architecture,
the machine a VM gets, the user and group QEMU runs as, and the Host's package manager. The
values are those of a Fedora x86_64 Host, the only kind of Host Agent Box runs on. A leaf: it
imports nothing from the package.

A Build asks the system it runs on for the architecture itself (vm/Containerfile, rpm -E
%_arch; vm/build/orca-release, platform.machine()), which is the Host's: a build VM has the
Host's CPU.
"""

ARCH = "x86_64"             # the Host's architecture as libvirt, Fedora and rpm (%_arch) name it
MACHINE = "q35"             # virt-install --machine of a VM and a build VM
FEATURES = "acpi=on,apic=on"    # virt-install --features of a VM and a build VM
QEMU_USER = "qemu"          # the user libvirt runs QEMU as: owns a VM's System disk, reads its fw_cfg files
QEMU_GROUP = "qemu"         # QEMU's group: a build VM's run directory and virtiofsd's socket
INSTALL = "dnf install"     # how the owner installs a package, in hints
# whether updates need a Host reboot: compares installed packages with the boot, offline
REBOOT_CHECK = ("dnf", "-q", "--disablerepo=*", "needs-restarting")
