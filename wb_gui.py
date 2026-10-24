#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy Monitor — 实时查看 codebuddy (workbuddy) 的思考过程与输出。

数据源: ~/.codebuddy/projects/<项目路径>/<sessionId>.jsonl
用法:  双击 WorkBuddyMonitor.exe；开发时可 python wb_gui.py
自检:  python wb_gui.py --selftest
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

APP_TITLE = "WorkBuddy Monitor"
ICON_FILE = "workbuddy.ico"
ROOT = Path.home() / ".codebuddy" / "projects"


def resource_path(name: str) -> Path:
    """打包后资源解压在 _MEIPASS，开发运行时就在脚本同目录。"""
    base = getattr(sys, "_MEIPASS", None)
    return (Path(base) / name) if base else Path(__file__).resolve().parent / name


# ---------------------------------------------------------------- DPI 适配
# 设计稿按 96 DPI（Windows 100% 缩放）标定，运行到 125%/150% 屏幕时
# 由 UI_SCALE 统一放大，避免整个窗口被系统位图拉伸导致文字发虚。
UI_SCALE = 1.0

DPI_AWARENESS = "unaware"


def px(value: float) -> int:
    """设计像素 → 当前屏幕物理像素。0 保持 0（分隔线、内边距清零等）。"""
    if not value:
        return 0
    return max(1, int(round(value * UI_SCALE)))


def enable_dpi_awareness() -> str:
    """在创建任何窗口之前声明 DPI 感知级别。

    Windows 一旦为进程创建了窗口就锁定感知级别，之后再调用只会失败，
    所以这里必须在 Tk() 之前执行。打包后 bootloader 可能抢先设置过，
    因此 exe 侧同时用 manifest 声明 PerMonitorV2 作为主路径。
    """
    global DPI_AWARENESS
    if sys.platform != "win32":
        DPI_AWARENESS = "n/a"
        return DPI_AWARENESS
    user32 = ctypes.windll.user32
    try:  # Windows 10 1703+：PerMonitorV2，多屏不同缩放下也不模糊
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            DPI_AWARENESS = "per-monitor-v2"
            return DPI_AWARENESS
    except Exception:
        pass
    try:  # Windows 8.1+
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            DPI_AWARENESS = "per-monitor"
            return DPI_AWARENESS
    except Exception:
        pass
    try:  # Vista+
        if user32.SetProcessDPIAware():
            DPI_AWARENESS = "system"
            return DPI_AWARENESS
    except Exception:
        pass
    return DPI_AWARENESS


def system_dpi() -> float:
    """直接向 GDI 取屏幕 DPI。

    不依赖 Tk 的初始化状态：打包成 --noconsole 的 exe 后，Tk 偶尔按 96 DPI
    起算（winfo_fpixels 恒为 96），但 GDI 仍会返回该显示器真实的 120/144。
    DPI 未感知的进程这里会被系统虚拟化成 96，因此不会造成重复放大。
    """
    if sys.platform != "win32":
        return 96.0
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        if not hdc:
            return 96.0
        try:
            dpi = float(gdi32.GetDeviceCaps(hdc, 88) or 96)  # LOGPIXELSX
        finally:
            user32.ReleaseDC(0, hdc)
        return dpi if dpi > 0 else 96.0
    except Exception:
        return 96.0


def set_physical_size(root: tk.Tk, width: int, height: int) -> bool:
    """按物理像素设定客户区大小，成功返回 True。

    Tk 的 wm geometry 在「进程由 manifest 声明为 PerMonitorV2」时会把参数
    再按缩放折算一次（传 1476 得到 1180），而在「先建进程再用 API 提升」
    时不折算。两条路径行为不一致，因此这里直接调 SetWindowPos，并用
    GetWindowRect/GetClientRect 的差值补上边框与标题栏；窗口尚未映射时
    返回 False，由调用方稍后重试。
    """
    if sys.platform != "win32":
        try:
            root.geometry(f"{width}x{height}")
            return True
        except tk.TclError:
            return False
    try:
        root.update_idletasks()
        hwnd = int(root.wm_frame(), 16)
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        wrect, crect = wt.RECT(), wt.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(crect)):
            return False
        if crect.right <= 0 or crect.bottom <= 0:
            return False
        user32.GetWindowRect(hwnd, ctypes.byref(wrect))
        extra_w = (wrect.right - wrect.left) - crect.right
        extra_h = (wrect.bottom - wrect.top) - crect.bottom
        SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010
        user32.SetWindowPos(
            hwnd, 0, 0, 0, width + extra_w, height + extra_h,
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
        user32.GetClientRect(hwnd, ctypes.byref(crect))
        return abs(crect.right - width) <= 2 and abs(crect.bottom - height) <= 2
    except Exception:
        return False


def apply_ui_scale(root: tk.Misc) -> float:
    """求缩放系数，并把 Tk 的 point→像素换算校正到真实 DPI。

    只有 DPI 感知生效时 winfo_fpixels 才是物理 DPI；未生效时它恒为 96，
    此时两侧都会得到 96，保持 1.0，让系统按原有方式拉伸——宁可模糊，
    也不要把界面放大两次。
    """
    global UI_SCALE
    try:
        tk_dpi = float(root.winfo_fpixels("1i"))
    except tk.TclError:
        tk_dpi = 96.0
    dpi = max(tk_dpi, system_dpi()) if sys.platform == "win32" else tk_dpi
    try:
        # point 字号靠 scaling 换算成像素，必须与真实 DPI 一致，否则字会偏小
        root.tk.call("tk", "scaling", dpi / 72.0)
    except tk.TclError:
        pass
    UI_SCALE = max(1.0, dpi / 96.0)
    return UI_SCALE


def scaled_photo(path: Path, target: int) -> tk.PhotoImage:
    """把 PNG 缩到目标像素尺寸。

    PhotoImage 只支持整数倍的 zoom / subsample，这里搜索最接近目标宽度的
    分数倍组合（如 1.25 倍 = zoom(5).subsample(8)），避免为了 1.25 缩放
    引入 Pillow 依赖。
    """
    img = tk.PhotoImage(file=str(path))
    best_err, best_z, best_s = float("inf"), 1, 1
    for z in range(1, 9):
        for s in range(1, 13):
            err = abs(img.width() * z / s - target)
            if err < best_err:
                best_err, best_z, best_s = err, z, s
    if best_z > 1:
        img = img.zoom(best_z)
    if best_s > 1:
        img = img.subsample(best_s)
    return img

FILE_POLL_SECONDS = 0.25
SESSION_SCAN_SECONDS = 1.0
UI_TICK_MS = 50

INITIAL_REPLAY_EVENTS = 25
AUTO_SWITCH_FULL_READ_LIMIT = 2 * 1024 * 1024
READ_CHUNK_BYTES = 512 * 1024
MAX_JSON_LINE_BYTES = 5 * 1024 * 1024
TAIL_BACK_BYTES = 256 * 1024
MIN_ACTIVE_SESSION_BYTES = 1024  # 更小的会话一般只有快照记录，自动跟随时跳过
SESSION_MENU_LIMIT = 10          # 项目子菜单里列出多少个历史会话

MAX_REASONING_CHARS = 30_000
MAX_CALL_CHARS = 4_000
MAX_RESULT_CHARS = 8_000
MAX_MESSAGE_CHARS = 50_000

MAX_TEXT_CHARS = 2_000_000
TRIM_TEXT_CHARS = 500_000

# 中日韩字符 + 全角标点：这些字符 Consolas 里没有，需要单独指定中文字体
CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff01-\uff60\u3000-\u303f]+")

UPDATE_QUEUE_SIZE = 5000
DISPLAY_BACKLOG_SIZE = 5000
RENDER_BATCH_SIZE = 200

# 切换过渡节奏（毫秒 / 帧数）：淡出 → 换页 → 淡入 → 逐条追平
TRANSITION_OUT_MS = 24
TRANSITION_OUT_STEPS = 5
TRANSITION_IN_MS = 26
TRANSITION_IN_STEPS = 7
TRANSITION_REVEAL_PER_TICK = 2
FADE_FLOOR = 0.04       # 不完全淡到看不见，留一点残影，避免“黑屏一刀”

BG = "#0e1014"          # 主区域底色
PANEL_BG = "#0e1014"
SIDEBAR_BG = "#0a0c10"  # 侧栏
HEADER_BG = "#12151a"   # 顶栏 / 底栏
LINE = "#1c2129"        # 分隔线
FG = "#e6e8eb"
MUTED = "#8b929c"

# 终端高亮配色（对标 PowerShell 黑底亮白字）
COLORS = {
    "timestamp": "#7f7f7f",
    "task": "#e5c07b",
    "task_body": "#e8e2cf",
    "think_label": "#c678dd",
    "think": "#b9b9b9",
    "call": "#56b6c2",
    "call_body": "#d0d0d0",
    "result_ok": "#7ee787",
    "result_error": "#ff7b72",
    "result_body": "#c9c9c9",
    "result_err_body": "#eab3ae",
    "reply": "#ededed",
    "reply_label": "#5ac8fa",
    "md_head": "#79c0ff",
    "md_code_fg": "#cecece",
    "md_code_bg": "#161616",
    "dim": "#707070",
    "system": "#61afef",
    "warning": "#d19a66",
}

LABELS = {
    "task": "任务",
    "think": "思考",
    "call": "调用",
    "result_ok": "完成",
    "result_error": "失败",
    "reply": "回复",
    "system": "系统",
    "warning": "警告",
}

# 时间轴左侧角色符号 + 哪些类型用卡片版式
GUTTER = {
    "task": "▌", "think": "▌", "call": "▸", "result_ok": "✔",
    "result_error": "✖", "reply": "◆", "system": "·", "warning": "!",
}
BLOCK_TAGS = {"task": "blk_task", "reply": "blk_reply"}


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StreamEvent:
    kind: str
    text: str
    timestamp_ms: int | None = None
    label: str = ""  # 标签覆盖，如「读取」「执行」；空则用 LABELS


@dataclass(frozen=True, slots=True)
class UiUpdate:
    operation: str  # event | status | session | error
    event: StreamEvent | None = None
    text: str = ""
    path: str = ""      # 会话切换时带上路径，前端据此高亮侧栏对应条目
    agent: str = ""     # 来源 id
    project: str = ""   # 项目键


