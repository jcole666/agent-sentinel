# Agent 哨兵（Agent Sentinel）

一个常驻桌面的**宠物形态**悬浮小工具，集中显示本机多个 AI Agent 的实时状态：
**哪些在跑、在做什么任务、是否完成、是否需要审批**。

默认宠物是 Codex 桌面版的内置宠物 **Fireball**（官方精灵图动画，需本机装有 Codex 后用
`extract_fireball.py` 提取生成，素材不随仓库分发）。

状态 → 官方动画行：

| 状态 | 动画 |
|---|---|
| 空闲 idle | 缓慢呼吸 |
| 工作中 working | running 行 |
| 待审批 approval | waiting 行 + 右上角静态红角标 |
| 已完成 done | review 行 |
| 出错 error | failed 行 |

## 监控对象（可在 `config.json` 里改）

| Agent | 监控方式 |
|---|---|
| Claude Code | hooks 自动上报（任务/工具/待审批/完成） |
| Codex | **本地回合库监听**（实时，含任务标题）+ config.toml notify 兜底 |
| DeepSeek Harness | **TCP 连接监听**（生成回复必有 API 长连接，空闲即不显示任务） |
| WorkBuddy 国内版 / 国际版 | **本地 SQLite 会话表监听**（自动显示会话标题与状态） |
| Claude Desktop | **本地会话 JSON + 回合记录监听**（实时，含会话标题） |
| Cursor | **agent 回合记录监听**（`~/.cursor` 下的 transcript，实时，含提问摘要） |
| Grok Bot | **进程 CPU 活动检测**（对话在远端沙箱，本地无实时状态落盘） |

## 安装与启动

```bash
pip install PySide6-Essentials psutil pillow
python extract_fireball.py   # 从本机 Codex 提取 Fireball 图集（可选，缺图也能跑）
python sentinel.py
```

Windows 下双击 `启动哨兵.vbs` 可静默后台启动（`python make_launcher.py` 可重新生成）。

### Windows 启动器说明（踩过的坑）

- **路径解析不走环境变量**。`setx` 写入的变量，对已经运行的 `explorer.exe` 不可见，
  所以双击快捷方式时 `%SENTINEL_HOME%` 根本没被展开，脚本会跑到 vbs 所在目录找
  `sentinel.py`，找不到。现在改为优先读注册表 `HKCU\Environment\SENTINEL_HOME`，
  再退回 vbs 同目录、最后硬编码项目路径。
- **.vbs 必须存成 GBK/ANSI**。Windows Script Host 按 ANSI 读取脚本，UTF-8 写的中文
  路径会变成乱码；纯 ASCII 又不方便写死中文路径。`make_launcher.py` 用 `encoding="gbk"`
  输出。
- **已经有一个实例在跑时双击**：不再起第二个进程，而是请求 `/api/show` 把已有窗口
  叫回前台。
- **`pythonw.exe` 没有控制台**，启动失败是完全静默的。启动器会在 6 秒后探测
  `127.0.0.1:17890`，起不来才弹窗，并给出 `sentinel-launch.log` / `sentinel-error.log`
  的位置。

开机自启：把 `启动哨兵.vbs` 复制到 `Win+R` → `shell:startup` 打开的文件夹。

## 交互

- **安静模式**（默认开启）：空闲时静止 2.6 秒才缓慢呼吸一轮，干活/待审批动画整体放慢 1.6～2 倍，刷新率降到 12fps——不吵。想恢复官方原速，把 `config.json` 里的 `pet.calm` 改成 `false`
- 宠物永远置顶；**滚轮缩放**（0.7×～3.0×），或**右键宠物**选预设尺寸，尺寸自动记忆
- **点宠物**：原地展开/收起面板（面板向左展开，宠物不动）；**按住拖动**：移动位置
- 面板里：**鼠标悬停**某一行会浮起（描边变亮 + 跟随鼠标的柔光）；右键某一行手动标记，双击行=快捷完成，︿ 收回，— 隐藏到托盘
- 面板**始终整窗落在屏幕可用区内**：任务变多把窗口撑高时，会自动上移让出屏幕底边（不会让 Windows 把最后一行切掉）；任务减少后再回到你原来的位置

## 界面：液态玻璃

每个 Agent 是一张独立的玻璃卡片（不是表格行），整块面板是磨砂玻璃底。
收起状态下窗口只有小球、不覆盖面板区域，所以会**提前把展开后要用的背景拍好**——
点开面板就是成品，不会先白一下再变。

**为什么不用 Win11 原生亚克力**：`SetWindowCompositionAttribute` 的 acrylic/blurbehind
会把模糊铺满**整个窗口矩形**，既不会跟随圆角裁剪（圆角外是方的），也吃不到 `setMask`
——宠物区域会变成一块磨砂方块，白色 tint 还强到看不见模糊。实测后放弃。

**现在的做法**：自己抓背景 + 自己模糊。

1. 展开面板或拖动结束时，用 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` 把本窗口
   从**屏幕捕获**里排除（Win10 2004+），窗口本身一直可见 → 抓图这几毫秒用户看不出任何变化；
2. `QScreen.grabWindow(0, …)` 抓面板那一块桌面，抓完立刻解除排除；
3. 逐级降采样到 1/16 再放大回原尺寸（≈高斯模糊，比真高斯快得多）；
4. 贴到 `GlassPanel` 最底层，再叠白霜、左上角柔光、内阴影、1px 内高光 + 外描边。

所以玻璃里看到的是**抓拍那一刻的背景快照**——静态桌面完全看不出区别；背后放视频/快速滚动的
内容时会是轻微滞后的模糊。抓不到背景（异常）会自动退化成纯色玻璃，不会崩。

卡片 hover 用三件事表达，任何底色上都看得出来：填充不透明度 60→172、一圈冷色细边、
跟随鼠标位置的柔光；静止态刻意做得很轻（60/255），面板才像一整块玻璃而不是八条白杠。

### 展开/收起必须“一次 setGeometry”

窗口收起时只有小球（95×95），展开后是 404×554，而且**右边缘固定、向左长**。
早期写法是 `resize()` 再 `move()`，看着只是一前一后两行代码，实际是**两次原生窗口变更**，
Windows/DWM 会把它们各合成一帧：

| 时刻 | 窗口矩形（物理像素） | 用户看到 |
| --- | --- | --- |
| 0.0 ms | `(2151, 693, 143, 143)` | 小球 |
| 496.3 ms | `(2151, 693, 606, 143)` | 宽度先涨，右边缘甩到屏幕外 |
| 498.9 ms | `(1688, 672, 606, 852)` | 才挪到位 |
| 1400.3 ms | `(1688, 672, 143, 143)` | 收起时按左上角缩 → **小球瞬移到面板左边缘** |
| 1402.8 ms | `(2151, 672, 143, 143)` | 再弹回右边 |

「点一下小火人，他先跑到左边又马上回来」就是这么来的。修法：

- 先算好目标矩形，`setGeometry(rect)` **一次**提交位置+尺寸；
- 尺寸约束要**先放开、最后才收紧**——`setFixedSize/setMaximumSize` 在窗口可见时会把当前尺寸
  就地夹进新约束（Qt 内部就是 `resize()`），那同样是一次中间态；
- 高度**不要**用 `self.sizeHint()` 量：那会激活根布局，布局激活时会把窗口拉到“最小尺寸”，
  又是一次就地 resize。只问 `panel.sizeHint()`（面板不是窗口，不会反推整窗）；
- 收起时回到记下的 `_collapsed_geo`，而不是“当前顶边”——否则展开时被屏幕底边顶上去的那
  二十几像素会被带进收起态，小球回不到原位。

打点实测（`paintEvent` 里记时间戳）：几何变更后 **1.3ms** 小球已经画在 `ballx=309 / winw=404`
的正确位置、面板 2.3ms 到位，收起 **1.4ms** 变成 `ballx=0 / winw=95` —— 全部远小于一帧
（16.7ms），合成器根本看不到中间态。连续 6 次切换，几何恒为
`(2400,303,143,143) ↔ (1937,303,606,831)`，零漂移。

> 踩过的坑：抓背景时不能用 `hide()` 整个窗口——Windows 会顺手把窗口挪回「常规位置」，
> 表现为面板莫名跳屏；也不能用 `setUpdatesEnabled(False)` 抑制绘制——抑制期间改尺寸只会
> 标记部分脏区，恢复后上半截留着透明、背后内容直接透出来。
>
> 附带一个副作用要记住：**排除捕获期间截图是拍不到哨兵的**。拿截图去验证点击瞬间的动画会
> 得到“窗口凭空消失”的假象，那种情况只能靠程序内打点。

## 网络连接监听（DeepSeek Harness）

DeepSeek Harness 是纯云端应用：会话不落本地盘、没有 hook、窗口标题在对话结束后也不会变
（所以标题/CPU 方案会错误地一直显示「工作中」）。

但它的工作方式决定了：**流式生成回复期间，进程树必然持有一条到 API 端点（或本地代理）
的 ESTABLISHED TCP 连接；空闲时只剩进程间 IPC**。哨兵每 3 秒扫一次它的 TCP 连接：

- 出现外部连接（连续两轮确认，防抖）→ **工作中**，任务名取窗口标题里那段
- 连接消失 → **已完成**（保留约 1 分钟缓冲），随后**回到空闲、不再显示任务**
- 进程退出 → 清空

进程间连接的识别：回环且远端端口属于本进程树的监听端口或任一本地端口 → 视为 IPC 排除。

配置在 `config.json` 的 `watch_net`：

```json
"watch_net": { "process": "deepseek harness.exe", "title_suffix": "DeepSeek Harness", "done_seconds": 60 }
```

只读系统连接表，不解密、不抓包，只看连接是否存在。

## 窗口标题监听（备用方案，给无网络特征的 GUI 应用）

某些应用的窗口标题会带当前会话/任务名（例如 `问候 — DeepSeek Harness`）。哨兵每 2 秒扫一次窗口标题：

- 标题带任务名 → 立刻显示为**工作中**，任务名就是标题里那段
- 进程树持续 12 分钟无 CPU 活动 → 自动转为**已完成**（`idle_minutes` 可调）
- 窗口关闭或标题回到纯应用名 → 清空

```json
"watch_window": { "process": "xxx.exe", "title_suffix": "XXX", "idle_minutes": 12 }
```

注意：标题在对话结束后通常不会变，所以这个方案无法区分「还在生成」和「挂着窗口没动」，
只适合作为没有更好信号时的兜底。

## 本地回合库监听（Codex）

Codex 桌面版把每个回合的实时状态写在本地 sqlite：`thread_history_*.sqlite` 的
`thread_turns` 表（`status` = `inProgress` / `completed`），配合 `state_*.sqlite` 的
`threads` 表拿会话标题。哨兵每 3 秒只读查询一次：

- 有 `inProgress` 回合 → 立刻显示**工作中** + 会话标题（notify 只在回合结束时触发，做不到这点）
- 回合刚完成（3 分钟内）→ 已完成
- 超过 `active_minutes` 没动静 → 移除

```json
"watch_turns": { "turns_db_glob": "%USERPROFILE%\\.codex\\thread_history_*.sqlite",
                 "threads_db_glob": "%USERPROFILE%\\.codex\\state_*.sqlite",
                 "active_minutes": 10, "done_minutes": 3 }
