"""NetworkManager VPN Service Plugin (`org.freedesktop.NetworkManager.openvpn`) backed by OpenVPN 3."""

import argparse
import datetime
import json
import os
import pwd
import re
import socket
import struct
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import dbus
from dbus.mainloop.glib import DBusGMainLoop
import gi
import openvpn3
from openvpn3.constants import StatusMajor, StatusMinor

gi.require_version("NM", "1.0")
from gi.repository import GLib, NM  # noqa: E402

from openvpn3_kde_nm.config_sync import (  # noqa: E402
    claim_webauth_url,
    ensure_openvpn3_config,
    open_user_dbus_system_bus,
)
from openvpn3_kde_nm.ovpn_parser import reconstruct_ovpn_from_nm_vpn_data  # noqa: E402


LOG_FILE = Path("/tmp/openvpn3-kde-nm.log")
ROOT_LOG_FILE = Path("/run/nm-openvpn3-service.log")


def log_msg(msg: str) -> None:
    """Append a timestamped log entry to log file and stderr."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{ts}] [nm-openvpn3-service] {msg}\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    target_log = ROOT_LOG_FILE if os.getuid() == 0 else LOG_FILE
    try:
        with target_log.open("a", encoding="utf-8") as f:
            f.write(line)
        if os.getuid() == 0:
            os.chmod(target_log, 0o644)
    except Exception:
        pass


def ip_to_u32(ip_str: str) -> int:
    """Convert IPv4 string to NetworkManager uint32 (in_addr_t in host uint32)."""
    return struct.unpack("=I", socket.inet_aton(ip_str))[0]


def resolve_host_ipv4(host: str) -> Optional[str]:
    """Resolve a hostname or IP string to an IPv4 address string."""
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
            if infos:
                return str(infos[0][4][0])
        except Exception:
            pass
    return None


def get_tun_network_details(dev_name: str) -> Dict[str, object]:
    """Read IPv4 address, prefix, MTU, internal gateway, and DNS from a live tun interface."""
    details: Dict[str, object] = {
        "address": None,
        "prefix": 24,
        "mtu": 1500,
        "internal_gateway": None,
        "dns": [],
        "domains": [],
    }

    # 1. ip -j addr show dev <dev_name>
    try:
        proc = subprocess.run(
            ["ip", "-j", "addr", "show", "dev", dev_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            if data:
                iface = data[0]
                details["mtu"] = int(iface.get("mtu", 1500))
                for addr in iface.get("addr_info", []):
                    if addr.get("family") == "inet" and addr.get("local"):
                        details["address"] = str(addr["local"])
                        details["prefix"] = int(addr.get("prefixlen", 24))
                        break
    except Exception as exc:
        log_msg(f"Error reading ip addr for {dev_name}: {exc}")

    # 2. ip -j route show dev <dev_name>
    try:
        proc = subprocess.run(
            ["ip", "-j", "route", "show", "dev", dev_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            routes = json.loads(proc.stdout)
            for r in routes:
                gw = r.get("gateway")
                if gw and resolve_host_ipv4(str(gw)):
                    details["internal_gateway"] = str(gw)
                    break
    except Exception as exc:
        log_msg(f"Error reading ip route for {dev_name}: {exc}")

    # 3. resolvectl dns <dev_name>
    try:
        proc = subprocess.run(
            ["resolvectl", "dns", dev_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0 and ":" in proc.stdout:
            rhs = proc.stdout.split(":", 1)[1].strip()
            dns_list = []
            for tok in rhs.split():
                if resolve_host_ipv4(tok):
                    dns_list.append(tok)
            details["dns"] = dns_list
    except Exception:
        pass

    return details


def _open_user_session_bus(target_uid: int = 1000) -> dbus.bus.BusConnection:
    """Connect to the desktop user's session D-Bus (/run/user/<uid>/bus)."""
    pw = pwd.getpwuid(target_uid)
    dbus_addr = f"unix:path=/run/user/{target_uid}/bus"
    if os.getuid() == 0 and target_uid != 0:
        orig_euid = os.geteuid()
        orig_egid = os.getegid()
        try:
            os.setegid(pw.pw_gid)
            os.seteuid(pw.pw_uid)
            return dbus.bus.BusConnection(dbus_addr)
        finally:
            os.seteuid(orig_euid)
            os.setegid(orig_egid)
    return dbus.bus.BusConnection(dbus_addr)


