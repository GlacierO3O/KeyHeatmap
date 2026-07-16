"""
键盘热力图 KeyHeatmap v3.2
后台记录全局按键次数，托盘管理，浏览器展示热力图
右下角实时按键浮窗（最近5键 + 2秒连击计数 + 0.5s淡出）
v3.2: 新增鼠标点击统计（LMB/RMB/MMB）、称号徽章、按键排行、时段热图、趋势折线、完整设置页、主题切换
支持 GitHub Releases 自动更新检测
"""

import json
import math
import os
import sys
import time
import threading
import socket
import subprocess
import webbrowser
import ctypes
import urllib.request
import urllib.error
import shutil
import tempfile
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime, timedelta
from collections import defaultdict, deque
from urllib.parse import parse_qs, urlparse

CURRENT_VERSION = "3.5.1"
VERSION_URL = "https://raw.githubusercontent.com/GlacierO3O/KeyHeatmap/main/version.json"
VERSION_URL_CDN = "https://cdn.jsdelivr.net/gh/GlacierO3O/KeyHeatmap@main/version.json"
RELEASE_URL = "https://github.com/GlacierO3O/KeyHeatmap/releases/latest/download/KeyHeatmap.exe"

# ─── DPI 修复 ──────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except:
        pass

# ─── 附着到交互桌面（任务计划启动时需要）──
_hDesktop = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
if _hDesktop:
    ctypes.windll.user32.SetThreadDesktop(_hDesktop)
    ctypes.windll.user32.CloseDesktop(_hDesktop)

# ─── 数据目录 ──────────────────────────────────
DATA_DIR = Path(os.environ["APPDATA"]) / "KeyHeatmap"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATS_FILE = DATA_DIR / "stats.json"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = DATA_DIR / "debug.log"
SETTINGS_FILE = DATA_DIR / "settings.json"


# ─── 设置管理 ──────────────────────────────────

