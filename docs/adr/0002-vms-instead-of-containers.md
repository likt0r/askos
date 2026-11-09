# Agents run in KVM VMs, not containers

Projects need Docker for their dev stacks and tests. Running Docker inside a container requires a privileged (if rootless) container, which leaves the host kernel as the only boundary against a supply-chain exploit. We chose one KVM VM per unit of separation instead: its own kernel, native Docker, at the cost of roughly 1 GB RAM overhead per VM and a slower image build.
