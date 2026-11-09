# agentbox is a dependency-free Python package

`agentbox` started as a Bash script and grew to over 600 lines with Python embedded for the parts Bash cannot do safely (guest-agent JSON, sanitising VM output, journal parsing). The VM registry, JSON output for every command and tests against fake external programs would have pushed more and more logic into those embedded snippets. We moved the CLI to a Python package that uses only the standard library, like the Control Room backend (ADR 0003): nothing to install on the Host, and nothing a package registry could slip in.

The privilege model stays the same: the CLI runs as root (except `logs` and `pair`, which only need libvirt access), validates every argument itself, and is the only way the Control Room changes anything.

The package is installed root-owned under `/usr/local/lib/agentbox`, started by `/usr/local/bin/agentbox` with `python3 -I`, so neither `PYTHONPATH` nor the current directory can inject modules. Each installation gets its own directory and the `agentbox` symlink is switched with one rename, so a running call never sees half a package. Tests set `AGENTBOX_ROOT` to move every Host path into a temporary directory and put fake `virsh`, `qemu-img`, `journalctl`, `ss` and `curl` first on `PATH`; they run as a normal user without libvirt or a VM. Shortened time limits and fake addresses for tests are read from the environment only while `AGENTBOX_ROOT` is set.

_Amended by #85 (2026-11-08):_ the RPM ([ADR 0009](0009-agent-box-ships-as-a-fedora-rpm.md)) installs the package under `/usr/lib/agentbox`, started by `/usr/bin/agentbox`; the package manager replaces its files, there is no `agentbox` symlink switched by rename any more (see [releasing](../releasing.md)).

## Considered Options

- Keep Bash and move only the embedded snippets into Python modules: rejected, the two halves would keep sharing state through command lines and environment variables.
- A third-party CLI framework (click, typer): rejected, it would be the first dependency on the Host.
