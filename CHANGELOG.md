# Changelog

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
