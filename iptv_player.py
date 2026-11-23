#!/usr/bin/env python3
"""Pi IPTV — Lightweight PyQt5 + mpv IPTV player with native channel logos."""
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from PyQt5.QtCore import (
    QByteArray,
    QObject,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QShortcut,
    QSizePolicy,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PLAYLIST = os.path.join(BASE_DIR, "playlist.m3u")
LOGO_DIR = os.path.join(BASE_DIR, "logos")
BADGE_DIR = os.path.join(LOGO_DIR, "badges")
MAX720_SCRIPT = os.path.join(BASE_DIR, "max720p.lua")
APP_ICON_PATH = os.path.join(BASE_DIR, "iptv.svg")

FETCH_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


@dataclass
class Channel:
    index: int
    name: str
    url: str
    group: str = "General"
    language: str = "English"
    category: str = "General"
    logo_url: str = ""
    tvg_id: str = ""
    user_agent: str = ""


CATEGORY_ORDER = [
    ("All", "All"),
    ("News", "News"),
    ("Sports", "Sports"),
    ("Movies", "Movies"),
    ("Docs", "Docs"),
    ("Entertainment", "Ent."),
    ("Music", "Music"),
    ("Kids", "Kids"),
]


def classify_group(group: str):
    low = group.lower()
    lang = "Israel" if low.startswith("israel") else "English"
    if "news" in low or "general" in low or "business" in low:
        cat = "News"
    elif "sport" in low:
        cat = "Sports"
    elif "movie" in low:
        cat = "Movies"
    elif "doc" in low or "lifestyle" in low:
        cat = "Docs"
    elif "entertain" in low or "series" in low:
        cat = "Entertainment"
    elif "music" in low:
        cat = "Music"
    elif "kid" in low:
        cat = "Kids"
    else:
        cat = "News"
    return lang, cat


def parse_m3u(path: str) -> List[Channel]:
    channels: List[Channel] = []
    if not os.path.isfile(path):
        return channels
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = [l.strip() for l in f if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF:"):
            extinf = line
            ua = ""
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                if lines[j].startswith("#EXTVLCOPT:http-user-agent="):
                    ua = lines[j].split("=", 1)[1].strip()
                j += 1
            if j < len(lines):
                url = lines[j]
                # Find last comma outside quotes
                in_q = False
                comma_idx = -1
                for idx, ch in enumerate(extinf):
                    if ch == '"':
                        in_q = not in_q
                    elif ch == "," and not in_q:
                        comma_idx = idx
                name = extinf[comma_idx + 1 :].strip() if comma_idx != -1 else url
                grp_m = re.search(r'group-title="([^"]*)"', extinf)
                logo_m = re.search(r'tvg-logo="([^"]*)"', extinf)
                id_m = re.search(r'tvg-id="([^"]*)"', extinf)
                if not ua:
                    ua_m = re.search(r'http-user-agent="([^"]*)"', extinf)
                    if ua_m:
                        ua = ua_m.group(1)
                grp = grp_m.group(1) if grp_m and grp_m.group(1) else "General"
                lang, cat = classify_group(grp)
                channels.append(
                    Channel(
                        index=len(channels) + 1,
                        name=name,
                        url=url,
                        group=grp,
                        language=lang,
                        category=cat,
                        logo_url=logo_m.group(1) if logo_m else "",
                        tvg_id=id_m.group(1) if id_m else "",
                        user_agent=ua,
                    )
                )
                i = j + 1
                continue
        i += 1
    return channels


def logo_cache_path(url: str) -> str:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".png"
    return os.path.join(LOGO_DIR, key)


def badge_cache_path(url: str) -> str:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".png"
    return os.path.join(BADGE_DIR, key)


def image_avg_luminance(img: QImage) -> float:
    """Estimate average brightness of non-transparent pixels (0..255)."""
    small = img.scaled(24, 16, Qt.IgnoreAspectRatio, Qt.FastTransformation).convertToFormat(
        QImage.Format_ARGB32
    )
    total = 0.0
    weight = 0.0
    for y in range(small.height()):
        for x in range(small.width()):
            c = QColor.fromRgba(small.pixel(x, y))
            a = c.alpha() / 255.0
            if a > 0.15:
                lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
                total += lum * a
                weight += a
    return (total / weight) if weight > 0 else 128.0


def render_logo_badge(src: QPixmap, w: int = 60, h: int = 40) -> QPixmap:
    """Draw a channel logo inside a crisp rounded badge with auto-contrast background."""
    img = src.toImage()
    lum = image_avg_luminance(img)
    # Dark logos get a clean light backing; bright/white logos get a rich slate backing
    if lum < 120:
        bg_color = QColor("#e2e8f0")
        border_color = QColor("#cbd5e1")
    else:
        bg_color = QColor("#1e293b")
        border_color = QColor("#334155")

    out = QPixmap(w, h)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)

    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 7.0, 7.0)
    p.fillPath(path, bg_color)
    p.setPen(border_color)
    p.drawPath(path)

    pad_x, pad_y = 5, 4
    scaled = src.scaled(w - 2 * pad_x, h - 2 * pad_y, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    x = (w - scaled.width()) // 2
    y = (h - scaled.height()) // 2
    p.drawPixmap(x, y, scaled)
    p.end()
    return out


GROUP_COLORS = {
    "Israel - News & General": "#0284c7",
    "Israel - Entertainment & Series": "#7c3aed",
    "Israel - Sports": "#16a34a",
    "Israel - Kids": "#db2777",
    "Israel - Music": "#9333ea",
    "Israel - Kids & Music": "#db2777",
    "English - News & Business": "#2563eb",
    "English - Documentaries & Lifestyle": "#0d9488",
    "English - Movies": "#dc2626",
    "English - Sports": "#ca8a04",
    "English - Music": "#9333ea",
    "English - Kids": "#ea580c",
}


def render_monogram_badge(name: str, group: str, w: int = 60, h: int = 40) -> QPixmap:
    """Fallback crisp colored monogram icon when a logo is unavailable."""
    words = [w_part for w_part in re.split(r"[\s\-()]+", name) if w_part]
    if len(words) >= 2:
        label = (words[0][0] + words[1][0]).upper()
    elif words:
        label = words[0][:3].upper()
    else:
        label = "TV"

    bg = QColor(GROUP_COLORS.get(group, "#334155"))
    out = QPixmap(w, h)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)

    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 7.0, 7.0)
    p.fillPath(path, bg)

    font = QFont("Sans Serif", 11, QFont.Bold)
    p.setFont(font)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, label)
    p.end()
    return out


