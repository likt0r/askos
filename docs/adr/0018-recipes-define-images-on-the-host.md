# Recipes define Images on the Host

Every Image was defined in the installed Release ([ADR 0010](0010-images-are-built-from-the-installed-release.md)): a new Image, such as one for developing DMS plugins (#118), needed a Release of Agent Box. The owner decided (#117) that they can define Images on their own Host as **Recipes**, built and checked like any derived Image.

## Decision

- **A Recipe is one tar file.** At its top lies the entry file `Containerfile`, a plain Containerfile whose first instruction is `FROM ${BASE}`, as a derived Image of the Release starts; every other file in the tar (scripts, units, configuration, binaries) is its build context. Agent Box generates no Containerfile and knows no other recipe format.
- **Where it lives.** The current version is `/etc/agentbox/recipes/<name>/recipe.tar`, root-owned, `0600`; every save moves the version before it to `/etc/agentbox/recipes/<name>/versions/<time>.tar` and keeps the last 10. `agentbox` lists, shows and restores them; a restore is a save of the old version.
- **Only the owner writes Recipes**: as root through new declared `agentbox` commands, or signed in to the Control Room, whose backend calls those commands through its sudoers rule like every other Control Room action (`sudo -n agentbox <command>`). The tar goes to `agentbox` on stdin, never as an argument or a path. Nothing inside a VM can create, change or read a Recipe.
- **What `agentbox` checks on every save and again before every Build**, reading the tar's headers with Python's `tarfile` and nothing but the `Containerfile` member's content: the name follows the Image name pattern and is not the name of an Image the Release ships (those stay as they are; a Recipe never replaces one); members are regular files and directories only, with relative names, no `..`, no links, devices or setuid bits; at most 512 members, 64 MB in all, the `Containerfile` at most 256 KB; and its first instruction is `FROM ${BASE}`. A refused tar changes nothing. Every save, restore and removal is an Activity entry with the actor and the tar's SHA-256.
- **The Host does not unpack a Recipe.** A Recipe Build hands the tar to podman as its build context on stdin (`podman build --build-arg BASE=<Base Build> - < recipe.tar`), so `agentbox` never writes the owner's files onto the Host's filesystem, and what the Build downloads from the internet is parsed only inside the build container, as ADR 0010's amendment requires.
- **The Safeguard check is unchanged.** A Recipe Build sits on the Base Build's layers and passes the same Host-side check as every derived Image ([ADR 0008](0008-safeguards-checked-from-the-host-after-every-build.md)): the Safeguard files, the Git hooks and the guest-agent wrappers stay identical, or the Build is discarded and the previous one stays in use. Each Build records the SHA-256 of the Recipe it came from.
- **A Recipe Image is an Image like any other**: `agentbox images`, `build`, `set-image`, Update, the Maintenance window's weekly Build and the Reset onto a new Build treat it as they treat an Image of the Release. Removing a Recipe whose Image a VM uses is refused; its Builds go as the Builds of other Images go.
- **Egress groups** (#9) are not part of a Recipe yet; a Recipe adds no Allowlist entry.

## Consequences

- ADR 0008 built only reviewed code: `main`, later the installed Release. A Recipe is reviewed by the owner who writes it, nobody else. The Safeguard check still applies to every Recipe Build and still only catches what it was written for (ADR 0008's consequences): a Recipe can install software that undermines a Safeguard in a way the check does not look for, as a careless derived Image in the Release could.
- A stolen Control Room session can now change what a Build installs, not only start a Build or a Reset: it can write a Recipe, build it and reset a VM onto it. The Safeguard check, the Activity entry with the actor and the kept versions are what stand against it; the VM still sits behind its Allowlist.
- A Recipe can carry binaries no one can read in a diff; the versions show which files changed and their SHA-256, not what a binary does.
- Recipes are not part of a Release and are not backed up by Agent Box; they move to a new Host as files under `/etc/agentbox/recipes`.
- The Control Room backend reads a request body of at most 8 KB today; uploading a Recipe needs a larger limit for that one route (#119).

## Considered Options

- One Containerfile only, files embedded as heredocs (`COPY <<EOF`): rejected, every script and configuration file would sit in one long file, binaries only as base64 or downloads.
- A directory of separate files: rejected, the CLI and the Control Room would need one call per file and a version would be a set of files; a tar is one unit to upload, check, keep and restore.
- A declarative format (packages, files, a setup script) from which Agent Box writes the Containerfile: rejected, a second format to maintain and less than a Containerfile can do.
- A main shell script as the entry, with a Containerfile Agent Box writes around it: rejected for the same reason; a Recipe that needs only a script runs it from its Containerfile.
- Unpacking the tar on the Host into a build context directory: rejected, root on the Host would write files the owner (or a stolen session) chose; podman reads the tar as its context itself.
- Recipes from the owner's Git repository at a pinned commit: not now; the Host would fetch from the internet as root.
- Release Images as built-in Recipes the owner can override: rejected, an override would let a Recipe replace the Base image or an Image the Release vouches for.
