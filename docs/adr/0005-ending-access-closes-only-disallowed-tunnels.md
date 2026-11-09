# Ending access closes only tunnels that are no longer allowed

The proxy checks a destination when a tunnel opens; until now tunnels then ran without any limit. Cutting every tunnel when access ends would break downloads and clones that are still running, but leaving everything open lets a tunnel opened during Open web serve as a channel out for days. We close tunnels only when access is deliberately withdrawn:

- **Open web closes:** tunnels to destinations that are not on the Allowlist are closed.
- **An Allowlist entry is removed** (`agentbox disallow`): tunnels to that domain are closed.
- **A Temporary allowlist entry expires:** running tunnels continue; new ones are refused.

Tunnels to allowed destinations are never closed by the proxy, and there are no idle or lifetime timeouts.
