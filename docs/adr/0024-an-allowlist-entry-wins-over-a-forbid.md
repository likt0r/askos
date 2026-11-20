# An Allowlist entry wins over a Forbid, and the two never overlap

Status: Proposed (#39), with the owner's decisions of 2026-11-20 below. ARCHITECTURE.md's Network section, GLOSSARY.md ("Forbid") and docs/claims.md change with the code.

Since the Hold ([#39](https://github.com/likt0r/askos/issues/39), first step) the proxy keeps a connection to a destination that nothing allows waiting for up to `hold_seconds`, and the Control Room notifies the owner. A destination the owner does not want, telemetry or an ad host a program asks for again and again, is held and notified each time. The owner decided on 2026-11-20 that a **Forbid** settles it: per VM, for the durations an Allowlist entry has (15 minutes, 1 hour, 8 hours, 7 days, or permanent), removable and changeable as an Allowlist entry is, in the Control Room and on the CLI. While it holds, the proxy refuses the destination at once, without a hold or a notification; when it ends, the destination is unknown again. Open web lets through everything that is not explicitly forbidden, so a Forbid blocks there too.

A Forbid and an Allowlist entry can name the same destination: an entry for all VMs and a Forbid for one, a Forbid of a domain and an entry of its subdomain, a Regex entry and a Forbid of a host it matches. Which one decides has to be one rule, and the lists should never show both for a destination where only one can hold.

## Decision

- **A Forbid is stored as an Allowlist entry is.** `/etc/agentbox/forbids.json`, root-owned and readable by the proxies, in the Allowlist's record format (`domain`, `scope`, `expires`, `note`, `added`, `added_by`), checked by the same code for agentbox and the proxy (`agentbox/list_records.py`, `forbid_entries`). Its scope is always one VM, and it names a domain, never a pattern. It covers the domain and its subdomains, as a domain Allowlist entry does. The proxy reads the file again when it changes; a broken file keeps the last good state, and a proxy that starts with one refuses at once whatever no Allowlist entry allows, without a hold and also in Open web, until it is fixed.
- **The proxy decides in this order**: strong Signals and an unreadable Watchlist first ([ADR 0021](0021-strong-signals-always-block.md)), then the Allowlist, then the Forbids, then Open web, then the Hold. So an Allowlist entry wins over a Forbid; a Forbid refuses what no entry allows at once (`BLOCKED <host> (forbidden)`), in Open web too, and a held connection whose destination is forbidden meanwhile is refused at once.
- **The two never overlap for a destination.** Adding a Forbid of a domain for a VM removes the VM's own Allowlist entry of exactly that domain (the newer decision takes its place; Activity `allowlist.entry-forbidden`), and is refused (`forbid_overruled`) while another entry in force still allows the domain for the VM: one for all VMs, one of a parent domain, a Regex entry that matches it. That entry has to go or be narrowed first. Adding an Allowlist entry (`allow`), and `extend` or `make-permanent`, which can bring an expired entry back, remove the Forbids the entry covers within its scope (Activity `allowlist.forbid-lifted`). A subdomain allowed on its own stays allowed below a forbidden parent: the entry covers less than the Forbid, and the rest of the Forbid holds.
- **Access that ends on purpose closes tunnels** ([ADR 0005](0005-ending-access-closes-only-disallowed-tunnels.md)): a Forbid that takes the place of an entry removes it before it expired, so the proxy closes the tunnels nothing else allows; Open web no longer keeps a tunnel a Forbid covers, and a tunnel only Open web allows closes once a Forbid covers it (`CLOSED <host> (forbidden)`).
- **Expiry** works as for a Temporary allowlist entry: an expired Forbid refuses nothing at once, and `agentbox expire-allowlist` (the timer, every minute) removes it and records `allowlist.forbid-expired`.

## Consequences

- What a list shows holds: no Forbid is listed that an entry overrules, and no entry of a VM sits beside a Forbid of the same domain for it.
- Forbidding a destination an entry for all VMs allows needs that entry removed or narrowed first; there is no per-VM exception from an entry for all VMs.
- Allowing a parent domain for all VMs lifts every VM's Forbids below it, each in Activity.
- A broken `forbids.json` (only a hand edit can break it) stops a starting proxy's Hold and Open web until it is repaired; entries keep working.

## Considered Options

- **A Forbid wins over an Allowlist entry**, a per-VM exception from an entry for all VMs: rejected. The proxy would have to close entry-allowed tunnels when a Forbid comes, and the Allowlist would show entries that do not hold for some VMs.
- **Forbids as Allowlist entries with a flag** in `allowlist.json`: rejected. Every reader of the Allowlist (the proxy, uses, previews, copies to a new VM, the Control Room) would have to tell the two apart, and one that missed it would allow what is forbidden.
- **Accept a Forbid an entry overrules, with a warning**: rejected. The Forbid would be listed while it refuses nothing.
- **Regex Forbids**: not now. A Forbid comes from one blocked destination in the Control Room; a pattern would need the preview and confirmation of a Regex entry.
