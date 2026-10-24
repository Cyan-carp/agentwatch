# AGENTS.md · WorkBuddy Monitor

给任何接手的智能体看的交接单（Codex / WorkBuddy / 其他）。先读这一份，细节再看 `README.md`。

- 远程仓库：<https://github.com/Cyan-carp/wb-monitor>（公开，MIT）
- 发行版：`WorkBuddyMonitor.exe` 挂在 [Releases](https://github.com/Cyan-carp/wb-monitor/releases)，不入库
- `.workbuddy/memory/` 与 `wb-codebuddy.cmd` 是**本地文件**，已写进 `.gitignore`，不要提交

## 这是什么

一个 Windows 桌面小工具：只读地跟随 codebuddy（WorkBuddy）的会话 JSONL，把「任务 / 思考 / 工具调用 / 回复」实时渲染成时间轴，用来盯另一个智能体在干什么。界面是「Python 本地 HTTP 服务 + Edge/Chrome 的 `--app` 独立窗口」，不是 Tk。

- 数据源（只读）：`%USERPROFILE%\.codebuddy\projects\<项目slug>\*.jsonl`
- 成品：`WorkBuddyMonitor.exe`（PyInstaller onefile，12 MB）
- 依赖：仅标准库。`make-icon.py` 另外需要 Pillow，但它只在改图标时用

## 三条硬约束

1. **对被监控目录只读**。任何情况下不往 `%USERPROFILE%\.codebuddy\projects` 里写东西，不改被监控项目的文件。
2. **`WorkBuddyMonitor.exe` 是构建产物，不入库**（`.gitignore` 已排除）。要新版本就重新打包，见下文。
3. **改了 `wbm_web.py` 或 `web/index.html` 必须重新打包**。双击 exe 跑的是打包进去的那份，不会读磁盘上的源码；不重新打包就会以为改动「没生效」。

## 怎么跑

```powershell
# 用户视角：双击 WorkBuddyMonitor.exe

# 开发：直接跑源码（会拉起独立窗口）
python wbm_web.py

# 只起服务不拉窗口，在浏览器里调界面
python wbm_web.py --serve-only

# 自检：起服务、探接口、打印发现的项目，然后退出
python wbm_web.py --selftest
```

## 架构

| 文件 | 职责 |
| --- | --- |
| `wbm_web.py` | **入口**。`ThreadingHTTPServer` + SSE 推流 + 拉起浏览器 + 生命周期管理（窗口关掉就退出） |
| `wb_gui.py` | **数据层**（原 Tk 版）。会话发现、增量读 JSONL、事件提炼与美化。`MonitorApp(tk.Tk)` 仍保留，可 `python wb_gui.py` 独立运行旧版 |
| `web/index.html` | **前端**。原生 JS + 内联 SVG 图标，无构建步骤、无 CDN |

运行链路：`SessionWatcher`（线程，tail JSONL）→ `UiUpdate` → `wbm_web` 的泵线程转成 SSE JSON → 前端 `EventSource`。
新客户端接入先收一条 `reset`（状态/会话/统计），再回放最近 400 条事件。

接口：`GET /`、`GET /events`（SSE，5 秒心跳）、`GET /api/projects`、`GET /api/sessions?keyword=`、`POST /api/command`（`set_keyword` / `pin_session` / `pause`）。

## 改哪里

| 想改的东西 | 动这个 |
| --- | --- |
| 界面配色、字号、间距、markdown 转换规则 | `web/index.html`（`renderBody()` + `<style>`） |
| 窗口初始尺寸 / 浏览器参数 | `wbm_web.py` 的 `open_app_window()` |
| 端口起点 | `wbm_web.py` 的 `PORT_START`（默认 8765，试 24 个） |
| 回放条数、心跳频率 | `wbm_web.py` 的 `RECENT_LIMIT` / 泵线程 |
| 事件提炼、工具摘要、折叠行数 | `wb_gui.py` 的 `prettify_*` / `extract_record` |
| 会话发现与排序规则 | `wb_gui.py` 的 `list_projects` / `list_sessions` |
| 图标 | `make-icon.py`（比例在文件开头）**且**同步 `web/index.html` 的 favicon 与侧栏 SVG |

## 打包

```powershell
pip install pyinstaller    # 只需一次
python build.py            # 产物写到项目根目录，覆盖同名 exe
python build.py --keep     # 只构建到 dist/，不动根目录
```

`build.py` 按自身位置推导所有路径，换机器不用改。旧 exe 正在运行时它会自动改名为 `.exe.old` 腾位（Windows 允许重命名运行中的 exe），关了窗口再删。

等价的手动命令：

```powershell
python -m PyInstaller --noconfirm --clean --onefile --noconsole --noupx --name WorkBuddyMonitor --manifest dpi.manifest --icon workbuddy.ico --add-data "web\index.html;web" --add-data "workbuddy.ico;." --distpath . --workpath build --specpath build wbm_web.py
```

三个参数不能省：

- `--manifest dpi.manifest` —— 高 DPI 感知。漏了字体就会糊（进程按 96 DPI 渲染再被系统拉伸）
- `--add-data "...\web\index.html;web"` —— 漏了界面直接 500
- `--add-data "...\workbuddy.ico;."` —— 漏了窗口图标缺失

## 踩过的坑（别再踩）

| 坑 | 结论 |
| --- | --- |
| Edge 首次运行弹「同步你的浏览数据」，`--disable-features` 压不住 | 必须预写 `Default/Preferences`：`sync.requested=false`、`signin.allowed=false`、`distribution.skip_first_run_checks=true` |
| 上次的 Edge 还在后台，新启动被单例接管后自己退出 | 每次运行用独立 `run-<pid>` profile 目录，从母版复制 |
| 进程退出了但窗口还在（Chromium 转交 `--app` 请求） | 不能只看 `proc.poll()` 判退出；用「前端是否连上过 + 断连时长」判 |
| `--noconsole` 下子进程继承不到句柄 | `Popen` 必须显式 `stdin/stdout/stderr=DEVNULL` |
| bash 里 `--workpath "%TEMP%\x"` 不展开 | 会在项目目录建一个字面名为 `%TEMP%` 的文件夹。用绝对路径 |
| exe 正在运行，打包覆盖报 `safe-delete ... trash aborted` | `DeleteFileW` 也失败；只能 `MoveFileW` 改名腾位，重启后再删旧文件 |
| PyInstaller onefile 父进程拿不到窗口 | `Popen` 的 pid 是 bootloader，窗口属于子进程。按「窗口句柄快照取差集」找 |
| 打包版报「拒绝连接」 | 看 `%LOCALAPPDATA%\WorkBuddyMonitor\startup.log`（每次启动重写） |
| 关窗后十几秒才退 | 正常。SSE 心跳 5 秒，靠断连判定，不是秒退 |
| `wmic` 在本机安全策略黑名单里 | 用 `Get-CimInstance Win32_Process` 替代 |
| 两个实例抢同一个端口，窗口连到旧的、改了看着没生效 | **Windows 的 `SO_REUSEADDR` 含义是「允许绑到别人正在监听的端口」**。`_port_free()` 先 `connect_ex` 探活，再不带 REUSEADDR 地 bind；`_Server.allow_reuse_address = False` |
| 侧栏项目筛选与主区域跟随结果对不上 | 两处必须共用 `wb_gui.match_project()`（尾部 → 包含 → 不分大小写），别各写一套 |

## 排查

- 启动日志：`%LOCALAPPDATA%\WorkBuddyMonitor\startup.log` —— 监听端口、浏览器 pid、前端是否连上、退出原因
- 浏览器 profile：`%LOCALAPPDATA%\WorkBuddyMonitor\webview\run-<pid>\`（超过 1 小时的残留会自动清理）
- 页面顶部出现「未连接到本地服务」= 服务没跑。`file://` 直接打开 html 也会这样，属预期
- 冻结后挂起：`python wbm_web.py --serve-only` 后用浏览器访问 `http://127.0.0.1:8765/`

## 不要做

- 不要给界面加构建步骤（webpack/vite）或 CDN 依赖——这个项目的价值之一就是单文件、零依赖
- 不要为了「字体好看」把界面改回 Tk。Tk 走 GDI，中文字体会回退到宋体，这是当初换掉它的原因
- 不要提交 exe、`__pycache__`、`_*.py` 这类调试脚本
