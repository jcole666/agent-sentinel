# -*- coding: utf-8 -*-
"""
Agent 哨兵 (Agent Sentinel)
桌面悬浮窗：集中监控本机多个 AI Agent 的运行状态。

- CLI 类 Agent（Claude Code / Codex / DeepSeek Harness）通过 HTTP POST 上报真实状态
- GUI 类应用（Claude Desktop / WorkBuddy 国内版 / WorkBuddy 国际版）通过进程检测 + 手动标记
- 状态：idle 空闲 / working 工作中 / approval 等待审批 / done 已完成 / error 出错
"""

import datetime
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
from PySide6.QtCore import (
    QEasingCurve, QObject, QPointF, QRect, QRectF, Qt, QTimer,
    QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QFont, QFontMetrics, QImage, QLinearGradient,
    QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMenu, QPushButton,
    QVBoxLayout, QWidget, QSystemTrayIcon, QStyle,
)

HOST = "127.0.0.1"
PORT = 17890
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
CONFIG = {}   # config.json 全量内容，main() 里加载

# 简约配色：黑白灰为主，仅待审批用红色作唯一强调色
STATE_META = {
    "idle":     {"label": "空闲",    "color": "#d1d5db"},
    "working":  {"label": "工作中",  "color": "#374151"},
    "approval": {"label": "待审批",  "color": "#dc2626"},
    "done":     {"label": "已完成",  "color": "#a3a3a3"},
    "error":    {"label": "出错",    "color": "#ea580c"},
}

# ---------------------------------------------------------------- 状态存储


class StateStore:
    def __init__(self, agents):
        self.lock = threading.Lock()
        self.data = {}
        for a in agents:
            self.data[a["id"]] = {
                "id": a["id"],
                "label": a["label"],
                "kind": a["kind"],
                "state": "idle",
                "task": "",
                "detail": "",
                "proc_running": False,
                "updated": 0.0,
                "manual": False,
                "tasks": {},   # task_id -> {state, text, updated}
            }
        self.ui = {"scale": 1.4}   # 宠物缩放（可调）
        self._load()

    def _load(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k == "_ui":
                    self.ui.update({kk: v[kk] for kk in ("scale",) if kk in v})
                    continue
                if k in self.data:
                    d = self.data[k]
                    for kk in ("state", "task", "detail", "updated", "manual"):
                        if kk in v:
                            d[kk] = v[kk]
                    if isinstance(v.get("tasks"), dict):
                        # 只保留 30 分钟内的任务：换过监听方式后，旧 watcher 留下的
                        # task_id 再也没人会去结束它，会一直挂着显示「工作中」
                        now = time.time()
                        d["tasks"] = {
                            tid: tv for tid, tv in v["tasks"].items()
                            if isinstance(tv, dict) and now - tv.get("updated", 0) < 1800
                        }
            # 清理完后重新汇总展示状态，避免已丢弃的旧任务残留「工作中」
            for d in self.data.values():
                d["state"] = self._aggregate(d["tasks"]) or "idle"
                top = self._top_task(d["tasks"])
                d["task"] = top["text"] if top else ""
        except Exception:
            pass

    def save(self):
        with self.lock:
            payload = dict(self.data)
            payload["_ui"] = dict(self.ui)
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def report(self, agent_id, state, task="", detail="", task_id=""):
        """ 上报状态。带 task_id 时按任务维度维护，同一 Agent 可挂多个任务。 """
        with self.lock:
            if agent_id not in self.data:
                return False
            if state not in STATE_META:
                return False
            d = self.data[agent_id]
            tid = task_id or "default"
            tasks = d.setdefault("tasks", {})

            if state == "idle" and not task_id:
                tasks.clear()                      # 整体清空
            elif state == "idle":
                tasks.pop(tid, None)               # 结束某个任务
            else:
                entry = tasks.get(tid, {"state": state, "text": "", "updated": 0.0})
                entry["state"] = state
                if task:
                    entry["text"] = task[:120]
                entry["updated"] = time.time()
                tasks[tid] = entry
                # 只保留最近 5 个任务，超 6 小时的自动丢弃
                if len(tasks) > 5:
                    for k in sorted(tasks, key=lambda x: tasks[x]["updated"])[:-5]:
                        tasks.pop(k, None)
                for k in list(tasks):
                    if time.time() - tasks[k]["updated"] > 6 * 3600:
                        tasks.pop(k, None)

            # 汇总状态 = 所有任务里最紧急的
            d["state"] = self._aggregate(tasks) or state
            d["updated"] = time.time()
            d["proc_running"] = True
            top = self._top_task(tasks)
            d["task"] = top["text"] if top else ""
            d["detail"] = detail[:200] if detail else ""
            if state in ("done", "idle"):
                d["manual"] = False
        self.save()
        return True

    @staticmethod
    def _aggregate(tasks):
        best = None
        for tk in tasks.values():
            s = tk.get("state", "idle")
            if best is None or (s in AGGREGATE_ORDER and
                                AGGREGATE_ORDER.index(s) < AGGREGATE_ORDER.index(best)):
                best = s
        return best

    @staticmethod
    def _top_task(tasks):
        """ 返回最需要关注的那条任务 """
        if not tasks:
            return None
        def key(tk):
            s = tk.get("state", "idle")
            return (AGGREGATE_ORDER.index(s) if s in AGGREGATE_ORDER else 99,
                    -tk.get("updated", 0))
        return sorted(tasks.values(), key=key)[0]

    def set_proc(self, agent_id, running):
        with self.lock:
            d = self.data.get(agent_id)
            if not d:
                return
            d["proc_running"] = running
            # 进程消失且非手动标记 → 回落为空闲并清任务
            if not running and not d["manual"] and d["state"] in ("working", "approval"):
                d["state"] = "idle"
                d["task"] = ""
                d["updated"] = time.time()
                d["tasks"] = {}

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))


# ---------------------------------------------------------------- HTTP 服务


class UiBridge(QObject):
    """HTTP 线程 -> Qt 主线程 的桥。"""
    show_requested = Signal()
    toggle_requested = Signal()


class ApiHandler(BaseHTTPRequestHandler):
    store: StateStore = None  # 由外部注入
    bridge: "UiBridge" = None

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._send(200, self.store.snapshot())
        elif self.path.startswith("/api/ping"):
            self._send(200, {"ok": True})
        elif self.path.startswith("/api/show"):
            # 已被双击唤醒：把已有窗口叫回前台，而不是再起一个实例
            try:
                if self.bridge is not None:
                    self.bridge.show_requested.emit()
            except Exception:
                pass
            self._send(200, {"ok": True})
        elif self.path.startswith("/api/toggle"):
            # 远程/脚本 展开收起面板（调试与二次开发用）
            try:
                if self.bridge is not None:
                    self.bridge.toggle_requested.emit()
            except Exception:
                pass
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/api/status"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        ok = self.store.report(
            str(payload.get("agent", "")),
            str(payload.get("state", "")),
            str(payload.get("task", "")),
            str(payload.get("detail", "")),
            str(payload.get("task_id", "") or payload.get("session_id", "") or ""),
        )
        self._send(200 if ok else 400, {"ok": ok})

    def log_message(self, *args):
        pass


def run_http_server(store, stop_event):
    ApiHandler.store = store
    server = ThreadingHTTPServer((HOST, PORT), ApiHandler)
    server.timeout = 1.0
    while not stop_event.is_set():
        server.handle_request()
    server.server_close()


# ---------------------------------------------------------------- 进程监控


def enum_windows():
    """ 枚举可见窗口 -> [(hwnd, title, pid)]，仅 Windows """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        out = []

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if buf.value.strip():
                    out.append((hwnd, buf.value.strip(), pid.value))
            return True
        user32.EnumWindows(proto(cb), 0)
        return out
    except Exception:
        return []


