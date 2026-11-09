# Images build on the Base image; the Host checks every Build's safeguards from outside

Images other than the Base image exist so a project can get its toolchain without changing every VM, but a convenience edit to one of them must never remove a safeguard. Each Image is a Containerfile in the repository: the Base image's (`vm/Containerfile`) sets up the agents and every safeguard, every other Image (`vm/images/<name>/Containerfile`) starts `FROM ${BASE}`, the Base image's current Build. After podman has built an Image and before it becomes a disk, `agentbox` mounts the built filesystem on the Host (`podman image mount`) and checks it with Python reading files, never running anything from the Image; a missing or weakened safeguard discards the Build and the previous one stays in use. The expected values (package minimum age from the Settings, `ignore-scripts`, the hooks path, the proxy, the guest-agent wrappers, no variables that switch a safeguard off) live in the Host's installed `agentbox` package, not in the repository being built. A derived Image is also compared with the Base Build it was built on: it must sit on that Build's layers, leave the safeguard files and the Git hooks and guest-agent wrappers identical, and may add files where environment or configuration is read only if they do not mention a safeguard.

_Superseded in this paragraph by [ADR 0010](0010-images-are-built-from-the-installed-release.md): Builds come from the Containerfiles and guest files of the installed Release; nothing is fetched, and the read-only token is gone. The checks above still hold._

Builds come only from `main` of this repository, fetched as GitHub's tarball of the commit with an installation token the Host mints for each Build: `contents: read` for this one repository, held in memory, passed to curl on stdin, never stored, logged or shown, and unrelated to the token service the VMs ask. A Build runs as `agentbox-build@<image>.service` ([ADR 0004](0004-own-security-services.md)); one at a time.

## Consequences

- The check knows the safeguards it was written for. A derived Image could still undermine one in a way the check does not look for (a wrapper for `npm` earlier on `PATH`, a service rewriting a file at boot); `main` is reviewed, and the check catches the convenient edits.
- Adding a safeguard to the Base image means adding its check in `agentbox/safeguards.py` and deploying the package (`vm/setup-vms.sh`) before the Build that relies on it.
- A derived Image can only be built on a Base Build made by this pipeline (the comparison needs its manifest and its container image); Builds made before are kept and used, but not built on.

_Amended by #85 (2026-11-08):_ `vm/setup-vms.sh` is gone; the package is deployed by installing the Release with the check, the RPM ([ADR 0009](0009-agent-box-ships-as-a-fedora-rpm.md)).

## Considered Options

- Running a check script inside the built Image: rejected, the Image decides what that script, its interpreter and its tools do.
- Checking the Containerfiles statically: rejected, a `RUN` line can do anything.
- Building from a branch other than `main`, or from the Host's checkout: rejected. Agents in VMs can push branches to this repository, and a Build runs as root on the Host; only reviewed code is built.
- A fine-grained personal access token or a deploy key stored on the Host: rejected, one more long-lived secret; the GitHub App already exists and mints an hour-long token limited to what a Build needs.
- `git clone` with the token: rejected, git wants the token in a URL, a config file or a helper's environment; the tarball needs only one request whose header reaches curl on stdin.
- Build progress as events on the Control Room's stream: deferred. The backend runs unprivileged and cannot read the build journal; the UI polls the Build's log with a cursor until the planned root helper (`agentbox watch`) feeds the stream.
