# Agent Box

A machine (the Host) that runs coding agents inside isolated VMs, with outbound traffic limited to an allowlist and short-lived credentials. The user operates it through Orca and the Control Room, from other laptops or on the Host itself.

**Control Room**:
The web interface on the Host for operating VMs, the allowlist, alerts and credentials (German UI: Leitstand).
_Avoid_: dashboard, admin panel, web UI

**Settings**:
The one root-owned record (`/etc/agentbox/settings.json`) of what the owner can tune: Reset age and time, rebuild schedule, Builds kept, defaults for new VMs, longest Open web, default temporary-entry duration, young-domain threshold, feed interval, package minimum age, Orca version, automatic updates, Activity retention, session length and language (German UI: Einstellungen). Every value has a built-in default. The Control Room password and everything in the Host setup are not Settings; they change only on the Host.
_Avoid_: config, preferences, options

**Activity**:
The timeline of what happened on the Host and who caused it: sign-ins, actions, Allowlist changes, Alerts, GitHub tokens, credential changes, pairings, Builds and automatic Resets, kept for a configurable number of days (German UI: Aktivität).
_Avoid_: audit log, history, event log

**Actor**:
Who caused an Activity entry: a device and address in the Control Room, a timer, a connection to a VM's Orca port, `watch` for a VM it found Not responding, or the user at the terminal.
_Avoid_: user, initiator, source

## Machines

**Host**:
The machine that runs the VMs, the proxy and the credential services: a home server or the user's own laptop.
_Avoid_: server, box, hypervisor

**VM**:
An isolated virtual machine in which agents work; the unit of separation, with its own network, credentials, GitHub owners and allowlist entries.
_Avoid_: context, environment, sandbox, container

**VM registry**:
The root-owned records on the Host that define which VMs exist, one per VM: name, network number, Orca port, Image, optional Build pin, memory limit, Data disk size, GitHub owners, VM variables and creation date. Everything else about a VM is generated from its record.
_Avoid_: inventory, VM config

**VM variables**:
Environment variables of one VM, kept in its record and passed in at its next start, for `orca serve`, the agents and terminals it starts, and login shells (German UI: VM-Variablen). They are not secret; the Host refuses every name a Safeguard or Agent Box itself reads, so none switches a Safeguard off.
_Avoid_: env vars, environment, env

**Image**:
A clean operating-system template, defined by a Containerfile shipped in the installed Release and built on the Host, from which a VM's System disk is created; each VM uses exactly one Image.
_Avoid_: base disk, snapshot, template

**Base image**:
The Image every other Image builds on; it carries the agents and all Safeguards, which derived Images cannot remove.
_Avoid_: root image, parent

**Safeguard**:
A protection the Base image sets up inside every VM: package minimum age and no install scripts, Git hooks only from the Image, the Host as the only resolver and no proxy variables, the guest-agent wrappers, Claude Code without the owner's claude.ai connectors. Every Build is checked for all of them and discarded if one is missing or weakened.
_Avoid_: protection, hardening, policy

**Build**:
One built and checked version of an Image, named by the time it was made; the newest one is the Image's current Build, recent Builds are kept, and a VM can be pinned to an earlier one.
_Avoid_: image version, tag

**System disk**:
A VM's disposable disk, recreated on every Reset from its Image's current Build or the Build it is pinned to.
_Avoid_: root disk, OS disk

**Data disk**:
A VM's persistent disk holding repositories, Orca data and agent transcripts; survives every Reset and is archived when its VM is deleted, unless deleting it is asked for.
_Avoid_: home disk, volume

## Delivery

**Release**:
A numbered version of Agent Box itself, named by semantic versioning (`vMAJOR.MINOR.PATCH`), delivered as one package that contains the CLI, the Control Room and the Image definitions. Not a Build, which is a version of an Image.
_Avoid_: version, package, build

**Host setup**:
The root-owned record of how a Host is installed and exposed: its mode, bind address, ports, home network and certificate name. Changed only on the Host, never in the Control Room, and distinct from Settings.
_Avoid_: config, installation settings

**Local-only**:
The mode in which the Control Room and the Orca ports are reachable only from the Host itself, with no firewall rules for any other network.
_Avoid_: offline, standalone

**Network mode**:
The mode in which the Control Room and the Orca ports are also reachable from a named home network.
_Avoid_: LAN mode, remote mode, server mode

## VM lifecycle

**Pause**:
Saving a VM's memory to disk so it resumes exactly where it stopped.
_Avoid_: freeze, suspend, managedsave

**Restart**:
Rebooting a VM while keeping its System disk.
_Avoid_: reboot

**Reset**:
Replacing a VM's System disk with a fresh copy of its Image's Build; the Data disk is kept. A changed Image or Build pin takes effect here.
_Avoid_: wipe, rebuild, reprovision

