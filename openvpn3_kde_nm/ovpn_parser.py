"""Parser and generator for .ovpn profiles and NetworkManager VPN settings."""

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


INLINE_TAGS = ("ca", "cert", "key", "tls-auth", "tls-crypt", "tls-crypt-v2", "extra-certs")


def get_user_home(uid: Optional[int] = None) -> Path:
    """Return the home directory for the target desktop user."""
    if uid is None:
        uid = int(os.environ.get("SUDO_UID", os.getuid()))
    if uid == 0:
        uid = 1000
    import pwd
    try:
        return Path(pwd.getpwuid(uid).pw_dir)
    except KeyError:
        return Path.home()


def get_cert_dir(uid: Optional[int] = None, create: bool = True) -> Path:
    """Return ~/.cert/nm-openvpn directory used by KDE Connection Manager."""
    cert_dir = get_user_home(uid) / ".cert" / "nm-openvpn"
    if create:
        try:
            cert_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return cert_dir


def get_config_dir(uid: Optional[int] = None, create: bool = True) -> Path:
    """Return ~/.config/openvpn3-kde-nm directory."""
    cfg_dir = get_user_home(uid) / ".config" / "openvpn3-kde-nm"
    if create:
        try:
            cfg_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return cfg_dir


def get_profile_store_dir(uid: Optional[int] = None, create: bool = True) -> Path:
    """Return directory storing raw .ovpn profiles for OpenVPN 3."""
    store_dir = get_config_dir(uid, create=create) / "profiles"
    if create:
        try:
            store_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return store_dir


