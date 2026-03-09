import sys
import os
import json
import base64
import io
import threading
import winreg

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QSystemTrayIcon,
    QMenu, QAction, QMessageBox, QGroupBox, QKeySequenceEdit,
    QTextEdit
)
from PyQt5.QtCore import Qt, QRect, pyqtSignal, QPoint, QTimer
from PyQt5.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QKeySequence,
    QPen, QBrush, QScreen, QCursor
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
    "prompt": "Describe what you see in this image. Answer in the same language as any text visible in the image. Be concise."
}


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

        painter.setBrush(QColor(0, 0, 0, 100))
        painter.setPen(Qt.NoPen)
        painter.drawRect(self.rect())

        if self._selecting and not self._origin.isNull():
            rect = QRect(self._origin, self._current).normalized()
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(rect, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

            if self._screenshot:
                painter.drawPixmap(rect, self._screenshot, rect)

            pen = QPen(QColor(0, 150, 255), 2, Qt.SolidLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)

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


# ── Result overlay ─────────────────────────────────────────────────

class ResultOverlay(QWidget):
    """Shows the AI response text in the selected screen region."""
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setStyleSheet("""
            QTextEdit {
                background-color: rgba(30, 30, 30, 240);
                color: #e0e0e0;
                border: 2px solid #0096ff;
                border-radius: 6px;
                padding: 10px;
                font-size: 14px;
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        layout.addWidget(self._text)

    def show_result(self, rect: QRect, text: str):
        self.setGeometry(rect)
        self._text.setText(text)
        self.show()
        self.activateWindow()
        self.raise_()

    def show_loading(self, rect: QRect):
        self.setGeometry(rect)
        self._text.setText("⏳ Analyzing...")
        self._text.setAlignment(Qt.AlignCenter)
        self.show()
        self.activateWindow()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            self.closed.emit()

    def focusOutEvent(self, event):
        pass


# ── Settings window ───────────────────────────────────────────────

class SettingsWindow(QMainWindow):
    hotkey_changed = pyqtSignal(str)
    config_saved = pyqtSignal(dict)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.setWindowTitle(f"{APP_NAME} — Settings")
        self.setFixedSize(500, 420)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowMaximizeButtonHint)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(12)

        # ── API group ──
        api_group = QGroupBox("API")
        api_layout = QVBoxLayout()
        api_layout.addWidget(QLabel("Groq API Key:"))
        self.api_key_input = QLineEdit(self.config.get("api_key", ""))
        self.api_key_input.setEchoMode(QLineEdit.Password)
        api_layout.addWidget(self.api_key_input)

        api_layout.addWidget(QLabel("Model:"))
        self.model_input = QLineEdit(self.config.get("model", DEFAULT_CONFIG["model"]))
        api_layout.addWidget(self.model_input)

        api_layout.addWidget(QLabel("System prompt:"))
        self.prompt_input = QTextEdit()
        self.prompt_input.setMaximumHeight(60)
        self.prompt_input.setText(self.config.get("prompt", DEFAULT_CONFIG["prompt"]))
        api_layout.addWidget(self.prompt_input)

        api_group.setLayout(api_layout)
        main_layout.addWidget(api_group)

        # ── Hotkey group ──
        hk_group = QGroupBox("Hotkey")
        hk_layout = QHBoxLayout()
        hk_layout.addWidget(QLabel("Screenshot hotkey:"))
        self.hotkey_input = QLineEdit(self.config.get("hotkey", DEFAULT_CONFIG["hotkey"]))
        hk_layout.addWidget(self.hotkey_input)
        hk_group.setLayout(hk_layout)
        main_layout.addWidget(hk_group)

        # ── Autostart ──
        self.autostart_cb = QCheckBox("Launch on Windows startup")
        self.autostart_cb.setChecked(self.config.get("autostart", False))
        main_layout.addWidget(self.autostart_cb)

        # ── Buttons ──
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.setFixedHeight(36)
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)
        main_layout.addLayout(btn_layout)

        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #1e1e1e; }
            QGroupBox {
                color: #cccccc;
                border: 1px solid #444;
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 14px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QLabel { color: #bbbbbb; }
            QLineEdit, QTextEdit {
                background-color: #2d2d2d;
                color: #e0e0e0;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 6px;
            }
            QCheckBox { color: #bbbbbb; spacing: 8px; }
            QCheckBox::indicator {
                width: 18px; height: 18px;
                border: 1px solid #555;
                border-radius: 3px;
                background: #2d2d2d;
            }
            QCheckBox::indicator:checked {
                background: #0096ff;
                border-color: #0096ff;
            }
            QPushButton {
                background-color: #0096ff;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 20px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #0078d4; }
        """)

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

        self._setup_tray()
        self._register_hotkey(self.config.get("hotkey", DEFAULT_CONFIG["hotkey"]))

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(self._make_icon())
        self._tray.setToolTip(APP_NAME)

        menu = QMenu()
        action_screenshot = QAction("Take Screenshot", menu)
        action_screenshot.triggered.connect(self._start_screenshot)
        menu.addAction(action_screenshot)

        action_settings = QAction("Settings", menu)
        action_settings.triggered.connect(self._show_settings)
        menu.addAction(action_settings)

        menu.addSeparator()

        action_quit = QAction("Quit", menu)
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
        painter.setBrush(QColor(0, 150, 255))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(4, 4, 56, 56, 12, 12)
        painter.setPen(QColor(255, 255, 255))
        font = QFont("Segoe UI", 28, QFont.Bold)
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

        buf = io.BytesIO()
        pixmap.save(buf, "PNG")
        img_bytes = buf.getvalue()
        b64 = base64.b64encode(img_bytes).decode("utf-8")

        thread = threading.Thread(
            target=self._call_groq, args=(rect, b64), daemon=True
        )
        thread.start()

    def _call_groq(self, rect, b64_image):
        try:
            client = Groq(api_key=self.config.get("api_key", ""))
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

        QTimer.singleShot(0, lambda: self._result.show_result(rect, answer))

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
