"""
系统信息采集模块。

只依赖 psutil + 标准库，负责把"机器现在的样子"整理成一个扁平的 dict，
交给 renderer 绘图。所有字段都做了兜底，任何一项采集失败都不会中断整体流程
（这在容器 / 精简版 Linux 上很重要，很多 psutil 接口会抛 NotImplementedError）。
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import time
from datetime import datetime

try:  # psutil 是硬依赖，但缺了也不应该在 import 期就炸掉插件
    import psutil
except Exception:  # pragma: no cover
    psutil = None


# 这些是内核伪文件系统 / 容器挂载，显示出来只会干扰阅读
_SKIP_FSTYPES = {
    "tmpfs",
    "devtmpfs",
    "devfs",
    "devpts",
    "squashfs",
    "overlay",
    "aufs",
    "proc",
    "sysfs",
    "cgroup",
    "cgroup2",
    "ramfs",
    "rootfs",
    "autofs",
    "binfmt_misc",
    "nsfs",
    "tracefs",
    "debugfs",
    "securityfs",
    "pstore",
    "mqueue",
    "hugetlbfs",
    "configfs",
    "fusectl",
    "fuse.gvfsd-fuse",
    "fuse.portal",
    "none",
}


def _safe(func, default=None):
    """执行一个可能抛异常的采集动作。"""
    try:
        return func()
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# 静态信息（进程生命周期内基本不变，采集一次后缓存）
# --------------------------------------------------------------------------- #

_STATIC_CACHE: dict = {}


def _cpu_model() -> str:
    system = platform.system()
    try:
        if system == "Windows":
            import winreg  # noqa: PLC0415  Windows 专用

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
                return " ".join(str(name).split())
        if system == "Linux":
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    low = line.lower()
                    if low.startswith("model name") or low.startswith("hardware"):
                        value = line.split(":", 1)[1].strip()
                        if value:
                            return value
        if system == "Darwin":
            out = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                timeout=3,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            if out.strip():
                return out.strip()
    except Exception:
        pass
    return platform.processor() or platform.machine() or "未知处理器"


def _os_name() -> str:
    system = platform.system()
    if system == "Windows":
        version = platform.version()
        build = 0
        try:
            build = int(version.split(".")[-1])
        except Exception:
            pass
        if build >= 22000:
            return "Windows 11"
        if build >= 10240:
            return "Windows 10"
        return f"Windows {platform.release()}"
    if system == "Linux":
        pretty = _safe(lambda: _read_os_release(), None)
        if pretty:
            return pretty
        return f"Linux {platform.release()}"
    if system == "Darwin":
        ver = platform.mac_ver()[0]
        return f"macOS {ver}" if ver else "macOS"
    return f"{system} {platform.release()}".strip()


def _read_os_release() -> str:
    with open("/etc/os-release", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


def _kernel_version() -> str:
    system = platform.system()
    if system == "Windows":
        return platform.version() or platform.release()
    return platform.release() or system


_HIDDEN_PROCESSES = {"system idle process", "idle", "system", "kernel"}


def _static_info() -> dict:
    if _STATIC_CACHE:
        return _STATIC_CACHE

    model = _cpu_model()
    phys = _safe(lambda: psutil.cpu_count(logical=False), None) if psutil else None
    logical = _safe(lambda: psutil.cpu_count(logical=True), None) if psutil else None

    info = {
        "hostname": _safe(socket.gethostname, "unknown"),
        "os_name": _os_name(),
        "kernel": _kernel_version(),
        "arch": platform.machine() or "unknown",
        "python": platform.python_version(),
        "cpu_model": model,
        "cpu_phys": phys or logical or 0,
        "cpu_logical": logical or phys or 0,
        "boot_time": _safe(lambda: psutil.boot_time(), None) if psutil else None,
    }
    _STATIC_CACHE.update(info)
    return _STATIC_CACHE


# --------------------------------------------------------------------------- #
# 动态信息
# --------------------------------------------------------------------------- #


def _cpu_total_busy(times) -> tuple:
    """把一次 cpu_times 换算成 (总时长, 忙碌时长)，量纲是秒。"""
    total = sum(times)
    # Linux 的 guest / guest_nice 已经计入 user / nice，重复相加会算多
    total -= getattr(times, "guest", 0) or 0
    total -= getattr(times, "guest_nice", 0) or 0
    idle = (getattr(times, "idle", 0) or 0) + max(getattr(times, "iowait", 0) or 0, 0)
    return total, max(total - idle, 0.0)


def _cpu_snapshot():
    """返回 (总体 (总时长, 忙碌时长), 每核 [(总时长, 忙碌时长), ...])。

    这里刻意不用 psutil.cpu_percent(interval=None)：它依赖 psutil 内部的
    全局时间戳缓存，只要同一进程里别的代码（例如 AstrBot 自带的监控面板）
    在采样窗口内也调了一次 cpu_percent，基准点就会被重置，我们实际测到的
    只是最后那一小段，于是稳定读出 0%。cpu_times() 是无状态的，随便谁调用
    都不影响我们的差值计算。
    """
    if not psutil:
        return None, []
    overall = _safe(psutil.cpu_times, None)
    per_core = _safe(lambda: psutil.cpu_times(percpu=True), None) or []
    if not overall:
        return None, []
    try:
        return _cpu_total_busy(overall), [_cpu_total_busy(t) for t in per_core]
    except Exception:
        return None, []


def _usage_between(before, after) -> float:
    """两次采样之间的占用百分比。"""
    if not before or not after:
        return 0.0
    total_delta = after[0] - before[0]
    # 采样窗口太短（<10ms）时差值可能为 0，直接返回 0 而不是除零
    if total_delta <= 1e-3:
        return 0.0
    busy_delta = max(after[1] - before[1], 0.0)
    return max(min(busy_delta / total_delta * 100.0, 100.0), 0.0)


def _cpu_freq() -> dict:
    """当前频率与标称最高频率（单位 MHz）。

    current 是*pstate 当前档位*，不是占用率：现代 CPU 空闲时会主动降频，
    所以「0% 占用 + 1.5GHz」是正常的待机状态，同时给出 max 才能看出这一点。
    """
    freq = _safe(lambda: psutil.cpu_freq(), None)
    if not freq:
        return {"current": 0.0, "max": 0.0}
    current = float(getattr(freq, "current", 0) or 0)
    maximum = float(getattr(freq, "max", 0) or 0)
    # Windows 上偶尔 max 会返回 0 或小于 current，这种情况下忽略它
    if maximum <= current * 1.02:
        maximum = 0.0
    return {"current": current, "max": maximum}


def _load_avg():
    try:
        return tuple(round(v, 2) for v in os.getloadavg())
    except (AttributeError, OSError):
        return None


def _disks() -> list:
    if not psutil:
        return []
    result = []
    seen = set()
    for part in _safe(psutil.disk_partitions, []) or []:
        fstype = (part.fstype or "").lower()
        if fstype in _SKIP_FSTYPES or not part.mountpoint:
            continue
        if part.mountpoint in seen:
            continue
        usage = _safe(lambda: psutil.disk_usage(part.mountpoint), None)
        if not usage or usage.total <= 0:
            continue
        seen.add(part.mountpoint)
        result.append(
            {
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype or "-",
                "total": usage.total,
                "used": usage.used,
                "percent": usage.percent,
            }
        )
    result.sort(key=lambda d: d["total"], reverse=True)
    return result


def _snapshot_processes() -> dict:
    """遍历一次进程表，返回 {pid: (name, cpu_times, rss)}。

    Windows 上每次遍历都要逐个打开进程句柄（几百个进程约 1~2 秒），
    所以这个函数只应出现在后台缓存刷新里，不要放进用户查询的热路径。
    """
    if not psutil:
        return {}
    snap = {}
    for proc in psutil.process_iter(["name", "cpu_times", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name") or f"pid-{proc.pid}"
            cpu_times = info.get("cpu_times")
            total = (cpu_times.user + cpu_times.system) if cpu_times else 0.0
            mem = info.get("memory_info")
            snap[proc.pid] = (name, float(total), int(mem.rss) if mem else 0)
        except Exception:
            continue
    return snap


def collect_processes(top_n: int = 5, sample_interval: float = 1.0) -> list:
    """采集进程 TOP 列表（独立于 collect，供后台定时缓存调用）。

    通过两次进程快照的 cpu_times 差值计算占用，比逐进程 cpu_percent 更快也更稳。
    """
    if not psutil or top_n <= 0:
        return []
    first = _snapshot_processes()
    if sample_interval > 0:
        time.sleep(sample_interval)
    second = _snapshot_processes()

    elapsed = max(sample_interval, 1e-6)
    logical = max(psutil.cpu_count(logical=True) or 1, 1)
    total_mem = _safe(lambda: psutil.virtual_memory().total, 0) or 0
    rows = []
    for pid, (name, cpu2, rss) in second.items():
        if name.lower() in _HIDDEN_PROCESSES:
            continue
        prev = first.get(pid)
        if not prev:
            continue
        cpu = (cpu2 - prev[1]) / elapsed / logical * 100.0
        rows.append(
            {
                "pid": pid,
                "name": name,
                "cpu": max(cpu, 0.0),
                "rss": rss,
                "mem": (rss / total_mem * 100.0) if total_mem else 0.0,
            }
        )
    rows.sort(key=lambda r: (r["cpu"], r["rss"]), reverse=True)
    return rows[:top_n]


def pick_primary_disk(disks: list) -> dict | None:
    """优先返回系统盘：Linux 的 / 或 Windows 的 C:\\。"""
    for disk in disks:
        if disk.get("mountpoint") in ("/", "C:\\", "c:\\"):
            return disk
    return disks[0] if disks else None


def _net_counters():
    if not psutil:
        return None
    return _safe(psutil.net_io_counters, None)


def _disk_io_counters():
    if not psutil:
        return None
    return _safe(psutil.disk_io_counters, None)


def _temperatures() -> list:
    if not psutil:
        return []
    # Windows 版本的 psutil 根本没有这个属性，必须用 getattr 取
    reader = getattr(psutil, "sensors_temperatures", None)
    if not reader:
        return []
    sensors = _safe(reader, None)
    if not sensors:
        return []
    picked = []
    for _chip, entries in sensors.items():
        for entry in entries:
            current = getattr(entry, "current", None)
            if current is None:
                continue
            label = (getattr(entry, "label", "") or _chip or "").strip()
            picked.append({"label": label or "温度", "current": float(current)})
    picked.sort(key=lambda t: t["current"], reverse=True)
    return picked[:3]


def _battery():
    if not psutil:
        return None
    reader = getattr(psutil, "sensors_battery", None)
    if not reader:
        return None
    info = _safe(reader, None)
    if not info:
        return None
    return {
        "percent": round(float(info.percent), 1),
        "plugged": bool(getattr(info, "power_plugged", False)),
    }


def _process_count() -> int:
    if not psutil:
        return 0
    pids = _safe(psutil.pids, []) or []
    return len(pids)


def collect(sample_interval: float = 0.5, with_processes: bool = False, top_n: int = 5) -> dict:
    """采集一次快照。

    为了拿到"当前瞬时"的 CPU 与网络/磁盘速率，这里必须做两次采样取差值。
    sample_interval 是两次采样之间的间隔，0.5s 足够稳定，又不会让用户等太久。
    调用方应当用 asyncio.to_thread 包一层，避免阻塞事件循环。

    进程 TOP 列表不在本函数内采集（Windows 上遍历进程表很慢），
    请使用 collect_processes 配合后台定时缓存。
    """
    now = time.time()

    if not psutil:
        return {
            "ok": False,
            "error": "未安装 psutil 依赖",
            "static": _static_info(),
            "timestamp": now,
        }

    # --- 第一次采样 ---
    cpu0, cpu0_core = _cpu_snapshot()
    net0 = _net_counters()
    dio0 = _disk_io_counters()
    t0 = time.monotonic()

    if sample_interval > 0:
        time.sleep(sample_interval)

    # --- 第二次采样 ---
    cpu1, cpu1_core = _cpu_snapshot()
    net1 = _net_counters()
    dio1 = _disk_io_counters()
    t1 = time.monotonic()

    elapsed = max(t1 - t0, 1e-6)

    mem = _safe(psutil.virtual_memory, None)
    swap = _safe(psutil.swap_memory, None)

    net_up = net_down = 0.0
    net_sent = net_recv = 0
    if net0 and net1:
        net_sent = net1.bytes_sent
        net_recv = net1.bytes_recv
        sent_delta = max(net1.bytes_sent - net0.bytes_sent, 0)
        recv_delta = max(net1.bytes_recv - net0.bytes_recv, 0)
        net_up = sent_delta / elapsed
        net_down = recv_delta / elapsed

    disk_read = disk_write = 0.0
    disk_read_total = disk_write_total = 0
    if dio0 and dio1:
        disk_read_total = getattr(dio1, "read_bytes", 0)
        disk_write_total = getattr(dio1, "write_bytes", 0)
        disk_read = max(dio1.read_bytes - dio0.read_bytes, 0) / elapsed
        disk_write = max(dio1.write_bytes - dio0.write_bytes, 0) / elapsed

    static = _static_info()
    boot_time = static.get("boot_time")
    uptime = max(now - boot_time, 0) if boot_time else 0

    cpu_percent = _usage_between(cpu0, cpu1)
    per_core = [
        _usage_between(before, after)
        for before, after in zip(cpu0_core, cpu1_core)
    ]
    freq = _cpu_freq()

    return {
        "ok": True,
        "timestamp": now,
        "static": static,
        "uptime": uptime,
        "load_avg": _load_avg(),
        "cpu": {
            "percent": round(cpu_percent, 1),
            "per_core": [round(v, 1) for v in per_core],
            "freq": freq["current"],
            "freq_max": freq["max"],
        },
        "memory": {
            "total": getattr(mem, "total", 0),
            "used": getattr(mem, "used", 0),
            "available": getattr(mem, "available", 0),
            "percent": float(getattr(mem, "percent", 0.0)),
        },
        "swap": {
            "total": getattr(swap, "total", 0),
            "used": getattr(swap, "used", 0),
            "percent": float(getattr(swap, "percent", 0.0)),
        },
        "disks": _disks(),
        "disk_io": {
            "read_rate": disk_read,
            "write_rate": disk_write,
            "read_total": disk_read_total,
            "write_total": disk_write_total,
        },
        "net": {
            "up_rate": net_up,
            "down_rate": net_down,
            "sent": net_sent,
            "recv": net_recv,
        },
        "process_count": _process_count(),
        "temperatures": _temperatures(),
        "battery": _battery(),
        "processes": [],
    }


# --------------------------------------------------------------------------- #
# 格式化工具（renderer 与纯文本模式共用）
# --------------------------------------------------------------------------- #


def fmt_bytes(value, suffix: str = "") -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if num <= 0:
        # "0 B/s" 读起来很怪，统一显示成 "0 KB/s"
        return f"0 KB{suffix}".strip()
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while num >= 1024 and idx < len(units) - 1:
        num /= 1024.0
        idx += 1
    if idx == 0:
        text = f"{int(num)} {units[idx]}"
    elif num >= 100:
        text = f"{num:.0f} {units[idx]}"
    else:
        text = f"{num:.1f} {units[idx]}"
    return f"{text}{suffix}"


def fmt_rate(value) -> str:
    return fmt_bytes(value, "/s")


def fmt_uptime(seconds) -> str:
    try:
        seconds = int(max(seconds, 0))
    except (TypeError, ValueError):
        return "-"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours > 0:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def fmt_clock(ts) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def build_text_report(data: dict) -> str:
    """纯文本兜底（用户关闭图片 / 渲染失败时使用）。"""
    if not data.get("ok"):
        return f"状态采集失败：{data.get('error', '未知错误')}"

    static = data["static"]
    cpu = data["cpu"]
    mem = data["memory"]
    net = data["net"]
    freq = cpu.get("freq") or 0
    freq_text = f"  {freq / 1000:.2f}GHz" if freq else ""
    if cpu.get("freq_max"):
        freq_text += f" / {cpu['freq_max'] / 1000:.2f}GHz"
    lines = [
        f"【{static['hostname']}】{static['os_name']}",
        f"处理器：{static['cpu_model']}",
        f"核心：{static['cpu_phys']} 物理 / {static['cpu_logical']} 逻辑",
        f"CPU：{cpu['percent']:.1f}%{freq_text}",
        f"内存：{fmt_bytes(mem['used'])} / {fmt_bytes(mem['total'])} ({mem['percent']:.1f}%)",
        f"运行时长：{fmt_uptime(data['uptime'])}",
        f"网络：↓ {fmt_rate(net['down_rate'])}  ↑ {fmt_rate(net['up_rate'])}",
        f"进程数：{data['process_count']}",
    ]
    for disk in data.get("disks", []):
        lines.append(
            f"磁盘 {disk['mountpoint']}：{fmt_bytes(disk['used'])} / "
            f"{fmt_bytes(disk['total'])} ({disk['percent']:.1f}%)"
        )
    return "\n".join(lines)
