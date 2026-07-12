# ⌨️ KeyHeatmap

> 键盘热力图 — 可视化你的打字习惯

KeyHeatmap 是一款 Windows 键盘热力图工具，在后台静默运行，实时追踪你的每一次按键，将打字数据转化为直观的热力图和统计图表。
## 📸 截图

![KeyHeatmap Dashboard](https://raw.githubusercontent.com/GlacierO3O/KeyHeatmap/main/screenshot.png)


## ✨ 功能

- **右下角浮窗** — 实时显示最近按键 + 连击计数（毛玻璃效果）
- **网页热力图** — 访问 `http://localhost:18888` 查看完整仪表盘：
  - 🏆 **称号徽章** — 根据累计按键量自动生成称号（打字狂魔 / 键盘粉碎机 等）
  - ⌨️ **按键热力图** — 全键盘视觉化，颜色越红按键越频繁
  - 🕐 **时段热图** — 24 小时按键分布柱状图，鼠标悬停显示精确次数
  - 📈 **趋势折线图** — 每日按键趋势，矩形阴影带平滑跟随鼠标
  - 📊 **按键排行** — TOP 15 按键排行条形图
- **设置页面** — 浮窗透明度、毛玻璃效果开关、Combo 开关、游戏白名单等
- **游戏白名单** — `Ctrl+Shift+F8` 快速添加/移除当前进程，游戏中不显示浮窗
- **自动更新** — 基于 GitHub Releases 的版本检测与更新

## 🚀 快速开始

1. 从 [Releases](https://github.com/GlacierO3O/KeyHeatmap/releases) 下载最新 `KeyHeatmap.exe`
2. 双击启动（首次需管理员权限以支持浮窗覆盖其他窗口）
3. 任务栏右下角出现托盘图标即启动成功
4. 左键托盘图标 →「打开热力图」查看统计

## ⚙️ 热键

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+Shift+F8` | 添加/移除当前进程到游戏白名单 |

## 🛠️ 技术栈

- **语言**: Python
- **GUI 浮窗**: PyQt5 + Windows DWM 合成
- **Web 仪表盘**: 内嵌 HTTP 服务器 + Chart.js
- **打包**: PyInstaller

## 📄 License

MIT