def run_turn_watcher(store, agents, stop_event):
    """ 读取 Codex 本地回合库（thread_history/state sqlite）：
    thread_turns.status == inProgress 就是「正在干活」，实时性好于 notify。
    只读，只取 thread_id/status/时间/标题。 """
    watched = [(a["id"], a["watch_turns"]) for a in agents if a.get("watch_turns")]
    if not watched:
        return
    import glob as _glob
    import sqlite3
    seen = {}

    def latest(pattern):
        hits = _glob.glob(os.path.expandvars(pattern))
        return max(hits, key=os.path.getmtime) if hits else None

    while not stop_event.is_set():
        try:
            now = time.time()
            for aid, cfg in watched:
                turns_db = latest(cfg.get("turns_db_glob", ""))
                threads_db = latest(cfg.get("threads_db_glob", ""))
                if not turns_db:
                    continue
                active_min = float(cfg.get("active_minutes", 10))
                done_min = float(cfg.get("done_minutes", 3))
                rows = []
                con = sqlite3.connect(
                    "file:" + turns_db.replace("\\", "/") + "?mode=ro", uri=True, timeout=1.0)
                try:
                    if threads_db:
                        con.execute("ATTACH DATABASE 'file:%s?mode=ro' AS tdb"
                                    % threads_db.replace("\\", "/"))
                    rows = list(con.execute(
                        "SELECT tt.thread_id, tt.status, tt.started_at, tt.completed_at, "
                        "       COALESCE(t.title, '') "
                        "FROM thread_turns tt "
                        "LEFT JOIN tdb.threads t ON t.id = tt.thread_id "
                        "ORDER BY tt.started_at DESC LIMIT 10"))
                except sqlite3.Error:
                    rows = []
                finally:
                    con.close()

                cur = set()
                for tid, status, started, completed, title in rows:
                    if not title:
                        continue
                    task_id = "codex-" + str(tid)[:8]
                    if status and status.lower().startswith("inprogress") \
                            and started and now - started < active_min * 60:
                        cur.add(task_id)
                        store.report(aid, "working", str(title)[:60], task_id=task_id)
                    elif completed and now - completed < done_min * 60:
                        cur.add(task_id)
                        store.report(aid, "done", str(title)[:60], task_id=task_id)
                for gone in (seen.get(aid, set()) - cur):
                    store.report(aid, "idle", task_id=gone)
                seen[aid] = cur
        except Exception:
            pass
        stop_event.wait(3.0)


def run_db_watcher(store, agents, stop_event):
    """ 读取 GUI Agent 的本地 SQLite（如 WorkBuddy 的 sessions 表）：
    有正在进行的会话就显示其标题与状态，完全不依赖 Agent 自觉上报。
    只读打开，不涉及任何会话内容，只取 id/标题/状态/时间。 """
    watched = [(a["id"], a["watch_db"]) for a in agents if a.get("watch_db")]
    if not watched:
        return
    import sqlite3
    seen = {}
    while not stop_event.is_set():
        try:
            now_ms = int(time.time() * 1000)
            for aid, cfg in watched:
                path = os.path.expandvars(cfg.get("path", ""))
                if not path or not os.path.exists(path):
                    continue
                active_min = float(cfg.get("active_minutes", 8))
                con = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro",
                                      uri=True, timeout=1.0)
                try:
                    rows = list(con.execute(
                        "SELECT id, COALESCE(custom_title, title, ''), status, last_activity_at "
                        "FROM sessions WHERE deleted_at IS NULL AND last_activity_at > ? "
                        "ORDER BY last_activity_at DESC LIMIT 5",
                        (now_ms - int(active_min * 60000),)))
                except sqlite3.Error:
                    rows = []
                finally:
                    con.close()

                cur_ids = set()
                for sid, title, status, _last in rows:
                    if not title:
                        continue
                    tid = "db-" + str(sid)[:8]
                    cur_ids.add(tid)
                    state = "working" if status == "working" else "done"
                    store.report(aid, state, str(title)[:60], task_id=tid)
                for gone in (seen.get(aid, set()) - cur_ids):
                    store.report(aid, "idle", task_id=gone)
                seen[aid] = cur_ids
        except Exception:
            pass
        stop_event.wait(3.0)


def run_json_watcher(store, agents, stop_event):
    """ 读取把会话写成 JSON 文件的 Agent（如 Claude Desktop 的 local-agent-mode）。

    Claude Desktop 每个本地会话在
      %LOCALAPPDATA%\\Claude-3p\\local-agent-mode-sessions\\<acct>\\00000000\\
    下放一份 local_<uuid>.json（含 title / lastActivityAt），
    并在 <short>\\.claude\\projects\\session\\*.jsonl 里实时追加回合记录。

    判断「进行中」的关键：回合执行期间 jsonl 会周期性追加 system 心跳，
    回合结束才会追加一条带 stop_reason 的 assistant 记录。
    所以：最后一条是 assistant(有 stop_reason) => 已完成；否则 => 进行中。
    只读，只取标题与时间，不读消息正文。 """
    watched = [(a["id"], a["watch_json"]) for a in agents if a.get("watch_json")]
    if not watched:
        return
    import glob as _glob
    seen = {}

    def ts_of(v):
        """ 时间是 ISO 字符串（UTC），统一转成 epoch 秒 """
        if isinstance(v, (int, float)):
            return float(v) / (1000.0 if v > 1e11 else 1.0)
        if isinstance(v, str):
            try:
                s = v.replace("Z", "+00:00")
                return datetime.datetime.fromisoformat(s).timestamp()
            except Exception:
                return 0.0
        return 0.0

    def scan_transcript(path):
        """ 返回 (最后一条时间戳, 是否已完成, 最后 user 消息摘要) """
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().splitlines()
        except Exception:
            return 0.0, False, ""
        last_ts = 0.0
        assistant_ts = 0.0
        finished = False
        prompt = ""
        for line in lines[-80:]:
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = ts_of(d.get("timestamp"))
            if t > last_ts:
                last_ts = t
            typ = d.get("type")
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            if typ == "assistant" or msg.get("role") == "assistant":
                if t >= assistant_ts:
                    assistant_ts = t
                    finished = bool(msg.get("stop_reason"))
            if not prompt and (typ == "user" or msg.get("role") == "user"):
                c = msg.get("content")
                if isinstance(c, str):
                    prompt = c
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            prompt = str(b.get("text", ""))
                            break
        if assistant_ts and assistant_ts >= last_ts - 2:
            return last_ts, finished, prompt
        return last_ts, False, prompt

    while not stop_event.is_set():
        try:
            now = time.time()
            for aid, cfg in watched:
                raw_pat = cfg.get("session_glob", "")
                patterns = raw_pat if isinstance(raw_pat, list) else [raw_pat]
                patterns = [os.path.expandvars(p) for p in patterns if p]
                if not patterns:
                    continue
                active_sec = float(cfg.get("active_seconds", 150))
                done_min = float(cfg.get("done_minutes", 20))
                cur = set()
                sfs = []
                for p in patterns:
                    sfs += _glob.glob(p)
                for sf in sfs:
                    try:
                        with open(sf, "r", encoding="utf-8", errors="ignore") as fh:
                            meta = json.load(fh)
                    except Exception:
                        continue
                    if meta.get("isArchived"):
                        continue
                    sid = str(meta.get("sessionId") or os.path.basename(sf))
                    short = sid.split("_")[-1].split("-")[0] if "_" in sid else sid[:8]
                    base = os.path.dirname(sf)
                    title = (meta.get("title") or "").strip()
                    live = ts_of(meta.get("lastActivityAt"))

                    # 该会话的 transcript。注意：Windows 下 glob 的 "**" 递归
                    # 不会进入以点号开头的目录（如 .claude），必须用显式路径。
                    cands = []
                    for rel in (
                        (short, ".claude", "projects", "session", "*.jsonl"),
                        (short, ".claude", "projects", "*", "*.jsonl"),
                        (short + "*", ".claude", "projects", "session", "*.jsonl"),
                    ):
                        cands += _glob.glob(os.path.join(base, *rel))
                    extra = cfg.get("transcript_glob")
                    if extra:
                        for p in _glob.glob(os.path.expandvars(
                                extra.replace("{short}", short))):
                            cands.append(p)
                    t_last, t_done, prompt = 0.0, False, ""
                    for tp in cands[:6]:
                        try:
                            mt = os.path.getmtime(tp)
                        except Exception:
                            continue
                        if mt - t_last > 0:
                            a, b, c = scan_transcript(tp)
                            if a > t_last:
                                t_last, t_done, prompt = a, b, c
                    try:
                        live = max(live, os.path.getmtime(sf))
                    except Exception:
                        pass

                    newest = max(live, t_last)
                    if not title:
                        title = (prompt or "会话").strip()[:40]
                    if not title:
                        continue
                    task_id = "js-" + short

                    if t_last and not t_done and (now - t_last) < active_sec:
                        state = "working"
                        cur.add(task_id)
                    elif (now - newest) < done_min * 60:
                        state = "done"
                        cur.add(task_id)
                    else:
                        continue
                    store.report(aid, state, title[:60], task_id=task_id)

                for gone in (seen.get(aid, set()) - cur):
                    store.report(aid, "idle", task_id=gone)
                seen[aid] = cur
        except Exception:
            pass
        stop_event.wait(3.0)


