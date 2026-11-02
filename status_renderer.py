"""
图片渲染模块。

用 Pillow 纯手绘一张状态卡片，不依赖浏览器 / wkhtmltoimage，因此：
  * 冷启动只要几十毫秒，内存占用低；
  * 在任何能跑 psutil 的机器上都能出图；
  * 服务器不需要额外装 Chromium。

版式是竖屏手机卡片（逻辑宽 720，高度随内容浮动），在聊天窗口里点开就能
一屏读完，不需要横向拖动。

实现要点：
  1. 以逻辑坐标（宽 720）布局，实际画布按 scale（默认 2）放大，最后 LANCZOS
     缩回，等于做了一次超采样，圆弧和文字边缘不会有锯齿。
  2. 中文字体自动查找（Windows / macOS / Linux 常见路径 + fc-match 兜底）。
  3. 没有粗体字重时用 stroke_width 模拟加粗，避免为不同字重准备多份字体文件。
"""

from __future__ import annotations

import io
import math
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from glob import glob

from PIL import Image, ImageDraw, ImageFont

from status_collector import fmt_bytes, fmt_rate, fmt_uptime

# --------------------------------------------------------------------------- #
# 主题
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Theme:
    bg_top: tuple
    bg_bottom: tuple
    card: tuple
    stroke: tuple
    text: tuple
    muted: tuple
    faint: tuple
    accent: tuple
    ok: tuple
    warn: tuple
    danger: tuple
    track: tuple


DARK = Theme(
    bg_top=(12, 16, 25),
    bg_bottom=(21, 28, 43),
    card=(23, 30, 45),
    stroke=(38, 50, 71),
    text=(234, 240, 250),
    muted=(146, 160, 182),
    faint=(103, 116, 137),
    accent=(84, 146, 255),
    ok=(59, 214, 149),
    warn=(255, 183, 77),
    danger=(255, 94, 122),
    track=(40, 52, 74),
)

LIGHT = Theme(
    bg_top=(248, 250, 254),
    bg_bottom=(231, 238, 249),
    card=(255, 255, 255),
    stroke=(224, 231, 242),
    text=(26, 34, 50),
    muted=(103, 117, 139),
    faint=(152, 164, 182),
    accent=(40, 112, 242),
    ok=(15, 168, 114),
    warn=(212, 138, 18),
    danger=(224, 66, 92),
    track=(233, 239, 248),
)


def hex_to_rgb(value: str, fallback: tuple) -> tuple:
    try:
        value = value.strip().lstrip("#")
        if len(value) == 3:
            value = "".join(c * 2 for c in value)
        if len(value) != 6:
            return fallback
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


# --------------------------------------------------------------------------- #
# 字体
# --------------------------------------------------------------------------- #

_FONT_CANDIDATES = [
    # Windows
    ("C:/Windows/Fonts/msyh.ttc", 0),
    ("C:/Windows/Fonts/msyh.ttf", 0),
    ("C:/Windows/Fonts/simhei.ttf", 0),
    ("C:/Windows/Fonts/deng.ttf", 0),
    ("C:/Windows/Fonts/simsun.ttc", 0),
    # macOS
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
    # Linux（按覆盖率排序，Droid 兜底最稳）
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    ("/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf", 0),
    ("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", 0),
    ("/usr/share/fonts/truetype/arphic/uming.ttc", 0),
]

_GLOB_PATTERNS = [
    "/usr/share/fonts/**/*CJK*.ttc",
    "/usr/share/fonts/**/*CJK*.otf",
    "/usr/share/fonts/**/*CJKsc*",
    "/usr/share/fonts/**/*wqy*",
    "/usr/share/fonts/**/*Hei*",
    "/usr/share/fonts/**/*Song*",
    "/usr/share/fonts/**/*Droid*Fallback*",
    "/System/Library/Fonts/**/*Hei*",
]


