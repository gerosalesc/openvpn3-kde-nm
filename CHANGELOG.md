# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-25

### Added
- **Native Debian / Ubuntu (`.deb`) Packaging (`packaging/debian/`, `packaging/build-deb.sh`)**:
  - Builds a standards-compliant `openvpn3-kde-nm_<version>-1_all.deb` package targeting Debian 13 (Trixie), Ubuntu 26.04 LTS (Resolute), Ubuntu 24.04 LTS (Noble), and derivatives.
  - Installs `openvpn3-kde-cli`, `openvpn3-kde-sync`, `/usr/libexec/nm-openvpn3-service`, and the global systemd user unit `/usr/lib/systemd/user/openvpn3-kde-sync.service`.
  - Uses `dpkg-divert` and dpkg triggers via `packaging/common/nm-hook.sh` so the NetworkManager OpenVPN 3 hook survives `network-manager-openvpn` upgrades and cleanly restores OpenVPN 2 on removal.
- **Native Arch Linux (`.pkg.tar.zst` + `PKGBUILD`) Packaging (`packaging/arch/`, `packaging/build-arch.sh`)**:
  - Produces prebuilt `.pkg.tar.zst` packages alongside `PKGBUILD`, `openvpn3-kde-nm.install`, `.SRCINFO`, and AUR source tarballs.
  - Installs a pacman ALPM hook (`/usr/share/libalpm/hooks/90-openvpn3-kde-nm.hook`) so the NetworkManager hook survives `networkmanager-openvpn` upgrades and restores OpenVPN 2 bit-for-bit on `pacman -R openvpn3-kde-nm`.
- **Automated Release Packaging CI (`.github/workflows/release-packages.yml`) & Verification Suite (`packaging/tests/`)**:
  - Automatically builds, lints (`lintian` / `namcap`), container-tests across Debian 13, Ubuntu 26.04, Ubuntu 24.04, and Arch Linux, generates `SHA256SUMS.txt`, and attaches all distro packages to GitHub Releases.

### Changed
- **System-Package Awareness in `openvpn3-kde-cli` (`openvpn3_kde_nm/installer.py`)**:
  - Added `_is_system_package_install()` and `_detect_package_uninstall_hint()` so `openvpn3-kde-cli install` verifies and syncs system-packaged installs without overwriting package-managed files, and `openvpn3-kde-cli uninstall` directs users to `apt remove` / `pacman -R`.
- Updated `README.md` with Debian/Ubuntu (`.deb`) and Arch Linux (`.pkg.tar.zst` / `PKGBUILD`) installation and uninstallation instructions, and ignored local package build outputs (`out/`, `*.deb`, `*.pkg.tar.*`) in `.gitignore`.

## [1.1.1] - 2026-11-19

### Fixed
- **Survive `openvpn3-service-sessionmgr` & `configmgr` Idle Restarts**:
  - Wrapped `dbus.SystemBus.get_object` with `follow_name_owner_changes=True` in `open_user_dbus_system_bus()` and subscribed to `SessionManagerEvent` with `bus_name=None` in `session_watcher.py` so `openvpn3-kde-sync.service` never loses its D-Bus signal subscription or binds to a stale unique bus name (`:1.xxx`) after OpenVPN 3 background services shut down on idle.
- **Escape `NetworkManager.service`'s `ProtectHome=read-only` Sandbox for WebAuth**:
  - Updated `open_user_browser()` in `nm_vpn_service.py` to launch WebAuth SSO browser tabs via `systemd-run --user --collect -p KillMode=none xdg-open <url>` (with fallback to `org.freedesktop.portal.OpenURI`) and send KDE notifications directly over the user's Session D-Bus (`unix:path=/run/user/<uid>/bus`) instead of `runuser`.
