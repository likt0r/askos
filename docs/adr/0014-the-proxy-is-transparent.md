# The proxy is transparent: names come from the TLS handshake, the resolver answers every name with the Host

Until 0.5.1 a VM reached the internet through an explicit HTTPS proxy: `CONNECT host:443` to `10.77.<n>.1:3128`, and no resolver at all. Every program in a VM had to be told about that proxy, through the proxy variables of its environment, and many were not: running containers got none by the owner's decision (compose services call each other over plain HTTP and the proxy answered those calls with `405`), Compose builds got none from Podman, Java needed its own properties, and a tool that ignored the variables got nowhere with no useful error. The owner decided on 2026-11-11 (#36) to make the proxy transparent, in one release, with no parallel operation.

## Decision

- **The resolver.** Each VM network's libvirt dnsmasq is the VM's resolver and answers every name with the Host's address on that network (the dnsmasq option `address=/#/10.77.<n>.1`, passed through libvirt's `xmlns:dnsmasq` options namespace). It forwards nothing: libvirt sets `no-resolv` for an isolated network and the definition says so again, there are no upstream servers, and `no-hosts` keeps the Host's hosts file out of it. Any other record type (AAAA, HTTPS, SVCB, TXT) gets an empty answer (`NOERROR` with no records, through `local=/#/`; without it dnsmasq answers `REFUSED`, which some resolvers take for a failing server), so a client takes the one A record and asks nothing else.
- **The proxy.** The same process, `agentbox-proxy <vm>`, socket-activated as before, listens on `10.77.<n>.1:443`. A connection starts with a TLS ClientHello; the proxy reads it (across records and TCP segments, in at most 16,389 bytes: one record of the longest length TLS allows, with its header), takes the server name from its SNI extension, and from there the chain is what it was: classify, decide (Allowlist, Regex entries, Open web, Signals), resolve the name itself, connect to port 443, decide again, register the tunnel, pipe, with the ClientHello passed on first. The client's destination is always the Host and is ignored; the port is always 443. The proxy speaks no TLS itself and sees nothing of the connection but the server name. A connection that is no TLS handshake, or names no server, is refused and logged with the client's address. A refused connection gets a fatal TLS alert (`access_denied`, `unrecognized_name`, `internal_error`) so the client reports why; bytes that are no TLS get nothing.
- **Ports.** From a VM to the Host: UDP 67 (DHCP), TCP 443, UDP and TCP 53, TCP 9100 (nftables and the firewalld zone `libvirt`). Nothing else.
- **The VM** has no proxy variables and no resolver of its own; its resolver is the Host's address on its network. Containers need no settings at all.
- **The log** keeps its lines (`ALLOWED`, `BLOCKED`, `ALERT`, `CLOSED`, `ERROR`), so Activity, the Control Room, `agentbox watch` and the detection self-test work as before; the self-test sends ClientHellos. The reasons `not a TLS handshake` and `no server name` have the codes `not-tls` and `no-server-name`.
- **Tunnels** are closed as [ADR 0005](0005-ending-access-closes-only-disallowed-tunnels.md) says; nothing changes there.

## Why not filter DNS alone

A resolver that answers allowed names truthfully and refuses the rest would need no proxy. It was rejected: allowed and refused names share addresses on every CDN (`registry.npmjs.org` and any other site on the same edge), so a VM that learned one allowed address could reach everything behind it, and nothing on the Host would see the name. With the proxy in the path, every connection names its destination, and the name is what the Allowlist, the Signals and the log work with. The resolver here is only the trick that brings every connection to the proxy.

## Why not an established SNI proxy

- **HAProxy** (`mode tcp`, `req.ssl_sni`): rejected. The policy (per-VM entries, Temporary entries, Open web, Regex entries, strong Signals checked per connection, closing withdrawn tunnels) would live in HAProxy's ACLs or a Lua or SPOE agent beside the Python that owns it, so the same decision would exist in two systems, and reloading rules without dropping connections is HAProxy's problem to solve on every change of the Allowlist.
- **Squid** (`ssl_bump peek`): rejected already in [ADR 0004](0004-own-security-services.md); it cannot close tunnels selectively and would hold the policy in an external ACL helper.
- **sniproxy**: rejected, not packaged in Fedora, and the policy would again sit beside the code that owns it.

The proxy stays dependency-free Python ([ADR 0006](0006-agentbox-is-a-dependency-free-python-package.md)); the ClientHello parser is a page of code against two RFCs (8446, 6066) and is tested against handshakes OpenSSL writes.

## Known limits

- **Domain fronting** is what it was: an allowed name on a shared CDN can carry traffic to other sites on that CDN; the proxy sees the server name and nothing inside the connection.
- **HTTP/2 connection coalescing** is the same class: every name resolves to the Host's one address, so a browser may send requests for another host its certificate covers over a connection already open to an allowed one; the proxy sees no new server name and logs nothing.
- **Encrypted Client Hello** is decided by its outer server name, as any ClientHello. Chromium and Firefox send the extension in every handshake as GREASE, with the real destination as the outer name, so refusing it would break every browser in a VM (Playwright, headless Chromium). Real ECH needs the provider's ECHConfig from an HTTPS record, which the resolver never answers; only a client with an ECHConfig built in could hide an inner name behind an allowed outer one served by the same provider, the same class of risk as domain fronting.
- **Only TLS with a server name, on port 443.** Plain HTTP, other ports and TLS without a server name get nowhere. An IP literal as the name (RFC 6066 forbids it, clients do not send it), in any form a resolver reads as an address (`16909060`, `0x01020304`, `1.2.3`, a numeric last label), reaches the classifier as a raw IP, a strong Signal, and stays blocked unless an entry allows exactly that address.
- **A VM's UDP reaches only DHCP and the resolver** on the Host's address on its network, so there is no HTTP/3 (QUIC); clients fall back to TCP, since the HTTPS record that would announce `h3` is never answered.
- **Other services of the Host on ports 53 and 443** that listen on all addresses keep the resolver or the proxy from binding the Host's address in a VM network (the kernel refuses the address while the wildcard socket holds the port). Agent Box refuses to set up the networks then: `regenerate`, `setup`, `create`, `delete` and a change of the Settings stop before anything changes and name the port. A service bound to other addresses is no obstacle, and nftables keeps the VMs from it.
- **The VM's address stands for the client** in the log of a refused connection; containers in the VM share it.
- A ClientHello in more than 16,389 bytes counts as no TLS handshake; real ones are a few hundred bytes, post-quantum key shares bring them to about 2 KiB.

## Considered Options

- Keep the explicit proxy and teach every tool about it: rejected, the list of tools never ends, and each one that does not know gets no useful error.
- Transparent NAT redirect (`nftables dnat` of port 443 to the proxy) with the real addresses from a forwarding resolver: rejected, the VM would resolve names through the Host, which gives it a channel out through DNS (ADR 0004), and the proxy would learn nothing it does not learn from the SNI.
- Terminate TLS in the proxy (a CA of the Host trusted in every VM) to see and filter URLs: rejected, the proxy would hold every secret of every connection, and every VM would trust a key on the Host that a compromised proxy could use against it.
