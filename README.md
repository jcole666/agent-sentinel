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
| Codex | config.toml notify 自动上报 |
| DeepSeek Harness | 进程检测 + 通用 POST 上报 |
| Claude Desktop / WorkBuddy 国内版 / 国际版 | 进程检测 + 手动标记（GUI 应用无对外接口） |

## 安装与启动

```bash
pip install PySide6-Essentials psutil pillow
python extract_fireball.py   # 从本机 Codex 提取 Fireball 图集（可选，缺图也能跑）
python sentinel.py
```

Windows 下双击 `启动哨兵.vbs` 可静默后台启动（可用环境变量 `SENTINEL_PYTHON` 指定 Python）。
开机自启：把它复制到 `Win+R` → `shell:startup` 打开的文件夹。

## 交互

- 宠物永远置顶；**滚轮缩放**（0.7×～3.0×），或**右键宠物**选预设尺寸，尺寸自动记忆
- **点宠物**：原地展开/收起面板（面板向左展开，宠物不动）；**按住拖动**：移动位置
- 面板里：右键某一行手动标记，双击行=快捷完成，︿ 收回，— 隐藏到托盘

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
