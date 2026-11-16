"""CLI tool for managing OpenVPN 3 <-> KDE Connection Manager integration, stats, and logs."""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import dbus
import gi
import openvpn3

gi.require_version("NM", "1.0")
from gi.repository import NM  # noqa: E402

from openvpn3_kde_nm import __version__  # noqa: E402
from openvpn3_kde_nm.config_sync import (  # noqa: E402
    import_ovpn_file,
    open_user_dbus_system_bus,
    sync_all_profiles,
)
from openvpn3_kde_nm.nm_vpn_service import LOG_FILE, ROOT_LOG_FILE  # noqa: E402
from openvpn3_kde_nm.session_watcher import is_nm_openvpn3_plugin_installed  # noqa: E402


def format_bytes(num_bytes: int) -> str:
    """Format byte count into human-readable units."""
    val = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(val) < 1024.0 or unit == "TiB":
            return f"{val:.2f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.2f} TiB"


def cmd_status(_args: argparse.Namespace) -> int:
    bus = open_user_dbus_system_bus()
    cfgmgr = openvpn3.ConfigurationManager(bus)
    sessmgr = openvpn3.SessionManager(bus)
    nm_client = NM.Client.new(None)

    plugin_ok = is_nm_openvpn3_plugin_installed()
    print("=" * 78)
    print(" OpenVPN 3 <-> KDE Connection Manager Integration Status")
    print("=" * 78)
    print(
        f" NetworkManager OpenVPN 3 Plugin : "
        f"{'INSTALLED (Native KDE Connect/Disconnect Active)' if plugin_ok else 'USER-SPACE MODE (Run ./install.sh for root NM hook)'}"
    )

    # Check user systemd service
    svc_proc = subprocess.run(
        ["systemctl", "--user", "is-active", "openvpn3-kde-sync.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    svc_state = svc_proc.stdout.strip() or "inactive"
    print(f" Background Sync & WebAuth Daemon: {svc_state}")
    print("-" * 78)

    # Map NM VPN connections
    nm_vpns = {}
    active_nm = set()
    for ac in nm_client.get_active_connections():
        if ac.get_connection_type() in ("vpn", "tun"):
            active_nm.add(ac.get_id())

    for conn in nm_client.get_connections():
        s_con = conn.get_setting_connection()
        if s_con and s_con.get_connection_type() == "vpn":
            s_vpn = conn.get_setting_vpn()
            prof = (s_vpn.get_data_item("openvpn3-profile") if s_vpn else None) or conn.get_id()
            nm_vpns[prof] = {
                "id": conn.get_id(),
                "uuid": conn.get_uuid(),
                "active": conn.get_id() in active_nm,
            }

    print(" Configured Profiles (OpenVPN 3 & KDE Connections):")
    configs = cfgmgr.FetchAvailableConfigs()
    if not configs:
        print("   (none)")
    for cfg in configs:
        name = cfg.GetConfigName()
        nm_info = nm_vpns.get(name)
        nm_str = (
            f"Synced in KDE Connections (UUID: {nm_info['uuid']})"
            if nm_info
            else "Not yet synced (run `openvpn3-kde-cli sync`)"
        )
        print(f"   * {name:<18} -> {nm_str}")

    print("-" * 78)
    print(" Active OpenVPN 3 Sessions:")
    sessions = sessmgr.FetchAvailableSessions()
    if not sessions:
        print("   (no active sessions)")
    for sess in sessions:
        cname = str(sess.GetProperty("config_name"))
        dev = str(sess.GetProperty("device_name"))
        st = sess.GetStatus()
        conn_to = sess.GetProperty("connected_to")
        endpoint = f"{conn_to[0]}:{conn_to[1]}:{conn_to[2]}" if conn_to and len(conn_to) >= 3 else "N/A"
        stats = sess.GetStatistics()
        rx = format_bytes(int(stats.get("BYTES_IN", 0)))
        tx = format_bytes(int(stats.get("BYTES_OUT", 0)))
        print(f"   * {cname:<18} | Dev: {dev:<6} | Server: {endpoint:<22}")
        print(f"     Status: {st['major'].name} / {st['minor'].name} {st['message']}")
        print(f"     Traffic: RX {rx} ({stats.get('PACKETS_IN', 0)} pkts) | TX {tx} ({stats.get('PACKETS_OUT', 0)} pkts)")
        print(f"     Path: {sess.GetPath()}")
    print("=" * 78)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    bus = open_user_dbus_system_bus()
    sessmgr = openvpn3.SessionManager(bus)

    def print_once() -> bool:
        sessions = sessmgr.FetchAvailableSessions()
        if args.config:
            sessions = [
                s for s in sessions if str(s.GetProperty("config_name")) == args.config
            ]
        if not sessions:
            print("No matching active OpenVPN 3 sessions found.")
            return False

        if args.watch:
            os.system("clear")
        for sess in sessions:
            cname = str(sess.GetProperty("config_name"))
            dev = str(sess.GetProperty("device_name"))
            stats = sess.GetStatistics()
            print(f"=== OpenVPN 3 Live Statistics: {cname} ({dev}) ===")
            for k, v in stats.items():
                if "BYTES" in str(k):
                    print(f"  {str(k):<20}: {int(v):>14,}  ({format_bytes(int(v))})")
                else:
                    print(f"  {str(k):<20}: {int(v):>14,}")
            print()
        return True

    if not args.watch:
        print_once()
        return 0

    try:
        while print_once():
            print("Press Ctrl+C to stop watching...")
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    log_files = [str(p) for p in (ROOT_LOG_FILE, LOG_FILE) if p.is_file()]
    if not log_files:
        print(f"No log files found yet ({ROOT_LOG_FILE}, {LOG_FILE}).")
        return 0
    if args.follow:
        try:
            subprocess.run(["tail", "-n", str(args.lines), "-f", *log_files], check=False)
        except KeyboardInterrupt:
            pass
        return 0
    proc = subprocess.run(
        ["tail", "-n", str(args.lines), *log_files],
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(proc.stdout)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    name, cfg_path, nm_uuid = import_ovpn_file(
        args.file,
        profile_name=args.name,
        replace_existing=args.replace,
    )
    print(f"Imported '{name}' successfully:")
    print(f"  OpenVPN 3 Config Path     : {cfg_path}")
    print(f"  KDE NetworkManager UUID   : {nm_uuid}")
    return 0


def cmd_sync(_args: argparse.Namespace) -> int:
    results = sync_all_profiles()
    print(f"Synchronized {len(results)} profile(s) between OpenVPN 3 and KDE Connection Manager:")
    for item in results:
        if "error" in item:
            print(f"  [ERROR] {item['name']}: {item['error']}")
        else:
            print(
                f"  [OK] {item['name']:<18} ({item['direction']}) -> NM UUID: {item['nm_uuid']}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="openvpn3-kde-cli",
        description="Manage OpenVPN 3 integration with KDE Connection Manager (plasma-nm)",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show OpenVPN 3 and KDE Connection Manager integration status")

    p_stats = sub.add_parser("stats", help="Show live session statistics")
    p_stats.add_argument("-c", "--config", help="Filter by configuration profile name")
    p_stats.add_argument("-w", "--watch", action="store_true", help="Continuously refresh stats every second")

    p_logs = sub.add_parser("logs", help="View integration and OpenVPN 3 session logs")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow live log output")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="Number of lines to display")

    p_import = sub.add_parser("import", help="Import an .ovpn file into OpenVPN 3 and KDE Connections")
    p_import.add_argument("file", help="Path to the .ovpn file")
    p_import.add_argument("-n", "--name", help="Custom profile name in KDE Connections and OpenVPN 3")
    p_import.add_argument("--replace", action="store_true", default=True, help="Replace existing profile with same name")

    sub.add_parser("sync", help="Synchronize all OpenVPN 3 profiles and KDE Connections")

    args = parser.parse_args()
    if not args.command or args.command == "status":
        return cmd_status(args)
    if args.command == "stats":
        return cmd_stats(args)
    if args.command == "logs":
        return cmd_logs(args)
    if args.command == "import":
        return cmd_import(args)
    if args.command == "sync":
        return cmd_sync(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
