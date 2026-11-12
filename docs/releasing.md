# Cutting a Release

A **Release** is a numbered version of Agent Box, `vMAJOR.MINOR.PATCH` (ADR 0009). The version
lives in one place, the file `VERSION` at the repository root. `agentbox version`, the Control
Room's `GET /api/v1/version` (field `release`) and the sidebar of the Control Room all report it;
the RPM installs the file beside the code.

release-it computes the next version from the Conventional Commits on `main` since the last tag
(`fix` is the next PATCH, `feat` the next MINOR, `feat!` the next MAJOR; only commits of the other
types make a PATCH, see [CONTRIBUTING.md](../CONTRIBUTING.md)), writes `VERSION` and
`CHANGELOG.md`, commits, tags and creates a **draft** GitHub release. The tooling needs Node and
runs only in CI or on a maintainer's machine, never on a Host (ADR 0003). The first Release is
`0.1.0`: `VERSION` starts at `0.0.0`, and the first `feat` commit makes it `0.1.0`.

## The Release button

On GitHub: *Actions → Release → Run workflow*, branch `main`. It asks for two things:

- **Dry run** (on by default): release-it shows the next version and its changelog in the log of
  the `release` job and changes nothing; no RPM is built. Start every Release with one.
- **Increment**: `auto` takes the version from the commits as above; `patch`, `minor` or `major`
  override them.

The workflow ([.github/workflows/release.yml](../.github/workflows/release.yml)) refuses to run on
any other branch. Its jobs run one after another, in one workflow because a tag pushed with the
workflow's token starts no other workflow:

1. `release` runs release-it: it writes `VERSION` and `CHANGELOG.md`, commits them to `main` as
   `chore: release X.Y.Z`, tags `vX.Y.Z`, pushes both and creates the draft release with the
   changelog as its notes.
2. `rpm` checks out the tag, builds the Control Room UI from its lockfile (ADR 0011) and the RPM
   with `rpm/build.sh`, for the current and the previous Fedora release, as CI does on every
   pull request. Then it installs the RPM in a fresh Fedora container and checks it with
   `rpm/install-test.sh`, which also requires the installed `agentbox version` to report the tag.
3. `attach` attaches the RPMs (`agentbox-X.Y.Z-1.fc44.noarch.rpm`, `…fc45…`) and a `SHA256SUMS`
   file to the draft release. It refuses a release that is no longer a draft.

Only `release` and `attach` may write to the repository; `rpm`, which installs the UI's npm
packages, only reads. Nothing in the workflow uses an npm cache, and every action is pinned to a
commit (Dependabot proposes updates as `ci:` pull requests).

When `rpm` or `attach` fails for a transient reason (a download, a runner), *Re-run failed jobs*
builds the same tag again. *Re-run all jobs* never helps: it starts with `release` on the commit
before `chore: release X.Y.Z`, and release-it stops because the tag exists. A Release whose RPM
cannot be built is never published, and neither is one whose run GitHub cannot re-run (a run that
ended in an internal error, with a job that was never created): delete its draft
(`gh release delete vX.Y.Z`), fix the cause on `main` and start the next Release. Its tag, its
`chore: release X.Y.Z` commit and its section in `CHANGELOG.md` stay, so the next Release counts
from that tag. release-it needs at least one commit since the tag (`requireCommits`), so a Release
that failed for a cause outside the repository needs one as well. The next draft's notes list only
the commits since the tag: add the failed Release's section from `CHANGELOG.md` to them before
publishing.

## Publishing the draft

CI never publishes a Release. A maintainer reads the draft under *Releases*: its notes, the two
RPMs and `SHA256SUMS`. Then *Publish release* on GitHub, or:

```
gh release edit vX.Y.Z --draft=false
```

## Installing a Release on a Host

While the repository is private, a Release is delivered only as the RPM attached to its GitHub
release, unsigned, checked by its SHA256 (ADR 0009); only accounts with access to the repository
can download it. On the Host, with `gh` logged in as such an account (or on another machine,
copying the files to the Host afterwards):

```
gh release download vX.Y.Z -R likt0r/askos -p '*.rpm' -p SHA256SUMS
sha256sum -c SHA256SUMS                                   # every file: OK
sudo dnf install ./agentbox-X.Y.Z-1.fc$(rpm -E %fedora).noarch.rpm
```

`rpm -E %fedora` is the Host's Fedora release, so dnf gets the RPM built for it. A new Host
is set up afterwards with `sudo agentbox setup`.

## Updating a Host

A newer Release is downloaded and checked the same way and installed over the old one:

```
sudo dnf upgrade ./agentbox-X.Y.Z-1.fc$(rpm -E %fedora).noarch.rpm
agentbox version                                          # agentbox X.Y.Z
```

