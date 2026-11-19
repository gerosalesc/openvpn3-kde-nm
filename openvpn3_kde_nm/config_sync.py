"""Synchronize OpenVPN 3 configurations with KDE Connection Manager (NetworkManager)."""

import hashlib
import json
import os
import pwd
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import dbus
from dbus.mainloop.glib import DBusGMainLoop
import gi
import openvpn3

gi.require_version("NM", "1.0")
from gi.repository import GLib, NM  # noqa: E402

from openvpn3_kde_nm.ovpn_parser import (  # noqa: E402
    build_nm_vpn_data,
    get_config_dir,
    infer_profile_name,
    parse_ovpn,
    reconstruct_ovpn_from_nm_vpn_data,
    remove_extracted_certs,
    save_raw_profile,
    write_extracted_certs,
)


def get_state_file(uid: Optional[int] = None) -> Path:
    """Return path to synced_profiles.json tracking synced profiles."""
    return get_config_dir(uid, create=True) / "synced_profiles.json"


def load_synced_state(uid: Optional[int] = None) -> Dict[str, Dict[str, str]]:
    """Load the dictionary of profiles previously synced between OpenVPN 3 and NM."""
    sf = get_state_file(uid)
    if sf.is_file():
        try:
            return json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_synced_state(state: Dict[str, Dict[str, str]], uid: Optional[int] = None) -> None:
    """Persist the synced profiles dictionary."""
    try:
        sf = get_state_file(uid)
        sf.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _wrap_bus_follow_owner(bus: dbus.SystemBus) -> dbus.SystemBus:
    """
    Wrap bus.get_object so well-known bus names default to follow_name_owner_changes=True.
    This prevents openvpn3.SessionManager and ConfigurationManager from binding permanently
    to a stale unique bus name (:1.xxx) when openvpn3-service-sessionmgr exits on idle.
    """
    orig_get_object = bus.get_object

    def get_object_follow(
        bus_name: str,
        object_path: str,
        introspect: bool = True,
        follow_name_owner_changes: bool = True,
        **kwargs,
    ):
        follow = follow_name_owner_changes and bool(bus_name) and not str(bus_name).startswith(":")
        return orig_get_object(
            bus_name,
            object_path,
            introspect=introspect,
            follow_name_owner_changes=follow,
            **kwargs,
        )

    bus.get_object = get_object_follow  # type: ignore[method-assign]
    return bus


def open_user_dbus_system_bus(target_uid: Optional[int] = None) -> dbus.SystemBus:
    """
    Open a private D-Bus SystemBus connection authenticated as the desktop user
    even when invoked from a root process (e.g., NetworkManager VPN plugin).
    """
    if target_uid is None:
        target_uid = int(os.environ.get("SUDO_UID", os.getuid()))
    if target_uid == 0:
        target_uid = 1000

    DBusGMainLoop(set_as_default=True)

    if os.getuid() == 0 and target_uid != 0:
        pw = pwd.getpwuid(target_uid)
        orig_euid = os.geteuid()
        orig_egid = os.getegid()
        try:
            os.setegid(pw.pw_gid)
            os.seteuid(pw.pw_uid)
            bus = dbus.SystemBus(private=True)
        finally:
            os.seteuid(orig_euid)
            os.setegid(orig_egid)
        return _wrap_bus_follow_owner(bus)
    return _wrap_bus_follow_owner(dbus.SystemBus(private=True))


