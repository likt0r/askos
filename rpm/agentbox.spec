# Agent Box as a Fedora RPM, one per Release (ADR 0009). rpm/build.sh makes the source RPM:
# it puts the Release from the file VERSION in place of @VERSION@ (the version lives only
# there) and takes the sources from git and the Control Room UI from its CI build (ADR 0011).
#
# Installing creates the service users and the directories, installs the sudoers rule of the
# Control Room after visudo accepted it, and opens, enables and starts nothing: the Host
# setup, the Control Room password, the certificate, the services and the other Host steps
# (ARCHITECTURE.md, "Setting a Host up") come with `sudo agentbox setup`. The package never
# writes Settings, the VM registry, the certificate or the Host setup (ADR 0009).
#
# An update restarts the Control Room, the Alert watch, the expiry and cleanup timers and, when
# their code changed, the proxies of the VMs, and never a VM. Removal also takes the units
# agentbox regenerate wrote (docs/releasing.md). rpm/upgrade-test.sh checks both. What the
# scriptlets do beyond rpm's macros is in rpm/agentbox-package; they only call it.

Name:           agentbox
Version:        @VERSION@
Release:        1%{?dist}
Summary:        Coding agents in isolated VMs with allowlist egress and short-lived credentials
License:        MIT
URL:            https://github.com/likt0r/askos
Source0:        agentbox-%{version}.tar.gz
# web/ui/dist as built in CI from the lockfile (ADR 0011); the Host has no Node (ADR 0003)
Source1:        agentbox-ui-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros
# visudo for the check of the sudoers rule
BuildRequires:  sudo >= 1.9.10

Requires:       python3
Requires:       systemd
# regular expressions in the Control Room's sudoers rule
Requires:       sudo >= 1.9.10
# the VMs: libvirt with KVM, its virsh, qemu-img and virt-install
Requires:       libvirt-daemon-kvm
Requires:       libvirt-client
Requires:       qemu-img
Requires:       virt-install
# Builds of Images (ADR 0010)
Requires:       podman
# reads a manifest from the Host's container storage (the disk step's buildroot, agentbox/digests.py)
Requires:       skopeo
# the build VMs' image (agentbox/build_vm.py): its signed CHECKSUM checked by gpgv, run as
# agentbox-gpgv through setpriv, without network (unshare), against the Host's own Fedora key
Requires:       /usr/bin/gpgv
Requires:       /usr/bin/setpriv
Requires:       /usr/bin/unshare
Requires:       fedora-gpg-keys
# the build VMs (agentbox/build_vm_run.py): their output directory served by a virtiofsd of
# agentbox-buildvm, their cloud-init seed written by xorrisofs, their images handed in by skopeo
Requires:       virtiofsd
Requires:       xorriso
# the rules of the VM networks (agentbox-vmnet.service) and of Network mode (agentbox setup)
Requires:       nftables
Requires:       firewalld
# the Orca port of each VM, the certificate, the proxy's checks, Feeds and GitHub App
Requires:       socat
Requires:       openssl
Requires:       iproute
Requires:       curl
# visudo and chattr of agentbox-package in %%post
Requires(post): sudo >= 1.9.10
Requires(post): e2fsprogs
# agentbox setup: the owner user into libvirt and the members of agentbox-journal (usermod,
# gpasswd), and chattr for a VM disk directory it makes
Requires:       shadow-utils
Requires:       e2fsprogs
# Orca on the Host (for the Observer) is installed by the owner and stays an external
# dependency (README.md); the Images take theirs from Orca's GitHub release (vm/build/orca-release).

# The Containerfile and guest files are the Base image's build context: they belong to the
# VMs, not to the Host. Keep them byte for byte (no shebang rewriting) and take no
# dependencies from them.
%global __brp_mangle_shebangs_exclude_from ^%{_datadir}/agentbox/vm/
%global __requires_exclude_from ^%{_datadir}/agentbox/vm/

%description
Agent Box runs coding agents in isolated VMs on a Host, with outbound traffic limited to
an Allowlist and short-lived credentials. This package holds a Release: the agentbox CLI,
the Control Room, the proxy, detection and Feeds, the GitHub token service, the units,
and the Containerfile and guest files the Host builds its Images from.

After installing, run `sudo agentbox setup` to write the Host setup (Local-only or
Network mode), set the Control Room password, create the certificate, set up the Host
(groups, lists, the firewall zone of the VM networks, units) and start the services.

%prep
%autosetup -n agentbox-%{version}
mkdir -p web/ui/dist
tar -xzf %{SOURCE1} -C web/ui/dist
test -f web/ui/dist/index.html

%build
# nothing to build: Python without dependencies (ADR 0006), the UI comes built

