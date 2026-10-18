# -*- coding: utf-8 -*-
"""
Agent 哨兵 (Agent Sentinel)
桌面悬浮窗：集中监控本机多个 AI Agent 的运行状态。

- CLI 类 Agent（Claude Code / Codex / DeepSeek Harness）通过 HTTP POST 上报真实状态
- GUI 类应用（Claude Desktop / WorkBuddy 国内版 / WorkBuddy 国际版）通过进程检测 + 手动标记
- 状态：idle 空闲 / working 工作中 / approval 等待审批 / done 已完成 / error 出错
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPixmap
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
                        d["tasks"] = v["tasks"]
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


class ApiHandler(BaseHTTPRequestHandler):
    store: StateStore = None  # 由外部注入

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


class AgentRow(QWidget):
    """ 一个 Agent 一行；若它有多个任务，额外任务以缩进行展示 """

    def __init__(self, agent, store):
        super().__init__()
        self.agent = agent
        self.store = store
        self.setCursor(Qt.PointingHandCursor)
        self.vbox = QVBoxLayout(self)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(0)

        head = QWidget()
        lay = QHBoxLayout(head)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        self.dot = QLabel("●")
        self.dot.setFixedWidth(14)
        self.name = QLabel(agent["label"])
        self.name.setFixedWidth(118)
        self.name.setStyleSheet("font-weight:600; color:#1f2937;")
        self.state_lbl = QLabel("空闲")
        self.state_lbl.setFixedWidth(52)
        self.task = QLabel("—")
        self.task.setStyleSheet("color:#6b7280;")
        lay.addWidget(self.dot)
        lay.addWidget(self.name)
        lay.addWidget(self.state_lbl)
        lay.addWidget(self.task, 1)
        self.vbox.addWidget(head)

        self.sub_box = QVBoxLayout()
        self.sub_box.setContentsMargins(34, 0, 10, 4)
        self.sub_box.setSpacing(1)
        self.vbox.addLayout(self.sub_box)
        self._sub_labels = []

    def _set_subs(self, items):
        """ items: [(state, text, ts)] """
        while len(self._sub_labels) < len(items):
            lb = QLabel()
            lb.setStyleSheet("color:#9ca3af; font-size:11px;")
            self._sub_labels.append(lb)
            self.sub_box.addWidget(lb)
        for i, lb in enumerate(self._sub_labels):
            if i < len(items):
                st, text, ts = items[i]
                color = STATE_META.get(st, STATE_META["idle"])["color"]
                lb.setText(f'<span style="color:{color}">●</span> {text}'
                           + (f' · {rel_time(ts)}' if ts else ''))
                lb.show()
            else:
                lb.hide()

    def refresh(self, d):
        tasks = d.get("tasks") or {}
        st = d["state"]
        meta = STATE_META[st]
        color = meta["color"]
        self.dot.setStyleSheet(f"color:{color}; font-size:13px;")
        self.state_lbl.setText(meta["label"])
        weight = "600" if st == "approval" else "400"
        self.state_lbl.setStyleSheet(f"color:{color}; font-weight:{weight};")

        top = StateStore._top_task(tasks)
        parts = []
        if not d["proc_running"] and d["kind"] == "gui":
            parts.append("未运行")
        if top and top.get("text"):
            parts.append(top["text"])
        elif d.get("task"):
            parts.append(d["task"])
        if len(tasks) > 1:
            parts.append(f"共 {len(tasks)} 个任务")
        if d.get("updated"):
            parts.append(rel_time(d["updated"]))
        self.task.setText(" · ".join(parts) if parts else "—")

        # 其余任务展开为缩进行（最多再展示 3 条）
        rest = [t for t in tasks.values() if t is not top]
        rest.sort(key=lambda x: (AGGREGATE_ORDER.index(x.get("state", "idle"))
                                 if x.get("state") in AGGREGATE_ORDER else 99,
                                 -x.get("updated", 0)))
        self._set_subs([(t.get("state", "idle"), t.get("text", "")[:60], t.get("updated", 0))
                        for t in rest[:3]])

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


class SentinelWindow(QWidget):
    PANEL_WIDTH = 430

    def __init__(self, agents, store, cfg=None):
        super().__init__()
        self.store = store
        self.cfg = cfg or {}
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._drag_offset = None
        self._expanded = False

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

        # 展开面板
        self.panel = QWidget()
        self.panel.setObjectName("panel")
        self.panel.setStyleSheet(
            "#panel { background: rgba(255,255,255,250); border: 1px solid #e5e7eb;"
            " border-radius: 10px; }")
        root.addWidget(self.panel)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(4, 6, 4, 8)
        lay.setSpacing(2)

        # 标题栏
        bar = QHBoxLayout()
        bar.setContentsMargins(12, 2, 8, 0)
        title = QLabel("Agent 哨兵")
        title.setStyleSheet("font-weight:600; color:#111827; letter-spacing:1px;")
        bar.addWidget(title)
        bar.addStretch(1)
        btn_collapse = QPushButton("︿")
        btn_collapse.setFixedSize(24, 24)
        btn_collapse.setToolTip("收回小球")
        btn_collapse.clicked.connect(self.toggle_expand)
        btn_hide = QPushButton("—")
        btn_hide.setFixedSize(24, 24)
        btn_hide.setToolTip("隐藏到托盘")
        btn_hide.clicked.connect(self.hide)
        for b in (btn_collapse, btn_hide):
            b.setStyleSheet("QPushButton{border:none;color:#9ca3af;} QPushButton:hover{color:#111827;}")
            bar.addWidget(b)
        lay.addLayout(bar)

        self.rows = []
        for a in agents:
            row = AgentRow(a, store)
            lay.addWidget(row)
            self.rows.append((a["id"], row))

        foot = QLabel("右键行=标记 · 双击行=完成 · 滚轮/右键宠物=调大小")
        foot.setStyleSheet("color:#c4c9d1; font-size:10px; padding:2px 12px;")
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
                self.move(max(0, right_edge - self.width() + 1), top)
        else:
            self.setFixedSize(self.ball.px(), self.ball.px())
            if self.isVisible():
                self.move(max(0, right_edge - self.ball.px() + 1), top)

    def toggle_visible(self):
        self.setVisible(not self.isVisible())

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


def main():
    global CONFIG
    if already_running():
        print("Agent 哨兵已在运行（端口 %d 已占用），本次不重复启动。" % PORT)
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

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = SentinelWindow(agents, store, CONFIG.get("pet", {}))
    win.show()
    code = app.exec()
    stop_event.set()
    store.save()
    sys.exit(code)


if __name__ == "__main__":
    main()
