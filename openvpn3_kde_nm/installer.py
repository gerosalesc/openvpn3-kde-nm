"""Installer and uninstaller helpers for PyPI, pipx, and source-checkout setups."""

import importlib.resources
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from openvpn3_kde_nm.config_sync import sync_all_profiles


SERVICE_WRAPPER_TEMPLATE = '''#!/usr/bin/python3
"""Executable entry point for the NetworkManager OpenVPN 3 service plugin."""

import os
import sys

PKG_DIR = "/usr/local/lib/openvpn3-kde-nm"
if os.path.isdir(PKG_DIR) and PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

from openvpn3_kde_nm.nm_vpn_service import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
'''

NM_VPN_NAME_CONTENT = """[VPN Connection]
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
"""


def _resolve_target_user() -> pwd.struct_passwd:
    """Resolve the desktop user even when running under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return pwd.getpwnam(sudo_user)
    uid = os.getuid()
    if uid == 0:
        return pwd.getpwuid(1000)
    return pwd.getpwuid(uid)


def _run_privileged_script(script_body: str, action_label: str) -> bool:
    """Execute a shell script as root via direct exec, pkexec, or sudo."""
    if os.geteuid() == 0:
        subprocess.run(["bash", "-c", script_body], check=True)
        return True

    with tempfile.NamedTemporaryFile("w", prefix="openvpn3-kde-nm-", suffix=".sh", delete=False) as tf:
        tf.write("#!/usr/bin/env bash\nset -euo pipefail\n")
        tf.write(script_body)
        tmp_path = Path(tf.name)

    try:
        os.chmod(tmp_path, 0o755)
        has_gui = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if has_gui and shutil.which("pkexec"):
            print(f"Requesting privilege elevation via KDE Polkit dialog ({action_label})...")
            proc = subprocess.run(["pkexec", str(tmp_path)], check=False)
            if proc.returncode == 0:
                return True
            print(
                f"Note: pkexec cancelled or failed (exit {proc.returncode}). "
                f"You can run: sudo {shlex.quote(sys.executable)} -m openvpn3_kde_nm.cli {action_label}"
            )
            return False

        proc = subprocess.run(["sudo", str(tmp_path)], check=False)
        return proc.returncode == 0
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _load_service_template() -> str:
    """Read the bundled systemd user service template."""
    try:
        pkg_file = importlib.resources.files("openvpn3_kde_nm").joinpath(
            "data/openvpn3-kde-sync.service"
        )
        return pkg_file.read_text(encoding="utf-8")
    except Exception:
        fallback = Path(__file__).resolve().parent / "data" / "openvpn3-kde-sync.service"
        return fallback.read_text(encoding="utf-8")


def _resolve_sync_exec(user_home: Path) -> str:
    """Determine the ExecStart command for openvpn3-kde-sync."""
    repo_root = Path(__file__).resolve().parent.parent
    repo_sync = repo_root / "bin" / "openvpn3-kde-sync"
    local_bin = user_home / ".local" / "bin"
    local_sync = local_bin / "openvpn3-kde-sync"
    local_cli = local_bin / "openvpn3-kde-cli"
    repo_cli = repo_root / "bin" / "openvpn3-kde-cli"

    # If invoked from a git checkout, symlink bin/* into ~/.local/bin/
    if repo_sync.is_file() and repo_cli.is_file():
        local_bin.mkdir(parents=True, exist_ok=True)
        for src, dst in ((repo_cli, local_cli), (repo_sync, local_sync)):
            if dst.is_symlink() or not dst.exists():
                if dst.is_symlink():
                    dst.unlink()
                dst.symlink_to(src)
        return "%h/.local/bin/openvpn3-kde-sync"

    which_sync = shutil.which("openvpn3-kde-sync")
    if which_sync:
        return which_sync
    if local_sync.exists():
        return "%h/.local/bin/openvpn3-kde-sync"
    return f"{sys.executable} -m openvpn3_kde_nm.session_watcher"


def install_integration(skip_sync: bool = False) -> int:
    """Install systemd user daemon, sync OpenVPN 3 profiles, and hook NetworkManager."""
    pw = _resolve_target_user()
    user_home = Path(pw.pw_dir)
    pkg_dir = Path(__file__).resolve().parent

    print("[1/4] Configuring systemd user service...")
    exec_start = _resolve_sync_exec(user_home)
    service_text = _load_service_template().format(exec_start=exec_start)
    systemd_user_dir = user_home / ".config" / "systemd" / "user"
    systemd_user_dir.mkdir(parents=True, exist_ok=True)
    service_file = systemd_user_dir / "openvpn3-kde-sync.service"
    service_file.write_text(service_text, encoding="utf-8")
    if os.geteuid() == 0:
        os.chown(systemd_user_dir, pw.pw_uid, pw.pw_gid)
        os.chown(service_file, pw.pw_uid, pw.pw_gid)

    if not skip_sync:
        print("[2/4] Synchronizing existing OpenVPN 3 profiles with KDE Connections...")
        try:
            synced = sync_all_profiles(target_uid=pw.pw_uid)
            print(f"Synchronized {len(synced)} profile(s) with KDE Connection Manager.")
        except Exception as exc:
            print(f"Warning: profile sync failed ({exc}); continuing installation.")

    print("[3/4] Installing NetworkManager OpenVPN 3 service plugin hook...")
    with tempfile.TemporaryDirectory(prefix="openvpn3-kde-stage-") as stage_dir:
        os.chmod(stage_dir, 0o755)
        stage_path = Path(stage_dir)
        wrapper_stage = stage_path / "nm-openvpn3-service"
        wrapper_stage.write_text(SERVICE_WRAPPER_TEMPLATE, encoding="utf-8")
        os.chmod(wrapper_stage, 0o755)

        name_stage = stage_path / "nm-openvpn-service.name"
        name_stage.write_text(NM_VPN_NAME_CONTENT, encoding="utf-8")
        os.chmod(name_stage, 0o644)

        root_script = f"""
