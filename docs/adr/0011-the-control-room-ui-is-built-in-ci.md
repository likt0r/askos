# The Control Room UI is built in CI, not committed

ADR 0003 keeps Node off the Host, and the UI was built in a VM and committed as `web/ui/dist`. With Releases, GitHub Actions builds the UI at each tag with `npm ci` against the lockfile, puts it into the RPM, and the build attestation records where it came from. `web/ui/dist` leaves git, which removes generated-file diffs and merge conflicts.

The Host still has no Node. What changes is where the UI dependencies run: no longer in a VM behind the Allowlist, but on GitHub's runners, with pinned versions and an attestation instead. This amends ADR 0003; it does not replace it.

## Considered Options

- Keep committing `dist`: rejected, it duplicates every UI change in the diff and could drift from the source without anyone noticing.
- Build the UI in a VM during the Release: rejected, it would make the Release depend on one person's machine.