```

只读打开，只取 id/状态/时间/标题，不读会话内容。

## 本地会话库监听（WorkBuddy）

WorkBuddy 把会话元数据写在本地 SQLite 里（`sessions` 表有 `title` / `status` / `last_activity_at`）。
哨兵每 3 秒只读查询一次，显示**正在进行的会话标题和状态**，不需要 Agent 配合上报，
旧会话继续聊也会自动更新。

```json
"watch_db": { "path": "%USERPROFILE%\\.workbuddy\\workbuddy.db", "active_minutes": 8 }
```

- 国内版：`%USERPROFILE%\.workbuddy\workbuddy.db`；国际版：`%USERPROFILE%\.workbuddy-ai\workbuddy.db`
- 只读打开，**只取 id/标题/状态/时间，不读任何会话内容**
- `active_minutes`：多久没有活动就认为该会话已结束

## 本地会话 JSON 监听（Claude Desktop）

Claude Desktop 的本地 agent 会话会写到磁盘，不需要它配合上报：

- 会话元数据：`%LOCALAPPDATA%\Claude-3p\local-agent-mode-sessions\*\00000000\local_<uuid>.json`
  里面有 `title` / `lastActivityAt` / `isArchived`
- 回合记录：`<会话短 id>\.claude\projects\session\*.jsonl`，**实时追加**

判断「进行中」的关键：一个回合执行期间，jsonl 会每 30～120 秒追加一条 `system` 心跳；
只有回合结束才会追加一条带 `stop_reason` 的 `assistant` 记录。
所以哨兵看文件末尾——最后一条是 `assistant`（有 `stop_reason`）就判已完成，否则判进行中。

```json
"watch_json": {
  "session_glob": [
    "%LOCALAPPDATA%\\Claude-3p\\local-agent-mode-sessions\\*\\00000000\\local_*.json",
    "%LOCALAPPDATA%\\Claude-3p\\session-sync\\*\\local_*.json"
  ],
  "active_seconds": 150,
  "done_minutes": 20
}
```

- `active_seconds`：末尾记录的时效窗口（要大于心跳间隔，默认 150 秒）
- `done_minutes`：回合结束后「已完成」状态保留多久
- 只读，**只取标题与时间，不读消息正文**

> 注意：只覆盖 Claude Desktop 的**本地 agent 模式**会话。纯云端聊天不会在本地落盘，哨兵无法感知。

## agent 回合记录监听（Cursor）

Cursor 的 agent 把每回合的完整过程写到本地，格式和 Claude Code 的 transcript 一致：

```
%USERPROFILE%\.cursor\projects\<项目>\agent-transcripts\<uuid>\<uuid>.jsonl
```

- 每行形如 `{"role":"user","message":{"content":[{"type":"text","text":"..."}]}}`
- 首个用户消息里正文包在 `<user_query>` 标签中，哨兵拿它当任务标题
- **回合结束会追加最后一行** `{"type":"turn_ended","status":"success|error"}`；
  还没出现 `turn_ended` 就说明 agent 还在跑（持续追加 tool_use / tool_result）

```json
"watch_cursor": {
  "transcript_glob": "%USERPROFILE%\\.cursor\\projects\\*\\agent-transcripts\\*\\*.jsonl",
  "active_seconds": 300,
  "done_minutes": 10
}
```

- `active_seconds`：transcript 的静默超时（超过就认为没在跑）
- `done_minutes`：回合结束后「已完成」保留多久
- 一个 transcript = 一个任务，多项目并行时能同时挂多条

> 性能：单个 transcript 实测可达 267KB。全量读所有文件每 3 秒一轮太浪费，
> 所以先用 `mtime` 把明显过期的文件直接跳过（连打开都不打开），
> 再**只读尾部**判断 `turn_ended`、**只读头部**提取任务标题。

## 进程 CPU 活动检测（Grok Bot）

Grok Bot（内部代号 sand）的对话跑在**远端沙箱**里，本地拿不到实时会话状态。
实测下来，四条常见路子都走不通：

| 尝试 | 结果 |
|---|---|
| 读会话 blob | `sand-client-persistence\*.blob` 只在应用**启动/退出**时 flush（asar 里确认是 `will-quit` 时落盘），生成过程中不会实时更新 |
| 读 Local Storage leveldb | `Partitions\sand-*\Local Storage\leveldb\000003.log` 同样只在启动时写，运行期间不再更新 |
| 监听 TCP 连接 | 空闲时就常驻 12 条到 AWS / Cloudflare 的 HTTPS 连接，连接数无法区分忙闲 |
| 窗口标题 | 恒为 `Grok Bot`，不随对话变化 |

**但 CPU 是干净的**：空闲时整棵进程树的 CPU 时间几乎为 0。
所以判定逻辑走「进程树 CPU / 磁盘 IO 增量 + 滑动窗口」。

### 空闲基线（15 分钟实测，300 轮 × 3 秒）

这是定阈值的依据。空闲时 Electron 遥测会造成**周期性 IO 尖峰**，
单看单轮数据会误判，必须用滑动窗口抹平：

| 指标 | 单轮 p50 | 单轮 p90 | 单轮 max | 3 轮窗口均值 max |
|---|---|---|---|---|
| CPU (ms) | 0 | 16 | 141 | **52** |
| 磁盘 IO (字节) | 6906 | 8414 | 78887 | **48438** |

> 旧阈值 `cpu>=120ms 或 io>=20000`（单轮）就是被这些尖峰击穿的：
> 15 分钟里误报 6 次，其中 14:18 那次是**连续两轮**同时超标，
> 会真的把 Grok 显示成「工作中」。这正是「空闲却一直亮着」的根因。

### 当前判定

- 3 轮（9 秒）滑动窗口平均 CPU ≥ `cpu_avg_ms`（默认 90ms）→ 命中
- 或 3 轮窗口平均 IO ≥ `io_avg_bytes`（默认 80000 字节）→ 命中
- 连续 `confirm_rounds`（默认 2）轮窗口命中才置为 **工作中**，滤掉单点尖峰
- 停止后维持 `active_hold_seconds`（9 秒）→ **已完成**，
  再保持 `done_seconds`（45 秒）→ 回到**空闲**并清空任务

余量（相对空闲窗口峰值）：CPU 1.72x、IO 1.65x。
离线回放 15 分钟真实空闲数据：**0 次误报**；活动开始后约 **6 秒**进入 working。

```json
"watch_grok": {
  "process": "grok bot.exe",
  "window_rounds": 3,
  "cpu_avg_ms": 90,
  "io_avg_bytes": 80000,
  "confirm_rounds": 2,
  "done_seconds": 45,
  "active_hold_seconds": 9,
  "blob_dir": "%APPDATA%\\Grok Bot\\sand-client-persistence"
}
```

任务标题从 blob 里取最近一条用户提问（正文在 `richText` 的 ProseMirror
文档 JSON 里，需要递归找 `type: "text"` 节点）。blob 更新有延迟，
仅用于显示标题，不参与状态判定。

> 坑：blob 路径超 260 字符，必须用 `\\?\` 长路径前缀访问。
> 且这个前缀要**幂等**——`scandir` 在长路径目录下返回的条目路径本身已带前缀，
> 再套一次会变成 `\\?\UNC\?\C:\...` 而打不开。
>
> 坑：Windows 上 `psutil` 的 `io_counters()` 里 `rchar`/`wchar` 恒为 0，
> 只有 `read_bytes`/`write_bytes`（磁盘 IO）有效，别指望用它们测网络流量。

## 上报接口

本地 HTTP：`POST http://127.0.0.1:17890/api/status`（允许跨域，方便网页直接调）