@dataclass(frozen=True, slots=True)
class ControlCommand:
    operation: str
    value: str = ""
    label: str = ""  # 给用户看的名字（如 PowerSystem），value 是实际过滤关键字


@dataclass(slots=True)
class TailState:
    path: Path
    position: int = 0
    partial: bytes = b""
    discard_oversized: bool = False


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def _now_ms() -> int:
    return int(time.time() * 1000)


def clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return text[:limit].rstrip() + f"\n\n[内容过长，已截断 {cut} 个字符]"


def mix_color(color: str, toward: str, alpha: float) -> str:
    """把 color 按 alpha 混向 toward（1.0 = 原色，0.0 = 完全是 toward）。"""
    try:
        r1, g1, b1 = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        r2, g2, b2 = int(toward[1:3], 16), int(toward[3:5], 16), int(toward[5:7], 16)
    except (ValueError, IndexError):
        return color
    a = max(0.0, min(1.0, alpha))
    return "#%02x%02x%02x" % (
        int(r2 + (r1 - r2) * a),
        int(g2 + (g1 - g2) * a),
        int(b2 + (b1 - b2) * a),
    )


def extract_text_blocks(blocks: object) -> str:
    """从 content / rawContent 数组中提取 text 字段，保留换行。"""
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            value = block.get("text")
            if isinstance(value, str) and value.strip():
                parts.append(value)
    return "\n".join(parts)


def format_arguments(arguments: object) -> str:
    """工具参数：JSON 串尝试格式化，失败则原样显示。只解析，不执行。"""
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        raw = arguments.strip()
        if not raw:
            return ""
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return clip_text(" ".join(raw.split()), MAX_CALL_CHARS)
        return clip_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            MAX_CALL_CHARS,
        )
    try:
        return clip_text(
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            MAX_CALL_CHARS,
        )
    except (TypeError, ValueError):
        return clip_text(str(arguments), MAX_CALL_CHARS)


# --------------------------------------------------------------------------
# 日志美化：把原始记录提炼成可读文本，而不是原样搬运
# --------------------------------------------------------------------------
PREVIEW_HEAD_LINES = 8    # 长结果头部保留行数
PREVIEW_TAIL_LINES = 4    # 长结果尾部保留行数
RESULT_ALWAYS_LINES = 24  # 结果不超过此行数则全量显示
MAX_REPLY_LINES = 240
MAX_THINK_LINES = 160
BODY_INDENT = "  "        # 结果正文续行缩进

LINE_NO_RE = re.compile(r"^\s*\d+→")  # Read 结果的行号前缀，如 "  23→"
BLANKS_RE = re.compile(r"\n{3,}")

# 工具名 → 中文动词标签
TOOL_LABELS = {
    "Read": "读取", "Write": "写入", "Edit": "编辑", "MultiEdit": "编辑",
    "NotebookEdit": "编辑", "Bash": "执行", "PowerShell": "执行",
    "Grep": "查找", "Glob": "匹配", "Task": "子任务", "Agent": "子任务",
    "WebFetch": "抓网页", "WebSearch": "搜索", "TodoWrite": "计划",
    "TodoRead": "计划", "LS": "列目录",
}


def _parse_args(arguments: object) -> dict:
    """arguments 可能是 JSON 字符串或 dict，统一成 dict。"""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            value = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _short_path(path: object) -> str:
    """绝对路径只保留末尾两段，避免刷屏。"""
    if not isinstance(path, str) or not path.strip():
        return ""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[0] if parts else path


def _first_line(text: object, limit: int = 110) -> str:
    """取第一行并压平空白、截断。"""
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if not stripped:
        return ""
    line = " ".join(stripped.splitlines()[0].split())
    if len(line) > limit:
        line = line[:limit].rstrip() + " …"
    return line


def _cap_plain(lines: list[str], limit: int) -> list[str]:
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"⋯ 后面还有 {len(lines) - limit} 行，已在界面省略 ⋯"]


def prettify_call(name: str, arguments: object) -> tuple[str, str]:
    """工具调用 → (标签, 一行摘要)。"""
    label = TOOL_LABELS.get(name, name)
    args = _parse_args(arguments)
    detail = ""
    for key in ("file_path", "notebook_path", "path"):
        if isinstance(args.get(key), str):
            detail = _short_path(args[key])
            break
    if not detail and isinstance(args.get("command"), str):
        detail = _first_line(args["command"])
    for key in ("pattern", "url", "query", "description", "prompt"):
        if not detail and isinstance(args.get(key), str):
            detail = _first_line(args[key], 90)
    if not detail:
        detail = _first_line(format_arguments(arguments), 90)
    return label, detail


def _strip_line_numbers(lines: list[str]) -> bool:
    """若大部分行带 "  12→" 行号前缀，原地剥掉并返回 True。"""
    if not lines:
        return False
    numbered = sum(1 for ln in lines if LINE_NO_RE.match(ln))
    if numbered < max(3, int(len(lines) * 0.6)):
        return False
    for i, ln in enumerate(lines):
        lines[i] = LINE_NO_RE.sub("", ln, count=1)
    return True


def prettify_result(name: str, status: str, body: str) -> tuple[str, str]:
    """工具结果 → (标签, 多行文本)：摘要头 + 去行号正文 + 长内容折叠。"""
    label = "完成" if status == "completed" else "失败"
    text = body.strip("\n")
    if not text.strip():
        return label, f"{name} · 无输出"

    lines = [ln.rstrip() for ln in text.split("\n")]
    had_numbers = _strip_line_numbers(lines)
    total = len(lines)

    # 连续空行折叠为一个，避免预览配额被空行吃掉
    collapsed: list[str] = []
    for ln in lines:
        if ln.strip() or (collapsed and collapsed[-1].strip()):
            collapsed.append(ln)
    lines = collapsed

    summary_bits = [name]
    if had_numbers:
        summary_bits.append(f"共 {total} 行")

    if len(lines) > RESULT_ALWAYS_LINES:
        omitted = len(lines) - PREVIEW_HEAD_LINES - PREVIEW_TAIL_LINES
        shown = (
            lines[:PREVIEW_HEAD_LINES]
            + [f"⋯ 中间省略 {omitted} 行 ⋯"]
            + lines[-PREVIEW_TAIL_LINES:]
        )
    else:
        shown = lines

    pretty = [" · ".join(summary_bits)]
    pretty.extend(BODY_INDENT + ln for ln in shown)
    return label, "\n".join(pretty)


def prettify_think(text: str) -> str:
    lines = [ln.rstrip() for ln in BLANKS_RE.sub("\n\n", text).strip("\n").split("\n")]
    return "\n".join(_cap_plain(lines, MAX_THINK_LINES))


def prettify_message(text: str) -> str:
    lines = [ln.rstrip() for ln in BLANKS_RE.sub("\n\n", text).strip("\n").split("\n")]
    return "\n".join(_cap_plain(lines, MAX_REPLY_LINES))


def extract_record(record: dict) -> StreamEvent | None:
    """把一条 JSONL 记录转成可显示事件（已经过美化）；无关记录返回 None。"""
    kind = record.get("type")
    ts = record.get("timestamp")
    if not isinstance(ts, int):
        ts = None

    if kind == "reasoning":
        body = extract_text_blocks(record.get("rawContent")) or extract_text_blocks(
            record.get("content")
        )
        body = body.strip()
        if not body:
            return None
        return StreamEvent("think", clip_text(prettify_think(body), MAX_REASONING_CHARS), ts)

    if kind == "function_call":
        name = str(record.get("name") or "?")
        label, detail = prettify_call(name, record.get("arguments"))
        return StreamEvent("call", detail or name, ts, label)

    if kind == "function_call_result":
        name = str(record.get("name") or "?")
        status = str(record.get("status") or "?")
        output = record.get("output")
        if isinstance(output, dict):
            body = output.get("text")
            body = body if isinstance(body, str) else ""
        else:
            body = "" if output is None else str(output)
        label, text = prettify_result(name, status, body)
        return StreamEvent(
            "result_ok" if status == "completed" else "result_error",
            clip_text(text, MAX_RESULT_CHARS),
            ts,
            label,
        )

    if kind == "message":
        role = record.get("role")
        body = extract_text_blocks(record.get("content")).strip()
        if not body:
            return None
        if role == "user":
            return StreamEvent("task", clip_text(prettify_message(body), MAX_MESSAGE_CHARS), ts)
        if role == "assistant":
            return StreamEvent("reply", clip_text(prettify_message(body), MAX_MESSAGE_CHARS), ts)

    return None


def _match_strength(dirname: str, keyword: str) -> int:
    """项目目录与关键字匹配强度：2=精确/尾部匹配，1=包含，0=不匹配。

    编码目录形如 c-Users-alice-Desktop-xxx-myapp（用户名与盘符被拼进名字里），
    用尾部匹配可以避免 "alice" 这类关键字误命中更长的项目名。
    """
    if not keyword:
        return 2
    name = dirname.casefold()
    if name == keyword or name.endswith("-" + keyword):
        return 2
    return 1 if keyword in name else 0


def match_project(display: str, keyword: str) -> int:
    """界面上的项目名与关键字的匹配强度，语义同 _match_strength。

    侧栏筛选与主区域跟随都走这里，避免两处规则不一致：以前侧栏是精确相等，
    输 "workbuddy" 会显示「没有会话」，主区域却已经切到该项目了。
    """
    return _match_strength(display, keyword.strip().casefold())


def find_latest_session(root: Path, keyword: str) -> tuple[Path, int, int] | None:
    """返回 (路径, 修改时间ns, 大小)。

    排序优先级：项目匹配强度 > 会话是否有内容 > 修改时间。
    这样既能精确锁定项目，又不会停在只有快照的空壳会话上。
    """
    kw = keyword.strip().casefold()
    best: tuple[tuple[int, int, int, str], Path, int] | None = None
    try:
        walker = os.walk(root, onerror=lambda _e: None)
        for dirpath, _dirnames, filenames in walker:
            if not filenames:
                continue
            strength = _match_strength(Path(dirpath).name, kw)
            if strength == 0:
                continue
            for name in filenames:
                if not name.lower().endswith(".jsonl"):
                    continue
                path = Path(dirpath) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                active = 1 if stat.st_size >= MIN_ACTIVE_SESSION_BYTES else 0
                key = (strength, active, stat.st_mtime_ns, str(path).casefold())
                if best is None or key > best[0]:
                    best = (key, path, stat.st_size)
    except OSError:
        return None
    if best is None:
        return None
    return best[1], best[0][2], best[2]


