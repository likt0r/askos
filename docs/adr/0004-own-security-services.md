# Services the VMs or the UI can reach stay our own; established software where it adds no attack surface

We prefer established, packaged software over our own code, and checked every planned component against it. For the services an untrusted VM talks to, or that the Control Room UI can drive, the established options add attack surface or move our logic into a plugin without saving much, so these stay our own: the Control Room backend, the allowlist proxy and the detection in `agentbox-suspicious`. Elsewhere we adopt established software:

- **journald** for all logs, one identifier per VM, instead of an own activity-log format.
- **systemd units** for Image builds; a build's journal is its progress view.
- **HaGeZi lists** (threat intelligence, newly registered domains), fetched by a timer and looked up locally by `agentbox-suspicious`, instead of own RDAP lookups and feed polling.
- **The VM's guest agent** (qemu-guest-agent, already in every VM) to read a VM's journal: `journalctl` runs inside the VM and only its output comes back. This replaces mounting the System disk with `qemu-nbd` in the Host's kernel, which would parse filesystem metadata a VM controls. Reading the disk through libguestfs instead was tried and dropped: a running VM keeps writing, so a read-only view of its disk is inconsistent and reads fail.
- **virt-install** to define VMs instead of hand-written libvirt XML.
- **libvirt-guests** to shut the VMs down when the Host shuts down, instead of an own unit.
- **nginx** in front of Ollama to limit the API paths VMs may use.

The Control Room UI stays our own (Nuxt UI, built inside a VM, [ADR 0003](0003-no-node-on-the-host.md)).

_Superseded in part by [ADR 0014](0014-the-proxy-is-transparent.md) (2026-11-11):_ the proxy takes its destinations from the TLS handshake instead of `CONNECT` requests, and the VM's libvirt dnsmasq is its resolver, answering every name with the Host. The reasons the proxy stays our own hold; 0014 records the SNI proxies (HAProxy, sniproxy) that were not taken either.

## Considered Options

- Cockpit as the Control Room backend: rejected. Any script in a Cockpit package can run arbitrary commands as the logged-in user, and Cockpit ships a terminal, so a compromised UI build would get a shell on the Host. Our backend offers a fixed set of actions.
- Squid instead of our proxy: rejected. The external ACL helper would hold nearly all of our decision logic (per-VM rules, temporary entries, Open web, Alerts), Squid caches helper answers for an hour by default, cannot close tunnels selectively, and puts a large C++ parser in front of the VMs.
- blocky as the proxy's resolver: rejected. It is not packaged in Fedora, and classifying refused destinations through DNS means resolving names that are not on the Allowlist, which gives a VM a channel out through DNS. Our proxy resolves only after the Allowlist check.
- Incus instead of libvirt: rejected for now. Fedora 44 ships Incus 6.23, without the per-instance SELinux confinement added in 7.2 that libvirt (sVirt) already provides. Revisit once Fedora ships 7.2 or later.
- Docker Sandboxes or Coder as the whole platform: rejected. Neither fits Orca with a persistent Data disk, GPU passthrough or nested KVM; `sbx` is proprietary and Coder's agent firewall integration is a paid feature.
- Smokescreen as proxy: rejected, it identifies clients by TLS certificate and reloading rules without a restart is unclear.
- octo-sts for GitHub tokens: rejected, it needs an OIDC issuer the VMs do not have.