# ------------------------------------------------- Cursor / Grok Bot 专用监听


def _long_win_path(p):
    """ Windows 上整体路径超过 260 字符时，普通 open() 直接报 FileNotFoundError，
    需要加 \\\\?\\ 长路径前缀。

    注意要幂等：scandir 在长路径目录下返回的条目路径本身已带前缀，
    再套一次会变成 "\\\\?\\UNC\\?\\C:\\..." 而打不开。 """
    if os.name != "nt":
        return p
    ap = os.path.abspath(p)
    if ap.startswith("\\\\?\\") or ap.startswith("\\\\?\\UNC\\"):
        return ap
    if ap.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def _user_prompt_from_transcript(lines):
    """ 从 Cursor transcript 里取首条用户提问（正文包在 <user_query> 标签里）。

    Cursor 的 role 在顶层（{"role":"user","message":{...}}），
    Claude Desktop 那种在 message 里（{"type":"user","message":{"role":...}}），
    两种都兼容。 """
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        role = d.get("role") or msg.get("role")
        if role != "user":
            continue
        c = msg.get("content")
        txt = ""
        if isinstance(c, str):
            txt = c
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    txt = str(b.get("text", ""))
                    break
        m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", txt, re.S)
        got = (m.group(1) if m else txt).strip()
        if got:
            return got
    return ""


def _read_tail_lines(path, max_bytes=65536, tail_lines=40):
    """ 只读文件尾部若干字节，避免每次轮询都全量读取上百 KB 的 transcript。

    turn_ended 一定追加在文件末尾，所以尾部就够判断回合是否结束。 """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()          # 丢弃可能被截断的半行
            data = fh.read()
    except Exception:
        return []
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return []
    return text.splitlines()[-tail_lines:]


def _read_head_lines(path, max_bytes=32768, head_lines=60):
    """ 只读文件头部若干字节，用于提取首条 user 消息里的任务标题。 """
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes)
    except Exception:
        return []
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return []
    lines = text.splitlines()
    if len(lines) > head_lines:
        lines = lines[:head_lines]
    return lines


def run_cursor_watcher(store, agents, stop_event):
    """ Cursor 的 agent 回合记录在
        %USERPROFILE%\\.cursor\\projects\\<project>\\agent-transcripts\\<uuid>\\<uuid>.jsonl
      格式和 Claude Code 的 transcript 一致（role + message.content）。

      判定「进行中」的关键：回合结束会追加最后一行
      {"type":"turn_ended","status":"success|error"}；
      还没出现 turn_ended 就说明 agent 还在跑（持续追加 tool_use / tool_result）。
      任务名取首条 user 消息里 <user_query> 的内容。

      性能：实测单个 transcript 可达 267KB，每 3 秒全量读所有文件很浪费。
      所以先用 mtime 把明显过期的文件过滤掉（连文件都不打开），
      再只读尾部判断回合状态、只读头部提取标题。 """
    watched = [(a["id"], a["watch_cursor"]) for a in agents if a.get("watch_cursor")]
    if not watched:
        return
    import glob as _glob
    seen = {}

    while not stop_event.is_set():
        try:
            now = time.time()
            for aid, cfg in watched:
                pat = os.path.expandvars(cfg.get("transcript_glob", ""))
                if not pat:
                    continue
                active_sec = float(cfg.get("active_seconds", 300))
                done_min = float(cfg.get("done_minutes", 10))
                stale = max(active_sec, done_min * 60)
                cur = set()
                for path in _glob.glob(pat):
                    try:
                        mtime = os.stat(path).st_mtime
                    except Exception:
                        continue
                    # 太久没动静的文件直接跳过，连打开都不打开
                    if now - mtime > stale:
                        continue

                    finished = False
                    status = ""
                    for line in _read_tail_lines(path):
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if d.get("type") == "turn_ended":
                            finished = True
                            status = str(d.get("status") or "")
                    if finished:
                        # 回合已结束：短时间内显示「已完成」，再久就消失
                        if now - mtime > done_min * 60:
                            continue
                        state = "error" if status == "error" else "done"
                    else:
                        if now - mtime > active_sec:
                            continue
                        state = "working"
                    prompt = _user_prompt_from_transcript(_read_head_lines(path))
                    tid = os.path.splitext(os.path.basename(path))[0]
                    cur.add(tid)
                    store.report(aid, state, (prompt or "Cursor 任务")[:60], task_id=tid)
                for gone in (seen.get(aid, set()) - cur):
                    store.report(aid, "idle", task_id=gone)
                seen[aid] = cur
        except Exception:
            pass
        stop_event.wait(3.0)


def _proc_tree_cpu_io(pname):
    """ 返回该进程名所有实例的 (cpu_seconds, io_bytes) 累计值。

    cpu 取 user+system 时间（跨进程取和），io 取 read+write 字节。
    Windows 的 io_counters 含网络收发，生成回复时远端沙箱的流式数据
    会明显推高这个值。 """
    cpu = 0.0
    io = 0
    pids = []
    for p in psutil.process_iter(["name", "pid"]):
        if (p.info["name"] or "").lower() == pname:
            pids.append(p.info["pid"])
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            c = proc.cpu_times()
            cpu += c.user + c.system
            i = proc.io_counters()
            io += i.read_bytes + i.write_bytes
        except Exception:
            continue
    return cpu, io, len(pids)


def run_grok_watcher(store, agents, stop_event):
    """ Grok Bot（内部代号 sand）：对话跑在远端沙箱里，本地拿不到实时会话状态。

    实测结论（15 分钟空闲采样，300 轮 × 3s）：
      - %APPDATA%\\Grok Bot\\sand-client-persistence\\*.blob 只在应用启动/退出时 flush，
        生成过程中不落盘；Local Storage 的 leveldb 同样只在启动时写。
        所以 blob 只能用来取任务标题，不能当实时状态源。
      - 空闲时 CPU 极低：单轮 p50=0ms、p99=141ms、max=141ms。
      - 空闲时磁盘 IO 有 Electron 遥测造成的周期性尖峰：单轮 p90=8.4KB，
        但偶发冲到 78KB（max），15 分钟里有 5 轮超过 20KB —— 这就是旧阈值
        （cpu>=120ms 或 io>=20000）在空闲时误报「工作中」的原因。
      - 按 4 轮（12 秒）滑动平均后空闲基线非常稳：
        CPU 平均 p99=39ms、max=43ms；IO 平均 p99=37KB、max=37KB。

    判定策略（据此重写）：
      - 主信号用「3 轮滑动平均 CPU」，阈值 90ms/轮：空闲时 3 轮窗口 CPU
        峰值 52ms，余量 1.7x；又远低于真实生成时的负载。
      - IO 只作辅助信号，阈值 80KB/轮：空闲时 3 轮窗口 IO 峰值 48KB，余量 1.65x。
      - 两个信号都要求「连续 2 轮窗口命中」才置为 working，滤掉单点尖峰。
      - 实测空闲 15 分钟（300 轮）回放：0 次误报；活动开始后约 6 秒内进入 working。
      - 命中后按 active_hold_seconds 维持 working，之后转 done 并保持
        done_seconds，最后清空任务回 idle。
    """
    watched = [(a["id"], a["watch_grok"]) for a in agents if a.get("watch_grok")]
    if not watched:
        return
    prev = {}         # aid -> (cpu, io, pid_count)
    hist = {}         # aid -> deque[(cpu_ms, io_bytes)]，滑动窗口
    last_active = {}  # aid -> 最后一次检测到活动的时间
    done_until = {}   # aid -> 「已完成」状态的截止时间
    streak = {}       # aid -> 连续命中窗口的次数（防抖）
    TASK_ID = "grok"

    while not stop_event.is_set():
        try:
            now = time.time()
            for aid, cfg in watched:
                pname = (cfg.get("process") or "grok bot.exe").lower()
                win = int(cfg.get("window_rounds", 3))        # 滑动窗口轮数
                cpu_avg_ms = float(cfg.get("cpu_avg_ms", 90))  # 窗口内平均 CPU ms/轮
                io_avg = float(cfg.get("io_avg_bytes", 80000))  # 窗口内平均 IO bytes/轮
                need = int(cfg.get("confirm_rounds", 2))      # 连续命中几轮才算忙
                done_sec = float(cfg.get("done_seconds", 45))
                hold = float(cfg.get("active_hold_seconds", 9))

                cpu, io, nproc = _proc_tree_cpu_io(pname)
                if nproc == 0:
                    prev.pop(aid, None)
                    hist.pop(aid, None)
                    streak.pop(aid, None)
                    last_active.pop(aid, None)
                    done_until.pop(aid, None)
                    store.report(aid, "idle", task_id=TASK_ID)
                    continue

                old = prev.get(aid)
                prev[aid] = (cpu, io, nproc)
                if old is None:
                    continue
                d_cpu_ms = (cpu - old[0]) * 1000.0
                d_io = io - old[1]

                buf = hist.setdefault(aid, deque(maxlen=win))
                buf.append((d_cpu_ms, d_io))
                if len(buf) < win:
                    continue
                avg_cpu = sum(x[0] for x in buf) / len(buf)
                avg_io = sum(x[1] for x in buf) / len(buf)

                busy = avg_cpu >= cpu_avg_ms or avg_io >= io_avg
                if busy:
                    streak[aid] = streak.get(aid, 0) + 1
                    if streak[aid] >= need:
                        last_active[aid] = now
                else:
                    streak[aid] = 0

                # 任务名：从 blob 里取最近一条用户提问
                title = _grok_last_prompt(cfg.get("blob_dir", ""))
                # 状态机：
                #   活跃中 → working
                #   停止后先维持一小段 working（等最后一批渲染收尾），
                #   再转 done 并保持 done_seconds，最后回 idle 并清空任务
                if aid in last_active:
                    quiet = now - last_active[aid]
                    if quiet < hold:
                        store.report(aid, "working", (title or "Grok Bot 对话")[:60],
                                     task_id=TASK_ID)
                    else:
                        last_active.pop(aid, None)
                        done_until[aid] = now + done_sec
                        store.report(aid, "done", (title or "Grok Bot 对话")[:60],
                                     task_id=TASK_ID)
                elif aid in done_until:
                    if now < done_until[aid]:
                        store.report(aid, "done", (title or "Grok Bot 对话")[:60],
                                     task_id=TASK_ID)
                    else:
                        done_until.pop(aid, None)
                        store.report(aid, "idle", task_id=TASK_ID)
                else:
                    store.report(aid, "idle", task_id=TASK_ID)
        except Exception:
            pass
        stop_event.wait(3.0)


def _grok_last_prompt(blob_dir):
    """ 从 Grok 的 blob 持久化里取最近一条用户提问，作为任务标题。

    blob 路径超 260 字符，必须用 \\\\?\\ 长路径前缀访问。
    """
    d = os.path.expandvars(blob_dir or "")
    if not d or not os.path.isdir(_long_win_path(d)):
        return ""
    best = (0.0, "")
    try:
        with os.scandir(_long_win_path(d)) as it:
            for e in it:
                if not (e.is_file() and e.name.endswith(".blob")):
                    continue
                try:
                    with open(_long_win_path(e.path), "r", encoding="utf-8",
                              errors="ignore") as fh:
                        data = json.load(fh)
                except Exception:
                    continue
                v = data.get("value") if isinstance(data, dict) else None
                if not isinstance(v, dict):
                    continue
                entries = v.get("entries")
                if not isinstance(entries, list):
                    continue
                ts = 0.0
                prompt = ""
                for ent in entries:
                    if not isinstance(ent, dict):
                        continue
                    ets = ent.get("timestampMs") or 0
                    if ets:
                        ts = max(ts, ets / 1000.0)
                    if ent.get("role") != "user":
                        continue
                    txt = _grok_text(ent)
                    if txt:
                        prompt = txt
                if ts > best[0]:
                    best = (ts, prompt)
    except Exception:
        pass
    return best[1]


def _grok_text(entry):
    """ 从一条 entry 里抽出纯文本正文（richText 是 ProseMirror 文档 JSON）。 """
    raw = entry.get("richText")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if isinstance(raw, dict):
        parts = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "text" and isinstance(node.get("text"), str):
                    parts.append(node["text"])
                for val in node.values():
                    walk(val)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(raw)
        if parts:
            return " ".join(parts).strip()
    txt = entry.get("content")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    return ""


def run_net_watcher(store, agents, stop_event):
    """ 通过进程树的 TCP 连接判断「是否正在对话」（DeepSeek Harness 这类纯云端应用）。

    原理：流式生成回复期间，应用必须持有一条到 API 端点（或本地代理）的
    ESTABLISHED 连接；空闲时只剩进程间 IPC（连到自己监听端口的回环连接）。
    所以：出现外部连接 = 对话进行中；连接消失 = 对话结束。
    比窗口标题 / CPU 都准——标题对话结束后不变，CPU 会被界面定时刷新干扰。 """
    watched = [(a["id"], a["watch_net"]) for a in agents if a.get("watch_net")]
    if not watched:
        return
    streak = {}        # aid -> 连续检测到外部连接的次数（防抖）
    last_active = {}   # aid -> 最后一次有外部连接的时间
    TASK_ID = "net"

    def is_loopback(ip):
        return ip.startswith("127.") or ip == "::1"

    while not stop_event.is_set():
        try:
            now = time.time()
            wins = enum_windows()
            for aid, cfg in watched:
                pname = (cfg.get("process") or "").lower()
                if not pname:
                    continue
                pids = [p.pid for p in psutil.process_iter(["name"])
                        if (p.info["name"] or "").lower() == pname]
                if not pids:
                    streak.pop(aid, None)
                    if aid in last_active:
                        last_active.pop(aid, None)
                        store.report(aid, "idle", task_id=TASK_ID)
                    continue

                conns = []
                for pid in pids:
                    try:
                        conns += psutil.Process(pid).net_connections(kind="tcp")
                    except Exception:
                        continue
                listen_ports = {c.laddr.port for c in conns
                                if c.status == "LISTEN" and c.laddr}
                # 本进程树所有连接的本地端口集合：同一条进程间连接会从两端各看到一次，
                # 从主进程一侧看时远端端口是渲染进程的临时端口，必须也排除掉。
                local_ports = {c.laddr.port for c in conns if c.laddr}
                ext = any(
                    c.status == "ESTABLISHED" and c.raddr and
                    not (is_loopback(c.raddr.ip) and
                         (c.raddr.port in listen_ports or c.raddr.port in local_ports))
                    for c in conns)

                if ext:
                    streak[aid] = streak.get(aid, 0) + 1
                    if streak[aid] >= 2:          # 连续两轮才算，过滤瞬时请求
                        last_active[aid] = now
                else:
                    streak[aid] = 0

                # 任务名仍取窗口标题（标题里没有任务名就用占位）
                name = ""
                suffix = cfg.get("title_suffix", "")
                for _hwnd, title, pid in wins:
                    if pid not in pids or (suffix and not title.endswith(suffix)):
                        continue
                    nm = title
                    for sep in ("—", "-", "–"):
                        if sep in title:
                            nm = title.rsplit(sep, 1)[0].strip()
                            break
                    if nm and nm != suffix:
                        name = nm
                    break

                done_sec = float(cfg.get("done_seconds", 60))
                quiet = now - last_active.get(aid, 0)
                if aid in last_active and quiet < done_sec:
                    store.report(aid, "working", name or "对话中", task_id=TASK_ID)
                elif aid in last_active and quiet < done_sec * 3:
                    store.report(aid, "done", name or "对话", task_id=TASK_ID)
                else:
                    if aid in last_active:
                        last_active.pop(aid, None)
                    store.report(aid, "idle", task_id=TASK_ID)
        except Exception:
            pass
        stop_event.wait(3.0)


