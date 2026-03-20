import sys
import os
import json
import base64
import io
import threading
import winreg
import ctypes
import ctypes.wintypes

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QSystemTrayIcon,
    QMenu, QAction, QMessageBox, QGroupBox, QKeySequenceEdit,
    QTextEdit, QGraphicsDropShadowEffect, QSpacerItem, QSizePolicy,
    QFrame, QButtonGroup
)
from PyQt5.QtCore import Qt, QRect, pyqtSignal, QPoint, QTimer, QByteArray, QBuffer, QIODevice, QSize
from PyQt5.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QKeySequence,
    QPen, QBrush, QScreen, QCursor, QLinearGradient, QPainterPath,
    QFontDatabase
)

import keyboard
import mss
from PIL import Image
from openai import OpenAI


APP_NAME = "AI Answer"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", ""), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

PROVIDERS = {
    "groq": {
        "api_key": "",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
    },
    "ollama": {
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "gemma3:4b",
    },
}

DEFAULT_CONFIG = {
    "provider": "groq",
    "api_key": "",
    "base_url": PROVIDERS["groq"]["base_url"],
    "hotkey": "ctrl+shift+s",
    "autostart": False,
    "model": PROVIDERS["groq"]["model"],
    "prompt": "You are a solver. Look at the image and give ONLY the answer. Do NOT describe the image. If there are math problems — solve them and write the answers. If there is a question — answer it. If there is a task or exercise — complete it. Reply in the language of the text on the image. Be short."
}

# ── Windows Acrylic Blur ──────────────────────────────────────────

class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]

class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.POINTER(ACCENT_POLICY)),
        ("SizeOfData", ctypes.c_size_t),
    ]

def enable_acrylic(hwnd, color=0xCC1a1a2e):
    """Enable Windows acrylic blur behind a window. color = AABBGGRR"""
    accent = ACCENT_POLICY()
    accent.AccentState = 4  # ACCENT_ENABLE_ACRYLICBLURBEHIND
    accent.AccentFlags = 2
    accent.GradientColor = color
    data = WINDOWCOMPOSITIONATTRIBDATA()
    data.Attribute = 19  # WCA_ACCENT_POLICY
    data.Data = ctypes.pointer(accent)
    data.SizeOfData = ctypes.sizeof(accent)
    try:
        ctypes.windll.user32.SetWindowCompositionAttribute(
            ctypes.wintypes.HWND(hwnd), ctypes.pointer(data)
        )
    except Exception:
        pass


def enable_mica(hwnd):
    """Enable Mica effect on Windows 11."""
    try:
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.wintypes.HWND(hwnd), 38,
            ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass


# ── Config ────────────────────────────────────────────────────────

def load_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def set_autostart(enable):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enable:
            exe_path = sys.executable
            if getattr(sys, 'frozen', False):
                exe_path = sys.executable
            else:
                exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Autostart error: {e}")


# ── Hotkey capture widget ─────────────────────────────────────────

class HotkeyEdit(QLineEdit):
    """A line edit that captures key combinations by pressing them."""

    KEY_MAP = {
        Qt.Key_Control: "ctrl", Qt.Key_Shift: "shift",
        Qt.Key_Alt: "alt", Qt.Key_Meta: "win",
        Qt.Key_Tab: "tab", Qt.Key_Return: "enter",
        Qt.Key_Enter: "enter", Qt.Key_Backspace: "backspace",
        Qt.Key_Delete: "delete", Qt.Key_Escape: "esc",
        Qt.Key_Space: "space", Qt.Key_Up: "up",
        Qt.Key_Down: "down", Qt.Key_Left: "left",
        Qt.Key_Right: "right", Qt.Key_Home: "home",
        Qt.Key_End: "end", Qt.Key_PageUp: "page up",
        Qt.Key_PageDown: "page down", Qt.Key_Insert: "insert",
        Qt.Key_F1: "f1", Qt.Key_F2: "f2", Qt.Key_F3: "f3",
        Qt.Key_F4: "f4", Qt.Key_F5: "f5", Qt.Key_F6: "f6",
        Qt.Key_F7: "f7", Qt.Key_F8: "f8", Qt.Key_F9: "f9",
        Qt.Key_F10: "f10", Qt.Key_F11: "f11", Qt.Key_F12: "f12",
        Qt.Key_BracketLeft: "[", Qt.Key_BracketRight: "]",
        Qt.Key_Semicolon: ";", Qt.Key_Apostrophe: "'",
        Qt.Key_Comma: ",", Qt.Key_Period: ".",
        Qt.Key_Slash: "/", Qt.Key_Backslash: "\\",
        Qt.Key_Minus: "-", Qt.Key_Equal: "=",
        Qt.Key_QuoteLeft: "`",
    }

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setPlaceholderText("Click and press keys...")
        self._recording = False

    def mousePressEvent(self, event):
        self._recording = True
        self.setPlaceholderText("Press hotkey combo...")
        self.setText("")
        self.setStyleSheet(self.styleSheet())  # refresh
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if not self._recording:
            return

        key = event.key()
        # ignore lone modifier presses
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return

        parts = []
        mods = event.modifiers()
        if mods & Qt.ControlModifier:
            parts.append("ctrl")
        if mods & Qt.AltModifier:
            parts.append("alt")
        if mods & Qt.ShiftModifier:
            parts.append("shift")
        if mods & Qt.MetaModifier:
            parts.append("win")

        if key in self.KEY_MAP:
            parts.append(self.KEY_MAP[key])
        elif 0x20 <= key <= 0x7e:
            parts.append(chr(key).lower())
        else:
            parts.append(f"key{key}")

        combo = "+".join(parts)
        self.setText(combo)
        self._recording = False
        self.setPlaceholderText("Click and press keys...")
        self.clearFocus()

    def focusOutEvent(self, event):
        self._recording = False
        super().focusOutEvent(event)