def find_font(custom: str = "") -> tuple:
    """返回 (字体路径, ttc 索引)。找不到时返回 (None, 0)，Pillow 会用内置位图字体。"""
    if custom:
        path = custom.strip()
        if os.path.isfile(path):
            return path, 0
        print(f"[server_status] 指定的字体不存在：{path}", file=sys.stderr)

    for path, index in _FONT_CANDIDATES:
        if os.path.isfile(path):
            return path, index

    for pattern in _GLOB_PATTERNS:
        for path in sorted(glob(pattern, recursive=True)):
            if os.path.isfile(path) and path.lower().endswith((".ttf", ".ttc", ".otf")):
                return path, 0

    # 最后尝试 fontconfig
    if platform.system() == "Linux":
        try:
            out = subprocess.check_output(
                ["fc-match", "-f", "%{file}", "sans-serif:lang=zh"],
                timeout=3,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            if out and os.path.isfile(out.strip()):
                return out.strip(), 0
        except Exception:
            pass

    return None, 0


class FontBook:
    """按字号缓存字体对象。bold 用描边模拟，省掉一套粗体文件。"""

    def __init__(self, path, index, scale):
        self.path = path
        self.index = index
        self.scale = scale
        self._cache = {}

    def font(self, size: int):
        size = max(int(size), 6)
        if size not in self._cache:
            px = int(round(size * self.scale))
            if self.path:
                try:
                    self._cache[size] = ImageFont.truetype(
                        self.path, px, index=self.index
                    )
                except Exception:
                    self._cache[size] = ImageFont.load_default()
            else:
                self._cache[size] = ImageFont.load_default()
        return self._cache[size]


# --------------------------------------------------------------------------- #
# 画布
# --------------------------------------------------------------------------- #


class Canvas:
    """逻辑坐标绘图：所有对外接口都收逻辑像素，内部乘 scale。"""

    def __init__(self, image: Image.Image, fonts: FontBook, theme: Theme, scale: float):
        self.img = image
        self.d = ImageDraw.Draw(image)
        self.f = fonts
        self.t = theme
        self.s = scale
        self.bold_stroke = True

    def _xy(self, x, y):
        return (x * self.s, y * self.s)

    def text(self, xy, content, size, color, bold=False, anchor="lm"):
        font = self.f.font(size)
        stroke = 0
        if bold:
            stroke = max(1, int(round(size * self.s * 0.028)))
        self.d.text(
            self._xy(*xy),
            str(content),
            font=font,
            fill=color,
            anchor=anchor,
            stroke_width=stroke,
            stroke_fill=color,
        )

    def measure(self, content, size, bold=False):
        font = self.f.font(size)
        width = font.getlength(str(content))
        if bold:
            width += 2 * max(1, int(round(size * self.s * 0.028)))
        return width / self.s

    def fit_size(self, content, max_width, size, min_size=9, bold=False):
        """把文字缩到能放进 max_width 的最大字号。"""
        current = size
        while current > min_size and self.measure(content, current, bold) > max_width:
            current -= 1
        return current

    def ellipse(self, cx, cy, r, fill=None, outline=None, width=1):
        box = [
            (cx - r) * self.s,
            (cy - r) * self.s,
            (cx + r) * self.s,
            (cy + r) * self.s,
        ]
        self.d.ellipse(box, fill=fill, outline=outline, width=int(width * self.s))

    def round_rect(self, x0, y0, x1, y1, radius, fill=None, outline=None, width=1):
        self.d.rounded_rectangle(
            [x0 * self.s, y0 * self.s, x1 * self.s, y1 * self.s],
            radius=radius * self.s,
            fill=fill,
            outline=outline,
            width=max(1, int(round(width * self.s))) if outline else 0,
        )

    def line(self, points, color, width=1):
        self.d.line(
            [(x * self.s, y * self.s) for x, y in points],
            fill=color,
            width=max(1, int(round(width * self.s))),
            joint="curve",
        )

    def ring(self, cx, cy, radius, thickness, ratio, color):
        bbox = [
            (cx - radius) * self.s,
            (cy - radius) * self.s,
            (cx + radius) * self.s,
            (cy + radius) * self.s,
        ]
        w = max(1, int(round(thickness * self.s)))
        # 注意：Pillow 的 arc 画 start=0 end=360 会失效，底轨用 ellipse 画完整圆环
        self.d.ellipse(bbox, outline=self.t.track, width=w)
        ratio = max(0.0, min(float(ratio), 1.0))
        if ratio <= 0.002:
            return
        end = -90 + 360 * ratio
        self.d.arc(bbox, -90, end, fill=color, width=w)
        # 圆头端点（占比过小时跳过，避免出现孤零零的圆点）
        if ratio < 0.02:
            return
        mid_r = radius - thickness / 2
        dot = thickness / 2
        for deg in (-90, end):
            rad = math.radians(deg)
            self.ellipse(
                cx + mid_r * math.cos(rad),
                cy + mid_r * math.sin(rad),
                dot,
                fill=color,
            )

    def bar(self, x, y, width, height, ratio, color):
        radius = height / 2
        self.round_rect(x, y, x + width, y + height, radius, fill=self.t.track)
        ratio = max(0.0, min(float(ratio), 1.0))
        if ratio <= 0:
            return
        filled = max(width * ratio, height)
        self.round_rect(x, y, x + filled, y + height, radius, fill=color)


def _gradient(size, top, bottom):
    width, height = size
    strip = Image.new("RGB", (1, height))
    pixels = strip.load()
    for y in range(height):
        t = y / max(height - 1, 1)
        pixels[0, y] = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        )
    return strip.resize((width, height), Image.BILINEAR)