def run_window_watcher(store, agents, stop_event):
    """ 监听 GUI Agent 的窗口标题：标题里带任务名的，一开工就立刻显示为工作中。
    进程树长时间无 CPU 活动则视为已完成（idle_minutes 可调）。 """
    watched = [(a["id"], a["watch_window"]) for a in agents if a.get("watch_window")]
    if not watched:
        return
    last_active = {}

    def proc_name(pid):
        try:
            return (psutil.Process(pid).name() or "").lower()
        except Exception:
            return ""

    def tree_cpu(pid):
        """ 进程及其子进程的 CPU 占用合计（%） """
        total = 0.0
        try:
            root = psutil.Process(pid)
            procs = [root] + root.children(recursive=True)
            for p in procs:
                try:
                    total += p.cpu_percent(interval=0.15)
                except Exception:
                    pass
        except Exception:
            pass
        return total

    while not stop_event.is_set():
        try:
            wins = enum_windows()
            for aid, cfg in watched:
                want_proc = (cfg.get("process") or "").lower()
                suffix = cfg.get("title_suffix", "")
                hit = None
                for _hwnd, title, pid in wins:
                    if want_proc and proc_name(pid) != want_proc:
                        continue
                    if suffix and not title.endswith(suffix):
                        continue
                    hit = (title, pid)
                    break

                TASK_ID = "window"
                if not hit:
                    # 窗口不在 / 标题回到纯应用名 → 视为无任务
                    if aid in last_active:
                        last_active.pop(aid, None)
                        store.report(aid, "idle", task_id=TASK_ID)
                    continue

                title, pid = hit
                name = title
                for sep in ("—", "-", "–"):
                    if sep in title:
                        name = title.rsplit(sep, 1)[0].strip()
                        break
                if not name or name == suffix:
                    if aid in last_active:
                        last_active.pop(aid, None)
                        store.report(aid, "idle", task_id=TASK_ID)
                    continue

                cpu = tree_cpu(pid)
                now = time.time()
                if cpu > 1.0:
                    last_active[aid] = now

                idle_min = float(cfg.get("idle_minutes", 12))
                quiet_for = now - last_active.get(aid, now)
                state = "done" if quiet_for > idle_min * 60 else "working"
                store.report(aid, state, name, task_id=TASK_ID)
        except Exception:
            pass
        stop_event.wait(2.0)


def run_proc_watcher(store, agents, stop_event):
    while not stop_event.is_set():
        try:
            procs = []
            for p in psutil.process_iter(["name", "exe"]):
                try:
                    procs.append(((p.info["name"] or "").lower(),
                                  (p.info["exe"] or "").lower()))
                except Exception:
                    continue
            for a in agents:
                match = a.get("proc_match", {})
                name_eq = (match.get("name") or "").lower()
                path_has = (match.get("path_contains") or "").lower()
                running = False
                for pname, pexe in procs:
                    if name_eq and pname != name_eq:
                        continue
                    if path_has and path_has not in pexe:
                        continue
                    if name_eq or path_has:
                        running = True
                        break
                store.set_proc(a["id"], running)
        except Exception:
            pass
        stop_event.wait(5.0)


# ---------------------------------------------------------------- 悬浮窗 UI


class Bridge(QObject):
    """跨线程触发 UI 刷新"""
    tick = Signal()


