#!/usr/bin/env bash
set -euo pipefail

IPTV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Embed mpv via X11/XWayland window ID inside our PyQt5 player
export QT_QPA_PLATFORM=xcb

exec /usr/bin/python3 "${IPTV_DIR}/iptv_player.py" "${IPTV_DIR}/playlist.m3u" "$@"