def _level_color(percent, theme: Theme):
    if percent >= 88:
        return theme.danger
    if percent >= 70:
        return theme.warn
    return theme.ok


def _truncate(canvas: Canvas, content: str, max_width: float, size: int) -> str:
    if canvas.measure(content, size) <= max_width:
        return content
    ellipsis = "…"
    lo, hi = 0, len(content)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if canvas.measure(content[:mid] + ellipsis, size) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return content[:lo] + ellipsis if lo else ellipsis


# --------------------------------------------------------------------------- #
# 布局常量（竖屏：窄而高，手机上一屏读完）
# --------------------------------------------------------------------------- #

W = 720
MARGIN = 24
CONTENT_W = W - MARGIN * 2
GAP = 14
CARD_PAD = 20
RADIUS = 18

HEADER_H = 96
GAUGE_H = 250
RATE_H = 96
FOOTER_H = 34


def _freq_text(cpu: dict) -> str:
    current = float(cpu.get("freq") or 0)
    maximum = float(cpu.get("freq_max") or 0)

    def ghz(value):
        text = f"{value / 1000:.2f}"
        # 1.50 -> 1.5，避免无意义的尾随零
        return text.rstrip("0").rstrip(".") if "." in text else text

    if current and maximum:
        return f"{ghz(current)} / {ghz(maximum)} GHz"
    if current:
        return f"{ghz(current)} GHz"
    return ""


# --------------------------------------------------------------------------- #
# 主渲染
# --------------------------------------------------------------------------- #


