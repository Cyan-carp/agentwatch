# WorkBuddy Monitor

> 一个 Windows 小工具：只读地跟随 codebuddy / WorkBuddy 的会话 JSONL，把「任务 · 思考 · 工具调用 · 回复」实时渲染成时间轴，用来盯另一个智能体到底在干什么。

- **只读**：不往被监控目录写任何东西，不改被监控项目的文件
- **零依赖**：纯 Python 标准库，前端是单个 HTML，无构建步骤、无 CDN
- **像原生应用**：本地 HTTP 服务 + Chromium `--app` 窗口，没有地址栏和标签页

---

## 一、在新设备上部署

三种方式，选一种即可。**只有方式 C 需要 Python 打包环境**，A 和 B 都不用改代码。

### 前置条件

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 10 / 11（x64） | 用到 `%LOCALAPPDATA%` 和 `ctypes.windll`，macOS / Linux 跑不了 |
| 浏览器 | Edge（系统自带）或 Chrome，有其一即可 | 两个都没有时会退化成默认浏览器的普通标签页，不再是独立窗口 |
| 数据源 | `%USERPROFILE%\.codebuddy\projects\` 必须已存在 | 这是被监控方的会话目录。新设备上要先装 WorkBuddy / CodeBuddy 并至少跑过一次会话，否则界面只能显示「没有会话」 |
| Python | 3.10 及以上（实测 3.13） | 仅方式 B、C 需要。代码用了 `@dataclass(slots=True)`，3.9 会报错；需带 `tkinter`（python.org 安装包默认勾选） |
| 磁盘 | 约 15 MB | exe 12 MB，无其他依赖 |

### 方式 A：下载 exe 直接运行（推荐）

1. 打开 [Releases](https://github.com/Cyan-carp/wb-monitor/releases)，下载最新的 `WorkBuddyMonitor.exe`
2. 放到任意目录，例如 `D:\Tools\WorkBuddyMonitor\`
3. 双击运行。首次启动约 5 秒，弹出标题为 `WorkBuddy Monitor` 的独立窗口

免安装、免管理员权限、不写注册表。运行时只在两处落文件：

| 位置 | 内容 |
| --- | --- |
| `%LOCALAPPDATA%\WorkBuddyMonitor\startup.log` | 启动日志，每次启动重写，只留最近一次 |
| `%LOCALAPPDATA%\WorkBuddyMonitor\webview\run-<pid>\` | 浏览器独立配置目录，关闭后残留会在下次启动时清理 |

**卸载**：删掉 exe，再删 `%LOCALAPPDATA%\WorkBuddyMonitor` 即可。

> Windows 可能弹「已保护你的电脑」——这是 SmartScreen 对未签名 exe 的常规提示，不是检测到威胁。点「更多信息」→「仍要运行」。本项目没有代码签名证书。

### 方式 B：克隆源码运行

```powershell
git clone https://github.com/Cyan-carp/wb-monitor.git
cd wb-monitor
python wbm_web.py
```

零第三方依赖，**不需要 `pip install`**。启动后会自己拉起独立窗口。

调试用两个开关：

```powershell
python wbm_web.py --serve-only   # 只起服务不拉窗口，手动用浏览器开 http://127.0.0.1:8765/
python wbm_web.py --selftest     # 起服务、探接口、打印发现的项目，然后退出
```

### 方式 C：从源码打包成 exe

```powershell
pip install pyinstaller
python build.py
```

产物是项目根目录的 `WorkBuddyMonitor.exe`（约 12 MB）。`build.py` 里的路径全部按脚本自身位置推导，换机器、换目录都不用改；如果旧 exe 还在运行，它会自动把旧文件改名为 `.exe.old` 腾位，不会因为文件被占用而失败。

只构建不覆盖根目录（产物留在 `dist/`）：

```powershell
python build.py --keep
```

### 部署后怎么确认正常

1. 窗口标题是 `WorkBuddy Monitor`，左栏能看到项目名与会话数
2. 顶部状态灯亮蓝 = 正在跟随；灰 = 等待新会话
3. 看不到数据时，先看 `%LOCALAPPDATA%\WorkBuddyMonitor\startup.log`——里面有监听端口、浏览器 pid、前端是否连上、退出原因
4. 确认数据源非空：

```powershell
dir "$env:USERPROFILE\.codebuddy\projects"
```

这个目录为空或不存在，界面就只能是空的。本工具不生成会话数据，只读它。

---

## 二、界面与用法

窗口是 agent 工作台风格：左侧会话栏 + 顶部实时状态 + 中间工作流时间轴。

```
┌──────────────┬───────────────────────────────────────────────┐
│ ▮▮▮ WorkBuddy │ ● 跟随中 · sample-proj · 01a1667e  [暂停][清空]│
│     编码子 Agent├───────────────────────────────────────────────┤
│ 项目          │                                               │
│ [全部项目    ]│  ▌ 19:02:12 任务  这是用户授权的连接检查…      │
│ 最近会话    ⟳ │  ▌ 19:02:13 思考  先确认项目结构…              │
│ 19:02  23K   │  ▸ 19:02:15 读取  shared-memory/memory.md      │
│ 连接正常…     │  ✔ 19:02:15 完成  Read · 共 47 行              │
│ 04:34 566K   │     项目公共记忆                               │
│ 已完成第五…   │     ⋯ 中间省略 35 行                           │
│ ─────────────│  ◆ 19:02:20 回复  分析结果                     │
│ 事件 16 · 工具│                                               │
│ 9 · 思考 5    │                           [回到底部]          │
└──────────────┴───────────────────────────────────────────────┘
```

**时间轴的符号含义**（一眼分辨角色）：`▌ 任务`、`▌ 思考`、`▸ 读取/执行/查找…`、`✔ 完成`、`✖ 失败`、`◆ 回复`、`· 系统`。任务与回复是带底色和留白的卡片块，思考置灰，工具结果整体缩进并淡化。

**日志不是原样搬运，而是把 markdown 就地换成排版**：

- 行内 `` `code` `` 变成带底色的等宽小块，`**粗体**` 变粗体，`[[笔记/路径]]` 变成蓝色条目（只显示末两段路径），`[](链接)` 只留可读文字
- `## 标题` 变亮蓝加粗的一行；`- 项目` 的短横换成 CSS 画的小圆点；`1. 2. 3.` 保留序号；`---` 变分隔线；` ``` ` 代码块整体套底色
- 回复顶部的 YAML frontmatter 折叠成一行「省略元数据 N 行」，不占版面
- 工具调用提炼成一行摘要：`▸ 读取 shared-memory/xxx.md`、`▸ 执行 npm test`，而不是糊整段 JSON 参数
- 工具结果自动剥掉 `23→` 行号噪音，带摘要头（`共 47 行`）；超过 24 行只显示头 8 行 + 尾 4 行，中间折叠为 `⋯ 中间省略 N 行 ⋯`