```json
{ "agent": "claude-code", "state": "working", "task": "重构 auth 模块", "task_id": "sess-A" }
```

读取 / 控制：

| 接口 | 作用 |
|---|---|
| `GET /api/state` | 全部 Agent 的完整状态（含每个任务、进程是否在跑） |
| `GET /api/ping` | 探活 |
| `GET /api/show` | 把已运行的窗口叫回前台（不会开第二个实例） |
| `GET /api/toggle` | 展开/收起面板（脚本或二次开发用） |

- `agent`：`config.json` 里的 id
- `state`：`idle` / `working` / `approval` / `done` / `error`
- `task_id`：同一 Agent 可挂**多个任务**；行首显示最紧急的一条，其余以缩行列出（最多再 3 条）。带 `task_id` 发 `idle` 结束单个任务；不带则清空该 Agent 全部任务
- 每个 Agent 最多保留 5 条任务，超过 6 小时自动清理

## Claude Code 接入（自动上报）

合并进 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python <本仓库>/hooks/claude_hook.py" }] }],
    "PreToolUse":       [{ "hooks": [{ "type": "command", "command": "python <本仓库>/hooks/claude_hook.py" }] }],
    "Notification":     [{ "hooks": [{ "type": "command", "command": "python <本仓库>/hooks/claude_hook.py" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python <本仓库>/hooks/claude_hook.py" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "python <本仓库>/hooks/claude_hook.py" }] }]
  }
}
```

## Codex 接入（自动上报）

`~/.codex/config.toml`：

```toml
notify = [ "pythonw", "<本仓库>/hooks/codex_notify.py", "turn-ended" ]
```

脚本会把事件转发给 Codex 官方 desktop 通知组件（自动定位 `codex-computer-use.exe`），再上报哨兵，原功能不受影响。

## 文件

- `sentinel.py` — 主程序（宠物悬浮窗 + HTTP 服务 + 进程监控）
- `config.json` — Agent 清单与进程匹配规则
- `extract_fireball.py` — 从本机 Codex 提取 Fireball 精灵图
- `hooks/` — Claude Code / Codex 上报脚本
- `启动哨兵.vbs` — Windows 静默启动器
- `state.json` — 运行时状态（自动生成，已 gitignore）

## 备注

- 端口默认 `17890`（避免与本机代理常用端口 7890 冲突）
- Fireball 素材版权属 OpenAI，本仓库不分发；`extract_fireball.py` 只在你本机从你自己的 Codex 安装里提取
- 上报全走 127.0.0.1，纯本地，不上传任何数据

License: MIT
