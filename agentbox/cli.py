"""Command dispatch. Each module contributes COMMANDS (name -> handler(args)).

The dispatcher takes the global option --actor before the command and records
every command in Activity (activity.py), except the read-only ones listed in
activity.READ_ONLY: a new command is recorded without doing anything, and a
new read-only command belongs in that list.
"""
import json
import os
import re
import sys

from . import activity, allowlist, container_images, credentials, describe, detection, generate, github, guest
from . import images, lifecycle
from . import lists, network, pairings, paths, settings, setup, transfer, version, versions, vms, watch
from .errors import Fail

USAGE = """\
Controls the agent VMs on this Host.
  agentbox [--actor TEXT] <command> ...              who causes the command (Activity); else the user
  agentbox status [--json]
  agentbox version [--json]                          the version of this Release (also: agentbox --version)
  agentbox describe [<vm>] [--json]                  VMs for the Control Room: state, agents, System disk,
                                                     Image and Build, connected devices
  agentbox setup [--mode local|network] [--bind A --home-network N] [--web-port P]
                [--cert-name N] [--owner-user U] [--password-file F | --password-stdin] [--force] [--json]
                                                     set up the Host: Host setup, Control Room password,
                                                     certificate, Host steps (groups, lists, firewall, units,
                                                     migrations), service; local: this machine only,
                                                     network: also home network N; without options on a
                                                     Host that is set up, only the Host steps
  agentbox watch                                     (Control Room) events as JSON lines until stdin ends
  agentbox create <vm> [--image I] [--memory-mib N] [--data-disk-gib N] [--owner O]...
                [--copy-allowlist-from VM] [--json]    new VM; network and Orca port are picked automatically
  agentbox delete <vm> --confirm <vm> [--delete-data-disk] [--json]
                delete a VM; its Data disk is archived, or deleted with --delete-data-disk
  agentbox set-memory <vm> <mib> [--json]            memory limit (1024 MiB up to the Host's memory); applies
                                                     from the next start, for a running VM after a Restart
  agentbox start|wake|pause|restart <vm> [--json]
  agentbox reset <vm> [--no-start] [--json]          fresh System disk from the VM's Image
  agentbox focus <vm|off> [--json]                   pause every other VM
  agentbox pair <vm> [--json]                        show the current Orca Pairing code (starts the VM if needed)
  agentbox pairings <vm> [--json]                    Paired devices (the Observer marked, not removable)
  agentbox unpair <vm> <device> [--json]             remove a single device: Orca does not support it
  agentbox revoke-pairings <vm> --confirm <vm> [--json]
                revoke all pairings (new Orca identity); the Host pairs again as Observer
  agentbox import <vm> <folder> [<target>] [--json]
                copy a folder of the Host into the VM, unpacked, to ~/code/<target> (default: the folder's
                name); of a Git repository .git and the files Git does not ignore; starts the VM if needed
  agentbox logs <vm> [--unit U] [--priority P] [--since T] [--until T] [--boot current|all]
                [--grep TEXT] [--lines N] [--after-cursor C] [--json]   the VM's journal
  agentbox set-claude-token <vm> [--no-prompt] [--json]   token from `claude setup-token` (stdin)
  agentbox blocked|alerts <vm> [--json]
  agentbox allowlist [<vm>|all] [--json]             Allowlist entries (all, a VM's, those for all VMs)
                                                     with time left and use in the last 24 hours
  agentbox allow <vm|all> <domain> [--for 15min|1h|8h|7d | --temporary] [--note TEXT] [--force] [--json]
                Allowlist entry for one VM or all, permanent or temporary (suspicious ones only
                with --force)
  agentbox allow <vm|all> --regex PATTERN [--confirm-pattern] [--for ...|--temporary] [--note TEXT] [--json]
                Regex entry: never allows strong Signals; without --confirm-pattern it only shows
                which hosts of the proxy log of the last 7 days it would allow
  agentbox check-regex <allowlist|watchlist|legitimate|harmless> --regex PATTERN [<host>...]
                [--scope vm|all] [--json]            whether the pattern can be an entry of that list,
                                                     which hosts it matches, for the Allowlist the preview
                Every command of the four lists takes --regex PATTERN in place of <domain>: a Regex
                entry, matched with re.fullmatch against the whole host name (ADR 0013)
  agentbox disallow <vm|all> <domain> [--json]       remove an Allowlist entry, close open tunnels to it
  agentbox extend <vm|all> <domain> --for 15min|1h|8h|7d [--force] [--json]   extend a temporary one
  agentbox make-permanent <vm|all> <domain> [--force] [--json]
  agentbox set-note <vm|all> <domain> --note TEXT [--json]
  agentbox expire-allowlist [--json]                 (timer, every minute) remove expired Allowlist entries
  agentbox migrate-allowlist [--json]                take over the old Allowlist files once
  agentbox open-web <vm> <minutes> [--json]          allow every public domain for a while (Open web)
  agentbox close-web <vm> [--json]
  agentbox mark-seen <vm> [<domain>] [--json]        mark Alerts (all, or one domain's) as seen
  agentbox check-domain <domain> [--json]            a domain's Signals with their reasons
  agentbox watchlist [--json]                        the Watchlist kept by hand, state of the HaGeZi lists
  agentbox watch-add <domain> --category C [--source S] [--note TEXT] [--force] [--json]
                Watchlist entry (with subdomains), category exfil, c2, tunnel, paste, phishing,
                blockchain or payload; downgrading a strong one only with --force
  agentbox watch-remove <domain> [--force] [--json]  remove an entry (strong ones only with --force)
  agentbox watch-set-note <domain> --note TEXT [--json]
  agentbox legitimate [--json]                       Legitimate domains (added and built-in)
  agentbox legitimate-add <domain> [--note TEXT] [--force] [--json]
                no more pattern Signals (except blockchain) and HaGeZi hits for it and its
                subdomains; lifting strong Signals only with --force
  agentbox legitimate-remove <domain> [--json]       remove an added one (built-in ones stay)
  agentbox legitimate-set-note <domain> --note TEXT [--json]
  agentbox migrate-lists [--json]                    take over the Watchlist and Legitimate domains from
                                                     the old text files once
  agentbox harmless [--json]                         domains with a Harmless mark (no Alerts)
  agentbox mark-harmless <domain> [--json]           no more Alerts (blocks stay)
  agentbox unmark-harmless <domain> [--json]
  agentbox owners <vm> [--json]                      GitHub owners for App tokens
  agentbox set-owners <vm> <owner>... [--json]
  agentbox set-github-app <app-id> [--no-prompt] [--json]   add a GitHub App or replace its private key
                                                     (stdin), checked with GitHub first
  agentbox remove-github-app <app-id> [--json]       the token service no longer uses this App
  agentbox github-app [--json]                       where each App is installed, which VMs need that,
                                                     GitHub owners without an Installation, install links
  agentbox github-tokens <vm> [--since T] [--until T] [--lines N] [--json]
                                                     repositories the VM got tokens for, and refusals
  agentbox observe <vm> [--json]                     pair the Host with Orca in the VM (for agent status)
  agentbox migrate [--json]                          take over work and priv into the VM registry once
  agentbox regenerate [--json]                       networks, units, disks and VM definitions from the registry,
                                                     and the VMs' ports in the firewalld zone libvirt
  agentbox weekly-check [--json]                     (timer, daily) Reset old VMs when idle; with auto_update
                                                     also VMs whose Image has a newer Build
  agentbox cleanup-container-images [--json]         (timer, hourly) in each running VM with the Container
                                                     image cleanup on, remove the Container images no
                                                     container uses that were not used for its days
  agentbox images [--json]                           Images and their Builds, which VM uses which, the versions
                                                     of Orca, Claude Code, codex, opencode and Fedora in them
                                                     and what a new Build would bring
  agentbox containerfile <image> [--json]            the Image's Containerfile in the installed Release (what
                                                     its next Build takes), as text
  agentbox build <image>|--all [--json]              Build from the installed Release (unit agentbox-build@<image>)
  agentbox build <image> --reset <vm> [--reset <vm>]... [--json]
                Update: a Build and, if it succeeds, a Reset of these VMs onto it (sessions in them
                end, the Data disk stays; a failed Build resets nothing)
  agentbox update-now [--reset <vm>]... [--json]    Update now (unit agentbox-build@update): check versions,
                                                     build every Image with an update, then Reset these VMs
  agentbox build-log <image> [<build>] [--after-cursor C] [--lines N] [--json]
                                                     log of a Build (the running or newest one)
  agentbox set-image <vm> <image> [--build B] [--json]
                a VM's Image, pinned to an earlier Build or not; applies from the next Reset
  agentbox build-run <image>|all                     (unit agentbox-build@) run a Build
  agentbox fetch-versions [--json]                   (timer, daily, as agentbox-feeds) ask npm and GitHub for
                                                     the versions available
  agentbox check-versions [--json]                   (timer, then as root) check and take over the answers,
                                                     package updates of the current Builds; with auto_update
                                                     then an Update of the Images with an update; everything
                                                     again: systemctl start agentbox-versions.service
  agentbox settings [--json]                         Settings (defaults, what applies only later)
  agentbox set-settings KEY=VALUE... [--json]        change Settings (VALUE default: the default); all or
                                                     nothing, dependent timers and units are generated
  agentbox activity [--kind K[,K]] [--vm VM] [--since T] [--until T] [--search TEXT]
                [--lines N] [--json]                 Activity: sign-ins, actions, Allowlist changes, Alerts,
                                                     GitHub tokens, pairing, Builds, automatic Resets, Settings

With --json, a failure also goes to stderr as one JSON object:
  {"error": "<code>", "message": "<text>", "params": {...}}"""

