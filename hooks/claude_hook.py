# -*- coding: utf-8 -*-
"""
Claude Code → Agent 哨兵 上报 hook
读取 stdin 的 hook JSON，映射为状态后 POST 到本地哨兵服务。
配置方式见 README.md（写入 ~/.claude/settings.json 的 hooks 段）。
"""
import json
import sys
import urllib.request

SERVER = "http://127.0.0.1:17890/api/status"
AGENT = "claude-code"

EVENT_MAP = {
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "PermissionRequest": "approval",
    "Notification": "approval",
    "Stop": "done",
    "SubagentStop": "done",
    "SessionEnd": "idle",
}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    event = payload.get("hook_event_name", "")
    state = EVENT_MAP.get(event)
    if not state:
        return

    task = ""
    if event == "UserPromptSubmit":
        task = (payload.get("prompt") or "").strip().splitlines()[0][:80]
    elif event in ("PreToolUse", "PostToolUse", "PermissionRequest"):
        tool = payload.get("tool_name", "")
        task = f"调用工具: {tool}"
    elif event == "Notification":
        task = (payload.get("message") or "需要你的关注")[:80]

    body = json.dumps({"agent": AGENT, "state": state, "task": task,
                       "detail": event}).encode("utf-8")
    req = urllib.request.Request(SERVER, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # 哨兵没启动就静默跳过，不影响 Claude Code


if __name__ == "__main__":
    main()