def _friendly_dir_name(dirname: str) -> str:
    """编码目录名去掉用户路径前缀，露出项目名。"""
    for marker in ("-Desktop-", "-Documents-", "-Downloads-", "-OneDrive-"):
        if marker in dirname:
            return dirname.split(marker, 1)[1]
    return dirname


# --------------------------------------------------------------------------
# 项目发现（读取会话里的 cwd，得到真实项目路径）
# --------------------------------------------------------------------------
_CWD_CACHE: dict[str, str] = {}


def read_session_cwd(path: Path) -> str:
    """从会话开头若干条记录里取 cwd（真实项目目录），带缓存。"""
    key = str(path)
    cached = _CWD_CACHE.get(key)
    if cached is not None:
        return cached
    cwd = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(40):
                line = fh.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                value = record.get("cwd")
                if isinstance(value, str) and value.strip():
                    cwd = value.strip()
                    break
    except OSError:
        pass
    _CWD_CACHE[key] = cwd
    return cwd


def session_label(path: Path) -> str:
    """给用户看的会话名：项目名 · 会话前 8 位。"""
    cwd = read_session_cwd(path)
    project = Path(cwd).name if cwd else ""
    if not project:
        project = _friendly_dir_name(path.parent.name)
    return f"{project} · {path.stem[:8]}"


def list_projects(root: Path) -> list[tuple[str, str, int, Path]]:
    """扫描项目目录，返回 [(项目名, 过滤关键字, 会话数, 目录)]，按最近活动倒序。"""
    rows: list[tuple[int, str, str, int, Path]] = []
    try:
        subdirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []
    for sub in subdirs:
        try:
            files = list(sub.glob("*.jsonl"))
        except OSError:
            continue
        if not files:
            continue
        try:
            files.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
            mtime = files[0].stat().st_mtime_ns
        except OSError:
            continue
        # 新会话可能只有快照、没有 cwd，往前多试几个
        cwd = ""
        for candidate in files[:3]:
            cwd = read_session_cwd(candidate)
            if cwd:
                break
        display = Path(cwd).name if cwd else _friendly_dir_name(sub.name)
        rows.append((mtime, display, display, len(files), sub))

    rows.sort(key=lambda row: row[0], reverse=True)
    return [(display, keyword, count, path) for _m, display, keyword, count, path in rows]


def format_mtime(mtime: float) -> str:
    """今天只显示时分，其它显示月-日 时:分。"""
    dt = datetime.fromtimestamp(mtime)
    if dt.date() == datetime.now().date():
        return dt.strftime("今天 %H:%M")
    return dt.strftime("%m-%d %H:%M")


def human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}M"
    return f"{max(1, size // 1024)}K"


def _clip_preview(body: str, limit: int) -> str:
    first = " ".join(body.splitlines()[0].split()).lstrip("#").strip()
    if not first:
        first = " ".join(body.split())
    if len(first) > limit:
        first = first[:limit] + "…"
    return first or "（空）"


def _last_assistant_text(path: Path, back: int = 64 * 1024) -> str:
    """从文件尾部倒着找最后一条助手回复（代表这次会话的产出）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > back:
                fh.seek(size - back)
                chunks = fh.read().split(b"\n")[1:]  # 首行可能不完整
            else:
                chunks = fh.read().split(b"\n")
    except OSError:
        return ""
    for raw in reversed(chunks):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if record.get("type") == "message" and record.get("role") == "assistant":
            body = extract_text_blocks(record.get("content")).strip()
            if body:
                return body
    return ""


def read_session_preview(path: Path, limit: int = 36) -> str:
    """会话识别摘要：优先最后一条助手回复（代表产出），其次首条回复、首条用户消息。"""
    tail = _last_assistant_text(path)
    if tail:
        return _clip_preview(tail, limit)

    first_user = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(400):
                line = fh.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if record.get("type") != "message":
                    continue
                body = extract_text_blocks(record.get("content")).strip()
                if not body:
                    continue
                role = record.get("role")
                if role == "assistant":
                    return _clip_preview(body, limit)
                if role == "user" and not first_user:
                    first_user = body
    except OSError:
        pass
    return _clip_preview(first_user, limit) if first_user else "（无内容）"


def list_sessions(project_dir: Path, limit: int = SESSION_MENU_LIMIT) -> list[tuple[Path, float, int, str]]:
    """项目下最近若干会话：[(路径, 修改时间, 字节数, 摘要)]，新→旧。"""
    rows: list[tuple[float, Path, int]] = []
    try:
        files = list(project_dir.glob("*.jsonl"))
    except OSError:
        return []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((stat.st_mtime, path, stat.st_size))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [
        (path, mtime, size, read_session_preview(path))
        for mtime, path, size in rows[:limit]
    ]


# --------------------------------------------------------------------------
# 多来源：不同智能体的会话各放一处，结构化时统一成「智能体 → 项目 → 会话」
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AgentSource:
    """一个智能体的会话目录。

    kind 决定怎么解析：codebuddy 是一层项目目录 + 每会话一个 jsonl；codex 是
    按日期分目录的 rollout-*.jsonl，项目要从文件头的 cwd 里读。
    """

    id: str
    label: str
    root: Path
    kind: str = "codebuddy"

    def exists(self) -> bool:
        try:
            return self.root.is_dir()
        except OSError:
            return False


def discover_sources() -> list[AgentSource]:
    """本机装了哪个就收哪个，目录不存在直接跳过。加新智能体在这里加一行。"""
    home = Path.home()
    candidates = [
        AgentSource("workbuddy", "WorkBuddy CLI", home / ".codebuddy" / "projects", "codebuddy"),
        AgentSource("codex", "Codex CLI", home / ".codex" / "sessions", "codex"),
    ]
    return [src for src in candidates if src.exists()]


def _walk_jsonl(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        for name in filenames:
            if name.lower().endswith(".jsonl"):
                yield Path(dirpath) / name


_CODEX_CWD: dict[str, str] = {}


def _codex_cwd(path: Path) -> str:
    """从 rollout 文件头读 cwd（会话属于哪个项目），按路径缓存。"""
    key = str(path)
    if key in _CODEX_CWD:
        return _CODEX_CWD[key]
    cwd = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(4):
                line = fh.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if record.get("type") == "session_meta":
                    payload = record.get("payload")
                    value = payload.get("cwd") if isinstance(payload, dict) else None
                    if isinstance(value, str):
                        cwd = value.strip()
                    break
    except OSError:
        pass
    _CODEX_CWD[key] = cwd
    return cwd


def kind_for_path(path: Path) -> str:
    """按路径判断属于哪个智能体：.codex 下的是 codex rollout，其余按 codebuddy。"""
    parts = {part.lower() for part in path.parts}
    return "codex" if ".codex" in parts else "codebuddy"


def session_label_for(path: Path, kind: str | None = None) -> str:
    """会话显示名：项目名 · 短号。codex 的文件名是 rollout-<时间>-<uuid>。"""
    kind = kind or kind_for_path(path)
    if kind != "codex":
        return session_label(path)
    cwd = _codex_cwd(path)
    project = Path(cwd).name if cwd else ""
    short = path.stem[-8:]
    return f"{project} · {short}" if project else short


def project_of(source: AgentSource, path: Path) -> str:
    """会话归属的项目键：codex 用 cwd 全路径，codebuddy 用目录名（slug）。"""
    if source.kind == "codex":
        return _codex_cwd(path)
    return path.parent.name


def iter_session_files(source: AgentSource):
    if source.kind == "codex":
        yield from _walk_jsonl(source.root)
        return
    try:
        subdirs = [p for p in source.root.iterdir() if p.is_dir()]
    except OSError:
        return
    for sub in subdirs:
        try:
            files = list(sub.glob("*.jsonl"))
        except OSError:
            continue
        for path in files:
            yield path


def _project_title(source: AgentSource, key: str, files: list[Path]) -> str:
    if source.kind == "codex":
        return Path(key).name if key else "（未知项目）"
    for path in files[:3]:
        cwd = read_session_cwd(path)
        if cwd:
            return Path(cwd).name
    return _friendly_dir_name(key)


def list_source_projects(source: AgentSource) -> list[tuple[str, str, int]]:
    """该智能体下的项目：[(项目键, 显示名, 会话数)]，按最近活动倒序。"""
    buckets: dict[str, list[Path]] = {}
    for path in iter_session_files(source):
        key = project_of(source, path)
        if not key:
            key = "（未知项目）"
        buckets.setdefault(key, []).append(path)

    rows: list[tuple[float, str, str, int]] = []
    for key, files in buckets.items():
        newest = 0.0
        for path in files:
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
        rows.append((newest, key, _project_title(source, key, files), len(files)))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [(key, title, count) for _n, key, title, count in rows]


def read_preview_for(source: AgentSource, path: Path) -> str:
    if source.kind != "codex":
        return read_session_preview(path)
    text = _codex_last_text(path, ("assistant",)) or _codex_last_text(path, ("user",))
    return _clip_preview(text, 36) if text else "（无内容）"


def _codex_last_text(path: Path, roles: tuple[str, ...], tail_bytes: int = 262_144) -> str:
    """从文件尾部往前找最后一条指定角色的消息（只看末尾 256KB，够用且快）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()               # 丢掉可能被截断的半行
            data = fh.read()
    except OSError:
        return ""
    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        if payload.get("role") not in roles:
            continue
        body = _codex_blocks(payload.get("content")).strip()
        if body:
            return body
    return ""


def list_source_sessions(
    source: AgentSource, project_key: str, limit: int = SESSION_MENU_LIMIT
) -> list[tuple[Path, float, int, str]]:
    """某项目下最近若干会话：[(路径, 修改时间, 字节数, 摘要)]，新→旧。"""
    rows: list[tuple[float, Path, int]] = []
    for path in iter_session_files(source):
        if project_of(source, path) != project_key:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((stat.st_mtime, path, stat.st_size))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [
        (path, mtime, size, read_preview_for(source, path))
        for mtime, path, size in rows[:limit]
    ]