def claim_webauth_url(url: str, target_uid: int = 1000) -> bool:
    """
    Atomically claim a WebAuth URL across nm-openvpn3-service and openvpn3-kde-sync
    so the browser is opened exactly once per single-use WebAuth challenge URL.
    """
    now = time.time()
    # Clean up old lock files (> 10 minutes old)
    for old_lock in Path("/tmp").glob("openvpn3-webauth-*.lock"):
        try:
            if now - old_lock.stat().st_mtime > 600:
                old_lock.unlink()
        except OSError:
            pass

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    lock_path = Path(f"/tmp/openvpn3-webauth-{target_uid}-{url_hash}.lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, f"{os.getpid()} {int(now)}\n".encode("utf-8"))
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def remove_openvpn3_profile(profile_name: str, target_uid: Optional[int] = None) -> bool:
    """
    Remove a configuration profile (and disconnect any active session for it)
    from OpenVPN 3 when deleted from KDE Connection Manager.
    """
    if target_uid is None:
        target_uid = int(os.environ.get("SUDO_UID", os.getuid()))
    if target_uid == 0:
        target_uid = 1000

    removed = False
    bus = open_user_dbus_system_bus(target_uid)

    # 1. Disconnect any active session for this profile
    try:
        sessmgr = openvpn3.SessionManager(bus)
        for sess in sessmgr.FetchAvailableSessions():
            try:
                if str(sess.GetProperty("config_name")) == profile_name:
                    sess.Disconnect()
            except Exception:
                pass
    except Exception:
        pass

    # 2. Remove config from OpenVPN 3 ConfigurationManager
    try:
        cfgmgr = openvpn3.ConfigurationManager(bus)
        paths = list(cfgmgr.LookupConfigName(profile_name))
        for p in paths:
            try:
                cfg = cfgmgr.Retrieve(p)
                cfg.Remove()
                removed = True
            except Exception:
                pass
        for cfg in cfgmgr.FetchAvailableConfigs():
            try:
                if cfg.GetConfigName() == profile_name:
                    cfg.Remove()
                    removed = True
            except Exception:
                pass
    except Exception:
        pass

    # 3. Clean up extracted certs and state
    remove_extracted_certs(profile_name, uid=target_uid)
    state = load_synced_state(uid=target_uid)
    if profile_name in state:
        state.pop(profile_name, None)
        save_synced_state(state, uid=target_uid)

    return removed


def delete_nm_vpn_connection(profile_name: str) -> bool:
    """Delete a VPN connection from KDE Connection Manager (NetworkManager) by ID."""
    client = NM.Client.new(None)
    target_conn = None
    for conn in client.get_connections():
        s_con = conn.get_setting_connection()
        if s_con and s_con.get_connection_type() == "vpn" and conn.get_id() == profile_name:
            target_conn = conn
            break

    if target_conn is None:
        return False

    loop = GLib.MainLoop()
    deleted = {"ok": False}

    def on_deleted(con_obj, res):
        try:
            con_obj.delete_finish(res)
            deleted["ok"] = True
        except Exception:
            pass
        loop.quit()

    target_conn.delete_async(None, on_deleted)
    loop.run()
    return deleted["ok"]


def ensure_openvpn3_config(
    profile_name: str,
    ovpn_content: str,
    target_uid: Optional[int] = None,
    replace_existing: bool = False,
) -> str:
    """
    Ensure a configuration exists in OpenVPN 3 ConfigurationManager, owned by target_uid,
    with root ACL access granted and session ownership transfer enabled.
    Returns the D-Bus object path of the configuration.
    """
    if target_uid is None:
        target_uid = int(os.environ.get("SUDO_UID", os.getuid()))
    if target_uid == 0:
        target_uid = 1000

    bus = open_user_dbus_system_bus(target_uid)
    cfgmgr = openvpn3.ConfigurationManager(bus)

    existing_paths = cfgmgr.LookupConfigName(profile_name)
    cfg = None
    if existing_paths:
        if replace_existing:
            for p in existing_paths[1:]:
                try:
                    cfgmgr.Retrieve(p).Remove()
                except Exception:
                    pass
            try:
                cfgmgr.Retrieve(existing_paths[0]).Remove()
                existing_paths = []
            except Exception:
                pass
        else:
            cfg = cfgmgr.Retrieve(existing_paths[0])

    if cfg is None:
        cfg = cfgmgr.Import(profile_name, ovpn_content, False, True)

    try:
        cfg.AccessGrant(0)
    except Exception:
        pass
    try:
        cfg.SetOwnershipTransfer(True)
    except Exception:
        pass

    save_raw_profile(profile_name, ovpn_content, uid=target_uid)
    return str(cfg.GetPath())


def upsert_nm_vpn_connection(
    profile_name: str,
    ovpn_content: str,
    target_uid: Optional[int] = None,
    force_update: bool = False,
) -> str:
    """
    Create (or update if force_update=True) a native NetworkManager OpenVPN connection
    so it appears in KDE System Settings -> Connections and the KDE Networks tray applet.
    Returns the NetworkManager connection UUID.
    """
    if target_uid is None:
        target_uid = int(os.environ.get("SUDO_UID", os.getuid()))
    if target_uid == 0:
        target_uid = 1000

    parsed = parse_ovpn(ovpn_content)
    inline_blocks: Dict[str, str] = parsed.get("inline", {})  # type: ignore
    cert_paths = write_extracted_certs(profile_name, inline_blocks, uid=target_uid)
    vpn_data = build_nm_vpn_data(profile_name, parsed, cert_paths)
    save_raw_profile(profile_name, ovpn_content, uid=target_uid)

    client = NM.Client.new(None)
    existing_conn = None
    for conn in client.get_connections():
        s_con = conn.get_setting_connection()
        if not s_con or s_con.get_connection_type() != "vpn":
            continue
        if conn.get_id() == profile_name:
            existing_conn = conn
            break

    if existing_conn is not None and not force_update:
        # Check if legacy custom keys exist or vpn.timeout is still 0 (60s default)
        s_vpn = existing_conn.get_setting_vpn()
        has_legacy = s_vpn and (
            s_vpn.get_data_item("openvpn3-profile") is not None
            or s_vpn.get_data_item("openvpn3-setenv-username") is not None
            or s_vpn.get_data_item("persist-tun") is not None
            or s_vpn.get_timeout() == 0
        )
        if not has_legacy:
            return existing_conn.get_uuid()

    loop = GLib.MainLoop()
    result_holder: Dict[str, Optional[str]] = {"uuid": None, "error": None}

    if existing_conn is not None:
        conn_uuid = existing_conn.get_uuid()
        existing_conn.remove_setting(NM.SettingVpn)
        s_vpn = NM.SettingVpn.new()
        s_vpn.set_property("service-type", "org.freedesktop.NetworkManager.openvpn")
        s_vpn.set_property("timeout", 180)
        for k, v in vpn_data.items():
            s_vpn.add_data_item(k, v)
        existing_conn.add_setting(s_vpn)

        s_ip4 = existing_conn.get_setting_ip4_config()
        if s_ip4 is None:
            s_ip4 = NM.SettingIP4Config.new()
            existing_conn.add_setting(s_ip4)
        s_ip4.set_property("method", "auto")
        s_ip4.set_property("never-default", True)

        s_ip6 = existing_conn.get_setting_ip6_config()
        if s_ip6 is None:
            s_ip6 = NM.SettingIP6Config.new()
            existing_conn.add_setting(s_ip6)
        s_ip6.set_property("method", "auto")
        s_ip6.set_property("never-default", True)

        def on_updated(con_obj, res):
            try:
                con_obj.commit_changes_finish(res)
                result_holder["uuid"] = conn_uuid
            except Exception as exc:
                result_holder["error"] = str(exc)
            loop.quit()

        existing_conn.commit_changes_async(True, None, on_updated)
        loop.run()
    else:
        new_conn = NM.SimpleConnection.new()
        conn_uuid = str(uuid.uuid4())

        s_con = NM.SettingConnection.new()
        s_con.set_property("id", profile_name)
        s_con.set_property("uuid", conn_uuid)
        s_con.set_property("type", "vpn")
        s_con.set_property("autoconnect", False)
        new_conn.add_setting(s_con)

        s_vpn = NM.SettingVpn.new()
        s_vpn.set_property("service-type", "org.freedesktop.NetworkManager.openvpn")
        s_vpn.set_property("timeout", 180)
        for k, v in vpn_data.items():
            s_vpn.add_data_item(k, v)
        new_conn.add_setting(s_vpn)

        s_ip4 = NM.SettingIP4Config.new()
        s_ip4.set_property("method", "auto")
        s_ip4.set_property("never-default", True)
        new_conn.add_setting(s_ip4)

        s_ip6 = NM.SettingIP6Config.new()
        s_ip6.set_property("method", "auto")
        s_ip6.set_property("never-default", True)
        new_conn.add_setting(s_ip6)

        def on_added(cli_obj, res):
            try:
                added = cli_obj.add_connection_finish(res)
                result_holder["uuid"] = added.get_uuid()
            except Exception as exc:
                result_holder["error"] = str(exc)
            loop.quit()

        client.add_connection_async(new_conn, True, None, on_added)
        loop.run()

    if result_holder["error"]:
        raise RuntimeError(f"Failed to upsert NM VPN connection '{profile_name}': {result_holder['error']}")
    return result_holder["uuid"] or ""


def import_ovpn_file(
    ovpn_path: str,
    profile_name: Optional[str] = None,
    target_uid: Optional[int] = None,
    replace_existing: bool = False,
) -> Tuple[str, str, str]:
    """
    Import an .ovpn file into both OpenVPN 3 and KDE Connection Manager (NetworkManager).
    Returns (profile_name, openvpn3_config_path, nm_connection_uuid).
    """
    path = Path(ovpn_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Profile file not found: {path}")

    ovpn_content = path.read_text(encoding="utf-8")
    if not profile_name:
        profile_name = infer_profile_name(ovpn_content, fallback_name=path.stem)

    cfg_path = ensure_openvpn3_config(
        profile_name=profile_name,
        ovpn_content=ovpn_content,
        target_uid=target_uid,
        replace_existing=replace_existing,
    )
    nm_uuid = upsert_nm_vpn_connection(
        profile_name=profile_name,
        ovpn_content=ovpn_content,
        target_uid=target_uid,
        force_update=True,
    )
    state = load_synced_state(uid=target_uid)
    state[profile_name] = {"nm_uuid": nm_uuid, "openvpn3_path": cfg_path}
    save_synced_state(state, uid=target_uid)
    return profile_name, cfg_path, nm_uuid


def sync_all_profiles(target_uid: Optional[int] = None) -> List[Dict[str, str]]:
    """
    Bidirectionally synchronize OpenVPN 3 and KDE Connection Manager:
    - If a previously synced connection was deleted from KDE Connection Manager -> remove from OpenVPN 3
    - If a previously synced connection was deleted from OpenVPN 3 -> remove from KDE Connection Manager
    - If a new config exists in OpenVPN 3 -> add to KDE Connection Manager
    - If a new VPN connection was added in KDE Connection Manager -> add to OpenVPN 3
    """
    if target_uid is None:
        target_uid = int(os.environ.get("SUDO_UID", os.getuid()))
    if target_uid == 0:
        target_uid = 1000

    state = load_synced_state(uid=target_uid)
    bus = open_user_dbus_system_bus(target_uid)
    cfgmgr = openvpn3.ConfigurationManager(bus)
    client = NM.Client.new(None)

    # Gather current OpenVPN 3 configs
    o3_configs: Dict[str, openvpn3.Configuration] = {}
    for cfg in cfgmgr.FetchAvailableConfigs():
        try:
            name = cfg.GetConfigName()
            if name and not name.startswith("("):
                o3_configs[name] = cfg
        except Exception:
            pass

    # Gather current KDE NetworkManager OpenVPN connections
    nm_conns: Dict[str, NM.RemoteConnection] = {}
    for conn in client.get_connections():
        s_con = conn.get_setting_connection()
        if not s_con or s_con.get_connection_type() != "vpn":
            continue
        s_vpn = conn.get_setting_vpn()
        if not s_vpn or s_vpn.get_service_type() != "org.freedesktop.NetworkManager.openvpn":
            continue
        nm_conns[conn.get_id()] = conn

    results: List[Dict[str, str]] = []

    # 1. Check deletions of previously synced profiles
    for prev_name in list(state.keys()):
        in_o3 = prev_name in o3_configs
        in_nm = prev_name in nm_conns

        if in_o3 and not in_nm:
            # User removed connection from KDE Connection Manager -> remove from OpenVPN 3!
            remove_openvpn3_profile(prev_name, target_uid=target_uid)
            o3_configs.pop(prev_name, None)
            state.pop(prev_name, None)
            results.append({
                "name": prev_name,
                "nm_uuid": "(deleted)",
                "direction": "removed in kde-nm -> removed from openvpn3",
            })
        elif in_nm and not in_o3:
            # User removed config via openvpn3 CLI -> remove from KDE Connection Manager!
            delete_nm_vpn_connection(prev_name)
            remove_extracted_certs(prev_name, uid=target_uid)
            nm_conns.pop(prev_name, None)
            state.pop(prev_name, None)
            results.append({
                "name": prev_name,
                "nm_uuid": "(deleted)",
                "direction": "removed in openvpn3 -> removed from kde-nm",
            })
        elif not in_o3 and not in_nm:
            state.pop(prev_name, None)

    # 2. Sync existing/new OpenVPN 3 configs -> KDE Connection Manager
    for name, cfg in o3_configs.items():
        try:
            try:
                cfg.AccessGrant(0)
                cfg.SetOwnershipTransfer(True)
            except Exception:
                pass
            ovpn_content = cfg.Fetch()
            nm_uuid = upsert_nm_vpn_connection(
                name, ovpn_content, target_uid=target_uid, force_update=False
            )
            state[name] = {"nm_uuid": nm_uuid, "openvpn3_path": str(cfg.GetPath())}
            results.append({
                "name": name,
                "openvpn3_path": str(cfg.GetPath()),
                "nm_uuid": nm_uuid,
                "direction": "openvpn3 <-> kde-nm",
            })
        except Exception as exc:
            results.append({"name": name, "error": str(exc)})

    # 3. Sync new KDE Connection Manager connections -> OpenVPN 3
    for conn_id, conn in nm_conns.items():
        if conn_id in o3_configs:
            continue
        try:
            s_vpn = conn.get_setting_vpn()
            vpn_data = {k: s_vpn.get_data_item(k) for k in s_vpn.get_data_keys()} if s_vpn else {}
            ovpn_content = reconstruct_ovpn_from_nm_vpn_data(conn_id, vpn_data, uid=target_uid)
            cfg_path = ensure_openvpn3_config(conn_id, ovpn_content, target_uid=target_uid)
            state[conn_id] = {"nm_uuid": conn.get_uuid(), "openvpn3_path": cfg_path}
            results.append({
                "name": conn_id,
                "openvpn3_path": cfg_path,
                "nm_uuid": conn.get_uuid(),
                "direction": "kde-nm -> openvpn3",
            })
        except Exception as exc:
            results.append({"name": conn_id, "error": str(exc)})

    save_synced_state(state, uid=target_uid)
    return results
