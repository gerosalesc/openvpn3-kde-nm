# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
