#!/bin/bash
# Shared NetworkManager plugin hook and systemd user service helper for
# system packages (.deb on Debian/Ubuntu, .pkg.tar.zst on Arch Linux).
set -euo pipefail

HOOK_CONF="${OPENVPN3_KDE_NM_HOOK_CONF:-/usr/share/openvpn3-kde-nm/hook.conf}"
if [ -f "$HOOK_CONF" ]; then
    # shellcheck source=/dev/null
    . "$HOOK_CONF"
fi

PACKAGE_NAME="${PACKAGE_NAME:-openvpn3-kde-nm}"
HOOK_MODE="${HOOK_MODE:-backup}"
TARGET_SERVICE="${TARGET_SERVICE:-/usr/libexec/nm-openvpn3-service}"
DEFAULT_ORIG_PROG="${DEFAULT_ORIG_PROG:-/usr/libexec/nm-openvpn-service}"
NM_VPN_DIR="${NM_VPN_DIR:-/usr/lib/NetworkManager/VPN}"
ETC_NM_VPN_DIR="${ETC_NM_VPN_DIR:-/etc/NetworkManager/VPN}"
LEGACY_BACKUP_DIR="${LEGACY_BACKUP_DIR:-/var/backups}"
STATE_DIR="${STATE_DIR:-/var/lib/openvpn3-kde-nm}"
BACKUP_DIR="${BACKUP_DIR:-${STATE_DIR}/backups}"
SLEEP_INTERVAL="${OPENVPN3_KDE_NM_INOTIFY_SLEEP:-0.2}"

NM_NAME_FILE="${NM_VPN_DIR}/nm-openvpn-service.name"
ETC_NM_NAME_FILE="${ETC_NM_VPN_DIR}/nm-openvpn-service.name"
ORIG_PROG_RECORD="${STATE_DIR}/orig-program-path"
SERVICE_NAME="openvpn3-kde-sync.service"

stop_process_exact() {
    local name="$1"
    if command -v pkill >/dev/null 2>&1; then
        pkill -x "$name" 2>/dev/null || true
    elif command -v killall >/dev/null 2>&1; then
        killall "$name" 2>/dev/null || true
    fi
}

stop_process_pattern() {
    local pattern="$1"
    if command -v pkill >/dev/null 2>&1; then
        pkill -f "$pattern" 2>/dev/null || true
    fi
}

parse_program_from_name() {
    local name_file="$1"
    local prog=""
    if [ -f "$name_file" ]; then
        prog="$(sed -n 's/^[[:space:]]*program[[:space:]]*=[[:space:]]*//p' "$name_file" | head -n 1 | tr -d '\r')"
    fi
    if [ -n "$prog" ] && [[ "$prog" != *nm-openvpn3-service* ]]; then
        printf '%s\n' "$prog"
    else
        printf '%s\n' "$DEFAULT_ORIG_PROG"
    fi
}

cleanup_stale_etc_override() {
    if [ -f "$ETC_NM_NAME_FILE" ] && LC_ALL=C grep -aq "nm-openvpn3-service" "$ETC_NM_NAME_FILE" 2>/dev/null; then
        rm -f "$ETC_NM_NAME_FILE"
    fi
}