def open_user_browser(url: str, target_uid: int = 1000) -> None:
    """
    Open a WebAuth URL in the desktop user's browser and send a KDE notification.
    Uses the user's session D-Bus and systemd --user / xdg-desktop-portal so that
    the browser runs outside NetworkManager.service's ProtectHome=read-only sandbox.
    """
    if not claim_webauth_url(url, target_uid=target_uid):
        log_msg(f"WebAuth URL already claimed by user sync daemon; skipping duplicate open: {url}")
        return

    log_msg(f"WebAuth required! Opening URL for UID {target_uid}: {url}")
    try:
        pw = pwd.getpwuid(target_uid)
        runtime_dir = f"/run/user/{target_uid}"
        dbus_addr = f"unix:path={runtime_dir}/bus"

        # 1. Send KDE notification directly via user's session D-Bus
        sbus = None
        try:
            sbus = _open_user_session_bus(target_uid)
            notif_obj = sbus.get_object(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications"
            )
            notify = notif_obj.get_dbus_method("Notify", "org.freedesktop.Notifications")
            notify(
                "OpenVPN 3 (KDE Connection Manager)",
                0,
                "network-vpn",
                "OpenVPN 3 Web Authentication Required",
                f"Opening browser to complete authentication:\n{url}",
                [],
                {},
                8000,
            )
        except Exception as n_exc:
            log_msg(f"Session D-Bus notification note: {n_exc}")

        # 2. Launch browser via systemd --user (escapes NetworkManager's ProtectHome=read-only namespace)
        env = {
            "HOME": pw.pw_dir,
            "USER": pw.pw_name,
            "LOGNAME": pw.pw_name,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
            "XDG_RUNTIME_DIR": runtime_dir,
            "DBUS_SESSION_BUS_ADDRESS": dbus_addr,
        }
        xauth = Path(pw.pw_dir) / ".Xauthority"
        if xauth.is_file():
            env["XAUTHORITY"] = str(xauth)

        run_kwargs = {}
        if os.getuid() == 0 and target_uid != 0:
            run_kwargs["user"] = pw.pw_uid
            run_kwargs["group"] = pw.pw_gid

        proc = subprocess.run(
            ["systemd-run", "--user", "--collect", "-p", "KillMode=none", "xdg-open", url],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            **run_kwargs,
        )
        if proc.returncode == 0:
            log_msg(f"Launched WebAuth browser via systemd --user: {proc.stderr.strip()}")
            return

        log_msg(f"systemd-run --user returned {proc.returncode} ({proc.stderr.strip()}); trying portal OpenURI")
        if sbus is None:
            sbus = _open_user_session_bus(target_uid)
        portal_obj = sbus.get_object(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
        )
        open_uri = portal_obj.get_dbus_method("OpenURI", "org.freedesktop.portal.OpenURI")
        open_uri("", url, dbus.Dictionary({}, signature="sv"))
        log_msg("Launched WebAuth browser via org.freedesktop.portal.OpenURI")
    except Exception as exc:
        log_msg(f"Failed to launch browser for WebAuth: {exc}")


