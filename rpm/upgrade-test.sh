#!/usr/bin/env bash
# The update and removal test of the RPM (#82): in a fresh Fedora container, installs an
# older Release, gives it what a Host in use has (tests/rpm_host.py), updates it to the RPM
# given, reinstalls that and removes it, and checks each step with tests/test_rpm_upgrade.py:
# the Host's records and state stay byte for byte, nothing is enabled or started that was not
# before, no VM is touched, the proxies restart only when their code changed, and removal
# takes the package and the units agentbox regenerate wrote but nothing of the Host's. CI runs
# it on every pull request (.github/workflows/ci.yml).
#
#   rpm/upgrade-test.sh <agentbox-*.noarch.rpm> [<ui-build>]
#
# The older Release is built here with rpm/build.sh from this commit: VERSION with "-older"
# (1.2.0-older is 1.2.0~older for rpm, which sorts before 1.2.0) and a classifier with one
# comment line more, so its proxy code differs. <ui-build> as for rpm/build.sh.
#
# The container has no systemd, so systemctl only enables and disables there and
# systemd-update-helper marks and restarts nothing. Every program that could change a unit, a
# VM or the firewall is replaced by a wrapper that records its arguments and then runs it, and
# the tests check what the scriptlets asked for. What needs a running systemd (systemctl
# try-restart and daemon-reload) the wrapper answers itself: it succeeds, except that the
# restart of the proxies fails once in the update, so that the reinstall after it has to do it.
#
# Locally, with podman instead of docker:
#   podman run --rm --security-opt label=disable -v "$PWD:/src:ro" -v "$OUT:/out:ro" \
#     quay.io/fedora/fedora:45 /src/rpm/upgrade-test.sh /out/agentbox-<version>-1.fc45.noarch.rpm \
#     [/src/web/ui/dist]
set -euo pipefail

[ -e /run/.containerenv ] || [ -e /.dockerenv ] || {
  echo "rpm/upgrade-test.sh installs packages as root: only in a throwaway container." >&2
  exit 1
}
[ $# -ge 1 ] || { echo "Usage: rpm/upgrade-test.sh <agentbox-*.noarch.rpm> [<ui-build>]" >&2; exit 2; }

src=$(cd "$(dirname "$0")/.." && pwd)
current=$1
ui=${2:-$src/web/ui/dist}
state=$(mktemp -d)

# the older Release, from this commit's files
dnf -y -q install --setopt=install_weak_deps=False git-core >/dev/null
older=$state/older
mkdir -p "$older/tree"
git -c safe.directory='*' -C "$src" archive HEAD | tar -x -C "$older/tree"
(
  cd "$older/tree"
  printf '%s-older\n' "$(tr -d '[:space:]' < VERSION)" > VERSION
  printf '# the older Release of rpm/upgrade-test.sh\n' >> proxy/agentbox-suspicious
  git init -q
  git add -A
  git -c user.name=upgrade-test -c user.email=upgrade-test@localhost commit -q -m "older Release"
)
"$older/tree/rpm/build.sh" "$older/rpm" "$ui" >/dev/null
older_rpm=$(echo "$older"/rpm/agentbox-*.noarch.rpm)

# what the package requires, then the older Release
mapfile -t needs < <(rpm -qp --requires "$current" | grep -Ev '^(rpmlib|config|user|group)\(' \
                     | awk '{print $1}' | sort -u)
dnf -y -q install --setopt=install_weak_deps=False "${needs[@]}" findutils >/dev/null
dnf -y -q install --setopt=install_weak_deps=False "$older_rpm" >/dev/null
rpm -ql agentbox > "$state/files-older"
python3 -I -B "$src/tests/rpm_host.py" prepare "$state"

# from here on every call of these is recorded in $state/calls, one line per call, tab-separated
for program in /usr/bin/systemctl /usr/lib/systemd/systemd-update-helper /usr/bin/virsh /usr/bin/virt-install \
               /usr/bin/qemu-img /usr/bin/podman /usr/sbin/nft /usr/bin/firewall-cmd; do
  mv "$program" "$program.recorded"
  cat > "$program" <<WRAPPER
#!/bin/sh
{ printf '%s' "\${0##*/}"; printf '\t%s' "\$@"; printf '\n'; } >> $state/calls
if [ "\${0##*/}" = systemctl ]; then
  case "\$1" in
    try-restart) if rm "$state/fail-try-restart" 2>/dev/null; then exit 1; fi; exit 0 ;;
    daemon-reload) exit 0 ;;
  esac
fi
exec $program.recorded "\$@"
WRAPPER
  chmod 0755 "$program"
done

step() {    # step <name> <dnf arguments>: one dnf call, its calls and the file system after it
  local name=$1
  shift
  : > "$state/calls"
  dnf -y -q "$@" >/dev/null
  mv "$state/calls" "$state/calls-$name"
  python3 -I -B "$src/tests/rpm_host.py" snapshot "$state/$name"
  AGENTBOX_RPM_UPGRADE_STATE=$state AGENTBOX_RPM_STEP=$name \
    python3 -I -B -m unittest discover -s "$src/tests" -t "$src/tests" -p test_rpm_upgrade.py -v
}

python3 -I -B "$src/tests/rpm_host.py" snapshot "$state/before"
touch "$state/fail-try-restart"
step upgraded upgrade "$current"            # the proxy code changes; restarting fails once
step retried reinstall "$current"           # the same code, but the restart is still pending
step reinstalled reinstall "$current"       # the same code: the proxies keep running
rpm -ql agentbox > "$state/files-current"
step removed remove agentbox