# ── Screenshot selection overlay ──────────────────────────────────

class ScreenshotOverlay(QWidget):
    """Fullscreen transparent overlay for selecting a screen region."""
    area_selected = pyqtSignal(QRect, QPixmap)
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowState(Qt.WindowFullScreen)
        self.setCursor(Qt.CrossCursor)

        self._origin = QPoint()
        self._current = QPoint()
        self._selecting = False
        self._screenshot = None

    def start(self):
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            pixmap = QPixmap()
            pixmap.loadFromData(buf.getvalue())
            self._screenshot = pixmap

        screen = QApplication.primaryScreen()
        geom = screen.geometry()
        self.setGeometry(geom)
        self.showFullScreen()
        self.activateWindow()
        self.raise_()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._screenshot:
            painter.drawPixmap(0, 0, self._screenshot)

        # dim overlay
        painter.setBrush(QColor(0, 0, 0, 100))
        painter.setPen(Qt.NoPen)
        painter.drawRect(self.rect())

        if self._selecting and not self._origin.isNull():
            rect = QRect(self._origin, self._current).normalized()

            # clear selected area
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(rect, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

            # draw screenshot in selected area
            if self._screenshot:
                painter.drawPixmap(rect, self._screenshot, rect)

            # selection border with glow
            pen = QPen(QColor(100, 180, 255), 2, Qt.SolidLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)

            # corner handles
            handle = 6
            painter.setBrush(QColor(100, 180, 255))
            painter.setPen(Qt.NoPen)
            corners = [
                rect.topLeft(), rect.topRight() + QPoint(-handle, 0),
                rect.bottomLeft() + QPoint(0, -handle), rect.bottomRight() + QPoint(-handle, -handle)
            ]
            for c in corners:
                painter.drawRect(QRect(c, QSize(handle, handle)))

            # size label
            label = f"{rect.width()} x {rect.height()}"
            font = QFont("Segoe UI", 10)
            painter.setFont(font)
            lx = rect.left() + 4
            ly = rect.top() - 8
            if ly < 20:
                ly = rect.top() + 20
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(label) + 12
            painter.drawRoundedRect(lx - 2, ly - fm.height(), tw, fm.height() + 4, 4, 4)
            painter.setPen(QColor(200, 220, 255))
            painter.drawText(lx + 4, ly - 2, label)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._origin = event.pos()
            self._current = event.pos()
            self._selecting = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._current = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            rect = QRect(self._origin, event.pos()).normalized()
            if rect.width() > 10 and rect.height() > 10 and self._screenshot:
                cropped = self._screenshot.copy(rect)
                self.hide()
                self.area_selected.emit(rect, cropped)
            else:
                self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            self.cancelled.emit()


# ── Result overlay (frosted glass) ────────────────────────────────

class ResultOverlay(QWidget):
    """Shows the AI response in a frosted glass overlay."""
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)

        self._drag_pos = None
        self._handle_height = 40

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # drag handle bar
        self._handle = QWidget()
        self._handle.setFixedHeight(self._handle_height)
        self._handle.setCursor(Qt.OpenHandCursor)
        self._handle.setStyleSheet("background: transparent;")
        layout.addWidget(self._handle)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setStyleSheet("""
            QTextEdit {
                background-color: transparent;
                color: #ffffff;
                border: none;
                padding: 4px 12px 12px 12px;
                font-size: 14px;
                font-family: 'Segoe UI', sans-serif;
                selection-background-color: rgba(100, 180, 255, 100);
            }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
                margin: 4px 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 60);
                border-radius: 3px;
                min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)
        layout.addWidget(self._text)

        self._esc_hook = None

    def _apply_acrylic(self):
        hwnd = int(self.winId())
        # nearly clear frosted glass — page clearly visible through blur
        # format AABBGGRR: alpha=0x15 (~8%), very light tint
        enable_acrylic(hwnd, 0x15151015)

    def _update_mask(self):
        """Round the window corners by clipping with a mask."""
        from PyQt5.QtGui import QRegion, QBitmap
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 12, 12)
        bmp = QBitmap(self.size())
        bmp.fill(Qt.color0)
        p = QPainter(bmp)
        p.setBrush(Qt.color1)
        p.setPen(Qt.NoPen)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPath(path)
        p.end()
        self.setMask(bmp)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # subtle border only — acrylic does the background
        pen = QPen(QColor(255, 255, 255, 30), 1)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 12, 12)

        # drag handle grip dots
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 70))
        grip_y = self._handle_height // 2
        grip_w = 40
        grip_x = (self.width() - grip_w) // 2
        painter.drawRoundedRect(grip_x, grip_y - 2, grip_w, 4, 2, 2)

        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_mask()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_mask()
        QTimer.singleShot(10, self._apply_acrylic)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.pos().y() <= self._handle_height:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            self._handle.setCursor(Qt.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self._handle.setCursor(Qt.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def _hook_escape(self):
        self._unhook_escape()
        self._esc_hook = keyboard.on_press_key("esc", lambda _: QTimer.singleShot(0, self._close_overlay), suppress=False)

    def _unhook_escape(self):
        if self._esc_hook is not None:
            keyboard.unhook(self._esc_hook)
            self._esc_hook = None

    def _close_overlay(self):
        self._unhook_escape()
        self.hide()
        self.closed.emit()

    def _position_rect(self, rect, min_w, min_h):
        """Position overlay to the right of the screenshot rect."""
        screen = QApplication.primaryScreen().geometry()
        gap = 12
        w = max(min_w, 340)
        h = max(min_h, rect.height())

        # try right side
        x = rect.right() + gap
        y = rect.top()

        # if it goes off screen right, try left side
        if x + w > screen.right():
            x = rect.left() - w - gap

        # if still off screen, fallback to overlapping
        if x < screen.left():
            x = rect.left()

        # clamp vertically
        if y + h > screen.bottom():
            y = screen.bottom() - h
        if y < screen.top():
            y = screen.top()

        return QRect(x, y, w, h)

    def show_result(self, rect: QRect, text: str):
        r = self._position_rect(rect, 320, 120)
        self.setGeometry(r)
        self._text.setText(text)
        self.show()
        self.activateWindow()
        self.raise_()
        self._hook_escape()

    def show_loading(self, rect: QRect):
        r = self._position_rect(rect, 260, 80)
        self.setGeometry(r)
        self._text.setAlignment(Qt.AlignCenter)
        self._text.setText("Analyzing...")
        self.show()
        self.activateWindow()
        self.raise_()
        self._hook_escape()

    def hideEvent(self, event):
        self._unhook_escape()
        super().hideEvent(event)


# ── Settings window ───────────────────────────────────────────────

SETTINGS_STYLE = """
QMainWindow {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #0d0d1a, stop:0.5 #141428, stop:1 #0d0d1a);
}

QLabel {
    color: rgba(255, 255, 255, 0.7);
    font-size: 12px;
    font-family: 'Segoe UI', sans-serif;
}

QLabel#title {
    color: #ffffff;
    font-size: 22px;
    font-weight: bold;
    font-family: 'Segoe UI', sans-serif;
}

