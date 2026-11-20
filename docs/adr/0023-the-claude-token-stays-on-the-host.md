# The Claude token stays on the Host, behind an Anthropic route of the token service

Status: Accepted (#12), by the owner on 2026-11-20 with the decisions below. ARCHITECTURE.md's Credentials sections, GLOSSARY.md ("Claude token") and docs/claims.md change with the code.

The Claude token from `claude setup-token` is the one long-lived secret inside a VM. It is valid for a year and reaches the VM at boot (fw_cfg). Every process there can read it as `CLAUDE_CODE_OAUTH_TOKEN` and send it out through any allowed service, and it then works from anywhere until it is revoked with Anthropic ([Credentials in a VM](../../ARCHITECTURE.md#credentials-in-a-vm)). GitHub tokens already stay on the Host and are issued per request for one hour; the Claude token should get the same treatment. [brig's credential broker](https://github.com/brig-sh/brig/blob/main/docs/broker.md) (Apache-2.0) is the template: the guest gets a base URL and a token that only the broker accepts, and the broker adds the real credential on a verified TLS connection of its own to `api.anthropic.com`.

## What Claude Code does with a base URL and an OAuth token

Tested on 2026-11-20 with Claude Code 2.1.328. Each run had a fresh `HOME`, an empty environment except for the variables named, and a network namespace with only loopback (`unshare -rn`), so nothing could leave the machine. `ANTHROPIC_BASE_URL` pointed at a local Python server that logged every request and answered `/v1/messages` with a minimal SSE stream. A logging `HTTPS_PROXY` showed requests that bypass the base URL, and terminated their TLS with a throwaway CA (`NODE_EXTRA_CA_CERTS`) so their paths were visible. The token was a fake. Runs covered `claude -p`, an interactive session through a pty (a prompt, `/status`, `/usage`), a forced WebFetch tool call, and a 401 from the route. The Gateway documentation ([LLM gateway](https://code.claude.com/docs/en/llm-gateway), [compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol), [connect](https://code.claude.com/docs/en/llm-gateway-connect)) agrees with what was seen.

**Plain HTTP and OAuth work.** With `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`, Claude Code sends its inference requests to the base URL and shows the streamed answer. `/status` reports "Auth token: CLAUDE_CODE_OAUTH_TOKEN" and the base URL. Claude Code accepts a placeholder in any form, `sk-ant-oat01-…` or not. It never wrote the token to its home directory.

**What goes to the base URL:**

- `POST /v1/messages?beta=true`, streamed (`"stream": true`, answered with `text/event-stream`). This is the only path the route has to forward.
- `POST /v1/messages?beta=true` with `max_tokens: 1` and no stream, once at the start of an interactive session.
- `POST /v1/messages/count_tokens`, which the documentation names as optional. It was not seen in these runs; without it, Claude Code estimates the context size from characters.
- `HEAD /api/hello`, which warms the connection. The documentation says it is skipped when a proxy is configured, and it may be rejected.
- `GET /v1/models?limit=1000`, only with `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`, which the Image does not set.

**Headers:**

- The credential is in `Authorization: Bearer <token>`. No `x-api-key` header is sent.
- `anthropic-beta` carries `oauth-2025-04-20` among about ten other values. The documentation says Anthropic answers `401` without the OAuth value, and that the header must be forwarded unchanged as an open list.
- Also sent: `anthropic-version: 2023-06-01`, `x-claude-code-session-id`, `User-Agent: claude-cli/<version>`, `x-app`, `X-Stainless-*`, and `Accept-Encoding: gzip, deflate, br, zstd`.
- The documentation adds that `x-claude-code-agent-id` is sent for subagents.

**What bypasses the base URL to fixed hosts**, through the Allowlist proxy as today:

| Request | Credential | With the Image's environment (`DO_NOT_TRACK=1`, `DISABLE_AUTOUPDATER=1`) | With the route |
| :- | :- | :- | :- |
| `GET https://api.anthropic.com/api/claude_cli/bootstrap` | Bearer token | sent; a 401 is retried once and ignored | carries the placeholder, so Anthropic answers 401. No visible effect in either mode |
| `GET https://api.anthropic.com/api/directory/server-urls` | none | sent | unchanged |
| `GET https://downloads.claude.ai/claude-code-releases/plugins/claude-plugins-official/latest` | none | sent (interactive) | unchanged |
| `https://raw.githubusercontent.com/…` | none | sent (interactive) | unchanged (the Allowlist decides) |
| `GET https://api.anthropic.com/api/web/domain_info?domain=…` (WebFetch domain check) | none | sent per WebFetch | unchanged |
| `POST https://api.anthropic.com/api/eval/sdk-…` (feature flags), `POST /api/event_logging/v2/batch` (telemetry) | Bearer token | not sent | not sent |
| fast mode availability check (documentation only, not seen) | the login | — | fails with the placeholder; `/fast` may report fast mode unavailable |

With `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, a `-p` run sent only `/v1/messages`: the bootstrap and the MCP directory requests disappeared. The WebFetch domain check stayed; the documentation says that variable does not cover it. The interactive session was not repeated with the variable. The documentation says the official plugin marketplace fetch is not covered either and has a variable of its own (`CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL`). The variable also turns off feature-flag fetching and the features that depend on it.

**Behaviour that changes with a base URL that is not `api.anthropic.com`:**

- `/status` shows remote managed settings and organisation policy as "not fetched with a custom ANTHROPIC_BASE_URL". Setup tokens cannot load those today either.
- Remote Control is disabled and gateway hint headers are off (documentation).
- `/usage` shows "No usage limits to show for this sign-in" and makes no request. The compatibility guide says Claude Code reads `anthropic-ratelimit-unified-*` response headers to show plan usage, so the route forwards them.

**A 401 from the route:** Claude Code retries once, then exits with `Failed to authenticate. API Error: 401 <the route's message>`. It does not try to refresh the token anywhere. The route's English text therefore reaches the user as it is written.

**`ANTHROPIC_AUTH_TOKEN` instead of `CLAUDE_CODE_OAUTH_TOKEN`:** Claude Code treats the placeholder as a gateway credential. It drops `oauth-2025-04-20` and `extended-cache-ttl-2025-04-11` from `anthropic-beta`, and skips the bootstrap request (both seen). The documentation says such a credential replaces the subscription login, so Claude Code then acts as a gateway client billed per token. The route would then have to add the OAuth value itself and would get a client that believes it is not on a subscription.

**Orca**, read in its source, read-only:

- Orca never reads `CLAUDE_CODE_OAUTH_TOKEN` for requests of its own.
- Its usage meter reads `~/.claude/.credentials.json`, which a VM does not have, and calls the fixed `https://api.anthropic.com/api/oauth/usage`. It fails in a VM today and keeps failing.
- It passes `ANTHROPIC_BASE_URL` on to every `claude` it starts and never checks or changes it. Its structured and chat launches pass the token variable on as well. That last point is read from the code and was not run.
- With a custom base URL its model catalogue is never marked "not signed in".

## Decision

- **A second listener of the token service.** The token service per VM gets a second socket unit, `<Host address on the VM network>:9101`, `Accept=yes`, beside `:9100`. Its process speaks HTTP/1.1 and serves only the Anthropic route.
  - Like the GitHub part, it runs once per connection as a system user. It knows the VM by the address it listens on: nftables lets only the VM's own network reach that address.
  - The nftables rules and the Host's VM ports gain port 9101.
  - The implementation is stdlib Python, as [ADR 0004](0004-own-security-services.md) (services a VM talks to stay our own) and [ADR 0006](0006-agentbox-is-a-dependency-free-python-package.md) want. Python's `-I` flag applies, and no proxy is taken from the environment.
- **The Claude token never enters the VM.**
  - `set-claude-token` keeps checking a token with Anthropic before it stores it. The file `claude-<vm>` becomes readable only by the route's service user and stops being an fw_cfg file.
  - Instead the VM gets a **placeholder** through fw_cfg. It is random per VM, made by `regenerate` when the VM has none, and kept until the VM is deleted.
  - `guest-setup` writes the placeholder as `CLAUDE_CODE_OAUTH_TOKEN`, and `ANTHROPIC_BASE_URL=http://host.agentbox:9101`, into `/run/agentbox/env` and `/run/agentbox/agent-env`. The values of Agent Box come last, so they win. Both names stay reserved for VM variables and VM secrets.
  - Every VM gets both values whether a token is stored or not. That is what lets a token set later work without a Restart.
- **What the route accepts.** It accepts only a request whose `Authorization` is `Bearer <this VM's placeholder>`, with no `x-api-key`. Any other credential is refused, so the route never forwards a login or key brought into the VM with or without the owner's token. Then:
  - `POST /v1/messages` and `POST /v1/messages/count_tokens` (with their query string) are forwarded.
  - `HEAD /api/hello` is answered locally with `204`.
  - Every other path or method gets `404`.
  - `CONNECT`, an `Upgrade` header, `Expect: 100-continue`, a body over 32 MB (Anthropic's limit for the Messages API), and an ambiguous request get `400`. An ambiguous request is one with both `Content-Length` and `Transfer-Encoding`, a repeated `Host` or `Authorization`, or a path that is not canonical.
  - The route follows no redirect.
- **How a request goes upstream.**
  - The route reads the token file at each request, not once per connection or per process. A changed token is used from the next request on. A file without a token (`none`) refuses with `401` and a message that names `agentbox set-claude-token`.
  - The route opens its own TLS connection to `api.anthropic.com:443` with `ssl.create_default_context()`. The certificate and host name are verified, and a TLS failure is a `502` with no retry.
  - `Authorization` is replaced with the real token. Hop-by-hop headers, `Proxy-*`, `Forwarded` and `X-Forwarded-*`, `Cookie` and `Accept-Encoding` are dropped, the last so the answer comes back uncompressed.
  - `anthropic-*`, `x-claude-code-*`, `content-type`, `accept`, `user-agent` and `x-stainless-*` are forwarded unchanged, as the compatibility guide asks.
- **How the answer comes back.**
  - The route forwards the status, `content-type`, `request-id`, `retry-after`, `x-should-retry` and `anthropic-ratelimit-unified-*`, and drops `Set-Cookie` and every other header.
  - An event stream reaches the VM event by event: the route writes and flushes each chunk as it arrives. It buffers nothing, so the `ping` events keep Claude Code's idle watchdog quiet.
  - The exact bytes of the real token are removed from every header and from the body, also across chunk boundaries, as brig does. A token that the answer re-encodes is not found; Anthropic is not known to echo it.
  - Error bodies from Anthropic otherwise pass through unchanged, because Claude Code reads their wording to recover from rejected capabilities.
- **One Activity line per request**, from the route's own process. It goes into the Activity namespace under its own identifier (`agentbox-claude-<vm>`) and never carries the token, the placeholder, a header value or a body:
  - `[<vm>] claude POST /v1/messages 200 model=<model> <seconds>s <bytes>B request-id=<id>`, with the model taken from the body;
  - `[<vm>] refused: <reason>: <English text>`, where `<reason>` is a stable code (`no-token`, `wrong-placeholder`, `path-not-allowed`, `upgrade`, `too-large`, `invalid-request`, `upstream-tls`, `upstream-error`).
  - A successful `HEAD /api/hello` is not logged; its refusals are.
- **`set-claude-token` takes effect at once**, from the VM's next request, without a Restart. Replacing the token never interrupts a stream already running; that stream finishes with the token it started with. Removing the token works from the next request on.

## Plain HTTP on the VM network

Confirmed: the VM talks to the route over plain HTTP.

- The VM's network is an isolated libvirt bridge with exactly one VM and the Host. nftables drops everything from it that is not addressed to the Host's address on that network, and no third party is on that path.
- What crosses it is the placeholder, which is worth nothing from anywhere else, and the request and answer bodies. Code in the VM can read those anyway.
- The real token travels only between the Host and `api.anthropic.com`, over verified TLS.
- TLS on the VM's side would need a CA in the Image and a key on the Host, rotated and checked by the Safeguards, and would protect nothing the bridge does not.
- Claude Code accepts an `http://` base URL (tested above).
- brig uses TLS for its own name because its guests can share a network. Its AWS route uses plain HTTP on a link-local address, as this route does on the bridge.

## What the route does not protect

- **The VM can still use the owner's subscription** for as long as the route exists. Any process in it can send prompts through the route, use up the plan's limits, and send whatever it reads to Anthropic, which every Allowlist already allows. The route stops only the token leaving the VM. Afterwards a leak is the placeholder, which works nowhere but on that VM's network.
- **Processes on the Host** can connect to the route's address. Without the placeholder they get nothing, and the placeholder is readable only by root, QEMU and the route's service user. The GitHub token service has no such check today.
- **The route's service user holds every VM's Claude token.** A flaw in the route's process exposes them, as one in the GitHub part exposes the App keys. The route has a user of its own, `agentbox-claude`, so a flaw in one does not reach the other's secrets.
- **Requests that bypass the base URL** carry the placeholder to `api.anthropic.com` (the bootstrap request) and fail there. That is harmless, but it is traffic with a token-shaped value outside the route.
- **`/login` in the VM** with someone's claude.ai account still works through the Allowlist, as today. The route refuses that login's token, so Claude Code then needs the base URL removed, which code in the VM can do. The connector Safeguard (`ENABLE_CLAUDEAI_MCP_SERVERS=false`) stays as it is.
- **Other credentials** (VM secrets, Codex or OpenCode logins) are untouched.

## Consequences

- No real Claude token exists in a VM: not in fw_cfg, the environment, the System disk or the Data disk. A test reads the generated `agent-env` and the fw_cfg arguments.
- `set-claude-token` no longer waits for the next start. README and ARCHITECTURE.md drop "at the VM's next start", and "passed in at boot" becomes "never enters the VM".
- Every Claude Code request of every VM passes through one more Host process. It is per connection and stdlib, and a stream holds its process for as long as the stream runs, so `MaxConnections` must cover parallel subagents.
- Remote Control, remote managed settings and gateway hint headers are off in VMs. Fast mode may report itself unavailable. Feature flags and telemetry were already not fetched with the Image's environment.
- The Allowlist proxy no longer sees Claude Code's inference as connections to `api.anthropic.com`; Activity shows it as route lines instead.
- Images built before this lose Claude until they are rebuilt. Their `guest-setup` sets no base URL, so whatever placeholder they read goes straight to Anthropic and is refused. There is no compatibility shim: this is alpha, and breaking it is accepted.

## Considered Options

- **Header injection in the transparent Allowlist proxy** (Docker Sandboxes, spacy-cloud/agent-box): rejected. The proxy passes TLS through by SNI ([ADR 0014](0014-the-proxy-is-transparent.md)). Injecting a header would mean intercepting TLS to `api.anthropic.com`, with a CA in every Image and the proxy holding the token.
- **The placeholder in `ANTHROPIC_AUTH_TOKEN`:** rejected. Claude Code then drops the OAuth beta value and acts as an API-billed client; the route would have to add the value back and fight a client in the wrong mode.
- **TLS between the VM and the route:** rejected, see Plain HTTP above.
- **No placeholder check, the network alone identifies the VM:** rejected. The route would forward any credential a VM sends (a `/login` token, an API key) with the owner's token or its own, and every process on the Host could use the subscription.
- **A long-running daemon instead of a process per connection:** not taken. The per-connection model of the GitHub part reads the files at each request, which gives "effective at once" for free. The cost, one Python start per connection, is small against a model call.

## Decided by the owner (2026-11-20)

1. **The route's service user.**
   - (a) `agentbox-gh`, which already runs the token service.
   - (b) A user of its own, `agentbox-claude`, with the token file `agentbox-claude:agentbox-claude 0400` (group `agentbox-claude` 0440 if the Control Room's backend must read it), and the placeholder readable by QEMU and that user.

   *Decided: (b).* A flaw in one part then reaches neither the App keys from the route nor the Claude tokens from the GitHub part. The cost is one more sysusers entry.
2. **Port or path.**
   - (a) A second socket on `:9101`.
   - (b) The same `:9100`, with the GitHub line protocol and HTTP told apart by the first line.

   *Decided: (a).* It keeps two protocols and two parsers apart. The cost is one more port in nftables and the Host's VM ports.
3. **The placeholder's form and life.**
   - Form: (a) `sk-ant-oat01-agentbox-<random>`, which `sanitize` already redacts everywhere and which secret scanners (GitHub push protection) flag if an agent commits it; (b) a form of its own, `agentbox-claude-<random>`, which nobody mistakes for a real token. Claude Code accepts both (tested).
   - Life: made once per VM and kept until the VM is deleted, or made anew at every start.

   *Decided: (b), made once per VM.* A placeholder that looks like a real token would make every report of it look like a leak. A new one at every start adds little, because the network already binds it to the VM. `guest-setup`, `set-claude-token` and `claude_token_since` all check for `sk-ant-` today and change with it.
4. **Where the base URL comes from.**
   - (a) Fixed in `guest-setup` (`http://host.agentbox:9101`), written only when the placeholder entry exists.
   - (b) A second fw_cfg value written by the Host.
   - (c) The Image's `DefaultEnvironment`, beside `AGENTBOX_GH_ADDR`.

   *Decided: (a).* The address is the same for every VM, as with the GitHub service, and tying it to the placeholder entry keeps Claude Code from pointing at a route the Host does not serve.
5. **`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` in the Image.** It removes the bootstrap request (which carries the placeholder to Anthropic), the MCP directory and plugin marketplace fetches, release notes and update checks. It also turns off feature flags and the features that depend on them, and the fast mode check.
   - (a) Set it, as a Safeguard.
   - (b) Set it as a plain default.
   - (c) Leave it unset.

   *Decided: (b).* All token-bearing traffic then goes through the route. The bootstrap request did nothing visible with the placeholder, so (c) is acceptable too. It is not a Safeguard, because changing it leaks nothing.
6. **`anthropic.com` and `claude.ai` on the Allowlist a new Host starts with.** With the route, Claude Code no longer needs `api.anthropic.com` for inference. It still uses it for the WebFetch domain check and the bootstrap. Taking those entries out would stop a `/login` in the VM and Anthropic's hosts as a channel, but would break the WebFetch check (`skipWebFetchPreflight` turns it off) and claude.ai artifacts.

   *Decided: keep the entries for now and decide in an issue of its own.* That change is outside #12.
7. **A command to remove a Claude token.** Today only deleting a VM removes it. The route makes "removing cuts off at once" possible.

   *Decided: add `agentbox remove-claude-token <vm>`* (and the Control Room's button), which writes `none`. Running streams finish.
8. **What a running stream does when the token is replaced or removed.**
   - (a) It finishes.
   - (b) The route checks the file between chunks and ends the stream.

   *Decided: (a).* The request was allowed when it was sent, and (b) would cut a long answer in the middle with no gain against a VM that sends the next request a moment later.
9. **`MaxConnections` of the route's socket.** It is 16 for the GitHub part. Each running stream and each parallel subagent holds one connection, plus keep-alive connections.

   *Decided: 64,* and a refusal at the limit that Activity shows.
10. **What the Activity line names.**
    - (a) The model, the duration, the size and Anthropic's `request-id`.
    - (b) (a) plus the token counts from the final `message_delta`.

    *Decided: (a) first.* It is enough to see which VM uses the subscription and how much. Token counts would need the route to parse the stream.
