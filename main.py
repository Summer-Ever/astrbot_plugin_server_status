"""
AstrBot 服务器状态监控插件。

在聊天会话中发送 `/status`（或中文别名 `/状态`、`/服务器状态` 等），
机器人会采集本机实时指标，渲染成一张状态卡片图片发回会话。

架构说明：
  * status_collector.py  —— psutil 数据采集 + 文本报告
  * status_renderer.py   —— Pillow 卡片渲染（无浏览器依赖）
  * main.py              —— 指令注册、权限/冷却、后台进程缓存

为什么进程 TOP 用后台缓存？
  Windows 上遍历一次进程表要 1~2 秒（每个进程都要打开句柄），
  如果放在用户查询的热路径里，一次查询要等 5 秒以上。
  所以这里用一个后台任务每隔 process_refresh_interval 秒刷新一次缓存，
  查询时直接读缓存，主路径只做 0.4s 的系统采样，秒回。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# AstrBot 以文件方式加载 main.py，同目录模块不在默认搜索路径里。
# 这里把插件目录插到最前面，保证 status_collector / status_renderer 可导入。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import status_collector as collector  # noqa: E402
import status_renderer as renderer  # noqa: E402

from astrbot.api import AstrBotConfig, logger  # noqa: E402
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter  # noqa: E402
from astrbot.api.star import Context, Star  # noqa: E402

try:  # v3.5+ 提供插件数据目录工具
    from astrbot.api.star import StarTools

    def _data_dir() -> str:
        return StarTools.get_data_dir("astrbot_plugin_server_status")

except Exception:

    def _data_dir() -> str:
        path = os.path.join("data", "plugin_data", "astrbot_plugin_server_status")
        os.makedirs(path, exist_ok=True)
        return path


class ServerStatusPlugin(Star):
    """服务器状态监控插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._proc_cache: dict = {}
        self._proc_task: asyncio.Task | None = None
        self._proc_stop = asyncio.Event()
        self._cooldowns: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self):
        """插件加载完成后的异步初始化：启动进程缓存后台任务。"""
        self._proc_stop.clear()
        self._proc_task = asyncio.create_task(self._proc_cache_loop())
        logger.info("[server_status] 服务器状态插件已加载，发送 /status 或 /状态 查询")

    async def terminate(self):
        """插件卸载 / 停用时清理后台任务。"""
        self._proc_stop.set()
        if self._proc_task:
            self._proc_task.cancel()
            try:
                await self._proc_task
            except (asyncio.CancelledError, Exception):
                pass
            self._proc_task = None
        logger.info("[server_status] 服务器状态插件已卸载")

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #

    @filter.command(
        "status",
        alias={
            "状态",
            "服务器状态",
            "状态查询",
            "系统状态",
            "运行状态",
            "主机状态",
            "服务器监控",
            "sysinfo",
        },
    )
    async def server_status(self, event: AstrMessageEvent):
        """查询服务器的实时状态（CPU / 内存 / 磁盘 / 网络），以图片形式返回"""
        # --- 权限 ---
        if bool(self.config.get("admin_only", False)):
            role = str(getattr(event, "role", "member") or "member").lower()
            if role not in ("admin", "owner"):
                yield event.plain_result("该指令仅限管理员使用。")
                return

        # --- 冷却 ---
        cooldown = float(self.config.get("cooldown_seconds", 5) or 0)
        key = event.unified_msg_origin
        now = time.monotonic()
        last = self._cooldowns.get(key)
        if cooldown > 0 and last is not None and now - last < cooldown:
            yield event.plain_result("查询太频繁啦，请稍后再试～")
            return
        self._cooldowns[key] = now

        # --- 采集 ---
        sample_interval = float(self.config.get("sample_interval", 0.5) or 0.5)
        try:
            data = await asyncio.to_thread(
                collector.collect, sample_interval, False, 0
            )
        except Exception as exc:
            logger.error(f"[server_status] 采集失败: {exc}")
            yield event.plain_result(f"状态采集失败：{exc}")
            return
        data["elapsed_ms"] = (
            time.time() - data.get("timestamp", time.time())
        ) * 1000

        # Bot 自身信息（footer 用）
        data["bot"] = self._bot_info()

        # --- 进程缓存 ---
        if bool(self.config.get("show_processes", True)):
            top_n = int(self.config.get("process_top_n", 5) or 5)
            rows = self._proc_cache.get("rows") or []
            stamp = self._proc_cache.get("time")
            data["processes"] = rows[:top_n]
            data["processes_meta"] = {
                "age": (time.time() - stamp) if stamp else None,
            }

        # --- 输出 ---
        if str(self.config.get("render_mode", "image")).lower() == "text":
            yield event.plain_result(collector.build_text_report(data))
            return

        try:
            config = {
                "theme": self.config.get("theme", "dark"),
                "accent_color": self.config.get("accent_color", ""),
                "show_processes": bool(self.config.get("show_processes", True)),
                "show_disk_partitions": bool(
                    self.config.get("show_disk_partitions", True)
                ),
                "max_disk_partitions": self.config.get("max_disk_partitions", 4),
                "image_width": self.config.get("image_width", 720),
                "render_scale": self.config.get("render_scale", 2),
                "font_path": self.config.get("font_path", ""),
                "footer_text": self.config.get("footer_text", ""),
            }
            png_bytes = await asyncio.to_thread(
                self._render_to_bytes, data, config
            )
        except Exception as exc:
            logger.error(f"[server_status] 渲染失败，回落为文本: {exc}")
            yield event.plain_result(collector.build_text_report(data))
            return

        yield self._image_result(event, png_bytes)

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    def _bot_info(self) -> dict:
        info = {"version": None, "plugins": None, "platforms": None, "memory": None}
        try:
            info["version"] = getattr(self.context, "version", None)
            registered = getattr(self.context, "get_all_stars", None)
            if callable(registered):
                info["plugins"] = len(registered() or [])
            platforms = getattr(self.context, "platform_manager", None)
            insts = getattr(platforms, "get_insts", None)
            if callable(insts):
                info["platforms"] = len(insts() or [])
            import psutil  # noqa: PLC0415

            proc = psutil.Process()
            info["memory"] = proc.memory_info().rss
        except Exception:
            pass
        return info

    @staticmethod
    def _render_to_bytes(data: dict, config: dict) -> bytes:
        image = renderer.render(data, config)
        import io  # noqa: PLC0415

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    def _image_result(
        self, event: AstrMessageEvent, png_bytes: bytes
    ) -> MessageEventResult:
        send_mode = str(self.config.get("send_mode", "base64")).lower()

        if send_mode == "file":
            # 某些平台 / 协议端对 base64 支持不佳时，可改为落盘发送文件
            try:
                cache_dir = _data_dir()
                os.makedirs(cache_dir, exist_ok=True)
                path = os.path.join(cache_dir, "status_cache.png")
                with open(path, "wb") as fh:
                    fh.write(png_bytes)
                return event.image_result(path)
            except Exception as exc:
                logger.warning(f"[server_status] 文件落盘失败，改用 base64: {exc}")

        import astrbot.api.message_components as Comp  # noqa: PLC0415

        return event.chain_result([Comp.Image.fromBytes(png_bytes)])

    # ------------------------------------------------------------------ #
    # 后台任务
    # ------------------------------------------------------------------ #

    async def _proc_cache_loop(self):
        interval = int(self.config.get("process_refresh_interval", 60) or 60)
        interval = max(interval, 15)
        top_n = int(self.config.get("process_top_n", 5) or 5)

        # 启动后先尽快刷一轮，让首次查询就有数据
        first = True
        while not self._proc_stop.is_set():
            try:
                rows = await asyncio.to_thread(
                    collector.collect_processes, top_n, 1.0
                )
                self._proc_cache = {"rows": rows, "time": time.time()}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[server_status] 进程缓存刷新失败: {exc}")

            wait = 5 if first else interval  # 首轮失败时 5 秒后重试
            first = False
            try:
                await asyncio.wait_for(self._proc_stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue
