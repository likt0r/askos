# A watcher as root pauses a VM on an Alert with a strong Signal

Since [ADR 0021](0021-strong-signals-always-block.md) a destination with a strong Signal is always blocked, but the VM goes on working until someone opens the Control Room. An agent that just tried to reach a known exfiltration host is most likely following planted instructions, and what it tries next goes to the hosts it may reach (GitHub, the registries). The owner decided on 2026-11-10 and 2026-11-12 that such an Alert pauses the VM (#11), by default, through a watcher that reads the Activity namespace rather than a call from the proxy.

## Decision

- **A root service reads, the proxy never calls.** `agentbox alert-watch` runs as root in `agentbox-alert-watch.service` (shipped in the RPM, enabled and started by `agentbox setup`). It follows the proxies' `ALERT` lines in the journald namespace of Activity (`journalctl --follow`, only entries of the user `agentbox-proxy`, only `ALERT` lines). The proxy, which handles all of a VM's traffic, gets no sudo right, no socket and no other way to reach root.
- **Only the VM's own proxy can pause it.** An Alert counts only when journald's own fields say that `agentbox-proxy-<vm>.service` wrote it as `agentbox-proxy` under the identifier `agentbox-proxy-<vm>` (the check every reader of Activity makes, [ADR 0007](0007-activity-in-its-own-journald-namespace.md)) and the line names the same VM. So a compromised proxy can pause only its own VM, which it could stop anyway by refusing every connection.
- **Only strong Signals pause.** The trigger is an `ALERT` line with at least one strong Signal (`STRONG` in `agentbox/list_records.py`, which the classifier and the watcher share: `threat`, `exfil`, `c2`, `tunnel`, `paste`, `phishing`, `blockchain`, `lookalike`, `raw-ip`, `metadata`). Weak Signals (`young`, `payload`, random-looking labels, unusual TLDs, many labels) never pause. The `ALERT` line rather than `BLOCKED (suspicious: …)`: a Harmless mark silences the Alert of a false positive, and so its Pause too, while the destination stays blocked. A tunnel that a Feed update closes (`CLOSED`, after its Alert) pauses its VM as well.
- **The Setting `alert_action`** (`pause` by default, or `none`) is read at every such Alert. A Settings file agentbox refuses counts as `pause`, the stricter choice, and the watcher says so in its log.
- **An ordinary Pause, under the lifecycle lock, waited for.** The watcher takes the VM's lifecycle lock and waits for it (a Reset, Restart, start or delete that holds it finishes first), then pauses the VM with `managedsave` only if it runs. A VM that is off, paused already or no longer registered stays as it is, and nothing is recorded. Each VM waits in a thread of its own, so a held lock delays no other VM; further Alerts for a VM whose Pause is pending are left to it. `agentbox start`, a connection to its Orca or SSH port, a Restart or a Reset resume or end the Pause like any other.
- **Recorded and shown.** A Pause made is an Activity entry of kind `alert`, action `alert.paused`, actor `alert-watch`, with the host and the Signals; a Pause that failed is the same entry with outcome `failed`. Under the identifier `agentbox-activity` only root in `agentbox-alert-watch.service` may write kind `alert`, and only that action. The VM is marked with the Alert (`/var/lib/agentbox/alert-pause-<vm>.json`) until the Pause ends; `describe`, `watch` and the Control Room show it as paused by an Alert, and the Control Room's notification of the Alert is followed by one that says the VM was paused.
- **Nothing else.** Of a line the watcher takes the VM's name, vouched for by journald and required to be a registered VM, and the host and Signals, which go only into the entry and the mark as text. It runs no command it reads and changes no list.
- **Where it left off.** It keeps the time of the last Alert line it read (`/var/lib/agentbox/alert-watch.json`); started again (an update restarts it), it reads from there on. An Alert older than ten minutes pauses nothing, so a watcher that was stopped for a while does not pause VMs for what happened long before.

## Consequences

- Every attempt to reach a destination with a strong Signal is blocked and pauses the VM within seconds. Resuming it without removing the cause pauses it again at the next attempt.
- A false positive among strong Signals stops the VM's work until the owner marks it harmless (no Alert, no Pause, still blocked) or lifts the Signal.
- One more root service on the Host. It reads what the proxies write; journald's fields, not the lines' text, decide whose VM an Alert is.
- While the watcher does not run (stopped, failing, the Host booting), Alerts block but do not pause; once it runs again it catches up on those of the last ten minutes.
- `pair` without root starts a VM but may not remove the mark; the mark shows only while the VM is paused, so a later Pause by hand of such a VM would show the old Alert.

## Considered Options

- A callback in the proxy that runs `agentbox pause` through sudo: rejected by the owner (2026-11-10). The proxy handles all of a VM's traffic; it must not gain a way to root.
- Reacting in `agentbox watch`: rejected. It runs only for the Control Room and only while its backend runs; pausing belongs in a service of its own.
- Triggering on `BLOCKED (suspicious: …)`: rejected, a Harmless mark could then not stop the Pause of a false positive.
- Skipping a VM whose lock is held: rejected. What holds it (a start, a Reset) can leave the VM running, and the Alert would pause nothing.
- Default `none` (owner, 2026-11-10): superseded on 2026-11-12 by `pause`.