migrate_legacy_install_sh_backups() {
    local legacy_name_bak="${LEGACY_BACKUP_DIR}/nm-openvpn-service.name.openvpn2.bak"
    local legacy_prog_bak="${LEGACY_BACKUP_DIR}/nm-openvpn-service.openvpn2.bak"

    if [ -f "$legacy_name_bak" ] && [ -f "$NM_NAME_FILE" ]; then
        if LC_ALL=C grep -aq "nm-openvpn3-service" "$NM_NAME_FILE" 2>/dev/null; then
            mv -f "$legacy_name_bak" "$NM_NAME_FILE"
            chmod 0644 "$NM_NAME_FILE"
            if [ "$(id -u)" -eq 0 ]; then
                chown root:root "$NM_NAME_FILE"
            fi
        fi
    fi

    if [ -f "$legacy_prog_bak" ] && [ -f "$DEFAULT_ORIG_PROG" ] && [ ! -L "$DEFAULT_ORIG_PROG" ]; then
        if LC_ALL=C grep -aq "openvpn3_kde_nm" "$DEFAULT_ORIG_PROG" 2>/dev/null; then
            mv -f "$legacy_prog_bak" "$DEFAULT_ORIG_PROG"
            chmod 0755 "$DEFAULT_ORIG_PROG"
            if [ "$(id -u)" -eq 0 ]; then
                chown root:root "$DEFAULT_ORIG_PROG"
            fi
        fi
    fi
}

write_overridden_name_file() {
    local orig_file="$1"
    local dest_file="$2"
    local tmp_file

    mkdir -p "$(dirname "$dest_file")"
    tmp_file="$(mktemp "${dest_file}.tmp.XXXXXX")"

    if [ -f "$orig_file" ]; then
        if grep -q '^[[:space:]]*program[[:space:]]*=' "$orig_file"; then
            sed "s|^[[:space:]]*program[[:space:]]*=.*|program=${TARGET_SERVICE}|" "$orig_file" > "$tmp_file"
        else
            cat "$orig_file" > "$tmp_file"
            printf '\nprogram=%s\n' "$TARGET_SERVICE" >> "$tmp_file"
        fi
    else
        cat > "$tmp_file" <<EOF
[VPN Connection]
name=openvpn
service=org.freedesktop.NetworkManager.openvpn
program=${TARGET_SERVICE}
supports-multiple-connections=true

[libnm]
plugin=libnm-vpn-plugin-openvpn.so
EOF
    fi

    chmod 0644 "$tmp_file"
    if [ "$(id -u)" -eq 0 ]; then
        chown root:root "$tmp_file"
    fi

    # Remove and pause briefly so NetworkManager's inotify detects the update
    rm -f "$dest_file"
    if [ "$SLEEP_INTERVAL" != "0" ]; then
        sleep "$SLEEP_INTERVAL"
    fi
    mv -f "$tmp_file" "$dest_file"
}

apply_divert() {
    local divert_name="${NM_NAME_FILE}.openvpn2"
    local pkg_owner orig_prog divert_prog prog_owner

    mkdir -p "$STATE_DIR"

    pkg_owner="$(dpkg-divert --listpackage "$NM_NAME_FILE" 2>/dev/null || true)"
    if [ "$pkg_owner" != "$PACKAGE_NAME" ]; then
        migrate_legacy_install_sh_backups
        if [ -e "$NM_NAME_FILE" ] || [ -L "$NM_NAME_FILE" ]; then
            dpkg-divert --package "$PACKAGE_NAME" --add --rename \
                --divert "$divert_name" "$NM_NAME_FILE" >/dev/null
        fi
    fi

    orig_prog="$(parse_program_from_name "$divert_name")"
    printf '%s\n' "$orig_prog" > "$ORIG_PROG_RECORD"

    if [ "$orig_prog" != "$TARGET_SERVICE" ]; then
        divert_prog="${orig_prog}.openvpn2"
        prog_owner="$(dpkg-divert --listpackage "$orig_prog" 2>/dev/null || true)"
        if [ "$prog_owner" != "$PACKAGE_NAME" ]; then
            if [ -e "$orig_prog" ] || [ -L "$orig_prog" ]; then
                dpkg-divert --package "$PACKAGE_NAME" --add --rename \
                    --divert "$divert_prog" "$orig_prog" >/dev/null
            fi
        fi
    fi

    write_overridden_name_file "$divert_name" "$NM_NAME_FILE"

    if [ "$orig_prog" != "$TARGET_SERVICE" ]; then
        prog_owner="$(dpkg-divert --listpackage "$orig_prog" 2>/dev/null || true)"
        if [ "$prog_owner" = "$PACKAGE_NAME" ]; then
            ln -sfn "$TARGET_SERVICE" "$orig_prog"
        fi
    fi

    cleanup_stale_etc_override
    stop_process_exact "nm-openvpn-service"
}

