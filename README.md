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

## 安装与启动

```bash
pip install PySide6-Essentials psutil pillow
python extract_fireball.py   # 从本机 Codex 提取 Fireball 图集（可选，缺图也能跑）
python sentinel.py
```

Windows 下双击 `启动哨兵.vbs` 可静默后台启动（可用环境变量 `SENTINEL_PYTHON` 指定 Python）。
开机自启：把它复制到 `Win+R` → `shell:startup` 打开的文件夹。

## 交互

- **安静模式**（默认开启）：空闲时静止 2.6 秒才缓慢呼吸一轮，干活/待审批动画整体放慢 1.6～2 倍，刷新率降到 12fps——不吵。想恢复官方原速，把 `config.json` 里的 `pet.calm` 改成 `false`
- 宠物永远置顶；**滚轮缩放**（0.7×～3.0×），或**右键宠物**选预设尺寸，尺寸自动记忆
- **点宠物**：原地展开/收起面板（面板向左展开，宠物不动）；**按住拖动**：移动位置
- 面板里：右键某一行手动标记，双击行=快捷完成，︿ 收回，— 隐藏到托盘

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

## 上报接口

本地 HTTP：`POST http://127.0.0.1:17890/api/status`

```json
{ "agent": "claude-code", "state": "working", "task": "重构 auth 模块", "task_id": "sess-A" }
```

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
