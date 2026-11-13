# Security policy

Agent Box is a security boundary around coding agents: the VMs, the Allowlist proxy, the
short-lived credentials. A hole in it matters, so please report one privately.

## Supported versions

Only the latest published Release is supported
([Releases](https://github.com/likt0r/askos/releases/latest); `agentbox version` shows the one a
Host runs). A fix comes as a new Release, never as a patch to an older one; update a Host as in
[docs/releasing.md](docs/releasing.md#updating-a-host).

## Reporting a vulnerability

Do not open a public issue, pull request or discussion for it. Report it through GitHub's
private vulnerability reporting: the repository's **Security** tab, then **Report a
vulnerability**, or directly at <https://github.com/likt0r/askos/security/advisories/new>. Only
you and the maintainers see the report.

Please include:

- the Release (`agentbox version`) and the mode of the Host (Local-only or Network mode);
- what an attacker needs (an agent in a VM following planted instructions, a malicious package,
  access to the home network, a session in the Control Room, ...) and what they get;
- the steps or a proof of concept, and which mechanism it gets past.

Test only on a Host and with accounts that are yours.

## What to expect

Agent Box is maintained by one person, so there is no fixed response time. Your report is
answered in its advisory: whether it is confirmed, and if not, why. A confirmed hole is fixed in
a new Release, and the advisory is published with that Release, crediting you unless you ask
not to be named. Please keep the details to yourself until then. There is no bug bounty.

## Known limits

Some limits of the protection are known and documented:
[What it does not protect against](ARCHITECTURE.md#what-it-does-not-protect-against) in
ARCHITECTURE.md, next to the [threat model](ARCHITECTURE.md#threat-model). A limit listed
there is not a vulnerability. A way past a mechanism that the list does not name is one, and so
is a limit that turns out worse than the list says.

Holes in software Agent Box builds on (Orca, Claude Code and the other agents, QEMU and libvirt,
Fedora's packages) belong to those projects; tell us as well when Agent Box makes one worse.