class Settings:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "auto_update": True,
            "update_skip_until": None,
            "combo_float_enabled": True,
            "game_counting_enabled": True,
            "game_whitelist": [],
            "mouse_tracking_enabled": False,
            "mouse_in_overlay": False,
            "float_opacity": 88,
            "glass_enabled": True,
            "theme_day_time": "06:00",
            "theme_night_time": "18:00",
        }
        self._load()

    def _load(self):
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    self._data.update(json.load(f))
            except:
                pass

    def _save(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._save()

    def should_check_update(self):
        auto = self.get("auto_update", True)
        if not auto:
            return False
        skip_until = self.get("update_skip_until")
        if skip_until:
            try:
                if datetime.now() < datetime.fromisoformat(skip_until):
                    return False
            except:
                pass
        return True

    def set_skip_7_days(self):
        skip_until = (datetime.now() + timedelta(days=7)).isoformat()
        self.set("update_skip_until", skip_until)


# ─── 更新检测 ──────────────────────────────────

def _fetch_version_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "KeyHeatmap"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_update():
    for url in (VERSION_URL_CDN, VERSION_URL):
        try:
            data = _fetch_version_json(url)
            latest = data.get("version", "")
            if not latest:
                continue
            if latest > CURRENT_VERSION:
                return True, latest, data.get("notes", "")
            return False, latest, data.get("notes", "")
        except Exception as e:
            log(f"update check failed ({url}): {e}")
    return None


def download_update():
    try:
        log(f"downloading update from {RELEASE_URL}")
        tmp = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
        tmp_path = tmp.name
        tmp.close()
        req = urllib.request.Request(RELEASE_URL, headers={"User-Agent": "KeyHeatmap"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        log(f"downloaded to {tmp_path}")
        return tmp_path
    except Exception as e:
        log(f"download failed: {e}")
        return None


def apply_update(downloaded_path):
    try:
        current_exe = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
        current_dir = os.path.dirname(current_exe)
        bat_path = os.path.join(tempfile.gettempdir(), "keyheatmap_update.bat")
        with open(bat_path, "w", encoding="gbk") as f:
            f.write("@echo off\n")
            f.write("chcp 65001 >nul\n")
            f.write("echo 正在更新 KeyHeatmap...\n")
            f.write("timeout /t 2 /nobreak >nul\n")
            f.write(f'taskkill /f /im "KeyHeatmap.exe" 2>nul\n')
            f.write("timeout /t 1 /nobreak >nul\n")
            f.write(f'copy /y "{downloaded_path}" "{current_exe}" >nul\n')
            f.write(f'del "{downloaded_path}" 2>nul\n')
            f.write(f'start "" "{current_exe}"\n')
            f.write("del \"%~f0\"\n")
        subprocess.Popen(f'cmd /c "{bat_path}"', shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
        log(f"update batch created: {bat_path}")
        return True
    except Exception as e:
        log(f"apply_update failed: {e}")
        return False


# ─── 导入 Win32 OverlayEngine ──────────────────
_sys_path_extra = os.environ.get("APPDATA", "") + r"\KeyHeatmap"
if _sys_path_extra not in sys.path:
    sys.path.insert(0, _sys_path_extra)
import overlay_engine
from overlay_engine import OverlayEngine


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S.%f')[:12]}] {msg}\n")
    except:
        pass


overlay_engine._log_fn = log

# ─── 键盘布局定义 ──────────────────────────────
KEYBOARD_ROWS = [
    ["Esc", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"],
    ["`", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "=", "⌫"],
    ["Tab", "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "[", "]", "\\"],
    ["Caps", "A", "S", "D", "F", "G", "H", "J", "K", "L", ";", "'", "↵"],
    ["Shift", "Z", "X", "C", "V", "B", "N", "M", ",", ".", "/", "Shift"],
    ["Ctrl", "Win", "Alt", "Sp", "Alt", "Win", "Menu", "Ctrl"],
]

MOUSE_KEYS = ["LMB", "RMB", "MMB"]

KEY_NAME_MAP = {
    "Key.esc": "Esc", "Key.f1": "F1", "Key.f2": "F2", "Key.f3": "F3",
    "Key.f4": "F4", "Key.f5": "F5", "Key.f6": "F6", "Key.f7": "F7",
    "Key.f8": "F8", "Key.f9": "F9", "Key.f10": "F10", "Key.f11": "F11", "Key.f12": "F12",
    "Key.tab": "Tab", "Key.caps_lock": "Caps",
    "Key.ctrl_l": "Ctrl", "Key.ctrl_r": "Ctrl",
    "Key.shift_l": "Shift", "Key.shift_r": "Shift",
    "Key.alt_l": "Alt", "Key.alt_r": "Alt",
    "Key.cmd_l": "Win", "Key.cmd_r": "Win",
    "Key.menu": "Menu",
    "Key.space": "Sp", "Key.enter": "↵", "Key.backspace": "⌫",
    "Key.up": "↑", "Key.down": "↓", "Key.left": "←", "Key.right": "→",
    "Key.delete": "Del", "Key.insert": "Ins",
    "Key.home": "Home", "Key.end": "End",
    "Key.page_up": "PgUp", "Key.page_down": "PgDn",
    "Key.print_screen": "PrtSc", "Key.scroll_lock": "ScrLk", "Key.pause": "Pause",
    "Key.num_lock": "NumLk",
}


# ─── 数据管理 ──────────────────────────────────

class KeyStats:
    def __init__(self):
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.stats = {"all": defaultdict(int), "daily": {}}
        self.hourly = {}  # {date: {hour: count}}
        self.lock = threading.Lock()
        self._load()
        self._check_day_rollover()
        self._save_timer = threading.Thread(target=self._auto_save_loop, daemon=True)
        self._save_timer.start()

    def _auto_save_loop(self):
        while True:
            time.sleep(30)
            try:
                with self.lock:
                    self._save()
            except:
                pass

    def _load(self):
        if STATS_FILE.exists():
            try:
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.stats["all"] = defaultdict(int, data.get("all", {}))
                self.stats["daily"] = data.get("daily", {})
                self.hourly = data.get("hourly", {})
            except Exception as e:
                log(f"stats load failed: {e}")

    def _save(self):
        data = {"all": dict(self.stats["all"]), "daily": self.stats["daily"], "hourly": self.hourly}
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _check_day_rollover(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.today:
            self._make_snapshot(self.today)
            self.today = today

    def _make_snapshot(self, date_str):
        if date_str in self.stats["daily"]:
            snap = {"date": date_str, "stats": self.stats["daily"][date_str],
                    "all_at_time": dict(self.stats["all"]),
                    "hourly": self.hourly.get(date_str, {})}
            with open(SNAPSHOTS_DIR / f"{date_str}.json", "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)

    def record(self, key_name):
        with self.lock:
            self._check_day_rollover()
            self.stats["all"][key_name] += 1
            today = self.today
            if today not in self.stats["daily"]:
                self.stats["daily"][today] = {}
            self.stats["daily"][today][key_name] = self.stats["daily"][today].get(key_name, 0) + 1
            # 小时统计
            hour = datetime.now().hour
            if today not in self.hourly:
                self.hourly[today] = {}
            self.hourly[today][str(hour)] = self.hourly[today].get(str(hour), 0) + 1

    def get_display_name(self, raw_key):
        parts = str(raw_key).split(".")
        if len(parts) > 1:
            return KEY_NAME_MAP.get(str(raw_key), parts[-1].upper())
        ch = str(raw_key).strip("'")
        return ch.upper() if len(ch) == 1 else ch

    def get_data(self, period="all"):
        with self.lock:
            if period == "today":
                return self.stats["daily"].get(datetime.now().strftime("%Y-%m-%d"), {})
            elif period == "week":
                result = defaultdict(int)
                for i in range(7):
                    d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                    if d in self.stats["daily"]:
                        for k, v in self.stats["daily"][d].items():
                            result[k] += v
                return dict(result)
            return dict(self.stats["all"])

    def get_hourly_data(self):
        """返回今天每小时按键数 {0: n, 1: n, ... 23: n}"""
        with self.lock:
            today = datetime.now().strftime("%Y-%m-%d")
            hdata = self.hourly.get(today, {})
            return {h: hdata.get(str(h), 0) for h in range(24)}

    def get_trend_data(self):
        """返回最近7天每日总按键数 {date_str: total}"""
        with self.lock:
            result = {}
            for i in range(6, -1, -1):
                d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                day_data = self.stats["daily"].get(d, {})
                result[d] = sum(day_data.values())
            return result

    def get_badge(self):
        """根据累计总按键量生成称号"""
        total = sum(self.stats["all"].values())
        if total > 500000:
            return "🏆 键圣"
        elif total > 200000:
            return "👑 键皇"
        elif total > 100000:
            return "⚡ 键宗"
        elif total > 50000:
            return "🔥 键王"
        elif total > 20000:
            return "🎯 键盘侠"
        elif total > 5000:
            return "⌨️ 码字工"
        elif total > 500:
            return "🖊️ 初学者"
        else:
            return "🌱 键盘萌新"

    def get_ranking(self, period="all", top_n=15):
        """返回按键排行 TOP N [(key, count), ...]"""
        data = self.get_data(period)
        sorted_items = sorted(data.items(), key=lambda x: x[1], reverse=True)
        return sorted_items[:top_n]

    def save_and_snapshot(self):
        with self.lock:
            self._save()
            self._make_snapshot(datetime.now().strftime("%Y-%m-%d"))


# ─── 右下角浮窗 v2 ─────────────────────────────

class KeyOverlay:
    CARD_W = 52
    CARD_H = 40
    GAP = 5
    PAD = 8
    MAX_KEYS = 5

    def __init__(self):
        self.root = None
        self.canvas = None
        self.hwnd = None
        self.display_keys = deque(maxlen=self.MAX_KEYS)
        self.display_counts = {}
        self.key_timestamps = deque()
        self.fade_after_id = None
        self.fade_step_id = None
        self.current_alpha = 0.0
        self._ready = threading.Event()
        self.color_scheme = "dark"
        self._xN_anim_id = None
        self._xN_anim_data = None
        self._theme_stop = threading.Event()
        self._fullscreen_cache = 0.0
        self._fullscreen_cached_val = False

    @property
    def win_w(self):
        return self.PAD * 2 + self.CARD_W * self.MAX_KEYS + self.GAP * (self.MAX_KEYS - 1)

    @property
    def win_h(self):
        return self.PAD * 2 + self.CARD_H

    def start(self):
        t = threading.Thread(target=self._tk_loop, daemon=True, name="overlay-tk")
        t.start()
        self._ready.wait(timeout=3)

    def _tk_loop(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', 0.0)

        self.color_scheme = self._read_theme()
        self._start_theme_watcher()

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = screen_w - self.win_w - 28
        y = screen_h - self.win_h - 76
        self.root.geometry(f'{self.win_w}x{self.win_h}+{x}+{y}')

        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.canvas.pack(fill='both', expand=True)

        # 获取窗口句柄供外部使用
        try:
            self.root.update_idletasks()
            self.hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        except:
            self.hwnd = None

        self._ready.set()
        self.root.mainloop()

    def _is_fullscreen_exclusive(self):
        now = time.time()
        if now - self._fullscreen_cache < 1.0:
            return self._fullscreen_cached_val
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                self._fullscreen_cached_val = False
            else:
                style = ctypes.windll.user32.GetWindowLongW(hwnd, -16)
                if style & 0x00C00000:
                    self._fullscreen_cached_val = False
                else:
                    rect = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    sw = ctypes.windll.user32.GetSystemMetrics(0)
                    sh = ctypes.windll.user32.GetSystemMetrics(1)
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    self._fullscreen_cached_val = (abs(w - sw) <= 10 and abs(h - sh) <= 10)
        except:
            self._fullscreen_cached_val = False
        self._fullscreen_cache = now
        return self._fullscreen_cached_val

    def on_key(self, key_name):
        if not self.root:
            return
        if self._is_fullscreen_exclusive():
            return

        now = time.time()
        while self.key_timestamps and now - self.key_timestamps[0][1] > 2.0:
            self.key_timestamps.popleft()
        self.key_timestamps.append((key_name, now))
        count = sum(1 for k, t in self.key_timestamps if k == key_name)

        def _update():
            if key_name in self.display_keys:
                self.display_keys.remove(key_name)
            self.display_keys.appendleft(key_name)
            for k in self.display_keys:
                self.display_counts[k] = 1
            self.display_counts[key_name] = count
            self._draw()
            self._schedule_fade()

        try:
            self.root.after(0, _update)
        except:
            pass

    def schedule(self, ms, func):
        """供外部线程调度在主线程执行"""
        if self.root:
            self.root.after(ms, func)

    def _draw(self):
        if not self.canvas:
            return
        self.canvas.delete('all')

        w, h = self.win_w, self.win_h
        is_light = (self.color_scheme == "light")

        if is_light:
            bg_panel = '#f0f0f0'
            line_bottom = '#d0d0d0'
            card_default = '#e8e8e8'
            card_outline = '#cccccc'
            key_text_color = '#1a1a1a'
            card_colors = {
                10: '#ffcdd2',
                5:  '#ffe0b2',
                2:  '#c8e6c9',
                1:  card_default,
            }
        else:
            bg_panel = '#141414'
            line_bottom = '#2a2a2a'
            card_default = '#1e2d3d'
            card_outline = '#2a2a2a'
            key_text_color = '#ffffff'
            card_colors = {
                10: '#c0392b',
                5:  '#e67e22',
                2:  '#27ae60',
                1:  card_default,
            }

        self.canvas.create_rectangle(0, 2, w, h, fill=bg_panel, outline='', width=0)

        for i in range(w):
            if is_light:
                r = int(180 + 30 * (i / w))
                g = int(200 - 20 * (i / w))
                b = int(210 - 20 * (i / w))
            else:
                r = int(40 + 10 * (i / w))
                g = int(200 - 40 * (i / w))
                b = int(120 + 60 * (i / w))
            color = f'#{r:02x}{g:02x}{b:02x}'
            self.canvas.create_line(i, 0, i, 2, fill=color)

        self.canvas.create_line(0, h - 1, w, h - 1, fill=line_bottom)

        keys = list(self.display_keys)
        visible = keys[:self.MAX_KEYS]

        now = time.time()
        real_counts = {}
        for k, t in self.key_timestamps:
            if now - t <= 2.0:
                real_counts[k] = real_counts.get(k, 0) + 1

        xN_queue = []
        for i, key in enumerate(visible):
            x = self.PAD + i * (self.CARD_W + self.GAP)
            y = self.PAD
            count = real_counts.get(key, self.display_counts.get(key, 1))

            if count >= 10:
                bg = card_colors[10]
            elif count >= 5:
                bg = card_colors[5]
            elif count >= 2:
                bg = card_colors[2]
            else:
                bg = card_colors[1]

            r = 6
            self._rrect(x, y, x + self.CARD_W, y + self.CARD_H, r, fill=bg, outline=card_outline)

            cx = x + self.CARD_W // 2
            cy = y + self.CARD_H // 2 + 1
            font_size = 20 if len(key) <= 1 else (14 if len(key) <= 2 else 11)
            self.canvas.create_text(cx, cy, text=key, fill=key_text_color,
                                    font=('Segoe UI', font_size, 'bold'))

            if count >= 2:
                tag = f'x{count}'
                tag_w = 18 if count < 10 else 24
                tag_h = 16
                tag_x = x + self.CARD_W - 2
                tag_y = y - 4
                xN_queue.append((tag, tag_w, tag_h, tag_x, tag_y))

        if xN_queue:
            self._animate_xN(xN_queue)

    def _rrect(self, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
            x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
            x1, y2, x1, y2-radius, x1, y1+radius, x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _read_theme(self):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            apps_val = None
            sys_val = None
            try:
                apps_val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            except FileNotFoundError:
                pass
            try:
                sys_val, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            except FileNotFoundError:
                pass
            winreg.CloseKey(key)
            is_dark = (apps_val == 0 or sys_val == 0)
            theme = "dark" if is_dark else "light"
            return theme
        except Exception:
            return "dark"

    def _start_theme_watcher(self):
        self._theme_stop.clear()
        def watch():
            current = self.color_scheme
            while not self._theme_stop.is_set():
                new_scheme = self._read_theme()
                if new_scheme != current:
                    current = new_scheme
                    def apply():
                        self.color_scheme = new_scheme
                        if self.canvas:
                            self._draw()
                    try:
                        self.root.after(0, apply)
                    except Exception:
                        pass
                self._theme_stop.wait(10)
        t = threading.Thread(target=watch, daemon=True, name="theme-watcher")
        t.start()

    def _animate_xN(self, xN_queue):
        if not self.canvas or not xN_queue:
            return
        if self._xN_anim_id:
            try:
                self.root.after_cancel(self._xN_anim_id)
            except Exception:
                pass
        self._xN_anim_id = None

        scales = [0.60, 0.85, 1.15, 1.30, 1.00]
        delays = [0, 35, 35, 35, 45]

        def frame(idx):
            if idx >= len(scales) or not self.canvas:
                self._xN_anim_id = None
                return
            scale = scales[idx]
            self.canvas.delete("xN_anim")
            for tag_text, tag_w, tag_h, tag_x, tag_y in xN_queue:
                try:
                    count = int(tag_text[1:])
                except ValueError:
                    count = 2
                sw = tag_w * scale
                sh = tag_h * scale
                sx = tag_x - sw / 2
                sy = tag_y + (tag_h - sh) / 2
                r = int(8 * max(scale, 0.5))
                if count >= 10:
                    fill = '#ff1744'
                elif count >= 5:
                    fill = '#ff6d00'
                else:
                    fill = '#ff3b30'
                text_fill = '#ffffff'
                self._rrect(sx, sy, sx + sw, sy + sh, r, fill=fill, tags="xN_anim")
                font_sz = max(int(10 * scale), 6)
                self.canvas.create_text(tag_x, sy + sh / 2, text=tag_text,
                                        fill=text_fill,
                                        font=('Segoe UI', font_sz, 'bold'),
                                        tags="xN_anim")
            self._xN_anim_id = self.root.after(delays[idx], frame, idx + 1)

        frame(0)

    def _schedule_fade(self):
        if self.fade_after_id:
            self.root.after_cancel(self.fade_after_id)
        if self.fade_step_id:
            self.root.after_cancel(self.fade_step_id)
        self.root.attributes('-alpha', 0.88)
        self.current_alpha = 0.88
        self.fade_after_id = self.root.after(500, self._do_fade)

    def _do_fade(self):
        self._fade_step()

    def _fade_step(self):
        self.current_alpha -= 0.06
        if self.current_alpha <= 0.01:
            self.root.attributes('-alpha', 0.0)
            self.current_alpha = 0.0
            return
        self.root.attributes('-alpha', self.current_alpha)
        self.fade_step_id = self.root.after(30, self._fade_step)

    def stop(self):
        self._theme_stop.set()
        if self._xN_anim_id:
            try:
                self.root.after_cancel(self._xN_anim_id)
            except Exception:
                pass
        if self.root:
            try:
                self.root.after(0, self.root.destroy)
            except:
                pass


# ─── HTML模板 ──────────────────────────────────

DASHBOARD_CSS = """:root {
  --bg-body: #0d0d0d;
  --bg-card: #111;
  --bg-tab-bar: #1a1a1a;
  --bg-tab-active: #2a2a2a;
  --text-primary: #e0e0e0;
  --text-dim: #666;
  --text-mid: #888;
  --text-bright: #fff;
  --accent-from: #34d399;
  --accent-to: #f472b6;
  --card-shadow: 0 8px 32px rgba(0,0,0,0.5);
  --card-shadow-inset: inset 0 1px 0 rgba(255,255,255,0.03);
  --tab-shadow: 0 1px 3px rgba(0,0,0,0.3);
  --footer-color: #444;
  --legend-color: #666;
}
.theme-light {
  --bg-body: #f0f2f5;
  --bg-card: #ffffff;
  --bg-tab-bar: #e8ecf1;
  --bg-tab-active: #ffffff;
  --text-primary: #1a1a2e;
  --text-dim: #999;
  --text-mid: #666;
  --text-bright: #1a1a2e;
  --accent-from: #6366f1;
  --accent-to: #a855f7;
  --card-shadow: 0 4px 16px rgba(0,0,0,0.08);
  --card-shadow-inset: inset 0 1px 0 rgba(0,0,0,0.03);
  --tab-shadow: 0 1px 3px rgba(0,0,0,0.08);
  --footer-color: #bbb;
  --legend-color: #888;
}
.theme-dark {
  --bg-body: #0d0d0d;
  --bg-card: #111;
  --bg-tab-bar: #1a1a1a;
  --bg-tab-active: #2a2a2a;
  --text-primary: #e0e0e0;
  --text-dim: #666;
  --text-mid: #888;
  --text-bright: #fff;
  --accent-from: #34d399;
  --accent-to: #f472b6;
  --card-shadow: 0 8px 32px rgba(0,0,0,0.5);
  --card-shadow-inset: inset 0 1px 0 rgba(255,255,255,0.03);
  --tab-shadow: 0 1px 3px rgba(0,0,0,0.3);
  --footer-color: #444;
  --legend-color: #666;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg-body); color: var(--text-primary);
    padding: 30px 20px 40px;
    min-height: 100vh;
    display: flex; flex-direction: column; align-items: center;
}
.header { text-align: center; margin-bottom: 0; }
.header h1 {
    font-size: 28px; font-weight: 700;
    background: linear-gradient(135deg, var(--accent-from), var(--accent-to));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.header .sub { color: var(--text-dim); font-size: 13px; margin-top: 6px; }
.tabs { display: flex; gap: 4px; margin: 14px 0 28px 0; background: var(--bg-tab-bar); border-radius: 10px; padding: 4px; }
.tab { padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 500; color: var(--text-mid); transition: all 0.2s; border: none; background: transparent; }
.tab:hover { color: var(--text-primary); }
.tab.active { background: var(--bg-tab-active); color: var(--text-bright); box-shadow: var(--tab-shadow); }
.tab-theme { padding: 8px 12px; font-size: 16px; text-decoration: none; }
.summary { display: flex; gap: 30px; margin-bottom: 24px; color: var(--text-dim); font-size: 13px; }
.summary span { color: var(--accent-from); font-weight: 600; }
.keyboard {
    background: var(--bg-card); border-radius: 14px;
    padding: 20px 24px 24px;
    display: inline-flex; flex-direction: column; gap: 6px;
    box-shadow: var(--card-shadow), var(--card-shadow-inset);
}
.row { display: flex; gap: 5px; justify-content: center; }
.key {
    height: 44px; min-width: 44px;
    border-radius: 7px;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    font-size: 11px;
    cursor: default;
    transition: transform 0.12s, box-shadow 0.12s;
    border: 1px solid rgba(128,128,128,0.15);
    position: relative;
    user-select: none;
}
.key:hover { transform: translateY(-2px); box-shadow: 0 4px 14px rgba(0,0,0,0.25); z-index: 10; border-color: rgba(128,128,128,0.35); }
.key-label { font-weight: 600; font-size: 12px; text-shadow: 0 1px 2px rgba(0,0,0,0.15); line-height: 1; }
.key-count { font-size: 10px; opacity: 0.7; margin-top: 1px; line-height: 1; }
.ranking {
    background: var(--bg-card); border-radius: 14px;
    padding: 20px 24px; margin-top: 20px;
    box-shadow: var(--card-shadow);
    max-width: 560px; width: 100%;
}
.ranking-title {
    font-size: 15px; font-weight: 600; margin-bottom: 14px;
    background: linear-gradient(135deg, var(--accent-from), var(--accent-to));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.rank-row {
    display: flex; align-items: center;
    gap: 8px; margin-bottom: 5px; font-size: 13px;
}
.rank-no {
    width: 28px; text-align: right;
    color: var(--text-dim); font-weight: 700;
    flex-shrink: 0;
}

.rank-key {
    width: 40px; text-align: center;
    font-weight: 600; color: var(--text-primary);
    flex-shrink: 0;
}
.rank-bar-wrap {
    flex: 1; height: 18px;
    background: var(--bg-tab-bar); border-radius: 4px;
    overflow: hidden; min-width: 40px;
}
.rank-bar {
    height: 100%; border-radius: 4px;
    background: linear-gradient(90deg, var(--accent-from), var(--accent-to));
    transition: width 0.5s ease;
    opacity: 0.85;
}
.rank-count {
    width: 52px; text-align: right;
    color: var(--text-mid); font-size: 12px;
    flex-shrink: 0;
}
.legend { 
    display: flex; align-items: center; justify-content: center; gap: 12px;
    margin: 20px auto;
    padding: 14px 28px;
    background: var(--bg-card); border-radius: 14px;
    font-size: 12px; color: var(--legend-color);
    max-width: 400px;
}
.legend-bar { width: 180px; height: 10px; border-radius: 5px; background: linear-gradient(90deg, var(--accent-from), var(--accent-to)); }
.mouse-row { margin-top: 14px; padding-top: 10px; border-top: 2px dashed rgba(128,128,128,0.2); }
.mouse-row-label { text-align: center; font-size: 11px; color: var(--text-dim); margin-bottom: 8px; letter-spacing: 0.5px; }
.mouse-key { }
.footer { margin-top: 30px; text-align: center; font-size: 11px; color: var(--footer-color); }
.title-badge {
    display: inline-block; padding: 4px 14px;
    background: linear-gradient(135deg, var(--accent-from), var(--accent-to));
    border-radius: 20px; color: #fff; font-size: 14px; font-weight: 600;
}
.title-wrap { text-align: center; margin: 14px 0 0 0; }
.stats-panel {
    background: var(--bg-card); border-radius: 14px;
    padding: 16px 24px 20px; margin-top: 20px;
    box-shadow: var(--card-shadow);
    max-width: 640px; width: 100%;
}
.sub-tabs {
    display: flex; gap: 4px; margin-bottom: 16px;
    background: var(--bg-tab-bar); border-radius: 8px; padding: 3px;
}
.sub-tab {
    padding: 6px 14px; border-radius: 6px; cursor: pointer;
    font-size: 13px; font-weight: 500; color: var(--text-mid);
    transition: all 0.2s; border: none; background: transparent;
}
.sub-tab:hover { color: var(--text-primary); }
.sub-tab.active { background: var(--bg-tab-active); color: var(--text-bright); box-shadow: var(--tab-shadow); }
.mouse-toggle-btn {
    margin-left: auto; padding: 4px 10px; border-radius: 6px; cursor: pointer;
    font-size: 12px; font-weight: 500; border: 1px solid var(--border-color);
    background: transparent; color: var(--text-mid); transition: all 0.2s;
    white-space: nowrap;
}
.mouse-toggle-btn:hover { color: var(--text-primary); border-color: var(--accent-from); }
.mouse-toggle-btn.on {
    background: var(--bg-tab-active); color: var(--text-bright);
    border-color: var(--accent-from); box-shadow: var(--tab-shadow);
}
.sub-panel { }
.panel-title {
    font-size: 14px; font-weight: 600; margin-bottom: 14px;
    background: linear-gradient(135deg, var(--accent-from), var(--accent-to));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.hourly-chart {
    display: flex; align-items: flex-end; gap: 4px;
    height: 160px; padding: 0 4px; position: relative;
}
.hour-col {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: flex-end; height: 100%;
}
.hour-bar {
    width: 100%; max-width: 20px; min-height: 2px;
    background: linear-gradient(180deg, var(--accent-from), var(--accent-to));
    border-radius: 3px 3px 0 0; transition: height 0.4s ease;
    opacity: 0.85; position: relative;
}
.hour-now .hour-bar {
    opacity: 1; box-shadow: 0 0 8px var(--accent-from);
}
.hour-count {
    position: absolute; top: -18px; left: 50%; transform: translateX(-50%);
    font-size: 10px; color: var(--text-mid); white-space: nowrap;
}
.hour-label {
    font-size: 10px; color: var(--text-dim); margin-top: 4px;
}
.trend-wrap {
    display: flex; justify-content: center; position: relative;
}
.trend-svg {
    width: 100%; max-width: 560px; height: auto;
}
.trend-line {
    stroke: var(--accent-from); stroke-width: 2.5;
    stroke-linecap: round; stroke-linejoin: round;
}
.trend-area {
    fill: url(#trendGrad); opacity: 0.3;
}
.trend-dot {
    fill: var(--accent-from); stroke: var(--bg-card); stroke-width: 2; cursor: pointer; pointer-events: none;
}
.trend-hit { fill: transparent; cursor: pointer; pointer-events: none; }
.trend-hover { fill: transparent; cursor: pointer; }
.trend-band {
    fill: var(--accent-from); opacity: 0; pointer-events: none;
    transition: opacity 0.15s ease, x 0.25s ease-out, width 0.25s ease-out;
}
.trend-band.on { opacity: 0.08; }
.trend-label {
    font-size: 10px; fill: var(--text-dim);
}
.chart-tooltip {
    position: absolute; pointer-events: none;
    opacity: 0; transform: translateY(6px);
    transition: opacity 0.18s ease, transform 0.18s ease;
    background: var(--bg-card); color: var(--text-bright);
    border: 1px solid #34d399;
    border-radius: 10px; padding: 8px 14px;
    font-size: 13px; font-weight: 600;
    white-space: nowrap; z-index: 10;
    box-shadow: 0 6px 24px rgba(0,0,0,0.35);
    backdrop-filter: blur(12px);
}
.chart-tooltip.show {
    opacity: 1; transform: translateY(0);
}
/* 更新弹窗 */
.update-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.65);
    display: flex; align-items: center; justify-content: center;
    z-index: 9999; backdrop-filter: blur(4px);
}
.update-overlay.hidden { display: none; }
.update-modal {
    background: var(--bg-card); border: 1px solid rgba(124,58,237,0.3);
    border-radius: 16px; padding: 28px 32px 24px; width: 420px; max-width: 90vw;
    box-shadow: 0 8px 40px rgba(0,0,0,0.5);
}
.update-modal h2 {
    font-size: 20px; font-weight: 700; color: var(--text-bright);
    margin-bottom: 6px;
}
.update-modal .update-ver {
    font-size: 13px; color: var(--text-dim); margin-bottom: 16px;
}
.update-modal .update-ver b { color: #a855f7; }
.update-modal .update-log {
    font-size: 13px; color: var(--text-mid); line-height: 1.6;
    background: rgba(124,58,237,0.06); border-radius: 10px;
    padding: 12px 16px; margin-bottom: 20px; max-height: 180px; overflow-y: auto;
}
.update-btns { display: flex; justify-content: flex-end; gap: 10px; align-items: center; }
.update-btns .btn-cancel {
    background: transparent; border: 1px solid var(--text-dim);
    color: var(--text-mid); padding: 8px 20px; border-radius: 8px;
    cursor: pointer; font-size: 13px;
}
.update-btns .btn-update {
    background: linear-gradient(135deg, #7c3aed, #a855f7);
    border: none; color: #fff; padding: 8px 24px; border-radius: 8px;
    cursor: pointer; font-size: 13px; font-weight: 600;
}
.update-skip { display: flex; align-items: center; gap: 6px; margin-right: auto; }
.update-skip label {
    font-size: 12px; color: var(--text-dim); cursor: pointer; user-select: none;
}
.update-skip input { accent-color: #7c3aed; }
.status-msg { font-size: 12px; color: var(--text-dim); margin-top: 10px; text-align: center; }
"""


SETTINGS_CSS = """:root {
  --bg-body: #0d0d0d;
  --bg-card: #111;
  --bg-tab-bar: #1a1a1a;
  --bg-tab-active: #2a2a2a;
  --text-primary: #e0e0e0;
  --text-dim: #666;
  --text-mid: #888;
  --text-bright: #fff;
  --accent-from: #34d399;
  --accent-to: #f472b6;
  --card-shadow: 0 4px 16px rgba(0,0,0,0.3);
  --tab-shadow: 0 1px 3px rgba(0,0,0,0.3);
  --footer-color: #444;
  --input-bg: #1a1a1a;
  --input-border: #333;
  --input-focus: #34d399;
  --wl-bg: #1e2d3d;
  --wl-text: #ccc;
  --toggle-off-bg: #444;
  --toggle-off-text: #999;
  --btn-green: #27ae60;
  --btn-green-hover: #2ecc71;
  --btn-red: #e74c3c;
  --btn-red-hover: #ff6b6b;
  --status-on: #27ae60;
  --status-off: #999;
}
.theme-light {
  --bg-body: #f0f2f5;
  --bg-card: #ffffff;
  --bg-tab-bar: #e8ecf1;
  --bg-tab-active: #ffffff;
  --text-primary: #1a1a2e;
  --text-dim: #999;
  --text-mid: #666;
  --text-bright: #1a1a2e;
  --accent-from: #6366f1;
  --accent-to: #a855f7;
  --card-shadow: 0 2px 8px rgba(0,0,0,0.06);
  --tab-shadow: 0 1px 3px rgba(0,0,0,0.08);
  --footer-color: #bbb;
  --input-bg: #ffffff;
  --input-border: #d0d0d0;
  --input-focus: #6366f1;
  --wl-bg: #eef0ff;
  --wl-text: #333;
  --toggle-off-bg: #d0d0d0;
  --toggle-off-text: #888;
  --btn-green: #27ae60;
  --btn-green-hover: #2ecc71;
  --btn-red: #e74c3c;
  --btn-red-hover: #ff6b6b;
  --status-on: #27ae60;
  --status-off: #999;
}
.theme-dark {
  --bg-body: #0d0d0d;
  --bg-card: #111;
  --bg-tab-bar: #1a1a1a;
  --bg-tab-active: #2a2a2a;
  --text-primary: #e0e0e0;
  --text-dim: #666;
  --text-mid: #888;
  --text-bright: #fff;
  --accent-from: #34d399;
  --accent-to: #f472b6;
  --card-shadow: 0 4px 16px rgba(0,0,0,0.3);
  --tab-shadow: 0 1px 3px rgba(0,0,0,0.3);
  --footer-color: #444;
  --input-bg: #1a1a1a;
  --input-border: #333;
  --input-focus: #34d399;
  --wl-bg: #1e2d3d;
  --wl-text: #ccc;
  --toggle-off-bg: #444;
  --toggle-off-text: #999;
  --btn-green: #27ae60;
  --btn-green-hover: #2ecc71;
  --btn-red: #e74c3c;
  --btn-red-hover: #ff6b6b;
  --status-on: #27ae60;
  --status-off: #999;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg-body); color: var(--text-primary);
    padding: 30px 20px 40px;
    min-height: 100vh;
    display: flex; flex-direction: column; align-items: center;
}
.header { text-align: center; margin-bottom: 0; }
.header h1 {
    font-size: 28px; font-weight: 700;
    background: linear-gradient(135deg, var(--accent-from), var(--accent-to));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.header .sub { color: var(--text-dim); font-size: 13px; margin-top: 6px; }
.tabs { display: flex; gap: 4px; margin: 14px 0 28px 0; background: var(--bg-tab-bar); border-radius: 10px; padding: 4px; }
.tab { padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 500; color: var(--text-mid); transition: all 0.2s; border: none; background: transparent; text-decoration: none; }
.tab:hover { color: var(--text-primary); }
.tab.active { background: var(--bg-tab-active); color: var(--text-bright); box-shadow: var(--tab-shadow); }
.tab-theme { padding: 8px 12px; font-size: 16px; text-decoration: none; }
.container { width: 100%; max-width: 560px; }
.card {
    background: var(--bg-card); border-radius: 12px; padding: 20px 24px;
    margin-bottom: 16px;
    box-shadow: var(--card-shadow);
}
.card-title { font-size: 15px; font-weight: 600; color: var(--text-primary); margin-bottom: 12px; }
.card-desc { font-size: 12px; color: var(--text-dim); margin-bottom: 12px; line-height: 1.5; }
.row { display: flex; align-items: center; justify-content: space-between; }
.toggle-btn {
    display: inline-block; padding: 6px 18px; border-radius: 6px;
    font-size: 13px; font-weight: 600; text-decoration: none;
    transition: all 0.15s; cursor: pointer;
}
.toggle-on { background: var(--btn-green); color: #fff; }
.toggle-off { background: var(--toggle-off-bg); color: var(--toggle-off-text); }
.toggle-on:hover { background: var(--btn-green-hover); }
.toggle-off:hover { background: #555; }
.wl-section { margin-top: 8px; }
.wl-list { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.wl-item {
    display: inline-flex; align-items: center; gap: 8px;
    background: var(--wl-bg); border-radius: 6px; padding: 5px 10px;
    font-size: 13px; color: var(--wl-text);
}
.wl-remove { color: var(--btn-red); text-decoration: none; font-size: 12px; }
.wl-remove:hover { color: var(--btn-red-hover); }
.wl-add { display: flex; gap: 8px; }
.wl-add input {
    flex: 1; background: var(--input-bg); border: 1px solid var(--input-border); border-radius: 6px;
    padding: 8px 12px; color: var(--text-primary); font-size: 13px; outline: none;
}
.wl-add input:focus { border-color: var(--input-focus); }
.wl-add button {
    background: var(--btn-green); color: #fff; border: none; border-radius: 6px;
    padding: 8px 16px; font-size: 13px; font-weight: 600; cursor: pointer;
}
.wl-add button:hover { background: var(--btn-green-hover); }
.time-row {
    display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
}
.time-row label {
    font-size: 13px; color: var(--text-mid); white-space: nowrap;
}
.time-row input[type="time"] {
    background: var(--input-bg); border: 1px solid var(--input-border);
    border-radius: 6px; padding: 6px 10px; color: var(--text-primary);
    font-size: 13px; outline: none; width: 120px;
}
.time-row input[type="time"]:focus { border-color: var(--input-focus); }
.time-row button {
    background: var(--btn-green); color: #fff; border: none; border-radius: 6px;
    padding: 6px 16px; font-size: 13px; font-weight: 600; cursor: pointer;
}
.time-row button:hover { background: var(--btn-green-hover); }
.slider-row {
    display: flex; align-items: center; gap: 10px;
}
.slider-row input[type="range"] {
    flex: 1; -webkit-appearance: none; appearance: none;
    height: 6px; border-radius: 3px;
    background: var(--bg-tab-bar); outline: none;
}
.slider-row input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none;
    width: 18px; height: 18px; border-radius: 50%;
    background: var(--btn-green); cursor: pointer;
}
.slider-row input[type="range"]::-moz-range-thumb {
    width: 18px; height: 18px; border-radius: 50%;
    background: var(--btn-green); cursor: pointer; border: none;
}
.slider-val {
    width: 36px; text-align: right;
    font-size: 13px; font-weight: 600; color: var(--text-primary);
    flex-shrink: 0;
}
.footer { margin-top: 30px; text-align: center; font-size: 11px; color: var(--footer-color); }
"""


def build_dashboard_html(data, period_label, max_count, theme, badge, ranking, hourly, trend, mouse_data, settings_obj, url_theme=None):
    """构建完整仪表盘 HTML。theme=解析后的渲染主题, url_theme=URL原始参数（用于切换循环）"""
    if url_theme is None:
        url_theme = theme
    is_light = (theme == 'light')
    mouse_track = settings_obj.get("mouse_tracking_enabled", False) if settings_obj else False
    total = sum(data.values()) if data else 1
    if mouse_data:
        total += sum(mouse_data.values())

    zero_bg = (228, 230, 235) if is_light else (38, 38, 38)

    # 锚点色与时段热图渐变保持一致（对应 CSS --accent-from / --accent-to）
    if is_light:
        accent_from = (99, 102, 241)   # #6366f1
        accent_to   = (168, 85, 247)   # #a855f7
    else:
        accent_from = (52, 211, 153)   # #34d399
        accent_to   = (244, 114, 182)  # #f472b6

    def _interp_anchors(frm, to):
        return [
            (0.00, zero_bg),
            (0.20, frm),
            (0.40, tuple(int(frm[i] + (to[i] - frm[i]) * 0.25) for i in range(3))),
            (0.60, tuple(int(frm[i] + (to[i] - frm[i]) * 0.50) for i in range(3))),
            (0.80, tuple(int(frm[i] + (to[i] - frm[i]) * 0.75) for i in range(3))),
            (1.00, to),
        ]

    # ─── 键盘行 ───
    rows_html = ""
    for row_keys in KEYBOARD_ROWS:
        cells = []
        for k in row_keys:
            count = data.get(k, 0)
            max_val = max(max_count, 1)
            log_ratio = math.log(count + 1) / math.log(max_val + 1) if count > 0 else 0.0

            anchors = _interp_anchors(accent_from, accent_to)
            r, g, b = zero_bg
            for i in range(len(anchors) - 1):
                lo_r, hi_r = anchors[i][0], anchors[i + 1][0]
                if lo_r <= log_ratio <= hi_r:
                    t_ratio = (log_ratio - lo_r) / (hi_r - lo_r) if hi_r > lo_r else 0
                    r = int(anchors[i][1][0] + t_ratio * (anchors[i + 1][1][0] - anchors[i][1][0]))
                    g = int(anchors[i][1][1] + t_ratio * (anchors[i + 1][1][1] - anchors[i][1][1]))
                    b = int(anchors[i][1][2] + t_ratio * (anchors[i + 1][1][2] - anchors[i][1][2]))
                    break

            width_class = ""
            if k == "Sp":
                width_class = ' style="width:240px"'
            elif k in ("⌫", "Tab", "Caps", "↵", "Shift"):
                width_class = ' style="width:80px"'
            elif k == "Ctrl":
                width_class = ' style="width:70px"'

            cells.append(
                f'<div class="key" style="background:rgb({r},{g},{b})"'
                f'{width_class} title="{k}: {count} 次 ({count*100//total if total else 0}%)">'
                f'<span class="key-label">{k}</span>'
                f'<span class="key-count">{count}</span>'
                f'</div>'
            )
        rows_html += f'<div class="row">{"".join(cells)}</div>\n'

    # ─── 鼠标行 ───
    mouse_html = ""
    if mouse_data:
        mouse_cells = []
        for mk in MOUSE_KEYS:
            mc = mouse_data.get(mk, 0)
            max_val = max(max(mouse_data.values()), 1) if mouse_data else 1
            log_ratio = math.log(mc + 1) / math.log(max_val + 1) if mc > 0 else 0.0
            anchors = _interp_anchors(accent_from, accent_to)
            r2, g2, b2 = zero_bg
            for i in range(len(anchors) - 1):
                lo_r2, hi_r2 = anchors[i][0], anchors[i + 1][0]
                if lo_r2 <= log_ratio <= hi_r2:
                    t2 = (log_ratio - lo_r2) / (hi_r2 - lo_r2) if hi_r2 > lo_r2 else 0
                    r2 = int(anchors[i][1][0] + t2 * (anchors[i + 1][1][0] - anchors[i][1][0]))
                    g2 = int(anchors[i][1][1] + t2 * (anchors[i + 1][1][1] - anchors[i][1][1]))
                    b2 = int(anchors[i][1][2] + t2 * (anchors[i + 1][1][2] - anchors[i][1][2]))
                    break
            mouse_cells.append(
                f'<div class="key mouse-key" style="background:rgb({r2},{g2},{b2});width:110px" '
                f'title="{mk}: {mc} 次">'
                f'<span class="key-label">{mk}</span>'
                f'<span class="key-count">{mc}</span>'
                f'</div>'
            )
        mouse_html = (
            '<div class="mouse-row"><div class="mouse-row-label">鼠标点击</div>'
            f'<div class="row">{"".join(mouse_cells)}</div></div>'
        )

    # ─── 称号徽章 ───
    badge_html = f'<div class="title-wrap"><div class="title-badge">{badge}</div></div>'

    # ─── 排行 TOP 15 ───
    ranking_rows = ""
    max_rank_count = ranking[0][1] if ranking else 1
    for idx, (k, v) in enumerate(ranking[:15]):
        pct = int(v / max_rank_count * 100) if max_rank_count > 0 else 0
        rank_no_class = ""
        if idx == 0:
            rank_no_class = " top1"
        elif idx == 1:
            rank_no_class = " top2"
        elif idx == 2:
            rank_no_class = " top3"
        ranking_rows += (
            f'<div class="rank-row"><span class="rank-no{rank_no_class}">{idx+1}</span>'
            f'<span class="rank-key">{k}</span>'
            f'<div class="rank-bar-wrap"><div class="rank-bar" style="width:{pct}%"></div></div>'
            f'<span class="rank-count">{v:,}</span></div>\n'
        )

    if not ranking_rows:
        ranking_rows = '<div style="color:var(--text-dim);text-align:center;padding:20px;">暂无数据</div>'

    # ─── 时段热图 ───
    max_hourly = max(hourly.values()) if hourly else 1
    hourly_cols = ""
    for h in range(24):
        val = hourly.get(h, 0)
        h_pct = int(val / max_hourly * 100) if max_hourly > 0 else 0
        label = f"{h:02d}"
        active_cls = ' active' if h == datetime.now().hour else ''
        hourly_cols += (
            f'<div class="hour-col{active_cls}">'
            f'<div class="hour-bar" style="height:{max(h_pct, 2)}%" data-hour="{h}" data-count="{val}">'
            f'<span class="hour-count">{val if val > 0 else ""}</span></div>'
            f'<span class="hour-label">{label}</span></div>\n'
        )

    # ─── 趋势折线 ───
    trend_dates = sorted(trend.keys())
    if trend_dates:
        max_trend = max(trend.values()) if trend else 1
        margin = 40
        svg_w = 600
        svg_h = 260
        plot_w = svg_w - margin * 2
        plot_h = svg_h - margin * 2 - 20
        n = len(trend_dates)

        points = []
        for i, d in enumerate(trend_dates):
            val = trend.get(d, 0)
            x = margin + (i / max(n - 1, 1)) * plot_w
            y = margin + plot_h - (val / max(max_trend, 1)) * plot_h
            points.append(f"{x:.1f},{y:.1f}")

        polyline_pts = " ".join(points)
        area_pts = f"{margin},{margin + plot_h} " + " ".join(points) + f" {margin + (n-1)*plot_w/max(n-1,1):.1f},{margin + plot_h}"

        dots = ""
        labels = ""
        for i, d in enumerate(trend_dates):
            val = trend.get(d, 0)
            x = margin + (i / max(n - 1, 1)) * plot_w
            y = margin + plot_h - (val / max(max_trend, 1)) * plot_h
            short_date = d[-5:]  # MM-DD
            dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" class="trend-dot" data-date="{d}" data-count="{val}"/>'
            labels += f'<text x="{x:.1f}" y="{svg_h - 8}" text-anchor="middle" class="trend-label">{short_date}</text>'

        trend_svg = (
            f'<svg viewBox="0 0 {svg_w} {svg_h}" class="trend-svg">'
            '<defs><linearGradient id="trendGrad" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#34d399"/>'
            '<stop offset="100%" stop-color="#34d399" stop-opacity="0"/>'
            '</linearGradient></defs>'
            f'<polyline points="{polyline_pts}" fill="none" class="trend-line"/>'
            f'<polygon points="{area_pts}" class="trend-area"/>'
            f'<rect x="{margin}" y="{margin}" width="{plot_w}" height="{plot_h}" class="trend-hover"/>'
            f'<rect x="0" y="{margin - 15}" width="{margin}" height="{plot_h + 30}" class="trend-band" id="trend-band"/>'
            f'{dots}{labels}'
            f'</svg>'
        )
    else:
        trend_svg = '<div style="color:var(--text-dim);text-align:center;padding:40px;">暂无趋势数据</div>'

    # ─── 主题图标 ───
    theme_icons = {"light": "☀", "dark": "🌙", "auto": "🔄"}
    next_theme = {"light": "dark", "dark": "auto", "auto": "light"}.get(url_theme, "dark")

    day_time = settings_obj.get("theme_day_time", "06:00")
    night_time = settings_obj.get("theme_night_time", "18:00")
    mouse_toggle_html = f'<button class="mouse-toggle-btn{" on" if mouse_track else ""}" onclick="toggleMouseRanking()" id="mouseRankingBtn">{"鼠标按键：开" if mouse_track else "鼠标按键：关"}</button>'

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>键盘热力图 | KeyHeatmap</title>
<style>
{DASHBOARD_CSS}
</style>
</head>
<body class="theme-{theme}">

<div class="header">
    <h1>键盘热力图</h1>
    <div class="sub">{period_label} - 刷新于 {datetime.now().strftime("%H:%M:%S")}</div>
</div>
{badge_html}
<div class="tabs">
    <button class="tab" onclick="switchPeriod('today')">今天</button>
    <button class="tab" onclick="switchPeriod('week')">本周</button>
    <button class="tab" onclick="switchPeriod('all')">全部</button>
    <span style="flex:1"></span>
    <a class="tab tab-theme" href="/?theme={next_theme}" title="切换主题">{theme_icons.get(url_theme, '🌙')}</a>
    <a class="tab" href="/?page=settings&theme={url_theme}" style="text-decoration:none;">设置</a>
</div>
<div class="summary">
    总按键 <span>{total:,}</span> 次 - 涉及 <span>{len(data)}</span> 个键 - 最忙键 <span>{max(data, key=data.get) if data else '-'}</span>
</div>
<div class="keyboard">
{rows_html}{mouse_html}
</div>
<div class="legend">低频 <div class="legend-bar"></div> 高频</div>
<div class="stats-panel">
    <div class="sub-tabs">
        <button class="sub-tab active" onclick="switchSubTab('ranking')">按键排行</button>
        <button class="sub-tab" onclick="switchSubTab('hourly')">时段热图</button>
        <button class="sub-tab" onclick="switchSubTab('trend')">趋势折线</button>
        {mouse_toggle_html}
    </div>
    <div id="sub-ranking" class="sub-panel">
        <div class="ranking">
            <div class="ranking-title">按键排行 TOP 15</div>
{ranking_rows}
        </div>
    </div>
    <div id="sub-hourly" class="sub-panel" style="display:none">
        <div class="panel-title">24小时按键分布</div>
        <div class="hourly-chart">
{hourly_cols}<div class="chart-tooltip" id="hourly-tooltip"></div></div>
    </div>
    <div id="sub-trend" class="sub-panel" style="display:none">
        <div class="panel-title">近7天趋势</div>
        <div class="trend-wrap">
{trend_svg}<div class="chart-tooltip" id="trend-tooltip"></div></div>
    </div>
</div>
<div class="footer">KeyHeatmap - 后台默默统计中 - <span id="refresh-hint">页面每30秒自动刷新</span></div>
<script>
var THEME_PARAM = "{url_theme}";
var DAY_TIME = "{day_time}";
var NIGHT_TIME = "{night_time}";

function switchSubTab(name) {{
    document.querySelectorAll('.sub-tab').forEach(function(btn) {{ btn.classList.remove('active'); }});
    document.querySelectorAll('.sub-panel').forEach(function(p) {{ p.style.display = 'none'; }});
    event.target.classList.add('active');
    document.getElementById('sub-' + name).style.display = '';
}}

function timeToMinutes(t) {{
    var parts = t.split(':');
    return parseInt(parts[0]) * 60 + parseInt(parts[1]);
}}
function isNightNow() {{
    var now = new Date();
    var cur = now.getHours() * 60 + now.getMinutes();
    var d = timeToMinutes(DAY_TIME);
    var n = timeToMinutes(NIGHT_TIME);
    return cur >= n || cur < d;
}}
function resolveTheme(t) {{
    if (t === 'auto') return isNightNow() ? 'dark' : 'light';
    return t;
}}
function applyThemeClass() {{
    document.body.className = 'theme-' + resolveTheme(THEME_PARAM);
}}
var lastThemeApplied = resolveTheme(THEME_PARAM);
applyThemeClass();
setInterval(function() {{
    if (THEME_PARAM !== 'auto') return;
    var current = resolveTheme('auto');
    if (current !== lastThemeApplied) {{
        lastThemeApplied = current;
        applyThemeClass();
    }}
}}, 30000);
function cycleTheme() {{
    var next = {{'light':'dark','dark':'auto','auto':'light'}}[THEME_PARAM];
    var params = new URLSearchParams(window.location.search);
    params.set('theme', next);
    window.location.search = params.toString();
}}
function switchPeriod(p) {{ window.location.href = '/?period=' + p + '&theme=' + THEME_PARAM; }}
function toggleMouseRanking() {{
    fetch('/api/toggle_mouse_tracking')
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{ location.reload(); }});
}}
(function() {{
    var params = new URLSearchParams(window.location.search);
    var period = params.get('period') || 'today';
    document.querySelectorAll('.tab').forEach(function(btn) {{
        btn.classList.remove('active');
        if (btn.textContent === {{ 'today': '今天', 'week': '本周', 'all': '全部' }}[period]) {{
            btn.classList.add('active');
        }}
    }});
}})();
setTimeout(function() {{ location.reload(); }}, 30000);

// 时段热图 tooltip
(function() {{
    var tooltip = document.getElementById('hourly-tooltip');
    if (!tooltip) return;
    var container = document.querySelector('.hourly-chart');
    document.querySelectorAll('.hour-bar').forEach(function(bar) {{
        bar.addEventListener('mouseenter', function(e) {{
            var hour = this.getAttribute('data-hour');
            var count = this.getAttribute('data-count');
            tooltip.textContent = hour + ':00 - ' + count + ' 次';
            tooltip.classList.add('show');
        }});
        bar.addEventListener('mousemove', function(e) {{
            var cr = container.getBoundingClientRect();
            tooltip.style.left = (e.clientX - cr.left + 12) + 'px';
            tooltip.style.top = (e.clientY - cr.top - 28) + 'px';
        }});
        bar.addEventListener('mouseleave', function() {{
            tooltip.classList.remove('show');
        }});
    }});
}})();

// 趋势折线 tooltip
(function() {{
    var tooltip = document.getElementById('trend-tooltip');
    var band = document.getElementById('trend-band');
    var hover = document.querySelector('.trend-hover');
    if (!tooltip || !hover) return;
    var dots = document.querySelectorAll('.trend-dot');
    var hoverX = parseFloat(hover.getAttribute('x'));
    var hoverW = parseFloat(hover.getAttribute('width'));
    // 数据点间隔 = 绘图区宽 / (点数-1)，单点时覆盖全宽
    var interval = dots.length > 1 ? hoverW / (dots.length - 1) : hoverW;
    hover.addEventListener('mousemove', function(e) {{
        var svg = this.closest('svg');
        var svgRect = svg.getBoundingClientRect();
        var mx = e.clientX - svgRect.left;
        var viewBox = svg.viewBox.baseVal;
        var sx = viewBox.x + (mx / svgRect.width) * viewBox.width;
        var nearest = dots[0], minD = Infinity;
        dots.forEach(function(d) {{
            var cx = parseFloat(d.getAttribute('cx'));
            var dist = Math.abs(cx - sx);
            if (dist < minD) {{ minD = dist; nearest = d; }}
        }});
        var d = nearest.getAttribute('data-date');
        var c = nearest.getAttribute('data-count');
        var cx = parseFloat(nearest.getAttribute('cx'));
        tooltip.textContent = d + '  ' + c + ' 次';
        tooltip.classList.add('show');
        tooltip.style.left = (mx + 15) + 'px';
        tooltip.style.top = (e.clientY - svgRect.top - 25) + 'px';
        if (band) {{
            band.setAttribute('x', cx - interval / 2);
            band.setAttribute('width', interval);
            band.classList.add('on');
        }}
    }});
    hover.addEventListener('mouseleave', function() {{
        tooltip.classList.remove('show');
        if (band) band.classList.remove('on');
    }});
}})();

/* ─── 更新检测 ─── */
(function() {{
    fetch('/api/check-update')
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{
            if (d.has_update) {{
                var overlay = document.getElementById('update-overlay');
                overlay.querySelector('.update-ver').innerHTML = '当前 v' + d.current + ' → 最新 <b>v' + d.latest + '</b>';
                overlay.querySelector('.update-log').innerHTML = (d.changelog || '无更新日志').replace(/\\n/g, '<br>');
                overlay.classList.remove('hidden');
            }}
        }})
        .catch(function(){{}});
}})();
function doUpdate() {{
    var msg = document.getElementById('update-status');
    msg.textContent = '正在下载更新...';
    fetch('/api/update/apply')
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{
            if (d.status === 'applying') {{
                msg.textContent = '更新已开始，程序即将重启...';
            }} else {{
                msg.textContent = '下载失败，请稍后重试';
            }}
        }})
        .catch(function(){{ msg.textContent = '更新失败，请检查网络'; }});
}}
function dismissUpdate() {{
    var cb = document.getElementById('update-skip-cb');
    if (cb && cb.checked) {{
        fetch('/api/update/skip');
    }}
    document.getElementById('update-overlay').classList.add('hidden');
}}
</script>

<!-- 更新弹窗 -->
<div id="update-overlay" class="update-overlay hidden">
<div class="update-modal">
<h2>发现新版本</h2>
<div class="update-ver"></div>
<div class="update-log"></div>
<div class="update-btns">
<div class="update-skip"><input type="checkbox" id="update-skip-cb"><label for="update-skip-cb">7天内不提示</label></div>
<button class="btn-cancel" onclick="dismissUpdate()">取消</button>
<button class="btn-update" onclick="doUpdate()">立即更新</button>
</div>
<div id="update-status" class="status-msg"></div>
</div>
</div>

</body>
</html>"""


def build_settings_html(settings_obj, theme):
    """构建设置页面 HTML"""
    combo_on = settings_obj.get("combo_float_enabled", True)
    game_count_on = settings_obj.get("game_counting_enabled", True)
    glass_on = settings_obj.get("glass_enabled", True)
    mouse_track_on = settings_obj.get("mouse_tracking_enabled", False)
    mouse_overlay_on = settings_obj.get("mouse_in_overlay", False)
    auto_update_on = settings_obj.get("auto_update", True)
    autostart_on = HeatmapHandler.tray.autostart_enabled if getattr(HeatmapHandler, "tray", None) else False
    whitelist = settings_obj.get("game_whitelist", [])
    day_time = settings_obj.get("theme_day_time", "06:00")
    night_time = settings_obj.get("theme_night_time", "18:00")
    opacity = settings_obj.get("float_opacity", 88)
    theme_icons = {"light": "☀", "dark": "🌙", "auto": "🔄"}
    next_theme = {"light": "dark", "dark": "auto", "auto": "light"}.get(theme, "dark")

    # 白名单列表
    wl_html = ""
    if whitelist:
        for proc in whitelist:
            wl_html += (
                f'<span class="wl-item">{proc} '
                f'<a href="/?page=settings&action=remove_whitelist&process={proc}&theme={theme}" '
                f'class="wl-remove">移除</a></span>'
            )
    else:
        wl_html = '<span style="color:var(--text-dim);">暂无白名单进程</span>'

    # Toggle 辅助
    def toggle_row(title, desc, status_on, true_text, false_text, action_url, status_color_var="var(--status-on)"):
        color = status_color_var if status_on else "var(--status-off)"
        text = true_text if status_on else false_text
        btn_class = "toggle-btn toggle-on" if status_on else "toggle-btn toggle-off"
        btn_text = "关闭" if status_on else "开启"
        return f"""<div class="card">
        <div class="card-title">{title}</div>
        <div class="card-desc">{desc}</div>
        <div class="row">
            <span style="color:var(--text-mid);font-size:13px;">当前状态：<b style="color:{color}">{text}</b></span>
            <a class="{btn_class}" href="/?page=settings&action={action_url}&theme={theme}">{btn_text}</a>
        </div>
    </div>"""

    CHECK_UPDATE_JS = """function checkUpdateNow() {
    var status = document.getElementById('check-status');
    status.style.color = 'var(--accent-from)';
    var dots = 0;
    var timer = setInterval(function() {
        dots = (dots + 1) % 4;
        status.textContent = '正在检查' + '.'.repeat(dots);
    }, 300);
    fetch('/api/check-update')
        .then(function(r) { return r.json(); })
        .then(function(d) {
            clearInterval(timer);
            if (d.has_update) {
                status.innerHTML = '发现新版本 <b style="color:#a855f7">v' + d.latest + '</b>！请到仪表盘页面进行更新。';
                status.style.color = '#a855f7';
            } else if (d.current) {
                status.textContent = '已是最新版本 v' + d.current;
                status.style.color = '#4ade80';
            } else {
                status.textContent = '检查失败，请稍后重试';
                status.style.color = 'var(--text-dim)';
            }
        })
        .catch(function() {
            clearInterval(timer);
            status.textContent = '网络错误，无法检查更新';
            status.style.color = 'var(--text-dim)';
        });
}"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>设置 | KeyHeatmap</title>
<style>
{SETTINGS_CSS}
</style>
</head>
<body class="theme-{theme}">

<div class="header" style="margin-top:14px;"><h1>设置</h1></div>
<div class="tabs">
    <a class="tab" href="/?theme={theme}">热力图</a>
    <a class="tab active" href="/?page=settings&theme={theme}">设置</a>
    <span style="flex:1"></span>
    <a class="tab tab-theme" href="/?theme={next_theme}" title="切换主题">{theme_icons.get(theme, '🌙')}</a>
</div>
<div class="container">
{toggle_row("Combo 浮窗", "控制右下角按键连击浮窗的显示。关闭后所有浮窗（桌面和游戏内）都不会出现。", combo_on, "运行中", "已关闭", "toggle_combo_float")}
{toggle_row("游戏内计数", "关闭后，在白名单游戏中的按键不会被计入统计。桌面应用照常计数。", game_count_on, "计数中", "已暂停", "toggle_game_counting")}
    <div class="card">
        <div class="card-title">游戏白名单</div>
        <div class="card-desc">白名单中的进程会被识别为游戏，受上述两项设置的影响。通过 Ctrl+Shift+F8 热键也可快速添加/移除当前前台进程。</div>
        <div class="wl-section">
            <div class="wl-list">
{wl_html}
</div>
            <form class="wl-add" action="/?page=settings" method="GET">
                <input type="hidden" name="page" value="settings">
                <input type="hidden" name="action" value="add_whitelist">
                <input type="hidden" name="theme" value="{theme}">
                <input type="text" name="process" placeholder="输入进程名，如 R6s.exe">
                <button type="submit">添加</button>
            </form>
        </div>
    </div>
    <div class="card">
        <div class="card-title">自动主题切换</div>
        <div class="card-desc">设置白天和夜晚的切换时间。选择 <b>auto</b> 主题后，页面会在设定时间自动切换亮色/暗色主题。</div>
        <form class="time-form" action="/?page=settings" method="GET">
            <input type="hidden" name="page" value="settings">
            <input type="hidden" name="action" value="save_theme_times">
            <input type="hidden" name="theme" value="{theme}">
            <div class="time-row">
                <label>白天开始</label>
                <input type="time" name="day_time" value="{day_time}">
                <label>夜晚开始</label>
                <input type="time" name="night_time" value="{night_time}">
                <button type="submit">保存</button>
            </div>
        </form>
    </div>
    <div class="card">
        <div class="card-title">浮窗透明度</div>
        <div class="card-desc">控制右下角按键浮窗的透明度。100 为完全不透明，30 为最透明。</div>
        <div class="slider-row">
            <input type="range" id="opacity-slider" min="30" max="100" value="{opacity}" oninput="document.getElementById('opacity-val').textContent=this.value">
            <span class="slider-val" id="opacity-val">{opacity}</span>
            <button class="toggle-btn toggle-on" style="padding:6px 12px;border:none;cursor:pointer;font-size:13px;flex-shrink:0;" onclick="saveOpacity();return false;">应用</button>
        </div>
    </div>
{toggle_row("毛玻璃效果", "为浮窗背景添加模糊效果，类似 Windows 亚克力材质。配合半透明效果更佳。", glass_on, "已启用", "已关闭", "toggle_float_blur")}
{toggle_row("鼠标统计", "开启后统计鼠标左键（LMB）、右键（RMB）和中键/滚轮按压（MMB）的点击次数，与键盘按键一同展示在热力图中。", mouse_track_on, "已启用", "已关闭", "toggle_mouse_tracking")}
{toggle_row("鼠标浮窗", "控制鼠标点击是否显示在右下角实时浮窗中。仅当「鼠标统计」开启时生效。", mouse_overlay_on, "已启用", "已关闭", "toggle_mouse_in_overlay")}
{toggle_row("自动更新", "开启后打开仪表盘时会自动检测 GitHub 上的最新版本。关闭则不会自动检测。", auto_update_on, "已启用", "已关闭", "toggle_auto_update")}
{toggle_row("开机自启", "开启后 KeyHeatmap 会随 Windows 开机自动启动，无需手动打开。", autostart_on, "已启用", "已关闭", "toggle_autostart")}
    <div class="card">
        <div class="card-title">版本更新</div>
        <div class="card-desc">当前版本：<b style="color:#a855f7">v{CURRENT_VERSION}</b>。点击下方按钮手动检测是否有新版本可用。</div>
        <div class="row" style="margin-top:12px;">
            <a class="toggle-btn toggle-on" style="padding:8px 20px;border:none;cursor:pointer;font-size:13px;text-decoration:none;" onclick="checkUpdateNow();return false;">检查更新</a>
            <span id="check-status" style="color:var(--text-dim);font-size:13px;margin-left:12px;"></span>
        </div>
    </div>
</div>
<div class="footer">KeyHeatmap - 设置实时生效</div>
<script>
var THEME_PARAM = "{theme}";
var DAY_TIME = "{day_time}";
var NIGHT_TIME = "{night_time}";

function timeToMinutes(t) {{
    var parts = t.split(':');
    return parseInt(parts[0]) * 60 + parseInt(parts[1]);
}}
function isNightNow() {{
    var now = new Date();
    var cur = now.getHours() * 60 + now.getMinutes();
    var d = timeToMinutes(DAY_TIME);
    var n = timeToMinutes(NIGHT_TIME);
    return cur >= n || cur < d;
}}
function resolveTheme(t) {{
    if (t === 'auto') return isNightNow() ? 'dark' : 'light';
    return t;
}}
function applyThemeClass() {{
    document.body.className = 'theme-' + resolveTheme(THEME_PARAM);
}}
var lastThemeApplied = resolveTheme(THEME_PARAM);
applyThemeClass();
setInterval(function() {{
    if (THEME_PARAM !== 'auto') return;
    var current = resolveTheme('auto');
    if (current !== lastThemeApplied) {{
        lastThemeApplied = current;
        applyThemeClass();
    }}
}}, 30000);
function cycleTheme() {{
    var next = {{'light':'dark','dark':'auto','auto':'light'}}[THEME_PARAM];
    var params = new URLSearchParams(window.location.search);
    params.set('theme', next);
    window.location.search = params.toString();
}}
function saveOpacity() {{
    var val = document.getElementById('opacity-slider').value;
    var params = new URLSearchParams(window.location.search);
    params.set('action', 'save_float_opacity');
    params.set('opacity', val);
    window.location.href = '/?' + params.toString();
}}
{CHECK_UPDATE_JS}
</script>
</body>
</html>"""


# ─── HTTP 服务器 ───────────────────────────────

class HeatmapHandler(BaseHTTPRequestHandler):
    stats: KeyStats = None
    settings: Settings = None
    overlay = None

    def log_message(self, format, *args): pass

    def _json_response(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _parse_query(self):
        parsed = urlparse(self.path)
        return parse_qs(parsed.query)

    def _handle_api(self, path):
        if path == "/api/check-update":
            result = check_update()
            if result is None:
                self._json_response({
                    "has_update": False,
                    "current": CURRENT_VERSION,
                    "latest": CURRENT_VERSION,
                    "changelog": "",
                })
            else:
                has, latest, changelog = result
                self._json_response({
                    "has_update": has,
                    "current": CURRENT_VERSION,
                    "latest": latest,
                    "changelog": changelog,
                })
            return True

        if path == "/api/update/download":
            self._json_response({"status": "downloading"})
            def _bg_download():
                download_update()
            threading.Thread(target=_bg_download, daemon=True).start()
            return True

        if path == "/api/update/apply":
            downloaded = download_update()
            if downloaded:
                apply_update(downloaded)
                self._json_response({"status": "applying"})
            else:
                self._json_response({"error": "下载失败"}, 500)
            return True

        if path == "/api/settings":
            auto = self.settings.get("auto_update", True)
            skip = self.settings.get("update_skip_until")
            self._json_response({
                "auto_update": auto,
                "update_skip_until": skip,
                "version": CURRENT_VERSION,
                "combo_float_enabled": self.settings.get("combo_float_enabled", True),
                "game_counting_enabled": self.settings.get("game_counting_enabled", True),
                "glass_enabled": self.settings.get("glass_enabled", True),
                "mouse_tracking_enabled": self.settings.get("mouse_tracking_enabled", False),
                "mouse_in_overlay": self.settings.get("mouse_in_overlay", False),
                "float_opacity": self.settings.get("float_opacity", 88),
            })
            return True

        if path == "/api/stats/hourly":
            self._json_response(self.stats.get_hourly_data())
            return True

        if path == "/api/stats/trend":
            self._json_response(self.stats.get_trend_data())
            return True

        if path == "/api/stats/badge":
            self._json_response({"badge": self.stats.get_badge()})
            return True

        if path == "/api/stats/ranking":
            params = self._parse_query()
            period = params.get("period", ["all"])[0]
            ranking = self.stats.get_ranking(period)
            self._json_response({"ranking": ranking})
            return True

        if path == "/api/toggle_mouse_tracking":
            current = self.settings.get("mouse_tracking_enabled", False)
            self.settings.set("mouse_tracking_enabled", not current)
            self._json_response({"mouse_tracking_enabled": not current})
            return True

        return False

    def _handle_api_post(self, path):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            data = json.loads(body)
        except:
            data = {}

        if path == "/api/settings":
            if "auto_update" in data:
                self.settings.set("auto_update", bool(data["auto_update"]))
            if "update_skip_until" in data:
                self.settings.set("update_skip_until", data["update_skip_until"])
            self._json_response({"ok": True})
            return True

        if path == "/api/update/skip":
            self.settings.set_skip_7_days()
            self._json_response({"ok": True})
            return True

        return False

    def _handle_settings_action(self, params):
        """处理设置页面的 GET 请求和操作"""
        action = params.get("action", [None])[0]
        theme = params.get("theme", ["dark"])[0]

        if action == "toggle_combo_float":
            current = self.settings.get("combo_float_enabled", True)
            self.settings.set("combo_float_enabled", not current)
            self._redirect_to_settings(theme)
            return True

        if action == "toggle_game_counting":
            current = self.settings.get("game_counting_enabled", True)
            self.settings.set("game_counting_enabled", not current)
            self._redirect_to_settings(theme)
            return True

        if action == "toggle_float_blur":
            current = self.settings.get("glass_enabled", True)
            self.settings.set("glass_enabled", not current)
            if HeatmapHandler.overlay:
                HeatmapHandler.overlay.set_blur(not current)
            self._redirect_to_settings(theme)
            return True

        if action == "toggle_mouse_tracking":
            current = self.settings.get("mouse_tracking_enabled", False)
            self.settings.set("mouse_tracking_enabled", not current)
            self._redirect_to_settings(theme)
            return True

        if action == "toggle_mouse_in_overlay":
            current = self.settings.get("mouse_in_overlay", False)
            self.settings.set("mouse_in_overlay", not current)
            self._redirect_to_settings(theme)
            return True

        if action == "toggle_auto_update":
            current = self.settings.get("auto_update", True)
            self.settings.set("auto_update", not current)
            self._redirect_to_settings(theme)
            return True

        if action == "toggle_autostart":
            tray = getattr(HeatmapHandler, "tray", None)
            if tray and hasattr(tray, "_toggle_autostart"):
                tray._toggle_autostart()
            self._redirect_to_settings(theme)
            return True

        if action == "add_whitelist":
            proc = params.get("process", [""])[0].strip()
            if proc:
                wl = list(self.settings.get("game_whitelist", []))
                if proc not in wl:
                    wl.append(proc)
                    self.settings.set("game_whitelist", sorted(wl))
            self._redirect_to_settings(theme)
            return True

        if action == "remove_whitelist":
            proc = params.get("process", [""])[0].strip()
            if proc:
                wl = list(self.settings.get("game_whitelist", []))
                if proc in wl:
                    wl.remove(proc)
                    self.settings.set("game_whitelist", sorted(wl))
            self._redirect_to_settings(theme)
            return True

        if action == "save_theme_times":
            day_time = params.get("day_time", ["06:00"])[0]
            night_time = params.get("night_time", ["18:00"])[0]
            self.settings.set("theme_day_time", day_time)
            self.settings.set("theme_night_time", night_time)
            self._redirect_to_settings(theme)
            return True

        if action == "save_float_opacity":
            opacity = params.get("opacity", ["88"])[0]
            try:
                val = int(opacity)
                val = max(30, min(100, val))
                self.settings.set("float_opacity", val)
                if HeatmapHandler.overlay:
                    HeatmapHandler.overlay.set_window_opacity(val / 100.0)
            except:
                pass
            self._redirect_to_settings(theme)
            return True

        return False

    def _redirect_to_settings(self, theme):
        self.send_response(302)
        self.send_header("Location", f"/?page=settings&theme={theme}")
        self.end_headers()

    def _serve_settings_page(self, theme):
        html = build_settings_html(self.settings, theme)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_dashboard_page(self, period, theme):
        data = self.stats.get_data(period)
        max_count = max(data.values()) if data else 1
        labels = {"today": "今日统计", "week": "近7天统计", "all": "全部历史统计"}
        badge = self.stats.get_badge()
        ranking = self.stats.get_ranking(period)
        hourly = self.stats.get_hourly_data()
        trend = self.stats.get_trend_data()
        # 鼠标数据（热力图始终显示；排行根据开关过滤）
        mouse_track = self.settings.get("mouse_tracking_enabled", False)
        all_data = self.stats.get_data(period)
        mouse_data = {k: all_data.get(k, 0) for k in MOUSE_KEYS}
        if not mouse_track:
            ranking = [(k, v) for k, v in ranking if k not in MOUSE_KEYS]

        resolved_theme = theme
        if theme == "auto":
            resolved_theme = HeatmapHandler.overlay._read_theme() if HeatmapHandler.overlay else "dark"
        html = build_dashboard_html(data, labels.get(period, period), max_count,
                                     resolved_theme, badge, ranking, hourly, trend,
                                     mouse_data, self.settings, url_theme=theme)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            params = self._parse_query()

            # API routes
            if path.startswith("/api/"):
                self._handle_api(path)
                return

            # Settings actions
            page = params.get("page", [None])[0]
            if page == "settings":
                if self._handle_settings_action(params):
                    return

            theme = params.get("theme", ["dark"])[0]
            if theme not in ("light", "dark", "auto"):
                theme = "dark"

            if page == "settings":
                self._serve_settings_page(theme)
                return

            period = params.get("period", ["today"])[0]
            if period not in ("today", "week", "all"):
                period = "today"

            self._serve_dashboard_page(period, theme)

        except Exception as e:
            log(f"HTTP error: {e}")
            try:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"Error: {e}".encode())
            except:
                pass

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            if path.startswith("/api/"):
                self._handle_api_post(path)
                return
            self._json_response({"error": "not found"}, 404)
        except Exception as e:
            log(f"HTTP POST error: {e}")
            try:
                self._json_response({"error": str(e)}, 500)
            except:
                pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class HeatmapServer:
    def __init__(self, stats: KeyStats, settings: Settings, overlay=None):
        self.stats = stats
        self.settings = settings
        self.overlay = overlay
        self.port = self._find_port()
        self.server = None

    def _find_port(self):
        for port in range(18888, 19000):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", port))
                s.close()
                return port
            except OSError:
                continue
        return 18888

    def start(self):
        HeatmapHandler.stats = self.stats
        HeatmapHandler.settings = self.settings
        HeatmapHandler.overlay = self.overlay
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), HeatmapHandler)
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        log(f"HTTP server on port {self.port}")

    def open_browser(self, period="today"):
        webbrowser.open(f"http://127.0.0.1:{self.port}/?period={period}")

    def stop(self):
        if self.server:
            self.server.shutdown()


# ─── 虚拟键码 → 显示名映射（用于轮询模式） ───

VK_TO_DISPLAY = {
    0x1B: "Esc",
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4",
    0x74: "F5", 0x75: "F6", 0x76: "F7", 0x77: "F8",
    0x78: "F9", 0x79: "F10", 0x7A: "F11", 0x7B: "F12",
    0x2C: "PrtSc", 0x91: "ScrLk", 0x13: "Pause",
    0x30: "0", 0x31: "1", 0x32: "2", 0x33: "3", 0x34: "4",
    0x35: "5", 0x36: "6", 0x37: "7", 0x38: "8", 0x39: "9",
    0x41: "A", 0x42: "B", 0x43: "C", 0x44: "D", 0x45: "E",
    0x46: "F", 0x47: "G", 0x48: "H", 0x49: "I", 0x4A: "J",
    0x4B: "K", 0x4C: "L", 0x4D: "M", 0x4E: "N", 0x4F: "O",
    0x50: "P", 0x51: "Q", 0x52: "R", 0x53: "S", 0x54: "T",
    0x55: "U", 0x56: "V", 0x57: "W", 0x58: "X", 0x59: "Y", 0x5A: "Z",
    0x10: "Shift", 0xA0: "Shift", 0xA1: "Shift",
    0x11: "Ctrl", 0xA2: "Ctrl", 0xA3: "Ctrl",
    0x12: "Alt", 0xA4: "Alt", 0xA5: "Alt",
    0x5B: "Win", 0x5C: "Win",
    0x5D: "Menu",
    0x08: "⌫", 0x09: "Tab", 0x0D: "↵",
    0x20: "Sp", 0x14: "Caps",
    0x2D: "Ins", 0x2E: "Del",
    0x24: "Home", 0x23: "End",
    0x21: "PgUp", 0x22: "PgDn",
    0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    0x90: "NumLk",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-",
    0xBE: ".", 0xBF: "/", 0xC0: "`",
    0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
    0x60: "0", 0x61: "1", 0x62: "2", 0x63: "3", 0x64: "4",
    0x65: "5", 0x66: "6", 0x67: "7", 0x68: "8", 0x69: "9",
    0x6A: "*", 0x6B: "+", 0x6D: "-", 0x6E: ".", 0x6F: "/",
}

_POLLING_VK_LIST = sorted(VK_TO_DISPLAY.keys())

# 鼠标虚拟键码
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04

MOUSE_VK_MAP = {VK_LBUTTON: "LMB", VK_RBUTTON: "RMB", VK_MBUTTON: "MMB"}
_MOUSE_VK_LIST = [VK_LBUTTON, VK_RBUTTON, VK_MBUTTON]


# ─── 键盘监听 ──────────────────────────────────

class KeyListener:
    def __init__(self, stats: KeyStats, overlay: KeyOverlay = None, settings: Settings = None):
        self.stats = stats
        self.overlay = overlay
        self.settings = settings
        self.listener = None
        self.paused = False
        self._key_count = 0
        self._log_count = 0
        self._pressed_keys = set()
        self._heartbeat_stop = threading.Event()

        # 轮询模式
        self._polling_thread = None
        self._polling_stop = threading.Event()
        self._polling_active = False
        self._prev_state = {}
        self._cooldown_until = {}
        self._switch_timer = None
        self._mode = "pynput"

        # 鼠标轮询
        self._mouse_polling_thread = None
        self._mouse_polling_stop = threading.Event()
        self._mouse_prev_state = {}

        # 游戏白名单
        self._game_whitelist = set()
        self._hotkey_cooldown = 0.0
        self._fg_proc_name = ""
        self._fg_proc_name_ts = 0.0

    def _load_game_whitelist(self):
        wl = self.settings.get("game_whitelist", []) if self.settings else []
        self._game_whitelist = set(wl)

    def _save_game_whitelist(self):
        if self.settings:
            self.settings.set("game_whitelist", sorted(self._game_whitelist))

    def _get_foreground_process_name(self):
        now = time.time()
        if now - self._fg_proc_name_ts < 2.0:
            return self._fg_proc_name
        self._fg_proc_name_ts = now
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                self._fg_proc_name = ""
                return ""
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            TH32CS_SNAPPROCESS = 0x00000002
            kernel32 = ctypes.windll.kernel32

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", ctypes.wintypes.DWORD),
                    ("cntUsage", ctypes.wintypes.DWORD),
                    ("th32ProcessID", ctypes.wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.wintypes.ULONG)),
                    ("th32ModuleID", ctypes.wintypes.DWORD),
                    ("cntThreads", ctypes.wintypes.DWORD),
                    ("th32ParentProcessID", ctypes.wintypes.DWORD),
                    ("pcPriClassBase", ctypes.wintypes.LONG),
                    ("dwFlags", ctypes.wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260),
                ]

            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snapshot == -1:
                self._fg_proc_name = ""
                return ""
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            name = ""
            if kernel32.Process32First(snapshot, ctypes.byref(entry)):
                while True:
                    if entry.th32ProcessID == pid.value:
                        name = entry.szExeFile.decode("utf-8", errors="ignore")
                        break
                    if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                        break
            kernel32.CloseHandle(snapshot)
            self._fg_proc_name = name
            return name
        except:
            self._fg_proc_name = ""
            return ""

    def _should_suppress_float(self):
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                style = ctypes.windll.user32.GetWindowLongW(hwnd, -16)
                if not (style & 0x00C00000):
                    rect = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    sw = ctypes.windll.user32.GetSystemMetrics(0)
                    sh = ctypes.windll.user32.GetSystemMetrics(1)
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    if abs(w - sw) <= 10 and abs(h - sh) <= 10:
                        return True
            proc_name = self._get_foreground_process_name()
            if proc_name and proc_name in self._game_whitelist:
                return True
        except:
            pass
        return False

    def _should_count_key(self):
        """游戏内计数开关：关闭时白名单游戏中的按键不计数"""
        game_count_on = self.settings.get("game_counting_enabled", True) if self.settings else True
        if game_count_on:
            return True
        proc_name = self._get_foreground_process_name()
        if proc_name and proc_name in self._game_whitelist:
            return False
        return True

    def _toggle_game_whitelist(self):
        proc_name = self._get_foreground_process_name()
        if not proc_name:
            log("hotkey: no foreground process name")
            return
        if proc_name in self._game_whitelist:
            self._game_whitelist.discard(proc_name)
            self._save_game_whitelist()
            log(f"hotkey: REMOVED '{proc_name}' from game whitelist")
        else:
            self._game_whitelist.add(proc_name)
            self._save_game_whitelist()
            log(f"hotkey: ADDED '{proc_name}' to game whitelist")

    def _on_press(self, key):
        if self.paused:
            return
        if key in self._pressed_keys:
            return
        self._pressed_keys.add(key)
        try:
            from pynput.keyboard import Key
            if (key == Key.f8 and
                Key.ctrl in self._pressed_keys and
                Key.shift in self._pressed_keys):
                now = time.time()
                if now - self._hotkey_cooldown > 3.0:
                    self._hotkey_cooldown = now
                    threading.Thread(target=self._toggle_game_whitelist, daemon=True).start()
                    return
        except:
            pass
        self._handle_key(key)

    def _on_release(self, key):
        self._pressed_keys.discard(key)

    def _handle_key(self, key):
        try:
            if not self._should_count_key():
                return
            name = self.stats.get_display_name(key)
            if name:
                self.stats.record(name)
                self._key_count += 1
                self._log_count += 1
                if self._log_count <= 10 or self._log_count % 200 == 0:
                    log(f"keys captured: {self._key_count}")
                combo_on = self.settings.get("combo_float_enabled", True) if self.settings else True
                if self.overlay and combo_on and not self._should_suppress_float():
                    self.overlay.on_key(name)
        except Exception as e:
            log(f"on_press error: {e}")

    def _handle_key_by_name(self, name):
        try:
            if not self._should_count_key():
                return
            self.stats.record(name)
            self._key_count += 1
            self._log_count += 1
            if self._log_count <= 10 or self._log_count % 200 == 0:
                log(f"keys captured: {self._key_count}")
            combo_on = self.settings.get("combo_float_enabled", True) if self.settings else True
            if self.overlay and combo_on and not self._should_suppress_float():
                self.overlay.on_key(name)
        except Exception as e:
            log(f"polling on_press error: {e}")

    def _mouse_polling_loop(self):
        """GetAsyncKeyState 轮询鼠标按键，每 30ms 扫描一次"""
        user32 = ctypes.windll.user32
        get_key_state = user32.GetAsyncKeyState
        get_key_state.restype = ctypes.c_short

        while not self._mouse_polling_stop.wait(0.030):
            if self.paused:
                continue

            mouse_track = self.settings.get("mouse_tracking_enabled", False) if self.settings else False
            if not mouse_track:
                continue

            for vk in _MOUSE_VK_LIST:
                state = get_key_state(vk)
                is_down = (state & 0x8000) != 0
                was_down = self._mouse_prev_state.get(vk, False)

                if is_down and not was_down:
                    name = MOUSE_VK_MAP.get(vk)
                    if name:
                        if not self._should_count_key():
                            continue
                        self.stats.record(name)
                        self._key_count += 1
                        self._log_count += 1
                        mouse_overlay = self.settings.get("mouse_in_overlay", False) if self.settings else False
                        combo_on = self.settings.get("combo_float_enabled", True) if self.settings else True
                        if self.overlay and combo_on and mouse_overlay and not self._should_suppress_float():
                            self.overlay.on_key(name)

                self._mouse_prev_state[vk] = is_down

    def _start_mouse_polling(self):
        """启动鼠标轮询线程（GetAsyncKeyState，不依赖 pynput.mouse）"""
        self._mouse_polling_stop.clear()
        self._mouse_polling_thread = threading.Thread(
            target=self._mouse_polling_loop, daemon=True, name="polling-mouse"
        )
        self._mouse_polling_thread.start()
        log("mouse polling started (GetAsyncKeyState)")

    def _heartbeat_loop(self):
        stall_cycles = 0
        last_count = 0
        started_at = time.time()
        while not self._heartbeat_stop.wait(8.0):
            current = self._key_count
            if self._mode == "pynput" and current == 0 and time.time() - started_at > 60:
                log("pynput never received any keys in 60s, switching to tk polling")
                if not self._polling_active:
                    self._start_polling_tk()
                continue
            if current == last_count:
                stall_cycles += 1
                if stall_cycles >= 2 and current > 0:
                    listener_alive = self.listener.is_alive() if self.listener else False
                    log(f"HOOK WARNING: no keys in 16s, listener_alive={listener_alive}, mode={self._mode}")
                    if not listener_alive and not self._polling_active:
                        log("pynput listener died, attempting restart")
                        try:
                            from pynput import keyboard
                            self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
                            self.listener.start()
                            time.sleep(0.3)
                            if self.listener.is_alive():
                                log("pynput restarted successfully")
                                self._mode = "pynput"
                                stall_cycles = 0
                                continue
                        except Exception as e:
                            log(f"pynput restart failed: {e}")
                    if not self._polling_active:
                        log("auto-switching to tk polling mode")
                        self._start_polling_tk()
            else:
                stall_cycles = 0
            last_count = current

    def _start_polling(self):
        if self._polling_active:
            return
        if self.listener:
            def _stop_listener():
                try:
                    self.listener.stop()
                except Exception:
                    pass
            t = threading.Thread(target=_stop_listener, daemon=True, name="stop-pynput")
            t.start()
            t.join(timeout=1.0)
        self._mode = "polling"
        self._polling_stop.clear()
        self._polling_active = True
        self._polling_thread = threading.Thread(
            target=self._polling_loop, daemon=True, name="polling-kb"
        )
        self._polling_thread.start()
        log("polling mode started, pynput stopped")

    def _polling_loop(self):
        user32 = ctypes.windll.user32
        get_key_state = user32.GetAsyncKeyState
        get_key_state.restype = ctypes.c_short
        scan_list = _POLLING_VK_LIST
        poll_ticks = 0
        poll_any_down = 0

        while not self._polling_stop.wait(0.030):
            poll_ticks += 1
            if poll_ticks % 300 == 0:
                log(f"poll tick #{poll_ticks}, paused={self.paused}, any_down_frames={poll_any_down}")
                poll_any_down = 0
            if self.paused:
                continue

            ctrl = get_key_state(0x11) & 0x8000
            shift = get_key_state(0x10) & 0x8000
            f8 = get_key_state(0x77) & 0x8000
            if ctrl and shift and f8:
                now = time.time()
                if now - self._hotkey_cooldown > 3.0:
                    self._hotkey_cooldown = now
                    threading.Thread(target=self._toggle_game_whitelist, daemon=True).start()

            frame_has_down = False
            for vk in scan_list:
                state = get_key_state(vk)
                is_down = (state & 0x8000) != 0
                was_down = self._prev_state.get(vk, False)
                if is_down:
                    frame_has_down = True
                if is_down and not was_down:
                    now = time.time()
                    if now >= self._cooldown_until.get(vk, 0):
                        name = VK_TO_DISPLAY[vk]
                        self._handle_key_by_name(name)
                        self._cooldown_until[vk] = now + 0.15
                self._prev_state[vk] = is_down
            if frame_has_down:
                poll_any_down += 1

    def _start_polling_tk(self):
        if self._polling_active:
            return
        if not self.overlay or not self.overlay.hwnd:
            log("polling: overlay not ready, cannot start")
            return
        if self.listener:
            def _stop_listener():
                try:
                    self.listener.stop()
                except Exception:
                    pass
            t = threading.Thread(target=_stop_listener, daemon=True)
            t.start()
            t.join(timeout=1.0)
        self._mode = "polling"
        self._polling_stop.clear()
        self._polling_active = True
        self._prev_state.clear()
        self._cooldown_until.clear()
        try:
            self.overlay.schedule(30, self._polling_tk_tick)
        except Exception as e:
            log(f"polling: schedule() failed: {e}")
            self._polling_active = False
            return
        log("tk polling mode started (main thread after loop), pynput stopped")

    def _polling_tk_tick(self):
        if not self._polling_active or self._polling_stop.is_set():
            self._polling_active = False
            return
        try:
            user32 = ctypes.windll.user32
            get_key_state = user32.GetAsyncKeyState
            get_key_state.restype = ctypes.c_short

            self._poll_tick = getattr(self, '_poll_tick', 0) + 1
            if self._poll_tick % 167 == 0:
                any_down = any((get_key_state(vk) & 0x8000) for vk in _POLLING_VK_LIST)
                log(f"tk poll alive tick=#{self._poll_tick} any_key_down={any_down}")

            if not self.paused:
                ctrl = get_key_state(0x11) & 0x8000
                shift = get_key_state(0x10) & 0x8000
                f8 = get_key_state(0x77) & 0x8000
                if ctrl and shift and f8:
                    now = time.time()
                    if now - self._hotkey_cooldown > 3.0:
                        self._hotkey_cooldown = now
                        threading.Thread(target=self._toggle_game_whitelist, daemon=True).start()

                for vk in _POLLING_VK_LIST:
                    state = get_key_state(vk)
                    is_down = (state & 0x8000) != 0
                    was_down = self._prev_state.get(vk, False)
                    if is_down and not was_down:
                        now = time.time()
                        if now >= self._cooldown_until.get(vk, 0):
                            name = VK_TO_DISPLAY.get(vk)
                            if name:
                                self._handle_key_by_name(name)
                                self._cooldown_until[vk] = now + 0.15
                    self._prev_state[vk] = is_down
        except Exception as e:
            log(f"tk polling tick error: {e}")

        if self._polling_active and not self._polling_stop.is_set():
            try:
                self.overlay.schedule(30, self._polling_tk_tick)
            except Exception:
                self._polling_active = False

    def start(self):
        self._load_game_whitelist()
        try:
            from pynput import keyboard
            self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            self.listener.start()
            time.sleep(0.3)
            alive = self.listener.is_alive()
            log(f"listener started, alive={alive}, mode=pynput")
            self._mode = "pynput"
            self._heartbeat_stop.clear()
            threading.Thread(target=self._heartbeat_loop, daemon=True, name="hook-hb").start()
        except Exception as e:
            log(f"listener start failed: {e}, falling back to polling")
            self._start_polling_tk()

        # 启动鼠标轮询
        self._start_mouse_polling()

    def toggle_pause(self):
        self.paused = not self.paused
        return self.paused

    def stop(self):
        if self.listener:
            self.listener.stop()
        self._polling_stop.set()
        self._heartbeat_stop.set()
        self._mouse_polling_stop.set()
        log(f"listener stopped, total keys={self._key_count}, mode={self._mode}")


# ─── 系统托盘 ──────────────────────────────────

def create_tray_icon():
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 16, 60, 56], radius=6, fill=(80, 200, 120), outline=(60, 170, 100), width=2)
    for row in range(3):
        y = 22 + row * 10
        for col in range(5):
            x = 10 + col * 10
            draw.rounded_rectangle([x, y, x+7, y+6], radius=1, fill=(255, 255, 255, 200))
    for col in range(4):
        x = 15 + col * 10
        draw.rounded_rectangle([x, y+26, x+7, y+6+26], radius=1, fill=(255, 255, 255, 200))
    return img


class TrayApp:
    def __init__(self, stats, listener, server, overlay):
        self.stats = stats
        self.listener = listener
        self.server = server
        self.overlay = overlay
        self.autostart_enabled = self._check_autostart()
        self.icon = None

    def _get_autostart_cmd(self):
        if getattr(sys, 'frozen', False):
            return f'"{sys.executable}"'
        py_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(py_dir, "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        script_path = os.path.abspath(sys.argv[0])
        return f'"{pythonw}" "{script_path}"'

    def _check_autostart(self):
        try:
            result = subprocess.run(['schtasks', '/query', '/tn', 'KeyHeatmap'], capture_output=True, text=True)
            return result.returncode == 0
        except:
            return False

    def _ensure_autostart(self):
        if not self.autostart_enabled:
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\KeyHeatmap")
                was_enabled, _ = winreg.QueryValueEx(key, "AutostartWasEnabled")
                winreg.CloseKey(key)
                if was_enabled:
                    log("auto-repairing autostart (was previously enabled)")
                    self._create_autostart()
                    self.autostart_enabled = True
            except (FileNotFoundError, OSError):
                pass

    def _create_autostart(self):
        cmd = self._get_autostart_cmd()
        subprocess.run(['schtasks', '/create', '/tn', 'KeyHeatmap', '/tr', cmd, '/sc', 'onlogon', '/f', '/rl', 'limited'], capture_output=True, text=True)
        try:
            import winreg
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\KeyHeatmap")
            winreg.SetValueEx(key, "AutostartWasEnabled", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(key)
        except OSError:
            pass

    def _remove_autostart(self):
        subprocess.run(['schtasks', '/delete', '/tn', 'KeyHeatmap', '/f'], capture_output=True)
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\KeyHeatmap", 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "AutostartWasEnabled", 0, winreg.REG_DWORD, 0)
            winreg.CloseKey(key)
        except (FileNotFoundError, OSError):
            pass

    def _toggle_autostart(self):
        if self.autostart_enabled:
            self._remove_autostart()
            self.autostart_enabled = False
        else:
            self._create_autostart()
            self.autostart_enabled = True

    def _on_quit(self, icon, item):
        self.stats.save_and_snapshot()
        self.listener.stop()
        self.server.stop()
        if self.overlay:
            self.overlay.stop()
        icon.stop()
        os._exit(0)

    def _on_show(self, icon, item):
        self.server.open_browser("today")

    def _on_pause(self, icon, item):
        self.listener.toggle_pause()
        self._update_menu()

    def _on_autostart(self, icon, item):
        self._toggle_autostart()
        self._update_menu()

    def _update_menu(self):
        from pystray import Menu, MenuItem
        pause_label = "▶ 恢复记录" if self.listener.paused else "⏸ 暂停记录"
        auto_label = "🚀 开机自启  ✓" if self.autostart_enabled else "🚀 开机自启"
        self.icon.menu = Menu(
            MenuItem("📊 查看热力图", self._on_show, default=True),
            MenuItem(pause_label, self._on_pause),
            MenuItem(auto_label, self._on_autostart),
            Menu.SEPARATOR,
            MenuItem("❌ 退出", self._on_quit),
        )

    def run(self):
        from pystray import Menu, MenuItem, Icon
        tray_icon = create_tray_icon()
        auto_label = "🚀 开机自启  ✓" if self.autostart_enabled else "🚀 开机自启"
        menu = Menu(
            MenuItem("📊 查看热力图", self._on_show, default=True),
            MenuItem("⏸ 暂停记录", self._on_pause),
            MenuItem(auto_label, self._on_autostart),
            Menu.SEPARATOR,
            MenuItem("❌ 退出", self._on_quit),
        )
        self.icon = Icon("KeyHeatmap", tray_icon, "键盘热力图", menu)

        # ─── 托盘：无交互桌面时静默跳过，不影响其他功能 ───
        try:
            self.icon.run()
        except OSError as e:
            log(f"tray icon unavailable (non-interactive desktop): {e}")
            # 托盘不可用但程序继续运行：仪表盘 localhost:18888、浮窗、按键记录均正常
            # 保持进程不退出，等待 Ctrl+C 或任务管理器关闭
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass


# ─── 主入口 ────────────────────────────────────

def main():
    log("=== KeyHeatmap v3.2 starting ===")

    import socket as sock_mod
    try:
        probe = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
        probe.settimeout(0.5)
        probe.connect(('127.0.0.1', 18888))
        probe.close()
        log("another instance running, opening browser")
        webbrowser.open("http://127.0.0.1:18888/?period=today")
        return
    except (sock_mod.error, ConnectionRefusedError, OSError):
        pass

    log("initializing components")

    settings = Settings()
    stats = KeyStats()

    overlay = OverlayEngine()
    overlay.start()
    log("overlay started (Win32 DIB + UpdateLayeredWindow)")
    overlay.set_blur(settings.get("glass_enabled", True))
    overlay.set_window_opacity(settings.get("float_opacity", 88) / 100.0)

    server = HeatmapServer(stats, settings, overlay)
    server.start()

    listener = KeyListener(stats, overlay, settings)
    listener.start()

    log("all components started, entering tray loop")
    tray = TrayApp(stats, listener, server, overlay)
    HeatmapHandler.tray = tray
    try:
        tray._ensure_autostart()
    except Exception as e:
        log(f"_ensure_autostart error: {e}")
    try:
        tray.run()
    except Exception as e:
        import traceback
        log(f"tray.run() crashed: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