界面功能：

| 控件 | 说明 |
| --- | --- |
| 顶栏状态灯 | 亮蓝=跟随中，琥珀=已固定，灰=等待；右侧显示当前会话 |
| 「取消固定」 | 只在固定状态出现。点一下回到自动跟随最新会话（点侧栏某条会话就会进入固定） |
| 「刷新」下拉 | 定时向服务端补拿增量的间隔：关 / 3 秒 / 5 秒 / 10 秒，默认 5 秒，选择记在本机 |
| 侧栏「项目」 | 点输入框会下拉出所有项目（带会话数），也可以直接打简称按回车：`workbuddy`、`monitor`、`MONITOR` 都认，不分大小写；留空 = 全部项目（自动跟随最新会话） |
| 侧栏「最近会话」 | 列出该项目最近 20 次会话（时间 · 大小 · 产出摘要），**点一条即固定**跟随那次；`⟳` 重新扫描 |
| 暂停 / 继续 | 暂停**显示**但后台继续跟随，恢复后自动补齐，不漏事件 |
| 清空 | 只清屏，不影响跟随位置 |
| 回到底部 | 往上翻历史时会浮出，点一下回到实时末尾 |
| 快捷键 | `Ctrl+P` 暂停/继续　`Ctrl+L` 清屏 |

侧栏底部实时统计事件/工具/思考/回复条数。

### 跟随规则