- **Cross-Daemon WebAuth Deduplication & Extended VPN Connect Timeout**:
  - Added atomic WebAuth URL lock (`claim_webauth_url`) across `nm-openvpn3-service` and `openvpn3-kde-sync.service` so single-use WebAuth challenge URLs are opened exactly once.
  - Set `vpn.timeout = 180` seconds on NetworkManager VPN profiles to allow sufficient time for interactive SSO/MFA logins.

## [1.1.0] - 2026-11-16

### Added
- **PyPI Packaging Support**:
  - Updated `pyproject.toml` to use `setuptools>=77.0.0`, SPDX license expression (`MIT`), explicit `[tool.setuptools.packages.find]`, and `[tool.setuptools.package-data]` so `uv build` / `python -m build` produce clean source distributions and wheels with zero warnings.
  - Added `openvpn3_kde_nm/installer.py` and bundled `openvpn3_kde_nm/data/openvpn3-kde-sync.service` as package data.
  - Added `openvpn3-kde-cli install` and `openvpn3-kde-cli uninstall` subcommands so PyPI / `pipx` installations are completely self-contained without needing a git clone.
  - Documented `pipx install --system-site-packages openvpn3-kde-nm` in `README.md`.

### Changed
- Refactored `install.sh` and `uninstall.sh` to delegate directly to `openvpn3-kde-cli install` and `openvpn3-kde-cli uninstall`.

## [1.0.0] - 2026-11-16

### Added
- **Native NetworkManager OpenVPN 3 Service Plugin (`nm-openvpn3-service`)**:
  - Implements `NM.VpnServicePlugin` (`org.freedesktop.NetworkManager.VPN.Plugin`) to bridge KDE Connection Manager (`plasma-nm`) and NetworkManager directly to the OpenVPN 3 Linux D-Bus services (`net.openvpn.v3.sessions` and `net.openvpn.v3.configuration`).
  - Attaches seamlessly to already-active OpenVPN 3 sessions (`tun0`, etc.) and preserves split-tunnel routes (`preserve-routes = True`, `never-default = True`) configured by `openvpn3-service-netcfg`.
- **Bidirectional Profile & Session Synchronization (`openvpn3-kde-sync`)**:
  - Synchronizes existing OpenVPN 3 configurations (`openvpn3 configs-list`) into KDE System Settings -> Connections and the KDE Plasma Networks system tray widget.
  - Automatically removes the corresponding configuration and active session from OpenVPN 3 whenever a VPN connection is deleted in KDE Connection Manager (`connection-removed` event), and vice versa.
  - Synchronizes active session state (`Connected` / `Disconnected`) when sessions are started or stopped via the `openvpn3` CLI (`openvpn3 session-start` / `openvpn3 session-manage --disconnect`).
- **CloudConnexa / OpenVPN Cloud & WebAuth Support**:
  - Preserves `setenv USERNAME`, `socket-flags TCP_NODELAY`, `push-peer-info`, `persist-tun`, and inline certificates (`<ca>`, `<cert>`, `<key>`, `<tls-auth>`, `<tls-crypt>`).
  - Automatically recovers stripped `setenv USERNAME` values from the X.509 `Subject CN` of `cert.pem` when connections are imported or created via KDE's built-in `VPN (openvpn)` editor UI.
  - Detects `StatusMajor.SESSION / StatusMinor.SESS_AUTH_URL` events, opens the SSO authentication URL in the user's browser (`xdg-open`), and sends native KDE desktop notifications.
- **Command-Line Interface (`openvpn3-kde-cli`)**:
  - Added `status`, `stats` (including `--watch` real-time refresh), `logs` (including `-f` follow mode), `sync`, and `import` subcommands.
- **Installer & Uninstaller (`install.sh`, `uninstall.sh`)**:
  - Polkit (`pkexec`) / `sudo` installation of `/usr/local/libexec/nm-openvpn3-service` and `/usr/local/lib/openvpn3-kde-nm` (compatible with NetworkManager's `ProtectHome=read-only` systemd sandbox) with automatic backup and restore of original OpenVPN 2 files in `/var/backups/`.
