# -*- coding: utf-8 -*-
"""
Codex → Agent 哨兵 上报脚本（兼官方 desktop 转发器）
Codex config.toml: notify = ['<pythonw>', '<此脚本>', "turn-ended"]
事件 JSON 会作为最后一个参数传入；脚本先原样转发给官方
codex-computer-use.exe（保持桌面版集成不变），再上报哨兵。
"""
import glob
import json
import os
import subprocess
import sys
import urllib.request

SERVER = "http://127.0.0.1:17890/api/status"
AGENT = "codex"


def find_official_exe():
    """ 在本机 Codex 运行时目录里找官方通知组件（版本号目录会变） """
    base = os.path.expandvars(r"%LOCALAPPDATA%\OpenAI\Codex\runtimes\cua_node")
    pattern = os.path.join(base, "*", "bin", "node_modules", "@oai", "sky",
                           "bin", "windows", "codex-computer-use.exe")
    hits = glob.glob(pattern)
    return sorted(hits)[-1] if hits else None


def report(state, task=""):
    body = json.dumps({"agent": AGENT, "state": state, "task": task}).encode("utf-8")
    req = urllib.request.Request(SERVER, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def forward_to_official(args):
    exe = find_official_exe()
    if not exe:
        return
    try:
        subprocess.run([exe] + args, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def main():
    args = sys.argv[1:]
    forward_to_official(args)  # 保持 Codex 桌面版原有通知行为

    raw = args[-1] if args else "{}"
    try:
        event = json.loads(raw)
    except Exception:
        event = {}
    etype = event.get("type", "")
    if etype == "agent-turn-complete":
        report("done", (event.get("last-assistant-message") or "回合完成")[:80])
    elif etype == "approval-requested":
        report("approval", "Codex 请求审批")
    else:
        report("working", etype or "运行中")


if __name__ == "__main__":
    main()