def _iso_to_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


def _codex_blocks(blocks: object) -> str:
    """codex 的内容块：[{type: "output_text"|"input_text", text: "..."}]"""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _codex_exec_detail(raw: str) -> str:
    """codex 的自定义工具调用是一段 JS，抓一下里面最像命令的那一句。"""
    match = re.search(r'cmd\s*:\s*(["\'])(.+?)\1', raw, re.S)
    if match:
        return match.group(2).strip()
    match = re.search(r'await tools\.(\w+)\(', raw)
    if match:
        return f"tools.{match.group(1)}"
    return ""


def extract_codex(record: dict) -> StreamEvent | None:
    """codex rollout 记录 → 可显示事件。无关类型返回 None。"""
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    ts = _iso_to_ms(record.get("timestamp"))

    if kind == "message":
        role = payload.get("role")
        body = _codex_blocks(payload.get("content")).strip()
        if not body:
            return None
        if role == "user":
            # 平台注入的规则/工具说明不是用户自己打的，别当成任务显示
            if body.startswith("# AGENTS.md instructions") or body.startswith("<"):
                return None
            return StreamEvent("task", clip_text(prettify_message(body), MAX_MESSAGE_CHARS), ts)
        if role == "assistant":
            return StreamEvent("reply", clip_text(prettify_message(body), MAX_MESSAGE_CHARS), ts)
        return None

    if kind == "reasoning":
        # codex 的推理正文多半是加密的，只有 summary 偶尔有可读内容
        body = (_codex_blocks(payload.get("summary")) or _codex_blocks(payload.get("content"))).strip()
        if not body:
            return None
        return StreamEvent("think", clip_text(prettify_think(body), MAX_REASONING_CHARS), ts)

    if kind in ("custom_tool_call", "function_call"):
        name = str(payload.get("name") or "?")
        raw = payload.get("input") if kind == "custom_tool_call" else payload.get("arguments")
        label, detail = prettify_call(name, raw)
        if isinstance(raw, str):
            detail = _codex_exec_detail(raw) or detail
        return StreamEvent("call", detail or name, ts, label)

    if kind in ("custom_tool_call_output", "function_call_output"):
        body = _codex_blocks(payload.get("output")).strip()
        status = str(payload.get("status") or "completed")
        label, text = prettify_result(str(payload.get("name") or "工具"), status, body)
        return StreamEvent(
            "result_ok" if status == "completed" else "result_error",
            clip_text(text, MAX_RESULT_CHARS),
            ts,
            label,
        )
    return None


def extract_for(kind: str, record: dict) -> StreamEvent | None:
    """按来源类型选解析器。"""
    if kind == "codex":
        return extract_codex(record)
    return extract_record(record)


def find_latest_over(
    sources: list[AgentSource], keyword: str
) -> tuple[Path, int, int, AgentSource] | None:
    """跨来源找最新的会话：(路径, 修改时间ns, 大小, 来源)。

    排序优先级同 find_latest_session：项目匹配强度 > 有内容 > 修改时间。
    """
    kw = keyword.strip().casefold()
    best: tuple[tuple[int, int, int, str], Path, int, AgentSource] | None = None
    for source in sources:
        if not source.exists():
            continue
        for path in iter_session_files(source):
            try:
                stat = path.stat()
            except OSError:
                continue
            strength = _match_strength(project_of(source, path), kw)
            if strength == 0:
                continue
            active = 1 if stat.st_size >= MIN_ACTIVE_SESSION_BYTES else 0
            key = (strength, active, stat.st_mtime_ns, str(path).casefold())
            if best is None or key > best[0]:
                best = (key, path, stat.st_size, source)
    if best is None:
        return None
    return best[1], best[0][2], best[2], best[3]


# --------------------------------------------------------------------------
# 后台监控线程
# --------------------------------------------------------------------------
class SessionWatcher(threading.Thread):
    def __init__(
        self,
        root: "Path | list[AgentSource]",
        updates: "queue.Queue[UiUpdate]",
        commands: "queue.Queue[ControlCommand]",
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="session-watcher", daemon=True)
        if isinstance(root, list):
            self.sources = list(root)
        else:
            self.sources = [AgentSource("workbuddy", "WorkBuddy CLI", root, "codebuddy")]
        self.root = root
        self.updates = updates
        self.commands = commands
        self.stop_event = stop_event
        self.keyword = ""
        self.pinned: Path | None = None
        self.state: TailState | None = None
        self.current_path: Path | None = None
        self.current_kind = "codebuddy"
        self.wake = threading.Event()   # 收到命令时立即唤醒，切换不用等轮询

    # -- 主循环 ------------------------------------------------------------
    def run(self) -> None:
        next_scan = 0.0
        while not self.stop_event.is_set():
            try:
                self._consume_commands()
                now = time.monotonic()
                if now >= next_scan:
                    self._scan()
                    next_scan = now + SESSION_SCAN_SECONDS
                if self.state is not None:
                    self._tail()
            except Exception:
                self._emit(UiUpdate("error", text=traceback.format_exc(limit=3)))
            self.wake.wait(FILE_POLL_SECONDS)
            self.wake.clear()

    # -- 命令 --------------------------------------------------------------
    def _consume_commands(self) -> None:
        while True:
            try:
                cmd = self.commands.get_nowait()
            except queue.Empty:
                return
            if cmd.operation == "set_keyword":
                self.pinned = None
                self.keyword = cmd.value.strip()
                self.state = None
                self.current_path = None
                shown = cmd.label or self.keyword or "全部项目"
                self._emit(
                    UiUpdate(
                        "event",
                        event=StreamEvent(
                            "system",
                            f"切换到项目：{shown}",
                            _now_ms(),
                        ),
                    )
                )
                self._scan()
            elif cmd.operation == "pin_session":
                self.pinned = Path(cmd.value)
                self.keyword = ""
                self.state = None
                self.current_path = None
                shown = cmd.label or self.pinned.name
                self._emit(
                    UiUpdate(
                        "event",
                        event=StreamEvent(
                            "system", f"固定会话：{shown}（不再自动切换）", _now_ms()
                        ),
                    )
                )
                self._scan()

    # -- 会话发现 ----------------------------------------------------------
    def _scan(self) -> None:
        if self.pinned is not None:
            try:
                exists = self.pinned.exists()
            except OSError:
                exists = False
            if exists:
                if self.current_path != self.pinned:
                    try:
                        size = self.pinned.stat().st_size
                    except OSError:
                        size = 0
                    self._switch(self.pinned, size)
                return
            self.pinned = None
            self._emit(
                UiUpdate(
                    "event",
                    event=StreamEvent(
                        "system", "固定的会话已不存在，恢复自动跟随", _now_ms()
                    ),
                )
            )

        found = find_latest_over(self.sources, self.keyword)
        if found is None:
            if self.state is not None:
                self.state = None
                self.current_path = None
                self._emit(UiUpdate("session", text="—"))
                self._emit(UiUpdate("status", text="等待中"))
            return
        path, _mtime_ns, size, source = found
        if self.current_path != path:
            self._switch(path, size, source)

    def _source_for(self, path: Path) -> AgentSource | None:
        text = str(path).lower()
        for source in self.sources:
            if text.startswith(str(source.root).lower()):
                return source
        return None

    def _switch(self, path: Path, size: int, source: AgentSource | None = None) -> None:
        if source is None:
            source = self._source_for(path)
        self.current_path = path
        # 解析方式跟着来源走：按路径猜只在标准目录下可靠，测试或自定义目录会误判
        self.current_kind = source.kind if source is not None else kind_for_path(path)
        project_key = project_of(source, path) if source is not None else ""
        label = session_label_for(path, self.current_kind)
        self._emit(UiUpdate(
            "session", text=label, path=str(path),
            agent=source.id if source is not None else "",
            project=project_key,
        ))
        self._emit(
            UiUpdate(
                "event",
                event=StreamEvent("system", f"会话切换：{label}", _now_ms()),
            )
        )
        if size <= AUTO_SWITCH_FULL_READ_LIMIT:
            events, position, partial = self._read_head(path)
        else:
            events, position, partial = self._read_tail(path)

        for event in events[-INITIAL_REPLAY_EVENTS:]:
            self._emit(UiUpdate("event", event=event))

        self.state = TailState(path=path, position=position, partial=partial)
        self._emit(
            UiUpdate("status", text="固定会话" if self.pinned is not None else "跟随中")
        )

    def _read_head(self, path: Path) -> tuple[list[StreamEvent], int, bytes]:
        try:
            data = path.read_bytes()
        except OSError:
            return [], 0, b""
        lines = data.split(b"\n")
        partial = lines.pop()
        events = []
        for raw in lines:
            event = self._parse_line(raw)
            if event is not None:
                events.append(event)
        return events, len(data), partial

    def _read_tail(self, path: Path) -> tuple[list[StreamEvent], int, bytes]:
        try:
            size = path.stat().st_size
            back = min(size, TAIL_BACK_BYTES)
            with path.open("rb") as stream:
                stream.seek(size - back)
                data = stream.read()
        except OSError:
            return [], 0, b""
        lines = data.split(b"\n")
        if back < size:
            lines = lines[1:]  # 第一行可能不完整
        events = []
        for raw in lines:
            event = self._parse_line(raw)
            if event is not None:
                events.append(event)
        return events, size, b""

    # -- 增量读取 ----------------------------------------------------------
    def _tail(self) -> None:
        state = self.state
        if state is None:
            return
        try:
            size = state.path.stat().st_size
        except OSError:
            self.state = None
            self.current_path = None
            self._emit(UiUpdate("status", text="等待中"))
            return

        if size < state.position:
            state.position = 0
            state.partial = b""
            state.discard_oversized = False
            self._emit(
                UiUpdate(
                    "event",
                    event=StreamEvent(
                        "system", "检测到会话文件被截断，已从头重新跟随", _now_ms()
                    ),
                )
            )

        if size == state.position:
            return

        try:
            with state.path.open("rb") as stream:
                stream.seek(state.position)
                data = stream.read(READ_CHUNK_BYTES)
                state.position = stream.tell()
        except OSError:
            return

        if not data:
            return

        combined = state.partial + data
        lines = combined.split(b"\n")
        state.partial = lines.pop()
        if len(state.partial) > MAX_JSON_LINE_BYTES:
            state.partial = b""
            state.discard_oversized = True

        for raw in lines:
            if state.discard_oversized:
                state.discard_oversized = False
                continue
            event = self._parse_line(raw)
            if event is not None:
                self._emit(UiUpdate("event", event=event))

    def _parse_line(self, raw: bytes) -> StreamEvent | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        return extract_for(self.current_kind, obj)

    # -- 队列 --------------------------------------------------------------
    def _emit(self, update: UiUpdate) -> None:
        try:
            self.updates.put_nowait(update)
        except queue.Full:
            try:
                self.updates.get_nowait()
            except queue.Empty:
                pass
            try:
                self.updates.put_nowait(update)
            except queue.Full:
                pass


