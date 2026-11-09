# Contributing

Start with [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md) and
[GLOSSARY.md](GLOSSARY.md); decisions with lasting consequences are in [docs/adr/](docs/adr/).

## Tests

```
python3 -m unittest discover -s tests          # repo root; no root, libvirt or VM needed
cd web/ui && npm ci && npm run prepare:nuxt && npm test   # Control Room UI (VM or CI only)
```

A checkout's `bin/agentbox` runs only read-only commands unless `AGENTBOX_ROOT` points at a test
root; changes to a Host go through the installed CLI.

Both run on every pull request (`.github/workflows/ci.yml`), together with the title check
below (`.github/workflows/pr-title.yml`). CI also builds the Control Room UI (`npm run
generate`) and runs `tests/test_control_room_ui.py` against that build; the build is not
committed, so locally those tests are skipped until `npm run generate` has written
`web/ui/dist/` (see [web/ui/README.md](web/ui/README.md)).

CI also builds the RPM ([rpm/agentbox.spec](rpm/agentbox.spec)) with that UI for the current
and the previous Fedora release and installs it in a fresh Fedora container. `rpm/build.sh`
makes the source RPM from `HEAD` and builds the RPM from it as COPR does; `rpm/install-test.sh`
installs it and runs `tests/test_rpm.py` against the installation. `rpm/upgrade-test.sh`
builds an older Release from the same commit, gives it a Host in use, updates it to the RPM,
reinstalls and removes it, and runs `tests/test_rpm_upgrade.py` after each step. They run as
root in a throwaway container and refuse to run anywhere else; their headers show how to run
them with podman. Outside that container the two test modules only check the spec, the units
and the sudoers rule it installs, and its removal against `agentbox regenerate`. Nothing is
uploaded: an RPM exists only for a Release, whose workflow builds it the same way
([docs/releasing.md](docs/releasing.md)).

Only the owner changes files under `.github/workflows/`: the GitHub App never gets the
`workflows` permission, so a push from a VM that touches them is refused. Every action there is
pinned to a commit hash with its version as a comment; Dependabot (`.github/dependabot.yml`)
proposes updates weekly as `ci: bump …` pull requests. `tests/test_workflows.py` checks the pins,
the permissions and the Release workflow's scripts.

## Pull requests and Releases

Agent Box is delivered as **Releases** named by semantic versioning (see ADR 0009), started
with the Release button ([docs/releasing.md](docs/releasing.md)). release-it computes the next
version and the changelog from commit messages, so the commit message on `main` has to say what
changed.

- Merge pull requests with **squash-merge**. The commit message is the **pull request title**.
- The title uses [Conventional Commits](https://www.conventionalcommits.org/): `type: summary`
  or `type(scope): summary`. CI refuses other titles.
- Types: `feat` (new behaviour, next MINOR); `fix`, `perf` and `revert` (next PATCH); `docs`,
  `chore`, `refactor`, `test`, `ci`, `build` and `style` do not raise the version on their own,
  but a Release started with only such commits since the last one is a PATCH.
- A `!` after the type (`feat!: …`) means an update needs manual work from the owner (next
  MAJOR): an unmigrated format change in the VM registry or Host setup, a broken `/api/v1`, or a
  removed Safeguard.
