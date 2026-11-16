#!/usr/bin/env bash
set -euo pipefail

NM_VPN_FILE="/usr/lib/NetworkManager/VPN/nm-openvpn-service.name"

restore_root_files() {
    if [ -f /var/backups/nm-openvpn-service.openvpn2.bak ]; then
        mv -f /var/backups/nm-openvpn-service.openvpn2.bak /usr/libexec/nm-openvpn-service
        chmod 0755 /usr/libexec/nm-openvpn-service
        chown root:root /usr/libexec/nm-openvpn-service
    fi
    if [ -f /var/backups/nm-openvpn-service.name.openvpn2.bak ]; then
        rm -f "$NM_VPN_FILE"
        sleep 0.2
        mv -f /var/backups/nm-openvpn-service.name.openvpn2.bak "$NM_VPN_FILE"
        chmod 0644 "$NM_VPN_FILE"
        chown root:root "$NM_VPN_FILE"
    fi
    rm -f /etc/NetworkManager/VPN/nm-openvpn-service.name
    rm -f /usr/local/libexec/nm-openvpn3-service
    rm -rf /usr/local/lib/openvpn3-kde-nm
    killall nm-openvpn3-service 2>/dev/null || true
}

systemctl --user disable --now openvpn3-kde-sync.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/openvpn3-kde-sync.service"
systemctl --user daemon-reload 2>/dev/null || true

if [ "$(id -u)" -eq 0 ]; then
    restore_root_files
else
    sudo bash -c "$(declare -f restore_root_files); restore_root_files"
fi

echo "Uninstalled OpenVPN 3 NetworkManager plugin and restored OpenVPN 2 service."