**Update** (of an Image):
A new Build of an Image followed, once it succeeded, by a Reset of the VMs the owner picked onto it; a failed Build resets nothing. The way new versions of the agents, Orca and the packages reach VMs, never an update inside a running VM (German UI: Aktualisieren). Not installing a new Release, which is updating Agent Box itself. With automatic updates (a Setting, on by default) the daily version check builds every Image with an update at once and the daily Reset check moves idle VMs onto it; **Update now** runs the check, those Builds and the Resets of the VMs picked at once (German UI: Jetzt aktualisieren).
_Avoid_: upgrade, refresh

**Focus**:
Pausing all other VMs so one VM gets the Host's resources.
_Avoid_: exclusive mode

**Not responding**:
The state of a VM that libvirt reports as running but whose Orca has not answered for longer than the wake time limit (120 s): its system did not boot (the guest agent does not answer either), or it runs without Orca. Until that limit has passed after its start, such a VM is **Starting** (German UI: Antwortet nicht, Startet).
_Avoid_: hung, dead, unhealthy, booting

## Network

**Allowlist**:
The set of domains VMs may reach through the proxy.
_Avoid_: whitelist, base list

**Allowlist entry**:
One domain or pattern on the Allowlist (a domain including its subdomains), valid either for all VMs or for one VM (its scope), with a note and who added it when (German UI: Freigabe).
_Avoid_: exception, rule, base list entry

**Regex entry**:
An entry of the Allowlist, the Watchlist, the Legitimate domains or the Harmless marks whose value is a regular expression instead of a domain, matched against the whole host name (no subdomains of its own accord). On the Allowlist it never allows a destination with strong Signals, checked on every connection, and adding it shows what it would allow and needs a confirmation; as a Legitimate domain it lifts only weak Signals (German UI: Regex-Eintrag).
_Avoid_: wildcard, glob, pattern rule

**Temporary allowlist entry**:
An Allowlist entry with an expiry time (15 minutes, 1 hour, 8 hours or 7 days, extendable), after which it allows no new connections and disappears on its own; connections already open keep running (German UI: vorübergehende Freigabe).
_Avoid_: snooze, pass, temporary exception

**Open web**:
A time-limited mode in which a VM may reach any public domain except suspicious ones.
_Avoid_: unblock, web mode

## Detection

**Alert**:
A single event in which a VM tried to reach a suspicious destination; the owner can mark Alerts as seen so only new ones stand out (German UI: Verdacht).
_Avoid_: finding, incident, IOC

**Signal**:
A reason a destination counts as suspicious, such as exfil, threat, lookalike or young (German UI: Merkmal). Strong Signals keep a destination blocked in Open web and off the Allowlist unless forced.
_Avoid_: tag, trait

**Watchlist**:
Known suspicious destinations, maintained by hand on the Host (each entry with a category that becomes its Signal) and from the HaGeZi threat-intelligence feed, each covering its subdomains; they raise Alerts, and those with strong Signals stay blocked in Open web (German UI: Verdachtsliste).
_Avoid_: blocklist, denylist

**Feed**:
An external list fetched by a timer on the Host and looked up locally: HaGeZi's threat-intelligence list (part of the Watchlist) and its list of newly registered domains (the young Signal).
_Avoid_: blocklist, IOC source

**Legitimate domain**:
A well-known domain of a registry, vendor or distribution, kept on the Host or built into the classifier; it gets no pattern Signals except the blockchain pattern, and Feed hits for it and its subdomains do not count. It allows nothing by itself.
_Avoid_: trusted domain, safelist, whitelist

**Harmless mark**:
The owner's judgement that a destination is a false positive: it no longer raises Alerts, but stays blocked wherever its Signals say so (German UI: harmlos).
_Avoid_: exception, ignore, whitelist

## Orca

**Pair**:
Connecting a device to a VM's Orca runtime using a Pairing code.
_Avoid_: register, add environment

**Paired device**:
A device (laptop, or the Host itself) that is paired with a VM. Orca cannot remove a single one for Agent Box; all are revoked together (**Revoke pairings**: the VM's Orca gets a new identity and every device must pair again).
_Avoid_: client, environment

**Observer**:
The Host itself as a Paired device, used only to read agent status. Listed among the Paired devices but never removable; re-paired automatically after Revoke pairings.
_Avoid_: monitor, watcher

## GitHub

**GitHub owner**:
A GitHub account or organisation for whose repositories a VM may obtain tokens.
_Avoid_: org, allowed accounts

**GitHub App**:
An App the Host's token service signs in as to issue GitHub tokens; a Host keeps any number of them, one per trust boundary (an organisation's, a personal one). Each App's ID and private key exist only on the Host and are stored only after GitHub accepted them. For a repository, the first App (in ascending App ID) installed on it issues the token.
_Avoid_: bot, OAuth app, integration

**Installation**:
A GitHub App installed on one account or organisation; a VM gets tokens only for GitHub owners with an Installation of one of the Apps. A GitHub owner of a VM that no App is installed on is a missing Installation (German UI: fehlende Installation).
_Avoid_: grant, org access

## Claude

**Claude token**:
A VM's long-lived token from `claude setup-token`, stored on the Host, passed to the VM at boot and replaced only by one Anthropic accepts.
_Avoid_: API key, login, subscription
