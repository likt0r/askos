# VM variables are checked on the Host with the safeguard check's own patterns and reach the VM as an fw_cfg file

A project often needs environment variables in its VM that are not secret: a `DATABASE_URL` for a dev server, a tool's mode, a test flag. The owner decided (#29) that each VM gets **VM variables**: `"env"` in its registry record, set with `agentbox set-env` / `unset-env` or a table in the Control Room, for `orca serve`, the agents and terminals it starts, and login shells.

The core of it is that a VM variable must never switch a Safeguard off. The safeguard check ([ADR 0008](0008-safeguards-checked-from-the-host-after-every-build.md)) reads every Build, but a variable set at runtime never passes through it, and `/run/agentbox/env` (the `EnvironmentFile` of `orca serve`) overrides both systemd's `DefaultEnvironment` and the unit's `Environment=`. So the Host checks every name when it is set and when it reads a record, and refuses:

- the names the safeguard check refuses or flags itself (`safeguards.OVERRIDES`, `safeguards.MENTIONS`): the patterns have one source, so a variable that check learns to look for is refused here too;
- what Agent Box sets for the VM or what every process or shell reads first (`PATH`, `HOME`, `XDG_RUNTIME_DIR`, `LD_*`, `NODE_OPTIONS`, `BASH_ENV`, `ENV`, `CLAUDE_CODE_OAUTH_TOKEN`, `AGENTBOX_*`, `ORCA_PORT`, `PAIRING_ADDRESS`);
- every variable of the Image's `DefaultEnvironment` (`agentbox.conf` and the `agentbox-packages.conf` that `package-safeguards` writes). A test parses both and fails when one of their variables is not reserved, so a new one cannot be forgotten.

Only what a Safeguard or Agent Box itself reads is blocked, no broad prefixes such as `npm_config_*` or `UV_*`: the owner sets these variables to configure exactly such tools.

The values reach the VM like its Claude token: as a file QEMU passes in as the fw_cfg entry `opt/agentbox/env` (`/var/lib/agentbox/vm-env/<vm>.env`, `root:qemu`, mode 0640), one line `NAME=<base64 of the value>` per variable, or `none` without any. `regenerate` and `set-env` write it when it changed; the domain names the file once and is not defined again for a change. In the VM, `guest-setup` hands the entry to `/usr/lib/agentbox/fwcfg-env`, which checks each name again, decodes with `base64 -d`, evaluates nothing, and writes the variables quoted into `/run/agentbox/env` and `/run/agentbox/agent-env`, before the fixed values of Agent Box, so those win.

## Consequences

- A change applies at the VM's next start (Restart or Reset), as the memory limit does; a paused VM is not refused, since nothing of its definition changes.
- The first `regenerate` after the update defines every VM again, once, for the new entry (a paused one when it runs again). A domain that lacks the entry gives the VM no variables, not an error.
- The name check, the limits and the file format work on any list of names and values, and `fwcfg-env` on any entry: secrets per VM (#30) can come as a second entry without changing them.
- The patterns know the Safeguards they were written for. A variable can still point a tool at another configuration file (`npm_config_userconfig`), as a repository's own `.npmrc` can.
- `fwcfg-env` is a Safeguard file: a derived Image must keep it as the Base image has it, like `guest-setup`.

## Considered Options

- Values inline in virt-install's `--sysinfo`: rejected, virt-install splits that string at commas, and a value may hold any character.
- A list of reserved names of its own, apart from `safeguards.py`: rejected, the two would drift; the safeguard check's patterns are the definition of what weakens a Safeguard.
- Blocking whole prefixes (`npm_config_*`, `UV_*`, `GIT_*`): rejected, it would forbid the variables owners need most.
- `systemctl set-environment` in `guest-setup`, so services started apart from `orca serve` (the user manager, rootless Podman) see the variables too: not done. `orca serve` and everything it starts inherit them from its `EnvironmentFile`, shells from the profile; containers never inherit the host's environment, and changing the manager's environment would reach every later service of the VM, which nothing needs.
