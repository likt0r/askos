# The Control Room's backend is dependency-free; the Nuxt UI is built inside a VM

The Control Room backend can reset VMs, open the web, set credentials and reveal pairing codes, so it is the most sensitive code on the Host. We keep it as a small Python standard-library service and do not run Node (e.g. Nuxt/Nitro) on the Host. The UI uses Nuxt UI but is built as a static site inside a VM under the same package safeguards as any project, and only the generated files are deployed to the Host.

## Considered Options

- Nuxt/Nitro as backend: rejected, it would put a large node_modules tree with sudo-reachable endpoints outside any VM.