restore_divert() {
    local divert_name="${NM_NAME_FILE}.openvpn2"
    local orig_prog pkg_owner prog_owner

    if [ -f "$ORIG_PROG_RECORD" ]; then
        orig_prog="$(cat "$ORIG_PROG_RECORD")"
    else
        orig_prog="$(parse_program_from_name "$divert_name")"
    fi

    pkg_owner="$(dpkg-divert --listpackage "$NM_NAME_FILE" 2>/dev/null || true)"
    if [ "$pkg_owner" = "$PACKAGE_NAME" ]; then
        rm -f "$NM_NAME_FILE"
        if [ "$SLEEP_INTERVAL" != "0" ]; then
            sleep "$SLEEP_INTERVAL"
        fi
        dpkg-divert --package "$PACKAGE_NAME" --remove --rename \
            --divert "$divert_name" "$NM_NAME_FILE" >/dev/null
    fi

    if [ -n "$orig_prog" ] && [ "$orig_prog" != "$TARGET_SERVICE" ]; then
        prog_owner="$(dpkg-divert --listpackage "$orig_prog" 2>/dev/null || true)"
        if [ "$prog_owner" = "$PACKAGE_NAME" ]; then
            rm -f "$orig_prog"
            dpkg-divert --package "$PACKAGE_NAME" --remove --rename \
                --divert "${orig_prog}.openvpn2" "$orig_prog" >/dev/null
        fi
    fi

    cleanup_stale_etc_override
    stop_process_pattern "/nm-openvpn3-service"
    rm -f "$ORIG_PROG_RECORD"
    rmdir "$STATE_DIR" 2>/dev/null || true
}

apply_backup() {
    local backup_name="${BACKUP_DIR}/nm-openvpn-service.name.openvpn2"
    local backup_prog="${BACKUP_DIR}/nm-openvpn-service.openvpn2"
    local orig_prog

    mkdir -p "$BACKUP_DIR"

    if [ ! -f "$backup_name" ]; then
        migrate_legacy_install_sh_backups
    fi

    # Refresh backup on initial install or whenever upstream networkmanager-openvpn upgrades
    if [ -f "$NM_NAME_FILE" ] && ! LC_ALL=C grep -aq "nm-openvpn3-service" "$NM_NAME_FILE" 2>/dev/null; then
        cp -a "$NM_NAME_FILE" "$backup_name"
    fi

    orig_prog="$(parse_program_from_name "$backup_name")"
    printf '%s\n' "$orig_prog" > "$ORIG_PROG_RECORD"

    if [ "$orig_prog" != "$TARGET_SERVICE" ] && [ -e "$orig_prog" ] && [ ! -L "$orig_prog" ]; then
        if ! LC_ALL=C grep -aq "openvpn3_kde_nm" "$orig_prog" 2>/dev/null; then
            cp -a "$orig_prog" "$backup_prog"
        fi
    fi

    write_overridden_name_file "$backup_name" "$NM_NAME_FILE"

    if [ "$orig_prog" != "$TARGET_SERVICE" ] && [ -f "$backup_prog" ]; then
        ln -sfn "$TARGET_SERVICE" "$orig_prog"
    fi

    cleanup_stale_etc_override
    stop_process_exact "nm-openvpn-service"
}

