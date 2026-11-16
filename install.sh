#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

chmod +x "$PROJECT_DIR/bin/nm-openvpn3-service"
chmod +x "$PROJECT_DIR/bin/openvpn3-kde-sync"
chmod +x "$PROJECT_DIR/bin/openvpn3-kde-cli"

exec "$PROJECT_DIR/bin/openvpn3-kde-cli" install "$@"
