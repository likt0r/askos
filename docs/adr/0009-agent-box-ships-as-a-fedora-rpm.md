# Agent Box ships as a Fedora RPM, one Release per semver tag

Installing meant running Bash scripts with the owner's user, network and interface hardcoded. We now deliver Agent Box as an RPM for Fedora, one **Release** per semantic-version tag (`vMAJOR.MINOR.PATCH`, bumped from Conventional Commits by release-it). GitHub Actions builds the source RPM, COPR builds and signs the package, and the GitHub release carries the same RPM, its SHA256 and a build attestation (once the repository is public; until then see the amendment below). The current and the previous Fedora release are supported.

The Host cannot be a Flatpak: it needs root, the system libvirt daemon, systemd units, nft and firewalld rules, sudoers and `/etc` and `/var/lib`, none of which the sandbox allows. The Control Room is already served by `agentbox-web` as a static site, so it ships in the RPM and is opened in a browser. A Flatpak, PWA or Mac app is only worth building later as a client for a remote Host. A macOS Host would need a new hypervisor layer instead of libvirt, KVM, systemd and nft, which is a separate project, not packaging.

The RPM keeps what ADR 0006 requires (root-owned files, `python3 -I` starter, the `AGENTBOX_ROOT` test seam) but moves the files to `/usr/lib/agentbox` and `/usr/libexec`; the package manager replaces the atomic release directory switch.

- Installing opens and starts nothing beyond loopback. `sudo agentbox setup` writes the **Host setup** (mode, bind address, ports, home network, certificate name), the Control Room password and the certificate, and starts the services. **Local-only** is the default; **Network mode** is an explicit choice. The Host setup is root-owned, never part of Settings and never editable in the Control Room.
- An update never restarts, pauses or resets a VM. Services restart; a new Build takes effect at the next Reset.
- The package writes a Host setup only if none exists and never touches `settings.json`, the VM registry or the certificate, so an existing Host adopts it in place. `titan` moves with `0.1.0`; the install scripts then go.
- Releases start at `0.1.0`. `1.0.0` follows once Local-only and the update path have run on a second Host. MAJOR means an update needs manual work from the owner (an unmigrated format change, a broken `/api/v1`, a removed Safeguard).
- Automatic updates are the owner's choice and off by default, since the package runs as root on the Host.

## Considered Options

- Flatpak for the Host: impossible, see above.
- deb for Debian and Ubuntu from the start: deferred; the scripts and units assume `dnf` and firewalld, and a second package doubles the testing.
- Our own signed repository on GitHub Pages: rejected for now; COPR signs without us holding a key, at the price of depending on Fedora infrastructure.

## Amendment 2026-11-08: private Releases until the repository is public

While the repository is private, a Release is delivered as RPM attachments of its GitHub release, unsigned and checked by SHA256. The maintainer starts it with a button in GitHub Actions (`.github/workflows/release.yml`, [docs/releasing.md](../releasing.md)): release-it tags it, the RPMs for the current and the previous Fedora release are built from the tag and attached to the draft release with a `SHA256SUMS` file, and the maintainer publishes the draft. A Host downloads them with `gh release download`, checks the SHA256 and installs the file with `dnf`; only accounts with access to the repository can, and there is no repository for `dnf` to update from. COPR, its signature and the build attestation (also the one ADR 0011 names) follow with #101, when the repository goes public: COPR builds only public free software, and attestations for private repositories need GitHub Enterprise Cloud.