class OpenVpn3NmPlugin(NM.VpnServicePlugin):
    """NetworkManager VPN Service Plugin bridging KDE Connection Manager to OpenVPN 3."""

    def __init__(self, service_name: str, watch_peer: bool = True, target_uid: int = 1000):
        super().__init__(service_name=service_name, watch_peer=watch_peer)
        self.target_uid = target_uid
        self._user_bus: Optional[dbus.SystemBus] = None
        self._sessmgr: Optional[openvpn3.SessionManager] = None
        self._cfgmgr: Optional[openvpn3.ConfigurationManager] = None
        self._session: Optional[openvpn3.Session] = None
        self._session_path: Optional[str] = None
        self._profile_name: Optional[str] = None
        self._vpn_data: Dict[str, str] = {}
        self._connected_reported = False
        self._disconnecting = False
        self._poll_timer_id: Optional[int] = None

    def _ensure_dbus(self) -> None:
        if self._user_bus is None:
            DBusGMainLoop(set_as_default=True)
            self._user_bus = open_user_dbus_system_bus(self.target_uid)
            self._sessmgr = openvpn3.SessionManager(self._user_bus)
            self._cfgmgr = openvpn3.ConfigurationManager(self._user_bus)

    def do_connect(self, connection: NM.Connection) -> bool:
        return self._initiate_connection(connection)

    def do_connect_interactive(self, connection: NM.Connection, details: GLib.Variant) -> bool:
        return self._initiate_connection(connection)

    def _initiate_connection(self, connection: NM.Connection) -> bool:
        conn_id = connection.get_id()
        s_vpn = connection.get_setting_vpn()
        vpn_data: Dict[str, str] = {}
        if s_vpn:
            for k in s_vpn.get_data_keys():
                val = s_vpn.get_data_item(k)
                if val is not None:
                    vpn_data[k] = val

        self._vpn_data = vpn_data
        self._profile_name = vpn_data.get("openvpn3-profile") or conn_id
        self._connected_reported = False
        self._disconnecting = False

        log_msg(f"Connect requested from KDE/NetworkManager for '{conn_id}' (profile='{self._profile_name}')")
        GLib.idle_add(self._async_start_openvpn3, conn_id, vpn_data)
        return True

    def _find_existing_session(self, profile_name: str) -> Optional[openvpn3.Session]:
        """Find an already-running OpenVPN 3 session matching profile_name."""
        assert self._sessmgr is not None
        try:
            paths = self._sessmgr.LookupConfigName(profile_name)
            for p in paths:
                sess = self._sessmgr.Retrieve(p)
                st = sess.GetStatus()
                if st["major"] == StatusMajor.CONNECTION and st["minor"] in (
                    StatusMinor.CONN_CONNECTED,
                    StatusMinor.CONN_CONNECTING,
                    StatusMinor.CONN_RECONNECTING,
                ):
                    return sess
        except Exception as exc:
            log_msg(f"LookupConfigName('{profile_name}') note: {exc}")

        try:
            for sess in self._sessmgr.FetchAvailableSessions():
                cname = str(sess.GetProperty("config_name"))
                if cname == profile_name:
                    return sess
        except Exception as exc:
            log_msg(f"FetchAvailableSessions note: {exc}")

        return None

    def _async_start_openvpn3(self, conn_id: str, vpn_data: Dict[str, str]) -> bool:
        profile_name = self._profile_name or conn_id
        try:
            self._ensure_dbus()
            assert self._sessmgr is not None
            assert self._cfgmgr is not None

            # 1. Check if session is already running (e.g. started via `openvpn3 session-start`)
            existing_sess = self._find_existing_session(profile_name)
            if existing_sess is not None:
                self._session = existing_sess
                self._session_path = str(existing_sess.GetPath())
                log_msg(f"Attaching to existing OpenVPN 3 session {self._session_path} for '{profile_name}'")
                self._subscribe_session_callbacks(existing_sess)
                st = existing_sess.GetStatus()
                if st["major"] == StatusMajor.CONNECTION and st["minor"] == StatusMinor.CONN_CONNECTED:
                    GLib.timeout_add(100, self._report_connected_to_nm)
                    return False
                self._start_status_poll()
                return False

            # 2. Ensure configuration exists in OpenVPN 3
            cfg_paths = self._cfgmgr.LookupConfigName(profile_name)
            if not cfg_paths:
                log_msg(f"Config '{profile_name}' not found in openvpn3; reconstructing from NM settings")
                ovpn_text = reconstruct_ovpn_from_nm_vpn_data(conn_id, vpn_data, uid=self.target_uid)
                cfg_path = ensure_openvpn3_config(profile_name, ovpn_text, target_uid=self.target_uid)
                cfg = self._cfgmgr.Retrieve(cfg_path)
            else:
                cfg = self._cfgmgr.Retrieve(cfg_paths[0])
                try:
                    cfg.AccessGrant(0)
                    cfg.SetOwnershipTransfer(True)
                except Exception:
                    pass

            # 3. Create and start new OpenVPN 3 session
            log_msg(f"Creating new OpenVPN 3 tunnel for config '{profile_name}' ({cfg.GetPath()})")
            sess = self._sessmgr.NewTunnel(cfg)
            self._session = sess
            self._session_path = str(sess.GetPath())
            log_msg(f"Created OpenVPN 3 session: {self._session_path}")

            self._subscribe_session_callbacks(sess)
            sess.Ready()
            sess.Connect()
            log_msg(f"Called Connect() on session {self._session_path}")
            self._start_status_poll()

        except Exception as exc:
            log_msg(f"Error starting OpenVPN 3 session for '{profile_name}': {exc}\n{traceback.format_exc()}")
            self.failure(NM.VpnPluginFailure.CONNECT_FAILED)

        return False

    def _subscribe_session_callbacks(self, sess: openvpn3.Session) -> None:
        try:
            sess.StatusChangeCallback(self._on_status_change)
        except Exception as exc:
            log_msg(f"Note subscribing StatusChangeCallback: {exc}")
        try:
            sess.LogCallback(self._on_log)
        except Exception as exc:
            log_msg(f"Note subscribing LogCallback: {exc}")

    def _start_status_poll(self) -> None:
        if self._poll_timer_id is None:
            self._poll_timer_id = GLib.timeout_add(1000, self._poll_session_status)

    def _stop_status_poll(self) -> None:
        if self._poll_timer_id is not None:
            GLib.source_remove(self._poll_timer_id)
            self._poll_timer_id = None

    def _poll_session_status(self) -> bool:
        """Periodic check in case a D-Bus StatusChange signal was missed or session closed via CLI."""
        if self._disconnecting or self._session is None:
            self._poll_timer_id = None
            return False

        try:
            st = self._session.GetStatus()
            maj = st["major"]
            min_st = st["minor"]
            msg = st["message"]

            if maj == StatusMajor.CONNECTION and min_st == StatusMinor.CONN_CONNECTED:
                if not self._connected_reported:
                    self._report_connected_to_nm()
            elif (maj == StatusMajor.CONNECTION and min_st in (
                StatusMinor.CONN_DISCONNECTED,
                StatusMinor.CONN_FAILED,
                StatusMinor.CONN_AUTH_FAILED,
            )) or (maj == StatusMajor.SESSION and min_st == StatusMinor.PROC_STOPPED):
                log_msg(f"Session {self._session_path} ended ({maj.name}, {min_st.name}: {msg})")
                self._poll_timer_id = None
                self.disconnect()
                return False
        except Exception as exc:
            # Session object removed from D-Bus (e.g. `openvpn3 session-manage --disconnect` run in CLI)
            log_msg(f"Session {self._session_path} no longer present on D-Bus ({exc}); notifying NM")
            self._poll_timer_id = None
            self.disconnect()
            return False

        return True

    def _on_log(self, group: int, category: int, message: str) -> None:
        for line in message.splitlines():
            if line.strip():
                log_msg(f"[O3-LOG:{ self._profile_name }] {line.strip()}")

    def _on_status_change(self, major: int, minor: int, message: str) -> None:
        try:
            maj = StatusMajor(major)
            min_st = StatusMinor(minor)
        except Exception:
            log_msg(f"StatusChange raw ({major}, {minor}): {message}")
            return

        log_msg(f"StatusChange ({maj.name}, {min_st.name}): {message}")

        if maj == StatusMajor.SESSION and min_st == StatusMinor.SESS_AUTH_URL:
            open_user_browser(message, target_uid=self.target_uid)
            return

        if maj == StatusMajor.CONNECTION:
            if min_st == StatusMinor.CONN_CONNECTED:
                if not self._connected_reported:
                    GLib.timeout_add(250, self._report_connected_to_nm)
            elif min_st in (StatusMinor.CONN_AUTH_FAILED, StatusMinor.CONN_FAILED):
                self._stop_status_poll()
                self.failure(NM.VpnPluginFailure.CONNECT_FAILED)
            elif min_st == StatusMinor.CONN_DISCONNECTED:
                self._stop_status_poll()
                if not self._disconnecting:
                    self.disconnect()
        elif maj == StatusMajor.SESSION and min_st == StatusMinor.PROC_STOPPED:
            self._stop_status_poll()
            if not self._disconnecting:
                self.disconnect()

    def _report_connected_to_nm(self) -> bool:
        if self._connected_reported or self._session is None:
            return False

        try:
            dev_name = str(self._session.GetProperty("device_name"))
            if not dev_name:
                return True  # Wait until device_name is populated
        except Exception as exc:
            log_msg(f"Waiting for device_name property: {exc}")
            return True

        net_details = get_tun_network_details(dev_name)
        local_ip = net_details.get("address")
        if not local_ip:
            log_msg(f"Waiting for IPv4 address on {dev_name}...")
            return True

        # Determine external gateway IP
        ext_gw_ip = None
        try:
            conn_to = self._session.GetProperty("connected_to")
            if conn_to and len(conn_to) >= 2:
                ext_gw_ip = resolve_host_ipv4(str(conn_to[1]))
        except Exception:
            pass

        if not ext_gw_ip:
            remote_str = self._vpn_data.get("remote", "")
            first_remote = remote_str.split(",")[0].strip().split(":")[0] if remote_str else ""
            ext_gw_ip = resolve_host_ipv4(first_remote) or str(local_ip)

        prefix = int(net_details.get("prefix", 24))  # type: ignore
        mtu = int(net_details.get("mtu", 1500))  # type: ignore
        int_gw = net_details.get("internal_gateway")
        dns_list: List[str] = net_details.get("dns", [])  # type: ignore

        config_dict: Dict[str, GLib.Variant] = {
            NM.VPN_PLUGIN_CONFIG_TUNDEV: GLib.Variant("s", dev_name),
            NM.VPN_PLUGIN_CONFIG_EXT_GATEWAY: GLib.Variant("u", ip_to_u32(ext_gw_ip)),
            NM.VPN_PLUGIN_CONFIG_MTU: GLib.Variant("u", mtu),
            NM.VPN_PLUGIN_CONFIG_HAS_IP4: GLib.Variant("b", True),
            NM.VPN_PLUGIN_CONFIG_HAS_IP6: GLib.Variant("b", False),
            NM.VPN_PLUGIN_CAN_PERSIST: GLib.Variant("b", True),
        }

        ip4_dict: Dict[str, GLib.Variant] = {
            NM.VPN_PLUGIN_IP4_CONFIG_ADDRESS: GLib.Variant("u", ip_to_u32(str(local_ip))),
            NM.VPN_PLUGIN_IP4_CONFIG_PREFIX: GLib.Variant("u", prefix),
            NM.VPN_PLUGIN_IP4_CONFIG_PTP: GLib.Variant("u", ip_to_u32(str(int_gw or local_ip))),
            NM.VPN_PLUGIN_IP4_CONFIG_NEVER_DEFAULT: GLib.Variant("b", True),
            NM.VPN_PLUGIN_IP4_CONFIG_PRESERVE_ROUTES: GLib.Variant("b", True),
        }
        if int_gw:
            ip4_dict[NM.VPN_PLUGIN_IP4_CONFIG_INT_GATEWAY] = GLib.Variant("u", ip_to_u32(str(int_gw)))
        if dns_list:
            ip4_dict[NM.VPN_PLUGIN_IP4_CONFIG_DNS] = GLib.Variant(
                "au", [ip_to_u32(d) for d in dns_list]
            )

        log_msg(
            f"Reporting CONNECTED to NetworkManager: dev={dev_name}, ip={local_ip}/{prefix}, "
            f"ext_gw={ext_gw_ip}, int_gw={int_gw}, dns={dns_list}"
        )
        self._connected_reported = True
        self.set_config(GLib.Variant("a{sv}", config_dict))
        self.set_ip4_config(GLib.Variant("a{sv}", ip4_dict))
        return False

    def do_disconnect(self) -> bool:
        log_msg(f"Disconnect requested from KDE/NetworkManager for '{self._profile_name}' ({self._session_path})")
        self._disconnecting = True
        self._stop_status_poll()

        if self._session is not None:
            sess = self._session
            self._session = None
            try:
                sess.StatusChangeCallback(None)
            except Exception:
                pass
            try:
                sess.LogCallback(None)
            except Exception:
                pass
            try:
                sess.Disconnect()
                log_msg(f"Disconnected OpenVPN 3 session {self._session_path}")
            except Exception as exc:
                log_msg(f"Session already disconnected or closed: {exc}")

        self._connected_reported = False
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenVPN 3 NetworkManager VPN Service Plugin")
    parser.add_argument(
        "--bus-name",
        default="org.freedesktop.NetworkManager.openvpn",
        help="D-Bus service name for this instance",
    )
    parser.add_argument("--persist", action="store_true", help="Do not quit when VPN terminates")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    args, _ = parser.parse_known_args()

    log_msg(f"Starting OpenVpn3NmPlugin (bus_name={args.bus_name}, persist={args.persist}, uid={os.getuid()})")

    plugin = OpenVpn3NmPlugin(
        service_name=args.bus_name,
        watch_peer=not args.persist,
        target_uid=1000,
    )
    plugin.init(None)

    loop = GLib.MainLoop()
    plugin.connect("quit", lambda _p: loop.quit())

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    log_msg(f"Exiting OpenVpn3NmPlugin (bus_name={args.bus_name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