- 默认跟随全局最新会话：被监控方每次派新任务会生成新的会话文件，界面**自动切换**，并插入一条 `─── 项目名 · 会话号 ───` 分隔
- 点侧栏某条会话 = **固定**跟随那一次，不会因为别处产生新会话被切走；把「项目」框清空回车即解除固定
- 关键字匹配规则（侧栏列表与主区域跟随**同一套**，不会再出现「列表说没有会话、主区域却切走了」）：
  1. 与项目名完全相等，或以 `-关键字` 结尾（尾部命中）——优先级最高，`monitor` 能精确落到 `workbuddy-monitor`
  2. 上面没命中就退回包含匹配——`workbuddy` 也认
  3. 全部不分大小写
  4. 都不命中时列表显示「没有匹配「xxx」的项目」，主区域保持不动，不会乱跳到别的项目
- 输入简称命中后，输入框会自动补全成真实项目名，下次不用再猜
- 自动模式会跳过只有快照的空壳会话（<1KB），不会停在空白会话上发呆

## 三、为什么是「本地服务 + 浏览器窗口」

界面最早是 Tk 画的。Tk 走 GDI 渲染，英文字体（Consolas/Cascadia）不含中文字形，缺字时只能按注册表 `FontLink\SystemLink` 兜底，而这两个字体在该键下**没有配置项**，于是中文一路退到宋体，和英文风格割裂、边缘发虚。

现在的做法是把界面交给 Chromium：

- 中文由 `Microsoft YaHei UI`、英文与代码由 `Cascadia Mono` 直接渲染，走 DirectWrite + 亚像素抗锯齿，不再有回退问题
- 用 `--app=` 模式打开，窗口没有地址栏、没有标签页、没有右键菜单，看起来就是个独立应用；任务栏图标取自页面 favicon（即 `web/index.html` 里的三根活动条）

**进程结构**

| 文件 | 职责 |
| --- | --- |
| `wbm_web.py` | 入口。起 HTTP 服务、把 watcher 的事件转成 SSE 推给前端、拉起浏览器窗口、窗口关掉就退出 |
| `wb_gui.py` | 数据层（原 Tk 版）。会话发现、增量读取 JSONL、事件提炼与美化，被 `wbm_web.py` 复用。`python wb_gui.py` 仍可单独跑旧版 Tk 界面 |
| `web/index.html` | 前端。原生 JS，无构建步骤、无 CDN 依赖 |

**接口**

| 路径 | 说明 |
| --- | --- |
| `GET /` | 界面 |
| `GET /events` | SSE 事件流，5 秒一次 JSON 心跳；新客户端接入先收一条 `reset`（含状态/统计/序号）再回放最近 400 条 |
| `GET /api/state?after=N` | 定时轮询用：当前状态 + 序号 `N` 之后的事件增量 |
| `GET /api/projects` | 项目列表与各项目会话数 |
| `GET /api/sessions?keyword=` | 某项目最近会话（时间/大小/摘要/路径） |
| `POST /api/command` | `set_keyword` / `pin_session` / `pause` |

**更新是两条腿走路**

推送（SSE）负责实时，轮询（`/api/state`）负责兜底：

- 每条事件带单调递增的 `seq`，前端据此去重。两边同时送来同一条也只渲染一次
- 轮询按 `after=<已渲染的最大 seq>` 只取增量，空轮询返回 `{"events": []}`
- SSE 静默断掉、被中间层缓冲住、或者 pump 线程意外退出时，轮询仍会把画面推到最新，不会出现「界面停住但什么都没报错」
- 心跳用 JSON 而不是 SSE 注释（`:` 开头的行浏览器会静默丢弃），前端才能判断「服务活着但事件不流动」，超过 18 秒没收到任何东西就重连

**运行细节**

- 服务只监听 `127.0.0.1`，不对外网开放
- 端口从 `8765` 起试，最多试 24 个，取第一个真正空闲的。Windows 的 `SO_REUSEADDR` 允许绑到**别人正在监听**的端口，所以这里先 `connect` 探活再 bind，避免两个实例抢同一端口
- 浏览器优先 Edge，其次 Chrome；都找不到就用系统默认浏览器打开（此时是普通标签页，不是独立窗口）
- 浏览器用独立 profile，放在 `%LOCALAPPDATA%\WorkBuddyMonitor\webview`，**不碰你日常浏览器的数据**。每次启动建一个 `run-<pid>` 独占目录，从母版 `Preferences` 复制（已关掉同步、登录与默认浏览器推广）——否则上一次的 Edge 若还在后台驻留，Chromium 会把新窗口转交给旧实例再退出，表现成「点了没反应」
- 关掉窗口后十几秒内进程退出（靠 SSE 心跳发现连接已断，不是秒退）；前端一直连不上服务时，45 秒后自行退出，不留后台残留
- 启动过程写在 `%LOCALAPPDATA%\WorkBuddyMonitor\startup.log`，每次启动重写，只看最近一次

