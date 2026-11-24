# Changelog

## [0.11.0](https://github.com/likt0r/askos/compare/v0.10.1...v0.11.0) (2026-11-24)

### Features

* a Build in build VMs imports its image, checks it and makes its disk in an offline second VM ([#190](https://github.com/likt0r/askos/issues/190)) ([602bfde](https://github.com/likt0r/askos/commit/602bfdef7c4761c5d960392a0c4a021119fd1cbb)), references [#144](https://github.com/likt0r/askos/issues/144)
* show podman's STEP n/m and its command for a running Build ([#185](https://github.com/likt0r/askos/issues/185)) ([b78921e](https://github.com/likt0r/askos/commit/b78921e1fe6c9a75b7a97545be2e26dda2260f23)), closes [#183](https://github.com/likt0r/askos/issues/183), references [#186](https://github.com/likt0r/askos/issues/186) [#187](https://github.com/likt0r/askos/issues/187)
* the build VMs' image, the signed Fedora Cloud image, fetched and checked by the version check ([#188](https://github.com/likt0r/askos/issues/188)) ([2a65670](https://github.com/likt0r/askos/commit/2a65670205754f575f1defbe5546024b908ff536)), references [#144](https://github.com/likt0r/askos/issues/144)
* the first build VM runs a Build's podman build, behind an internal switch that is off ([#189](https://github.com/likt0r/askos/issues/189)) ([ed59eff](https://github.com/likt0r/askos/commit/ed59effbcbbdf890b316f571b1aa9effdeecb49c)), references [#144](https://github.com/likt0r/askos/issues/144)
* the Setting build_vm runs Builds and the package count in build VMs by default ([#191](https://github.com/likt0r/askos/issues/191)) ([f10dd5b](https://github.com/likt0r/askos/commit/f10dd5b9c39afd545f525eb1c3572ecfac5016d6)), closes [#144](https://github.com/likt0r/askos/issues/144), references [#144](https://github.com/likt0r/askos/issues/144) [#181](https://github.com/likt0r/askos/issues/181)

### Bug Fixes

* check-versions says "not fetched yet" for digests a fetch file has no entry for ([#187](https://github.com/likt0r/askos/issues/187)) ([c04aaf8](https://github.com/likt0r/askos/commit/c04aaf89a4817ce78d2ec4925e36098d64f7f435)), closes [#184](https://github.com/likt0r/askos/issues/184)

## [0.10.1](https://github.com/likt0r/askos/compare/v0.10.0...v0.10.1) (2026-11-23)

### Bug Fixes

* the disk step names the buildroot by its platform image's digest ([#186](https://github.com/likt0r/askos/issues/186)) ([1a15a11](https://github.com/likt0r/askos/commit/1a15a11d8ed6697a0ac7fbe59d3a034405905f74)), references [#181](https://github.com/likt0r/askos/issues/181) [#144](https://github.com/likt0r/askos/issues/144)

## [0.10.0](https://github.com/likt0r/askos/compare/v0.9.0...v0.10.0) (2026-11-23)

### Features

* forbid a destination for one VM for a while: refused at once, no hold ([#39](https://github.com/likt0r/askos/issues/39)) ([#179](https://github.com/likt0r/askos/issues/179)) ([b301397](https://github.com/likt0r/askos/commit/b3013971d2783f07a9e0a441b1058ec61f5fa451))
* pin fedora-bootc and bootc-image-builder by digest; the disk step's buildroot is the pristine fedora-bootc ([#144](https://github.com/likt0r/askos/issues/144)) ([#181](https://github.com/likt0r/askos/issues/181)) ([7d18026](https://github.com/likt0r/askos/commit/7d180264f7e42e862bf68cd0fa2f94dc60be9697))
* the proxy holds an unknown destination for up to 30 s so the owner can allow it live ([#39](https://github.com/likt0r/askos/issues/39), first step) ([#176](https://github.com/likt0r/askos/issues/176)) ([027c71f](https://github.com/likt0r/askos/commit/027c71f232db752e3178587e2450966eef4324ff))

### Bug Fixes

* a held destination is notified again after a minute ([#177](https://github.com/likt0r/askos/issues/177)) ([3824ddc](https://github.com/likt0r/askos/commit/3824ddc9d85f4fa3451d49e2708f31072f29092a)), references [#39](https://github.com/likt0r/askos/issues/39) [#39](https://github.com/likt0r/askos/issues/39)
* a reboot inside a VM ends it, so its next start reads the current SSH keys ([#160](https://github.com/likt0r/askos/issues/160)) ([#180](https://github.com/likt0r/askos/issues/180)) ([f9d3e41](https://github.com/likt0r/askos/commit/f9d3e41f5028ebc86ce6aab8a5d422041ea6b8a2))

## [0.9.0](https://github.com/likt0r/askos/compare/v0.8.1...v0.9.0) (2026-11-20)

### Features

* an Alert with a strong Signal pauses its VM ([#11](https://github.com/likt0r/askos/issues/11)) ([#173](https://github.com/likt0r/askos/issues/173)) ([e3d1048](https://github.com/likt0r/askos/commit/e3d10488ae19a0965bed29a551177c86ebf53ec7))
* Container image cleanup counts a container's image as used at every run, records a repeated failure once ([#68](https://github.com/likt0r/askos/issues/68), [#69](https://github.com/likt0r/askos/issues/69)) ([#169](https://github.com/likt0r/askos/issues/169)) ([b0604db](https://github.com/likt0r/askos/commit/b0604db551ac4a40ba25b75d002cfb0781afdd9f)), references [60/#67](https://github.com/likt0r/askos/issues/67) [#67](https://github.com/likt0r/askos/issues/67)
* Secrets per VM, passed like VM variables but never shown or logged on the Host ([#30](https://github.com/likt0r/askos/issues/30)) ([#172](https://github.com/likt0r/askos/issues/172)) ([6e0eb70](https://github.com/likt0r/askos/commit/6e0eb7067a6c635e060c6bc34ed33c3159241a87)), references [#166](https://github.com/likt0r/askos/issues/166)
* strong Signals always block, also on the Allowlist ([#59](https://github.com/likt0r/askos/issues/59)) ([#170](https://github.com/likt0r/askos/issues/170)) ([c44b809](https://github.com/likt0r/askos/commit/c44b809c6a2b7e51fb2dedbc318a2686c44a252e))
* the Claude token stays on the Host behind an Anthropic route of the token service ([#12](https://github.com/likt0r/askos/issues/12)) ([#175](https://github.com/likt0r/askos/issues/175)) ([e17f1e5](https://github.com/likt0r/askos/commit/e17f1e5270bac4295b128b4ffe4eab6406302dae))

## [0.8.1](https://github.com/likt0r/askos/compare/v0.8.0...v0.8.1) (2026-11-19)

### Bug Fixes

* the Base Build finds Orca's Node runtime pin of 1.4.261 ([#168](https://github.com/likt0r/askos/issues/168)) ([dbfaa24](https://github.com/likt0r/askos/commit/dbfaa24c3bf8995e30b390e19821d5aae877a6a8))

## [0.8.0](https://github.com/likt0r/askos/compare/v0.7.0...v0.8.0) (2026-11-19)

### Features

* agentbox ssh-config names VMs and keys their host keys on HostKeyAlias ([#134](https://github.com/likt0r/askos/issues/134)) ([#151](https://github.com/likt0r/askos/issues/151)) ([a5e09bb](https://github.com/likt0r/askos/commit/a5e09bb8445e956c2d1ace0fd1110efb26fa66c6)), references [#135](https://github.com/likt0r/askos/issues/135) [#126](https://github.com/likt0r/askos/issues/126)
* copy SSH keys from another VM in the Control Room ([#136](https://github.com/likt0r/askos/issues/136)) ([#158](https://github.com/likt0r/askos/issues/158)) ([8bba9ec](https://github.com/likt0r/askos/commit/8bba9ec1990ed22f5b0a83302803ae4cd115602c)), closes [#126](https://github.com/likt0r/askos/issues/126), references [#132](https://github.com/likt0r/askos/issues/132) [#135](https://github.com/likt0r/askos/issues/135)
* copy SSH keys from another VM, add your own with --mine ([#132](https://github.com/likt0r/askos/issues/132)) ([#154](https://github.com/likt0r/askos/issues/154)) ([ae6f67d](https://github.com/likt0r/askos/commit/ae6f67d269d7714b579485448512bf3149300162)), references [#126](https://github.com/likt0r/askos/issues/126)
* Images from Recipes the owner writes ([#117](https://github.com/likt0r/askos/issues/117)) ([#125](https://github.com/likt0r/askos/issues/125)) ([a2023a0](https://github.com/likt0r/askos/commit/a2023a07b0869ad1e4d2c34593895ece6f3be47e)), references [#123](https://github.com/likt0r/askos/issues/123) [#119](https://github.com/likt0r/askos/issues/119)
* open SSH sessions postpone the automatic Reset and show among devices ([#133](https://github.com/likt0r/askos/issues/133)) ([#156](https://github.com/likt0r/askos/issues/156)) ([e98c9df](https://github.com/likt0r/askos/commit/e98c9df942192ee28bd83e6ceabb72ce6d1b2fa4)), references [#135](https://github.com/likt0r/askos/issues/135) [#126](https://github.com/likt0r/askos/issues/126)
* Playwright with Chromium and Firefox in the Base image ([#162](https://github.com/likt0r/askos/issues/162)) ([#163](https://github.com/likt0r/askos/issues/163)) ([76a3988](https://github.com/likt0r/askos/commit/76a39887898660bbcd0239cd872fecf6aa089c0e))
* sign in to the Control Room with a one-time link from the Host ([#137](https://github.com/likt0r/askos/issues/137)) ([#149](https://github.com/likt0r/askos/issues/149)) ([f0ceaaf](https://github.com/likt0r/askos/commit/f0ceaaf7267ad1bdf6cde22e82a9b9440bc73a43))
* SSH access section on the Control Room's VM page ([#135](https://github.com/likt0r/askos/issues/135)) ([#157](https://github.com/likt0r/askos/issues/157)) ([b41e2be](https://github.com/likt0r/askos/commit/b41e2be7f7a99f88c547031db7cc772fd4535abc)), references [#136](https://github.com/likt0r/askos/issues/136) [#136](https://github.com/likt0r/askos/issues/136) [#126](https://github.com/likt0r/askos/issues/126)
* SSH access through the SSH port in Local-only ([#129](https://github.com/likt0r/askos/issues/129)) ([#147](https://github.com/likt0r/askos/issues/147)) ([660bd5a](https://github.com/likt0r/askos/commit/660bd5a06e9d2a2b1e4215ad6b29a94d897a354f)), references [#136](https://github.com/likt0r/askos/issues/136) [#130](https://github.com/likt0r/askos/issues/130) [#132](https://github.com/likt0r/askos/issues/132) [#126](https://github.com/likt0r/askos/issues/126)
* SSH keys take effect at once, removal ends SSH sessions ([#131](https://github.com/likt0r/askos/issues/131)) ([#152](https://github.com/likt0r/askos/issues/152)) ([01e10c1](https://github.com/likt0r/askos/commit/01e10c13a56abc3a11711fc6929288f37be00dd5)), references [#126](https://github.com/likt0r/askos/issues/126)
* SSH port in Network mode with its firewall rule ([#130](https://github.com/likt0r/askos/issues/130)) ([#148](https://github.com/likt0r/askos/issues/148)) ([c4dad2f](https://github.com/likt0r/askos/commit/c4dad2fb924ee83a6962522869d5667833257e7f)), closes [#150](https://github.com/likt0r/askos/issues/150), references [#150](https://github.com/likt0r/askos/issues/150) [#126](https://github.com/likt0r/askos/issues/126) [#150](https://github.com/likt0r/askos/issues/150) [#126](https://github.com/likt0r/askos/issues/126)
* the Control Room serves only its Nuxt UI and /api/v1 ([#82](https://github.com/likt0r/askos/issues/82)) ([8d2efba](https://github.com/likt0r/askos/commit/8d2efbaee4c25b062cb7c2e866d1f0abe345be70)), closes [#79](https://github.com/likt0r/askos/issues/79)

### Bug Fixes

* a refused settings.json stops the Control Room backend; small leftovers of [#84](https://github.com/likt0r/askos/issues/84)–[#89](https://github.com/likt0r/askos/issues/89) ([#98](https://github.com/likt0r/askos/issues/98)) ([807c8cf](https://github.com/likt0r/askos/commit/807c8cfbb46afe81097a94f77a8bff210f3527a3)), closes [#97](https://github.com/likt0r/askos/issues/97)
* a VM that is off never starts with a removed SSH key ([#153](https://github.com/likt0r/askos/issues/153)) ([#159](https://github.com/likt0r/askos/issues/159)) ([9299175](https://github.com/likt0r/askos/commit/92991757c52b3be1d6be7fead977b2f7b617b3ae))
* add-ssh-key reads FILE with the invoking user's rights ([#155](https://github.com/likt0r/askos/issues/155)) ([#161](https://github.com/likt0r/askos/issues/161)) ([7444923](https://github.com/likt0r/askos/commit/7444923f128cc8acfb258b7061de036ef81259e6))
* copies that drifted apart (proxy open_until, classify time limit, list texts, active states) ([#90](https://github.com/likt0r/askos/issues/90)) ([d90590a](https://github.com/likt0r/askos/commit/d90590abfca059dddc0f216d0033a82fb4515b80)), closes [#84](https://github.com/likt0r/askos/issues/84)
* guest-setup follows no link the agent left in its home ([#142](https://github.com/likt0r/askos/issues/142)) ([#143](https://github.com/likt0r/askos/issues/143)) ([49c99d5](https://github.com/likt0r/askos/commit/49c99d509248a8db320a1ccc9d999cd2ea1ea1ff))
* libvirt's idle state counts as running everywhere ([#116](https://github.com/likt0r/askos/issues/116)) ([#122](https://github.com/likt0r/askos/issues/122)) ([c1c8adc](https://github.com/likt0r/askos/commit/c1c8adc0ce1ac41ae37f5ebeb89ccff1261c5a2d))
* one helper for Host programs and one for programs in the VM, with one wording for their failures ([#95](https://github.com/likt0r/askos/issues/95)) ([6cee482](https://github.com/likt0r/askos/commit/6cee482bb4bbbdb783066a102b0645bca5cf9fe5)), closes [#86](https://github.com/likt0r/askos/issues/86)
* one pattern for an Image name, the registry takes the Settings' ([#106](https://github.com/likt0r/askos/issues/106)) ([#108](https://github.com/likt0r/askos/issues/108)) ([4667397](https://github.com/likt0r/askos/commit/4667397445f29345a6a97b4dd3adf9c823bf489f)), references [#99](https://github.com/likt0r/askos/issues/99)
* setup --force keeps the Control Room password; --new-password replaces it ([#138](https://github.com/likt0r/askos/issues/138)) ([#146](https://github.com/likt0r/askos/issues/146)) ([a417c62](https://github.com/likt0r/askos/commit/a417c62a8f774a9cf84c9f0a4da8891d9731c6f5))
* SSH logins get the image-wide variables the services get ([#164](https://github.com/likt0r/askos/issues/164)) ([#165](https://github.com/likt0r/askos/issues/165)) ([602267b](https://github.com/likt0r/askos/commit/602267b8b934b523b879f13aaf8cbc99e3a540ca))
* sshd off in the Base image and fixed by a Safeguard ([#127](https://github.com/likt0r/askos/issues/127)) ([#140](https://github.com/likt0r/askos/issues/140)) ([6b6822c](https://github.com/likt0r/askos/commit/6b6822c8e0cb31192f195c322b66206904d52181)), references [#128](https://github.com/likt0r/askos/issues/128) [#129](https://github.com/likt0r/askos/issues/129) [#126](https://github.com/likt0r/askos/issues/126)
* the Control Room backend reads Host setup, Settings and Release through the package ([#94](https://github.com/likt0r/askos/issues/94)) ([893a667](https://github.com/likt0r/askos/commit/893a667c5daf7e7c1cf2077de775ebc322ac0a91)), closes [#89](https://github.com/likt0r/askos/issues/89)
* the web UI asks the backend whether a Build runs ([#115](https://github.com/likt0r/askos/issues/115)) ([#121](https://github.com/likt0r/askos/issues/121)) ([6e21105](https://github.com/likt0r/askos/commit/6e21105d2cd628386ab13efa285b7dd0b536d1d4)), references [#105](https://github.com/likt0r/askos/issues/105)
* threat model and trust boundary (ADR 0019); a file over 1 MiB fails the Safeguard check ([#141](https://github.com/likt0r/askos/issues/141)) ([94a907b](https://github.com/likt0r/askos/commit/94a907be10ba816e639f3d48f5e4e7f4077d4d88)), closes [#142](https://github.com/likt0r/askos/issues/142) [#143](https://github.com/likt0r/askos/issues/143), references [#128](https://github.com/likt0r/askos/issues/128) [#119](https://github.com/likt0r/askos/issues/119) [#140](https://github.com/likt0r/askos/issues/140) [#127](https://github.com/likt0r/askos/issues/127)

## [0.7.0](https://github.com/likt0r/askos/compare/v0.6.1...v0.7.0) (2026-11-13)

### Features

* agentbox import copies a Host folder into a VM, unpacked under ~/code ([#48](https://github.com/likt0r/askos/issues/48)) ([2650e53](https://github.com/likt0r/askos/commit/2650e53f0af0ab7246f5d2b974cceee0fec6cef3)), closes [#47](https://github.com/likt0r/askos/issues/47), references [#46](https://github.com/likt0r/askos/issues/46)
* containers survive a Reset; per-VM Container image cleanup ([#67](https://github.com/likt0r/askos/issues/67)) ([6863411](https://github.com/likt0r/askos/commit/6863411a0e0ee9ea9c9cc4b945cbddcd2bb2fb7d)), closes [#60](https://github.com/likt0r/askos/issues/60) [#61](https://github.com/likt0r/askos/issues/61) [#62](https://github.com/likt0r/askos/issues/62) [#63](https://github.com/likt0r/askos/issues/63) [#64](https://github.com/likt0r/askos/issues/64), references [#60](https://github.com/likt0r/askos/issues/60)
* Feed hits count on claudeusercontent.com subdomains, like GitHub's user-content hosts ([#65](https://github.com/likt0r/askos/issues/65)) ([1a6879b](https://github.com/likt0r/askos/commit/1a6879b82f8ecb5c4ba838c5907c4d2ada771708)), closes [#58](https://github.com/likt0r/askos/issues/58), references [#23](https://github.com/likt0r/askos/issues/23) [#55](https://github.com/likt0r/askos/issues/55) [#58](https://github.com/likt0r/askos/issues/58) [#59](https://github.com/likt0r/askos/issues/59) [#59](https://github.com/likt0r/askos/issues/59) [#59](https://github.com/likt0r/askos/issues/59) [#59](https://github.com/likt0r/askos/issues/59)
* Feed hits count on the user-content hosts github.io and githubusercontent.com ([#55](https://github.com/likt0r/askos/issues/55)) ([24d7a1f](https://github.com/likt0r/askos/commit/24d7a1fac2edd5d160ec6b9bf55bebb4be1ff61f)), closes [#23](https://github.com/likt0r/askos/issues/23), references [#18](https://github.com/likt0r/askos/issues/18)
* one maintenance window runs Feeds, version check, weekly rebuild and Reset check in order ([#66](https://github.com/likt0r/askos/issues/66)) ([aa83671](https://github.com/likt0r/askos/commit/aa836719b30d26a5feecd4dd6e844eea4638331e)), closes [#49](https://github.com/likt0r/askos/issues/49)
* VM variables, environment variables per VM for orca serve, agents, terminals and shells ([#31](https://github.com/likt0r/askos/issues/31)) ([92d017a](https://github.com/likt0r/askos/commit/92d017adde2d022c1c0d2e0cdc44e8264780b81e)), closes [#29](https://github.com/likt0r/askos/issues/29)

### Bug Fixes

* the Control Room may run only its own agentbox commands, without --force (sudoers and CLI) ([#56](https://github.com/likt0r/askos/issues/56)) ([98e07ef](https://github.com/likt0r/askos/commit/98e07ef457d866d5a4acc39507ceb184a4c02b97)), closes [#50](https://github.com/likt0r/askos/issues/50)

## [0.6.1](https://github.com/likt0r/askos/compare/v0.6.0...v0.6.1) (2026-11-12)

### Bug Fixes

* redefining an existing VM network keeps its UUID ([#44](https://github.com/likt0r/askos/issues/44)) ([d382558](https://github.com/likt0r/askos/commit/d3825583038740cc97d5569f9591dba231aac2cf)), closes [#43](https://github.com/likt0r/askos/issues/43)
* the Activity journal's sockets keep group agentbox-journal across boots ([#45](https://github.com/likt0r/askos/issues/45)) ([8c6cc3b](https://github.com/likt0r/askos/commit/8c6cc3b01cc27bd884075b5e3c2be79891b4e0d9)), closes [#42](https://github.com/likt0r/askos/issues/42)

## [0.6.0](https://github.com/likt0r/askos/compare/v0.5.1...v0.6.0) (2026-11-12)

### Features

* the proxy is transparent, names come from the TLS handshake and the resolver answers every name with the Host ([#37](https://github.com/likt0r/askos/issues/37)) ([e3f5e2b](https://github.com/likt0r/askos/commit/e3f5e2b437c261d8996c2c91bd9ebe6634424c3d)), references [#36](https://github.com/likt0r/askos/issues/36)
* VM connections to closed ports of the Host are refused at once, not dropped ([#41](https://github.com/likt0r/askos/issues/41)) ([3e68cfc](https://github.com/likt0r/askos/commit/3e68cfc0fac3687dd302105cd100e70a4dc9f8bc)), closes [#40](https://github.com/likt0r/askos/issues/40)
* **vm:** the VM resolves through the Host and carries no proxy variables ([#38](https://github.com/likt0r/askos/issues/38)) ([5839279](https://github.com/likt0r/askos/commit/58392791db20e13ca0d96ffcea7b1bf7605922a5)), closes [#36](https://github.com/likt0r/askos/issues/36), references [#37](https://github.com/likt0r/askos/issues/37)

## [0.5.1](https://github.com/likt0r/askos/compare/v0.5.0...v0.5.1) (2026-11-11)

### Bug Fixes

* docker compose in the VM is Docker Compose again; running containers get no proxy ([#35](https://github.com/likt0r/askos/issues/35)) ([f51a25a](https://github.com/likt0r/askos/commit/f51a25a7ad29a4266b8cc9aa7281bef369012b49)), closes [#34](https://github.com/likt0r/askos/issues/34), references [#28](https://github.com/likt0r/askos/issues/28) [#33](https://github.com/likt0r/askos/issues/33)
* short image names in the VM resolve to docker.io, not Fedora's registries ([#33](https://github.com/likt0r/askos/issues/33)) ([cecd3e3](https://github.com/likt0r/askos/commit/cecd3e3bdc8f2087d3d2ad87b3e4e0d8f9aab5a0)), closes [#32](https://github.com/likt0r/askos/issues/32), references [#28](https://github.com/likt0r/askos/issues/28)

## [0.5.0](https://github.com/likt0r/askos/compare/v0.4.0...v0.5.0) (2026-11-11)

### Features

* docker and containers in the VM use the agent's Podman and the Host's proxy ([#28](https://github.com/likt0r/askos/issues/28)) ([add6bbb](https://github.com/likt0r/askos/commit/add6bbba47c00ca45985e013987f3bc07438bab7)), closes [#27](https://github.com/likt0r/askos/issues/27)

## [0.4.0](https://github.com/likt0r/askos/compare/v0.3.1...v0.4.0) (2026-11-10)

### Features

* keep Claude Code from fetching the owner's claude.ai connectors in the VM ([#17](https://github.com/likt0r/askos/issues/17)) ([e103c73](https://github.com/likt0r/askos/commit/e103c732a77752b99e0d921776945176bd449bc0)), closes [#8](https://github.com/likt0r/askos/issues/8), references [#8](https://github.com/likt0r/askos/issues/8)
* Regex entries in the Allowlist, Watchlist, Legitimate domains and Harmless marks ([#20](https://github.com/likt0r/askos/issues/20)) ([77b7fbf](https://github.com/likt0r/askos/commit/77b7fbf9c7a7b7a4eed4fdc4c5dbafd1a667c3d9)), closes [#2](https://github.com/likt0r/askos/issues/2), references [#21](https://github.com/likt0r/askos/issues/21) [#2](https://github.com/likt0r/askos/issues/2) [#21](https://github.com/likt0r/askos/issues/21) [#21](https://github.com/likt0r/askos/issues/21) [#21](https://github.com/likt0r/askos/issues/21)
* several GitHub Apps on one Host, the token service picks the App installed on the repository ([#26](https://github.com/likt0r/askos/issues/26)) ([447c659](https://github.com/likt0r/askos/commit/447c65979bce4c35d6c78b56c06fb18ca10a54db)), closes [#25](https://github.com/likt0r/askos/issues/25)

## [0.3.1](https://github.com/likt0r/askos/compare/v0.3.0...v0.3.1) (2026-11-09)

## 0.3.0 (2026-11-09)

### Features

* Agent Box ([7a86c98](https://github.com/likt0r/askos/commit/7a86c982e57500b6c5e73b1bc3451d1e38a12c41)), references [#4](https://github.com/likt0r/askos/issues/4)
