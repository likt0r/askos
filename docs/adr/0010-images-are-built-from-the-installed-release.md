# Images are built from the installed Release

Images used to be built from `main`, so pushing a commit could change what the next Build contained, independent of what was installed on the Host. The Containerfile and the guest files now ship in the Release under `/usr/share/agentbox/vm`, and the Host builds Images from there with podman. A Build changes only when the Host updates to a new Release (or the owner rebuilds), and it takes effect at the next Reset as before.

This keeps the Safeguards, the Containerfile and the Host-side checks (ADR 0008) of one Release in step: the checks that discard a Build are the ones that shipped with the Containerfile they check.

_Amended by #113 (2026-11-08):_ Orca no longer comes from the Host. The Build copied the Host's `/opt/Orca` and Orca's server runtime from the owner's home (`ORCA_RUNTIMES` in `/etc/agentbox/build.conf`), so a Build depended on what happened to be installed there. Now the Build container downloads Orca like the npm agents come from their registry: `vm/build/orca-release`, shipped with the Containerfile, takes the newest GitHub release of Orca at least `package_min_age_days` old, or the one the setting `orca_version` pins, downloads its x86_64 RPM over HTTPS only and checks it against the SHA-256 GitHub lists for it, and downloads the Node runtime of Orca's server from nodejs.org, checked against the checksums that Orca release pins. What comes from the internet is parsed only inside the Build container, never by root on the Host; the Host's root reads only the built Image, as before. A Build now changes when Orca publishes a release old enough, not only with a new Release of Agent Box or the packages; the Build records the Orca it installed, and the version check shows the one the next Build would take. The Safeguard checks of ADR 0008 are unchanged and still pass: Orca brings no file where they look.

## Considered Options

- Keep building from `main`: rejected, the Host would run safeguard checks and a Containerfile that were never released together.
- Ship prebuilt Images in the Release: rejected, they are large and the Host should build and check its own.