mkdir -p /var/backups /usr/local/libexec /usr/local/lib/openvpn3-kde-nm /etc/NetworkManager/VPN

rm -rf /usr/local/lib/openvpn3-kde-nm/openvpn3_kde_nm
cp -r {shlex.quote(str(pkg_dir))} /usr/local/lib/openvpn3-kde-nm/openvpn3_kde_nm
find /usr/local/lib/openvpn3-kde-nm -name '__pycache__' -type d -exec rm -rf {{}} +
chown -R root:root /usr/local/lib/openvpn3-kde-nm
chmod -R a+rX /usr/local/lib/openvpn3-kde-nm

install -o root -g root -m 0755 {shlex.quote(str(wrapper_stage))} /usr/local/libexec/nm-openvpn3-service

if [ -f /usr/libexec/nm-openvpn-service ] && [ ! -f /var/backups/nm-openvpn-service.openvpn2.bak ]; then
    if ! grep -q "openvpn3_kde_nm" /usr/libexec/nm-openvpn-service 2>/dev/null; then
        cp -a /usr/libexec/nm-openvpn-service /var/backups/nm-openvpn-service.openvpn2.bak
    fi
fi
install -o root -g root -m 0755 {shlex.quote(str(wrapper_stage))} /usr/libexec/nm-openvpn-service

NM_VPN_FILE="/usr/lib/NetworkManager/VPN/nm-openvpn-service.name"
if [ -f "${{NM_VPN_FILE}}.openvpn2.bak" ]; then
    mv -f "${{NM_VPN_FILE}}.openvpn2.bak" /var/backups/nm-openvpn-service.name.openvpn2.bak
elif [ -f "$NM_VPN_FILE" ] && [ ! -f /var/backups/nm-openvpn-service.name.openvpn2.bak ]; then
    cp -a "$NM_VPN_FILE" /var/backups/nm-openvpn-service.name.openvpn2.bak
fi

rm -f "$NM_VPN_FILE" /etc/NetworkManager/VPN/nm-openvpn-service.name
sleep 0.2

install -o root -g root -m 0644 {shlex.quote(str(name_stage))} "$NM_VPN_FILE"
install -o root -g root -m 0644 {shlex.quote(str(name_stage))} /etc/NetworkManager/VPN/nm-openvpn-service.name

killall nm-openvpn-service 2>/dev/null || true
"""
        _run_privileged_script(root_script, "install")

    if os.geteuid() != 0:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "openvpn3-kde-sync.service"],
            check=False,
        )
        subprocess.run(
            ["systemctl", "--user", "restart", "openvpn3-kde-sync.service"],
            check=False,
        )
        time.sleep(2)

    print("[4/4] Installation complete.")
    return 0


def uninstall_integration() -> int:
    """Disable user sync daemon and restore original OpenVPN 2 NetworkManager plugin."""
    pw = _resolve_target_user()
    user_home = Path(pw.pw_dir)

    if os.geteuid() != 0:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "openvpn3-kde-sync.service"],
            check=False,
            stderr=subprocess.DEVNULL,
        )
        service_file = user_home / ".config" / "systemd" / "user" / "openvpn3-kde-sync.service"
        if service_file.exists():
            service_file.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    root_script = """
NM_VPN_FILE="/usr/lib/NetworkManager/VPN/nm-openvpn-service.name"
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
"""
    ok = _run_privileged_script(root_script, "uninstall")
    if ok:
        print("Uninstalled OpenVPN 3 NetworkManager plugin and restored OpenVPN 2 service.")
        return 0
    return 1
