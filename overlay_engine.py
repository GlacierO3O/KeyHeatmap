"""
KeyHeatmap Overlay Engine v2.0
Windows native layered window (UpdateLayeredWindow) for GPU-compatible rendering.
Replaces tkinter Canvas overlay — WS_EX_LAYERED ensures visibility on
DWM-composited windows (Marvis, games, etc.) that tkinter GDI canvas couldn't cover.
"""

import ctypes
import ctypes.wintypes
import threading
import time
import math
import traceback
from collections import deque
from ctypes import wintypes

import numpy as np
from scipy.ndimage import uniform_filter

# logger import from parent module (injected at __init__)
_log_fn = None

def log(msg: str):
    """Bridge to keyheatmap's log function."""
    if _log_fn:
        _log_fn(msg)

# ─── Win32 API 常量 ──────────────────────────────────────────────────────

# 窗口类 / 扩展样式
WS_EX_LAYERED     = 0x00080000   # DWM 合成层窗口
WS_EX_TRANSPARENT = 0x00000020   # 鼠标穿透
WS_EX_TOOLWINDOW  = 0x00000080   # 隐藏任务栏图标
WS_EX_NOACTIVATE  = 0x08000000   # 不抢焦点
WS_EX_TOPMOST     = 0x00000008   # 置顶
WS_POPUP          = 0x80000000   # 无标题栏弹出窗口
WS_VISIBLE        = 0x10000000

# GWL 索引
GWL_EXSTYLE = -20
GWL_STYLE   = -16

# SetWindowPos 标志
SWP_NOMOVE       = 0x0002
SWP_NOSIZE       = 0x0001
SWP_NOACTIVATE   = 0x0010
SWP_NOZORDER     = 0x0004
SWP_SHOWWINDOW   = 0x0040
SWP_FRAMECHANGED = 0x0020
HWND_TOPMOST     = -1

# ShowWindow 命令
SW_HIDE = 0
SW_SHOW = 5

# UpdateLayeredWindow
ULW_ALPHA = 0x00000002

# SetLayeredWindowAttributes flags
LWA_COLORKEY = 0x00000001
LWA_ALPHA    = 0x00000002

# Color key for LWA_COLORKEY transparency (magenta — unlikely in real content)
KEY_COLOR = 0x00FF00FF  # BGR: R=255 G=0 B=255

# Raw Input 常量
RIDEV_INPUTSINK = 0x00000100
RID_INPUT   = 0x10000003
RIM_TYPEKEYBOARD = 1
RI_KEY_MAKE  = 0x0000  # key down
RI_KEY_BREAK = 0x0001  # key up
WM_INPUT = 0x00FF

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.wintypes.USHORT),
        ("usUsage",     ctypes.wintypes.USHORT),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("hwndTarget",  wintypes.HWND),
    ]

class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType",  ctypes.wintypes.DWORD),
        ("dwSize",  ctypes.wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam",  ctypes.wintypes.WPARAM),
    ]

class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode",        ctypes.wintypes.USHORT),
        ("Flags",           ctypes.wintypes.USHORT),
        ("Reserved",        ctypes.wintypes.USHORT),
        ("VKey",            ctypes.wintypes.USHORT),
        ("Message",         ctypes.wintypes.UINT),
        ("ExtraInformation", ctypes.wintypes.ULONG),
    ]

# 窗口消息
WM_TIMER        = 0x0113
WM_USER         = 0x0400
WM_KEY_UPDATE   = WM_USER + 1   # 按键更新消息
WM_THEME_CHANGE = WM_USER + 2   # 主题变化消息
WM_FADE_START   = WM_USER + 3   # 开始淡出
WM_FADE_STEP    = WM_USER + 4   # 淡出步进
WM_XN_ANIM      = WM_USER + 5   # xN 弹跳动画

# DIB 颜色表常量
DIB_RGB_COLORS = 0
BI_RGB = 0

# DWM 窗口合成 / Acrylic 模糊
WCA_ACCENT_POLICY = 19
ACCENT_ENABLE_BLURBEHIND = 3

class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_uint),
    ]

class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_uint),
        ("pvData", ctypes.c_void_p),
        ("cbData", ctypes.c_size_t),
    ]

SetWindowCompositionAttribute = ctypes.windll.user32.SetWindowCompositionAttribute
SetWindowCompositionAttribute.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWCOMPOSITIONATTRIBDATA)]
SetWindowCompositionAttribute.restype = ctypes.c_bool

# ─── 日志辅助 ────────────────────────────────────────────────────────────

def _engine_log(msg):
    """写入 debug.log（与 keyheatmap.py 共用）"""
    try:
        import os
        log_path = os.path.join(os.environ.get("APPDATA", ""), "KeyHeatmap", "debug.log")
        with open(log_path, "a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"[{datetime.now().strftime('%H:%M:%S.%f')[:12]}] [engine] {msg}\n")
    except Exception:
        pass


# ─── Win32 结构体定义 ────────────────────────────────────────────────────

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",          wintypes.DWORD),
        ("biWidth",         wintypes.LONG),
        ("biHeight",        wintypes.LONG),   # 正数 = 自底向上
        ("biPlanes",        wintypes.WORD),
        ("biBitCount",      wintypes.WORD),
        ("biCompression",   wintypes.DWORD),
        ("biSizeImage",     wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed",       wintypes.DWORD),
        ("biClrImportant",  wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
    ]

class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp",             wintypes.BYTE),
        ("BlendFlags",          wintypes.BYTE),
        ("SourceConstantAlpha", wintypes.BYTE),
        ("AlphaFormat",         wintypes.BYTE),
    ]

class POINT(ctypes.Structure):
    _fields_ = [
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
    ]

class SIZE(ctypes.Structure):
    _fields_ = [
        ("cx", wintypes.LONG),
        ("cy", wintypes.LONG),
    ]

class RECT(ctypes.Structure):
    _fields_ = [
        ("left",   wintypes.LONG),
        ("top",    wintypes.LONG),
        ("right",  wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]

class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc",         wintypes.HDC),
        ("fErase",      wintypes.BOOL),
        ("rcPaint",     RECT),
        ("fRestore",    wintypes.BOOL),
        ("fIncUpdate",  wintypes.BOOL),
        ("rgbReserved", wintypes.BYTE * 32),
    ]

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd",    wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam",  wintypes.WPARAM),
        ("lParam",  wintypes.LPARAM),
        ("time",    wintypes.DWORD),
        ("pt",      POINT),
    ]

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize",        wintypes.UINT),
        ("style",         wintypes.UINT),
        ("lpfnWndProc",   ctypes.c_void_p),
        ("cbClsExtra",    wintypes.INT),
        ("cbWndExtra",    wintypes.INT),
        ("hInstance",     wintypes.HINSTANCE),
        ("hIcon",         wintypes.HICON),
        ("hCursor",       wintypes.HICON),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName",  wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm",       wintypes.HICON),
    ]

# ─── GDI 对象类型别名 ────────────────────────────────────────────────────
HFONT      = wintypes.HANDLE
HBRUSH     = wintypes.HANDLE
HPEN       = wintypes.HANDLE
HBITMAP    = wintypes.HANDLE
HDC        = wintypes.HANDLE

# AlphaBlend — msimg32.dll (BLENDFUNCTION passed BY VALUE, not pointer)
MSIMG32 = ctypes.windll.msimg32
MSIMG32.AlphaBlend.argtypes = [
    HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    BLENDFUNCTION,
]
MSIMG32.AlphaBlend.restype = ctypes.c_bool

# ===========================================================================
#  HeatmapRenderer — 32 位 ARGB 位图绘制引擎
# ===========================================================================