## 四、改了源码之后

**双击 exe 跑的是打包进去的那份 `web/index.html` 和 `.py`，不会读磁盘上的源码。** 改完必须重新打包，否则会以为改动「没生效」。

```powershell
python build.py
```

打包脚本做的事情就是原来那条长命令（`--onefile --noconsole --noupx` + manifest + 图标 + 两个 `--add-data`），改成按脚本位置推导路径。想手动跑等价命令：

```powershell
python -m PyInstaller --noconfirm --clean --onefile --noconsole --noupx ^
  --name WorkBuddyMonitor --manifest dpi.manifest --icon workbuddy.ico ^
  --add-data "web\index.html;web" --add-data "workbuddy.ico;." ^
  --distpath . --workpath build --specpath build wbm_web.py
```

三个参数不能省：

- `--manifest dpi.manifest` —— 高 DPI 感知。漏了字体就会糊（进程按 96 DPI 渲染再被系统拉伸）
- `--add-data "...\web\index.html;web"` —— 漏了界面直接 500
- `--add-data "...\workbuddy.ico;."` —— 漏了窗口图标缺失

资源说明：

| 文件 | 用途 |
| --- | --- |
| `workbuddy.ico` | exe 文件图标（含 16/24/32/48/64/128/256 七个尺寸） |
| `web/index.html` | 界面本体，改配色/字号/间距/markdown 转换只动这一个文件 |
| `dpi.manifest` | 声明 PerMonitorV2 DPI 感知，打包必须带 |
| `build.py` | 打包脚本，路径自适应 |
| `icon-preview.png` | 各尺寸效果对照图，仅作参考，删掉不影响运行 |
| `make-icon.py` | 图标生成脚本（需要 Pillow）。三根活动条的几何比例写在开头，改完重跑即可再生成 ico 与预览图 |

图标是**纯图形**（圆角底板 + 三根长短不一的活动条），不含任何文字；`web/index.html` 里的 favicon 与侧栏品牌位用的是同一套比例，任务栏和界面里看到的是同一个符号。

## 五、排查

打包版没有控制台。启动异常会写进 `%LOCALAPPDATA%\WorkBuddyMonitor\startup.log` 并弹一个提示框；页面自己连不上服务时，顶部也会亮一条「未连接到本地服务」的提示。

| 现象 | 原因 |
| --- | --- |
| 画面不动了，状态是「已固定」 | 点过侧栏某条历史会话，跟随被固定住了。点顶栏「取消固定」，或在「项目」框回车清空 |
| 画面不动了，状态是「跟随中」 | 先看「刷新」下拉是不是被设成「关」；再看该会话文件本身是否还在写（被监控方空闲时当然没有新内容） |
| 界面里没有任何项目 / 没有会话 | 数据源 `%USERPROFILE%\.codebuddy\projects` 不存在或为空，先确认被监控方跑过会话 |
| 浏览器显示「拒绝连接」 | 服务已退出：多半是直接打开了 `web/index.html`，或旧实例被结束。看 `startup.log` 最后一行 |
| 页面顶部提示「未连接到本地服务」 | 同样是没有服务在跑，双击 exe 即可 |
| 窗口是空白页 / 写着「缺少 web/index.html」 | 打包漏了 `--add-data "...;web"` |
| 打开的是普通浏览器标签页 | 没找到 Edge/Chrome，退化成默认浏览器 |
| 端口全被占 | 8765–8788 都被占用，关掉占用的程序或改 `PORT_START` |
| 双击 exe 没反应，几秒后消失 | 看 `startup.log`；常见是被旧实例接管，或被杀软拦截 |
| 关窗后进程还在十几秒 | 正常，靠 SSE 心跳判断断连 |

## 许可证

MIT
