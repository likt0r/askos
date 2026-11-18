# Handover: SSH access to a VM

Status as of 2026-11-17. The design is settled and tracked in GitHub issues. **No code is written yet.**

## Where things are

- **Spec:** [#126 SSH access to a VM](https://github.com/likt0r/askos/issues/126). It has the problem, the solution, 53 user stories, the implementation decisions, the testing decisions and what is out of scope. **Read it first; it is the source of truth.** This file only adds what the spec does not say.
- **Tickets:** sub-issues of #126, labelled `ready-for-agent`, with GitHub's own blocking edges:

  | # | Ticket | Blocked by |
  |---|---|---|
  | #127 | fix: sshd off in the Base image and fixed by a Safeguard | – |
  | #128 | docs: ADR 0020 SSH access, glossary terms | – |
  | #129 | SSH access through the SSH port in Local-only (CLI tracer bullet) | #127, #128 |
  | #130 | SSH port in Network mode, firewall rule from regenerate | #129 |
  | #131 | SSH keys take effect at once, removal ends SSH sessions | #129 |
  | #132 | Copy SSH keys from another VM, add your own with `--mine` | #129 |
  | #133 | Open SSH sessions postpone the automatic Reset and show among devices | #129 |
  | #134 | `agentbox ssh-config`: names and host keys with `HostKeyAlias` | #129 |
  | #135 | Control Room: SSH access section on the VM page | #131, #133, #134 |
  | #136 | Control Room: copy SSH keys from another VM | #132, #135 |

- **Separate from SSH, also `ready-for-agent`, not linked to #126:**
  - [#137](https://github.com/likt0r/askos/issues/137): Control Room sign-in with a one-time link from the Host. The password stays.
  - [#138](https://github.com/likt0r/askos/issues/138): `setup --force` keeps the Control Room password on a mode switch; a new one only with `--new-password`.
- **Frontier (can start now):** #127, #128, #137, #138. Start with #127, because every VM runs sshd with password login on today (see Facts).

## Glossary changes for #128

The design session drafted these; #128 puts them into `GLOSSARY.md`, word for word unless the review finds a reason.

- **VM registry**: in the list of fields, "VM variables and creation date" becomes "VM variables, SSH access and creation date".
- **Safeguard**: the list ends "..., Claude Code without the owner's claude.ai connectors, SSH only by key and only as `agent`." (#127 may land this line, since it adds the Safeguard.)
- New, after **VM variables**:

  ```md
  **SSH access**:
  Whether a VM can be reached over SSH through its own SSH port on the Host, and with which SSH keys; off for a new VM. Only the user `agent` logs in, only with an SSH key, never with a password (German UI: SSH-Zugang).
  _Avoid_: SSH login, remote shell, SSH server

  **SSH key**:
  A public key entered for one VM that may log in to it over SSH; each VM has its own, and removing one ends every open SSH session of the VM (German UI: SSH-Schlüssel).
  _Avoid_: authorized key, public key (on its own)
  ```

## Decisions not obvious from the spec

- **The owner chose three ways in (A, B, C), then dropped B.**
  - (A) A SSH port per VM, kept.
  - (B) A command `agentbox ssh-proxy` as `ProxyCommand`, dropped. A `~/.ssh/config` alias with `HostName`/`Port` gives the same `ssh work.agentbox`. The proxy would have been a second way into the VM with its own permission check and its own session tracking.
  - (C) `add-ssh-key --mine`, kept.
  - If someone suggests `ssh-proxy` or ProxyJump again: both are already rejected and belong in ADR 0020 (0019 is the trust boundary) as rejected alternatives.
- **Why `HostKeyAlias` (#134):** the host is reached as `127.0.0.1` in Local-only and as the Pairing address in Network mode. Keying `known_hosts` on the alias `<vm>.agentbox` means a mode switch needs no new `known_hosts` line and gives no fingerprint warning.
- **The Control Room password is not dropped in Local-only.** Every process on the Host reaches `127.0.0.1:8800`. The CSRF header and `SameSite=Strict` stop browsers, not `curl` from an npm postinstall script. Hence #137 (a one-time link for root and members of `libvirt`, who control every VM anyway) instead of no password.
- **Grilling answers by number,** in case someone asks "why":
  - Q1: only the user `agent` logs in.
  - Q2: SSH keys per VM.
  - Q3: own Host port.
  - Q4: a connection wakes the VM.
  - Q5: an open session postpones the automatic Reset only.
  - Q6: the owner turns SSH access on from the Host, through the CLI or the Control Room; nothing in the VM can.
  - Q7: only `-L` forwarding.
  - Q8: key changes apply at once.
  - Q9: the host key comes from the Host and is stable.
  - Q11: sshd is off by default, as a fix PR first.
  - Q12: the sshd configuration is a Safeguard.
  - Q13: turning off keeps the keys.
  - Q14: key types and limits.
  - Q15: VMs that are off or paused, and Builds without SSH support.
  - Q16: `regenerate` writes the firewall rule.
  - Q17: no Activity entry per connection.
  - Q18: copy keys from another VM.
  - Q20: the host key is deleted with its VM.
  - Q21: the connection help.
  - Q22: `devices.kind` and the reason `ssh-connected`.
  - Q29: `HostKeyAlias`.
  - Q30–Q32: the sign-in link, keeping the password, and keeping both as separate issues.

## Facts found (verified unless marked)

- **`quay.io/fedora/fedora-bootc:44`:** a sub-agent ran the image locally and checked this.
  - It ships `openssh-server` 10.2p1, and `sshd.service` is **enabled** by the preset of `fedora-release-common`.
  - No file sets `PasswordAuthentication`, so OpenSSH's default **yes** applies.
  - `PermitRootLogin` is `prohibit-password`, `X11Forwarding yes` comes from `50-redhat.conf`, and `UsePAM yes`.
  - Host keys are generated at first boot (`sshd-keygen@`). A drop-in named `10-*.conf` sorts before `50-redhat.conf`, and sshd uses the first value it reads.
- **A locked account still logs in by key** with `UsePAM yes`, whether its password is `!`, `!!` or `*`; only an expired account is refused. Tested in a container without systemd or SELinux. So "`agent` has no password" is no protection against key login, and the sshd configuration must say it.
- **The Host reaches a VM without any limit.** The nftables rules of the VM networks limit only traffic from a VM to the Host. Every local process on the Host reaches `10.77.N.2:22`. This goes into ARCHITECTURE's "What it does not protect against" (#129).
- **The Orca port pattern to copy:** the socket and service units come from `units()` in the code that writes them (the `gui.socket` and `gui@.service` kinds). `ExecStartPre` runs `wake` with `--actor`, then `socat` forwards to the VM. The listen address comes from `hostsetup.orca_listen`. The fw_cfg entries are in `virt_install_args`.
- **Firewall rules for Network mode are written only by `agentbox setup`** today (`firewall_rules`/`_firewall` in the setup code). An upgrade does not run setup, so #130 makes `regenerate` write the SSH range rule.
- **What a mode switch does** (`setup --force`):
  - The Orca ports and the firewall rules follow at once.
  - The password is kept (#138); a new one only with `--new-password`.
  - The existing certificate stays, so its SAN may lack the new IP.
  - Running VMs keep the old Pairing address until their next start, because it comes through fw_cfg. This last point was derived from the code and is not tested on a Host.
- **Test seams:** the tests drive `bin/agentbox` with fakes. For #129 and the following tickets, follow the pattern of `AGENTBOX_ORCA_ADDR` in `tests/test_watch.py` when a test needs a local stand-in for a VM port. Image scripts are run directly in their tests: `fwcfg-env` and `guest-setup` in `tests/test_vm_env.py`, the wrapper in `tests/test_container_images.py`.
- **VM names:** routes under `/vms/<vm>/ssh…` are sub-paths, so no VM name needs to be reserved.

## How to work here (gotchas)

- **`gh` in a VM is only logged in inside a repo.** In a VM, `gh` is the Image's wrapper `/usr/lib/agentbox/bin/gh`, which fetches a GitHub App token for the repository named by `-R`/`--repo`, else for the `origin` of the current directory. Run from a scratch directory or `/tmp` without `-R`, it reports "not logged in" and creates nothing. Run it from the checkout (giving other files by absolute path), or pass `-R likt0r/askos`.
- **Before branching,** run `git fetch`; the local `main` can lag.
- **Every PR follows the same loop.** It starts as a draft (`gh pr create --draft`). It then goes through the [review loop](review-loop.md), with a fresh reviewer every round and the brief taken from `origin/main`.
- **Change a promise, update `docs/claims.md`.** A PR that changes a promise in ARCHITECTURE or an ADR updates `docs/claims.md` in the same PR; `tests/test_claims.py` checks it.
- **Tests:** `python3 tests/run_parallel.py`. The UI tests need Node, which runs only in a VM, not on the Host (ADR 0003). This matters for #135 and #136.
- **The project language is English:** code, docs and issues. The Control Room UI has German and English texts.

## Manual checks no test covers (name them under "Manual check left:" in the PRs)

- Password login refused (`ssh -o PubkeyAuthentication=no`).
- Root login refused.
- `-R` and `-A` refused.
- A removed key ends its session.
- The host key is unchanged after a Reset.
- SELinux lets systemd listen on 2201–2454.
- A wake through the SSH port.
- In Network mode: reachable from the home network, rejected from anywhere else.
