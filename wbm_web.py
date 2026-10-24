#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy Monitor — 网页版。

用 Chromium 的 app 模式承载界面：字体走 DirectWrite 渲染，中文不再经过
GDI 的字体链接兜底，观感与浏览器里的对话界面一致；同时窗口没有地址栏和
标签页，看起来就是个独立应用。

数据层复用 wb_gui 的会话发现与解析（SessionWatcher / list_projects 等），
这里只负责：HTTP 服务、SSE 推送、以及拉起浏览器窗口。

用法: 双击 WorkBuddyMonitor.exe；开发时 python wbm_web.py
自检: python wbm_web.py --selftest
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from wb_gui import (
    ROOT,
    ControlCommand,
    SessionWatcher,
    UiUpdate,
    format_mtime,
    human_size,
    list_projects,
    list_sessions,
    match_project,
    resource_path,
)

APP_TITLE = "WorkBuddy Monitor"
PORT_START = 8765
PORT_TRIES = 24
RECENT_LIMIT = 400          # 新客户端接入时回放多少条
BROWSER_GRACE = 45.0        # 窗口多久没连上服务，就认为这次启动失败
IDLE_EXIT_SECONDS = 4.0     # 连上过之后，断连多久算窗口已关（心跳 5 秒，实际约 5~9 秒退净）

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


# --------------------------------------------------------------------------
# 广播中心
# --------------------------------------------------------------------------
class Hub:
    """把 watcher 产出的事件分发给所有 SSE 客户端，并为新客户端保留回放。"""

    def __init__(self) -> None:
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._recent: list[dict] = []
        self.snapshot = {"status": "等待中", "session": "—", "stats": self.blank_stats()}

    @staticmethod
    def blank_stats() -> dict:
        return {"events": 0, "call": 0, "think": 0, "reply": 0}

    def subscribe(self) -> queue.Queue:
        box: queue.Queue = queue.Queue(maxsize=4000)
        with self._lock:
            self._clients.append(box)
            recent = list(self._recent)
            snap = dict(self.snapshot)
        box.put_nowait({"type": "reset", **snap})
        for item in recent:
            box.put_nowait(item)
        return box

    def unsubscribe(self, box: queue.Queue) -> None:
        with self._lock:
            try:
                self._clients.remove(box)
            except ValueError:
                pass

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def broadcast(self, msg: dict) -> None:
        with self._lock:
            if msg.get("type") == "event":
                self._recent.append(msg)
                if len(self._recent) > RECENT_LIMIT:
                    del self._recent[: len(self._recent) - RECENT_LIMIT]
            elif msg.get("type") in ("status", "session"):
                self.snapshot[msg["type"]] = msg.get("text", "")
            elif msg.get("type") == "stats":
                self.snapshot["stats"] = msg.get("stats", self.blank_stats())
            targets = list(self._clients)
        for box in targets:
            try:
                box.put_nowait(msg)
            except queue.Full:
                pass