class LogoLoader(QObject):
    logo_ready = pyqtSignal(int, QPixmap)  # channel.index, badge pixmap

    def __init__(self, channels: List[Channel]):
        super().__init__()
        self._channels = channels
        self._stop = False

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def stop(self):
        self._stop = True

    def _run(self):
        os.makedirs(LOGO_DIR, exist_ok=True)
        for ch in self._channels:
            if self._stop:
                return
            if not ch.logo_url:
                continue
            cache_file = logo_cache_path(ch.logo_url)
            if os.path.isfile(cache_file):
                continue
            try:
                req = urllib.request.Request(
                    ch.logo_url,
                    headers={"User-Agent": FETCH_UA, "Accept": "image/*,*/*;q=0.8"},
                )
                with urllib.request.urlopen(req, timeout=8, context=SSL_CTX) as resp:
                    data = resp.read(2 * 1024 * 1024)
                pix = QPixmap()
                if pix.loadFromData(QByteArray(data)):
                    scaled = pix.scaled(88, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    canvas = QImage(96, 64, QImage.Format_ARGB32_Premultiplied)
                    canvas.fill(Qt.transparent)
                    painter = QPainter(canvas)
                    painter.drawPixmap(
                        (96 - scaled.width()) // 2,
                        (64 - scaled.height()) // 2,
                        scaled,
                    )
                    painter.end()
                    canvas.save(cache_file, "PNG")
                    badge = render_logo_badge(QPixmap.fromImage(canvas))
                    os.makedirs(BADGE_DIR, exist_ok=True)
                    badge.save(badge_cache_path(ch.logo_url), "PNG")
                    self.logo_ready.emit(ch.index, badge)
            except Exception:
                pass


class MpvBridge(QObject):
    """Manages an embedded mpv subprocess via JSON Unix-domain IPC."""

    state_changed = pyqtSignal(dict)
    client_action = pyqtSignal(str)

    def __init__(self, wid: int, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._wid = wid
        self._sock_path = f"/tmp/pi-iptv-mpv-{os.getpid()}.sock"
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._stop = False
        self._req_id = 1
        self._state = {
            "pause": False,
            "mute": False,
            "volume": 80,
            "height": 0,
            "buffering": False,
            "idle": True,
        }

    def start(self):
        if os.path.exists(self._sock_path):
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass

        cmd = [
            "/usr/bin/mpv",
            f"--wid={self._wid}",
            f"--input-ipc-server={self._sock_path}",
            "--profile=fast",
            "--vo=gpu",
            "--gpu-context=x11egl",
            "--hwdec=v4l2m2m-copy,drm-copy",
            "--demuxer-max-bytes=32MiB",
            "--cache=yes",
            "--ytdl=no",
            "--idle=yes",
            "--keep-open=no",
            "--osc=no",
            "--osd-level=1",
            "--no-input-default-bindings",
            "--input-vo-keyboard=no",
            "--volume=80",
            "--volume-max=120",
        ]
        if os.path.isfile(MAX720_SCRIPT):
            cmd.append(f"--script={MAX720_SCRIPT}")

        # Strip WAYLAND_DISPLAY so mpv stays strictly on X11/XWayland and embeds into --wid
        mpv_env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}

        self._proc = subprocess.Popen(
            cmd,
            env=mpv_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        t = threading.Thread(target=self._ipc_loop, daemon=True)
        t.start()

    def _ipc_loop(self):
        # Wait up to 5s for mpv IPC socket to appear
        deadline = time.time() + 5.0
        while not self._stop and time.time() < deadline:
            if os.path.exists(self._sock_path):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self._sock_path)
                    with self._sock_lock:
                        self._sock = s
                    break
                except OSError:
                    pass
            time.sleep(0.05)

        if self._sock is None:
            return

        # Subscribe to properties & mouse gestures on embedded video
        self.send(["observe_property", 1, "pause"])
        self.send(["observe_property", 2, "mute"])
        self.send(["observe_property", 3, "volume"])
        self.send(["observe_property", 4, "video-params/h"])
        self.send(["observe_property", 5, "paused-for-cache"])
        self.send(["observe_property", 6, "core-idle"])
        self.send(["keybind", "MBTN_LEFT_DBL", "script-message toggle-fullscreen"])
        self.send(["keybind", "WHEEL_UP", "script-message vol-up"])
        self.send(["keybind", "WHEEL_DOWN", "script-message vol-down"])
        self.client_action.emit("mpv-ready")

        buf = b""
        while not self._stop:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", errors="ignore"))
                    except json.JSONDecodeError:
                        continue
                    self._handle_msg(msg)
            except OSError:
                break

    def _handle_msg(self, msg: dict):
        ev = msg.get("event")
        if ev == "property-change":
            name = msg.get("name")
            val = msg.get("data")
            if name == "pause" and isinstance(val, bool):
                self._state["pause"] = val
            elif name == "mute" and isinstance(val, bool):
                self._state["mute"] = val
            elif name == "volume" and isinstance(val, (int, float)):
                self._state["volume"] = int(round(val))
            elif name == "video-params/h":
                self._state["height"] = int(val) if isinstance(val, (int, float)) else 0
            elif name == "paused-for-cache" and isinstance(val, bool):
                self._state["buffering"] = val
            elif name == "core-idle" and isinstance(val, bool):
                self._state["idle"] = val
            self.state_changed.emit(dict(self._state))
        elif ev == "playback-restart":
            self.client_action.emit("playback-restart")
        elif ev == "client-message":
            args = msg.get("args") or []
            if args:
                self.client_action.emit(str(args[0]))

    def send(self, command: list):
        with self._sock_lock:
            if not self._sock:
                return
            self._req_id += 1
            payload = (json.dumps({"command": command, "request_id": self._req_id}) + "\n").encode(
                "utf-8"
            )
            try:
                self._sock.sendall(payload)
            except OSError:
                pass

    def play(self, url: str, user_agent: str = ""):
        if user_agent:
            self.send(["set_property", "user-agent", user_agent])
        else:
            self.send(["set_property", "user-agent", "mpv"])
        self.send(["set_property", "pause", False])
        self.send(["loadfile", url, "replace"])

    def toggle_pause(self):
        self.send(["cycle", "pause"])

    def toggle_mute(self):
        self.send(["cycle", "mute"])

    def set_volume(self, vol: int):
        vol = max(0, min(120, vol))
        self.send(["set_property", "volume", vol])

    def stop(self):
        self._stop = True
        self.send(["quit"])
        with self._sock_lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=1.2)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        if os.path.exists(self._sock_path):
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass


