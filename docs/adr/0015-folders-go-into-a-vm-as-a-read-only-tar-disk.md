# Folders go into a VM as a read-only tar disk

A repository with work in progress (branches, stashes, unpushed commits, uncommitted and untracked files) could only reach a VM through Git, which carries none of that. The owner decided (#47, after the spike in #46) that `agentbox import <vm> <folder> [<target>]` copies a folder of the Host into a VM, unpacked under `~/code` on its Data disk.

## Decision

- **Packing on the Host.** The Host packs the folder with Python's `tarfile` ([ADR 0006](0006-agentbox-is-a-dependency-free-python-package.md)) into `/var/lib/agentbox/transfers/<vm>-<id>.tar` (the directory `0711`, so QEMU reaches the file). Of a Git repository it takes the whole `.git` and what `git ls-files --cached --others --exclude-standard` lists, so ignored files stay out; of a folder without Git everything. A linked worktree (`.git` is a file) is refused, its repository lies elsewhere. Symbolic links stay links; links leading out of the folder and special files are skipped. The archive names no owner and carries no setuid bits. Packing runs with the rights of the user who ran `sudo` (in a root shell: of the folder's owner), not root's, so a user can send only what they could read themselves.
- **Transport.** The Host attaches the tar file to the running VM as a raw read-only disk (`virsh attach-disk --live --mode readonly --targetbus virtio --driver qemu --subdriver raw --serial agentbox-inbox`). The disk has no filesystem: nothing on either side mounts it, and the VM gets no share and no lasting access to the Host's files ([ADR 0001](0001-vm-is-the-unit-of-separation.md)). The Host then runs a wrapper of the Image through the guest agent, as it reads the VM's journal ([ADR 0004](0004-own-security-services.md)), detaches the disk and deletes the file, also when anything failed. Only a small JSON result (files, bytes) comes back, treated as untrusted.
- **Unpacking in the VM.** `agentbox-inbox receive <device> <target>` lies in the guest agent's hook directory, as the other wrappers, and is part of the Safeguard check. It opens `/dev/disk/by-id/virtio-agentbox-inbox` as root, then drops to the user `agent` before it reads a byte, and unpacks with `tarfile`'s `data` filter (no names or links leading out of the target, no device files, no setuid bits). The target is a new directory under `~/code`; an existing one is refused, nothing is overwritten. Hooks in `.git/hooks`, also of nested repositories and submodules, other than the samples are removed. The wrapper waits up to 30 s for the device link, which udev creates a moment after the attach; the Host detaches a disk an interrupted import left behind before it attaches the next.
- **Lifecycle.** A VM that is off is started as `pair` starts it, a paused one is refused. The VM's lifecycle lock is held from the attach to the deletion, so no Reset, Restart, Pause or delete comes in between.

## Consequences

- The VM sees the archive only while it unpacks it, and only as bytes it parses with a filter. The Host parses nothing that comes from the VM but two numbers and an error text.
- A VM needs a Build with the wrapper; on an older one the import fails with `inbox_missing` until a Build and a Reset.
- What the folder holds goes into the VM as it is: there is no secrets scan, no check of the GitHub owners named in `.git/config`, no free-space check and no `--move`. The owner deletes the source by hand.
- The command is CLI only; the Control Room does not offer it.

## Considered Options

- A shared folder (virtiofs, 9p): rejected, it gives the VM lasting access to a directory of the Host and a filesystem server on the Host to talk to.
- Writing into the VM's Data disk from the Host (`qemu-nbd`, libguestfs): rejected, the Host would parse filesystem metadata a VM controls, the reason ADR 0004 reads the journal through the guest agent.
- Sending the archive through the guest agent (`guest-file-write`): rejected, the whole archive would pass in base64 chunks through libvirt and the guest agent, which writes as root wherever it is told; the disk is read by a wrapper that has dropped root.
- Pushing through Git: rejected, it carries no stashes, uncommitted or untracked files.
- `tar` instead of `tarfile`: rejected, an external program on the Host's side with its own options for owners, links and absolute names, where `tarfile` is already there and its `data` filter is what the VM applies.
