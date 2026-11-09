#!/usr/bin/env bash
# Builds the source RPM of the checked-out commit and from it the RPM, the way COPR builds
# it: as a normal user, from nothing but the source RPM (ADR 0009). Runs as root in a
# throwaway Fedora container, since it installs the build tools; CI runs it on every pull
# request (.github/workflows/ci.yml).
#
#   rpm/build.sh <out-dir> [<ui-build>]
#
# <ui-build> is the Control Room UI as `npm run generate` writes it (default web/ui/dist);
# CI builds it from the lockfile (ADR 0011), the Host never does (ADR 0003). The sources
# are those of HEAD (git archive), the version is the file VERSION. <out-dir> receives
# agentbox-<version>-1.<dist>.src.rpm and agentbox-<version>-1.<dist>.noarch.rpm.
#
# Locally, with podman instead of docker:
#   podman run --rm --security-opt label=disable -v "$PWD:/src:ro" -v "$OUT:/out" \
#     quay.io/fedora/fedora:45 /src/rpm/build.sh /out [/src/web/ui/dist]
set -euo pipefail

[ -e /run/.containerenv ] || [ -e /.dockerenv ] || {
  echo "rpm/build.sh installs packages as root: only in a throwaway container." >&2
  exit 1
}
[ $# -ge 1 ] || { echo "Usage: rpm/build.sh <out-dir> [<ui-build>]" >&2; exit 2; }

src=$(cd "$(dirname "$0")/.." && pwd)
out=$1
ui=${2:-$src/web/ui/dist}
[ -f "$ui/index.html" ] || {
  echo "No Control Room UI in $ui: build it first (cd web/ui && npm ci && npm run generate)." >&2
  exit 1
}

release=$(tr -d '[:space:]' < "$src/VERSION")
# RPM versions cannot hold "-": a pre-release 1.2.0-rc.1 becomes 1.2.0~rc.1, which sorts
# before 1.2.0 as it should
version=${release//-/\~}

dnf -y -q install --setopt=install_weak_deps=False git-core rpm-build >/dev/null

top=$(mktemp -d)
mkdir -p "$top"/{SOURCES,SPECS,SRPMS}
git() { command git -c safe.directory='*' -C "$src" "$@"; }
# dates in the tarballs from the commit, so the same commit gives the same sources
epoch=$(git log -1 --format=%ct HEAD)
git -c tar.umask=022 archive --format=tar.gz --prefix="agentbox-$version/" \
  -o "$top/SOURCES/agentbox-$version.tar.gz" HEAD
tar -C "$ui" --sort=name --mtime="@$epoch" --owner=0 --group=0 --numeric-owner \
  -czf "$top/SOURCES/agentbox-ui-$version.tar.gz" .
# the spec as committed, with the version of this commit and a changelog entry of its date,
# from which rpm takes the dates of the build (what changed is in CHANGELOG.md)
git show HEAD:rpm/agentbox.spec | sed "s/@VERSION@/$version/" > "$top/SPECS/agentbox.spec"
printf '\n%%changelog\n* %s Agent Box - %s-1\n- Release %s, see CHANGELOG.md\n' \
  "$(LC_ALL=C date -u -d "@$epoch" '+%a %b %d %Y')" "$version" "$release" >> "$top/SPECS/agentbox.spec"
rpmbuild -bs --define "_topdir $top" "$top/SPECS/agentbox.spec" >/dev/null
srpm=$(echo "$top"/SRPMS/agentbox-*.src.rpm)

# BuildRequires from the source RPM, then the build as a normal user, as in COPR's mock
mapfile -t needs < <(rpm -qp --requires "$srpm" | grep -v '^rpmlib(' | awk '{print $1}' | sort -u)
dnf -y -q install --setopt=install_weak_deps=False "${needs[@]}" >/dev/null
id builder >/dev/null 2>&1 || useradd --create-home builder
build=$(runuser -u builder -- mktemp -d)
cp "$srpm" "$build/"
chown builder: "$build/$(basename "$srpm")"
runuser -u builder -- rpmbuild --rebuild --define "_topdir $build" "$build/$(basename "$srpm")"

mkdir -p "$out"
cp "$srpm" "$build"/RPMS/noarch/agentbox-*.noarch.rpm "$out/"
ls -l "$out"
