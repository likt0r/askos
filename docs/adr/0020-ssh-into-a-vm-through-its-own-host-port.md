# SSH into a VM through its own Host port, with SSH keys and the host key from the Host

The only way into a VM was Orca: its Orca port and a Paired device. An owner who wants a plain shell, `scp` or `rsync`, or an IDE's remote-SSH mode had no way in. At the same time every VM ran an sshd nobody chose: Fedora's `fedora-bootc` enables `sshd.service`, with password login on, root login by key and X11 forwarding. The owner decided (#126) that each VM gets **SSH access**, off for a new VM.

## Decision

- **Its own Host port.** A VM with SSH access is reachable through its SSH port on the Host, 2200 + its network number (2201–2454), exactly where its Orca port is reachable: on `127.0.0.1` in Local-only, also from the home network in Network mode. A socket unit per VM, made by `regenerate` only while SSH access is on, runs `wake` for each connection (Actor `SSH (<address>)`) and then forwards to port 22 of the VM, as the Orca port does. `agentbox ssh-config` and the Control Room hand out a `~/.ssh/config` block that names it `<vm>.agentbox`; Agent Box never writes into the owner's home.
- **SSH keys per VM, kept on the Host.** The VM's registry record holds SSH access (on or off; a record without it means off) and its SSH keys. The Host checks every key (Ed25519, ECDSA and their security-key forms, or RSA of at least 3072 bits; no options before it; at most 16; no duplicate by its SHA256 fingerprint, which the Host computes itself, [ADR 0006](0006-agentbox-is-a-dependency-free-python-package.md)) and passes them in: at start in an fw_cfg entry, in a running VM through a fixed guest-agent wrapper (`agentbox-ssh`) that checks them again and replaces the key file whole. A change applies at once in a running VM, when a paused VM resumes, and at the next start of a VM that is off. Removing a key or turning SSH access off ends every open SSH session of the VM, since the Host cannot tell which session used which key; turning it off keeps the keys.
- **The host key is made by the Host.** It is kept on the Host per VM, readable only by root and QEMU like the Claude token, made when SSH access is first turned on or a first key is added, passed in with the keys and deleted with the VM. It stays the same across every Reset, so `ssh` never warns about a changed host key after the weekly Reset. The config block keys `known_hosts` on a `HostKeyAlias`, so switching the Host between Local-only and Network mode needs no new line either.
- **Only `agent`, only by key, only `ssh -L`.** `agent` is the user Orca, the agents and the terminals run as, so SSH gives nothing `agent` does not already have. The Base image keeps sshd from starting by itself and makes no host keys in the VM (#127); `guest-setup` starts sshd only when SSH access is on and there is at least one key. Its configuration allows publickey login only, no root, `AllowUsers agent`, the key file and host key in the root-owned `/run/agentbox/ssh` (never `~/.ssh`), `AllowTcpForwarding local`, no agent, X11, stream-local or tunnel forwarding, no `GatewayPorts` and no `PermitUserEnvironment`. That configuration is a Safeguard: a Build, also of a Recipe, that weakens it or lets sshd start by itself is discarded.
- **The check reads files; it does not run sshd.** The Safeguard check after every Build reads sshd's configuration and systemd's units and presets from the mounted Image on the Host, as it reads every other Safeguard ([ADR 0008](0008-safeguards-checked-from-the-host-after-every-build.md)); it never runs `sshd -T` from the Build, since the Image decides what a program run from it does. By [ADR 0019](0019-the-host-and-what-its-owner-writes-are-trusted.md) it catches mistakes and convenient edits that weaken sshd or let it start by itself, not an Image made to get past it.

## The boundary

- **The Host decides** whether a VM is reachable over SSH and with which keys: the SSH port exists only while SSH access is on, and only the keys in the record reach the VM. Nothing running as `agent` can add a key; the key file is root's, outside the home.
- **The sshd in the VM enforces** key-only login, `agent` only and the forwarding limits. That is a Safeguard, not a security boundary: code that gets root in the VM can change sshd as it likes. For the same reason key options (`command=`, `from=`) are refused, and the checks the VM makes again on the keys are plausibility checks (ADR 0019).
- **Only the client finally refuses `-R` and `-A`.** Remote and agent forwarding open a path from the VM to the owner's laptop or home network past the proxy; the VM's sshd refuses them, but cannot be trusted to. The config block sets `ForwardAgent no` and `ForwardX11 no`, so the client offers neither, and `-R` takes the owner asking for it; `StrictHostKeyChecking yes` with the host key from the Host keeps the client from talking to anything but the VM.

## Consequences

- A VM is no longer reachable only through its Orca port. Every local process on the Host reaches port 22 of a VM directly (the nftables rules of the VM networks limit only traffic from a VM); it still needs a key.
- Any connection to the SSH port wakes the VM before sshd checks a key, so in Network mode every device in the home network can wake a VM with SSH access on, as it can through the Orca port.
- In Network mode a firewall rule accepts the home network on 2201–2454 and rejects the rest. `regenerate` writes it, so an update of Agent Box needs no `setup`.
- A Build without the wrapper keeps the keys the owner adds and reports `ssh_missing` until a Reset onto a newer Build.
- An open SSH session postpones the automatic Reset like an Orca connection, with its own reason `ssh-connected`. Activity records turning SSH access on or off and adding or removing keys, not each connection.
- Deleting a VM deletes its keys and host key, even when its Data disk is archived; `create` refuses a name whose earlier VM left them behind.

## Considered Options

- ProxyJump through the Host's sshd: rejected, it needs a login on the Host, and Agent Box never touches the Host's sshd.
- Tunnelling through Orca's pairing connection: rejected, Orca cannot do it.
- A separate `ssh-proxy` command on the Host as `ProxyCommand`: rejected, it adds a second path into the VM, with its own permission check and session tracking, for no gain over a config alias with `HostName` and `Port`.
- SSH keys for the whole Host, shared by VMs: rejected, a VM is the unit of separation ([ADR 0001](0001-vm-is-the-unit-of-separation.md)); a key that may enter `work` must not also enter `priv`. Keys can be copied from another VM once instead.
- Keys applied only at the next start, as VM variables are: rejected, a removed key would stay valid until then.
- A host key made in the VM: rejected, every Reset would make a new one.
- Running `sshd -T` from the Build for the Safeguard check: rejected, ADR 0008 runs nothing from the Image. Reading the files the way sshd does is more code, and by ADR 0019 it only has to be right for files written in good faith.