def rel_time(ts):
    if not ts:
        return ""
    secs = int(time.time() - ts)
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{secs // 60}分钟前"
    return f"{secs // 3600}小时前"


# ---- 液态玻璃：自己抓背景做模糊，形状/透明度完全可控 ----

GLASS_RADIUS = 18
CARD_RADIUS = 12


def _oneline(text, limit=120):
    """ 多行/带空白的原始文本 → 单行展示，避免画到卡片外 """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t[:limit]


def _rrect(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def _blur_image(img, factor=14):
    """ 降采样再逐级放大 = 廉价但足够柔和的高斯近似模糊 """
    if img is None or img.isNull():
        return QImage()
    w, h = img.width(), img.height()
    if w < 4 or h < 4:
        return img
    cur = img
    while cur.width() > max(12, w // factor) and cur.width() > 12:
        cur = cur.scaled(max(12, cur.width() // 2), max(12, cur.height() // 2),
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return cur.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


class GlassPanel(QWidget):
    """ 磨砂玻璃面板：把窗口背后的桌面抓一次、模糊后贴到底层，
        再叠一层白霜 + 顶高光 + 细描边。比 Win11 原生亚克力好在
        可以只作用于圆角形状内，不会把宠物区域糊成方块。 """

    def __init__(self):
        super().__init__()
        self._backdrop = QImage()
        self.setAttribute(Qt.WA_TranslucentBackground)

    def finish_capture(self, img):
        """ 贴上抓到并模糊过的背景；img 为空则退化为纯色玻璃 """
        if img is None or img.isNull():
            self._backdrop = QImage()
            self.update()
            return
        self._backdrop = _blur_image(img)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = _rrect(rect, GLASS_RADIUS)

        p.save()
        p.setClipPath(path)
        if not self._backdrop.isNull():
            p.drawImage(rect, self._backdrop)
        else:
            p.fillRect(rect, QColor(245, 246, 249))
        # 白霜（玻璃的“乳白感”都在这层）
        g = QLinearGradient(0, rect.top(), 0, rect.bottom())
        g.setColorAt(0.0, QColor(255, 255, 255, 200))
        g.setColorAt(0.55, QColor(250, 252, 255, 188))
        g.setColorAt(1.0, QColor(240, 244, 250, 178))
        p.fillPath(path, QBrush(g))
        # 左上角柔光，制造曲面反光
        rad = QRadialGradient(QPointF(rect.left() + rect.width() * 0.18,
                                      rect.top() - rect.height() * 0.15),
                              rect.width() * 0.9)
        rad.setColorAt(0.0, QColor(255, 255, 255, 130))
        rad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(rad))
        p.restore()

        # 内阴影（底部更重，模拟玻璃厚度）
        inner = _rrect(rect.adjusted(1.5, 1.5, -1.5, -1.5), GLASS_RADIUS - 1.5)
        ig = QLinearGradient(0, rect.top(), 0, rect.bottom())
        ig.setColorAt(0.0, QColor(15, 23, 42, 0))
        ig.setColorAt(0.72, QColor(15, 23, 42, 0))
        ig.setColorAt(1.0, QColor(15, 23, 42, 26))
        p.setPen(QPen(QBrush(ig), 2.0))
        p.drawPath(inner)
        # 内高光 + 外描边
        p.setPen(QPen(QColor(255, 255, 255, 225), 1))
        p.drawPath(_rrect(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                          GLASS_RADIUS - 0.5))
        p.setPen(QPen(QColor(15, 23, 42, 30), 1))
        p.drawPath(path)


class AgentRow(QWidget):
    """ 一个 Agent 一张液态玻璃卡片；有多个任务时卡片自动变高 """

    PAD_X = 13
    LINE1 = 20          # 首行（名字/状态）基线区高度
    LINE2 = 14          # 次行（任务描述）高度
    SUB_H = 14          # 每条额外任务
    PAD_T, PAD_B = 4, 5

    def __init__(self, agent, store):
        super().__init__()
        self.agent = agent
        self.store = store
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._hover = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

        self._state = "idle"
        self._task_text = "—"
        self._subs = []
        self._pulse = 0.0
        self._mouse = None
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._on_pulse)
        self._relayout()

    # ---- 尺寸 / 动画 ----
    def _calc_height(self):
        return (self.PAD_T + self.LINE1 + self.LINE2
                + len(self._subs) * self.SUB_H + self.PAD_B)

    def _relayout(self):
        h = self._calc_height()
        if self.height() != h:
            self.setFixedHeight(h)

    def _on_anim(self, v):
        self._hover = float(v)
        self.update()

    def _on_pulse(self):
        self._pulse = (self._pulse + 0.09) % 1.0
        # 只重画左侧状态点所在的一小片
        self.update(QRect(0, 0, 60, self.height()))

    def mouseMoveEvent(self, e):
        self._mouse = e.position()
        if self._hover > 0.01:
            self.update()

    def enterEvent(self, e):
        self._mouse = e.position()
        self._start_anim(1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._start_anim(0.0)
        super().leaveEvent(e)

    def _start_anim(self, target):
        self._anim.stop()
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(target)
        self._anim.start()

    # ---- 数据 ----
    def refresh(self, d):
        tasks = d.get("tasks") or {}
        st = d["state"]
        self._state = st
        top = StateStore._top_task(tasks)
        parts = []
        if not d["proc_running"] and d["kind"] == "gui":
            parts.append("未运行")
        if top and top.get("text"):
            parts.append(_oneline(top["text"]))
        elif d.get("task"):
            parts.append(_oneline(d["task"]))
        if len(tasks) > 1:
            parts.append(f"共 {len(tasks)} 个任务")
        if d.get("updated"):
            parts.append(rel_time(d["updated"]))
        self._task_text = " · ".join(parts) if parts else "—"

        rest = [t for t in tasks.values() if t is not top]
        rest.sort(key=lambda x: (AGGREGATE_ORDER.index(x.get("state", "idle"))
                                 if x.get("state") in AGGREGATE_ORDER else 99,
                                 -x.get("updated", 0)))
        self._subs = [(t.get("state", "idle"), _oneline(t.get("text", ""), 60),
                       t.get("updated", 0)) for t in rest[:3]]

        # 只有“活着”的状态才呼吸，避免整屏乱动
        if st in ("working", "approval") and not self._pulse_timer.isActive():
            self._pulse_timer.start(90)
        elif st not in ("working", "approval") and self._pulse_timer.isActive():
            self._pulse_timer.stop()
            self._pulse = 0.0
        self._mouse = None

        self._relayout()
        self.update()

    # ---- 绘制 ----
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        hv = self._hover
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = _rrect(rect, CARD_RADIUS)

        # 玻璃底板：静止时是薄薄一层霜，悬停时整片浮起
        a = int(60 + 112 * hv)
        g = QLinearGradient(0, rect.top(), 0, rect.bottom())
        g.setColorAt(0.0, QColor(255, 255, 255, min(255, a + 26)))
        g.setColorAt(0.55, QColor(252, 253, 255, a))
        g.setColorAt(1.0, QColor(232, 239, 250, max(0, a - 16)))
        p.fillPath(path, QBrush(g))

        if hv > 0.02:
            # 外发光：像被一盏柔光罩住
            for pad, alpha in ((3.0, 10), (2.0, 16), (1.2, 24)):
                gc = QColor(255, 255, 255, int(alpha * hv))
                p.setPen(QPen(gc, 1.2))
                p.drawPath(_rrect(rect.adjusted(-pad, -pad + 1, pad, pad + 1),
                                  CARD_RADIUS + pad))
            # 冷色细边：浅色底上也能一眼看出是哪一条被指着
            p.setPen(QPen(QColor(70, 90, 120, int(38 * hv)), 1.0))
            p.drawPath(_rrect(rect.adjusted(-0.5, -0.5, 0.5, 0.5),
                              CARD_RADIUS + 0.5))
        # 跟随鼠标的柔光：玻璃被手电筒照到的那一小片
        if hv > 0.02 and self._mouse is not None:
            p.save()
            p.setClipPath(path)
            rad2 = QRadialGradient(QPointF(self._mouse), 120)
            rad2.setColorAt(0.0, QColor(255, 255, 255, int(96 * hv)))
            rad2.setColorAt(0.55, QColor(255, 255, 255, int(34 * hv)))
            rad2.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillPath(path, QBrush(rad2))
            p.restore()

        # 外侧细阴影（贴着卡片下沿，做出离地感）
        p.setPen(QPen(QColor(15, 23, 42, int(8 + 16 * hv)), 1.0))
        p.drawPath(_rrect(rect.adjusted(0.6, 1.6, -0.6, 0.6), CARD_RADIUS))
        # 描边：外圈冷色发丝 + 内圈白高光，白底/深底都能看出边界
        p.setPen(QPen(QColor(70, 90, 120, int(20 + 24 * hv)), 1.0))
        p.drawPath(_rrect(rect.adjusted(-0.5, -0.5, 0.5, 0.5),
                          CARD_RADIUS + 0.5))
        p.setPen(QPen(QColor(255, 255, 255, int(150 + 90 * hv)), 1.0))
        p.drawPath(path)
        # 顶部一道高光，底部一道内阴影 = 玻璃厚度
        p.setPen(QPen(QColor(255, 255, 255, int(120 + 90 * hv)), 1.0))
        p.drawPath(_rrect(QRectF(rect.left() + 1, rect.top() + 0.5,
                                 rect.width() - 2, rect.height() / 2),
                          CARD_RADIUS - 1))
        if hv > 0.02:
            p.setPen(QPen(QColor(15, 23, 42, int(16 * hv)), 1.0))
            p.drawPath(_rrect(rect.adjusted(0.5, 1.5, -0.5, -0.5),
                              CARD_RADIUS))

        meta = STATE_META.get(self._state, STATE_META["idle"])
        color = QColor(meta["color"])
        x = self.PAD_X
        cy = self.PAD_T + self.LINE1 / 2

        # 状态点：工作中/待审批带一圈呼吸光晕
        dot = QPointF(x + 3.5, cy)
        if self._state in ("working", "approval"):
            phase = 0.5 + 0.5 * math.sin(self._pulse * math.tau)
            halo = QRadialGradient(dot, 11)
            hc = QColor(color)
            hc.setAlpha(int(52 + 46 * phase))
            halo.setColorAt(0.0, hc)
            hc2 = QColor(color)
            hc2.setAlpha(0)
            halo.setColorAt(1.0, hc2)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(dot, 11, 11)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(dot, 3.6, 3.6)

        # 名字
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        dim = self._state == "idle"
        p.setPen(QColor("#5f6876") if dim else QColor("#14181f"))
        name_x = x + 15
        fm = QFontMetrics(f)
        name_max = self.width() - name_x - 92
        p.drawText(QRect(name_x, self.PAD_T, name_max, self.LINE1),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self.agent["label"], Qt.ElideRight, name_max))

        # 状态胶囊（右对齐）
        sf = QFont(f)
        sf.setPixelSize(10)
        sf.setBold(self._state in ("working", "approval"))
        sfm = QFontMetrics(sf)
        label = meta["label"]
        tw = sfm.horizontalAdvance(label)
        pill_w = tw + 14
        pill_h = 17
        pill = QRectF(self.width() - self.PAD_X - pill_w,
                      cy - pill_h / 2, pill_w, pill_h)
        pill_bg = QColor(color)
        pill_bg.setAlpha(40 if self._state != "idle" else 18)
        p.setPen(Qt.NoPen)
        p.setBrush(pill_bg)
        p.drawRoundedRect(pill, pill_h / 2, pill_h / 2)
        p.setFont(sf)
        p.setPen(color if self._state != "idle" else QColor("#8d95a3"))
        p.drawText(pill, Qt.AlignCenter, label)

        # 任务描述（第二行）
        tf = QFont(f)
        tf.setPixelSize(11)
        tf.setBold(False)
        p.setFont(tf)
        tcol = QColor("#5d6673") if not dim else QColor("#9aa1ac")
        if hv > 0.02:
            tcol = QColor("#3d4653") if not dim else QColor("#848b97")
        p.setPen(tcol)
        tfm = QFontMetrics(tf)
        tmax = self.width() - self.PAD_X * 2 - 14
        p.drawText(QRect(name_x, self.PAD_T + self.LINE1 - 4, tmax, self.LINE2),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   tfm.elidedText(self._task_text, Qt.ElideRight, tmax))

        # 额外任务
        if self._subs:
            sfont = QFont(tf)
            sfont.setPixelSize(10)
            p.setFont(sfont)
            sfm2 = QFontMetrics(sfont)
            y = self.PAD_T + self.LINE1 + self.LINE2 - 4
            for st, text, ts in self._subs:
                sc = QColor(STATE_META.get(st, STATE_META["idle"])["color"])
                p.setPen(sc)
                p.drawText(QRect(name_x, y, 8, self.SUB_H),
                           Qt.AlignVCenter | Qt.AlignLeft, "•")
                p.setPen(QColor("#8b929e"))
                line = text + (f" · {rel_time(ts)}" if ts else "")
                p.drawText(QRect(name_x + 9, y, tmax - 9, self.SUB_H),
                           Qt.AlignVCenter | Qt.AlignLeft,
                           sfm2.elidedText(line, Qt.ElideRight, tmax - 9))
                y += self.SUB_H

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            menu = QMenu(self)
            for st, label in (("working", "标记：工作中"), ("approval", "标记：待审批"),
                              ("done", "标记：已完成"), ("idle", "清除全部任务")):
                act = menu.addAction(label)
                act.triggered.connect(lambda _=False, s=st: self.store.report(
                    self.agent["id"], s, manual_task(s)))
            menu.exec(QCursor.pos())

    def mouseDoubleClickEvent(self, e):
        # 双击快捷标记完成
        self.store.report(self.agent["id"], "done")

def manual_task(state):
    return {"working": "手动标记", "approval": "手动标记",
            "done": "手动标记完成", "idle": ""}.get(state, "")


# 聚合优先级：待审批 > 出错 > 工作中 > 已完成 > 空闲
AGGREGATE_ORDER = ["approval", "error", "working", "done", "idle"]


# 宠物配色（一个强调色，其余中性）
PET_BODY = QColor("#f9b8c8")      # 主体
PET_BODY_DARK = QColor("#e895ab")  # 描边/暗部
PET_INK = QColor("#2b2b2b")        # 眼睛
PET_BLUSH = QColor("#f28b9c")


class PetWidget(QWidget):
    """ Codex 官方内置宠物 Fireball 的精灵图播放器。
    图集：fireball.png（从 Codex 客户端 app.asar 提取，1536x2288，8列x11行，单格192x208）。
    状态→动画行沿用 Codex v2 契约：
      idle→row0 空闲呼吸 / working→row7 干活 / approval→row6 等待审批(+红角标)
      done→row8 检查产出 / error→row5 出错
    """
    SIZE = 68
    CELL_W, CELL_H = 192, 208
    # 状态: (行号, 帧数, 每帧时长ms) —— 时长取自 app.asar 动画表/契约文档
    SPRITE_STATES = {
        "idle":     (0, 7, [280, 110, 110, 140, 140, 320, 320]),
        "working":  (7, 6, [120, 120, 120, 120, 120, 220]),
        "approval": (6, 6, [150, 150, 150, 150, 150, 260]),
        "done":     (8, 6, [150, 150, 150, 150, 150, 280]),
        "error":    (5, 8, [140, 140, 140, 140, 140, 140, 140, 240]),
    }

    BASE = 68          # 1.0 倍尺寸
    MIN_SCALE, MAX_SCALE = 0.7, 3.0

    # 安静模式：整体放慢（倍数越大越慢），空闲时大部分时间保持静止
    CALM_SLOW = {"idle": 1.0, "working": 2.0, "approval": 1.6, "done": 1.3, "error": 1.8}
    IDLE_CALM_MS = [2600, 320, 320, 420, 420, 900, 900]   # 首帧静止 2.6 秒再呼吸一轮

    def __init__(self, on_toggle, on_move, on_press, store=None, cfg=None):
        super().__init__()
        self.store = store
        cfg = cfg or {}
        self.calm = bool(cfg.get("calm", True))
        self.fps = int(cfg.get("fps", 12)) if self.calm else 30
        self.scale = (store.ui.get("scale") if store else None) or 1.4
        self.setFixedSize(self.px(), self.px())
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Agent 哨兵宠物：点击展开/收起，滚轮缩放，右键菜单调尺寸，按住拖动")
        self._on_toggle = on_toggle
        self._on_move = on_move
        self._on_press = on_press
        self._press_pos = None
        self.state = "idle"
        self.count = 0
        self._state_since = time.monotonic()
        sheet = os.path.join(BASE_DIR, "fireball.png")
        self.sheet = QPixmap(sheet) if os.path.exists(sheet) else QPixmap()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(int(1000 / max(1, self.fps)))

    def px(self):
        return int(self.BASE * self.scale)

    @property
    def SIZE(self):
        return self.px()

    def set_scale(self, scale, on_resize=None):
        scale = max(self.MIN_SCALE, min(self.MAX_SCALE, round(scale, 2)))
        if abs(scale - self.scale) < 0.01:
            return
        self.scale = scale
        self.setFixedSize(self.px(), self.px())
        if self.store:
            self.store.ui["scale"] = scale
            self.store.save()
        if on_resize:
            on_resize()

    def set_status(self, state, count):
        if state != self.state:
            self._state_since = time.monotonic()
        self.state = state
        self.count = count

    def wheelEvent(self, e):
        step = 0.12 if e.angleDelta().y() > 0 else -0.12
        self.set_scale(self.scale + step, self._on_resize)

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        for label, val in (("小 (×1.0)", 1.0), ("中 (×1.4)", 1.4),
                           ("大 (×2.0)", 2.0), ("特大 (×2.6)", 2.6)):
            act = menu.addAction(label)
            act.triggered.connect(lambda _=False, v=val: self.set_scale(v, self._on_resize))
        menu.exec(QCursor.pos())

    def _on_resize(self):
        w = self.window()
        if hasattr(w, "apply_pet_size"):
            w.apply_pet_size()

    def _frame_rect(self):
        row, n, durs = self.SPRITE_STATES.get(self.state, self.SPRITE_STATES["idle"])
        if self.calm:
            if self.state == "idle":
                durs = self.IDLE_CALM_MS
            else:
                durs = [d * self.CALM_SLOW.get(self.state, 1.5) for d in durs]
        elapsed = (time.monotonic() - self._state_since) * 1000.0
        total = sum(durs)
        t = elapsed % total
        acc = 0.0
        idx = 0
        for i, d in enumerate(durs):
            acc += d
            if t < acc:
                idx = i
                break
        return idx * self.CELL_W, row * self.CELL_H, self.CELL_W, self.CELL_H

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        # 地面阴影
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 20))
        p.drawEllipse(18, self.SIZE - 10, self.SIZE - 36, 5)
        # 精灵帧（保留一点边距）
        if not self.sheet.isNull():
            sx, sy, sw, sh = self._frame_rect()
            margin = 4
            tw, th = self.SIZE - margin * 2, self.SIZE - margin * 2
            p.drawPixmap(margin, margin - 2, tw, th, self.sheet, sx, sy, sw, sh)
        # 待审批角标（静态）
        if self.state == "approval":
            p.setBrush(QColor("white"))
            p.drawEllipse(self.SIZE - 20, 2, 14, 14)
            p.setBrush(QColor("#dc2626"))
            p.drawEllipse(self.SIZE - 18, 4, 10, 10)
        # 底部活跃数
        if self.count > 0:
            p.setBrush(QColor(17, 24, 39, 190))
            p.drawRoundedRect((self.SIZE - 18) // 2, self.SIZE - 15, 18, 12, 6, 6)
            p.setPen(QColor("white"))
            f = p.font()
            f.setPixelSize(9)
            f.setBold(True)
            p.setFont(f)
            p.drawText(0, self.SIZE - 15, self.SIZE, 12, Qt.AlignCenter, str(self.count))
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            self._on_press(self._press_pos)

    def mouseMoveEvent(self, e):
        if self._press_pos and e.buttons() & Qt.LeftButton:
            self._on_move(e.globalPosition().toPoint())

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._press_pos:
            delta = e.globalPosition().toPoint() - self._press_pos
            self._press_pos = None
            if abs(delta.x()) < 6 and abs(delta.y()) < 6:
                self._on_toggle()  # 视为点击而非拖动
            else:
                w = self.window()
                if hasattr(w, "_after_drag"):
                    w._after_drag()


class SentinelWindow(QWidget):
    PANEL_WIDTH = 404

    def __init__(self, agents, store, cfg=None):
        super().__init__()
        self.store = store
        self.cfg = cfg or {}
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._drag_offset = None
        self._expanded = False
        self._bg_busy = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # 宠物（可缩放）
        self.ball = PetWidget(self.toggle_expand, self._drag_to, self._drag_start,
                              store, self.cfg)
        ball_bar = QHBoxLayout()
        ball_bar.setContentsMargins(0, 0, 0, 0)
        ball_bar.addStretch(1)
        ball_bar.addWidget(self.ball)
        root.addLayout(ball_bar)

        # 展开面板（液态玻璃）
        self.panel = GlassPanel()
        root.addWidget(self.panel)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(9, 7, 9, 7)
        lay.setSpacing(5)

        # 标题栏
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 0, 2, 0)
        title = QLabel("Agent 哨兵")
        title.setStyleSheet("font-weight:600; color:#0f1319; letter-spacing:2px;"
                            " font-size:12px; background:transparent;")
        bar.addWidget(title)
        bar.addStretch(1)
        btn_collapse = QPushButton("︿")
        btn_collapse.setFixedSize(24, 22)
        btn_collapse.setToolTip("收回小球")
        btn_collapse.clicked.connect(self.toggle_expand)
        btn_hide = QPushButton("—")
        btn_hide.setFixedSize(24, 22)
        btn_hide.setToolTip("隐藏到托盘")
        btn_hide.clicked.connect(self.hide)
        for b in (btn_collapse, btn_hide):
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton{border:none; background:transparent; color:#8b93a0;"
                " font-size:12px; border-radius:7px;}"
                "QPushButton:hover{background:rgba(255,255,255,150); color:#14181f;}")
            bar.addWidget(b)
        lay.addLayout(bar)

        self.rows = []
        for a in agents:
            row = AgentRow(a, store)
            lay.addWidget(row)
            self.rows.append((a["id"], row))

        foot = QLabel("右键行=标记 · 双击行=完成 · 滚轮/右键宠物=调大小")
        foot.setStyleSheet("color:rgba(96,105,120,195); font-size:10px;"
                           " padding:1px 6px 0 6px; background:transparent;")
        lay.addWidget(foot)

        # 托盘
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        self.tray.setToolTip("Agent 哨兵")
        menu = QMenu()
        menu.addAction("显示/隐藏", self.toggle_visible)
        menu.addAction("退出", QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.toggle_visible() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(600)

        self._apply_mode()

    # ---- 模式切换 ----
    def toggle_expand(self):
        self._expanded = not self._expanded
        self._apply_mode()

    def apply_pet_size(self):
        """ 宠物缩放后：收起态同步窗口大小，保持右边缘不动 """
        if not self._expanded:
            right = self.geometry().right()
            top = self.geometry().top()
            self.setFixedSize(self.ball.px(), self.ball.px())
            self.move(max(0, right - self.ball.px() + 1), top)

    def _apply_mode(self):
        # 展开/收起时保持宠物位置不动：窗口右边缘固定，面板向左展开
        right_edge = self.geometry().right()
        top = self.geometry().top()
        self.panel.setVisible(self._expanded)
        if self._expanded:
            self.setFixedWidth(self.PANEL_WIDTH)
            self.setMaximumHeight(16777215)
            self.adjustSize()
            if self.isVisible():
                y = top
                avail = self.screen().availableGeometry()
                overflow = (y + self.height()) - (avail.bottom() - 4)
                if overflow > 0:
                    y = max(avail.top(), y - overflow)
                self.move(max(0, right_edge - self.width() + 1), y)
            self._snap_backdrop(90)
        else:
            self.setFixedSize(self.ball.px(), self.ball.px())
            if self.isVisible():
                self.move(max(0, right_edge - self.ball.px() + 1), top)

    # ---- 玻璃背景：抓窗口背后的桌面，模糊后贴到面板底层 ----
    def _snap_backdrop(self, delay=90):
        """ 把面板与宠物临时藏起来（尺寸先锁死，免得布局回缩导致窗口跳动），
            抓拍背后的桌面，再原样显形。不 hide() 整个窗口，避免被系统挪位置。 """
        if not self._expanded or not self.isVisible() or self._bg_busy:
            return
        self._bg_busy = True
        self._bg_geo = QRect(self.geometry())
        self._bg_panel_geo = QRect(self.panel.geometry())
        self.setFixedHeight(self.height())        # 锁尺寸
        self.panel.hide()
        self.ball.hide()
        QTimer.singleShot(delay, self._grab_backdrop)

    def _grab_backdrop(self):
        img = QImage()
        geo, pg = self._bg_geo, self._bg_panel_geo
        try:
            pm = self.screen().grabWindow(0, geo.x() + pg.x(), geo.y() + pg.y(),
                                          pg.width(), pg.height())
            if pm is not None and not pm.isNull():
                img = pm.toImage()
        except Exception:
            pass
        self.panel.show()
        self.ball.show()
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        if self.geometry().topLeft() != geo.topLeft():
            self.move(geo.topLeft())
        self.panel.finish_capture(img)
        self._bg_busy = False

    def showEvent(self, e):
        super().showEvent(e)
        if self._expanded and not self._bg_busy:
            self._snap_backdrop(120)

    def toggle_visible(self):
        self.setVisible(not self.isVisible())

    def bring_to_front(self):
        """双击快捷方式时，若已有实例则在运行，把窗口叫回前台。"""
        try:
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    # ---- 刷新 ----
    def refresh(self):
        snap = self.store.snapshot()
        for aid, row in self.rows:
            row.refresh(snap[aid])
        # 聚合状态 → 小球
        top_state = "idle"
        active = 0
        for d in snap.values():
            tasks = d.get("tasks") or {}
            live = [t for t in tasks.values() if t.get("state") not in ("idle", "done")]
            if live:
                active += len(live)
            elif d["state"] != "idle" or d["proc_running"]:
                active += 1
            for s in AGGREGATE_ORDER:
                if d["state"] == s and AGGREGATE_ORDER.index(s) < AGGREGATE_ORDER.index(top_state):
                    top_state = s
        self.ball.set_status(top_state, active)
        self._refit()

    def _refit(self):
        """ 任务增减导致卡片变高/变矮时，窗口跟着走 """
        if not self._expanded:
            return
        need = self.sizeHint().height()
        if abs(need - self.height()) > 1:
            right = self.geometry().right()
            self.resize(self.PANEL_WIDTH, need)
            self.move(right - self.PANEL_WIDTH + 1, self.y())

    # ---- 拖动（面板模式下拖窗口空白处；宠物形态由 PetWidget 回调） ----
    def _drag_start(self, global_pos):
        self._drag_offset = global_pos - self.frameGeometry().topLeft()

    def _drag_to(self, global_pos):
        if self._drag_offset:
            self.move(global_pos - self._drag_offset)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_offset and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        self._drag_offset = None
        self._after_drag()

    def _after_drag(self):
        # 挪过位置后背景就变了，延迟重抓一次（晚一点抓，拖完的瞬间别闪）
        if self._expanded:
            QTimer.singleShot(280, lambda: self._snap_backdrop(50))


# ---------------------------------------------------------------- 入口


def already_running():
    """ 端口已被占 = 已有实例在跑，不再开第二个宠物 """
    import socket
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect((HOST, PORT))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def launch_log(msg):
    """pythonw.exe 下 sys.stdout 是 None，直接 print 会抛异常，这里安全落盘。"""
    try:
        with open(os.path.join(BASE_DIR, "sentinel-launch.log"), "a",
                  encoding="utf-8") as f:
            f.write("%s  [pid %d] %s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass
    try:
        if sys.stdout is not None:
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()
    except Exception:
        pass


def main():
    global CONFIG
    launch_log("start, python=%s" % sys.executable)
    if already_running():
        launch_log("already running on port %d, exit" % PORT)
        sys.exit(0)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
    agents = CONFIG["agents"]

    store = StateStore(agents)
    stop_event = threading.Event()
    threading.Thread(target=run_http_server, args=(store, stop_event), daemon=True).start()
    threading.Thread(target=run_proc_watcher, args=(store, agents, stop_event), daemon=True).start()
    threading.Thread(target=run_window_watcher, args=(store, agents, stop_event), daemon=True).start()
    threading.Thread(target=run_db_watcher, args=(store, agents, stop_event), daemon=True).start()
    threading.Thread(target=run_turn_watcher, args=(store, agents, stop_event), daemon=True).start()
    threading.Thread(target=run_json_watcher, args=(store, agents, stop_event), daemon=True).start()
    threading.Thread(target=run_net_watcher, args=(store, agents, stop_event), daemon=True).start()
    threading.Thread(target=run_cursor_watcher, args=(store, agents, stop_event), daemon=True).start()
    threading.Thread(target=run_grok_watcher, args=(store, agents, stop_event), daemon=True).start()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = SentinelWindow(agents, store, CONFIG.get("pet", {}))
    bridge = UiBridge()
    bridge.show_requested.connect(win.bring_to_front)
    bridge.toggle_requested.connect(win.toggle_expand)
    ApiHandler.bridge = bridge
    win.show()
    code = app.exec()
    stop_event.set()
    store.save()
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # pythonw.exe 没有控制台，启动失败时用户看不到任何提示。
        # 这里把 traceback 落到项目目录的 sentinel-error.log 便于排查。
        try:
            with open(os.path.join(BASE_DIR, "sentinel-error.log"), "a",
                      encoding="utf-8") as _f:
                _f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  python=" +
                         sys.executable + "\n")
                _f.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        raise