QLabel#subtitle {
    color: rgba(255, 255, 255, 0.4);
    font-size: 12px;
    font-family: 'Segoe UI', sans-serif;
}

QLineEdit {
    background-color: rgba(255, 255, 255, 0.06);
    color: #e0e0e0;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    font-family: 'Segoe UI', sans-serif;
    selection-background-color: rgba(100, 140, 255, 0.4);
}
QLineEdit:focus {
    border: 1px solid rgba(100, 140, 255, 0.5);
    background-color: rgba(255, 255, 255, 0.08);
}
QLineEdit:hover {
    background-color: rgba(255, 255, 255, 0.08);
}

QTextEdit {
    background-color: rgba(255, 255, 255, 0.06);
    color: #e0e0e0;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    font-family: 'Segoe UI', sans-serif;
    selection-background-color: rgba(100, 140, 255, 0.4);
}
QTextEdit:focus {
    border: 1px solid rgba(100, 140, 255, 0.5);
    background-color: rgba(255, 255, 255, 0.08);
}

QCheckBox {
    color: rgba(255, 255, 255, 0.8);
    spacing: 10px;
    font-size: 13px;
    font-family: 'Segoe UI', sans-serif;
}
QCheckBox::indicator {
    width: 20px; height: 20px;
    border: 2px solid rgba(255, 255, 255, 0.2);
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.05);
}
QCheckBox::indicator:hover {
    border-color: rgba(100, 140, 255, 0.5);
    background: rgba(100, 140, 255, 0.1);
}
QCheckBox::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #667eea, stop:1 #764ba2);
    border-color: transparent;
    image: none;
}