class HeatmapRenderer:
    """
    在 32 位 BGRA DIB Section 上绘制热力图浮窗内容。
    所有像素值使用预乘 Alpha（UpdateLayeredWindow 要求 AC_SRC_ALPHA）。
    """

    # 绘图常量
    CARD_W  = 60
    CARD_H  = 62
    GAP     = 10
    PAD     = 8
    MAX_KEYS = 5

    @property
    def panel_w(self):
        return self.PAD * 2 + self.CARD_W * self.MAX_KEYS + self.GAP * (self.MAX_KEYS - 1)

    @property
    def panel_h(self):
        return self.PAD * 2 + self.CARD_H

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        # macOS Dock S-curve animation state
        self._bar_scales = {}     # key_name → current animated scale
        self._bar_positions = {}  # key_name → last slot index
        self._height_anim = {}  # key_name → (start_h, target_h, start_time)
        self._init_gdi_resources()

    def _init_gdi_resources(self):
        """创建 DIB Section + 内存 DC。"""
        user32 = ctypes.windll.user32
        gdi32  = ctypes.windll.gdi32

        # 获取屏幕 DC
        self.hdc_screen = user32.GetDC(None)

        # 创建内存 DC
        self.hdc_mem = gdi32.CreateCompatibleDC(self.hdc_screen)

        # 构建 BITMAPINFO：32bpp BGRA，自顶向下（biHeight 为负）
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth       = self.width
        bmi.bmiHeader.biHeight      = -self.height   # 负值 = 自顶向下，第一行是顶部
        bmi.bmiHeader.biPlanes      = 1
        bmi.bmiHeader.biBitCount    = 32
        bmi.bmiHeader.biCompression = BI_RGB

        # 创建 DIB Section，bits_ptr 指向像素缓冲区
        self._bits_ptr = ctypes.c_void_p()
        self.hbm = gdi32.CreateDIBSection(
            self.hdc_mem,
            ctypes.byref(bmi),
            DIB_RGB_COLORS,
            ctypes.byref(self._bits_ptr),
            None,
            0
        )
        # 构建可写入的 ctypes 数组视图
        pixel_count = self.width * self.height * 4
        bits_addr = self._bits_ptr.value if self._bits_ptr else 0
        self._pixels = (ctypes.c_uint8 * pixel_count).from_address(bits_addr) if bits_addr else None

        # 选择 DIB 到 DC
        self._old_bm = gdi32.SelectObject(self.hdc_mem, self.hbm)

    # ── 像素级操作 ─────────────────────────────────────────────────────

    def clear(self):
        """用全透明填充整个缓冲区。"""
        if self._pixels:
            ctypes.memset(self._pixels, 0, len(self._pixels))

    def _set_pixel(self, x: int, y: int, b: int, g: int, r: int, a: int):
        """写入单个预乘像素（坐标裁剪）。"""
        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            return
        if a == 0:
            return
        offset = (y * self.width + x) * 4
        # 预乘: output = input * alpha / 255
        if a == 255:
            self._pixels[offset]     = b
            self._pixels[offset + 1] = g
            self._pixels[offset + 2] = r
            self._pixels[offset + 3] = a
        else:
            self._pixels[offset]     = (b * a) // 255
            self._pixels[offset + 1] = (g * a) // 255
            self._pixels[offset + 2] = (r * a) // 255
            self._pixels[offset + 3] = a

    def _fill_rect(self, x: int, y: int, w: int, h: int,
                   r: int, g: int, b: int, a: int = 255):
        """填充矩形区域（纯色，带 alpha）。"""
        x1 = max(0, x)
        y1 = max(0, y)
        x2 = min(self.width, x + w)
        y2 = min(self.height, y + h)
        if a == 0 or x1 >= x2 or y1 >= y2:
            return

        if a == 255:
            # 快速路径：逐行 memset
            for py in range(y1, y2):
                offset = (py * self.width + x1) * 4
                for px in range(x1, x2):
                    self._pixels[offset]     = b
                    self._pixels[offset + 1] = g
                    self._pixels[offset + 2] = r
                    self._pixels[offset + 3] = 255
                    offset += 4
        else:
            pb = (b * a) // 255
            pg = (g * a) // 255
            pr = (r * a) // 255
            for py in range(y1, y2):
                offset = (py * self.width + x1) * 4
                for px in range(x1, x2):
                    self._pixels[offset]     = pb
                    self._pixels[offset + 1] = pg
                    self._pixels[offset + 2] = pr
                    self._pixels[offset + 3] = a
                    offset += 4

    def _draw_rounded_rect(self, x: int, y: int, w: int, h: int, radius: int,
                           fill_r: int, fill_g: int, fill_b: int, fill_a: int = 255,
                           outline_r: int = 0, outline_g: int = 0, outline_b: int = 0,
                           outline_a: int = 0):
        """
        绘制圆角矩形。支持填充 + 描边，带逐像素 alpha 和 1px 软边缘抗锯齿。
        """
        r = min(radius, w // 2, h // 2)
        if r < 1:
            self._fill_rect(x, y, w, h, fill_r, fill_g, fill_b, fill_a)
            return

        r_f = float(r)

        for py in range(y, y + h):
            for px in range(x, x + w):
                # 计算到四个角圆心的最小距离
                dist_sq = 0.0
                is_corner = False

                if px < x + r and py < y + r:
                    dx = px - (x + r)
                    dy = py - (y + r)
                    dist_sq = dx * dx + dy * dy
                    is_corner = True
                elif px >= x + w - r and py < y + r:
                    dx = px - (x + w - r - 1)
                    dy = py - (y + r)
                    dist_sq = dx * dx + dy * dy
                    is_corner = True
                elif px < x + r and py >= y + h - r:
                    dx = px - (x + r)
                    dy = py - (y + h - r - 1)
                    dist_sq = dx * dx + dy * dy
                    is_corner = True
                elif px >= x + w - r and py >= y + h - r:
                    dx = px - (x + w - r - 1)
                    dy = py - (y + h - r - 1)
                    dist_sq = dx * dx + dy * dy
                    is_corner = True

                if is_corner:
                    dist = dist_sq ** 0.5
                    aa_half = 1.0
                    r_inner = r_f - aa_half
                    r_outer = r_f + aa_half
                    if dist >= r_outer:
                        continue
                    elif dist <= r_inner:
                        alpha_mod = 1.0
                    else:
                        alpha_mod = (r_outer - dist) / (r_outer - r_inner)
                    pixel_a = int(fill_a * alpha_mod)
                    if pixel_a < 1:
                        continue
                    self._set_pixel(px, py, fill_b, fill_g, fill_r, pixel_a)
                else:
                    self._set_pixel(px, py, fill_b, fill_g, fill_r, fill_a)

    def _draw_horizontal_gradient(self, x: int, y: int, w: int, h: int, a: int,
                                  start_rgb, end_rgb):
        """水平渐变条（用于顶部装饰线）。"""
        sr, sg, sb = start_rgb
        er, eg, eb = end_rgb
        for col in range(w):
            t = col / max(w - 1, 1)
            r = int(sr + (er - sr) * t)
            g = int(sg + (eg - sg) * t)
            b = int(sb + (eb - sb) * t)
            # 一条竖线
            for row in range(h):
                self._set_pixel(x + col, y + row, b, g, r, a)

    def _draw_text_gdi(self, text: str, cx: int, cy: int,
                       font_name: str, font_size: int, bold: bool,
                       r: int, g: int, b: int, a: int = 255):
        """
        使用 GDI TextOutW 在内存 DC 上绘制文字。
        仅在文字实际绘制区域做 alpha 修正，避免干扰其他像素。
        """
        gdi32 = ctypes.windll.gdi32

        weight = 700 if bold else 400
        hfont = gdi32.CreateFontW(
            -font_size, 0, 0, 0, weight, 0, 0, 0,
            0, 0, 0, 2, 0, font_name
        )
        old_font = gdi32.SelectObject(self.hdc_mem, hfont)
        gdi32.SetTextColor(self.hdc_mem, (b << 16) | (g << 8) | r)
        gdi32.SetBkMode(self.hdc_mem, 1)  # TRANSPARENT

        text_buf = ctypes.create_unicode_buffer(text)
        gdi32.TextOutW(self.hdc_mem, cx, cy, text_buf, len(text))

        # 获取文字实际像素尺寸，只在这个矩形内修正 alpha
        sz = ctypes.wintypes.SIZE()
        gdi32.GetTextExtentPoint32W(self.hdc_mem, text_buf, len(text), ctypes.byref(sz))
        text_w, text_h = sz.cx, sz.cy

        gdi32.SelectObject(self.hdc_mem, old_font)
        gdi32.DeleteObject(hfont)

        if self._pixels and text_w > 0 and text_h > 0:
            x1 = max(0, cx)
            y1 = max(0, cy)
            x2 = min(self.width, cx + text_w + 2)
            y2 = min(self.height, cy + text_h + 2)
            for py in range(y1, y2):
                row_start = py * self.width
                for px in range(x1, x2):
                    offset = (row_start + px) * 4
                    if self._pixels[offset + 3] == 0:
                        val = self._pixels[offset] | self._pixels[offset + 1] | self._pixels[offset + 2]
                        if val != 0:
                            if a == 255:
                                self._pixels[offset + 3] = 255
                            else:
                                self._pixels[offset + 3] = a
                                self._pixels[offset]     = (self._pixels[offset] * a) // 255
                                self._pixels[offset + 1] = (self._pixels[offset + 1] * a) // 255
                                self._pixels[offset + 2] = (self._pixels[offset + 2] * a) // 255

    # ── 完整帧绘制 ─────────────────────────────────────────────────────

    # macOS Dock S 曲线弹性动画 ————————————————————————————————————————
    def _get_dock_scale(self, index: int, total: int, curve_strength: float = 0.12) -> float:
        """macOS Dock 式 S 曲线：中间键最大，向两侧衰减。"""
        if total <= 1:
            return 1.0
        center = (total - 1) / 2.0
        max_dist = max(abs(i - center) for i in range(total))
        if max_dist < 0.001:
            return 1.0
        dist = abs(index - center)
        return 1.0 - curve_strength * (dist / max_dist) ** 2

    def draw_frame(self, display_keys: list, display_counts: dict,
                   color_scheme: str, xN_data: list = None,
                   skip_panel_bg: bool = False):
        """
        绘制完整一帧热力图浮窗——macOS Dock 式 S 曲线弹性热力条。

        display_keys: 最近按键名列表（最多 5 个）
        display_counts: key → 连击计数（2 秒窗口）
        color_scheme: "dark" | "light"
        xN_data: xN 弹跳动画数据 [(tag_text, tag_w, tag_h, tag_x, tag_y, scale), ...]
        skip_panel_bg: 跳过面板背景（启用 External Blur 时由调用方负责背景合成）
        """
        self.clear()

        is_light = (color_scheme == "light")

        # ══════════════════════════════════════════════════════════════
        # 主题配色
        # ══════════════════════════════════════════════════════════════
        if is_light:
            bg_panel     = (0xf0, 0xf0, 0xf0)   # 面板背景
            line_bottom  = (0xd0, 0xd0, 0xd0)   # 底部分割线
            # 热力条渐变端点：底部冷色 → 顶部暖色
            bar_cold     = (180, 200, 210)       # 底部（低频）：浅蓝灰
            bar_hot      = (250, 80, 60)         # 顶部（高频）：暖红
            # 顶部装饰线
            top_start    = (180, 200, 210)
            top_end      = (210, 180, 240)
        else:
            bg_panel     = (0x1a, 0x1a, 0x1a)
            line_bottom  = (0x2a, 0x2a, 0x2a)
            bar_cold     = (40, 120, 180)        # 底部：深蓝
            bar_hot      = (255, 80, 40)         # 顶部：暖红
            top_start    = (40, 200, 120)
            top_end      = (50, 160, 180)

        # ══════════════════════════════════════════════════════════════
        # 面板：毛玻璃圆角背景（模拟 Acrylic）
        # ══════════════════════════════════════════════════════════════
        PANEL_RADIUS = 16

        if not skip_panel_bg:
            # Glass panel: single translucent layer
            self._draw_rounded_rect(0, 0, self.width, self.height, PANEL_RADIUS,
                                    0x12, 0x12, 0x1a, 130)
            # Top highlight
            self._draw_horizontal_gradient(PANEL_RADIUS, 0,
                                           self.width - PANEL_RADIUS * 2, 1, 110,
                                           (210, 220, 240), (180, 190, 220))
        else:
            # External blur mode: 仅画圆角蒙版边界的 alpha 过渡（让 _unpremultiply_colors 接管背景）
            # 用极低 alpha 画出圆角形状，给 _unpremultiply_colors 的 alpha≤0 检测留空间
            pass

        # ══════════════════════════════════════════════════════════════
        # 底部基准线生长式热力条
        # ══════════════════════════════════════════════════════════════
        BAR_MARGIN = 2
        BAR_H = 40              # 条最大高度
        BAR_BOTTOM = self.PAD + self.CARD_H - 8  # 条底部基准线（为文字留8px底边距）
        BAR_RADIUS = 5          # 顶部圆角

        bar_w = self.CARD_W - BAR_MARGIN * 2  # 56px

        keys = list(display_keys)
        visible = keys[:self.MAX_KEYS]
        n = len(visible)

        # ── Dock 弹性动画 ──
        current_keys = {k for k in visible}
        target_scales = {}
        for i, key in enumerate(visible):
            target_scales[key] = self._get_dock_scale(i, n)

        for k, cur in list(self._bar_scales.items()):
            if k in target_scales:
                target = target_scales[k]
                self._bar_scales[k] = cur + (target - cur) * 0.35
            else:
                new_scale = cur * 0.6
                if new_scale < 0.015:
                    del self._bar_scales[k]
                else:
                    self._bar_scales[k] = new_scale

        for k, target in target_scales.items():
            if k not in self._bar_scales:
                self._bar_scales[k] = target * 0.30

        stale = [k for k in self._bar_scales if k not in current_keys and self._bar_scales[k] < 0.015]
        for k in stale:
            del self._bar_scales[k]
        # 清理不可见键的高度动画状态
        for k in list(self._height_anim):
            if k not in current_keys:
                del self._height_anim[k]

        # ── 绘制每个键：底部生长式热力条 ──
        for i, key in enumerate(visible):
            slot_x = self.PAD + i * (self.CARD_W + self.GAP)
            bar_x = slot_x + BAR_MARGIN
            count = display_counts.get(key, 1)
            scale = self._bar_scales.get(key, 1.0)

            # 条高度：按次数从底部生长，count=1→1/3, count=2→2/3, count≥3→全高
            target_h = min(BAR_H, int(BAR_H * count / 3.0))
            # 基于时间的缓出动画（target 变化时平滑过渡）
            now = time.time()
            if key in self._height_anim:
                s_h, prev_target, s_t = self._height_anim[key]
                if prev_target != target_h:
                    # 目标变了：从当前动画位置继续
                    elapsed = now - s_t
                    prog = min(1.0, elapsed / 0.25)  # 250ms 动画
                    eased = 1.0 - (1.0 - prog) ** 3
                    s_h = s_h + (prev_target - s_h) * eased
                    s_t = now
            else:
                s_h = target_h * 0.30
                s_t = now
            self._height_anim[key] = (s_h, target_h, s_t)

            elapsed = now - s_t
            prog = min(1.0, elapsed / 0.25)
            eased = 1.0 - (1.0 - prog) ** 3
            draw_h = max(2, int(s_h + (target_h - s_h) * eased))
            bar_draw_y = BAR_BOTTOM - draw_h  # 底部基准线对齐

            # ── 颜色：0-3次蓝渐变，3次后逐渐变红 ──
            if count <= 3:
                # 蓝→暖渐变
                heat_lerp = min(1.0, count / 3.0)
                red_boost = 0.0
            else:
                heat_lerp = 1.0
                red_boost = min(1.0, (count - 3) / 7.0)  # 10次全红

            # ── 逐行绘制条（底部冷蓝、顶部暖色，红化后顶部偏红） ──
            for row in range(draw_h):
                py = bar_draw_y + row
                t = row / max(draw_h - 1, 1)  # 0=底部, 1=顶部
                heat_t = t * heat_lerp

                cr, cg, cb = bar_cold
                hr, hg, hb = bar_hot
                r = cr + int((hr - cr) * heat_t)
                g = cg + int((hg - cg) * heat_t)
                b = cb + int((hb - cb) * heat_t)

                # 红色增强：顶部区域向纯红过渡
                if red_boost > 0.001 and t > 0.5:
                    local_red = red_boost * (t - 0.5) * 2.0  # 顶部更强
                    r2, g2, b2 = 255, 30, 20
                    r = int(r + (r2 - r) * local_red)
                    g = int(g + (g2 - g) * local_red)
                    b = int(b + (b2 - b) * local_red)

                r = max(0, min(255, r))
                g = max(0, min(255, g))
                b = max(0, min(255, b))

                for col in range(bar_w):
                    px = bar_x + col

                    bar_alpha = 150
                    alpha = bar_alpha
                    # 全高条的顶部圆角
                    if draw_h >= BAR_H - 2 and row < BAR_RADIUS:
                        abs_row = row
                        if col < BAR_RADIUS:
                            dx = col - BAR_RADIUS
                            dy = abs_row - BAR_RADIUS
                            dist2 = dx * dx + dy * dy
                            if dist2 >= BAR_RADIUS * BAR_RADIUS:
                                continue
                            if dist2 > (BAR_RADIUS - 1) * (BAR_RADIUS - 1):
                                alpha = int(bar_alpha * max(0.0, BAR_RADIUS - dist2 ** 0.5))
                        elif col >= bar_w - BAR_RADIUS:
                            dx = col - (bar_w - BAR_RADIUS - 1)
                            dy = abs_row - BAR_RADIUS
                            dist2 = dx * dx + dy * dy
                            if dist2 >= BAR_RADIUS * BAR_RADIUS:
                                continue
                            if dist2 > (BAR_RADIUS - 1) * (BAR_RADIUS - 1):
                                aa = int(bar_alpha * max(0.0, BAR_RADIUS - dist2 ** 0.5))
                                alpha = min(alpha, aa)

                    self._set_pixel(px, py, b, g, r, alpha)

            # ── 高光线：全高条顶部 ──
            if draw_h >= 44 and scale > 0.8:
                hl_y = bar_draw_y
                for hl_col in range(2, bar_w - 2):
                    hl_px = bar_x + hl_col
                    if hl_col < BAR_RADIUS:
                        dx = hl_col - BAR_RADIUS
                        if dx * dx + (-BAR_RADIUS) * (-BAR_RADIUS) >= BAR_RADIUS * BAR_RADIUS:
                            continue
                    if hl_col >= bar_w - BAR_RADIUS:
                        dx = hl_col - (bar_w - BAR_RADIUS - 1)
                        if dx * dx + (-BAR_RADIUS) * (-BAR_RADIUS) >= BAR_RADIUS * BAR_RADIUS:
                            continue
                    offset = (hl_y * self.width + hl_px) * 4
                    if self._pixels[offset + 3] >= 150:
                        nr = min(255, self._pixels[offset + 2] + 55)
                        ng = min(255, self._pixels[offset + 1] + 55)
                        nb = min(255, self._pixels[offset] + 55)
                        self._set_pixel(hl_px, hl_y, nb, ng, nr, 255)

            # ── 键名：条下方（BAR_BOTTOM 与面板底之间的空白区） ──
            label_font = 12
            tw_est = int(label_font * 0.72 * len(key))
            text_x = bar_x + max(0, (bar_w - tw_est) // 2)
            text_y = BAR_BOTTOM + 1  # 紧贴条底部
            self._draw_text_gdi(key, text_x, text_y,
                                "Segoe UI", label_font, True, 220, 220, 220, 255)

        # ══════════════════════════════════════════════════════════════
        # xN 弹跳动画（固定槽位定位，无偏移）
        # ══════════════════════════════════════════════════════════════
        if xN_data:
            for tag_text, tag_w, tag_h, tag_x, tag_y, scale in xN_data:
                try:
                    cnt = int(tag_text[1:])
                except ValueError:
                    cnt = 2
                sw = tag_w * scale
                sh = tag_h * scale
                sx = int(tag_x - sw / 2)
                sy = int(tag_y + (tag_h - sh) / 2)
                rad = int(6 * max(scale, 0.5))

                if cnt >= 10:
                    xN_fill = (0xff, 0x17, 0x44)
                elif cnt >= 5:
                    xN_fill = (0xff, 0x6d, 0x00)
                else:
                    xN_fill = (0xff, 0x3b, 0x30)

                self._draw_rounded_rect(sx, sy, int(sw), int(sh), rad,
                                        xN_fill[1], xN_fill[2], xN_fill[0], 255)

                font_sz = max(int(10 * scale), 6)
                tw = int(font_sz * 0.6 * len(tag_text))
                self._draw_text_gdi(tag_text,
                                    sx + (int(sw) - tw) // 2,
                                    sy + (int(sh) - font_sz) // 2 - 1,
                                    "Segoe UI", font_sz, True,
                                    255, 255, 255, 255)

    def draw_cards_frame(self, display_keys: list, display_counts: dict,
                         color_scheme: str, xN_data: list = None,
                         skip_panel_bg: bool = False):
        """卡片式布局：5 张卡片，边框+阴影+热力渐变，右上角 xN 标签。"""
        self.clear()

        is_light = (color_scheme == "light")

        PANEL_RADIUS = 14
        CARD_RADIUS = 5
        CARD_COUNT = 5
        PAD = 8
        GAP = 6
        SHADOW_DX, SHADOW_DY = 1, 2

        if is_light:
            panel_fill  = (0xf0, 0xf1, 0xf4)
            card_fill   = (0xff, 0xff, 0xff)
            card_border = (0xcd, 0xcf, 0xd6)
            shadow_clr  = (0x90, 0x92, 0x9a)
            label_color = (0x60, 0x62, 0x70)
            count_cold  = (0x44, 0x46, 0x54)
        else:
            panel_fill  = (0x14, 0x14, 0x1e)
            card_fill   = (0x28, 0x28, 0x34)
            card_border = (0x3c, 0x3e, 0x4c)
            shadow_clr  = (0x06, 0x06, 0x0c)
            label_color = (0x88, 0x8a, 0x96)
            count_cold  = (0xaa, 0xac, 0xbc)

        # ── 面板背景 ──
        if not skip_panel_bg:
            self._draw_rounded_rect(0, 0, self.width, self.height, PANEL_RADIUS,
                                    panel_fill[1], panel_fill[2], panel_fill[0], 200)

            # ── 装饰顶线 ──
            for col in range(PANEL_RADIUS, self.width - PANEL_RADIUS):
                t = (col - PANEL_RADIUS) / max(self.width - PANEL_RADIUS * 2 - 1, 1)
                self._set_pixel(col, 0,
                                int(180 + 40 * t),
                                int(120 + 80 * t),
                                int(60 + 140 * t), 120)

        # ── 卡片尺寸 ──
        keys = list(display_keys)[:CARD_COUNT]
        n = len(keys)
        if n == 0:
            return

        card_w = (self.width - PAD * 2 - GAP * (CARD_COUNT - 1)) // CARD_COUNT
        card_h = self.height - PAD * 2

        # Build card position map for xN tag placement
        card_positions = {}
        for i, key in enumerate(keys):
            cx = PAD + i * (card_w + GAP)
            card_positions[key] = (cx, card_w)

        # ── 绘制卡片 ──
        for i, key in enumerate(keys):
            cx = PAD + i * (card_w + GAP)
            cy = PAD
            count = display_counts.get(key, 0)

            self._draw_rounded_rect(cx + SHADOW_DX, cy + SHADOW_DY,
                                    card_w, card_h, CARD_RADIUS,
                                    shadow_clr[1], shadow_clr[2], shadow_clr[0], 80)
            self._draw_rounded_rect(cx - 1, cy - 1, card_w + 2, card_h + 2, CARD_RADIUS + 1,
                                    card_border[1], card_border[2], card_border[0], 100)
            self._draw_rounded_rect(cx, cy, card_w, card_h, CARD_RADIUS,
                                    card_fill[1], card_fill[2], card_fill[0], 220)

            # 热力渐变
            if count >= 10:
                heat_r, heat_g, heat_b, heat_peak = 255, 40, 20, 60
            elif count >= 5:
                heat_r, heat_g, heat_b, heat_peak = 255, 120, 20, 48
            elif count >= 3:
                heat_r, heat_g, heat_b, heat_peak = 255, 170, 50, 36
            elif count >= 1:
                heat_r, heat_g, heat_b, heat_peak = 80, 140, 200, 24
            else:
                heat_peak = 0

            if heat_peak > 0:
                mid_y = card_h // 4
                for row in range(mid_y, card_h):
                    py = cy + row
                    a = int(heat_peak * (row - mid_y) / max(card_h - mid_y - 1, 1))
                    if a > 0:
                        for px in range(cx + CARD_RADIUS, cx + card_w - CARD_RADIUS):
                            self._set_pixel(px, py, heat_b, heat_g, heat_r, a)

            # 按键名
            label_font = 10
            tw_est = int(label_font * 0.60 * len(key))
            tx = cx + max(3, (card_w - tw_est) // 2)
            ty = cy + 6
            self._draw_text_gdi(key, tx, ty,
                                "Segoe UI", label_font, False,
                                label_color[0], label_color[1], label_color[2], 190)

            # 计数值
            cnt_str = str(count)
            cnt_font = 20 if count < 10 else (17 if count < 100 else 14)
            cnt_tw = int(cnt_font * 0.58 * len(cnt_str))
            cnx = cx + (card_w - cnt_tw) // 2
            cny = cy + card_h // 2 - cnt_font // 2 + 2

            if count > 0:
                if count >= 10:
                    cr, cg, cb = 255, 48, 28
                elif count >= 5:
                    cr, cg, cb = 255, 132, 20
                elif count >= 3:
                    cr, cg, cb = 255, 178, 48
                else:
                    cr, cg, cb = count_cold[0], count_cold[1], count_cold[2]
                self._draw_text_gdi(cnt_str, cnx, cny,
                                    "Segoe UI", cnt_font, True, cr, cg, cb, 255)

        # ── xN 弹跳标签：固定在对应卡片右上角 ──
        if xN_data:
            for tag_text, tag_w, tag_h, tag_x, tag_y, scale in xN_data:
                # Ignore old tag_x/tag_y from bar mode; use card positions
                try:
                    cnt = int(tag_text[1:])
                except ValueError:
                    cnt = 2
                # Find which key this tag belongs to by matching sorted index
                # xN_data order matches display_keys order; rebuild card positions sorted
                sorted_keys = sorted(card_positions.keys(),
                                     key=lambda k: card_positions[k][0])
                tag_idx = None
                for idx, k in enumerate(sorted_keys):
                    if display_counts.get(k, 0) == cnt:
                        tag_idx = idx
                        break
                if tag_idx is None:
                    tag_idx = 0

                key = sorted_keys[min(tag_idx, len(sorted_keys) - 1)]
                cx, cw = card_positions[key]

                tag_sx = cx + cw - int(tag_w * scale) + 2
                tag_sy = PAD - 2
                rad = int(5 * max(scale, 0.6))
                if cnt >= 10:
                    xf = (0xff, 0x17, 0x44)
                elif cnt >= 5:
                    xf = (0xff, 0x6d, 0x00)
                else:
                    xf = (0xff, 0x3b, 0x30)
                self._draw_rounded_rect(tag_sx, tag_sy, int(tag_w * scale), int(tag_h * scale),
                                        rad, xf[1], xf[2], xf[0], 255)
                fs = max(int(10 * scale), 6)
                tw = int(fs * 0.6 * len(tag_text))
                self._draw_text_gdi(tag_text,
                                    tag_sx + (int(tag_w * scale) - tw) // 2,
                                    tag_sy + (int(tag_h * scale) - fs) // 2 - 1,
                                    "Segoe UI", fs, True, 255, 255, 255, 255)

    def height_animation_active(self) -> bool:
        """检查是否有高度渐变动画仍在进行中。"""
        now = time.time()
        for key, (s_h, target_h, s_t) in list(self._height_anim.items()):
            if now - s_t < 0.25:
                return True
        return False

    def resize(self, width: int, height: int):
        """重新创建 DIB Section。"""
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32

        # 清理旧资源
        if hasattr(self, '_old_bm') and self._old_bm:
            gdi32.SelectObject(self.hdc_mem, self._old_bm)
        if hasattr(self, 'hbm') and self.hbm:
            gdi32.DeleteObject(self.hbm)

        self.width = width
        self.height = height
        self._init_gdi_resources()

    def cleanup(self):
        """释放所有 GDI 资源。"""
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32

        if hasattr(self, '_old_bm') and self._old_bm:
            gdi32.SelectObject(self.hdc_mem, self._old_bm)
            self._old_bm = None
        if hasattr(self, 'hbm') and self.hbm:
            gdi32.DeleteObject(self.hbm)
            self.hbm = None
        if hasattr(self, 'hdc_mem') and self.hdc_mem:
            gdi32.DeleteDC(self.hdc_mem)
            self.hdc_mem = None
        if hasattr(self, 'hdc_screen') and self.hdc_screen:
            user32.ReleaseDC(None, self.hdc_screen)
            self.hdc_screen = None


# ===========================================================================
#  OverlayEngine — 窗口创建 & 消息循环
# ===========================================================================

# 全局映射：hwnd → OverlayEngine 实例（供 WndProc 回调查找）
_wnd_map = {}
_wnd_map_lock = threading.Lock()

# WndProc 函数类型
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)

@WNDPROC
def _wnd_proc(hwnd, msg, wparam, lparam):
    """全局窗口过程，分发消息到对应的 OverlayEngine 实例。"""
    with _wnd_map_lock:
        engine = _wnd_map.get(hwnd)
    if engine:
        try:
            return engine._handle_message(msg, wparam, lparam)
        except Exception as e:
            _engine_log(f"[wndproc] exception in msg 0x{msg:04X}: {e}\n{traceback.format_exc()}")
            pass
    # 明确定义 DefWindowProcW 的 argtypes 避免 64 位 LPARAM 溢出
    dwp = ctypes.windll.user32.DefWindowProcW
    dwp.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    dwp.restype = ctypes.c_longlong  # LRESULT on x64
    return dwp(hwnd, msg, wparam, lparam)


class OverlayEngine:
    """
    Windows 原生 layered window 浮窗引擎。
    使用 CreateWindowExW + UpdateLayeredWindow 实现 GPU 兼容渲染。
    对外接口与旧版 KeyOverlay 兼容。
    """

    CARD_W  = 60
    CARD_H  = 58
    GAP     = 10
    PAD     = 10
    MAX_KEYS = 5

    def __init__(self):
        self.hwnd = None
        self._hwnd_int = 0
        self.renderer: HeatmapRenderer = None
        self._ready = threading.Event()
        self._thread = None
        self._msg_thread_id = 0

        # 显示状态
        self.display_keys = deque(maxlen=self.MAX_KEYS)
        self.display_counts = {}       # key → count
        self.key_timestamps = deque()  # (key_name, timestamp)

        # 淡出状态
        self.source_alpha = 0          # 0=全透明, 255=不透明
        self.fade_state = "idle"       # "idle" | "waiting" | "fading"
        self.fade_wait_end = 0.0       # 等待结束时间戳

        # xN 弹跳动画状态
        self._xN_anim_data = None      # 当前动画队列
        self._xN_anim_scales = [0.60, 0.85, 1.15, 1.30, 1.00]
        self._xN_anim_delays = [0, 35, 35, 35, 45]
        self._xN_anim_idx = 0
        self._xN_anim_timer = None
        self._xN_last_counts = {}       # 上次动画时的 count，避免重复弹跳

        # 主题
        self.color_scheme = "dark"
        self._theme_stop = threading.Event()

        # 渲染模式: "heatmap" | "cards"
        self.overlay_mode = "heatmap"

        # 全屏检测缓存
        self._fullscreen_cache = 0.0
        self._fullscreen_cached_val = False

        # 定时回调表（timer_id → callback）
        self._timers = {}
        self._next_timer_id = 1000
        self._timers_lock = threading.Lock()

        # 位置信息
        self.window_x = 0
        self.window_y = 0

        # Raw Input 键盘回调
        self._key_callback = None

        # 半透明 + 毛玻璃设置
        self.window_opacity = 1.0       # 0.3 - 1.0，窗口整体透明度
        self.blur_enabled = False       # 毛玻璃模糊效果
        self._glass_cache = None        # (pixels_bytes, width, height, timestamp)
        self._glass_cache_interval = 0.05  # 屏幕捕获缓存间隔（秒），50ms 保证拖拽时背景实时跟随

    def set_key_callback(self, callback):
        """注册 Raw Input 键盘回调。callback(name: str) 在键按下时调用。"""
        self._key_callback = callback

    # ── 属性 ──────────────────────────────────────────────────────────

    @property
    def win_w(self):
        return self.PAD * 2 + self.CARD_W * self.MAX_KEYS + self.GAP * (self.MAX_KEYS - 1)

    @property
    def win_h(self):
        return self.PAD * 2 + self.CARD_H

    # ── 生命周期 ──────────────────────────────────────────────────────

    def start(self):
        """启动浮窗（后台线程创建窗口并运行消息循环）。"""
        self._thread = threading.Thread(
            target=self._run_message_loop, daemon=True, name="overlay-engine"
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self):
        """停止浮窗。"""
        self._theme_stop.set()
        if self.hwnd:
            user32 = ctypes.windll.user32
            user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE

    def restart(self):
        """重启浮窗：停止旧窗口线程并重新启动。"""
        log("[engine] restarting overlay...")
        self.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self.hwnd = None
        self._thread = None
        self._ready.clear()
        self._theme_stop = threading.Event()
        self.start()
        log("[engine] overlay restarted")

    # ── 公开接口（与 KeyOverlay 兼容）────────────────────────────────

    def on_key(self, key_name: str):
        """外部调用：收到按键事件。向窗口线程投递更新消息。"""
        # ── DComp external overlay path ──
        if hasattr(self, '_external_hwnd') and self._external_hwnd:
            now = time.time()
            while self.key_timestamps and now - self.key_timestamps[0][1] > 2.0:
                self.key_timestamps.popleft()
            self.key_timestamps.append((key_name, now))
            count = sum(1 for k, t in self.key_timestamps if k == key_name)

            # Send via WM_COPYDATA with full key name struct
            import struct
            user32 = ctypes.windll.user32
            # Build KHO_KEYDATA: WCHAR name[32] + int count
            name_utf16 = key_name.encode('utf-16-le')[:62]  # 31 chars max
            name_utf16 += b'\x00' * (64 - len(name_utf16))  # pad to 64 bytes (32 WCHARs)
            data = name_utf16 + struct.pack('<i', count)
            COPYDATASTRUCT = ctypes.c_buffer(data)
            cds = ctypes.c_buffer(ctypes.sizeof(ctypes.c_ulong) * 3)
            # COPYDATASTRUCT: dwData, cbData, lpData
            ctypes.memmove(cds, ctypes.c_ulong(0x4B484F), ctypes.sizeof(ctypes.c_ulong))
            ctypes.memmove(ctypes.cast(ctypes.addressof(cds) + ctypes.sizeof(ctypes.c_ulong), ctypes.c_void_p),
                           ctypes.c_ulong(len(data)), ctypes.sizeof(ctypes.c_ulong))
            ctypes.memmove(ctypes.cast(ctypes.addressof(cds) + ctypes.sizeof(ctypes.c_ulong) * 2, ctypes.c_void_p),
                           ctypes.addressof(COPYDATASTRUCT), ctypes.sizeof(ctypes.c_void_p))
            user32.SendMessageW(self._external_hwnd, 0x004A, 0, ctypes.addressof(cds))
            return

        if not self.hwnd:
            log("[overlay] on_key: no hwnd, dropping")
            return
        # 全屏独占检测
        if self._is_fullscreen_exclusive():
            log("[overlay] on_key: fullscreen exclusive, dropping")
            return
        # 投递消息到窗口线程处理
        # 使用 WM_KEY_UPDATE，lParam 无法传字符串，走共享队列
        # 简化方案：直接在调用线程更新时间戳，只投递渲染请求
        now = time.time()
        while self.key_timestamps and now - self.key_timestamps[0][1] > 2.0:
            self.key_timestamps.popleft()
        self.key_timestamps.append((key_name, now))

        # 统计当前键在 2 秒内的次数
        count = sum(1 for k, t in self.key_timestamps if k == key_name)

        # 更新 display_keys（线程安全：只在窗口线程渲染，这里只是排队）
        # 但 display_keys 在渲染线程读取，这里写入需要锁
        # 简化：通过 PostMessage 传递（用 WM_KEY_UPDATE + lParam 特性打包）
        result = ctypes.windll.user32.PostMessageW(self.hwnd, WM_KEY_UPDATE,
                                                    ord(key_name[0]) if key_name else 0,
                                                    count)
        if not result:
            is_valid = ctypes.windll.user32.IsWindow(self.hwnd)
            log(f"[overlay] on_key: PostMessageW FAILED for '{key_name}', "
                f"GetLastError={ctypes.windll.kernel32.GetLastError()}, IsWindow={is_valid}")

    def schedule(self, delay_ms: int, callback):
        """
        在消息泵线程上调度一次性回调（替代 tkinter root.after）。
        用于 KeyListener 的轮询模式。
        """
        if not self.hwnd:
            return
        with self._timers_lock:
            tid = self._next_timer_id
            self._next_timer_id += 1
            self._timers[tid] = callback
        user32 = ctypes.windll.user32
        user32.SetTimer(self.hwnd, tid, delay_ms, None)

    def after(self, delay_ms: int, callback):
        """schedule 的别名（兼容旧代码）。"""
        self.schedule(delay_ms, callback)

    def update_idletasks(self):
        """兼容旧代码（空操作）。"""
        pass

    def set_window_opacity(self, opacity: float):
        """设置窗口整体透明度。0.3（最透）到 1.0（完全不透明）。"""
        self.window_opacity = max(0.3, min(1.0, opacity))
        self._glass_cache = None  # 毛玻璃缓存失效
        _engine_log(f"opacity set to {self.window_opacity:.2f}")

    def set_blur(self, enabled: bool):
        """启用/禁用毛玻璃模糊效果。"""
        self.blur_enabled = enabled
        self._glass_cache = None
        _engine_log(f"blur {'enabled' if enabled else 'disabled'}")

    def set_mode(self, mode: str):
        """切换渲染模式: 'heatmap' | 'cards'"""
        self.overlay_mode = mode

    # ── 消息循环 ──────────────────────────────────────────────────────

    def _run_message_loop(self):
        """在独立线程中创建窗口并运行 Win32 消息泵。"""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        self._msg_thread_id = kernel32.GetCurrentThreadId()

        # 注册窗口类
        class_name = "KeyHeatmapOverlayClassV3"  # CS_OWNDC variant
        wc = WNDCLASSEXW()
        wc.cbSize        = ctypes.sizeof(WNDCLASSEXW)
        wc.style         = 0x0020  # CS_OWNDC: own DC for layered window GDI paint
        wc.lpfnWndProc   = ctypes.cast(_wnd_proc, ctypes.c_void_p)
        wc.cbClsExtra    = 0
        wc.cbWndExtra    = 0
        wc.hInstance     = kernel32.GetModuleHandleW(None)
        wc.hIcon         = None
        wc.hCursor       = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        wc.hbrBackground = None
        wc.lpszMenuName  = None
        wc.lpszClassName = class_name
        wc.hIconSm       = None

        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            err = kernel32.GetLastError()
            if err == 1410:  # ERROR_CLASS_ALREADY_EXISTS — expected on restart
                pass
            else:
                _engine_log(f"RegisterClassExW failed: {err}")
                return

        # 读取主题
        self.color_scheme = self._read_theme()
        self._start_theme_watcher()

        # 计算位置
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        self.window_x = (sw - self.win_w) // 2
        self.window_y = sh - self.win_h - 55  # 远离任务栏

        # 创建窗口
        # P5: WS_EX_LAYERED + LWA_ALPHA=255 (constant-opaque layered window).
        # Unlike UpdateLayeredWindow (per-pixel-alpha, lower Z-plane),
        # LWA_ALPHA uses a different DWM path that composites the window's
        # GDI backing bitmap with a constant alpha value.  At alpha=255
        # this is fully opaque and should respect normal Z-order against
        # DirectComposition windows like Marvis.
        exstyle = (WS_EX_LAYERED | WS_EX_TOOLWINDOW |
                   WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        style = WS_POPUP

        hwnd = user32.CreateWindowExW(
            exstyle,
            class_name,
            "KeyHeatmap Overlay",
            style,
            self.window_x, self.window_y,
            self.win_w, self.win_h,
            None, None,
            kernel32.GetModuleHandleW(None),
            None
        )

        if not hwnd:
            _engine_log(f"CreateWindowExW failed: {kernel32.GetLastError()}")
            return

        self.hwnd = hwnd
        self._hwnd_int = hwnd or 0

        # 注册到全局映射
        with _wnd_map_lock:
            _wnd_map[hwnd] = self

        # 验证扩展样式
        try:
            actual_exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _engine_log(
                f"overlay HWND=0x{self._hwnd_int:X} "
                f"GWL_EXSTYLE=0x{actual_exstyle:08X} "
                f"TOOLWINDOW={bool(actual_exstyle & WS_EX_TOOLWINDOW)} "
                f"LAYERED={bool(actual_exstyle & WS_EX_LAYERED)} "
                f"TRANSPARENT={bool(actual_exstyle & WS_EX_TRANSPARENT)} "
                f"NOACTIVATE={bool(actual_exstyle & WS_EX_NOACTIVATE)} "
                f"TOPMOST={bool(actual_exstyle & WS_EX_TOPMOST)}"
            )
        except Exception as e:
            _engine_log(f"exstyle verify failed: {e}")

        # 创建渲染器
        self.renderer = HeatmapRenderer(self.win_w, self.win_h)

        # Set LWA_ALPHA only (NO LWA_COLORKEY)
        # LWA_COLORKEY places the window on a lower DWM plane that
        # DirectComposition windows (Marvis, Electron) can still occlude.
        # Pure LWA_ALPHA uses the same DWM path as our earlier test window.
        user32.SetLayeredWindowAttributes(
            hwnd,
            0,              # colorkey disabled
            255,            # constant alpha (255 = fully opaque)
            LWA_ALPHA       # LWA_ALPHA only — no colorkey
        )

        # Show window at full opacity on startup, then fade out.
        # Hidden windows (SWP_HIDEWINDOW + LWA_ALPHA=0) can be spuriously
        # destroyed by Windows, leaving a dead HWND.  Always-visible + fade
        # avoids this.
        self.source_alpha = 255
        self._capture_glass_background()
        self._render()
        self._unpremultiply_colors()
        self._update_constant_alpha()
        
        # Apply rounded corner clipping region
        self._apply_rounded_corners()
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        
        # Immediate fade-out after 2s idle — keeps window alive but invisible
        user32.PostMessageW(hwnd, WM_FADE_START, 0, 0)

        # [PATCH P0] Acrylic blur disabled: ACCENT_ENABLE_BLURBEHIND causes DWM to
        # discard the entire layered visual when the window behind is a DXGI flip-model
        # surface (hardware-accelerated Electron/Chromium).  The panel already draws a
        # semi-transparent rounded rect via _draw_rounded_rect, which provides a good
        # enough glass-like background without involving DWM blur.
        #
        # # 尝试启用 DWM 模糊背后（失败则回退到模拟玻璃）
        # try:
        #     accent = ACCENTPOLICY()
        #     accent.AccentState = ACCENT_ENABLE_BLURBEHIND
        #     accent.AccentFlags = 0
        #     accent.GradientColor = 0
        #     accent.AnimationId = 0
        #     data = WINDOWCOMPOSITIONATTRIBDATA()
        #     data.Attribute = WCA_ACCENT_POLICY
        #     data.pvData = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
        #     data.cbData = ctypes.sizeof(accent)
        #     ok = SetWindowCompositionAttribute(hwnd, ctypes.pointer(data))
        #     _engine_log(f"acrylic blur: {'enabled' if ok else 'failed'}")
        # except Exception as e:
        #     _engine_log(f"acrylic blur: not supported ({e})")
        _engine_log("acrylic blur: disabled (P0 patch for HWND-accelerated windows)")

        # ── 注册 Raw Input 键盘（绕过 DXGI flip-model 限制）──
        try:
            rid = RAWINPUTDEVICE()
            rid.usUsagePage = 0x01  # Generic Desktop Controls
            rid.usUsage = 0x06      # Keyboard
            rid.dwFlags = RIDEV_INPUTSINK
            rid.hwndTarget = hwnd
            ok = ctypes.windll.user32.RegisterRawInputDevices(
                ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE))
            _engine_log(f"raw input registration: {'OK' if ok else 'FAILED (err=' + str(kernel32.GetLastError()) + ')'}")
        except Exception as e:
            _engine_log(f"raw input registration exception: {e}")

        _engine_log(f"overlay positioned at x={self.window_x} y={self.window_y} "
                    f"screen={sw}x{sh}")
        self._ready.set()

        # 消息循环
        msg = MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                _engine_log(f"message loop exit: GetMessage returned {ret}")
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 清理
        self._cleanup()

    def _cleanup(self):
        """销毁窗口和 GDI 资源。"""
        user32 = ctypes.windll.user32
        with _wnd_map_lock:
            _wnd_map.pop(self.hwnd, None)
        if self.renderer:
            self.renderer.cleanup()
            self.renderer = None
        if self.hwnd:
            # 杀死所有定时器
            for tid in list(self._timers.keys()):
                user32.KillTimer(self.hwnd, tid)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        self._theme_stop.set()

    # ── 消息处理 ──────────────────────────────────────────────────────

    def _handle_message(self, msg, wparam, lparam):
        """窗口过程回调（在消息泵线程内执行）。"""
        user32 = ctypes.windll.user32

        # Debug: log all messages briefly
        if msg in (0x000F, 0x0014, 0x0005, 0x0002, 0x0047, 0x0024, 0x0085, 0x0046, 0x001F, 0x00FF):
            _engine_log(f"[msg] msg=0x{msg:04X} wparam=0x{wparam:X} lparam=0x{lparam:X}")

        if msg == WM_TIMER:
            timer_id = wparam
            # 检查是否是系统定时器（fade / xN 动画）
            if timer_id == 1:
                self._on_fade_timer()
            elif timer_id == 2:
                self._on_xn_anim_timer()
            elif timer_id == 3:
                self._on_height_anim_timer()
            elif timer_id == 4:
                self._on_background_refresh_timer()
            else:
                # 用户注册的回调
                with self._timers_lock:
                    cb = self._timers.pop(timer_id, None)
                user32.KillTimer(self.hwnd, timer_id)
                if cb:
                    try:
                        cb()
                    except Exception as e:
                        _engine_log(f"timer callback error: {e}")
            return 0

        elif msg == WM_KEY_UPDATE:
            # 按键更新。wParam = ord(first char), lParam = count
            # 但 key_name 可能不是单字符，需要从 key_timestamps 重建
            self._handle_key_update()
            return 0

        elif msg == WM_INPUT:
            return self._handle_raw_input(lparam)

        elif msg == 0x000F:  # WM_PAINT
            if self.renderer:
                gdi32 = ctypes.windll.gdi32
                ps = PAINTSTRUCT()
                hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
                hdc_mem = self.renderer.hdc_mem
                mem_str = f"0x{hdc_mem:X}" if hdc_mem else "NULL"
                _engine_log(f"[paint] BeginPaint hdc=0x{hdc:X} mem={mem_str}")
                result = gdi32.BitBlt(
                    hdc,
                    ps.rcPaint.left, ps.rcPaint.top,
                    ps.rcPaint.right - ps.rcPaint.left,
                    ps.rcPaint.bottom - ps.rcPaint.top,
                    hdc_mem,
                    ps.rcPaint.left, ps.rcPaint.top,
                    0x00CC0020  # SRCCOPY
                )
                _engine_log(f"[paint] BitBlt result={result}, rect=({ps.rcPaint.left},{ps.rcPaint.top})-({ps.rcPaint.right},{ps.rcPaint.bottom})")
                user32.EndPaint(self.hwnd, ctypes.byref(ps))
            else:
                _engine_log("[paint] WM_PAINT but no renderer")
            return 0

        elif msg == 0x0014:  # WM_ERASEBKGND
            return 1  # skip background erase to avoid flicker

        elif msg == WM_FADE_START:
            self.fade_state = "waiting"
            self.fade_wait_end = time.time() + 2.0  # 2s for debugging
            # 设置 2s 等待定时器
            user32.SetTimer(self.hwnd, 1, 2000, None)
            return 0

        elif msg == 0x0010:  # WM_CLOSE
            user32.DestroyWindow(self.hwnd)
            return 0

        return user32.DefWindowProcW(self.hwnd, msg, wparam, lparam)

    def _handle_raw_input(self, lparam):
        """解析 WM_INPUT 消息中的键盘原始输入，通过回调分发虚拟键码。"""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 获取原始输入数据大小
        size = ctypes.wintypes.UINT(0)
        user32.GetRawInputData(
            lparam, RID_INPUT, None, ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER))

        if size.value == 0:
            user32.DefWindowProcW(self.hwnd, WM_INPUT, 0, lparam)
            return 0

        buf = ctypes.create_string_buffer(size.value)
        written = user32.GetRawInputData(
            lparam, RID_INPUT, ctypes.byref(buf), ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER))

        if written < 0:
            user32.DefWindowProcW(self.hwnd, WM_INPUT, 0, lparam)
            return 0

        # 解析 RAWINPUT 头部
        header = RAWINPUTHEADER.from_buffer_copy(buf)
        if header.dwType != RIM_TYPEKEYBOARD:
            user32.DefWindowProcW(self.hwnd, WM_INPUT, 0, lparam)
            return 0

        # 解析 RAWKEYBOARD
        kbd = RAWKEYBOARD.from_buffer_copy(buf, ctypes.sizeof(RAWINPUTHEADER))

        # 仅处理 key down（RI_KEY_MAKE），忽略 key up
        # 使用 _pressed_keys 去重：长按重复的 WM_KEYDOWN 只算一次
        if kbd.Flags == RI_KEY_MAKE and kbd.Message in (0x0100, 0x0104):
            vk = kbd.VKey
            if vk not in getattr(self, '_pressed_keys', set()):
                if not hasattr(self, '_pressed_keys'):
                    self._pressed_keys = set()
                self._pressed_keys.add(vk)
                if self._key_callback:
                    try:
                        self._key_callback(vk)
                    except Exception as e:
                        _engine_log(f"raw input callback error (vk={vk}): {e}")
        elif kbd.Flags == RI_KEY_BREAK:
            vk = kbd.VKey
            pressed = getattr(self, '_pressed_keys', None)
            if pressed is not None:
                pressed.discard(vk)

        # 必须调用 DefWindowProcW 完成清理
        user32.DefWindowProcW(self.hwnd, WM_INPUT, 0, lparam)
        return 0

    def _handle_key_update(self):
        """在窗口线程中处理按键更新：重建显示列表 + 渲染 + 启动淡出。"""
        log("[overlay] _handle_key_update called")
        # 从 timestamps 重建最新的 display_keys 和 counts
        now = time.time()
        # 清理过期
        while self.key_timestamps and now - self.key_timestamps[0][1] > 2.0:
            self.key_timestamps.popleft()

        # 重建：按出现顺序排列最近唯一键
        seen = set()
        new_keys = []
        for k, t in reversed(self.key_timestamps):
            if k not in seen:
                seen.add(k)
                new_keys.append(k)
        # 不反转：最新按键排最左

        # 更新
        self.display_keys.clear()
        for k in new_keys:
            self.display_keys.append(k)

        # 重建 counts
        self.display_counts.clear()
        for k in self.display_keys:
            cnt = sum(1 for kk, tt in self.key_timestamps if kk == k)
            self.display_counts[k] = cnt

        # 收集 xN 数据（只对 count 变化的键播动画）
        visible = list(self.display_keys)[:self.MAX_KEYS]
        xN_queue = []

        # 清理已过期 count 的追踪记录
        for k in list(self._xN_last_counts):
            if k not in visible or self.display_counts.get(k, 0) < 2:
                del self._xN_last_counts[k]

        for i, key in enumerate(visible):
            count = self.display_counts.get(key, 1)
            if count >= 2 and count > self._xN_last_counts.get(key, 0):
                self._xN_last_counts[key] = count
                x = self.PAD + i * (self.CARD_W + self.GAP)
                y = self.PAD
                tag = f'x{count}'
                tag_w = 18 if count < 10 else 24
                tag_h = 16
                tag_x = x + self.CARD_W - 2
                tag_y = y - 4
                xN_queue.append((tag, tag_w, tag_h, tag_x, tag_y))

        # 启动 xN 弹跳动画（合并模式）
        if xN_queue:
            self._start_xn_animation(xN_queue)

        # 渲染：动画进行中则用动画帧；否则持久化渲染 count≥2 的 xN
        self._capture_glass_background()
        if self._xN_anim_data and self._xN_anim_idx < len(self._xN_anim_scales):
            scale = self._xN_anim_scales[self._xN_anim_idx]
            current_xn = [(tag, tw, th, tx, ty, scale)
                          for tag, tw, th, tx, ty in self._xN_anim_data]
            self._render(xN_data=current_xn)
        else:
            persistent_xn = self._build_persistent_xn(visible)
            self._render(xN_data=persistent_xn if persistent_xn else None)

        # 显示并启动淡出
        self.source_alpha = int(255 * self.window_opacity)
        self._unpremultiply_colors()
        self._update_constant_alpha()
        ctypes.windll.user32.ShowWindow(self.hwnd, SW_SHOW)
        self._paint_dib_to_window()

        # 启动后台刷新定时器（窗口可见期间持续更新毛玻璃背景）
        ctypes.windll.user32.SetTimer(self.hwnd, 4, 50, None)

        # 刷新置顶
        self._force_topmost()

        # 启动高度渐变动画（xN 弹跳期间不抢帧）
        if (self.renderer
                and self.renderer.height_animation_active()
                and not (self._xN_anim_data and self._xN_anim_idx < len(self._xN_anim_scales))):
            user32 = ctypes.windll.user32
            user32.KillTimer(self.hwnd, 3)
            user32.SetTimer(self.hwnd, 3, 16, None)

        # 淡出计划
        ctypes.windll.user32.PostMessageW(self.hwnd, WM_FADE_START, 0, 0)

    def _build_persistent_xn(self, visible):
        """构建 persist 态 xN 数据：所有 count≥2 的可见键，scale=1.0。"""
        result = []
        for i, key in enumerate(visible):
            count = self.display_counts.get(key, 1)
            if count < 2:
                continue
            x = self.PAD + i * (self.CARD_W + self.GAP)
            y = self.PAD
            tag = f'x{count}'
            tag_w = 18 if count < 10 else 24
            tag_h = 16
            tag_x = x + self.CARD_W - 2
            tag_y = y - 4
            result.append((tag, tag_w, tag_h, tag_x, tag_y, 1.0))
        return result

    def _start_xn_animation(self, xN_queue):
        """启动或合并 xN 弹跳关键帧动画。"""
        user32 = ctypes.windll.user32
        if self._xN_anim_data is not None and self._xN_anim_idx < len(self._xN_anim_scales):
            # 动画进行中：合并新标签，不杀定时器
            existing = {item[0] for item in self._xN_anim_data}
            for item in xN_queue:
                if item[0] not in existing:
                    self._xN_anim_data.append(item)
        else:
            # 无动画或已结束：重新开始
            self._xN_anim_data = xN_queue
            self._xN_anim_idx = 0
            if self._xN_anim_timer is not None:
                user32.KillTimer(self.hwnd, self._xN_anim_timer)
            self._xN_anim_timer = None
            user32.SetTimer(self.hwnd, 2,
                           self._xN_anim_delays[0] if self._xN_anim_delays[0] > 0 else 1, None)

    def _on_xn_anim_timer(self):
        """xN 弹跳动画的定时回调。"""
        user32 = ctypes.windll.user32
        if not self._xN_anim_data or self._xN_anim_idx >= len(self._xN_anim_scales):
            user32.KillTimer(self.hwnd, 2)
            self._xN_anim_data = None
            self._xN_anim_timer = None
            return

        scale = self._xN_anim_scales[self._xN_anim_idx]
        # 构建带 scale 的 xN 数据
        scaled_data = []
        for tag, tw, th, tx, ty in self._xN_anim_data:
            scaled_data.append((tag, tw, th, tx, ty, scale))

        # 渲染（带上 xN 动画数据）
        self._capture_glass_background()
        self._render(xN_data=scaled_data)
        self._update_window()

        self._xN_anim_idx += 1
        if self._xN_anim_idx < len(self._xN_anim_scales):
            delay = self._xN_anim_delays[self._xN_anim_idx]
            user32.SetTimer(self.hwnd, 2, max(delay, 1), None)
        else:
            user32.KillTimer(self.hwnd, 2)
            self._xN_anim_data = None
            self._xN_anim_timer = None
            # 动画结束，立即切到 persist 态渲染 xN
            visible = list(self.display_keys)[:self.MAX_KEYS]
            persistent_xn = self._build_persistent_xn(visible)
            self._capture_glass_background()
            self._render(xN_data=persistent_xn if persistent_xn else None)
            self._update_window()
            # xN 弹跳结束后，如果高度动画还在跑就续上
            if self.renderer and self.renderer.height_animation_active():
                user32.KillTimer(self.hwnd, 3)
                user32.SetTimer(self.hwnd, 3, 16, None)

    def _on_height_anim_timer(self):
        """高度渐变动画定时器：持续重绘直到所有条收敛。"""
        user32 = ctypes.windll.user32
        if not self.renderer or not self.renderer.height_animation_active():
            user32.KillTimer(self.hwnd, 3)
            return
        # 保持 xN 标签显示
        visible = list(self.display_keys)[:self.MAX_KEYS]
        persistent_xn = self._build_persistent_xn(visible)
        self._capture_glass_background()
        self._render(xN_data=persistent_xn if persistent_xn else None)
        self._update_window()
        self._force_topmost()
        user32.SetTimer(self.hwnd, 3, 16, None)

    def _on_fade_timer(self):
        """淡出阶段定时器（每 30ms 步进）。"""
        user32 = ctypes.windll.user32

        if self.fade_state == "waiting":
            # 等待结束，开始淡出
            self.fade_state = "fading"
            user32.SetTimer(self.hwnd, 1, 30, None)
            self._fade_step()
        elif self.fade_state == "fading":
            self._fade_step()
        else:
            user32.KillTimer(self.hwnd, 1)

    def _fade_step(self):
        """淡出步进：降低 source_alpha 并更新整窗透明度。"""
        self.source_alpha = max(0, self.source_alpha - 15)

        if self.source_alpha > 0:
            self._update_constant_alpha()
            self._force_topmost()
        else:
            self.fade_state = "idle"
            ctypes.windll.user32.KillTimer(self.hwnd, 4)  # 停止后台刷新
            ctypes.windll.user32.ShowWindow(self.hwnd, SW_HIDE)
            ctypes.windll.user32.KillTimer(self.hwnd, 1)

    def _on_background_refresh_timer(self):
        """后台刷新定时器：窗口可见期间持续更新毛玻璃背景。

        在 xN 弹跳动画或高度动画进行中时跳过，避免渲染冲突。
        """
        user32 = ctypes.windll.user32
        # 动画进行中时跳过（各自定时器会处理渲染）
        if self._xN_anim_data is not None and self._xN_anim_idx < len(self._xN_anim_scales):
            return
        if self.renderer and self.renderer.height_animation_active():
            return
        if not self.hwnd or not self.renderer or self.fade_state == "idle":
            user32.KillTimer(self.hwnd, 4)
            return

        visible = list(self.display_keys)[:self.MAX_KEYS]
        persistent_xn = self._build_persistent_xn(visible)
        self._capture_glass_background()
        self._render(xN_data=persistent_xn if persistent_xn else None)
        self._unpremultiply_colors()
        self._update_constant_alpha()
        self._paint_dib_to_window()

    # ── 毛玻璃效果 ──────────────────────────────────────────────────────

    def _capture_glass_background(self):
        """捕获浮窗背后的屏幕区域，用 numpy+scipy 做盒式模糊实现毛玻璃效果。"""
        if not self.hwnd:
            return

        now = time.time()
        if (self._glass_cache is not None
                and now - self._glass_cache[3] < self._glass_cache_interval):
            return  # 缓存中，直接复用

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        # 获取浮窗在屏幕上的位置
        rect = RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        x, y = rect.left, rect.top
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return

        # 捕获屏幕像素
        hdc_screen = user32.GetDC(None)
        hdc_cap = gdi32.CreateCompatibleDC(hdc_screen)
        hbm_cap = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        gdi32.SelectObject(hdc_cap, hbm_cap)
        gdi32.BitBlt(hdc_cap, 0, 0, w, h, hdc_screen, x, y, 0x00CC0020)

        # 读回像素
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        buf_size = w * h * 4
        pixels = (ctypes.c_ubyte * buf_size)()
        gdi32.GetDIBits(hdc_cap, hbm_cap, 0, h, ctypes.byref(pixels),
                        ctypes.byref(bmi), DIB_RGB_COLORS)

        # 清理 GDI
        gdi32.DeleteObject(hbm_cap)
        gdi32.DeleteDC(hdc_cap)
        user32.ReleaseDC(None, hdc_screen)

        # numpy+scipy 盒式模糊（C 级速度，远快于纯 Python 循环）
        arr = np.frombuffer(pixels, dtype=np.uint8).reshape((h, w, 4))
        rgb = arr[:, :, :3].astype(np.float32)
        # uniform_filter 的 size 参数对应盒式模糊核边长，7 即 7×7 核
        blurred = uniform_filter(rgb, size=7, axes=(0, 1))
        arr[:, :, :3] = np.clip(blurred, 0, 255).astype(np.uint8)
        self._glass_cache = (arr.tobytes(), w, h, now)

    # ── 渲染 ──────────────────────────────────────────────────────────

    def _render(self, xN_data=None):
        """调用 HeatmapRenderer 绘制当前帧。"""
        if not self.renderer:
            return
        keys = list(self.display_keys)
        skip_bg = self.blur_enabled
        if self.overlay_mode == "cards":
            self.renderer.draw_cards_frame(keys, self.display_counts,
                                           self.color_scheme, xN_data,
                                           skip_panel_bg=skip_bg)
        else:
            self.renderer.draw_frame(keys, self.display_counts,
                                     self.color_scheme, xN_data,
                                     skip_panel_bg=skip_bg)

    def _unpremultiply_colors(self):
        """反预乘颜色（供 BitBlt SRCCOPY 使用）。

        HeatmapRenderer 输出预乘 Alpha（供 UpdateLayeredWindow AC_SRC_ALPHA）。
        BitBlt SRCCOPY 不处理 alpha，直接拷贝像素值 → 预乘值偏暗。
        这里反预乘恢复原始颜色。Alpha=0 的背景像素填充为面板底色，
        因为 LWA_ALPHA 不支持逐像素透明。
        毛玻璃模式下，背景像素使用屏幕捕获的模糊画面。

        numpy 向量化实现，避免每帧 28k+ Python 循环。
        """
        if not self.renderer:
            return
        dib = self.renderer._pixels
        if not dib:
            return

        # 毛玻璃：捕获模糊背景（先于 render 已调用过一次用于初始干净帧，
        # 这里再次调用确保每次 paint 前背景都是最新的，缓存间隔内会复用）
        if self.blur_enabled:
            self._capture_glass_background()

        # 首次进入 blur 分支时打日志
        if self.blur_enabled and not getattr(self, '_blur_logged', False):
            self._blur_logged = True
            has_cache = self._glass_cache is not None
            _engine_log(f"blur render: enabled has_cache={has_cache} win={self.win_w}x{self.win_h}")

        # 面板底色（深色半透明）
        PANEL_B, PANEL_G, PANEL_R = 25, 26, 38
        glass_tint = 210  # 毛玻璃模式下背景混合强度 (0-255)，越高越透

        # 向量化：reshape DIB → (h, w, 4) numpy 数组
        arr = np.frombuffer(dib, dtype=np.uint8).reshape((self.win_h, self.win_w, 4))
        alpha = arr[:, :, 3]
        bg_mask = alpha <= 0
        fg_mask = ~bg_mask

        # 背景像素：毛玻璃混合 或 纯面板底色
        if self.blur_enabled and self._glass_cache:
            glass_bytes, gw, gh, _ts = self._glass_cache
            garr = np.frombuffer(glass_bytes, dtype=np.uint8).reshape((gh, gw, 4))
            # uint16 防 uint8 溢出
            arr[bg_mask, 0] = (garr[bg_mask, 0].astype(np.uint16) * glass_tint
                               + PANEL_B * (255 - glass_tint)) // 255
            arr[bg_mask, 1] = (garr[bg_mask, 1].astype(np.uint16) * glass_tint
                               + PANEL_G * (255 - glass_tint)) // 255
            arr[bg_mask, 2] = (garr[bg_mask, 2].astype(np.uint16) * glass_tint
                               + PANEL_R * (255 - glass_tint)) // 255
        else:
            arr[bg_mask, 0] = PANEL_B
            arr[bg_mask, 1] = PANEL_G
            arr[bg_mask, 2] = PANEL_R
        arr[bg_mask, 3] = 0xFF

        # 反预乘前景像素：c' = c * 255 / a（uint32 防溢出）
        if fg_mask.any():
            a_fg = alpha[fg_mask].astype(np.uint32)
            for c in range(3):
                arr[fg_mask, c] = np.clip(
                    (arr[fg_mask, c].astype(np.uint32) * 255) // a_fg,
                    0, 255
                ).astype(np.uint8)
        # alpha channel preserved for SetWindowRgn corner clipping

        # 写回 bytearray（避免重复 frombuffer 开销）
        dib[:] = arr.tobytes()

    def _paint_dib_to_window(self):
        """触发 WM_PAINT（必须走 BeginPaint 路径，DWM 才认）。"""
        if not self.hwnd or not self.renderer:
            return
        ctypes.windll.user32.InvalidateRect(self.hwnd, None, True)
        ctypes.windll.user32.UpdateWindow(self.hwnd)

    def _invalidate_window(self):
        """触发异步 WM_PAINT（仅作 fallback，实际用 _paint_dib_to_window 同步刷新）。"""
        if not self.hwnd:
            return
        ctypes.windll.user32.InvalidateRect(self.hwnd, None, False)

    def _update_constant_alpha(self):
        """用 LWA_ALPHA 更新整窗透明度（用于淡入淡出）。"""
        if not self.hwnd:
            return
        ctypes.windll.user32.SetLayeredWindowAttributes(
            self.hwnd,
            0,              # no colorkey
            self.source_alpha,
            LWA_ALPHA       # LWA_ALPHA only — DComp-compatible path
        )

    def _update_window(self):
        """使用 LWA_ALPHA + 同步 BitBlt 路径更新窗口。

        相比 UpdateLayeredWindow（逐像素 alpha）：
        - 走 GDI BitBlt + DWM 恒定 alpha 合成路径
        - Z-order 穿透 DComp（Marvis/Electron）
        - 圆角由 SetWindowRgn 裁切
        """
        if not self.hwnd or not self.renderer:
            return
        self._unpremultiply_colors()
        self._update_constant_alpha()
        self._paint_dib_to_window()

    # ── 置顶 ──────────────────────────────────────────────────────────

    def _apply_rounded_corners(self):
        """用 SetWindowRgn 裁切圆角。

        LWA_ALPHA 路径不支持逐像素 alpha，圆角像素会以原始颜色
        显示（不透明）。用 CreateRoundRectRgn 裁切掉四角。
        """
        if not self.hwnd:
            return
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        # Corner radius matching the renderer's rounded rect
        r = 12
        hrgn = gdi32.CreateRoundRectRgn(0, 0, self.win_w + 1, self.win_h + 1, r * 2, r * 2)
        if hrgn:
            user32.SetWindowRgn(self.hwnd, hrgn, True)
            # hrgn ownership transferred to window — no need to DeleteObject

    def _force_topmost(self):
        """Win32 级别强行置顶。"""
        if not self.hwnd:
            return
        user32 = ctypes.windll.user32
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    # ── 全屏检测 ──────────────────────────────────────────────────────

    def _is_fullscreen_exclusive(self):
        """缓存 1 秒的全屏独占检测。"""
        now = time.time()
        if now - self._fullscreen_cache < 1.0:
            return self._fullscreen_cached_val
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                self._fullscreen_cached_val = False
            else:
                style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                if style & 0x00C00000:  # WS_CAPTION
                    self._fullscreen_cached_val = False
                else:
                    rect = RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    sw = user32.GetSystemMetrics(0)
                    sh = user32.GetSystemMetrics(1)
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    self._fullscreen_cached_val = (abs(w - sw) <= 10 and abs(h - sh) <= 10)
        except:
            self._fullscreen_cached_val = False
        self._fullscreen_cache = now
        return self._fullscreen_cached_val

    # ── 主题检测 ──────────────────────────────────────────────────────

    def _read_theme(self):
        """读取 Windows 注册表判断当前主题模式。"""
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

            _engine_log(f"[theme] AppsUseLightTheme={apps_val} SystemUsesLightTheme={sys_val} -> {theme}")
            return theme
        except Exception as e:
            _engine_log(f"[theme] read error: {e}")
            return "dark"

    def _start_theme_watcher(self):
        """后台线程每 60 秒检测主题变化。"""
        self._theme_stop.clear()

        def watch():
            current = self.color_scheme
            while not self._theme_stop.is_set():
                new_scheme = self._read_theme()
                if new_scheme != current:
                    current = new_scheme
                    self.color_scheme = new_scheme
                    # 立即重绘
                    if self.hwnd:
                        self._capture_glass_background()
                        self._render()
                        self._unpremultiply_colors()
                        self._update_constant_alpha()
                        self._paint_dib_to_window()
                self._theme_stop.wait(60)

        t = threading.Thread(target=watch, daemon=True, name="theme-watcher")
        t.start()
