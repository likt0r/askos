# Strong Signals always block, also a destination on the Allowlist

A domain entry of the Allowlist let its destination through whatever its Signals. Strong Signals were checked when a domain entry was added, extended or made permanent (where `--force` overrode them), and on every connection only for Regex entries and in Open web. So a Feed hit or a strong Watchlist entry that turned up after an entry was added did not block its destination, and a broad entry let through every host below it: `claudeusercontent.com`, on the Allowlist a new Host starts with, let through `<evil>.claudeusercontent.com` from the threat-intelligence Feed (#59). The owner decided on 2026-11-12 that strong Signals always block.

## Decision

- **Strong Signals come first.** The proxy classifies every destination on every connection, as it already did for Alerts, and blocks one with a strong Signal whatever would allow it: a domain entry, a Regex entry or Open web (`BLOCKED <host> (suspicious: <signals>)`, after the `ALERT` line). Weak Signals still only raise Alerts.
- **While the hand-maintained Watchlist cannot be read, nothing passes** (fail closed), Allowlist entries included: which Signals are strong is unknown then (`watchlist.json` broken, also in a single entry, or missing without the text file from before). The proxy logs `BLOCKED <host> (Watchlist unreadable)`, and the Control Room words it as such a block. Every VM stands still until the file is repaired, which the owner accepted; once it is, entries work again without a restart. A proxy without the classifier, which tells the Signals, lets nothing through either.
- **Running tunnels close** ([ADR 0005](0005-ending-access-closes-only-disallowed-tunnels.md) amended): a tunnel whose destination gains a strong Signal (a Feed update, a new Watchlist entry) closes, after an `ALERT` line, with `CLOSED <host> (suspicious: <signals>)`; while the Watchlist cannot be read every tunnel closes, `CLOSED <host> (Watchlist unreadable)`. The proxy checks this in its check loop, once a second. It classifies its open tunnels again only when a file the Signals come from has changed (the Watchlist and the Legitimate domains, each also as the text file from before, and the Feeds; one `stat` each), each host once.
- **Only lifting the Signal lets such a destination through**, as before: removing or lowering its Watchlist entry (`watch-remove` or `watch-add` with `--force`), or adding a Legitimate domain (`legitimate-add --force`), which lifts Feed hits and name patterns except below the built-in user-content hosts ([ADR 0009](0009-watchlist-and-legitimate-domains-live-on-the-host.md)). Nothing lifts the blockchain pattern, a raw IP or a metadata address.
- **`allow`, `extend` and `make-permanent` refuse a destination with a strong Signal, also with `--force`** (`domain_suspicious`), so the Allowlist holds no entry that cannot work when it is made. The message names how to lift the Signal and points to `agentbox check-domain`; the Control Room shows the same, with the strong Signals and their reasons. `--force` keeps one meaning there: while which Signals are strong cannot be told (the Watchlist cannot be read, or the classifier does not answer) it stores the entry unchecked, which the proxy checks on every connection once it can.

## Consequences

- An entry stored before this with `allow --force` for a destination with strong Signals stays on the Allowlist but lets nothing through; `disallow` removes it. Nothing migrates it.
- No VM can reach a blockchain RPC endpoint (`mainnet.*`, `rpc.*`, `infura`, ...), a raw IP or a metadata address any more: no list lifts them.
- **False positives below a user-content host cannot be lifted.** No Legitimate entry lifts a Feed hit below `github.io`, `githubusercontent.com` or `claudeusercontent.com`, so a Feed entry on, for example, `objects.githubusercontent.com` blocks GitHub release downloads in every VM until the Feed drops it. The owner decided on 2026-11-12 not to add an escape hatch for now; a follow-up if it bites.
- A broken `watchlist.json` stops every VM's network and closes every tunnel until it is repaired.
- Feed updates and Watchlist changes take effect on the next connection, and on open tunnels within the check interval, like other list changes.
- Each connection runs the classifier once, as before; a check tick with open tunnels adds six `stat` calls and classifies only the hosts of tunnels whose files changed.
- The detection self-test covers a domain entry that lets neither a Feed hit below it nor anything while the Watchlist cannot be read through.

## Considered Options

- Keep storing a forced entry, and say it stays blocked while the Signal lasts: rejected. The Allowlist would hold entries that cannot work, and the Control Room would have to explain them.
- Keep the last good state of a broken Watchlist for Allowlist entries: rejected, for the reason [ADR 0009](0009-watchlist-and-legitimate-domains-live-on-the-host.md) rejects it for Open web: a proxy started later has none, and proxies would decide differently.
- Let running tunnels to a destination that gains a strong Signal run on, as for an expired entry: rejected by the owner. The VM is connected to a destination now known to be bad.
- An escape hatch for false positives below user-content hosts (an entry that lifts one Feed hit there): deferred, see above.
