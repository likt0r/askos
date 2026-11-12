# Changelog

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
