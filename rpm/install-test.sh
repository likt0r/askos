#!/usr/bin/env bash
# The installation test of the RPM: installs it as root in a fresh Fedora container and
# checks the result with tests/test_rpm.py (installed paths, owners and modes, the
# `python3 -I` starters, the version, and that installing wrote no Host setup and enabled,
# started or opened nothing). Then it runs `agentbox setup` there twice and checks the Host
# steps a container can show (InstalledSetupTest). CI runs it on every pull request
# (.github/workflows/ci.yml).
#
#   rpm/install-test.sh <agentbox-*.noarch.rpm> [<tag>]
#
# <tag> (vMAJOR.MINOR.PATCH), when given, is the Release the installed CLI has to report.
#
# Locally, with podman instead of docker:
#   podman run --rm --security-opt label=disable -v "$PWD:/src:ro" -v "$OUT:/out:ro" \
#     quay.io/fedora/fedora:45 /src/rpm/install-test.sh /out/agentbox-<version>-1.fc45.noarch.rpm
set -euo pipefail

[ -e /run/.containerenv ] || [ -e /.dockerenv ] || {
  echo "rpm/install-test.sh installs packages as root: only in a throwaway container." >&2
  exit 1
}
[ $# -ge 1 ] || { echo "Usage: rpm/install-test.sh <agentbox-*.noarch.rpm> [<tag>]" >&2; exit 2; }

src=$(cd "$(dirname "$0")/.." && pwd)
package=$1
state=$(mktemp -d)

# First what the package requires, so that the two snapshots around its own installation
# differ only by what installing it did. The users and groups it creates itself.
mapfile -t needs < <(rpm -qp --requires "$package" | grep -Ev '^(rpmlib|config|user|group)\(' \
                     | awk '{print $1}' | sort -u)
dnf -y -q install --setopt=install_weak_deps=False "${needs[@]}" findutils >/dev/null

# every path of the root file system with its type, mode, owner, size, time and link target;
# the package databases, caches, logs and this test's own files left out
snapshot() {
  find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /tmp \
                 -o -path /var/tmp -o -path /var/cache -o -path /var/log -o -path /usr/lib/sysimage \
                 -o -path /var/lib/rpm -o -path /var/lib/dnf -o -path /src -o -path "$state" \) -prune \
         -o -printf '%p\t%y\t%m\t%u\t%g\t%s\t%T@\t%l\n' | LC_ALL=C sort > "$1"
}
snapshot "$state/before"
dnf -y -q install --setopt=install_weak_deps=False "$package" >/dev/null
snapshot "$state/after"
rpm -q --scripts agentbox > "$state/scripts"

AGENTBOX_RPM_STATE=$state AGENTBOX_RPM_TAG=${2:-} \
  python3 -I -B -m unittest discover -s "$src/tests" -t "$src/tests" -p test_rpm.py -v
# only now: setup changes /etc, which the tests above check as installing left it
AGENTBOX_RPM_STATE=$state AGENTBOX_RPM_SETUP=1 \
  python3 -I -B -m unittest discover -s "$src/tests" -t "$src/tests" -p test_rpm.py -k InstalledSetupTest -v