# --------------------------------------------------------------------------
# 请求处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "WorkBuddyMonitor"
    hub: Hub = None          # 由 serve() 注入
    app: "MonitorServer" = None

    def log_message(self, *_args) -> None:   # 静音默认访问日志
        pass

    # -- 工具 --------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                self._serve_index()
            elif path == "/events":
                self._serve_events()
            elif path == "/api/projects":
                self._serve_projects()
            elif path == "/api/sessions":
                self._serve_sessions()
            elif path == "/api/ping":
                self._json({"ok": True})
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()

    def _serve_index(self) -> None:
        page = resource_path("web/index.html")
        if not page.exists():
            self._send(500, "缺少 web/index.html".encode("utf-8"),
                       "text/plain; charset=utf-8")
            return
        self._send(200, page.read_bytes(), "text/html; charset=utf-8")

    def _serve_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        box = self.hub.subscribe()
        try:
            while True:
                try:
                    msg = box.get(timeout=5.0)
                except queue.Empty:
                    # 心跳：顺便让服务端尽快感知连接是否还在（关窗后能及时发现）
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(msg, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(box)

    def _serve_projects(self) -> None:
        items = []
        for display, keyword, count, _dir in list_projects(ROOT):
            items.append({"name": display, "keyword": keyword, "count": count})
        self._json({"projects": items})

    @staticmethod
    def _resolve_project(keyword: str) -> tuple[Path, str] | None:
        """把界面输入的项目名解析成 (目录, 显示名)：精确/尾部优先，其次包含。

        与主区域 find_latest_session 用的是同一套匹配强度，两处结果才一致。
        """
        if not keyword.strip():
            return None
        best: tuple[int, Path, str] | None = None
        for display, _kw, _count, path in list_projects(ROOT):
            strength = match_project(display, keyword)
            if strength and (best is None or strength > best[0]):
                best = (strength, path, display)
        return (best[1], best[2]) if best else None

    def _serve_sessions(self) -> None:
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        keyword = (qs.get("keyword") or [""])[0]
        hit = self._resolve_project(keyword)
        if hit is None and keyword.strip():
            # 有关键字但没有任何项目命中，不能回退到「最新会话」，否则会误导
            self._json({"sessions": [], "matched": False, "project": ""})
            return
        if hit is None:
            from wb_gui import find_latest_session

            best = find_latest_session(ROOT, "")
            target = best[0].parent if best else None
            resolved = ""
        else:
            target, resolved = hit
        sessions = []
        if target is not None:
            for path, mtime, size, preview in list_sessions(target):
                sessions.append({
                    "path": str(path),
                    "time": format_mtime(mtime),
                    "size": human_size(size),
                    "preview": preview,
                })
        self._json({"sessions": sessions, "matched": True, "project": resolved})

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path != "/api/command":
            self._json({"ok": False, "error": "unknown"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        op = str(body.get("op", ""))
        value = str(body.get("value", ""))
        label = str(body.get("label", ""))
        try:
            self.app.command(op, value, label)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 500)
            return
        self._json({"ok": True})


# --------------------------------------------------------------------------
# 服务主体
# --------------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    """关掉 SO_REUSEADDR：Windows 上它允许抢占别人已监听的端口。

    保持默认值会在已有实例时「绑定成功」，于是窗口连上旧实例、看到旧界面。
    """

    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 32


class MonitorServer:
    def __init__(self) -> None:
        self.hub = Hub()
        self.updates: "queue.Queue[UiUpdate]" = queue.Queue(maxsize=5000)
        self.commands: "queue.Queue[ControlCommand]" = queue.Queue()
        self.stop_event = threading.Event()
        self.watcher = SessionWatcher(ROOT, self.updates, self.commands, self.stop_event)
        self.stats = {"events": 0, "call": 0, "think": 0, "reply": 0}
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self._pump_thread: threading.Thread | None = None
        self._exit_flag = threading.Event()

    # -- 命令 --------------------------------------------------------------
    def command(self, op: str, value: str, label: str) -> None:
        if op == "pause":
            return                      # 前端本地处理
        try:
            self.commands.put_nowait(ControlCommand(op, value, label))
        except queue.Full:
            pass
        self.watcher.wake.set()

    # -- 事件泵 ------------------------------------------------------------
    def start(self) -> None:
        self.watcher.start()
        self._pump_thread = threading.Thread(target=self._pump, name="pump", daemon=True)
        self._pump_thread.start()

    def _pump(self) -> None:
        while not self.stop_event.is_set():
            try:
                update = self.updates.get(timeout=0.5)
            except queue.Empty:
                continue
            kind = update.operation
            if kind == "event" and update.event is not None:
                ev = update.event
                if ev.kind in self.stats:
                    self.stats[ev.kind] += 1
                self.stats["events"] += 1
                self.hub.broadcast({
                    "type": "event",
                    "kind": ev.kind,
                    "label": ev.label,
                    "text": ev.text,
                    "ts": ev.timestamp_ms,
                })
                if self.stats["events"] % 4 == 0:
                    self.hub.broadcast({"type": "stats", "stats": dict(self.stats)})
            elif kind == "status":
                self.hub.broadcast({"type": "status", "text": update.text})
            elif kind == "session":
                self.hub.broadcast({"type": "session", "text": update.text})
            elif kind == "error":
                self.hub.broadcast({"type": "event", "kind": "warning",
                                    "label": "错误", "text": update.text, "ts": None})
        self.hub.broadcast({"type": "stats", "stats": dict(self.stats)})

    # -- HTTP --------------------------------------------------------------
    def serve(self) -> str:
        last_error: Exception | None = None
        for offset in range(PORT_TRIES):
            port = PORT_START + offset
            if not self._port_free(port):
                continue
            try:
                httpd = _Server(("127.0.0.1", port), Handler)
            except OSError as exc:
                last_error = exc
                continue
            self.httpd = httpd
            self.port = port
            Handler.hub = self.hub
            Handler.app = self
            threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
            return f"http://127.0.0.1:{port}/"
        raise RuntimeError(f"没有可用端口（{PORT_START} 起试了 {PORT_TRIES} 个）：{last_error}")

    @staticmethod
    def _port_free(port: int) -> bool:
        """端口是否真的可用。

        先 connect 试一次：Windows 上 SO_REUSEADDR 的含义是「可以绑到别人已经在
        监听的端口」，只看 bind 成功会把自己骗过去——两个进程抢同一个端口，浏览器
        连到旧的那个，表现成「改完没生效」。所以先用 connect 探活，再不带
        SO_REUSEADDR 地 bind。
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.15)
        try:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return False
        finally:
            probe.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.watcher.wake.set()
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 浏览器窗口
# --------------------------------------------------------------------------
def webview_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    path = base / "WorkBuddyMonitor" / "webview"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_startup(message: str) -> None:
    """--noconsole 没有控制台，把启动过程落到 %LOCALAPPDATA%\\WorkBuddyMonitor\\startup.log。

    每次启动第一行会重写文件，只保留最近一次，出问题时把这个文件发出来即可。
    """
    try:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        root = base / "WorkBuddyMonitor"
        root.mkdir(parents=True, exist_ok=True)
        path = root / "startup.log"
        mode = "a" if getattr(log_startup, "_opened", False) else "w"
        if mode == "w":
            log_startup._opened = True          # type: ignore[attr-defined]
        with path.open(mode, encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except Exception:
        pass


def prepare_profile() -> Path:
    """为这次运行准备一份**独占**的浏览器 profile。

    两个原因：
    1. 只用命令行参数压不住 Edge 首次运行的同步推广弹窗，必须预写好
       Preferences（已看过欢迎页、关闭同步与登录）；
    2. 如果沿用同一个 profile 目录，且上一次的 Edge 还在后台驻留，Chromium
       会通过进程单例把命令行转交给旧实例、然后自身退出——新窗口可能不出现，
       表现为「点了没反应」。每次都换一个目录就不会被接管。
    """
    root = webview_dir()
    master = root / "Default" / "Preferences"

    # 母版 Preferences 只写一次（真·首次运行）
    if not master.exists():
        master.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "browser": {
                "has_seen_welcome_page": True,
                "check_default_browser": False,
            },
            "distribution": {
                "skip_first_run_checks": True,
                "import_bookmarks": False,
                "make_chrome_default": False,
                "make_chrome_default_for_user": False,
                "suppress_first_run_default_browser_prompt": True,
                "suppress_first_run_bubble": True,
            },
            "sync": {"requested": False, "has_setup_completed": True},
            "signin": {"allowed": False, "allowed_on_next_startup": False},
            "credentials_enable_service": False,
            "profile": {"password_manager_enabled": False},
        }
        master.write_text(json.dumps(payload), encoding="utf-8")

    # 清掉一小时前遗留的运行目录（可能因进程占用没删干净）
    now = time.time()
    for old in root.glob("run-*"):
        try:
            if now - old.stat().st_mtime > 3600:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass

    run_dir = root / f"run-{os.getpid()}"
    shutil.rmtree(run_dir, ignore_errors=True)
    (run_dir / "Default").mkdir(parents=True, exist_ok=True)
    shutil.copy2(master, run_dir / "Default" / "Preferences")
    return run_dir


def browser_exe() -> str | None:
    for candidate in EDGE_CANDIDATES + CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def open_app_window(url: str, width: int = 1180, height: int = 800) -> subprocess.Popen | None:
    exe = browser_exe()
    if exe is None:
        return None
    args = [
        exe,
        f"--app={url}",
        f"--window-size={width},{height}",
        f"--user-data-dir={prepare_profile()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-features=msEdgeSyncPromo,msEdgeIdentity,msEdgeCollections,Translate,MediaRouter",
        "--metrics-recording-only",
    ]
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def run_selftest() -> None:
    server = MonitorServer()
    try:
        url = server.serve()
        server.start()
        time.sleep(1.2)
        import urllib.request

        with urllib.request.urlopen(url + "api/projects", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print("服务地址:", url)
        print("发现项目:", len(data["projects"]))
        for item in data["projects"][:5]:
            print(f"  - {item['name']} ({item['count']} 个会话)")
        with urllib.request.urlopen(url + "api/ping", timeout=5) as resp:
            print("健康检查:", resp.read().decode())
    finally:
        server.shutdown()


def report_fatal(exc: BaseException) -> None:
    """--noconsole 下异常无处可看，落到临时文件并弹一个提示框。"""
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_startup(f"启动失败：{exc!r}")
    log_path = ""
    try:
        path = Path(os.environ.get("TEMP", str(Path.home()))) / "workbuddy-web-crash.txt"
        path.write_text(detail, encoding="utf-8")
        log_path = str(path)
    except Exception:
        pass
    try:
        import ctypes

        text = f"启动失败：{exc}\n\n详细堆栈：{log_path or '（无法写入日志）'}"
        ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10)
    except Exception:
        pass


def main() -> None:
    if "--selftest" in sys.argv:
        run_selftest()
        return
    if "--serve-only" in sys.argv:
        server = MonitorServer()
        url = server.serve()
        server.start()
        print(url, flush=True)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            server.shutdown()
        return

    server = MonitorServer()
    log_startup(f"启动，数据目录 {ROOT}")
    url = server.serve()
    log_startup(f"服务已监听 {url}")
    server.start()

    proc = open_app_window(url)
    if proc is None:
        import webbrowser

        log_startup("没找到 Edge/Chrome，改用默认浏览器")
        webbrowser.open(url)
        print(f"未找到 Edge/Chrome，已用默认浏览器打开：{url}")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return
    log_startup(f"已拉起浏览器窗口 pid={proc.pid}")

    # 退出条件（不能只看浏览器进程句柄：Chromium 有时会把请求转交给已在运行的
    # 实例后自身退出，此时进程已结束但窗口还活着，一旦立刻收摊就是「拒绝连接」）
    started = time.monotonic()
    seen_client = False
    idle_since: float | None = None
    try:
        while True:
            now = time.monotonic()
            if server.hub.client_count() > 0:
                if not seen_client:
                    seen_client = True
                    log_startup("前端已连接")
                idle_since = None
            elif seen_client:
                if idle_since is None:
                    idle_since = now
                elif now - idle_since > IDLE_EXIT_SECONDS:
                    log_startup("前端已断开，退出")
                    break
            elif now - started > BROWSER_GRACE:
                log_startup(f"{BROWSER_GRACE:.0f} 秒内没有前端连上来，退出")
                break

            if proc.poll() is not None and not seen_client and now - started > BROWSER_GRACE:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                     # noqa: BLE001
        report_fatal(exc)
        raise SystemExit(1)
