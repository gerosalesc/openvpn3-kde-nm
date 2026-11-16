# OpenVPN 3 KDE Connection Manager Integration (`openvpn3-kde-nm`)

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform: Linux / KDE Plasma](https://img.shields.io/badge/platform-Linux%20%7C%20KDE%20Plasma-informational.svg)](https://kde.org/)

Native integration between **OpenVPN 3 Linux (`openvpn3`)** and **KDE Connection Manager (`plasma-nm` / NetworkManager)**.

Manage, connect, disconnect, and monitor your OpenVPN 3 profiles directly from **KDE System Settings -> Connections** and the **KDE Plasma Networks System Tray Applet**, just like OpenVPN 2—with full support for **OpenVPN Cloud / CloudConnexa**, **WebAuth / SSO browser login**, and **bidirectional CLI synchronization**.

---

## Why This Exists

KDE Connection Manager (`plasma-nm`) and `network-manager-openvpn` are built around classic OpenVPN 2.x (`/usr/sbin/openvpn`), which:
1. Strips directives required by OpenVPN Cloud / CloudConnexa such as `setenv USERNAME "..."` and `socket-flags TCP_NODELAY` when importing `.ovpn` files.
2. Does not integrate with OpenVPN 3's D-Bus architecture (`net.openvpn.v3.configuration`, `net.openvpn.v3.sessions`, `net.openvpn.v3.netcfg`).
3. Does not show sessions started via `openvpn3 session-start` as **Connected** inside the KDE Networks tray applet or KDE System Settings.

`openvpn3-kde-nm` bridges `org.freedesktop.NetworkManager.openvpn` to OpenVPN 3 so KDE's built-in `VPN (openvpn)` UI and system tray widget natively control `openvpn3`.

---

## Architecture

```mermaid
flowchart TB
    subgraph KDE["KDE Plasma Desktop"]
        KCM["System Settings -> Connections<br/>(plasmanetworkmanagement_openvpnui.so)"]
        Tray["KDE System Tray Networks Applet"]
        Browser["Web Browser & KDE Notifications<br/>(WebAuth / SSO)"]
    end

    subgraph NM["NetworkManager (System Bus)"]
        NMDaemon["NetworkManager Daemon"]
        Plugin["nm-openvpn3-service<br/>(NM.VpnServicePlugin)"]
    end

    subgraph Sync["User Session Service"]
        Watcher["openvpn3-kde-sync.service<br/>(KdeOpenVpn3SyncDaemon)"]
        CLI["openvpn3-kde-cli / openvpn3 CLI"]
    end

    subgraph O3["OpenVPN 3 Linux (D-Bus)"]
        CfgMgr["net.openvpn.v3.configuration"]
        SessMgr["net.openvpn.v3.sessions"]
        NetCfg["net.openvpn.v3.netcfg (tun0)"]
    end

    KCM <--> NMDaemon
    Tray <--> NMDaemon
    NMDaemon <--> Plugin
    Plugin <--> CfgMgr
    Plugin <--> SessMgr
    Watcher <--> NMDaemon
    Watcher <--> CfgMgr
    Watcher <--> SessMgr
    Watcher --> Browser
    CLI <--> CfgMgr
    CLI <--> SessMgr
    SessMgr --> NetCfg
```

---

## Key Features

- **Native KDE Connections & System Tray Control**:
  - Existing `openvpn3` profiles (`openvpn3 configs-list`) automatically appear under **VPN** in KDE System Settings -> Connections and the KDE Networks tray widget.
  - Clicking **Connect** or **Disconnect** in KDE starts or stops the OpenVPN 3 session.
  - Already-running sessions started from the terminal (`openvpn3 session-start --config <name>`) automatically attach and display as **Connected** in KDE without dropping packets or resetting routes.
- **Bidirectional Deletion & Profile Sync**:
  - Deleting a VPN connection in **KDE System Settings -> Connections** automatically disconnects any active session and removes the configuration from `openvpn3` (`openvpn3 configs-list`).
  - Removing a profile via `openvpn3 config-remove` removes it from KDE Connection Manager.
- **CloudConnexa / OpenVPN Cloud & X.509 CN Recovery**:
  - Preserves `setenv USERNAME "..."`, `socket-flags TCP_NODELAY`, `push-peer-info`, `persist-tun`, and inline certificates (`<ca>`, `<cert>`, `<key>`, `<tls-auth>`, `<tls-crypt>`).
  - If a profile is imported via KDE's built-in *"Import VPN connection..."* button (which strips `setenv USERNAME`), `openvpn3-kde-nm` automatically recovers the CloudVPN username from the X.509 `Subject CN` of the extracted `cert.pem`.
- **Automatic WebAuth / SSO Browser Login**:
  - Listens for `StatusMajor.SESSION / StatusMinor.SESS_AUTH_URL` D-Bus signals, opens the authentication URL in your default browser, and displays a native KDE desktop notification.
- **Live Traffic Statistics & Logs CLI (`openvpn3-kde-cli`)**:
  - Inspect integration status, watch real-time session byte/packet counters, and tail session logs.

---

## Prerequisites

- **OS**: Linux with **KDE Plasma** (`plasma-nm`) and **NetworkManager** (`network-manager-openvpn`)
- **OpenVPN 3 Linux**: `openvpn3-client` (including `python3` `openvpn3` D-Bus bindings)
- **Python 3.10+** with system packages:
  - `python3-gi` (`gi.repository.NM`, `GLib`, `Gio`)
  - `python3-dbus` (`dbus-python`)
  - `openssl` CLI

---

## Installation

Clone the repository and run the installer script:

```bash
git clone git@github.com:gerosalesc/openvpn3-kde-nm.git
cd openvpn3-kde-nm
./install.sh
```

### What `install.sh` Does
1. Symlinks `openvpn3-kde-cli` and `openvpn3-kde-sync` into `~/.local/bin/`.
2. Installs and enables the user systemd service `~/.config/systemd/user/openvpn3-kde-sync.service`.
3. Synchronizes your existing `openvpn3` configurations (`openvpn3 configs-list`) with KDE Connection Manager.
4. Uses PolicyKit (`pkexec`) or `sudo` to:
   - Install the Python package to `/usr/local/lib/openvpn3-kde-nm` (so NetworkManager can execute it inside its `ProtectHome=read-only` systemd sandbox).
   - Back up `/usr/libexec/nm-openvpn-service` and `/usr/lib/NetworkManager/VPN/nm-openvpn-service.name` to `/var/backups/`.
   - Register `/usr/local/libexec/nm-openvpn3-service` as NetworkManager's OpenVPN service plugin.

---

## Usage

### Managing Connections in KDE
- **Connect / Disconnect**: Open the **KDE Networks** system tray icon (or **System Settings -> Connections**), find your OpenVPN 3 profile, and click **Connect** or **Disconnect**.
- **Remove a Profile**: Right-click or select the profile in **System Settings -> Connections** and click **Delete**. It will be removed from both KDE Connection Manager and `openvpn3`.

### CLI Reference (`openvpn3-kde-cli`)

```bash
# Check integration status, synced profiles, and active sessions
openvpn3-kde-cli status

# Watch live traffic statistics (refreshes every second)
openvpn3-kde-cli stats --watch

# Show statistics for a specific profile
openvpn3-kde-cli stats --config ClassMiami4

# Follow live service and session logs
openvpn3-kde-cli logs -f

# Manually trigger profile synchronization
openvpn3-kde-cli sync

# Import a new .ovpn file into both OpenVPN 3 and KDE Connections
openvpn3-kde-cli import /path/to/profile.ovpn --name MyVPN

# Display version
openvpn3-kde-cli --version
```

---

## Uninstallation

To remove the integration and restore the original OpenVPN 2 NetworkManager service:

```bash
./uninstall.sh
```

---

## Project Layout

```text
openvpn3-kde-nm/
├── bin/
│   ├── nm-openvpn3-service          # NetworkManager VPN service executable
│   ├── openvpn3-kde-cli             # CLI management utility
│   └── openvpn3-kde-sync            # User session sync & WebAuth daemon
├── openvpn3_kde_nm/
│   ├── __init__.py                  # Package version metadata
│   ├── cli.py                       # CLI implementation (status, stats, logs, sync, import)
│   ├── config_sync.py               # Bidirectional OpenVPN 3 <-> NetworkManager sync
│   ├── nm_vpn_service.py            # NM.VpnServicePlugin D-Bus implementation
│   ├── ovpn_parser.py               # .ovpn parser, PEM extractor, X.509 CN recovery
│   └── session_watcher.py           # Session event watcher & WebAuth handler
├── systemd/
│   └── openvpn3-kde-sync.service    # systemd user unit
├── CHANGELOG.md
├── LICENSE
├── README.md
├── install.sh
├── pyproject.toml
└── uninstall.sh
```

---

## License

Released under the [MIT License](LICENSE).
