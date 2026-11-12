# Agent Box

Run coding agents (Claude Code, Codex, OpenCode) on a home server without handing them
your machine, your network or your long-lived credentials.

Each agent works inside its own VM. A VM can only reach the internet through an
allowlist proxy on the Host, gets GitHub tokens that are valid for one repository and
one hour, and has a system disk that is thrown away on every reset. You drive the
agents from your laptop with [Orca](https://onorca.dev) and operate the box itself
from the Control Room in your browser.

The terms used here (Host, VM, Reset, Allowlist entry, Alert, …) are defined in
[GLOSSARY.md](GLOSSARY.md). How the pieces fit together is in
[ARCHITECTURE.md](ARCHITECTURE.md); the reasons behind the larger decisions are in
[docs/adr/](docs/adr/).

## What it protects against

- **Rogue or manipulated agents** (prompt injection from a repo, issue or web page)
- **Supply-chain attacks through packages** (install scripts stealing tokens)

It does not try to protect against theft of the machine or targeted attacks on the
person running it.

## Requirements

- Fedora, the current or the previous release (tested on Fedora 44), on a machine with KVM
  (AMD-V/VT-x enabled in the firmware): a home server, or the laptop you work on
- Enough memory for the VMs you want to run (each VM needs roughly 1 GB on top of
  what its projects use)
- Orca on your laptops, and on the Host (`/opt/Orca`) for the Observer, which reads the agents'
  status (the Images take Orca from its GitHub release, not from the Host)
- A GitHub App you own (see below) and a Claude subscription or API account
- In Network mode, the Host reachable from your laptops (home network or VPN)
- Ports 53 (TCP and UDP) and 443 (TCP) not held on all of the Host's addresses by another
  service: the VMs' resolver and proxy listen on the Host's address in each VM network

## Installing

Agent Box is delivered as **Releases**, one RPM per Fedora release ([ADR
0009](docs/adr/0009-agent-box-ships-as-a-fedora-rpm.md)). While the repository is private, a
Release is attached to its GitHub release, unsigned, with a `SHA256SUMS` file, and only accounts
with access to the repository can download it. With `gh` logged in as such an account, on the
Host:

```sh
gh release download vX.Y.Z -R likt0r/askos -p '*.rpm' -p SHA256SUMS
sha256sum -c SHA256SUMS                                   # every file: OK
sudo dnf install ./agentbox-X.Y.Z-1.fc$(rpm -E %fedora).noarch.rpm
agentbox version
```

Installing creates the service users, the directories and the Control Room's sudoers rule, and
opens, enables and starts nothing. `sudo agentbox setup` then sets the Host up in one of two
modes:

```sh
sudo agentbox setup                                       # Local-only, the default
sudo agentbox setup --mode network --bind 192.0.2.8 --home-network 192.0.2.0/24   # Network mode
```

It writes the **Host setup** (`/etc/agentbox/host.json`), asks for the Control Room password
twice (at least 16 characters), creates the certificate and does the Host steps listed in
[ARCHITECTURE.md](ARCHITECTURE.md#setting-a-host-up): groups, the lists a new Host starts
with, the firewall zone of the VM networks, the units, and the Control Room. Then:

```sh
sudo agentbox set-github-app <APP_ID> < key.pem   # a GitHub App (see below), checked with GitHub first
sudo agentbox build base                     # the first Build of the Base image, from the installed Release (10–20 min)
journalctl -fu agentbox-build@base           #   follow it until it says the Build is the current one
sudo agentbox create priv --owner <owner>    # a VM: network, Orca port, disks (see below)
sudo agentbox pair priv                      # start it and print its Pairing code
```

### Local-only and Network mode

| | Local-only (default) | Network mode |
| --- | --- | --- |
| Control Room | `https://127.0.0.1:8800` | `https://<bind>:8800` |
| Orca ports (6801–7054) | `127.0.0.1` only | every address, but the firewall lets only the home network through |
| Firewall | no rule for any network | in the default zone: the home network accepted on the Control Room's port and 6801–7054; everything else rejected there and on 6768–7054 and 16768 |
| Pairing codes name | `127.0.0.1` | the bind address |
| For | a laptop that is its own Host | a home server driven from laptops in the home network (or a VPN into it) |

Options of `agentbox setup`:

- `--mode local|network`; in Network mode also `--bind ADDRESS` (the Host's IPv4 address in the
  home network) and `--home-network CIDR` (e.g. `192.0.2.0/24`)
- `--web-port N`: the Control Room's port, 8800 by default
- `--cert-name NAME`: the name the browser uses for the Host, put into the certificate
- `--owner-user USER`: the person who uses the Host, by default the user who ran `sudo`. They
  go into the group `libvirt` (control of every VM), and the Builds take Orca's Node runtime
  from their home; a service or system account is refused
- `--password-file FILE` or `--password-stdin` instead of the prompt, for scripted installs
- `--force`: replace an existing Host setup and the password
- `--json`

The Host setup and the password change only on the Host, never in the Control Room. Running
`sudo agentbox setup` again without options keeps both and does the Host steps again, changing
only what differs (after an update, or when a run stopped). To switch modes, run it with
`--force` and the new options: the Orca ports of the VMs and the firewall rules follow at once,
and it asks for the password again. An existing certificate stays; delete `cert.pem` and
`key.pem` in `/etc/agentbox/web` first if its names no longer fit.

Setup enables `libvirt-guests.service`, so a shutdown or reboot of the
Host shuts the VMs down instead of killing them: running VMs get a clean shutdown, all at
once, and are forced off after 60 seconds; Paused VMs keep their Pause; no VM starts at
boot, each starts on its first Orca connection as before. Do not stop or restart
`libvirt-guests.service` by hand: that shuts all running VMs down.

### Updating

A newer Release is downloaded and checked the same way and installed over the old one:

```sh
sudo dnf upgrade ./agentbox-X.Y.Z-1.fc$(rpm -E %fedora).noarch.rpm
agentbox version
```

An update never starts, stops, pauses or resets a VM and leaves the Host setup, Settings, the
VM registry, the lists, the certificate and the GitHub App as they are; the next Build uses the
new Release's Containerfiles, and a VM gets that Build at its next Reset. Automatic updates of
Agent Box itself are off (those of the agent tools in the Images are on, see [Tool
versions](#tool-versions)). What restarts and what does not, and how to remove Agent Box: [Updating a
Host](docs/releasing.md#updating-a-host) in docs/releasing.md.

### Unsupported platforms

- **Debian, Ubuntu and other distributions without `dnf`:** there is no package for them. Setup
  and the units assume Fedora's tools: firewalld for the zone of the VM networks and the rules of
  Network mode, an RPM installed with `dnf`, and `dnf needs-restarting` for the Control Room's
  reboot notice; a second package would double the testing ([ADR 0009](docs/adr/0009-agent-box-ships-as-a-fedora-rpm.md)).
- **macOS as the Host:** the Host needs KVM, libvirt, systemd, nftables and firewalld, none of
  which macOS has. It would need another hypervisor layer, a project of its own, not a package.
  A Mac works as a laptop that drives a Host in Network mode, with Orca and a browser.

### GitHub App

Create an app under *Settings → Developer settings → GitHub Apps* with webhooks off and
these repository permissions: Contents, Issues, Pull requests (read and write),
Checks and Commit statuses (read), Metadata (read). Nothing else. Install it on the
accounts and organisations whose repositories agents may work on, then set the
GitHub owners per VM: `sudo agentbox set-owners priv <owner>...`. Builds do not need the
App: they take the Containerfiles of the installed Release (see Images and Builds below).
For an organisation admin to install it from the link `agentbox github-app` shows, the App
must be installable by any account (*Where can this GitHub App be installed?*).

A Host can hold several Apps. Use one App per trust boundary: for example an App owned by an
organisation and installable only there, whose key its admins may hold, plus a personal App
for your private repositories. One App installed on both would let anyone with its key reach
the private repositories. Add each with `sudo agentbox set-github-app <APP_ID> < key.pem`.
There is nothing to map: for a repository, the token service asks the Apps in ascending App
ID and the first one installed there issues the token. If an App answers with anything but
"not installed" (an error, a revoked key), the request is refused rather than tried with the
next App. `agentbox github-app` lists every App with its installations and install link.

To rotate an App's key, generate a new private key on GitHub and run
`sudo agentbox set-github-app <APP_ID> < new-key.pem` with that App's ID. It signs a JWT with
the new key and asks GitHub for the App first; only if GitHub accepts it is that App's key
replaced (the other Apps stay), and the token service uses it from its next request on. Then
delete the old key on GitHub and the downloaded file. `sudo agentbox remove-github-app <APP_ID>`
removes an App and its key from the Host.

Coming from a release with a single App: its key file `/etc/agentbox/github/app.pem` is no
longer read. Add the App again with `sudo agentbox set-github-app <APP_ID> < key.pem`, then
delete the old file: `sudo rm /etc/agentbox/github/app.pem`.

### Pairing Orca

```sh
sudo agentbox pair priv     # prints an orca://pair?... code
```

Add it as a new environment in Orca on your laptop. The code gives full access to the
VM, so only use it on your own devices. The Control Room reveals it only after an explicit
confirmation (`confirm: true` in the API).

Keep Orca on the laptop at the version in the VM's Image, and at least at 1.4.232: older
versions show a VM's dev servers in the built-in browser as pages of the laptop (see [Viewing a
dev server](#viewing-a-dev-server)). Every Build takes the newest Orca release that is at least
`package_min_age_days` old, or the one `orca_version` pins (see [Settings](#settings)); the
Control Room shows the Orca of each Image and VM and the one the next Build would take (see
[Tool versions](#tool-versions)). Updating Orca on the laptops and on the Host stays yours.

### Managing pairings

```sh
sudo agentbox pairings priv [--json]                  # Paired devices: paired / last seen, Observer marked
sudo agentbox revoke-pairings priv --confirm priv     # new Orca identity: every device must pair again
```

Orca (checked in its 2026 desktop build) has no CLI or RPC command to list or remove
Paired devices; only its desktop GUI does. Agent Box therefore reads Orca's device registry
(`orca-devices.json` in its data directory in the VM) through a fixed wrapper in the Image
(`agentbox-devices`, run by the guest agent, no shell, tokens never leave the VM). **Orca
does not support removing a single device to Agent Box**: `agentbox unpair <vm> <device>`
only says so (and refuses the Observer). Use `revoke-pairings` instead: it stops Orca
in the VM, deletes its device registry and key, and starts it again, so Orca has a new
identity and every device (also those only reading) must pair again with a new code.
Running agent terminals may be interrupted by the restart. The Host re-pairs itself as
Observer automatically when it was one (otherwise `agentbox observe <vm>`).

## Day to day

```sh
agentbox status                     # VMs, memory, agents
agentbox create lab --owner likt0r --copy-allowlist-from priv   # a new VM
agentbox delete lab --confirm lab   # delete a VM, its Data disk is archived
agentbox set-memory lab 32768       # memory limit in MiB, from the VM's next start or Restart
agentbox blocked priv               # what a VM tried to reach and was refused
agentbox allow priv docs.rs         # add an Allowlist entry for one VM
agentbox allow all crates.io --for 8h --note "Rust crates"   # for all VMs, for 8 hours
agentbox allow priv --regex '^mirror[0-9]+\.vendor\.example$'  # a Regex entry: shows what it would allow
agentbox allow priv --regex '^mirror[0-9]+\.vendor\.example$' --confirm-pattern   # ... and adds it
agentbox allowlist priv             # what applies to a VM: remaining time, uses in 24 h, note
agentbox extend priv docs.rs --for 1h   # a temporary entry runs an hour longer
agentbox disallow priv docs.rs      # remove it; open tunnels to it are closed
agentbox open-web priv 30           # open web for 30 minutes
agentbox alerts priv                # suspicious destinations a VM tried
agentbox mark-seen priv             # its Alerts so far count as seen
agentbox check-domain example.com   # a domain's Signals and why
agentbox watchlist                  # the hand-maintained Watchlist and the Feeds' state
agentbox watch-add drop.example --category exfil --source "incident 11/2026"   # a new Watchlist entry
agentbox watch-remove drop.example --force   # remove one (strong categories only with --force)
agentbox watch-add --regex '^npm-[a-z]+\.(com|net)$' --category phishing   # a family of lookalikes
agentbox check-regex harmless --regex '^ci[0-9]+\.example$' ci7.example   # can it be an entry, does it match
agentbox legitimate                 # the Legitimate domains, the Host's and the built-in ones
agentbox legitimate-add pnpm.io --note "pnpm registry"   # no pattern Signals or Feed hits for it
agentbox mark-harmless foo.example  # a false positive: no more Alerts (blocking stays)
agentbox reset priv                 # fresh system disk, data disk kept
agentbox images                     # Images, their Builds, which VM uses and is pinned to which, tool versions
agentbox containerfile rust         # the Containerfile of an Image in the installed Release (its next Build)
systemctl start agentbox-versions   # check now what a new Build would bring (the daily timer does it too)
agentbox update-now --reset priv    # Update now: check, build every Image with an update, then reset priv
agentbox build base                 # rebuild the Base image now (--all: every Image)
agentbox build base --reset priv    # Update: rebuild, then reset priv onto the new Build if it succeeded
agentbox build-log base             # the running or newest Build's log (--json --after-cursor C)
agentbox set-image priv base --build 20261101-1530   # back to an earlier Build at the next Reset
agentbox set-image priv rust        # another Image, always its current Build, at the next Reset
agentbox set-claude-token priv      # store a token from `claude setup-token`, checked first
agentbox github-app                 # where each GitHub App is installed, owners without one, install links
agentbox github-tokens priv         # repositories priv got tokens for, refused requests
agentbox set-github-app 123456 < key.pem   # add an App or replace its key, checked with GitHub first
agentbox remove-github-app 123456   # the token service no longer uses this App
agentbox pair priv                  # current Orca pairing code (starts the VM if needed)
agentbox pairings priv              # the VM's Paired devices
agentbox logs priv                  # the VM's journal, current boot, last 200 entries
agentbox logs priv --unit agentbox-orca.service --since -1h
agentbox logs priv --priority err --grep timeout --boot all --json
agentbox activity                   # Activity: sign-ins, actions, Alerts, GitHub tokens, Builds, Resets
agentbox activity --kind alert,github --vm priv --since -7d --search likt0r
```

`agentbox logs` filters: `--unit`, `--priority`, `--since`/`--until` (`-30min`, `-2h`,
`today`, `2026-10-29 10:00`), `--boot current|all`, `--grep`, `--lines` (1–5000),
`--after-cursor` (continue after the `cursor` of an earlier `--json` answer).

Allowlist entries apply to one VM (`allow <vm> …`) or to all VMs (`allow all …`) and cover
subdomains. Without `--for` an entry is permanent; `--for 15min|1h|8h|7d` makes it a
Temporary allowlist entry (`--temporary`: the default duration, one hour), `--note TEXT`
says why it is there. Allowing a domain that is already allowed never shortens its entry.
`extend <vm|all> <domain> --for D` adds time to a temporary entry, `make-permanent` drops its
expiry, `set-note` changes the note, `disallow` removes it. Destinations with strong Signals
need `--force` for `allow`, `extend` and `make-permanent`; the Control Room never passes it.
While the Watchlist cannot be read, every destination needs it: which Signals are strong is
unknown then.
An entry can also be a pattern, a **Regex entry**: `--regex PATTERN` in place of the domain,
in every command of the four lists (Allowlist, Watchlist, Legitimate domains, Harmless marks),
also to remove or change it ([ADR 0013](docs/adr/0013-regex-entries-in-every-list.md)). It
matches the whole host name (`re.fullmatch`, lowercase), not subdomains of its own accord. A
pattern has at most 200 printable ASCII characters without spaces and no uppercase letters,
backreferences, lookarounds or quantifiers inside a repeated group (for
`^([a-z0-9-]+\.)+example\.org$` take a domain entry, or `^[a-z0-9.-]+\.example\.org$`), and must match none of a
fixed sample of unrelated hosts (`.*\.com` is refused; on the Watchlist it is fine, a broad
pattern there only raises false Alerts). A regex Allowlist entry never allows a destination
with strong Signals, which the proxy checks on every connection (so `--force` does not apply),
and nothing while the Watchlist cannot be read. `allow --regex` first lists the hosts of the
scope's proxy log of the last 7 days the pattern would allow and adds it only with
`--confirm-pattern`; `agentbox check-regex` tells whether a pattern can be an entry of a list
and which hosts it matches. A regex Legitimate domain lifts only weak Signals. A stored
pattern the rules refuse (a hand edit, or stricter rules in a later Release) counts as nothing,
on the Watchlist keeps Open web closed, and is listed with the reason until you remove it. In the Control
Room, the add form of each list switches between *Domain* and *Regex*; a test field checks a
pattern as adding it would and says whether a host matches it. For the Allowlist the same check
shows the preview, and the entry is added only after the confirmation, which a changed pattern
or scope takes back. Regex entries are marked as such and show their pattern verbatim.
When a temporary entry expires, new connections are refused but tunnels already open keep
running; a timer removes expired entries every minute and records each in Activity
(`agentbox-allowlist-expire.timer`). Removing an entry, or the end of Open web, closes the
tunnels that nothing else allows any more
([ADR 0005](docs/adr/0005-ending-access-closes-only-disallowed-tunnels.md)).

The hand-maintained Watchlist and the Legitimate domains are records on the Host
(`/etc/agentbox/watchlist.json`, `legitimate.json`), edited with the CLI or in the Control
Room's Watchlist view, and every change is recorded in Activity
([ADR 0009](docs/adr/0009-watchlist-and-legitimate-domains-live-on-the-host.md)).
`watch-add <domain> --category C` takes `exfil`, `c2`, `tunnel`, `paste`, `phishing`,
`blockchain` (strong) or `payload` (weak), plus `--source` (the incident) and `--note`;
`watch-set-note` and `legitimate-set-note` change notes. A Legitimate domain gets no pattern
Signals except the blockchain pattern (`rpc.`, `mainnet`, `infura`, …), and Feed hits for it and its
subdomains do not count; the Feeds are stored as fetched, so this takes effect, and ends, at
once. Changes that would lift strong
Signals need `--force`, and only the CLI on the Host passes it:
- removing a Watchlist entry of a strong category, or lowering it to `payload`;
- adding a Legitimate domain that has strong Signals itself or has threat-intelligence entries
  at or below it (also ones a Legitimate domain of the Host lifts now), or any while the
  threat-intelligence list has not been fetched yet.

The built-in Legitimate domains (registries and vendors such as `github.com`) cannot be
removed. `proxy/suspicious-domains` and `proxy/legitimate-domains` only fill a new Host:
`agentbox setup` installs them where there is no record, no text file and no `*.migrated` file,
and `agentbox migrate-lists` takes them over once (a domain listed twice keeps its strongest
category; lines it cannot take it names, and until then a change from the Control Room refuses
to migrate). Later changes to them do not reach an
existing Host, and a record lost after the migration is not refilled from them: `agentbox setup`
says how to restore it.

While the Watchlist cannot be read (`watchlist.json` broken, also in a single entry, or missing
without the text file from before, `suspicious-domains`), Open web lets nothing through and `open-web` is refused;
Allowlist entries keep working. `agentbox watchlist` and the Control Room's Watchlist view
say why. A missing `watchlist.json` is created again only by `watch-add --force` on the Host, the
admin's repair path: it opens Open web again, so the Control Room cannot. `migrate-lists`
creates none without the text file.

`agentbox create <name>` takes `--image` (an Image with a Build, `base` by default), `--memory-mib` (default
26624, at most the Host's memory), `--data-disk-gib` (default 300), `--owner` (repeatable)
and `--copy-allowlist-from <vm>` (a one-time copy of that VM's own Allowlist entries;
deleting a VM removes its own entries). Names are 2 to 16 characters of `a-z`, `0-9` and `-`,
start with a letter and cannot be changed later; words the CLI or the Control Room use
themselves (`off`, `all`, `status`, `login`, `settings`, `v1`, …) are refused. The VM gets the
lowest free network number and the Orca port 6800 + that number. It is not started:
`agentbox pair <name>` starts it and prints the Pairing code.

`agentbox set-memory <name> <mib>` changes the memory limit later, within the same bounds
as `create` (1024 MiB up to the Host's memory). It writes the record and the VM's libvirt
definition at once and says when the limit applies: a VM that is off gets it at its next
start; a running VM keeps running with its current memory until its next Restart or Reset
(`agentbox restart <name>`; a reboot inside the VM is not enough). A paused VM is refused,
because its saved memory belongs to the old definition: start it first. The Control Room
changes it on the VM's overview (`PUT /api/v1/vms/<name>/memory`).

`agentbox delete <name> --confirm <name>` stops the VM and removes everything the Host
had for it. The System disk is deleted; the Data disk is moved to
`/var/lib/agentbox/archive/<name>-data-<time>.qcow2`, or deleted with `--delete-data-disk`.
An interrupted delete is finished by running it again. To bring an archived Data disk back,
create the VM again and, before its first start, move the archived file over
`/var/lib/agentbox/vms/<name>-data.qcow2`.

In the Control Room API, `POST /api/<name>/create` takes `image`, `memory_mib`,
`data_disk_gib`, `github_owners` and `copy_allowlist_from`; `POST /api/<name>/delete` needs
`{"confirm": "<name>"}` and takes `"delete_data_disk": true`. Both answer with the JSON of
`agentbox create|delete --json`.

Pairings in the API: `POST /api/<name>/pairings` answers with the JSON of `agentbox pairings
--json`; `POST /api/<name>/revoke-pairings` needs `{"confirm": "<name>"}`; `POST /api/<name>/unpair`
takes `{"device": "<id>"}` and is always refused (see above); `POST /api/<name>/pair` reveals
the Pairing code only with `{"confirm": true}`.

Credentials are read from stdin by the CLI and from the request body by the Control Room,
and never shown again: not in output, error messages, logs or Activity.
`agentbox set-claude-token <vm>` takes the token from `claude setup-token`, checks it with
one minimal request to Anthropic and replaces the VM's token only if Anthropic accepts it
(it takes effect at the VM's next start). `agentbox set-github-app <APP_ID>` takes an
App's private key (`.pem`) the same way, adding the App or replacing its key.
`agentbox github-app` lists every App's installations with the VMs whose GitHub owners rely
on each, the owners of VMs where no App is installed, and each App's link for installing it.
An App GitHub cannot be asked for (a revoked key, an error) is listed with its error and counts
as installed nowhere: its owners show as missing until it is fixed, and the token service
refuses there too. `agentbox github-tokens <vm>` lists, per
repository, the tokens the VM got and its refused requests with the reason; it takes
`--since`, `--until` and `--lines` (default 1000) like `activity`. In the Control Room API,
`POST /api/<vm>/claude-token` takes `{"token": "..."}` and answers with a message;
`POST /api/github-app` takes `{"app_id": "123456", "private_key": "-----BEGIN ..."}`, and
it, `GET /api/github-app` and `GET /api/<vm>/github-tokens?since=-7d&lines=200` answer with
the JSON of the command.

The Allowlist in the Control Room API: `GET /api/allowlist` lists every entry (`?vm=<vm>`:
those that apply to a VM, `?vm=all`: those for all VMs) as `agentbox allowlist --json` does,
with `scope`, `expires`, `remaining`, `expired`, `note`, `added`, `added_by` and `uses_24h`.
`POST /api/<vm|all>/allow` takes `domain`, `duration` (`15min`, `1h`, `8h`, `7d`, `default`
for the default temporary duration, or `permanent`, the default) and `note`; this is also how
a blocked domain is allowed from a VM's blocked list. `POST /api/<vm|all>/extend` takes
`domain` and `duration`, `make-permanent` and `disallow` take `domain`, `set-note` takes
`domain` and `note`. Destinations with strong Signals are refused with the reason (HTTP 400);
only the CLI can allow them, with `--force`.

### Viewing a dev server

A dev server an agent starts in a VM (`npm run dev`, `nuxt dev`, …) is opened in Orca's
built-in browser on the laptop: in the worktree's Ports panel, **Open in Browser** on the
port, or `http://localhost:<port>` in a new browser tab of the VM's workspace. The page is
shown on the laptop, but Orca carries its connections through the pairing connection and
`orca serve` makes them inside the VM, so `localhost` is the VM and no port of the VM is
opened on the Host. The laptop's own browser cannot reach it. Orca's setting *Remote server
workspaces* stays on *This device* (the default); rendering on the server is not available
with `orca serve`.

Whatever the page loads from the internet (fonts, CDNs, APIs) is fetched by the VM through
the proxy, so the Allowlist applies as to everything else: only HTTPS to allowed domains
gets through, and what is missing shows up in `agentbox blocked <vm>` and needs an Allowlist
entry or Open web.

This needs Orca 1.4.232 or later on the laptop. Older versions show these tabs as pages of
the laptop, where `localhost` is the laptop ("Can't reach 127.0.0.1:3000"), and their Ports
panel offers only the system browser. A tab opened before the update stays that way; open
it again.

### Containers in a VM

Agents run containers with the agent's rootless Podman. `docker` is Podman under that name
(`podman-docker`), so `docker run` and `docker build` are `podman run` and `podman build`.
`podman compose` and `docker compose` both run [Docker Compose](https://docs.docker.com/compose/)
(the `docker-compose` CLI plugin), which builds and starts containers through the agent's
Podman socket. Tools that use the Docker API reach that socket through `DOCKER_HOST`, which
is set for services and shells. Containers have no DNS and no route out, the same as the rest
of the VM. Short image names such as `postgres:16-alpine` resolve to docker.io, as with Docker
(`/etc/containers/registries.conf.d/50-agentbox.conf`), not to Fedora's registries first.

`podman build` and `docker build` get the Host's proxy: Podman passes it into a Dockerfile's
`RUN` lines, and the VM's `/etc/hosts`, which has `host.agentbox`, comes along
(`/etc/containers/containers.conf.d/50-agentbox.conf`). Builds through the Docker API on the
Podman socket get no proxy, because Podman passes none there: Compose builds, and image
builds of docker-py or Testcontainers. Such a build that needs the internet gets the proxy as
build arguments. In a compose file:

```yaml
services:
  app:
    build:
      context: .
      args:
        https_proxy: ${https_proxy:-}
        HTTPS_PROXY: ${HTTPS_PROXY:-}
```

In the VM these take the proxy from the agent's environment; elsewhere they are empty, which
means no proxy. `https_proxy` and `HTTPS_PROXY` are predefined build arguments, so the
Dockerfile needs no `ARG` for them.

Running containers do not get the proxy, by the owner's decision: compose services call each
other by name over HTTP (`http://api:8080`), and with proxy variables set many programs (Bun,
Go, curl, Python) send those calls to the Host's proxy, where they fail with `405`. No general
`NO_PROXY` can list every service name. A service that needs the internet gets the proxy by
hand in its compose file, with the services it calls in `NO_PROXY`:

```yaml
services:
  worker:
    environment:
      HTTPS_PROXY: http://host.agentbox:3128
      https_proxy: http://host.agentbox:3128
      NO_PROXY: localhost,127.0.0.1,api,db
      no_proxy: localhost,127.0.0.1,api,db
```

The Allowlist applies as to everything else, and a tool that ignores the proxy variables
gets nowhere.

The proxy lets only HTTPS through, so a Dockerfile must fetch packages over HTTPS from
allowlisted mirrors. Plain HTTP gets `405 Method Not Allowed`, which hits Debian's and
Ubuntu's default `http://` apt sources. Switch them before the first `apt-get`, in a base
image that already has `ca-certificates` (`node`, `python` and `buildpack-deps` do,
`debian` does not):

```Dockerfile
RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/* /etc/apt/sources.list 2>/dev/null; \
    apt-get update
```

### Images and Builds

An **Image** is defined in this repository, ships with each Release and is built on the Host
from the installed Release's files (`/usr/share/agentbox/vm`). Editing happens here, through
reviewed commits, never in the Control Room; a change reaches a Host with the next Release it
installs, and its VMs with their next Reset after a Build.

- `vm/Containerfile` is the **Base image**: Fedora bootc with the agents, Orca and every
  **Safeguard** (`vm/files/`, and `package-safeguards`, which writes the package minimum age
  for npm, pnpm, Bun and uv).
- `vm/images/<name>/Containerfile` is another Image (name: `a-z`, `0-9`, `-`, at most 32
  characters). Its directory is the build context, and it starts from the Base image's
  current Build:

  ```Dockerfile
  ARG BASE
  FROM ${BASE}
  RUN dnf -y install --setopt=install_weak_deps=False clang lld && dnf clean all
  ```

A **Build** runs as the systemd unit `agentbox-build@<image>.service`; its journal is its
log (`agentbox build-log <image>`, or `journalctl -u agentbox-build@<image>`). It takes
the installed Release's Containerfiles and guest files, records which Release they came
from (`agentbox images` and the Control Room show it), builds the container image with
podman (`package_min_age_days` and `orca_version` of the Settings go into the Base image), checks every
Safeguard in the built filesystem, makes the disk with bootc-image-builder and only then
makes it the Image's current Build. A Build that fails or whose check finds a Safeguard
missing or weakened is discarded: the previous Build stays in use, and the failure is in
the log and in Activity (kind `build`). Builds run one at a time; `agentbox build --all`
(and the weekly timer, `rebuild_day` and `rebuild_time` of the Settings) builds the Base
image first, then every other Image on it.

The check verifies, in every Build: npm's global configuration ignores install scripts and
has `min-release-age` (and npm is at least version 12), uv, pnpm and Bun have the same
minimum age, Git takes hooks only from the Image and its `pre-push` hook refuses `main` and
`master`, every service goes through the Host's proxy and there is no DNS, the guest-agent
wrappers are in place, Claude Code fetches none of the owner's claude.ai connectors
(`ENABLE_CLAUDEAI_MCP_SERVERS=false` for every service and shell), and no environment
variable switches any of this off. A derived Image must also be built on the Base Build,
leave every Safeguard file, the Git hooks and the guest-agent wrappers exactly as the Base
has them, and may add files where environment or configuration is read only if they do not
mention a Safeguard. The check reads the files from the Host and never runs anything from
the Image ([ADR 0008](docs/adr/0008-safeguards-checked-from-the-host-after-every-build.md)).

Builds are `/var/lib/agentbox/images/<image>-<build>.qcow2` (build ids are the time,
`YYYYMMDD-HHMMSS`), and `<image>.qcow2` points to the current one. The newest `builds_kept`
per Image are kept, and so is any Build a VM's System disk is backed by or a VM is pinned
to. Each VM names its Image and, optionally, a Build it is pinned to: `agentbox set-image
<vm> <image> [--build B]` changes both, and they take effect at the VM's next Reset, never
in the middle of work. Builds made by the former `vm/build-disk.sh`
(`base-YYYYMMDD-HHMM.qcow2` and the link `base.qcow2`) are Builds of the Base image as they
are; System disks keep their backing files. A derived Image needs one Base Build made by
`agentbox build`.

The Control Room shows each Image's definition on the System page: its Containerfile, linked
on GitHub at the Release tag (or, for a Build from before, the commit) its current Build came
from, and on request its text from the installed Release (`agentbox containerfile <image>`),
which the next Build takes. Nothing is fetched for it; without a known Release or commit the
path stays plain text.

In the Control Room API: `GET /api/images` answers with `agentbox images --json` (each
Image's Builds with status, tool versions, `used_by`, `pinned_by`, and a running Build
with its step, and what is `available`); `GET /api/images/<image>/log?build=&after_cursor=&lines=`
with the Build's log; `POST /api/build` takes `{"image": "<image>"}` or `{"image": "all"}`;
`POST /api/<vm>/set-image` takes `{"image": "...", "build": null or "<build>"}`. For live
progress the UI polls the log with `after_cursor` while `GET /api/images` shows a Build running.

#### Tool versions

Every Build records the versions of Orca, Claude Code, codex, opencode and the Fedora release
it contains, read from its files after the Safeguard check (unknown when they cannot be
read, and for Builds made before). Where they come from: Orca is the RPM of its GitHub
release (`stablyai/orca`), the newest that is no draft or prerelease and was published at least
`package_min_age_days` ago, or the one `orca_version` pins; the Build container downloads it
over HTTPS only and installs it only when its SHA-256 matches the one GitHub lists for it, and
does the same for the Node runtime of Orca's server, checked against the checksums that Orca
release pins (`vm/build/orca-release`). Any mismatch fails the Build. The npm tools are the
newest versions published at least `package_min_age_days` ago (npm `--before` in
`vm/Containerfile`); the packages are Fedora's at the time of the Build.

Once a day, two hours before the Reset time (`agentbox-versions.timer`, from `reset_time`;
at once: `sudo systemctl start agentbox-versions`) the Host checks what a new Build would
bring: for the npm tools the version the Build would pick now, from the npm registry; for Orca the release the Build would take now, from GitHub
(by the same rule, or the pinned one); for the Base image the number of Fedora package updates for its
current Build, counted with `dnf check-update` in a container from it. The questions to the
npm registry and GitHub run as the unprivileged service user `agentbox-feeds`
(`agentbox fetch-versions`); root only checks its answer and counts the packages
(`agentbox check-versions`). `agentbox images` and the
Control Room (System page, and each VM's page for the Build it runs) show the versions next
to what is available and mark an Image a new Build would update. A check that fails shows
"unknown" and never blocks a Build or a Reset.

An **Update** brings new versions into VMs: a Build of the Image, and when it succeeds, a
Reset of the VMs you pick onto it (`sudo agentbox build base --reset priv --reset work`, or
Update on the Image in the Control Room, which asks first). Sessions in those VMs end, their
Data disks stay; a VM pinned to a Build cannot be picked, and a failed Build resets nothing.
Each step (the Build, each Reset) is in Activity. Nothing is ever updated inside a running
VM: the next Reset would undo it, and it would skip the Safeguard check of a Build.

**Automatic updates** (`auto_update`, on by default): when the daily check finds an update
for an Image (a tool a new Build would change, or package updates), it starts the Build at
once instead of waiting for the weekly rebuild, in the unit `agentbox-build@update`; when the
Base image has one, every Image is built on the new Base Build. The minimum age and the
Orca pin still decide what is installed. Changing `orca_version` or `package_min_age_days` re-runs the version check (via `regenerate`), which with `auto_update` on can start a Build at that time of day; Resets still happen only at `reset_time`. A Build that runs already (the run lock is held) or
an update that runs keeps it from starting; the next day's check tries again. The daily Reset
check then also resets every VM whose Image has a newer current Build than its System disk,
however young the disk, with the same rules as the Reset by age: only when no Orca client is
connected and no agent works, else it is postponed to the next night; a VM pinned to a Build
is left alone. Activity shows the start (`build.automatic`), each Build and each automatic
Reset with the reason `update`. With `auto_update=false` new versions come with the weekly
rebuild and VMs get them at their Reset by age, as before.

**Update now** (`sudo agentbox update-now [--reset <vm>]...`, or Update now on the System
page of the Control Room, which asks first in the dialog of an Update) does all of it at
once: the version check (the fetch as `agentbox-feeds` through its unit, then the root
part), a Build of every Image with an update (none when nothing is new), and, when every
Build succeeded, a Reset of the VMs you picked onto the newest Build of their Image. Sessions
in them end, their Data disks stay; a VM pinned to a Build cannot be picked, and a failed
Build resets nothing. Progress: `journalctl -fu agentbox-build@update`.

### Activity
`agentbox activity` shows the newest entries first; filters: `--kind` (`signin`, `action`,
`allowlist`, `alert`, `github`, `pairing`, `build`, `reset`, `settings`, `credentials`, several
separated by commas),
`--vm`, `--since`/`--until` (as for `logs`), `--search` (text, any case), `--lines`
(1–5000, default 200). Activity is kept for 90 days; to change that, see Settings below
(`sudo agentbox set-settings activity_retention_days=30`).

### Settings
`agentbox settings` shows every setting with its value; defaults are marked, and settings that
take effect later (the package minimum age at the next Build, session length at the next
sign-in, ...) say so. `sudo agentbox set-settings KEY=VALUE...` changes one or more; every
value is validated first and nothing is saved when one is invalid (`VALUE` `default` goes
back to the built-in default and removes the key from the file, also when it holds the
default value, so a later change of the built-in default applies). Saving a changed value
regenerates what depends on it, so no restart by hand is needed: the timers of the daily Reset check (`reset_time`), of the weekly rebuild
(`rebuild_day`, `rebuild_time`) and of the Feeds (`feed_interval_hours`), the Feeds service
(`young_domain_days`), the version check (`package_min_age_days`, `orca_version`; a change
checks at once; its time follows `reset_time`) and the retention of Activity. Without `/etc/agentbox/settings.json`
every setting has its default; a broken file is an error naming the file and the value.

| Key | Default | Allowed |
| --- | --- | --- |
| `reset_age_days` | 7 | 1–365, age of the System disk for the automatic Reset |
| `reset_time` | 04:00 | HH:MM, when the daily check runs (the version check runs two hours before) |
| `auto_update` | true | true, false: Images with an update are built after the daily version check, and the daily Reset check moves idle VMs onto the new Build (see [Tool versions](#tool-versions)) |
| `rebuild_day`, `rebuild_time` | sun, 03:00 | mon…sun, HH:MM, the weekly Build of every Image |
| `builds_kept` | 3 | 1–20, Builds kept per Image (from the next Build) |
| `default_memory_mib`, `default_data_disk_gib`, `default_image` | 26624, 300, base | defaults of `create` |
| `open_web_max_minutes` | 240 | 1–1440, longest Open web |
| `temporary_entry_default` | 1h | 15min, 1h, 8h, 7d (`allow --temporary`, and `duration: default` in the API) |
| `young_domain_days` | 14 | 7, 14, 21, 28, 35 (days of the newly-registered list) |
| `feed_interval_hours` | 12 | 1–168 |
| `package_min_age_days` | 2 | 1–30 (applies at the next Build), also for the Orca release a Build takes |
| `orca_version` | empty | empty: every Build takes the newest Orca release at least `package_min_age_days` old; a version such as `1.4.250` pins that release, for when a newer one causes trouble (`orca_version=` or `orca_version=default` goes back; applies at the next Build) |
| `activity_retention_days` | 90 | 1–3650 |
| `session_hours` | 12 | 1–168 (next sign-in) |
| `default_language` | browser | browser, en, de: the Control Room's language after sign-in in a browser whose language switch is on Automatic (at once in the browser that saves it, elsewhere at the next sign-in or page load); `browser` is the browser's language, English if it offers neither. It does not apply on the sign-in page |

The Control Room API has `GET /api/settings` and `POST /api/settings` with a JSON object of
changes (only these keys; anything else is refused). Password, home network and ports are
not Settings and cannot be changed from the browser.

Every command also takes `--json`, and `--actor TEXT` before the command names who caused
it in Activity (the Control Room passes the browser's device and address, timers pass
`timer`; without it, the user who ran `sudo`). Commands other than `logs` and `pair`
need root.

The Control Room offers the same in the browser; creating and deleting VMs, the GitHub
App, the token list, the Allowlist with scope and temporary entries, and Images and Builds
only through its API until the new UI has views for them. The new UI talks to the
versioned API `/api/v1`, described with its errors and live events in
[docs/control-room-api.md](docs/control-room-api.md); `agentbox describe [<vm>] --json`
shows a VM as the Control Room does, and `agentbox watch` is the root helper whose events
the backend streams to every browser.

## Tests

The tests need no root, libvirt or VM. From the repo root:

```bash
python3 tests/run_parallel.py                  # all tests, one worker per CPU
python3 tests/run_parallel.py -v test_vms      # one module (or class), with the time per test class
python3 -m unittest discover -s tests          # fallback: the same tests one after another
```

## Status

Working on one Host. VMs are described in a registry instead of a fixed pair (`work`,
`priv`) and are created and deleted with the CLI or the Control Room API. Images other than
the Base image can be added in `vm/images/`; every Build is checked for the Safeguards and
built weekly from the installed Release. The Control Room
is being rebuilt with Nuxt UI. Its backend already collects the Host's readings (CPU, GPU,
memory, NVMe temperatures, memory per VM) with 30 minutes of history and streams them live,
together with VM state, Activity, Alerts, GitHub tokens, Build progress, a needed Host
reboot, notifications and the installed UI's version. The new UI (source in `web/ui/`,
built in CI and not committed, see [web/ui/README.md](web/ui/README.md)) has sign-in, the
live VM list with the connection state, English and German; its other views are
placeholders that the next steps fill. Each Release brings its build. See the ADRs and
ARCHITECTURE.md for what is in place.
