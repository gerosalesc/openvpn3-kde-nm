#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)

chmod +x "$PROJECT_DIR/bin/nm-openvpn3-service"
chmod +x "$PROJECT_DIR/bin/openvpn3-kde-sync"
chmod +x "$PROJECT_DIR/bin/openvpn3-kde-cli"

echo "[1/4] Installing user CLI symlinks and systemd user service..."
mkdir -p "$USER_HOME/.local/bin"
ln -sf "$PROJECT_DIR/bin/openvpn3-kde-cli" "$USER_HOME/.local/bin/openvpn3-kde-cli"
ln -sf "$PROJECT_DIR/bin/openvpn3-kde-sync" "$USER_HOME/.local/bin/openvpn3-kde-sync"

mkdir -p "$USER_HOME/.config/systemd/user"
cp "$PROJECT_DIR/systemd/openvpn3-kde-sync.service" "$USER_HOME/.config/systemd/user/openvpn3-kde-sync.service"

echo "[2/4] Synchronizing existing OpenVPN 3 profiles with KDE Connections..."
"$PROJECT_DIR/bin/openvpn3-kde-cli" sync

echo "[3/4] Installing NetworkManager OpenVPN 3 service plugin hook..."
install_root_files() {
    mkdir -p /var/backups
    mkdir -p /usr/local/libexec
    mkdir -p /usr/local/lib/openvpn3-kde-nm

    rm -rf /usr/local/lib/openvpn3-kde-nm/openvpn3_kde_nm
    cp -r "$PROJECT_DIR/openvpn3_kde_nm" /usr/local/lib/openvpn3-kde-nm/openvpn3_kde_nm
    chown -R root:root /usr/local/lib/openvpn3-kde-nm
    chmod -R a+rX /usr/local/lib/openvpn3-kde-nm

    install -o root -g root -m 0755 "$PROJECT_DIR/bin/nm-openvpn3-service" /usr/local/libexec/nm-openvpn3-service

    if [ -f /usr/libexec/nm-openvpn-service ] && [ ! -f /var/backups/nm-openvpn-service.openvpn2.bak ]; then
        if ! grep -q "openvpn3_kde_nm" /usr/libexec/nm-openvpn-service 2>/dev/null; then
            cp -a /usr/libexec/nm-openvpn-service /var/backups/nm-openvpn-service.openvpn2.bak
        fi
    fi
    install -o root -g root -m 0755 "$PROJECT_DIR/bin/nm-openvpn3-service" /usr/libexec/nm-openvpn-service

    NM_VPN_FILE="/usr/lib/NetworkManager/VPN/nm-openvpn-service.name"
    if [ -f "${NM_VPN_FILE}.openvpn2.bak" ]; then
        mv -f "${NM_VPN_FILE}.openvpn2.bak" /var/backups/nm-openvpn-service.name.openvpn2.bak
    elif [ -f "$NM_VPN_FILE" ] && [ ! -f /var/backups/nm-openvpn-service.name.openvpn2.bak ]; then
        cp -a "$NM_VPN_FILE" /var/backups/nm-openvpn-service.name.openvpn2.bak
    fi

    rm -f "$NM_VPN_FILE" /etc/NetworkManager/VPN/nm-openvpn-service.name
    sleep 0.2

    cat > "$NM_VPN_FILE" <<'EOF'
[VPN Connection]
name=openvpn
service=org.freedesktop.NetworkManager.openvpn
program=/usr/local/libexec/nm-openvpn3-service
supports-multiple-connections=true

[libnm]
plugin=libnm-vpn-plugin-openvpn.so

[GNOME]
auth-dialog=/usr/libexec/nm-openvpn-auth-dialog
properties=libnm-openvpn-properties
supports-external-ui-mode=true
supports-hints=true
EOF
    chmod 0644 "$NM_VPN_FILE"
    chown root:root "$NM_VPN_FILE"

    mkdir -p /etc/NetworkManager/VPN
    cp -f "$NM_VPN_FILE" /etc/NetworkManager/VPN/nm-openvpn-service.name
    chmod 0644 /etc/NetworkManager/VPN/nm-openvpn-service.name
    chown root:root /etc/NetworkManager/VPN/nm-openvpn-service.name

    killall nm-openvpn-service 2>/dev/null || true
}

if [ "$(id -u)" -eq 0 ]; then
    install_root_files
else
    TMP_SCRIPT=$(mktemp /tmp/install-nm-openvpn3.XXXXXX.sh)
    cat > "$TMP_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$PROJECT_DIR"
$(declare -f install_root_files)
install_root_files
EOF
    chmod 0755 "$TMP_SCRIPT"
    if command -v pkexec >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
        echo "Requesting privilege elevation via KDE Polkit dialog (pkexec)..."
        pkexec "$TMP_SCRIPT" || {
            echo "Note: pkexec cancelled or failed. You can run: sudo $PROJECT_DIR/install.sh"
        }
    else
        sudo "$TMP_SCRIPT"
    fi
    rm -f "$TMP_SCRIPT"
fi

if [ "$(id -u)" -ne 0 ]; then
    systemctl --user daemon-reload
    systemctl --user enable --now openvpn3-kde-sync.service
    systemctl --user restart openvpn3-kde-sync.service
    sleep 2
fi

echo "[4/4] Verifying status..."
"$PROJECT_DIR/bin/openvpn3-kde-cli" status
