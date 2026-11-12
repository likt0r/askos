# Containers in a VM live on its Data disk

Rootless Podman kept its storage in the agent's home on the System disk, so every Reset threw away every Container image, container and volume, and the next `docker compose up` downloaded everything again. We moved the agent's Podman storage (`~/.local/share/containers`) onto the Data disk, like the build caches already there: Container images, containers and volumes now survive every Reset. A Reset still gives a clean system, but no longer clean containers.

So that the Data disk does not fill up with Container images nobody uses any more, each VM has a **Container image cleanup**, turned on or off, with its own number of days. Every hour the Host triggers it in each running VM that has it on, through the guest agent, as it reads the VM's journal, so turning it off or changing the days takes effect at the next run; it removes only Container images no container uses and that were not used for that long, never containers or volumes.

## Consequences

- A tampered Container image survives a Reset, as tampered repositories and build caches on the Data disk already do. A Reset is no remedy for a compromised dev stack; deleting the Data disk is.
- Stopped and running containers come back after a Reset as stopped containers; nothing starts them again on its own.
- The storage is read by whatever Podman version the next Build brings.

## Considered Options

- A pull-through cache on the Host for every VM (own or `docker-distribution`): rejected for now. It would help new VMs and save downloads shared between VMs, but needs a new port and service every VM talks to, an Allowlist check and logging per VM, and a size limit on the Host. With few VMs on one laptop, what costs time and bandwidth is the Reset, which the Data disk solves.
- Keeping only Container images, not containers and volumes: rejected, the owner wants a dev stack's data to survive a Reset as well.
- A cleanup timer inside the VM: rejected, it would learn its days only at boot (fw_cfg), so a change in the Control Room would wait for a Restart.
