"""User-space daemon syncing OpenVPN 3 CLI sessions/configs with KDE Connection Manager & handling WebAuth."""

import datetime
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Dict, Optional, Set

import dbus
from dbus.mainloop.glib import DBusGMainLoop
import gi
import openvpn3
from openvpn3.constants import SessionManagerEventType, StatusMajor, StatusMinor

gi.require_version("NM", "1.0")
from gi.repository import GLib, NM  # noqa: E402

from openvpn3_kde_nm.config_sync import (  # noqa: E402
    remove_openvpn3_profile,
    sync_all_profiles,
)


LOG_FILE = Path("/tmp/openvpn3-kde-nm.log")
NM_SERVICE_NAME_FILE = Path("/usr/lib/NetworkManager/VPN/nm-openvpn-service.name")


def log_msg(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{ts}] [openvpn3-kde-sync] {msg}\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def is_nm_openvpn3_plugin_installed() -> bool:
    """Return True if NetworkManager's openvpn service points to nm-openvpn3-service."""
    try:
        if NM_SERVICE_NAME_FILE.is_file():
            text = NM_SERVICE_NAME_FILE.read_text(encoding="utf-8")
            return "nm-openvpn3-service" in text
    except Exception:
        pass
    return False


def send_kde_notification(title: str, body: str, icon: str = "network-vpn") -> None:
    """Send a native KDE desktop notification via org.freedesktop.Notifications."""
    try:
        sbus = dbus.SessionBus()
        notif_obj = sbus.get_object(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications"
        )
        notify = notif_obj.get_dbus_method("Notify", "org.freedesktop.Notifications")
        notify("OpenVPN 3 (KDE)", 0, icon, title, body, [], {}, 5000)
    except Exception as exc:
        log_msg(f"Notification fallback ({exc})")
        try:
            subprocess.Popen(
                ["notify-send", "-a", "OpenVPN 3 (KDE)", "-i", icon, title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


class MonitoredSession:
    """Tracks a single OpenVPN 3 session for WebAuth, status changes, and KDE NM sync."""

    def __init__(self, watcher: "KdeOpenVpn3SyncDaemon", session: openvpn3.Session):
        self.watcher = watcher
        self.session = session
        self.path = str(session.GetPath())
        self.config_name = "Unknown"
        self._last_auth_url: Optional[str] = None
        self._connected_notified = False

        try:
            self.config_name = str(session.GetProperty("config_name"))
        except Exception:
            pass

        try:
            self.session.StatusChangeCallback(self._on_status_change)
        except Exception as exc:
            log_msg(f"Could not attach StatusChangeCallback to {self.path}: {exc}")

        try:
            self.session.LogCallback(self._on_log)
        except Exception:
            pass

        self.check_initial_status()

    def check_initial_status(self) -> None:
        try:
            st = self.session.GetStatus()
            self._handle_status(st["major"], st["minor"], st["message"], initial=True)
        except Exception as exc:
            log_msg(f"Initial status check failed for {self.path}: {exc}")

    def _on_log(self, group: int, category: int, message: str) -> None:
        for line in message.splitlines():
            if line.strip():
                log_msg(f"[SESSION-LOG:{self.config_name}] {line.strip()}")

    def _on_status_change(self, major: int, minor: int, message: str) -> None:
        try:
            maj = StatusMajor(major)
            min_st = StatusMinor(minor)
        except Exception:
            return
        self._handle_status(maj, min_st, message, initial=False)

    def _handle_status(
        self, maj: StatusMajor, min_st: StatusMinor, message: str, initial: bool = False
    ) -> None:
        log_msg(
            f"Session '{self.config_name}' ({self.path}) status: {maj.name}/{min_st.name} msg='{message}'"
        )

        if maj == StatusMajor.SESSION and min_st == StatusMinor.SESS_AUTH_URL:
            if message and message != self._last_auth_url:
                self._last_auth_url = message
                log_msg(f"Opening WebAuth URL for '{self.config_name}': {message}")
                send_kde_notification(
                    f"OpenVPN 3 Authentication: {self.config_name}",
                    f"Opening browser for SSO login:\n{message}",
                )
                try:
                    webbrowser.open_new(message)
                except Exception:
                    subprocess.Popen(
                        ["xdg-open", message],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
        elif maj == StatusMajor.CONNECTION and min_st == StatusMinor.CONN_CONNECTED:
            if not self._connected_notified:
                self._connected_notified = True
                if not initial:
                    send_kde_notification(
                        "OpenVPN 3 Connected",
                        f"Profile '{self.config_name}' is now connected.",
                    )
            self.watcher.ensure_nm_connection_active(self.config_name)
        elif maj == StatusMajor.CONNECTION and min_st == StatusMinor.CONN_DISCONNECTED:
            if not initial:
                send_kde_notification(
                    "OpenVPN 3 Disconnected",
                    f"Profile '{self.config_name}' was disconnected.",
                )

    def stop(self) -> None:
        try:
            self.session.StatusChangeCallback(None)
        except Exception:
            pass
        try:
            self.session.LogCallback(None)
        except Exception:
            pass


class KdeOpenVpn3SyncDaemon:
    """Main daemon synchronizing OpenVPN 3 and KDE Connection Manager."""

    def __init__(self) -> None:
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.sessmgr = openvpn3.SessionManager(self.bus)
        self.cfgmgr = openvpn3.ConfigurationManager(self.bus)
        self.nm_client = NM.Client.new(None)
        self.sessions: Dict[str, MonitoredSession] = {}
        self._activating_nm: Set[str] = set()
        self._recent_deactivations: Dict[str, float] = {}

        # Sync existing profiles on startup
        try:
            synced = sync_all_profiles()
            log_msg(f"Initial profile sync complete ({len(synced)} profiles)")
        except Exception as exc:
            log_msg(f"Initial profile sync error: {exc}")

        # Subscribe to OpenVPN 3 SessionManager events
        self.sessmgr.SessionManagerCallback(self._on_session_mgr_event)

        # Register existing OpenVPN 3 sessions
        for sess in self.sessmgr.FetchAvailableSessions():
            self._register_session(str(sess.GetPath()))

        # Watch NetworkManager connections (additions, removals, activations)
        self.nm_client.connect("connection-added", self._on_nm_connection_added)
        self.nm_client.connect("connection-removed", self._on_nm_connection_removed)
        self.nm_client.connect("active-connection-added", self._on_nm_active_connection_added)
        self.nm_client.connect("active-connection-removed", self._on_nm_active_connection_removed)

        # Periodic reconciliation every 10 seconds (detects CLI imports/removals)
        GLib.timeout_add_seconds(10, self._periodic_sync)

    def _register_session(self, path: str) -> None:
        if path in self.sessions:
            return
        try:
            sess = self.sessmgr.Retrieve(path)
            self.sessions[path] = MonitoredSession(self, sess)
        except Exception as exc:
            log_msg(f"Failed to register session {path}: {exc}")

    def _on_session_mgr_event(self, event: object) -> None:
        path = str(event.GetPath())  # type: ignore[attr-defined]
        ev_type = event.GetType()  # type: ignore[attr-defined]
        owner = event.GetOwner()  # type: ignore[attr-defined]
        log_msg(f"SessionManagerEvent: {ev_type.name} path={path} owner={owner}")

        if ev_type == SessionManagerEventType.SESS_CREATED:
            GLib.timeout_add(300, lambda: (self._register_session(path), False)[1])
        elif ev_type == SessionManagerEventType.SESS_DESTROYED:
            mon = self.sessions.pop(path, None)
            if mon:
                mon.stop()

    def ensure_nm_connection_active(self, profile_name: str) -> None:
        """
        Ensure that a connected OpenVPN 3 session is also shown as 'Connected'
        in KDE Connection Manager (NetworkManager).
        """
        if not is_nm_openvpn3_plugin_installed():
            return
        if profile_name in self._activating_nm:
            return
        import time
        if time.time() - self._recent_deactivations.get(profile_name, 0.0) < 6.0:
            return

        for ac in self.nm_client.get_active_connections():
            if ac.get_id() == profile_name and ac.get_connection_type() == "vpn":
                return

        for conn in self.nm_client.get_connections():
            s_con = conn.get_setting_connection()
            if not s_con or s_con.get_connection_type() != "vpn":
                continue
            if conn.get_id() == profile_name:
                log_msg(f"Activating KDE NetworkManager VPN state for live session '{profile_name}'")
                self._activating_nm.add(profile_name)

                def on_activated(cli_obj, res):
                    self._activating_nm.discard(profile_name)
                    try:
                        cli_obj.activate_connection_finish(res)
                        log_msg(f"KDE NetworkManager VPN '{profile_name}' is now active")
                    except Exception as exc:
                        log_msg(f"Note activating NM VPN '{profile_name}': {exc}")

                self.nm_client.activate_connection_async(conn, None, None, None, on_activated)
                break

    def _on_nm_connection_added(
        self, _client: NM.Client, connection: NM.RemoteConnection
    ) -> None:
        s_con = connection.get_setting_connection()
        if s_con and s_con.get_connection_type() == "vpn":
            log_msg(f"New NM VPN connection added in KDE ('{connection.get_id()}'); syncing to OpenVPN 3")
            GLib.timeout_add(500, self._run_sync_once)

    def _on_nm_connection_removed(
        self, _client: NM.Client, connection: NM.RemoteConnection
    ) -> None:
        """When user deletes a VPN connection in KDE Connection Manager, remove it from OpenVPN 3."""
        conn_id = connection.get_id()
        s_con = connection.get_setting_connection()
        is_vpn = (s_con and s_con.get_connection_type() == "vpn") or not s_con
        if is_vpn and conn_id:
            log_msg(
                f"VPN connection '{conn_id}' removed from KDE Connection Manager; "
                f"removing from OpenVPN 3 as well"
            )
            try:
                removed = remove_openvpn3_profile(conn_id)
                log_msg(f"OpenVPN 3 removal for '{conn_id}': removed={removed}")
            except Exception as exc:
                log_msg(f"Error removing '{conn_id}' from OpenVPN 3: {exc}")
        GLib.timeout_add(200, self._run_sync_once)

    def _on_nm_active_connection_removed(
        self, _client: NM.Client, active_conn: NM.ActiveConnection
    ) -> None:
        if active_conn.get_connection_type() == "vpn":
            import time
            conn_id = active_conn.get_id()
            if conn_id:
                self._recent_deactivations[conn_id] = time.time()

    def _on_nm_active_connection_added(
        self, _client: NM.Client, active_conn: NM.ActiveConnection
    ) -> None:
        """
        Fallback if root nm-openvpn3-service is not active:
        When user clicks Connect in KDE Connection Manager, start the OpenVPN 3 session.
        """
        if is_nm_openvpn3_plugin_installed():
            return
        if active_conn.get_connection_type() != "vpn":
            return
        conn_id = active_conn.get_id()
        log_msg(f"KDE VPN activation detected (user-space mode) for '{conn_id}'; starting OpenVPN 3 session")
        try:
            existing = self.sessmgr.LookupConfigName(conn_id)
            if existing:
                return
            cfg_paths = self.cfgmgr.LookupConfigName(conn_id)
            if not cfg_paths:
                sync_all_profiles()
                cfg_paths = self.cfgmgr.LookupConfigName(conn_id)
            if cfg_paths:
                cfg = self.cfgmgr.Retrieve(cfg_paths[0])
                sess = self.sessmgr.NewTunnel(cfg)
                self._register_session(str(sess.GetPath()))
                sess.Ready()
                sess.Connect()
        except Exception as exc:
            log_msg(f"Failed to start user-space OpenVPN 3 session for '{conn_id}': {exc}")

    def _run_sync_once(self) -> bool:
        try:
            sync_all_profiles()
        except Exception as exc:
            log_msg(f"Sync error: {exc}")
        return False

    def _periodic_sync(self) -> bool:
        try:
            sync_all_profiles()
            for sess in self.sessmgr.FetchAvailableSessions():
                st = sess.GetStatus()
                if st["major"] == StatusMajor.CONNECTION and st["minor"] == StatusMinor.CONN_CONNECTED:
                    cname = str(sess.GetProperty("config_name"))
                    self.ensure_nm_connection_active(cname)
        except Exception:
            pass
        return True


def main() -> int:
    log_msg("Starting KdeOpenVpn3SyncDaemon")
    _daemon = KdeOpenVpn3SyncDaemon()
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    log_msg("Stopping KdeOpenVpn3SyncDaemon")
    return 0


if __name__ == "__main__":
    sys.exit(main())
