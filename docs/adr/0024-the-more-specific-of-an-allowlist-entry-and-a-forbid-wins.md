# The more specific of an Allowlist entry and a Forbid wins

Status: Accepted (#39), by the owner on 2026-11-20. ARCHITECTURE.md's Network section, GLOSSARY.md ("Forbid") and docs/claims.md change with the code.

Since the Hold ([#39](https://github.com/likt0r/askos/issues/39), first step) the proxy keeps a connection to a destination that nothing allows waiting for up to `hold_seconds`, and the Control Room notifies the owner. A destination the owner does not want, telemetry or an ad host a program asks for again and again, is held and notified each time. The owner decided on 2026-11-20 that a **Forbid** settles it: per VM, for the durations an Allowlist entry has (15 minutes, 1 hour, 8 hours, 7 days, or permanent), removable and changeable as an Allowlist entry is, in the Control Room and on the CLI. While it holds, the proxy refuses the destination at once, without a hold or a notification; when it ends, the destination is unknown again. Open web lets through everything that is not explicitly forbidden, so a Forbid blocks there too.

A Forbid and an Allowlist entry can both match a destination: an entry `github.com` and a Forbid of `gist.github.com`, an entry for all VMs and a Forbid for one, a Regex entry and a Forbid of a host it matches. The owner decided on 2026-11-20 that **the more specific one wins**.

## Decision

- **A Forbid is stored as an Allowlist entry is.** `/etc/agentbox/forbids.json`, root-owned and readable by the proxies, in the Allowlist's record format (`domain`, `scope`, `expires`, `note`, `added`, `added_by`), checked by the same code for agentbox and the proxy (`agentbox/list_records.py`, `forbid_entries`). Its scope is always one VM, and it names a domain, never a pattern. It covers the domain and its subdomains, as a domain Allowlist entry does. The proxy reads the file again when it changes; a broken file keeps the last good state, and a proxy that starts with one refuses at once whatever no Allowlist entry allows, without a hold and also in Open web, until it is fixed.
- **Specificity** (`list_records.specificity`), of the entries and Forbids that match a destination for the VM, from most to least specific:
  1. any named domain above any pattern (a Regex entry); patterns rank among themselves only by scope;
  2. of two domains the longer, counted in labels; the domains that cover one host are all its suffixes, so the longer is a subdomain of the other;
  3. at the same domain one VM's above all VMs'.
  A full tie, the same domain for the same VM, never happens: agentbox stores no such pair (below). Should a hand edit leave both, the Forbid wins.
- **The proxy decides in this order**: strong Signals and an unreadable Watchlist first ([ADR 0021](0021-strong-signals-always-block.md)); then the most specific matching Allowlist entry in force against the most specific Forbid in force that covers the destination, the more specific one winning; then Open web, which never lets through what a Forbid wins for; then the Hold. A Forbid that wins refuses at once (`BLOCKED <host> (forbidden)`), in Open web too, and a held connection for which a Forbid comes to win is refused at once. So a Forbid of `gist.github.com` for `work` beats an entry `github.com`, an entry `api.gist.github.com` beats that Forbid, and a Forbid of `x.example` for `work` beats an entry `x.example` for all VMs.
- **The same domain for the same VM is never both allowed and forbidden.** The newer decision takes the other's place, recorded in Activity: `forbid` removes the VM's own Allowlist entry of exactly that domain (`allowlist.entry-forbidden`); `allow` for one VM, and `extend` or `make-permanent` of such an entry (the last two can bring an expired entry back), remove that VM's Forbid of exactly that domain (`allowlist.forbid-lifted`, JSON `forbids_lifted`). Nothing else is removed: an entry for all VMs, a pattern or a parent domain meets only Forbids that are more specific and keep winning, and a Forbid under a broader entry is the point of forbidding.
- **Access that ends on purpose closes tunnels** ([ADR 0005](0005-ending-access-closes-only-disallowed-tunnels.md)): when the Forbids change, a tunnel closes whose destination a Forbid now wins for that is more specific than any that covered it before (`CLOSED <host> (forbidden)`), whether an entry or Open web allowed it; a Forbid that takes the place of an entry removes it before it expired, so its tunnels close as for `disallow`. A tunnel whose more specific entry merely expired keeps running, as after any expiry.
- **Expiry** works as for a Temporary allowlist entry: an expired Forbid refuses nothing, and `agentbox expire-allowlist` (the timer, every minute) removes it and records `allowlist.forbid-expired`.

## Consequences

- A VM can be kept from one host below a domain all VMs may reach, and allowed one host below a domain it is forbidden, without touching the other VMs' entries.
- What decides is never the order in which entries and Forbids were made, only how specific they are, except for the same domain and VM, where the newer one replaces the older.
- Allowing a domain for all VMs leaves every VM's Forbid of it in place; to let a VM reach it, remove its Forbid or allow the domain for that VM.
- The blocked list in the Control Room applies the same rule to domain entries to mark a destination as allowed or forbidden.
- A broken `forbids.json` (only a hand edit can break it) stops a starting proxy's Hold and Open web until it is repaired; entries keep working.

## Considered Options

- **An Allowlist entry always wins over a Forbid**, a Forbid only replacing the Hold: rejected by the owner. A Forbid could not take one host out of a domain the VMs may reach.
- **A Forbid always wins**: rejected. An entry of one host below a forbidden domain could never work.
- **Forbids as Allowlist entries with a flag** in `allowlist.json`: rejected. Every reader of the Allowlist (the proxy, uses, previews, copies to a new VM, the Control Room) would have to tell the two apart, and one that missed it would allow what is forbidden.
- **An entry for all VMs lifts every VM's Forbid of its domain**: rejected. The Forbid is more specific and would win the moment it was made again; the outcome would depend on the order of commands.
- **Regex Forbids**: not now. A Forbid comes from one blocked destination in the Control Room; a pattern would need the preview and confirmation of a Regex entry.