DARK_QSS = """
QMainWindow, QWidget {
    background-color: #0b1120;
    color: #e2e8f0;
    font-family: "Nunito Sans", "DejaVu Sans", sans-serif;
    font-weight: bold;
}
QSplitter::handle {
    background-color: #1e293b;
    width: 3px;
}
QSplitter::handle:hover {
    background-color: #0ea5e9;
}
QFrame#sidebar {
    background-color: #0f172a;
    border-left: 1px solid #1e293b;
}
QFrame#controlsBar {
    background-color: #0f172a;
    border-top: 1px solid #1e293b;
}
QComboBox {
    background-color: #1e293b;
    color: #f1f5f9;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 13pt;
    font-weight: bold;
    min-height: 24px;
}
QComboBox:hover, QComboBox:focus {
    border-color: #0ea5e9;
}
QComboBox::drop-down {
    border: none;
    width: 28px;
}
QComboBox QAbstractItemView {
    background-color: #1e293b;
    color: #f1f5f9;
    selection-background-color: #0284c7;
    selection-color: #ffffff;
    border: 1px solid #334155;
    font-size: 13pt;
    font-weight: bold;
    padding: 4px;
}
QLineEdit {
    background-color: #1e293b;
    color: #f1f5f9;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 13pt;
    font-weight: bold;
    min-height: 24px;
}
QLineEdit:focus {
    border-color: #0ea5e9;
}
QListWidget {
    background-color: #0f172a;
    border: none;
    outline: none;
    font-size: 15pt;
    font-weight: bold;
    padding: 4px 6px;
}
QListWidget::item {
    color: #cbd5e1;
    font-weight: bold;
    padding: 6px 8px;
    margin: 2px 0px;
    border-radius: 8px;
    border: 1px solid transparent;
}
QListWidget::item:hover {
    background-color: #1e293b;
}
QListWidget::item:selected {
    background-color: #0369a1;
    color: #ffffff;
    border: 1px solid #38bdf8;
}
QListWidget::item:disabled {
    color: #ffffff;
    background-color: #1e293b;
    border-radius: 6px;
    padding: 4px 8px;
}
QScrollBar:vertical {
    background: #0f172a;
    width: 10px;
    margin: 2px 0px;
}
QScrollBar::handle:vertical {
    background: #334155;
    border-radius: 5px;
    min-height: 36px;
}
QScrollBar::handle:vertical:hover {
    background: #475569;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QPushButton {
    background-color: #1e293b;
    color: #f1f5f9;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 14pt;
    font-weight: bold;
    min-width: 38px;
    min-height: 36px;
}
QPushButton:hover {
    background-color: #334155;
    border-color: #0ea5e9;
}
QPushButton:pressed {
    background-color: #0284c7;
}
QPushButton[pill="true"] {
    background-color: #1e293b;
    color: #f8fafc;
    border: 1px solid #475569;
    border-radius: 8px;
    padding: 5px 8px;
    font-size: 11.5pt;
    font-weight: bold;
    min-width: 0px;
    min-height: 28px;
}
QPushButton[pill="true"]:hover {
    background-color: #334155;
    color: #ffffff;
    border-color: #38bdf8;
}
QPushButton[pill="true"]:checked {
    background-color: #0284c7;
    color: #ffffff;
    border: 1px solid #38bdf8;
}
QPushButton[pill="true"]:disabled {
    background-color: #0f172a;
    color: #475569;
    border-color: #1e293b;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #334155;
    border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background: #0ea5e9;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #f8fafc;
    border: 2px solid #0ea5e9;
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 9px;
}
QLabel#nowTitle {
    color: #f8fafc;
    font-size: 14pt;
    font-weight: bold;
}
QLabel#statusBadge {
    background-color: #1e293b;
    color: #38bdf8;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 3px 9px;
    font-size: 11pt;
    font-weight: bold;
}
"""