def render(data: dict, config: dict | None = None) -> Image.Image:
    config = config or {}

    theme_name = str(config.get("theme", "dark")).lower()
    theme = LIGHT if theme_name == "light" else DARK

    accent = hex_to_rgb(str(config.get("accent_color", "")), theme.accent)
    if accent != theme.accent:
        theme = Theme(**{**theme.__dict__, "accent": accent})

    scale = float(config.get("render_scale", 2) or 2)
    scale = min(max(scale, 1.0), 3.0)

    font_path, font_index = find_font(str(config.get("font_path", "") or ""))
    fonts = FontBook(font_path, font_index, scale)

    static = data.get("static", {})
    cpu = data.get("cpu", {})
    memory = data.get("memory", {})
    net = data.get("net", {})
    disk_io = data.get("disk_io", {})
    disks = data.get("disks", []) or []
    processes = data.get("processes", []) or []
    show_processes = bool(config.get("show_processes", True)) and bool(processes)
    show_disks = bool(config.get("show_disk_partitions", True)) and bool(disks)
    bot = data.get("bot") or {}

    # ---------------- 先算高度 ---------------- #
    disk_rows = min(len(disks), int(config.get("max_disk_partitions", 4) or 4))
    disk_h = 49 + disk_rows * 46
    proc_h = 76 + len(processes) * 32

    total_h = MARGIN + HEADER_H + GAP + GAUGE_H + GAP + RATE_H
    if show_disks:
        total_h += GAP + disk_h
    if show_processes:
        total_h += GAP + proc_h
    total_h += GAP + FOOTER_H + MARGIN

    img = _gradient((int(W * scale), int(total_h * scale)), theme.bg_top, theme.bg_bottom)
    canvas = Canvas(img, fonts, theme, scale)
    now = datetime.now()

    inner_x = MARGIN + CARD_PAD
    inner_w = CONTENT_W - CARD_PAD * 2

    # ---------------- Header ---------------- #
    top = MARGIN
    canvas.ellipse(MARGIN + 3, top + 7, 3.5, fill=theme.ok)
    canvas.text((MARGIN + 13, top + 7), "实时系统监控", 11, theme.muted)

    canvas.text((W - MARGIN, top + 9), now.strftime("%H:%M"), 21, theme.text, bold=True, anchor="rm")
    weekday = "一二三四五六日"[now.weekday()]
    canvas.text(
        (W - MARGIN, top + 29),
        f"{now.strftime('%m-%d')} 周{weekday}",
        10.5,
        theme.muted,
        anchor="rm",
    )

    canvas.text((MARGIN, top + 47), "服务器状态", 30, theme.text, bold=True)

    subtitle = " · ".join(
        part
        for part in (
            str(static.get("hostname", "unknown")),
            str(static.get("os_name", "-")),
            f"已运行 {fmt_uptime(data.get('uptime', 0))}" if data.get("uptime") else "",
        )
        if part
    )
    sub_size = canvas.fit_size(subtitle, CONTENT_W, 11.5, 9)
    canvas.text((MARGIN, top + 77), subtitle, sub_size, theme.muted)

    # ---------------- CPU / 内存 ---------------- #
    gauge_top = MARGIN + HEADER_H + GAP
    canvas.round_rect(
        MARGIN, gauge_top, W - MARGIN, gauge_top + GAUGE_H, RADIUS,
        fill=theme.card, outline=theme.stroke,
    )

    cores = int(static.get("cpu_logical", 0) or 0)
    freq_text = _freq_text(cpu)
    cpu_detail = " · ".join(p for p in (f"{cores} 线程" if cores else "", freq_text) if p)
    # CPU 数字用的是哪种口径，直接在卡片上标出来，避免和任务管理器对不上时产生误会
    metric_note = "任务管理器口径" if str(cpu.get("metric")) == "utility" else "时间占比"
    cpu_sub = _truncate(
        canvas,
        f"{metric_note} · {static.get('cpu_model', '-')}",
        CONTENT_W / 2 - 30,
        9.5,
    )
    mem_detail = f"{fmt_bytes(memory.get('used', 0))} / {fmt_bytes(memory.get('total', 0))}"
    mem_sub = f"可用 {fmt_bytes(memory.get('available', 0))}"

    gauges = [
        {"label": "CPU", "value": float(cpu.get("percent", 0.0)), "detail": cpu_detail, "sub": cpu_sub},
        {"label": "内存", "value": float(memory.get("percent", 0.0)), "detail": mem_detail, "sub": mem_sub},
    ]

    col_w = CONTENT_W / 2
    ring_r, ring_t = 74, 14
    ring_cy = gauge_top + 24 + ring_r

    for idx, gauge in enumerate(gauges):
        cx = MARGIN + col_w * idx + col_w / 2
        if idx > 0:
            canvas.line(
                [
                    (MARGIN + col_w, gauge_top + 38),
                    (MARGIN + col_w, gauge_top + GAUGE_H - 30),
                ],
                theme.stroke,
                1,
            )

        color = _level_color(gauge["value"], theme)
        canvas.ring(cx, ring_cy, ring_r, ring_t, gauge["value"] / 100.0, color)
        canvas.text((cx, ring_cy - 5), f"{gauge['value']:.0f}%", 28, theme.text, bold=True, anchor="mm")
        canvas.text((cx, ring_cy + 24), gauge["label"], 11, theme.muted, anchor="mm")

        detail_size = canvas.fit_size(gauge["detail"], col_w - 28, 12.5, 9)
        canvas.text((cx, gauge_top + 196), gauge["detail"], detail_size, theme.text, anchor="mm")
        if gauge["sub"]:
            canvas.text((cx, gauge_top + 216), gauge["sub"], 9.5, theme.faint, anchor="mm")

    # ---------------- 网络 / 磁盘速率 ---------------- #
    rate_top = gauge_top + GAUGE_H + GAP
    canvas.round_rect(
        MARGIN, rate_top, W - MARGIN, rate_top + RATE_H, RADIUS,
        fill=theme.card, outline=theme.stroke,
    )

    stats = [
        ("下行", fmt_rate(net.get("down_rate", 0)), theme.accent),
        ("上行", fmt_rate(net.get("up_rate", 0)), theme.ok),
        ("磁盘读", fmt_rate(disk_io.get("read_rate", 0)), theme.warn),
        ("磁盘写", fmt_rate(disk_io.get("write_rate", 0)), theme.danger),
    ]
    cell_w = inner_w / len(stats)
    for idx, (label, value, color) in enumerate(stats):
        cx = inner_x + cell_w * idx + cell_w / 2
        if idx > 0:
            canvas.line(
                [(inner_x + cell_w * idx, rate_top + 28), (inner_x + cell_w * idx, rate_top + RATE_H - 26)],
                theme.stroke,
                1,
            )
        value_size = canvas.fit_size(value, cell_w - 14, 15.5, 10, bold=True)
        canvas.text((cx, rate_top + 40), value, value_size, color, bold=True, anchor="mm")
        canvas.text((cx, rate_top + 62), label, 10, theme.muted, anchor="mm")

    # ---------------- 磁盘分区 ---------------- #
    disk_top = rate_top + RATE_H + GAP
    if show_disks:
        canvas.round_rect(
            MARGIN, disk_top, W - MARGIN, disk_top + disk_h, RADIUS,
            fill=theme.card, outline=theme.stroke,
        )
        canvas.text((inner_x, disk_top + CARD_PAD + 9), "磁盘分区", 13, theme.text, bold=True)
        canvas.text(
            (inner_x + inner_w, disk_top + CARD_PAD + 9),
            f"{len(disks)} 个挂载点",
            10.5,
            theme.muted,
            anchor="rm",
        )

        row_y = disk_top + CARD_PAD + 30
        for disk in disks[:disk_rows]:
            mount = str(disk.get("mountpoint", "-"))
            fstype = str(disk.get("fstype", "-"))
            percent = float(disk.get("percent", 0) or 0)
            color = _level_color(percent, theme)

            canvas.text((inner_x, row_y), mount, 12.5, theme.text, bold=True)
            mount_w = canvas.measure(mount, 12.5, True)
            canvas.text((inner_x + mount_w + 7, row_y + 1), fstype, 9.5, theme.faint)

            canvas.text((inner_x + inner_w, row_y), f"{percent:.0f}%", 11, color, bold=True, anchor="rm")
            canvas.text(
                (inner_x + inner_w - 42, row_y),
                f"{fmt_bytes(disk.get('used', 0))} / {fmt_bytes(disk.get('total', 0))}",
                10.5,
                theme.muted,
                anchor="rm",
            )

            canvas.bar(inner_x, row_y + 18, inner_w, 7, percent / 100.0, color)
            row_y += 46

    # ---------------- 进程 TOP ---------------- #
    if show_processes:
        proc_top = disk_top + (disk_h + GAP if show_disks else 0)
        canvas.round_rect(
            MARGIN, proc_top, W - MARGIN, proc_top + proc_h, RADIUS,
            fill=theme.card, outline=theme.stroke,
        )
        canvas.text((inner_x, proc_top + CARD_PAD + 9), "进程占用 TOP", 13, theme.text, bold=True)

        meta = data.get("processes_meta") or {}
        note = "按 CPU 排序"
        if meta.get("age") is not None:
            note += f" · {int(meta['age'])}s 前"
        canvas.text((inner_x + inner_w, proc_top + CARD_PAD + 9), note, 10, theme.muted, anchor="rm")

        head_y = proc_top + CARD_PAD + 36
        name_w = inner_w - 150
        canvas.text((inner_x, head_y), "进程", 10, theme.faint)
        canvas.text((inner_x + inner_w - 96, head_y), "CPU", 10, theme.faint, anchor="rm")
        canvas.text((inner_x + inner_w, head_y), "内存", 10, theme.faint, anchor="rm")

        row_y = head_y + 26
        for proc in processes:
            name = _truncate(canvas, str(proc.get("name", "-")), name_w, 11.5)
            canvas.text((inner_x, row_y), name, 11.5, theme.text)

            cpu_value = float(proc.get("cpu", 0) or 0)
            cpu_color = theme.accent if cpu_value >= 1 else theme.muted
            canvas.text((inner_x + inner_w - 96, row_y), f"{cpu_value:.1f}%", 11, cpu_color, anchor="rm")
            canvas.text(
                (inner_x + inner_w, row_y),
                fmt_bytes(proc.get("rss", 0)),
                11,
                theme.muted,
                anchor="rm",
            )
            row_y += 32

    # ---------------- Footer ---------------- #
    foot_y = total_h - MARGIN - FOOTER_H
    parts = []
    if bot.get("version"):
        parts.append(f"AstrBot {bot['version']}")
    if bot.get("plugins") is not None:
        parts.append(f"{bot['plugins']} 插件")
    footer_text = str(config.get("footer_text", "") or "")
    if footer_text:
        parts.append(footer_text)

    if parts:
        canvas.text((MARGIN, foot_y + 10), "  ·  ".join(parts), 9.5, theme.faint)
    canvas.text(
        (W - MARGIN, foot_y + 10),
        f"耗时 {data.get('elapsed_ms', 0):.0f} ms",
        9.5,
        theme.faint,
        anchor="rm",
    )

    # ---------------- 缩回目标尺寸 ---------------- #
    target_w = int(config.get("image_width", W) or W)
    target_w = max(480, min(target_w, 1080))
    target_h = int(round(total_h * target_w / W))
    return img.resize((target_w, target_h), Image.LANCZOS)


def render_bytes(data: dict, config: dict | None = None, fmt: str = "PNG") -> bytes:
    image = render(data, config)
    buffer = io.BytesIO()
    if fmt.upper() == "JPEG":
        image.convert("RGB").save(buffer, format="JPEG", quality=92, optimize=True)
    else:
        image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