QPushButton#save_btn {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #667eea, stop:1 #764ba2);
    color: white;
    border: none;
    border-radius: 10px;
    padding: 12px 32px;
    font-weight: 600;
    font-size: 14px;
    font-family: 'Segoe UI', sans-serif;
}
QPushButton#save_btn:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #7b8ef8, stop:1 #8b5fbf);
}
QPushButton#save_btn:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #5a6fd6, stop:1 #6a3f92);
}

QFrame#separator {
    background: rgba(255, 255, 255, 0.06);
    max-height: 1px;
}

QFrame#card {
    background-color: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
}

QPushButton#toggle_btn {
    background-color: rgba(255, 255, 255, 0.06);
    color: rgba(255, 255, 255, 0.5);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    padding: 10px 20px;
    font-size: 13px;
    font-weight: 600;
    font-family: 'Segoe UI', sans-serif;
}
QPushButton#toggle_btn:hover {
    background-color: rgba(255, 255, 255, 0.1);
    color: rgba(255, 255, 255, 0.7);
}
QPushButton#toggle_btn:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #667eea, stop:1 #764ba2);
    color: white;
    border: none;
}
"""

TRAY_MENU_STYLE = """
QMenu {
    background-color: #1a1a2e;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    padding: 6px;
    font-family: 'Segoe UI', sans-serif;
}
QMenu::item {
    color: rgba(255, 255, 255, 0.8);
    padding: 8px 24px 8px 12px;
    border-radius: 6px;
    font-size: 13px;
}
QMenu::item:selected {
    background: rgba(100, 140, 255, 0.2);
    color: #ffffff;
}
QMenu::separator {
    height: 1px;
    background: rgba(255, 255, 255, 0.08);
    margin: 4px 8px;
}
"""


class SettingsWindow(QMainWindow):
    hotkey_changed = pyqtSignal(str)
    config_saved = pyqtSignal(dict)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.setWindowTitle(f"{APP_NAME}")
        self.setFixedSize(480, 700)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowMaximizeButtonHint)
        self.setStyleSheet(SETTINGS_STYLE)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(4)
        main_layout.setContentsMargins(28, 24, 28, 24)

        # ── Header ──
        title = QLabel("AI Answer")
        title.setObjectName("title")
        main_layout.addWidget(title)

        subtitle = QLabel("Screenshot to AI-powered answer")
        subtitle.setObjectName("subtitle")
        main_layout.addWidget(subtitle)

        main_layout.addSpacing(20)

        # ── Provider toggle ──
        main_layout.addWidget(self._section_label("PROVIDER"))
        main_layout.addSpacing(4)
        toggle_layout = QHBoxLayout()
        toggle_layout.setSpacing(8)

        self.btn_groq = QPushButton("☁  Groq (Cloud)")
        self.btn_groq.setObjectName("toggle_btn")
        self.btn_groq.setCheckable(True)
        self.btn_groq.setCursor(Qt.PointingHandCursor)

        self.btn_ollama = QPushButton("💻  Ollama (Local)")
        self.btn_ollama.setObjectName("toggle_btn")
        self.btn_ollama.setCheckable(True)
        self.btn_ollama.setCursor(Qt.PointingHandCursor)

        self._provider_group = QButtonGroup(self)
        self._provider_group.setExclusive(True)
        self._provider_group.addButton(self.btn_groq, 0)
        self._provider_group.addButton(self.btn_ollama, 1)

        current_provider = self.config.get("provider", "groq")
        if current_provider == "ollama":
            self.btn_ollama.setChecked(True)
        else:
            self.btn_groq.setChecked(True)

        self.btn_groq.clicked.connect(lambda: self._switch_provider("groq"))
        self.btn_ollama.clicked.connect(lambda: self._switch_provider("ollama"))

        toggle_layout.addWidget(self.btn_groq)
        toggle_layout.addWidget(self.btn_ollama)
        main_layout.addLayout(toggle_layout)

        main_layout.addSpacing(14)

        # ── Base URL ──
        main_layout.addWidget(self._section_label("BASE URL"))
        main_layout.addSpacing(4)
        self.base_url_input = QLineEdit(self.config.get("base_url", DEFAULT_CONFIG["base_url"]))
        self.base_url_input.setPlaceholderText("http://localhost:11434/v1")
        main_layout.addWidget(self.base_url_input)

        main_layout.addSpacing(14)

        # ── API Key ──
        main_layout.addWidget(self._section_label("API KEY"))
        main_layout.addSpacing(4)
        self.api_key_input = QLineEdit(self.config.get("api_key", ""))
        self.api_key_input.setPlaceholderText("API key")
        self.api_key_input.setEchoMode(QLineEdit.Password)
        main_layout.addWidget(self.api_key_input)

        main_layout.addSpacing(14)

        # ── Model ──
        main_layout.addWidget(self._section_label("MODEL"))
        main_layout.addSpacing(4)
        self.model_input = QLineEdit(self.config.get("model", DEFAULT_CONFIG["model"]))
        self.model_input.setPlaceholderText("model name")
        main_layout.addWidget(self.model_input)

        main_layout.addSpacing(14)

        # ── Prompt ──
        main_layout.addWidget(self._section_label("PROMPT"))
        main_layout.addSpacing(4)
        self.prompt_input = QTextEdit()
        self.prompt_input.setFixedHeight(68)
        self.prompt_input.setText(self.config.get("prompt", DEFAULT_CONFIG["prompt"]))
        main_layout.addWidget(self.prompt_input)

        main_layout.addSpacing(14)

        # ── Hotkey ──
        main_layout.addWidget(self._section_label("HOTKEY"))
        main_layout.addSpacing(4)
        self.hotkey_input = HotkeyEdit(self.config.get("hotkey", DEFAULT_CONFIG["hotkey"]))
        self.hotkey_input.setPlaceholderText("Click and press keys...")
        main_layout.addWidget(self.hotkey_input)

        main_layout.addSpacing(16)

        # ── Separator ──
        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFrameShape(QFrame.HLine)
        main_layout.addWidget(sep)

        main_layout.addSpacing(12)

        # ── Autostart ──
        self.autostart_cb = QCheckBox("  Launch on Windows startup")
        self.autostart_cb.setChecked(self.config.get("autostart", False))
        main_layout.addWidget(self.autostart_cb)

        main_layout.addStretch()

        # ── Save button ──
        save_btn = QPushButton("Save")
        save_btn.setObjectName("save_btn")
        save_btn.setFixedHeight(44)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.clicked.connect(self._save)
        main_layout.addWidget(save_btn)

    def _section_label(self, text):
        lbl = QLabel(text)
        lbl.setFixedHeight(16)
        lbl.setStyleSheet("""
            color: rgba(255, 255, 255, 0.35);
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1.5px;
            font-family: 'Segoe UI', sans-serif;
            padding: 0;
            margin: 0;
        """)
        return lbl

    def _switch_provider(self, provider):
        preset = PROVIDERS[provider]
        self.base_url_input.setText(preset["base_url"])
        self.api_key_input.setText(preset["api_key"])
        self.model_input.setText(preset["model"])

    def _save(self):
        self.config["provider"] = "ollama" if self.btn_ollama.isChecked() else "groq"
        self.config["base_url"] = self.base_url_input.text().strip() or DEFAULT_CONFIG["base_url"]
        self.config["api_key"] = self.api_key_input.text().strip()
        self.config["model"] = self.model_input.text().strip()
        self.config["prompt"] = self.prompt_input.toPlainText().strip()
        new_hotkey = self.hotkey_input.text().strip().lower()
        old_hotkey = self.config.get("hotkey", "")
        self.config["hotkey"] = new_hotkey
        self.config["autostart"] = self.autostart_cb.isChecked()

        save_config(self.config)
        set_autostart(self.config["autostart"])

        # always re-register hotkey on save
        self.hotkey_changed.emit(new_hotkey)

        self.config_saved.emit(self.config)
        self.hide()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ── Main application ─────────────────────────────────────────────

class AIAnswerApp(QApplication):
    trigger_screenshot = pyqtSignal()
    _groq_result = pyqtSignal(QRect, str)

    def __init__(self, argv):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)

        self.config = load_config()
        save_config(self.config)

        self._settings = SettingsWindow(self.config)
        self._settings.hotkey_changed.connect(self._register_hotkey)
        self._settings.config_saved.connect(self._on_config_saved)

        self._overlay = ScreenshotOverlay()
        self._overlay.area_selected.connect(self._on_area_selected)
        self._overlay.cancelled.connect(self._on_cancelled)

        self._result = ResultOverlay()
        self._result.closed.connect(self._on_result_closed)

        self.trigger_screenshot.connect(self._start_screenshot)
        self._groq_result.connect(self._on_groq_result)

        self._setup_tray()
        self._register_hotkey(self.config.get("hotkey", DEFAULT_CONFIG["hotkey"]))

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(self._make_icon())
        self._tray.setToolTip(APP_NAME)

        menu = QMenu()
        menu.setStyleSheet(TRAY_MENU_STYLE)

        action_screenshot = QAction("  Screenshot", menu)
        action_screenshot.triggered.connect(self._start_screenshot)
        menu.addAction(action_screenshot)

        action_settings = QAction("  Settings", menu)
        action_settings.triggered.connect(self._show_settings)
        menu.addAction(action_settings)

        menu.addSeparator()

        action_quit = QAction("  Quit", menu)
        action_quit.triggered.connect(self._quit)
        menu.addAction(action_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _make_icon(self):
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        # gradient background
        grad = QLinearGradient(0, 0, 64, 64)
        grad.setColorAt(0, QColor(102, 126, 234))
        grad.setColorAt(1, QColor(118, 75, 162))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(4, 4, 56, 56, 14, 14)

        painter.setPen(QColor(255, 255, 255))
        font = QFont("Segoe UI", 22, QFont.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "AI")
        painter.end()
        return QIcon(pixmap)

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_settings()

    def _show_settings(self):
        self._settings.show()
        self._settings.activateWindow()
        self._settings.raise_()

    def _register_hotkey(self, hotkey):
        if not hotkey:
            return
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        try:
            keyboard.add_hotkey(hotkey, self._on_hotkey_pressed)
            print(f"Hotkey registered: {hotkey}")
        except Exception as e:
            print(f"Hotkey registration error: {e}")

    def _on_hotkey_pressed(self):
        self.trigger_screenshot.emit()

    def _start_screenshot(self):
        self._result.hide()
        QTimer.singleShot(150, self._overlay.start)

    def _on_area_selected(self, rect: QRect, pixmap: QPixmap):
        self._result.show_loading(rect)

        ba = QByteArray()
        buffer = QBuffer(ba)
        buffer.open(QIODevice.WriteOnly)
        pixmap.save(buffer, "PNG")
        buffer.close()
        b64 = base64.b64encode(ba.data()).decode("utf-8")

        if not b64:
            self._result.show_result(rect, "Error: Failed to capture screenshot")
            return

        thread = threading.Thread(
            target=self._call_groq, args=(rect, b64), daemon=True
        )
        thread.start()

    def _call_groq(self, rect, b64_image):
        try:
            api_key = self.config.get("api_key", "") or "ollama"
            base_url = self.config.get("base_url", DEFAULT_CONFIG["base_url"])
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
            completion = client.chat.completions.create(
                model=self.config.get("model", DEFAULT_CONFIG["model"]),
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": self.config.get("prompt", DEFAULT_CONFIG["prompt"]),
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{b64_image}",
                                },
                            },
                        ],
                    }
                ],
                temperature=0.5,
                max_tokens=1024,
            )
            answer = completion.choices[0].message.content
        except Exception as e:
            answer = f"Error: {e}"

        self._groq_result.emit(rect, answer)

    def _on_groq_result(self, rect, text):
        self._result.show_result(rect, text)

    def _on_cancelled(self):
        pass

    def _on_result_closed(self):
        pass

    def _on_config_saved(self, cfg):
        self.config = cfg

    def _quit(self):
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self._tray.hide()
        self.quit()


def main():
    app = AIAnswerApp(sys.argv)
    app._show_settings()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
