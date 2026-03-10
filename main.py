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
    QFrame
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
from groq import Groq


APP_NAME = "AI Answer"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", ""), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "api_key": "",
    "hotkey": "ctrl+shift+s",
    "autostart": False,
    "model": "meta-llama/llama-4-scout-17b-16e-instruct",
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setStyleSheet("""
            QTextEdit {
                background-color: transparent;
                color: #ffffff;
                border: none;
                padding: 16px;
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

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # fallback glass background
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 12, 12)
        painter.setClipPath(path)

        painter.setBrush(QColor(20, 20, 35, 180))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 12, 12)

        # subtle border
        pen = QPen(QColor(255, 255, 255, 40), 1)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 12, 12)

        # top highlight line
        grad = QLinearGradient(0, 0, self.width(), 0)
        grad.setColorAt(0, QColor(255, 255, 255, 0))
        grad.setColorAt(0.5, QColor(255, 255, 255, 30))
        grad.setColorAt(1, QColor(255, 255, 255, 0))
        painter.setPen(QPen(QBrush(grad), 1))
        painter.drawLine(20, 1, self.width() - 20, 1)

        painter.end()

    def showEvent(self, event):
        super().showEvent(event)
        # enable acrylic blur via Windows API
        hwnd = int(self.winId())
        # AABBGGRR: semi-transparent dark blue-ish
        enable_acrylic(hwnd, 0xB01a1a2e)

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

    def show_result(self, rect: QRect, text: str):
        min_w, min_h = 320, 120
        r = QRect(rect)
        if r.width() < min_w:
            r.setWidth(min_w)
        if r.height() < min_h:
            r.setHeight(min_h)
        self.setGeometry(r)
        self._text.setText(text)
        self.show()
        self.activateWindow()
        self.raise_()
        self._hook_escape()

    def show_loading(self, rect: QRect):
        min_w, min_h = 260, 80
        r = QRect(rect)
        if r.width() < min_w:
            r.setWidth(min_w)
        if r.height() < min_h:
            r.setHeight(min_h)
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
        self.setFixedSize(480, 520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowMaximizeButtonHint)
        self.setStyleSheet(SETTINGS_STYLE)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(6)
        main_layout.setContentsMargins(28, 24, 28, 24)

        # ── Header ──
        title = QLabel("AI Answer")
        title.setObjectName("title")
        main_layout.addWidget(title)

        subtitle = QLabel("Screenshot to AI-powered answer")
        subtitle.setObjectName("subtitle")
        main_layout.addWidget(subtitle)

        main_layout.addSpacing(16)

        # ── API Key ──
        main_layout.addWidget(self._section_label("API KEY"))
        self.api_key_input = QLineEdit(self.config.get("api_key", ""))
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("gsk_...")
        main_layout.addWidget(self.api_key_input)

        main_layout.addSpacing(10)

        # ── Model ──
        main_layout.addWidget(self._section_label("MODEL"))
        self.model_input = QLineEdit(self.config.get("model", DEFAULT_CONFIG["model"]))
        self.model_input.setPlaceholderText("meta-llama/llama-4-scout-17b-16e-instruct")
        main_layout.addWidget(self.model_input)

        main_layout.addSpacing(10)

        # ── Prompt ──
        main_layout.addWidget(self._section_label("PROMPT"))
        self.prompt_input = QTextEdit()
        self.prompt_input.setFixedHeight(64)
        self.prompt_input.setText(self.config.get("prompt", DEFAULT_CONFIG["prompt"]))
        main_layout.addWidget(self.prompt_input)

        main_layout.addSpacing(10)

        # ── Hotkey ──
        main_layout.addWidget(self._section_label("HOTKEY"))
        self.hotkey_input = QLineEdit(self.config.get("hotkey", DEFAULT_CONFIG["hotkey"]))
        self.hotkey_input.setPlaceholderText("ctrl+shift+s")
        main_layout.addWidget(self.hotkey_input)

        main_layout.addSpacing(12)

        # ── Separator ──
        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFrameShape(QFrame.HLine)
        main_layout.addWidget(sep)

        main_layout.addSpacing(8)

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
        lbl.setStyleSheet("""
            color: rgba(255, 255, 255, 0.35);
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1.5px;
            font-family: 'Segoe UI', sans-serif;
            margin-bottom: 2px;
        """)
        return lbl

    def _save(self):
        self.config["api_key"] = self.api_key_input.text().strip()
        self.config["model"] = self.model_input.text().strip()
        self.config["prompt"] = self.prompt_input.toPlainText().strip()
        new_hotkey = self.hotkey_input.text().strip().lower()
        old_hotkey = self.config.get("hotkey", "")
        self.config["hotkey"] = new_hotkey
        self.config["autostart"] = self.autostart_cb.isChecked()

        save_config(self.config)
        set_autostart(self.config["autostart"])

        if new_hotkey != old_hotkey:
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
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        try:
            keyboard.add_hotkey(hotkey, self._on_hotkey_pressed, suppress=True)
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
            api_key = self.config.get("api_key", "")
            if not api_key:
                self._groq_result.emit(rect, "Error: API key not set. Open Settings and enter your Groq API key.")
                return
            client = Groq(api_key=api_key, timeout=30.0)
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
                max_completion_tokens=1024,
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