COMMANDS = {**guest.COMMANDS, **lifecycle.COMMANDS, **network.COMMANDS, **credentials.COMMANDS,
            **generate.COMMANDS, **vms.COMMANDS, **activity.COMMANDS, **detection.COMMANDS,
            **pairings.COMMANDS, **settings.COMMANDS, **github.COMMANDS, **allowlist.COMMANDS,
            **images.COMMANDS, **describe.COMMANDS, **watch.COMMANDS, **lists.COMMANDS,
            **setup.COMMANDS, **version.COMMANDS, **versions.COMMANDS, **transfer.COMMANDS,
            **container_images.COMMANDS}

# The options that take a value, as USAGE shows them ("--note TEXT", "--for 15min|1h|...").
# Given as --note VALUE, the value is the next argument, whatever it looks like.
VALUED = frozenset(re.findall(r"(--[a-z][a-z-]*) (?:[A-Z<]|[a-z0-9]+\|)", USAGE))


def wants_json(args):
    """--json is among the arguments, and not as the value of an option (`--note --json`)."""
    value = False
    for a in args:
        if value:
            value = False
        elif a == "--json":
            return True
        elif a in VALUED:
            value = True
    return False


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = wants_json(args)
    try:
        actor, args = activity.take_actor(args)
        as_json = wants_json(args)
        cmd = args.pop(0) if args else "status"
        if cmd == "--version":
            cmd = "version"
        if cmd not in COMMANDS:
            print(USAGE, file=sys.stderr)
            return 1
        # Everything except logs and pair needs the files under /etc/agentbox and
        # /var/lib/agentbox, which belong to root. AGENTBOX_ROOT (tests) redirects them.
        if os.geteuid() != 0 and cmd not in guest.NO_ROOT | versions.NO_ROOT and not paths.ROOT:
            raise Fail(f"{cmd} needs root: sudo agentbox {' '.join([cmd, *args])}", error="sudo_failed",
                       command=cmd)
        # the actor (--actor, else the user) of every Activity entry of this call
        activity.set_actor(actor)
        # A checkout's CLI would write units and the like naming the files under /usr,
        # which need not be there or be its own (paths.py)
        if not paths.INSTALLED and not paths.ROOT and cmd not in activity.READ_ONLY:
            raise Fail(f"{cmd}: this agentbox is not installed ({paths.CODE}) and does not change the Host, "
                       "its units would name files that need not exist here; use the installed one: "
                       f"sudo agentbox {' '.join([cmd, *args])}", error="action_failed", command=cmd)
        with activity.command(cmd, args):
            COMMANDS[cmd](args)
        return 0
    except Fail as f:
        sys.stdout.flush()
        report(f, as_json)
        return f.exit
    except KeyboardInterrupt:
        return 130
    finally:
        # journal entries left out because no known writer wrote them: how many, once
        warning = activity.untrusted_warning()
        if warning:
            print(f"agentbox: {warning}", file=sys.stderr)


def report(f, as_json):
    """A failure on stderr. Text: "agentbox: <message>", or another program's own error
    output unchanged (raw). With --json instead one line {"error": <code>, "message":
    <text>, "params": {...}}, which the Control Room reads (docs/control-room-api.md); a raw
    message there without the "agentbox: " of lines it passes on (create)."""
    if as_json:
        message = "\n".join(line.removeprefix("agentbox: ") for line in f.message.strip().splitlines()) \
            if f.raw else f.message
        print(json.dumps({"error": f.error, "message": message, "params": f.params}, default=str),
              file=sys.stderr)
    elif f.raw:
        if f.message:
            sys.stderr.write(f.message if f.message.endswith("\n") else f.message + "\n")
    else:
        print(f"agentbox: {f.message}", file=sys.stderr)