# --------------------------------------------------------------------------
# 主界面
# --------------------------------------------------------------------------
class MonitorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        # 必须先于任何 widget 尺寸计算：px() 依赖 UI_SCALE
        apply_ui_scale(self)
        self.updates: "queue.Queue[UiUpdate]" = queue.Queue(maxsize=UPDATE_QUEUE_SIZE)
        self.commands: "queue.Queue[ControlCommand]" = queue.Queue()
        self.stop_event = threading.Event()

        self.paused = False
        self.backlog: deque[StreamEvent] = deque(maxlen=DISPLAY_BACKLOG_SIZE)
        self.text_char_count = 0
        self._stats = {"events": 0, "call": 0, "think": 0, "reply": 0}

        # 过渡与动画状态
        self._reveal: deque[StreamEvent] = deque(maxlen=DISPLAY_BACKLOG_SIZE)
        self._transition_id = 0
        self._pending_label = ""
        self._fade_alpha = 1.0
        self._swapping = False
        self._tag_specs: dict[str, dict] = {}
        self._scroll_anim = False
        self._pulse_phase = 0.0
        self._status_base_color = COLORS["task"]
        self._resume_status: str | None = None
        self._session_rows: dict[Path, dict] = {}

        self.auto_scroll_var = tk.BooleanVar(value=True)
        self.keyword_var = tk.StringVar()
        self.status_var = tk.StringVar(value="等待中")
        self.session_var = tk.StringVar(value="—")
        self._project_names: dict[str, str] = {}

        self._configure_window()
        self._build_ui()

        self.watcher = SessionWatcher(
            ROOT, self.updates, self.commands, self.stop_event
        )
        self.watcher.start()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(UI_TICK_MS, self._drain)
        self.after(600, self._refresh_sessions)

    # -- 窗口 --------------------------------------------------------------
    def _configure_window(self) -> None:
        self.title(APP_TITLE)
        # 先隐藏，等 main() 把物理尺寸定好再显示，避免首帧按未缩放尺寸闪一下。
        self.withdraw()
        self.geometry("1180x760")
        self.minsize(px(940), px(600))
        self.after(0, self._fit_physical_size)
        self.configure(bg=BG)
        icon = resource_path(ICON_FILE)
        if icon.exists():
            try:
                self.iconbitmap(str(icon))          # 标题栏 + 任务栏
                self.iconbitmap(default=str(icon))  # 后续弹出的窗口沿用
            except tk.TclError:
                pass

    def _fit_physical_size(self, attempt: int = 0) -> None:
        """窗口映射后按物理像素定尺寸；窗口句柄还没就绪时短暂重试。"""
        if set_physical_size(self, px(1180), px(760)):
            return
        if attempt < 20:
            self.after(40, lambda: self._fit_physical_size(attempt + 1))

    def _pick_font(self) -> tuple[str, int]:
        families = set(tkfont.families(self))
        # 实测在 125% 缩放下 Consolas 的笔画比 Cascadia Mono 更实、行距更紧，
        # 深色底上对比更好，因此仍以 Consolas 为首选。
        for name in ("Consolas", "Cascadia Mono", "Courier New"):
            if name in families:
                return name, 12
        return "TkFixedFont", 12

    def _pick_cjk_font(self) -> str:
        """正文中文字体。

        Consolas 不含中文字形，而它的 FontLink 在系统里没有配置，GDI 只能
        回退到兜底的宋体——衬线 + 小字号点阵，与无衬线等宽的正文完全两种
        风格。这里显式指定黑体：无衬线、字形方正，且 linespace 20px 比
        Consolas 的 23px 更小，混排时不会把行距撑开。
        """
        families = set(tkfont.families(self))
        for name in ("SimHei", "黑体", "Microsoft YaHei UI", "微软雅黑", "Noto Sans SC"):
            if name in families:
                return name
        return "TkDefaultFont"

    def _pick_ui_font(self) -> str:
        """界面文字用无衬线字体，代码/会话号仍用等宽，层次更像产品。"""
        families = set(tkfont.families(self))
        for name in ("Microsoft YaHei UI", "Segoe UI", "Microsoft YaHei", "PingFang SC"):
            if name in families:
                return name
        return "TkDefaultFont"

    def _build_ui(self) -> None:
        mono, mono_size = self._pick_font()
        self.mono_small = (mono, max(8, mono_size - 4))
        self.mono_body = (mono, mono_size)
        ui_sans = self._pick_ui_font()
        # 界面标签（菜单、输入框、顶部会话名）里会出现项目名等中文，
        # 用等宽字体只会回退到宋体，因此这里统一走无衬线。
        self.ui_font = (ui_sans, max(9, mono_size - 3))
        self.sans_small = (ui_sans, 9)
        self.sans_font = (ui_sans, 10)
        self.sans_bold = (ui_sans, 10, "bold")
        self.brand_font = (ui_sans, 12, "bold")

        shell = tk.Frame(self, bg=BG)
        shell.pack(fill="both", expand=True)

        # ---------------- 侧栏 ----------------
        sidebar = tk.Frame(shell, bg=SIDEBAR_BG, width=px(248))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=SIDEBAR_BG)
        brand.pack(fill="x", padx=px(14), pady=(px(16), px(14)))
        avatar_img = None
        png = resource_path("workbuddy-68.png")
        if png.exists():
            try:
                avatar_img = scaled_photo(png, px(34))
            except tk.TclError:
                avatar_img = None
        if avatar_img is not None:
            self._avatar_img = avatar_img          # 保持引用，防止被回收
            tk.Label(brand, image=avatar_img, bg=SIDEBAR_BG,
                     borderwidth=0, highlightthickness=0).pack(side="left")
        else:
            avatar = tk.Canvas(brand, width=px(34), height=px(34), bg=SIDEBAR_BG,
                               highlightthickness=0)
            avatar.pack(side="left")
            avatar.create_rectangle(0, 0, px(34), px(34), fill="#172033", outline="#2b4472")
            avatar.create_text(px(17), px(17), text="WB", fill="#8fb3ff",
                               font=(ui_sans, 10, "bold"))
        titles = tk.Frame(brand, bg=SIDEBAR_BG)
        titles.pack(side="left", padx=(px(10), px(0)))
        tk.Label(titles, text="WorkBuddy", bg=SIDEBAR_BG, fg="#eef1f5",
                 font=self.brand_font).pack(anchor="w")
        tk.Label(titles, text="编码子 Agent · 实时", bg=SIDEBAR_BG, fg=MUTED,
                 font=self.sans_small).pack(anchor="w")

        self._hsep(sidebar)

        proj = tk.Frame(sidebar, bg=SIDEBAR_BG)
        proj.pack(fill="x", padx=px(14), pady=(px(12), px(4)))
        tk.Label(proj, text="项目", bg=SIDEBAR_BG, fg=MUTED,
                 font=self.sans_small).pack(anchor="w")
        prow = tk.Frame(proj, bg=SIDEBAR_BG)
        prow.pack(fill="x", pady=(px(6), px(0)))
        entry = tk.Entry(
            prow, textvariable=self.keyword_var, bg="#12161c", fg=FG,
            insertbackground=FG, relief="flat", font=self.ui_font,
            highlightthickness=1, highlightbackground=LINE, highlightcolor="#39444f",
        )
        entry.pack(side="left", fill="x", expand=True, ipady=px(4))
        entry.bind("<Return>", self._apply_keyword)
        self.project_btn = tk.Button(
            prow, text="▾", command=self._show_projects,
            bg="#1a1f27", fg="#cfd4dc", activebackground="#252c36",
            activeforeground="#ffffff", relief="flat", borderwidth=0,
            padx=px(8), pady=px(2), font=self.sans_small, cursor="hand2",
            highlightthickness=0,
        )
        self.project_btn.pack(side="left", padx=(px(6), px(0)))

        head = tk.Frame(sidebar, bg=SIDEBAR_BG)
        head.pack(fill="x", padx=px(14), pady=(px(12), px(4)))
        tk.Label(head, text="最近会话", bg=SIDEBAR_BG, fg=MUTED,
                 font=self.sans_small).pack(side="left")
        tk.Button(
            head, text="⟳", command=self._refresh_sessions,
            bg=SIDEBAR_BG, fg="#6b7380", activebackground=SIDEBAR_BG,
            activeforeground="#cfd4dc", relief="flat", borderwidth=0,
            font=self.sans_small, cursor="hand2", highlightthickness=0,
        ).pack(side="right")

        self.sessions_box = tk.Frame(sidebar, bg=SIDEBAR_BG)
        self.sessions_box.pack(fill="x", padx=px(10))

        bottom = tk.Frame(sidebar, bg=SIDEBAR_BG)
        bottom.pack(side="bottom", fill="x", padx=px(14), pady=px(12))
        self.stats_var = tk.StringVar(value="事件 0 · 工具 0 · 思考 0 · 回复 0")
        tk.Label(bottom, textvariable=self.stats_var, bg=SIDEBAR_BG,
                 fg="#7c848f", font=self.sans_small).pack(anchor="w")
        tk.Checkbutton(
            bottom, text="自动滚动", variable=self.auto_scroll_var,
            bg=SIDEBAR_BG, fg="#c8ccd3", selectcolor="#161b22",
            activebackground=SIDEBAR_BG, activeforeground=FG,
            font=self.sans_small, relief="flat", highlightthickness=0, bd=px(0),
        ).pack(anchor="w", pady=(px(8), px(0)))
        tk.Label(bottom, text="Ctrl+P 暂停 · Ctrl+L 清空", bg=SIDEBAR_BG,
                 fg="#565d68", font=self.sans_small).pack(anchor="w", pady=(px(6), px(0)))

        # ---------------- 主区 ----------------
        main = tk.Frame(shell, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(main, bg=HEADER_BG)
        header.pack(fill="x")
        inner = tk.Frame(header, bg=HEADER_BG)
        inner.pack(fill="x", padx=px(16), pady=px(10))

        self.status_dot = tk.Canvas(inner, width=px(12), height=px(12), bg=HEADER_BG,
                                    highlightthickness=0)
        self.status_dot.pack(side="left", pady=(px(1), px(0)))
        self._dot = self.status_dot.create_oval(
            px(3), px(3), px(10), px(10), fill=COLORS["task"], outline=""
        )

        self.status_label = tk.Label(
            inner, textvariable=self.status_var, bg=HEADER_BG,
            fg=COLORS["task"], font=self.sans_bold,
        )
        self.status_label.pack(side="left", padx=(px(8), px(0)))

        tk.Label(inner, text="·", bg=HEADER_BG, fg="#3c434d",
                 font=self.sans_font).pack(side="left", padx=px(8))
        tk.Label(inner, textvariable=self.session_var, bg=HEADER_BG,
                 fg="#aeb5bf", font=self.ui_font).pack(side="left")

        self._button(inner, "清空", self._clear_output).pack(side="right")
        self.pause_btn = self._button(inner, "暂停", self._toggle_pause)
        self.pause_btn.pack(side="right", padx=(px(0), px(6)))

        tk.Frame(main, bg=LINE, height=px(1)).pack(fill="x")

        body = tk.Frame(main, bg=BG)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(
            body, wrap="word", state="disabled", undo=False,
            bg=PANEL_BG, fg=FG, insertbackground=FG,
            selectbackground="#26405f", relief="flat",
            padx=px(14), pady=px(12), borderwidth=0, highlightthickness=0,
            font=self.mono_body, spacing1=px(0), spacing2=px(0), spacing3=px(3),
        )
        self.text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        scroll.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=scroll.set)

        tk.Frame(main, bg=LINE, height=px(1)).pack(fill="x")
        footer = tk.Frame(main, bg=HEADER_BG)
        footer.pack(fill="x")
        self.footer_var = tk.StringVar(value="等待会话")
        tk.Label(footer, textvariable=self.footer_var, bg=HEADER_BG,
                 fg="#6b7380", font=self.sans_small).pack(side="left", padx=px(16), pady=px(5))
        tk.Label(footer, text="只读展示，不会改动项目文件", bg=HEADER_BG,
                 fg="#565d68", font=self.sans_small).pack(side="right", padx=px(16))

        self._configure_tags()

        self.bind_all("<Control-p>", self._shortcut_pause)
        self.bind_all("<Control-l>", self._shortcut_clear)

    def _hsep(self, parent: tk.Misc, padx: int = 14) -> None:
        tk.Frame(parent, bg=LINE, height=px(1)).pack(fill="x", padx=padx)

    def _button(self, parent: tk.Misc, label: str, command):
        btn = tk.Button(
            parent, text=label, command=command,
            bg="#1a1f27", fg="#cfd4dc", activebackground="#252c36",
            activeforeground="#ffffff", relief="flat", borderwidth=0,
            padx=px(10), pady=px(3), font=self.sans_small, cursor="hand2",
            highlightthickness=0,
        )
        btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg="#222936"))
        btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg="#1a1f27"))
        return btn

    def _configure_tags(self) -> None:
        mono, mono_size = self._pick_font()
        bold_font = (mono, mono_size, "bold")
        stamp_font = (mono, max(8, mono_size - 4))

        # 先把所有标签的目标色记下来，之后统一由 _apply_fade 上色 / 做淡入淡出
        specs: dict[str, dict] = {}

        def spec(tag: str, fg: str | None = None, bg: str | None = None, **opts) -> None:
            conf: dict = dict(opts)
            if fg:
                conf["foreground"] = fg
            if bg:
                conf["background"] = bg
            specs[tag] = conf

        spec("timestamp", "#5f6671", font=stamp_font)
        spec("rule", "#232932")
        spec("spacer", spacing1=px(6))
        spec("banner", "#4e5763", font=(mono, max(9, mono_size - 2)),
             justify="center", spacing1=px(6), spacing3=px(10))

        label_colors = {
            "task": COLORS["task"], "think": COLORS["think_label"],
            "call": COLORS["call"], "result_ok": COLORS["result_ok"],
            "result_error": COLORS["result_error"], "reply": COLORS["reply_label"],
            "system": COLORS["system"], "warning": COLORS["warning"],
        }
        body_colors = {
            "task": COLORS["task_body"], "think": COLORS["think"],
            "call": COLORS["call_body"], "result_ok": COLORS["result_body"],
            "result_error": COLORS["result_err_body"], "reply": COLORS["reply"],
            "system": COLORS["system"], "warning": COLORS["warning"],
        }
        for kind in label_colors:
            spec(f"{kind}_label", label_colors[kind], font=bold_font)
            spec(f"g_{kind}", label_colors[kind], font=bold_font)
            spec(kind, body_colors[kind])

        # 置灰 / Markdown 轻量渲染（要在正文色之后定义，优先级更高）
        spec("dim", "#79808a")
        spec("md_head", COLORS["md_head"], font=bold_font)
        spec("md_code", COLORS["md_code_fg"], COLORS["md_code_bg"])

        # 任务 / 回复用卡片版式：左右留白 + 上下间距
        spec("blk_task", bg="#151310", lmargin1=px(16), lmargin2=px(16), rmargin=px(16),
             spacing1=px(4), spacing3=px(8))
        spec("blk_reply", bg="#101519", lmargin1=px(16), lmargin2=px(16), rmargin=px(16),
             spacing1=px(4), spacing3=px(10))
        spec("indent", lmargin1=px(18), lmargin2=px(18), rmargin=px(10))

        # 中文用单独一副字体（只改 font，不动颜色），见 _pick_cjk_font
        cjk_family = self._pick_cjk_font()
        spec("cjk", font=(cjk_family, mono_size))
        spec("cjk_bold", font=(cjk_family, mono_size, "bold"))

        self._tag_specs = specs
        self._apply_fade(1.0)
        # 优先级要压过 *_label / md_head 等带 bold 的标签，否则中文仍走回退字体
        for tag in ("cjk", "cjk_bold"):
            try:
                self.text.tag_raise(tag)
            except tk.TclError:
                pass

    def _apply_fade(self, alpha: float) -> None:
        """把整套标签色按 alpha 混向底色，实现整屏淡入淡出。"""
        self._fade_alpha = alpha
        for tag, conf in self._tag_specs.items():
            opts: dict[str, str] = {}
            fg = conf.get("foreground")
            bg = conf.get("background")
            if fg:
                opts["foreground"] = mix_color(fg, PANEL_BG, alpha)
            if bg:
                opts["background"] = mix_color(bg, PANEL_BG, alpha)
            if opts:
                try:
                    self.text.tag_configure(tag, **opts)
                except tk.TclError:
                    pass

    # -- 控制 --------------------------------------------------------------
    def _slice_hint(self) -> None:
        """点击后的即时反馈：先亮起“切换中”，等后台真正换页。"""
        current = self.status_var.get()
        if current.startswith("切换"):
            return
        self._hint_prev = current
        self.status_var.set("切换中")
        self._update_status_color()
        self._hint_id = getattr(self, "_hint_id", 0) + 1
        token = self._hint_id
        self.after(1600, lambda: self._end_hint(token))

    def _end_hint(self, token: int) -> None:
        if token != getattr(self, "_hint_id", 0):
            return
        if self.status_var.get().startswith("切换"):
            self.status_var.set(getattr(self, "_hint_prev", "跟随中"))
            self._update_status_color()

    def _apply_keyword(self, _event=None) -> str:
        raw = self.keyword_var.get().strip()
        try:
            self.commands.put_nowait(
                ControlCommand("set_keyword", raw, self._project_names.get(raw, raw))
            )
            self.watcher.wake.set()
        except queue.Full:
            pass
        self._slice_hint()
        return "break"

    def _pick_project(self, keyword: str, display: str) -> None:
        """从下拉菜单选中某个项目：立即切换。"""
        self.keyword_var.set(keyword)
        try:
            self.commands.put_nowait(
                ControlCommand("set_keyword", keyword, display)
            )
            self.watcher.wake.set()
        except queue.Full:
            pass
        self._slice_hint()

    def _pick_session(self, path: Path) -> None:
        """固定跟随某一次历史会话：点下就高亮，正文交给过渡动画。"""
        label = session_label(path)
        self.keyword_var.set("")
        try:
            self.commands.put_nowait(
                ControlCommand("pin_session", str(path), label)
            )
            self.watcher.wake.set()
        except queue.Full:
            pass

        if path == getattr(self.watcher, "current_path", None):
            self._paint_session_rows(path)   # 点的就是当前这条，只刷新高亮
            return

        # 乐观反馈：不等后台确认，先让这一行选中、状态变「切换中」
        self._paint_session_rows(path)
        self._slice_hint()

    def _show_projects(self) -> None:
        """弹出项目列表；每个项目展开是该项目的历史会话（实时扫描，无需重启）。"""
        projects = list_projects(ROOT)
        self._project_names = {kw: disp for disp, kw, _c, _p in projects}

        menu = tk.Menu(
            self, tearoff=0, bg="#161616", fg="#cccccc",
            activebackground="#264f78", activeforeground="#ffffff",
            bd=px(0), font=self.ui_font,
        )
        menu.add_command(
            label="全部项目（跟随最新）",
            command=lambda: self._pick_project("", "全部项目"),
        )
        menu.add_separator()
        if not projects:
            menu.add_command(
                label="（还没发现项目，先在 codex 里派一次任务）", state="disabled"
            )
        for display, keyword, count, project_dir in projects:
            sub = tk.Menu(
                menu, tearoff=0, bg="#161616", fg="#cccccc",
                activebackground="#264f78", activeforeground="#ffffff",
                bd=px(0), font=self.ui_font,
            )
            sub.add_command(
                label="跟随该项目最新会话",
                command=lambda k=keyword, d=display: self._pick_project(k, d),
            )
            sub.add_separator()
            for path, mtime, size, preview in list_sessions(project_dir):
                sub.add_command(
                    label=f"{format_mtime(mtime)}  {human_size(size):>5}  {preview}",
                    command=lambda p=path: self._pick_session(p),
                )
            menu.add_cascade(label=f"{display}  ({count})", menu=sub)

        try:
            x = self.project_btn.winfo_rootx()
            y = self.project_btn.winfo_rooty() + self.project_btn.winfo_height() + 2
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    # -- 侧栏 --------------------------------------------------------------
    def _current_project_dir(self) -> Path | None:
        path = getattr(getattr(self, "watcher", None), "current_path", None)
        if isinstance(path, Path):
            return path.parent
        kw = self.keyword_var.get().strip()
        if kw:
            for display, key, _count, pdir in list_projects(ROOT):
                if kw in (display, key):
                    return pdir
        return None

    def _refresh_sessions(self) -> None:
        box = getattr(self, "sessions_box", None)
        if box is None:
            return

        project_dir = self._current_project_dir()
        current = getattr(getattr(self, "watcher", None), "current_path", None)
        sessions = list_sessions(project_dir, limit=6) if project_dir is not None else []

        # 列表没变时只改高亮，不重建控件——避免每次切换都整块闪烁
        paths = tuple(item[0] for item in sessions)
        if paths and tuple(self._session_rows.keys()) == paths:
            self._paint_session_rows(current)
            return

        for child in list(box.winfo_children()):
            child.destroy()
        self._session_rows.clear()

        if project_dir is None:
            tk.Label(box, text="等待会话…", bg=SIDEBAR_BG, fg="#565d68",
                     font=self.sans_small, anchor="w").pack(fill="x", padx=px(4), pady=px(6))
            return

        if not sessions:
            tk.Label(box, text="该项目还没有会话", bg=SIDEBAR_BG, fg="#565d68",
                     font=self.sans_small, anchor="w").pack(fill="x", padx=px(4), pady=px(6))
            return

        for path, mtime, size, preview in sessions:
            bg = SIDEBAR_BG
            row = tk.Frame(box, bg=bg, cursor="hand2")
            row.pack(fill="x", pady=px(1))

            top = tk.Frame(row, bg=bg)
            top.pack(fill="x", padx=px(8), pady=(px(5), px(0)))
            time_label = tk.Label(top, text=format_mtime(mtime), bg=bg, fg="#6f7681",
                                  font=self.mono_small)
            time_label.pack(side="left")
            size_label = tk.Label(top, text=human_size(size), bg=bg, fg="#5a616b",
                                  font=self.mono_small)
            size_label.pack(side="right")

            text = preview if len(preview) <= 22 else preview[:22] + "…"
            desc = tk.Label(row, text=text, bg=bg, fg="#9aa1ab",
                            font=self.sans_small, anchor="w")
            desc.pack(fill="x", padx=px(8), pady=(px(1), px(5)))

            self._session_rows[path] = {
                "row": row, "top": top, "time": time_label,
                "size": size_label, "desc": desc,
            }
            for widget in (row, top, desc, time_label, size_label):
                widget.bind("<Button-1>", lambda _e, p=path: self._pick_session(p))

        self._paint_session_rows(current)

    def _paint_session_rows(self, current: Path | None) -> None:
        """只更新高亮与文字颜色，不销毁重建，切换时侧栏不会闪。"""
        for path, parts in self._session_rows.items():
            active = current == path
            bg = "#141a22" if active else SIDEBAR_BG
            parts["row"].configure(bg=bg)
            parts["top"].configure(bg=bg)
            parts["time"].configure(bg=bg, fg="#8f98a4" if active else "#6f7681")
            parts["size"].configure(bg=bg)
            parts["desc"].configure(bg=bg, fg="#d6dce4" if active else "#9aa1ab")

    # -- 统计与快捷键 ------------------------------------------------------
    def _bump_stats(self, kind: str) -> None:
        stats = self._stats
        stats["events"] += 1
        if kind == "call":
            stats["call"] += 1
        elif kind == "think":
            stats["think"] += 1
        elif kind == "reply":
            stats["reply"] += 1
        if hasattr(self, "stats_var"):
            self.stats_var.set(
                f"事件 {stats['events']} · 工具 {stats['call']} · "
                f"思考 {stats['think']} · 回复 {stats['reply']}"
            )

    def _shortcut_pause(self, _event=None) -> str:
        self._toggle_pause()
        return "break"

    def _shortcut_clear(self, _event=None) -> str:
        self._clear_output()
        return "break"

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_btn.configure(text="继续" if self.paused else "暂停")
        if self.paused:
            self.status_var.set("已暂停")
        else:
            pinned = getattr(self.watcher, "pinned", None) is not None
            self.status_var.set("固定会话" if pinned else "跟随中")
        self._update_status_color()

    def _clear_output(self) -> None:
        """清屏也走淡出，不会“啪”一下整块消失。"""
        self._transition_id += 1      # 取消进行中的过渡动画
        token = self._transition_id
        self._pending_label = ""
        self._reveal.clear()
        self.backlog.clear()
        self._scroll_anim = False
        self._swapping = True
        self._animate_fade(token, 0, TRANSITION_OUT_STEPS, True)

    def _update_status_color(self) -> None:
        status = self.status_var.get()
        if status.startswith("已暂停"):
            color = "#a78bfa"
        elif status.startswith(("同步", "切换")):
            color = "#5ac8fa"
        elif status.startswith("固定"):
            color = COLORS["think_label"]
        elif status.startswith("跟随"):
            color = COLORS["result_ok"]
        elif status.startswith("错误"):
            color = COLORS["result_error"]
        else:
            color = COLORS["task"]
        self._status_base_color = color
        self.status_label.configure(fg=color)
        try:
            self.status_dot.itemconfigure(self._dot, fill=color)
        except (AttributeError, tk.TclError):
            pass

    # -- 切换过渡 ----------------------------------------------------------
    def _begin_transition(self, label: str) -> None:
        """柔化切换：旧内容淡出 → 换页横幅 → 新内容淡入 + 逐条追平。"""
        self._transition_id += 1
        self._pending_label = label
        self._reveal.clear()          # 丢弃上一会话还没渲染的事件
        self._scroll_anim = False
        self._swapping = True         # 淡出期间不插入，免得刚画上就被清掉
        self._animate_fade(self._transition_id, 0, TRANSITION_OUT_STEPS, True)

    def _animate_fade(self, token: int, step: int, total: int, out: bool) -> None:
        if token != self._transition_id:
            return
        progress = step / max(1, total)
        alpha = (1.0 - progress) if out else progress
        self._apply_fade(max(FADE_FLOOR, alpha))
        if step >= total:
            if out:
                self._swap_view(token)
            return
        delay = TRANSITION_OUT_MS if out else TRANSITION_IN_MS
        self.after(delay, lambda: self._animate_fade(token, step + 1, total, out))

    def _swap_view(self, token: int) -> None:
        """淡出结束的瞬间换页：清屏 + 居中的会话横幅，随后开始淡入。"""
        if token != self._transition_id:
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text_char_count = 0
        self._stats = {"events": 0, "call": 0, "think": 0, "reply": 0}
        if hasattr(self, "stats_var"):
            self.stats_var.set("事件 0 · 工具 0 · 思考 0 · 回复 0")
        label = self._pending_label
        if label and label != "—":
            self._put(f"\n{'─' * 3} {label} {'─' * 3}\n", ("banner",))
        self.text.configure(state="disabled")
        self.text.yview_moveto(0.0)
        self._swapping = False
        self._animate_fade(token, 0, TRANSITION_IN_STEPS, False)

    def _follow_bottom(self) -> None:
        """平滑跟随到底：每次靠近剩余可视距离的 60%，而不是一眼跳到末尾。"""
        try:
            top, bottom = self.text.yview()
        except tk.TclError:
            return
        delta = 1.0 - bottom
        if delta <= 0.001:
            self.text.yview_moveto(1.0)
            self._scroll_anim = False
            return
        if delta > 0.35:             # 一次涌入太多（如恢复暂停），直接到位
            self.text.see("end")
            self._scroll_anim = False
            return
        self.text.yview_moveto(min(1.0, top + delta * 0.6))
        self._scroll_anim = True

    def _pulse(self) -> None:
        """跟随中时状态灯缓慢呼吸，让界面看起来是“活的”。"""
        try:
            status = self.status_var.get()
            if not status.startswith(("跟随", "同步", "切换")):
                self.status_dot.coords(self._dot, px(3.5), px(3.5), px(9.5), px(9.5))
                return
            self._pulse_phase = (self._pulse_phase + 0.28) % (2 * math.pi)
            k = 0.5 + 0.5 * math.sin(self._pulse_phase)
            r = 2.9 + 1.2 * k
            fill = mix_color(self._status_base_color, HEADER_BG, 0.45 + 0.55 * k)
            c = px(6.5)
            self.status_dot.coords(self._dot, c - r * UI_SCALE, c - r * UI_SCALE,
                                   c + r * UI_SCALE, c + r * UI_SCALE)
            self.status_dot.itemconfigure(self._dot, fill=fill)
        except (AttributeError, tk.TclError):
            pass

    # -- 渲染 --------------------------------------------------------------
    def _drain(self) -> None:
        try:
            near_bottom = self.text.yview()[1] >= 0.98
        except tk.TclError:
            return

        handled = 0
        while handled < RENDER_BATCH_SIZE:
            try:
                update = self.updates.get_nowait()
            except queue.Empty:
                break
            handled += 1
            self._handle(update)

        changed = False
        if not self.paused:
            while self.backlog:                 # 恢复暂停后并入待渲染队列
                self._reveal.append(self.backlog.popleft())
            if self._reveal and not self._swapping:
                if self._fade_alpha < 0.999:    # 过渡期放慢，形成逐条追平
                    budget = TRANSITION_REVEAL_PER_TICK
                else:
                    budget = max(1, min(24, len(self._reveal) // 8))
                rendered = 0
                while self._reveal and rendered < budget:
                    self._insert_event(self._reveal.popleft())
                    rendered += 1
                changed = rendered > 0

        if self._resume_status and not self._reveal and not self.paused:
            self.status_var.set(self._resume_status)
            self._resume_status = None
            self._update_status_color()

        if self.auto_scroll_var.get() and ((changed and near_bottom) or self._scroll_anim):
            self._follow_bottom()

        self._maybe_trim()
        self._pulse()
        self.after(UI_TICK_MS, self._drain)

    def _handle(self, update: UiUpdate) -> None:
        if update.operation == "status":
            if not self.paused:
                text = update.text
                if text == "跟随中" and self._reveal:
                    self._resume_status = text
                    text = "同步中"
                self.status_var.set(text)
                self._update_status_color()
        elif update.operation == "session":
            previous = self.session_var.get()
            self.session_var.set(update.text)
            if hasattr(self, "footer_var"):
                self.footer_var.set(f"当前会话  {update.text}")
            if update.text != previous and update.text != "—":
                self._begin_transition(update.text)
            self._refresh_sessions()
        elif update.operation == "error":
            first = (update.text or "").splitlines()
            message = first[0] if first else "未知错误"
            self._insert_event(StreamEvent("warning", f"[内部错误] {message}", _now_ms()))
        elif update.operation == "event" and update.event is not None:
            event = update.event
            if event.kind == "system" and event.text.startswith("会话切换："):
                return          # 换页横幅已经说明了同样的信息，避免重复
            if self.paused:
                self.backlog.append(event)
            else:
                self._reveal.append(event)

    def _insert_event(self, event: StreamEvent) -> None:
        if event.timestamp_ms:
            try:
                stamp = datetime.fromtimestamp(event.timestamp_ms / 1000).strftime("%H:%M:%S")
            except (OSError, ValueError, OverflowError):
                stamp = datetime.now().strftime("%H:%M:%S")
        else:
            stamp = datetime.now().strftime("%H:%M:%S")

        kind = event.kind
        label = event.label or LABELS.get(kind, kind)
        body = event.text.rstrip("\n")
        lines = body.split("\n") if body else [""]
        block = BLOCK_TAGS.get(kind, "")

        def tags(*extra: str) -> tuple[str, ...]:
            parts = [kind, *extra]
            if block:
                parts.append(block)
            return tuple(parts)

        self.text.configure(state="normal")
        if kind == "task":
            self.text.insert("end", "\n", ("spacer",))
            self.text.insert("end", "─" * 46 + "\n", tags("rule"))
        elif kind == "reply":
            self.text.insert("end", "\n", ("spacer",))

        self._put(GUTTER.get(kind, "·") + " ", tags(f"g_{kind}"))
        self._put(f"{stamp} ", tags("timestamp"))
        self._put(f"{label}  ", tags(f"{kind}_label"), bold=True)
        self._insert_body(lines, kind, block)
        self.text.configure(state="disabled")

        self._bump_stats(kind)
        self.text_char_count += len(stamp) + len(label) + len(body) + 8

    def _put(self, content: str, tags: tuple[str, ...], bold: bool = False) -> None:
        """插入文本，中文连续段换用中文字体。

        Text 里同一个字符的字体由优先级最高的标签决定，所以中文段额外挂
        cjk / cjk_bold（已 tag_raise 到最前），英文部分仍走 Consolas。
        """
        if not content:
            return
        if CJK_RE.search(content) is None:
            self.text.insert("end", content, tags)
            return
        cjk_tag = "cjk_bold" if bold else "cjk"
        pos = 0
        for match in CJK_RE.finditer(content):
            if match.start() > pos:
                self.text.insert("end", content[pos:match.start()], tags)
            self.text.insert("end", match.group(), tuple(tags) + (cjk_tag,))
            pos = match.end()
        if pos < len(content):
            self.text.insert("end", content[pos:], tags)

    def _insert_body(self, lines: list[str], kind: str, block: str) -> None:
        """正文逐行插入：首行接在标签后，后续行缩进；顺带做 Markdown 轻量渲染。"""
        in_code = False
        for idx, line in enumerate(lines):
            stripped = line.strip()
            extra: list[str] = []
            if idx > 0:
                extra.append("indent")
            if stripped.startswith("```"):
                in_code = not in_code
                extra.append("md_code")
            elif in_code:
                extra.append("md_code")
            elif stripped.startswith("#"):
                extra.append("md_head")
            elif stripped.startswith("⋯"):
                extra.append("dim")
            elif kind.startswith("result") and idx > 0:
                extra.append("dim")
            elif kind == "think":
                extra.append("dim")
            parts = [kind, *extra]
            if block:
                parts.append(block)
            prefix = "" if idx == 0 else "   "
            self._put(prefix + line + "\n", tuple(parts), bold="md_head" in extra)

    def _maybe_trim(self) -> None:
        if self.text_char_count <= MAX_TEXT_CHARS:
            return
        self.text.configure(state="normal")
        try:
            boundary = self.text.index(f"1.0 + {TRIM_TEXT_CHARS} chars")
            found = self.text.search("\n", boundary, stopindex="end")
            if found:
                self.text.delete("1.0", f"{found} + 1 chars")
            else:
                self.text.delete("1.0", boundary)
        except tk.TclError:
            pass
        self.text.configure(state="disabled")
        self.text_char_count = max(0, self.text_char_count - TRIM_TEXT_CHARS)
        self._insert_event(
            StreamEvent("system", "为保持界面性能，已清理最早的部分显示内容", _now_ms())
        )

    # -- 异常与关闭 --------------------------------------------------------
    def report_callback_exception(self, exc, value, tb) -> None:
        detail = "".join(traceback.format_exception(exc, value, tb))
        try:
            self._insert_event(StreamEvent("warning", f"[回调异常] {detail.splitlines()[-1]}", _now_ms()))
        except Exception:
            pass

    def _on_close(self) -> None:
        self.stop_event.set()
        try:
            self.watcher.wake.set()
        except AttributeError:
            pass
        try:
            self.watcher.join(timeout=2)
        except Exception:
            pass
        self.destroy()


# --------------------------------------------------------------------------
# 自检 & 入口
# --------------------------------------------------------------------------
def run_selftest() -> None:
    print(f"root = {ROOT}")
    found = find_latest_session(ROOT, "")
    if found is None:
        print("未找到任何会话文件")
        return
    path, mtime_ns, size = found
    print(f"最新会话: {path}")
    print(f"大小: {size} bytes, mtime_ns: {mtime_ns}")
    # 直接复用解析逻辑
    data = path.read_bytes()
    lines = data.split(b"\n")
    partial = lines.pop()
    events = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            event = extract_record(obj)
            if event is not None:
                events.append(event)
    print(f"解析事件数: {len(events)}  (半行: {len(partial)} bytes)")
    from collections import Counter
    print("事件分布:", dict(Counter(e.kind for e in events)))
    print("--- 最近 8 条 ---")
    for event in events[-8:]:
        stamp = (
            datetime.fromtimestamp(event.timestamp_ms / 1000).strftime("%H:%M:%S")
            if event.timestamp_ms
            else "--:--:--"
        )
        print(f"{stamp} [{LABELS.get(event.kind, event.kind)}] {event.text[:120]}")


def probe_dpi() -> None:
    """把 DPI 探测结果写入临时文件。

    --noconsole 打包后没有 stdout，排查「界面模糊 / 尺寸不对」时用：
        WorkBuddyMonitor.exe --dpi-probe
    结果落在 %TEMP%\\workbuddy-dpi.txt。
    """
    lines = []
    try:
        lines.append(f"platform      = {sys.platform}")
        lines.append(f"awareness fn  = {enable_dpi_awareness()}")
        if sys.platform == "win32":
            v = ctypes.c_int(-1)
            ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(v))
            lines.append(
                "GetProcessDpiAwareness = "
                + {0: "UNAWARE", 1: "SYSTEM", 2: "PER_MONITOR"}.get(v.value, str(v.value))
            )
        lines.append(f"system_dpi()  = {system_dpi()}")
        root = tk.Tk()
        root.withdraw()
        lines.append(f"winfo_fpixels = {root.winfo_fpixels('1i')}")
        lines.append(f"tk scaling    = {root.tk.call('tk', 'scaling')}")
        lines.append(f"apply_ui_scale= {apply_ui_scale(root)}")
        lines.append(f"tk scaling(2) = {root.tk.call('tk', 'scaling')}")
        lines.append(f"px(1180)x    = {px(1180)} x {px(760)}")
        f13 = tkfont.Font(root=root, family="Cascadia Mono", size=12)
        lines.append(f"font 12pt     = linespace {f13.metrics('linespace')}px")
        # wm geometry 往返：写 1476 读回来是不是 1476，反映 Tk 是否二次折算
        root.geometry("1476x951")
        root.update_idletasks()
        lines.append(f"wm roundtrip  = set 1476x951 -> read {root.geometry()}")
        hwnd = int(root.wm_frame(), 16)
        if hwnd:
            r = wt.RECT()
            ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(r))
            lines.append(f"client px     = {r.right} x {r.bottom}")
        lines.append(f"set_physical  = {set_physical_size(root, px(1180), px(760))}")
        if hwnd:
            r2 = wt.RECT()
            ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(r2))
            lines.append(f"client after  = {r2.right} x {r2.bottom}")
        root.destroy()
    except Exception as exc:
        lines.append(f"ERROR: {type(exc).__name__}: {exc}")
    out = Path(tempfile.gettempdir()) / "workbuddy-dpi.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    if "--selftest" in sys.argv:
        run_selftest()
        return
    if "--dpi-probe" in sys.argv:
        probe_dpi()
        return
    try:
        enable_dpi_awareness()
        app = MonitorApp()
        # 窗口映射后才拿得到有效句柄，这里同步校正一次物理尺寸，
        # 不等 after 回调，避免首帧按未缩放尺寸显示。
        app.update_idletasks()
        app.update()
        set_physical_size(app, px(1180), px(760))
        app.deiconify()
        # Tk 在 mainloop 真正开始后会按 remembered geometry 再排一次版，
        # 这里在稳定后再校正一次，确保最终停在物理像素尺寸上。
        app.after(220, lambda: set_physical_size(app, px(1180), px(760)))
        app.mainloop()
    except Exception as exc:
        try:
            messagebox.showerror(APP_TITLE, f"启动失败：{exc}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
