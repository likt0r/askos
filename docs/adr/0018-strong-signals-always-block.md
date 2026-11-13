# Strong Signals always block, also for destinations on the Allowlist

A domain entry of the Allowlist let its destination through whatever its Signals (#59). Strong Signals were checked when an entry was added, extended or made permanent, where `--force` overrode them, and on every connection only for Regex entries and in Open web. So a Feed hit or a strong Watchlist entry that appeared after the entry was added did not block it, and a broad entry such as `claudeusercontent.com` (on the Allowlist a new Host starts with) let through every subdomain, also one the threat-intelligence Feed lists; the same for any entry above a user-content host (`github.io`, `githubusercontent.com`, #23, #58).

The owner decided on 2026-11-12:

- **Strong Signals always block a connection.** The proxy classifies every destination on every connection (it did so already, for Alerts) and checks the Signals before the Allowlist and Open web: a destination with a strong Signal is blocked whether a domain entry, a Regex entry or Open web would allow it, also an entry added with `--force`. The log names the Signals as for Open web, `BLOCKED <host> (suspicious: <signals>)`, after the `ALERT <host> [<signals>]` line every such connection raises.
- **Only lifting the Signal lets such a destination through,** as before: removing or lowering its Watchlist entry, or adding a Legitimate domain, each with `--force` on the CLI ([ADR 0009](0009-watchlist-and-legitimate-domains-live-on-the-host.md)). No Legitimate domain lifts Feed hits below a user-content host, the blockchain pattern, raw IPs or metadata addresses, so nothing lets those through.
- **While the hand-maintained Watchlist cannot be read, nothing passes (fail closed),** Allowlist entries included: which Signals are strong is unknown then. Every VM stands still until `watchlist.json` is repaired; that is accepted. The proxy logs `BLOCKED <host> (Watchlist unreadable)`, which replaces `(Watchlist unreadable, Open web closed)` and `(Watchlist unreadable, regex entries closed)`; agentbox maps all three to the code `watchlist-unreadable`. A proxy without its classifier lets nothing through either (`BLOCKED <host> (no agentbox-suspicious)`).
- **Running tunnels close** when their destination gains a strong Signal (a Feed update, a new Watchlist entry): `CLOSED <host> (suspicious: <signals>)`, after an `ALERT` line, like `disallow` closes the tunnels of a removed entry. While the Watchlist cannot be read every tunnel closes, `CLOSED <host> (Watchlist unreadable)`. This amends [ADR 0005](0005-ending-access-closes-only-disallowed-tunnels.md).
- **No escape hatch** for false positives below user-content hosts for now; a follow-up if it bites.

Changes take effect as other list changes do, on the next connection, and for open tunnels within the proxy's check interval (once a second). The proxy classifies an open tunnel's destination again only when a file the Signals come from has changed since it was classified (the Watchlist, the Legitimate domains, each also as the text file from before, and the Feeds; `signals_key` in the classifier, a `stat` per file), each host once per change. So the cost per connection is the one it had (the classification for Alerts), and an idle proxy only adds those `stat` calls per check while tunnels are open.

`allow`, `extend` and `make-permanent` refuse a destination with strong Signals, also with `--force`, with `domain_suspicious` ([ADR 0012](0012-the-server-speaks-english-with-stable-codes.md)) and a message that says how to lift the Signal and which Signals nothing lifts: the proxy would block it all the same, and the Allowlist should never hold an entry that cannot work. `--force` keeps one meaning there: while the Watchlist cannot be read, it stores an entry that cannot be checked; the proxy checks it on every connection once the Watchlist is readable again. The Control Room shows the refusal with the destination's strong Signals, how to lift them, and `agentbox check-domain` to see where they come from, instead of an `allow --force` command.

## Consequences

- A Feed false positive below a user-content host, such as `objects.githubusercontent.com`, blocks it in every VM until the Feed drops it, also GitHub release downloads; nothing on the Host can lift it.
- Blockchain endpoints (the blockchain pattern, also below Legitimate domains) were reachable only through `allow --force`; now no VM can reach them.
- Entries that `allow --force` stored before stay on the Allowlist but let nothing through while their Signal lasts; `disallow` removes them. Nothing has to be migrated.
- A broken or missing `watchlist.json` stops every VM's network, not only Open web; `agentbox setup` and the Watchlist commands already say how to repair it.
- The detection self-test checks that a domain entry lets neither a strong destination through nor anything while the Watchlist cannot be read.
- With #11 (pausing a VM on an Alert with a strong Signal) every attempt to reach such a destination would also pause the VM.

## Considered Options

- Keep storing an entry forced for a strong destination and say it stays blocked while the Signal lasts: rejected, the Allowlist would hold entries that cannot work, and the list would no longer say what a VM can reach.
- Drop `--force` from `allow`, `extend` and `make-permanent`: rejected, it still has its job while the Watchlist cannot be read, and the proxy now checks every such entry anyway.
- Classify every open tunnel on every check: rejected, the cost would grow with tunnels times checks for nothing; the Signals change only when one of their files does.
- Keep running tunnels open when their destination gains a strong Signal (ADR 0005 as it was): rejected by the owner, a tunnel to a destination the Feed now names as malicious is exactly the channel out to cut.
- An escape hatch for false positives below user-content hosts (a per-host override that lifts a Feed hit): left for a follow-up; it would be a way to switch off the rule #23 and #58 built in.