restore_backup() {
    local backup_name="${BACKUP_DIR}/nm-openvpn-service.name.openvpn2"
    local backup_prog="${BACKUP_DIR}/nm-openvpn-service.openvpn2"
    local orig_prog

    if [ -f "$ORIG_PROG_RECORD" ]; then
        orig_prog="$(cat "$ORIG_PROG_RECORD")"
    else
        orig_prog="$(parse_program_from_name "$backup_name")"
    fi

    if [ -n "$orig_prog" ] && [ -f "$backup_prog" ] && [ -d "$(dirname "$orig_prog")" ]; then
        if [ ! -e "$orig_prog" ] || [ -L "$orig_prog" ] || LC_ALL=C grep -aq "openvpn3_kde_nm" "$orig_prog" 2>/dev/null; then
            rm -f "$orig_prog"
            mv -f "$backup_prog" "$orig_prog"
            chmod 0755 "$orig_prog"
            if [ "$(id -u)" -eq 0 ]; then
                chown root:root "$orig_prog"
            fi
        fi
    fi

    if [ -f "$backup_name" ] && [ -d "$(dirname "$NM_NAME_FILE")" ]; then
        rm -f "$NM_NAME_FILE"
        if [ "$SLEEP_INTERVAL" != "0" ]; then
            sleep "$SLEEP_INTERVAL"
        fi
        mv -f "$backup_name" "$NM_NAME_FILE"
        chmod 0644 "$NM_NAME_FILE"
        if [ "$(id -u)" -eq 0 ]; then
            chown root:root "$NM_NAME_FILE"
        fi
    fi

    cleanup_stale_etc_override
    stop_process_pattern "/nm-openvpn3-service"
    rm -f "$ORIG_PROG_RECORD"
    rmdir "$BACKUP_DIR" 2>/dev/null || true
    rmdir "$STATE_DIR" 2>/dev/null || true
}

run_user_systemctl() {
    local action="$1"
    local runuser_bin=""

    if command -v runuser >/dev/null 2>&1; then
        runuser_bin="$(command -v runuser)"
    elif [ -x /usr/sbin/runuser ]; then
        runuser_bin="/usr/sbin/runuser"
    fi

    if [ ! -d /run/systemd/system ] || ! command -v loginctl >/dev/null 2>&1 || ! command -v systemctl >/dev/null 2>&1; then
        return 0
    fi
    if [ -z "$runuser_bin" ]; then
        return 0
    fi

    loginctl list-users --no-legend 2>/dev/null | while read -r uid username _rest; do
        [ -n "${uid:-}" ] && [ -n "${username:-}" ] || continue
        case "$uid" in
            ''|*[!0-9]*) continue ;;
        esac
        [ "$uid" -ge 1000 ] || continue
        [ -S "/run/user/${uid}/bus" ] || continue

        if [ "$action" = "restart" ]; then
            "$runuser_bin" -u "$username" -- env \
                XDG_RUNTIME_DIR="/run/user/${uid}" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
                systemctl --user daemon-reload >/dev/null 2>&1 || true
            "$runuser_bin" -u "$username" -- env \
                XDG_RUNTIME_DIR="/run/user/${uid}" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
                systemctl --user restart "$SERVICE_NAME" >/dev/null 2>&1 || true
        elif [ "$action" = "stop" ]; then
            "$runuser_bin" -u "$username" -- env \
                XDG_RUNTIME_DIR="/run/user/${uid}" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
                systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
        fi
    done
}

enable_user_service() {
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --global enable "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    run_user_systemctl restart
}

disable_user_service() {
    run_user_systemctl stop
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --global disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
}

case "${1:-}" in
    apply)
        if [ "$HOOK_MODE" = "divert" ]; then
            apply_divert
        else
            apply_backup
        fi
        ;;
    restore)
        if [ "$HOOK_MODE" = "divert" ]; then
            restore_divert
        else
            restore_backup
        fi
        ;;
    enable-user-service)
        enable_user_service
        ;;
    disable-user-service)
        disable_user_service
        ;;
    *)
        echo "Usage: $0 {apply|restore|enable-user-service|disable-user-service}" >&2
        exit 2
        ;;
esac