An update replaces the package's files under `/usr` and nothing of the Host's (ADR 0009). The
Host setup, Settings, the VM registry, the Allowlist, Watchlist and Legitimate domains, the
Control Room's password and certificate, the GitHub App, Images, Builds and disks in
`/etc/agentbox` and `/var/lib/agentbox` stay byte for byte; no unit is enabled or disabled,
and no `agentbox` command runs.

- **No VM is started, stopped, paused, reset or defined again.** The next Build (`agentbox
  build`, or the weekly rebuild) uses the new Release's Containerfiles, and a VM gets that
  Build at its next Reset. A Build that runs during the update copied the Containerfiles it
  builds from when it started.
- **The Control Room and the expiry timer** of temporary Allowlist entries restart if they run.
- **The proxies of the VMs restart only when the update changes their code** (`agentbox-proxy`
  or the classifier `agentbox-suspicious` it loads). Their sockets
  stay open, so a VM loses only its open connections; new ones wait for the restart. A proxy
  that does not run stays stopped. If the restart fails, the next update tries it again
  (until the Host restarts, which starts the proxies with the new code anyway).
- **The token service and the Orca port of each VM** start a process per connection, and the
  Reset check, the Feeds and the rebuild a new run each time: each takes the new Release from
  its next start.
- **The units and VM networks `agentbox regenerate` wrote, and the ports of the firewalld
  zone `libvirt`,** stay as they are until it runs again (`sudo agentbox regenerate`, or
  `sudo agentbox setup`, which keeps the Host setup and does the Host steps again); either
  command opens the ports the VMs reach in that zone. It brings each VM network to the current definition; a VM
  running on a network it had to restart has no network until its next Restart, and
  `regenerate` names it. A socket whose unit changed is restarted on its new address, a VM's
  proxy stopped first.

`rpm/upgrade-test.sh` checks all of this in CI: it updates an older Release on a Host in use
to the RPM of the pull request, reinstalls it and removes it.

### Automatic updates

They are off, and nothing can turn them on yet. While the repository is private, a Release
exists only as a file attached to its GitHub release: there is no repository for dnf to update
from, so neither dnf nor `dnf5-automatic` (which applies Fedora's updates on the Host) ever
updates Agent Box, and every update is the `dnf upgrade` above, run by the owner. Once
Releases are published through COPR (#101), a Host that enables that repository gets them
like any other package, and `dnf5-automatic` could install them too. Whether it does stays
the owner's choice (ADR 0009): the package runs as root on the Host.

## Removing Agent Box

```
sudo dnf remove agentbox
```

Removal stops and disables the Control Room and the expiry timer, and stops, disables and
deletes the units `agentbox regenerate` wrote to `/etc/systemd/system`: each VM's proxy, token
service and Orca port, the Reset check, the Feeds and the Builds. So nothing is left enabled
that would run a removed program, and the VMs lose their way out with their proxies. Then
the package's files go, with the sudoers rule.

What stays:

- everything in `/etc/agentbox` and `/var/lib/agentbox`: the Host setup, Settings, the VM
  registry, the lists, the Control Room's password and certificate, the GitHub App, Images,
  Builds, the VMs' disks and archived Data disks;
- the VMs with their libvirt domains, networks and storage pool (a running VM keeps running,
  without a proxy); to remove them too, run `sudo agentbox delete <name> --confirm <name>` for
  each before removing the package;
- the rules of `agentbox-vmnet`, which keep the VM networks closed until the Host restarts,
  and its link in `multi-user.target.wants`, so that installing again closes them at boot;
- what the other Host steps of `agentbox setup` changed outside these directories: the
  firewall rules of Network mode and of the VM networks' zone, `libvirt-guests` and its
  configuration, the owner's groups; and the service users and groups, and Activity
  (`journalctl --namespace=agentbox`).

After installing a Release again, `sudo agentbox setup` keeps the Host setup and the password
and does the Host steps again: it writes and enables the units of the VMs, the Reset check,
the Feeds and the Builds from the Host's records as they were, and starts the Control Room and
the expiry timer.

## On a maintainer's machine

The button is the way to cut a Release. release-it also runs locally, for a dry run or when CI
is not available:

```
npm ci                 # once, in the repository root: release-it and its plugins, exact versions
npm run release:dry    # shows the next version and the changelog, changes nothing
npm run release        # on main, with GITHUB_TOKEN set: bump, commit, tag, push, draft release
```

With only commits that raise no version, release-it finds none and stops;
`npm run release -- patch` makes the PATCH the button would. A local run builds no RPM.

`npm audit` reports a denial-of-service in `basic-ftp`, pulled in through release-it's proxy
support. release-it only talks to GitHub here and never uses FTP, and the suggested fix is a
downgrade to release-it 15, so it is left as it is until release-it itself updates.