%install
# The layout of agentbox/paths.py under the prefix /usr (ARCHITECTURE.md, "Where the files live").
install -D -m 0755 bin/agentbox %{buildroot}%{_bindir}/agentbox
install -d -m 0755 %{buildroot}%{_prefix}/lib/agentbox/agentbox
install -m 0644 agentbox/*.py %{buildroot}%{_prefix}/lib/agentbox/agentbox/
install -m 0644 VERSION %{buildroot}%{_prefix}/lib/agentbox/VERSION
# compiled here, so root running agentbox leaves no files of its own in /usr
%py_byte_compile %{python3} %{buildroot}%{_prefix}/lib/agentbox/agentbox

install -d -m 0755 %{buildroot}%{_libexecdir}
install -m 0755 proxy/agentbox-proxy proxy/agentbox-suspicious proxy/agentbox-feeds \
    github-app/agentbox-gh-tokend github-app/agentbox-claude-route web/agentbox-web %{buildroot}%{_libexecdir}/
# loaded by agentbox-web from next to it, not run
install -m 0644 web/agentbox-readings %{buildroot}%{_libexecdir}/
# run by the scriptlets
install -m 0755 rpm/agentbox-package %{buildroot}%{_libexecdir}/

# the Control Room UI
install -d -m 0755 %{buildroot}%{_datadir}/agentbox/web/ui
cp -R web/ui/dist/. %{buildroot}%{_datadir}/agentbox/web/ui/

# the Base image's build context (ADR 0010): what vm/Containerfile copies, at the same paths
install -d -m 0755 %{buildroot}%{_datadir}/agentbox/vm/vm %{buildroot}%{_datadir}/agentbox/vm/github-app
cp -R vm/. %{buildroot}%{_datadir}/agentbox/vm/vm/
install -m 0755 github-app/agentbox-gh-token github-app/git-credential-agentbox github-app/gh \
    %{buildroot}%{_datadir}/agentbox/vm/github-app/

# the Allowlist, Watchlist and Legitimate domains a new Host starts with
install -d -m 0755 %{buildroot}%{_datadir}/agentbox/lists
install -m 0644 proxy/allowlist proxy/suspicious-domains proxy/legitimate-domains \
    %{buildroot}%{_datadir}/agentbox/lists/

# only directories and files, root's, readable by all, executable where git says so: links
# in the UI build could point anywhere
find %{buildroot}%{_datadir}/agentbox ! -type d ! -type f -delete
find %{buildroot}%{_datadir}/agentbox -type d -exec chmod 0755 {} +
find %{buildroot}%{_datadir}/agentbox -type f -perm /111 -exec chmod 0755 {} +
find %{buildroot}%{_datadir}/agentbox -type f ! -perm /111 -exec chmod 0644 {} +

# units that come with the package; agentbox regenerate writes the others to /etc
install -d -m 0755 %{buildroot}%{_unitdir}/systemd-journald@agentbox.socket.d
install -m 0644 rpm/units/agentbox-web.service rpm/units/agentbox-vmnet.service \
    rpm/units/agentbox-allowlist-expire.service rpm/units/agentbox-allowlist-expire.timer \
    rpm/units/agentbox-container-image-cleanup.service rpm/units/agentbox-container-image-cleanup.timer \
    rpm/units/agentbox-alert-watch.service \
    %{buildroot}%{_unitdir}/
install -m 0644 rpm/units/agentbox-journal.conf %{buildroot}%{_unitdir}/systemd-journald@agentbox.socket.d/

install -D -m 0644 rpm/agentbox.sysusers %{buildroot}%{_sysusersdir}/agentbox.conf
install -D -m 0644 rpm/agentbox.tmpfiles %{buildroot}%{_tmpfilesdir}/agentbox.conf
install -D -m 0440 rpm/agentbox-web.sudoers %{buildroot}%{_sysconfdir}/sudoers.d/agentbox-web

# the Host's directories; what goes into them comes from agentbox setup and the CLI
install -d -m 0755 %{buildroot}%{_sysconfdir}/agentbox
install -d -m 0750 %{buildroot}%{_sysconfdir}/agentbox/web %{buildroot}%{_sysconfdir}/agentbox/github \
    %{buildroot}%{_sysconfdir}/agentbox/claude
install -d -m 0755 %{buildroot}%{_sharedstatedir}/agentbox
install -d -m 0711 %{buildroot}%{_sharedstatedir}/agentbox/images %{buildroot}%{_sharedstatedir}/agentbox/vms

%check
# A sudoers rule visudo rejects would stop sudo on the Host for everyone.
visudo -cqf rpm/agentbox-web.sudoers
# The detection self-test agentbox setup runs before it changes anything, with the lists a
# new Host starts with: the proxy and the classifier of this Release, on a Unix socket.
lists=$(mktemp -d)
cp proxy/suspicious-domains proxy/legitimate-domains "$lists/"
AGENTBOX_CONF_DIR="$lists" %{python3} -I -B proxy/agentbox-suspicious --self-test
rm -rf "$lists"

%pre
# update only: the hash of the proxy code of the Release this one replaces, taken by that
# Release's agentbox-package before its files are replaced; an update from a Release without
# one restarts no proxy
if [ $1 -gt 1 ] && [ -x %{_libexecdir}/agentbox-package ]; then
    %{_libexecdir}/agentbox-package proxy-code-snapshot || :
fi

%post
%{_libexecdir}/agentbox-package sudoers-check
if [ $1 -eq 1 ]; then
    %{_libexecdir}/agentbox-package nocow-directories
fi
# the Activity journal's runtime directory, owned as journald owns it, and the directory of
# the Control Room's sign-in links (agentbox.tmpfiles)
%tmpfiles_create agentbox.conf
# No %%systemd_post: its preset would leave the units disabled on a new Host anyway, and
# would disable the ones a Host has enabled. agentbox setup enables and starts what it needs.

%preun
# removal only: stop and disable the Control Room, the Alert watch, the expiry timer and the
# timer of the Container image cleanup. agentbox-vmnet stays: its rules keep the VM networks
# closed until the Host restarts.
%systemd_preun agentbox-web.service agentbox-alert-watch.service agentbox-allowlist-expire.timer agentbox-container-image-cleanup.timer
# removal only: the units agentbox regenerate wrote to /etc/systemd/system run programs of
# this package: per VM the proxy, the token service, its Anthropic route and the Orca port, and the Maintenance
# window, the Feeds, the Builds and the version check (and the timers of before the
# Maintenance window, should any be left). They are disabled, stopped and deleted with it, and systemd forgets
# them, so none is left enabled pointing at a removed program; agentbox setup writes them again after a new
# install. Running instances of the templates end with their connection or Build. The
# Host's records and state in /etc/agentbox and /var/lib/agentbox, its VMs and their
# networks stay (docs/releasing.md).
if [ $1 -eq 0 ]; then
    units=$(%{_libexecdir}/agentbox-package generated-units)
    if [ -n "$units" ]; then
        /usr/lib/systemd/systemd-update-helper remove-system-units $units || :
    fi
    %{_libexecdir}/agentbox-package forget-generated-units
fi

%postun
# update only: restart what runs, so it runs the new Release; no VM is touched
%systemd_postun_with_restart agentbox-web.service agentbox-alert-watch.service agentbox-allowlist-expire.timer agentbox-container-image-cleanup.timer

%posttrans
# update only: the proxies of the VMs that run restart when this Release brings other proxy
# code, or when the restart of an update before failed; no VM is touched
%{_libexecdir}/agentbox-package proxy-code-restart

%files
%license LICENSE
%doc README.md
%{_bindir}/agentbox
%{_prefix}/lib/agentbox/
%{_libexecdir}/agentbox-proxy
%{_libexecdir}/agentbox-suspicious
%{_libexecdir}/agentbox-feeds
%{_libexecdir}/agentbox-gh-tokend
%{_libexecdir}/agentbox-claude-route
%{_libexecdir}/agentbox-web
%{_libexecdir}/agentbox-readings
%{_libexecdir}/agentbox-package
%{_datadir}/agentbox/
%{_unitdir}/agentbox-web.service
%{_unitdir}/agentbox-vmnet.service
%{_unitdir}/agentbox-allowlist-expire.service
%{_unitdir}/agentbox-allowlist-expire.timer
%{_unitdir}/agentbox-container-image-cleanup.service
%{_unitdir}/agentbox-container-image-cleanup.timer
%{_unitdir}/agentbox-alert-watch.service
%{_unitdir}/systemd-journald@agentbox.socket.d/
%{_sysusersdir}/agentbox.conf
%{_tmpfilesdir}/agentbox.conf
%config %attr(0440,root,root) %{_sysconfdir}/sudoers.d/agentbox-web
%dir %attr(0755,root,root) %{_sysconfdir}/agentbox
%dir %attr(0750,root,agentbox-web) %{_sysconfdir}/agentbox/web
%dir %attr(0750,root,agentbox-gh) %{_sysconfdir}/agentbox/github
%dir %attr(0750,root,agentbox-claude) %{_sysconfdir}/agentbox/claude
%dir %attr(0755,root,root) %{_sharedstatedir}/agentbox
%dir %attr(0711,root,root) %{_sharedstatedir}/agentbox/images
%dir %attr(0711,root,root) %{_sharedstatedir}/agentbox/vms
