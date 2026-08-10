import json
import socket
import time
import webbrowser
import urllib.request

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

BASE = "http://127.0.0.1:18888"
LOCK_PORT = 18889


def api(method, path):
    try:
        req = urllib.request.Request(BASE + path, method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return {}


def create_icon_image():
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 16, 60, 56], radius=6, fill=(80, 200, 120), outline=(60, 170, 100), width=2)
    for row in range(3):
        y = 22 + row * 10
        for col in range(5):
            x = 10 + col * 10
            draw.rounded_rectangle([x, y, x + 7, y + 6], radius=1, fill=(255, 255, 255, 200))
    for col in range(4):
        x = 15 + col * 10
        draw.rounded_rectangle([x, y + 26, x + 7, y + 6 + 26], radius=1, fill=(255, 255, 255, 200))
    return img


def open_heatmap(icon, item):
    webbrowser.open(BASE + "/?period=today")


def toggle_pause(icon, item):
    api("POST", "/api/pause")
    icon.menu = build_menu()


def toggle_autostart(icon, item):
    api("POST", "/api/toggle_autostart")
    icon.menu = build_menu()


def quit_app(icon, item):
    api("POST", "/api/quit")
    icon.stop()


def build_menu():
    state = api("GET", "/api/tray/state")
    paused = bool(state.get("paused"))
    autostart = bool(state.get("autostart"))
    return Menu(
        MenuItem("📊 查看热力图", open_heatmap, default=True),
        MenuItem("▶ 恢复记录" if paused else "⏸ 暂停记录", toggle_pause),
        MenuItem("🚀 开机自启  ✓" if autostart else "🚀 开机自启", toggle_autostart),
        Menu.SEPARATOR,
        MenuItem("❌ 退出", quit_app),
    )


def main():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return
    for _ in range(40):
        if api("GET", "/api/tray/state"):
            break
        time.sleep(0.5)
    icon = Icon("KeyHeatmapHelper", create_icon_image(), "键盘热力图", build_menu())
    icon.run()


if __name__ == "__main__":
    main()