def extract_cert_cn_and_issuer(cert_pem_or_path: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract X.509 Subject CN and Issuer CN from PEM content or file path."""
    try:
        if os.path.isfile(cert_pem_or_path):
            cmd = ["openssl", "x509", "-in", cert_pem_or_path, "-noout", "-subject", "-issuer"]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=5)
        else:
            cmd = ["openssl", "x509", "-noout", "-subject", "-issuer"]
            proc = subprocess.run(
                cmd, input=cert_pem_or_path, capture_output=True, text=True, check=False, timeout=5
            )
        if proc.returncode != 0:
            return None, None
        subject_cn = None
        issuer_cn = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("subject="):
                m = re.search(r"CN\s*=\s*([^\n,/]+(?:/[^\n,]+)*)", line)
                if m:
                    subject_cn = m.group(1).strip()
            elif line.startswith("issuer="):
                m = re.search(r"CN\s*=\s*([^\n,]+)", line)
                if m:
                    issuer_cn = m.group(1).strip()
        return subject_cn, issuer_cn
    except Exception:
        return None, None


def infer_profile_name(ovpn_content: str, fallback_name: str = "OpenVPN3-Profile") -> str:
    """Infer a clean profile name from comments, setenv, or filename."""
    for line in ovpn_content.splitlines():
        line = line.strip()
        if line.startswith("# OVPN_WEBAUTH_FRIENDLY_USERNAME="):
            val = line.split("=", 1)[1].strip()
            parts = [p for p in val.split("/") if p]
            if len(parts) >= 3:
                return parts[-1]
        if line.startswith("# OVPN_FRIENDLY_PROFILE_NAME="):
            val = line.split("=", 1)[1].strip()
            if val:
                fallback_name = val
    return fallback_name


def parse_ovpn(ovpn_content: str) -> Dict[str, object]:
    """Parse an .ovpn configuration string into structured directives and inline blocks."""
    inline_blocks: Dict[str, str] = {}
    for tag in INLINE_TAGS:
        pattern = re.compile(rf"<{re.escape(tag)}>\s*(.*?)\s*</{re.escape(tag)}>", re.DOTALL)
        match = pattern.search(ovpn_content)
        if match:
            inline_blocks[tag] = match.group(1).strip() + "\n"

    cleaned = ovpn_content
    for tag in INLINE_TAGS:
        cleaned = re.sub(rf"<{re.escape(tag)}>.*?</{re.escape(tag)}>", "", cleaned, flags=re.DOTALL)

    remotes: List[str] = []
    setenv: Dict[str, str] = {}
    directives: Dict[str, str] = {}
    flags: List[str] = []
    comments: List[str] = []

    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith(";"):
            comments.append(line)
            continue

        parts = line.split(None, 1)
        key = parts[0]
        val = parts[1].strip() if len(parts) > 1 else ""

        if key == "remote":
            rparts = val.split()
            host = rparts[0] if rparts else ""
            port = rparts[1] if len(rparts) > 1 else "1194"
            proto = rparts[2] if len(rparts) > 2 else "udp"
            entry = f"{host}:{port}:{proto}"
            if entry not in remotes:
                remotes.append(entry)
        elif key == "setenv":
            sparts = val.split(None, 1)
            if len(sparts) == 2:
                k, v = sparts[0], sparts[1].strip('"').strip("'")
                setenv[k] = v
        elif val == "":
            flags.append(key)
        else:
            directives[key] = val

    return {
        "inline": inline_blocks,
        "remotes": remotes,
        "setenv": setenv,
        "directives": directives,
        "flags": flags,
        "comments": comments,
        "raw": ovpn_content,
    }


def write_extracted_certs(
    profile_name: str,
    inline_blocks: Dict[str, str],
    uid: Optional[int] = None,
) -> Dict[str, str]:
    """Write inline PEM blocks to ~/.cert/nm-openvpn/<profile_name>-*.pem for KDE UI."""
    cert_dir = get_cert_dir(uid, create=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile_name)
    paths: Dict[str, str] = {}

    mapping = {
        "ca": "ca",
        "cert": "cert",
        "key": "key",
        "tls-auth": "tls-auth",
        "tls-crypt": "tls-crypt",
        "tls-crypt-v2": "tls-crypt-v2",
        "extra-certs": "extra-certs",
    }

    for tag, suffix in mapping.items():
        if tag in inline_blocks and inline_blocks[tag].strip():
            pem_path = cert_dir / f"{safe_name}-{suffix}.pem"
            try:
                pem_path.write_text(inline_blocks[tag], encoding="utf-8")
                os.chmod(pem_path, 0o600)
                if uid is not None and os.getuid() == 0:
                    import pwd
                    pw = pwd.getpwuid(uid)
                    os.chown(pem_path, pw.pw_uid, pw.pw_gid)
            except OSError:
                pass
            paths[tag] = str(pem_path)

    return paths


def remove_extracted_certs(profile_name: str, uid: Optional[int] = None) -> None:
    """Remove extracted PEM files and stored raw profile for a deleted connection."""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile_name)
    cert_dir = get_cert_dir(uid, create=False)
    if cert_dir.is_dir():
        for pem_file in cert_dir.glob(f"{safe_name}-*.pem"):
            try:
                pem_file.unlink()
            except OSError:
                pass

    store_dir = get_profile_store_dir(uid, create=False)
    prof_path = store_dir / f"{safe_name}.ovpn"
    if prof_path.is_file():
        try:
            prof_path.unlink()
        except OSError:
            pass


def save_raw_profile(profile_name: str, ovpn_content: str, uid: Optional[int] = None) -> Optional[Path]:
    """Save the complete .ovpn profile in ~/.config/openvpn3-kde-nm/profiles/."""
    try:
        store_dir = get_profile_store_dir(uid, create=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile_name)
        prof_path = store_dir / f"{safe_name}.ovpn"
        prof_path.write_text(ovpn_content, encoding="utf-8")
        os.chmod(prof_path, 0o600)
        if uid is not None and os.getuid() == 0:
            import pwd
            pw = pwd.getpwuid(uid)
            os.chown(prof_path, pw.pw_uid, pw.pw_gid)
        return prof_path
    except OSError:
        return None


def load_raw_profile(profile_name: str, uid: Optional[int] = None) -> Optional[str]:
    """Load a previously saved raw .ovpn profile if available."""
    store_dir = get_profile_store_dir(uid, create=False)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile_name)
    prof_path = store_dir / f"{safe_name}.ovpn"
    if prof_path.is_file():
        try:
            return prof_path.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def build_nm_vpn_data(
    _profile_name: str,
    parsed: Dict[str, object],
    cert_paths: Dict[str, str],
) -> Dict[str, str]:
    """Construct standard NetworkManager vpn.data dictionary compatible with KDE plasma-nm."""
    directives: Dict[str, str] = parsed.get("directives", {})  # type: ignore
    flags: List[str] = parsed.get("flags", [])  # type: ignore
    remotes: List[str] = parsed.get("remotes", [])  # type: ignore

    vpn_data: Dict[str, str] = {
        "connection-type": "tls",
        "dev": directives.get("dev", "tun"),
        "cert-pass-flags": "4",
    }

    if remotes:
        vpn_data["remote"] = ", ".join(remotes)
    if "cipher" in directives:
        vpn_data["cipher"] = directives["cipher"]
    if "auth" in directives:
        vpn_data["auth"] = directives["auth"]
    if "remote-cert-tls" in directives:
        vpn_data["remote-cert-tls"] = directives["remote-cert-tls"]
    if "key-direction" in directives:
        vpn_data["ta-dir"] = directives["key-direction"]
    if "push-peer-info" in flags:
        vpn_data["push-peer-info"] = "yes"

    if "ca" in cert_paths:
        vpn_data["ca"] = cert_paths["ca"]
    if "cert" in cert_paths:
        vpn_data["cert"] = cert_paths["cert"]
    if "key" in cert_paths:
        vpn_data["key"] = cert_paths["key"]
    if "tls-auth" in cert_paths:
        vpn_data["ta"] = cert_paths["tls-auth"]
    if "tls-crypt" in cert_paths:
        vpn_data["tls-crypt"] = cert_paths["tls-crypt"]
    if "tls-crypt-v2" in cert_paths:
        vpn_data["tls-crypt-v2"] = cert_paths["tls-crypt-v2"]
    if "extra-certs" in cert_paths:
        vpn_data["extra-certs"] = cert_paths["extra-certs"]

    return vpn_data


def reconstruct_ovpn_from_nm_vpn_data(conn_id: str, vpn_data: Dict[str, str], uid: Optional[int] = None) -> str:
    """
    Reconstruct a full OpenVPN 3 .ovpn configuration from NetworkManager vpn.data.
    Automatically recovers CloudConnexa/OpenVPN Cloud setenv USERNAME from the
    User Certificate X.509 Subject CN if needed.
    """
    stored = load_raw_profile(conn_id, uid=uid)

    cert_path = vpn_data.get("cert", "")
    setenv_username = ""
    if cert_path and os.path.isfile(cert_path):
        subject_cn, issuer_cn = extract_cert_cn_and_issuer(cert_path)
        if subject_cn and ("/" in subject_cn or (issuer_cn and "CloudVPN" in issuer_cn)):
            setenv_username = subject_cn

    if stored:
        if not cert_path or not os.path.isfile(cert_path):
            return stored
        stored_parsed = parse_ovpn(stored)
        stored_cert = stored_parsed.get("inline", {}).get("cert", "").strip()
        try:
            current_cert = Path(cert_path).read_text(encoding="utf-8").strip()
            if stored_cert and stored_cert in current_cert:
                return stored
        except Exception:
            return stored

    lines: List[str] = []
    if setenv_username:
        lines.append(f'setenv USERNAME "{setenv_username}"')

    lines.append("client")
    lines.append(f"dev {vpn_data.get('dev', 'tun')}")

    remote_str = vpn_data.get("remote", "")
    for item in remote_str.split(","):
        item = item.strip()
        if not item:
            continue
        rparts = item.split(":")
        host = rparts[0]
        port = rparts[1] if len(rparts) > 1 else vpn_data.get("port", "1194")
        proto = rparts[2] if len(rparts) > 2 else ("tcp" if vpn_data.get("proto-tcp") == "yes" else "udp")
        lines.append(f"remote {host} {port} {proto}")

    if vpn_data.get("remote-cert-tls"):
        lines.append(f"remote-cert-tls {vpn_data['remote-cert-tls']}")
    else:
        lines.append("remote-cert-tls server")

    if vpn_data.get("cipher"):
        lines.append(f"cipher {vpn_data['cipher']}")
    if vpn_data.get("auth"):
        lines.append(f"auth {vpn_data['auth']}")

    lines.append("persist-tun")
    lines.append("nobind")
    lines.append("verb 3")
    lines.append("socket-flags TCP_NODELAY")
    lines.append("push-peer-info")

    if vpn_data.get("ta-dir"):
        lines.append(f"key-direction {vpn_data['ta-dir']}")

    inline_file_map = [
        ("ca", vpn_data.get("ca")),
        ("cert", vpn_data.get("cert")),
        ("key", vpn_data.get("key")),
        ("tls-auth", vpn_data.get("ta")),
        ("tls-crypt", vpn_data.get("tls-crypt")),
        ("tls-crypt-v2", vpn_data.get("tls-crypt-v2")),
        ("extra-certs", vpn_data.get("extra-certs")),
    ]

    for tag, fpath in inline_file_map:
        if fpath and os.path.isfile(fpath):
            content = Path(fpath).read_text(encoding="utf-8").strip()
            lines.append(f"\n<{tag}>\n{content}\n</{tag}>")

    ovpn_text = "\n".join(lines) + "\n"
    save_raw_profile(conn_id, ovpn_text, uid=uid)
    return ovpn_text