class IptvWindow(QMainWindow):
    def __init__(self, playlist_path: str):
        super().__init__()
        self.setWindowTitle("Pi IPTV")
        if os.path.isfile(APP_ICON_PATH):
            self.setWindowIcon(QIcon(APP_ICON_PATH))

        self.channels = parse_m3u(playlist_path)
        self.filtered_channels: List[Channel] = list(self.channels)
        self.current_channel: Optional[Channel] = None
        self.active_lang: str = "All"
        self.active_cat: str = "All"
        self.lang_buttons: Dict[str, QPushButton] = {}
        self.cat_buttons: Dict[str, QPushButton] = {}
        self.badges: Dict[int, QPixmap] = {}
        self.sidebar_visible_before_fs = True
        self._updating_vol_slider = False
        self._mpv_ready = False
        self._loading_channel = True
        self._last_badge_state = None
        self._last_paused = None
        self._last_muted = None

        self._load_cached_badges()
        self._build_ui()
        self._bind_shortcuts()

        # Start embedded mpv once native X11 window is created
        self.mpv: Optional[MpvBridge] = None
        self.logo_loader = LogoLoader(self.channels)
        self.logo_loader.logo_ready.connect(self._on_logo_ready)
        self.logo_loader.start()

        QTimer.singleShot(100, self._start_mpv)

    def _load_cached_badges(self):
        os.makedirs(BADGE_DIR, exist_ok=True)
        for ch in self.channels:
            pix = None
            if ch.logo_url:
                bfile = badge_cache_path(ch.logo_url)
                if os.path.isfile(bfile):
                    cached = QPixmap(bfile)
                    if not cached.isNull():
                        pix = cached
                if pix is None:
                    cache_file = logo_cache_path(ch.logo_url)
                    if os.path.isfile(cache_file):
                        raw = QPixmap(cache_file)
                        if not raw.isNull():
                            pix = render_logo_badge(raw)
                            pix.save(bfile, "PNG")
            if pix is None:
                pix = render_monogram_badge(ch.name, ch.group)
            self.badges[ch.index] = pix

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(3)
        outer.addWidget(self.splitter, 1)

        # Left pane: Video + slim bottom controls
        left_pane = QWidget()
        left_layout = QVBoxLayout(left_pane)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self.video_widget = QWidget()
        self.video_widget.setAttribute(Qt.WA_NativeWindow, True)
        self.video_widget.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        self.video_widget.setFocusPolicy(Qt.NoFocus)
        self.video_widget.setStyleSheet("background-color: #000000;")
        self.video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        left_layout.addWidget(self.video_widget, 1)

        # Slim bottom controls bar
        self.controls_bar = QFrame()
        self.controls_bar.setObjectName("controlsBar")
        self.controls_bar.setFixedHeight(58)
        cb = QHBoxLayout(self.controls_bar)
        cb.setContentsMargins(12, 6, 12, 6)
        cb.setSpacing(10)

        self.now_logo = QLabel()
        self.now_logo.setFixedSize(54, 36)
        cb.addWidget(self.now_logo)

        self.now_title = QLabel("Starting IPTV...")
        self.now_title.setObjectName("nowTitle")
        cb.addWidget(self.now_title, 1)

        self.status_badge = QLabel("CONNECTING")
        self.status_badge.setObjectName("statusBadge")
        cb.addWidget(self.status_badge)

        self.btn_prev = QPushButton("⏮")
        self.btn_prev.setToolTip("Previous channel (P / PageUp)")
        self.btn_prev.clicked.connect(self.play_prev)
        cb.addWidget(self.btn_prev)

        self.btn_pause = QPushButton("⏸")
        self.btn_pause.setToolTip("Play / Pause (Space)")
        self.btn_pause.clicked.connect(self.toggle_pause)
        cb.addWidget(self.btn_pause)

        self.btn_next = QPushButton("⏭")
        self.btn_next.setToolTip("Next channel (N / PageDown)")
        self.btn_next.clicked.connect(self.play_next)
        cb.addWidget(self.btn_next)

        self.btn_mute = QPushButton("🔊")
        self.btn_mute.setToolTip("Mute (M)")
        self.btn_mute.clicked.connect(self.toggle_mute)
        cb.addWidget(self.btn_mute)

        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 120)
        self.vol_slider.setValue(80)
        self.vol_slider.setFixedWidth(115)
        self.vol_slider.valueChanged.connect(self._on_vol_slider)
        cb.addWidget(self.vol_slider)

        self.btn_sidebar = QPushButton("☰")
        self.btn_sidebar.setToolTip("Toggle channel list (L)")
        self.btn_sidebar.clicked.connect(self.toggle_sidebar)
        cb.addWidget(self.btn_sidebar)

        self.btn_fs = QPushButton("⛶")
        self.btn_fs.setToolTip("Fullscreen (F / Double-click video)")
        self.btn_fs.clicked.connect(self.toggle_fullscreen)
        cb.addWidget(self.btn_fs)

        for btn in (
            self.btn_prev,
            self.btn_pause,
            self.btn_next,
            self.btn_mute,
            self.vol_slider,
            self.btn_sidebar,
            self.btn_fs,
        ):
            btn.setFocusPolicy(Qt.NoFocus)

        left_layout.addWidget(self.controls_bar)
        self.splitter.addWidget(left_pane)

        # Right pane: Language tabs + Category filter pills + Search + Categorized Channel List
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(380)
        sb = QVBoxLayout(self.sidebar)
        sb.setContentsMargins(10, 10, 10, 10)
        sb.setSpacing(8)

        # Row 1: Language filter pills
        lang_row = QHBoxLayout()
        lang_row.setSpacing(6)
        il_count = sum(1 for c in self.channels if c.language == "Israel")
        en_count = sum(1 for c in self.channels if c.language == "English")
        for key, label in [
            ("All", f"All ({len(self.channels)})"),
            ("Israel", f"Israel ({il_count})"),
            ("English", f"English ({en_count})"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == "All")
            btn.setProperty("pill", True)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(lambda _=False, k=key: self._set_lang(k))
            self.lang_buttons[key] = btn
            lang_row.addWidget(btn)
        sb.addLayout(lang_row)

        # Row 2-3: Category filter grid (2 rows x 4 pills)
        cat_grid = QGridLayout()
        cat_grid.setSpacing(6)
        for idx, (key, short_label) in enumerate(CATEGORY_ORDER):
            btn = QPushButton(short_label)
            btn.setCheckable(True)
            btn.setChecked(key == "All")
            btn.setProperty("pill", True)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(lambda _=False, k=key: self._set_cat(k))
            self.cat_buttons[key] = btn
            cat_grid.addWidget(btn, idx // 4, idx % 4)
        sb.addLayout(cat_grid)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search channels...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_filter)
        self.search_input.returnPressed.connect(lambda: self._focus_list(force=True))
        sb.addWidget(self.search_input)

        self.list_widget = QListWidget()
        self.list_widget.setIconSize(QSize(60, 40))
        self.list_widget.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.itemClicked.connect(self._on_item_activated)
        self.list_widget.itemActivated.connect(self._on_item_activated)
        sb.addWidget(self.list_widget, 1)

        self.splitter.addWidget(self.sidebar)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([1380, 540])

        self._update_cat_buttons()
        self._populate_list()
        self._select_first_channel_row()
        self.list_widget.setFocus()

    def _bind_shortcuts(self):
        QShortcut(QKeySequence("F"), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("Escape"), self, self._exit_fullscreen_or_clear)
        QShortcut(QKeySequence("Space"), self, self.toggle_pause)
        QShortcut(QKeySequence("M"), self, self.toggle_mute)
        QShortcut(QKeySequence("N"), self, self.play_next)
        QShortcut(QKeySequence("P"), self, self.play_prev)
        QShortcut(QKeySequence("PgDown"), self, self.play_next)
        QShortcut(QKeySequence("PgUp"), self, self.play_prev)
        QShortcut(QKeySequence("Left"), self, lambda: self._cycle_cat(-1))
        QShortcut(QKeySequence("Right"), self, lambda: self._cycle_cat(1))
        QShortcut(QKeySequence("L"), self, self.toggle_sidebar)
        QShortcut(QKeySequence("Ctrl+L"), self, self.toggle_sidebar)
        QShortcut(QKeySequence("/"), self, self._focus_search)
        QShortcut(QKeySequence("Ctrl+F"), self, self._focus_search)
        QShortcut(QKeySequence("+"), self, lambda: self.adjust_volume(5))
        QShortcut(QKeySequence("="), self, lambda: self.adjust_volume(5))
        QShortcut(QKeySequence("-"), self, lambda: self.adjust_volume(-5))
        QShortcut(QKeySequence("Q"), self, self.close)

    def _select_first_channel_row(self):
        for row in range(self.list_widget.count()):
            if self.list_widget.item(row).data(Qt.UserRole) is not None:
                self.list_widget.setCurrentRow(row)
                return

    def _populate_list(self):
        self.list_widget.clear()
        # Group channels by their section header in original order
        grouped: Dict[str, List[Channel]] = {}
        group_order: List[str] = []
        for ch in self.filtered_channels:
            if ch.group not in grouped:
                grouped[ch.group] = []
                group_order.append(ch.group)
            grouped[ch.group].append(ch)

        show_headers = len(group_order) > 1 or self.active_cat == "All"
        header_font = QFont("Nunito Sans", 11, QFont.Bold)
        selected_item = None

        for grp in group_order:
            chs = grouped[grp]
            if show_headers:
                hdr = QListWidgetItem(f"  {grp.upper()}  ({len(chs)})")
                hdr.setFlags(Qt.NoItemFlags)
                hdr.setData(Qt.UserRole, None)
                hdr.setFont(header_font)
                hdr.setSizeHint(QSize(0, 36))
                self.list_widget.addItem(hdr)

            for ch in chs:
                item = QListWidgetItem(QIcon(self.badges[ch.index]), f" {ch.index:02d}  {ch.name}")
                item.setData(Qt.UserRole, ch.index)
                item.setSizeHint(QSize(0, 52))
                self.list_widget.addItem(item)
                if self.current_channel and ch.index == self.current_channel.index:
                    selected_item = item

        if selected_item is not None:
            self.list_widget.setCurrentItem(selected_item)
        else:
            self._select_first_channel_row()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._focus_list)
        QTimer.singleShot(350, self._focus_list)

    def _focus_list(self, force: bool = False):
        if self.sidebar.isVisible() and (force or not self.search_input.hasFocus()):
            self.list_widget.setFocus(Qt.OtherFocusReason)
            cur = self.list_widget.currentItem()
            if cur is None or cur.data(Qt.UserRole) is None:
                self._select_first_channel_row()

    def _update_cat_buttons(self):
        pool = [
            c for c in self.channels if self.active_lang == "All" or c.language == self.active_lang
        ]
        counts: Dict[str, int] = {"All": len(pool)}
        for c in pool:
            counts[c.category] = counts.get(c.category, 0) + 1
        for key, short_label in CATEGORY_ORDER:
            btn = self.cat_buttons[key]
            cnt = counts.get(key, 0)
            btn.setText(f"{short_label} ({cnt})" if cnt > 0 else short_label)
            btn.setEnabled(cnt > 0)
            btn.setChecked(key == self.active_cat)

    def _set_lang(self, lang: str):
        self.active_lang = lang
        for k, b in self.lang_buttons.items():
            b.setChecked(k == lang)
        # Reset category to All if current category has 0 channels in this language
        pool = [c for c in self.channels if lang == "All" or c.language == lang]
        if self.active_cat != "All" and not any(c.category == self.active_cat for c in pool):
            self.active_cat = "All"
        self._update_cat_buttons()
        self._apply_filter()
        self._focus_list(force=True)

    def _set_cat(self, cat: str):
        self.active_cat = cat
        for k, b in self.cat_buttons.items():
            b.setChecked(k == cat)
        self._apply_filter()
        self._focus_list(force=True)

    def _cycle_cat(self, step: int):
        if self.search_input.hasFocus() or not self.sidebar.isVisible():
            return
        enabled_cats = [k for k, _ in CATEGORY_ORDER if self.cat_buttons[k].isEnabled()]
        if not enabled_cats:
            return
        idx = enabled_cats.index(self.active_cat) if self.active_cat in enabled_cats else 0
        self._set_cat(enabled_cats[(idx + step) % len(enabled_cats)])

    def _apply_filter(self):
        q = self.search_input.text().strip().lower()
        self.filtered_channels = [
            ch
            for ch in self.channels
            if (self.active_lang == "All" or ch.language == self.active_lang)
            and (self.active_cat == "All" or ch.category == self.active_cat)
            and (
                not q
                or q in ch.name.lower()
                or q in ch.group.lower()
                or q in ch.category.lower()
                or q == str(ch.index)
            )
        ]
        self._populate_list()

    def _on_logo_ready(self, ch_index: int, badge: QPixmap):
        self.badges[ch_index] = badge
        for row in range(self.list_widget.count()):
            it = self.list_widget.item(row)
            if it.data(Qt.UserRole) == ch_index:
                it.setIcon(QIcon(badge))
                break
        if self.current_channel and self.current_channel.index == ch_index:
            self.now_logo.setPixmap(badge.scaled(54, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _start_mpv(self):
        wid = int(self.video_widget.winId())
        self.mpv = MpvBridge(wid, self)
        self.mpv.state_changed.connect(self._on_mpv_state)
        self.mpv.client_action.connect(self._on_mpv_action)
        self.mpv.start()

    def _on_mpv_action(self, action: str):
        if action == "mpv-ready":
            self._mpv_ready = True
            if self.channels and self.current_channel is None:
                self.play_channel(self.channels[0])
            self._focus_list()
        elif action == "playback-restart":
            self._loading_channel = False
            if self.mpv:
                self._on_mpv_state(dict(self.mpv._state))
        elif action == "toggle-fullscreen":
            self.toggle_fullscreen()
        elif action == "vol-up":
            self.adjust_volume(5)
        elif action == "vol-down":
            self.adjust_volume(-5)

    def _on_mpv_state(self, st: dict):
        if st["pause"] != self._last_paused:
            self._last_paused = st["pause"]
            self.btn_pause.setText("▶" if st["pause"] else "⏸")
        muted = st["mute"] or st["volume"] == 0
        if muted != self._last_muted:
            self._last_muted = muted
            self.btn_mute.setText("🔇" if muted else "🔊")
        if self.vol_slider.value() != st["volume"]:
            self._updating_vol_slider = True
            self.vol_slider.setValue(st["volume"])
            self._updating_vol_slider = False

        h = st["height"]
        if st["pause"]:
            badge_key = ("PAUSED", 0)
            badge_txt = "PAUSED"
            badge_css = (
                "background:#1e293b; color:#fbbf24; border:1px solid #334155; "
                "border-radius:6px; padding:3px 9px; font-size:11pt; font-weight:bold;"
            )
        elif self._loading_channel or st["buffering"] or (st["idle"] and h == 0):
            badge_key = ("LOADING", 0)
            badge_txt = "LOADING..."
            badge_css = (
                "background:#1e293b; color:#38bdf8; border:1px solid #334155; "
                "border-radius:6px; padding:3px 9px; font-size:11pt; font-weight:bold;"
            )
        else:
            badge_key = ("LIVE", h)
            res_txt = f" · {h}p" if h > 0 else ""
            badge_txt = f"● LIVE{res_txt}"
            badge_css = (
                "background:#052e16; color:#4ade80; border:1px solid #166534; "
                "border-radius:6px; padding:3px 9px; font-size:11pt; font-weight:bold;"
            )

        if badge_key != self._last_badge_state:
            self._last_badge_state = badge_key
            self.status_badge.setText(badge_txt)
            self.status_badge.setStyleSheet(badge_css)

    def _on_item_activated(self, item: QListWidgetItem):
        ch_index = item.data(Qt.UserRole)
        for ch in self.channels:
            if ch.index == ch_index:
                self.play_channel(ch)
                break

    def play_channel(self, ch: Channel):
        self.current_channel = ch
        self._loading_channel = True
        self._last_badge_state = ("CONNECTING", 0)
        self.now_title.setText(f"{ch.index}. {ch.name}")
        self.now_logo.setPixmap(
            self.badges[ch.index].scaled(54, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        self.status_badge.setText("CONNECTING...")
        self.setWindowTitle(f"{ch.name} — Pi IPTV")

        for row in range(self.list_widget.count()):
            it = self.list_widget.item(row)
            if it.data(Qt.UserRole) == ch.index:
                self.list_widget.setCurrentRow(row)
                self.list_widget.scrollToItem(it)
                break

        if self.mpv and self._mpv_ready:
            self.mpv.play(ch.url, ch.user_agent)

    def play_next(self):
        pool = self.filtered_channels or self.channels
        if not pool:
            return
        if not self.current_channel:
            self.play_channel(pool[0])
            return
        ids = [c.index for c in pool]
        pos = ids.index(self.current_channel.index) if self.current_channel.index in ids else -1
        self.play_channel(pool[(pos + 1) % len(pool)])

    def play_prev(self):
        pool = self.filtered_channels or self.channels
        if not pool:
            return
        if not self.current_channel:
            self.play_channel(pool[-1])
            return
        ids = [c.index for c in pool]
        pos = ids.index(self.current_channel.index) if self.current_channel.index in ids else 0
        self.play_channel(pool[(pos - 1) % len(pool)])

    def toggle_pause(self):
        if self.mpv:
            self.mpv.toggle_pause()

    def toggle_mute(self):
        if self.mpv:
            self.mpv.toggle_mute()

    def _on_vol_slider(self, val: int):
        if not self._updating_vol_slider and self.mpv:
            self.mpv.set_volume(val)

    def adjust_volume(self, delta: int):
        new_val = max(0, min(120, self.vol_slider.value() + delta))
        self.vol_slider.setValue(new_val)

    def toggle_sidebar(self):
        if self.isFullScreen():
            return
        self.sidebar.setVisible(not self.sidebar.isVisible())

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.showMaximized()
            self.controls_bar.setVisible(True)
            self.sidebar.setVisible(self.sidebar_visible_before_fs)
        else:
            self.sidebar_visible_before_fs = self.sidebar.isVisible()
            self.sidebar.setVisible(False)
            self.controls_bar.setVisible(False)
            self.showFullScreen()

    def _exit_fullscreen_or_clear(self):
        if self.isFullScreen():
            self.toggle_fullscreen()
        elif self.search_input.hasFocus() and self.search_input.text():
            self.search_input.clear()
        else:
            self.list_widget.setFocus()

    def _focus_search(self):
        if not self.sidebar.isVisible() and not self.isFullScreen():
            self.sidebar.setVisible(True)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def closeEvent(self, event):
        self.logo_loader.stop()
        if self.mpv:
            self.mpv.stop()
        super().closeEvent(event)


def main():
    playlist = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLAYLIST
    app = QApplication(sys.argv)
    app.setApplicationName("Pi IPTV")
    app.setStyleSheet(DARK_QSS)
    win = IptvWindow(playlist)
    win.showMaximized()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
