#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcldlgui.py —— pcldl 的桌面端（tkinter，零第三方依赖）。

引擎复用 pcldl.py（ModNet.vb 的移植），这里只做界面与任务队列：
  * 多任务队列，可限制同时下载数
  * 每条任务独立显示进度 / 速度 / 剩余时间 / 活动线程数
  * 暂停即断点续传（复用 .pcl_tmp 状态文件），重开程序任务列表还在
  * 新建任务支持多镜像、限速、线程上限、Cookie/Referer、哈希校验

用法：
  python pcldlgui.py            # 启动
  python pcldlgui.py --selftest URL   # 无界面跑一遍，用于 CI / 冒烟
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import tkinter as tk
import uuid
from tkinter import ttk, filedialog, messagebox

import pcldl

# ------------------------------------------------------------------ 基础
APP = "pcldl"
VERSION = "1.0"

DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "pcldl")
TASK_FILE = os.path.join(DATA_DIR, "tasks.json")
CONF_FILE = os.path.join(DATA_DIR, "settings.json")

BG = "#eef2f8"
CARD = "#ffffff"
ACCENT = "#2f7cf6"
ACCENT_D = "#1f63d0"
TEXT = "#1d2430"
SUB = "#77808f"
LINE = "#dde5f0"
OK = "#1f9d63"
ERR = "#e0464b"
WARN = "#c67c00"
IDLE = "#8a93a3"

STATE_TEXT = {
    "queued": "等待中", "running": "下载中", "paused": "已暂停",
    "done": "已完成", "failed": "失败",
}
STATE_COLOR = {
    "queued": IDLE, "running": ACCENT, "paused": WARN,
    "done": OK, "failed": ERR,
}

if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

FONT = ("Microsoft YaHei UI", 10) if os.name == "nt" else ("Helvetica", 11)
FONT_B = (FONT[0], 10, "bold")
FONT_S = (FONT[0], 9)
FONT_T = (FONT[0], 15, "bold")


def human(n: float) -> str:
    return pcldl.human(n) if n and n > 0 else "-"


def fmt_eta(sec: float) -> str:
    if not sec or sec <= 0 or sec > 99 * 3600:
        return "--:--"
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60:02d}:{sec % 60:02d}"


# ------------------------------------------------------------------ 任务
class Task:
    def __init__(self, urls, out, *, limit=0, threads=16,
                 headers=None, hashes=None, state="queued",
                 size=-1, done=0, elapsed=0.0):
        self.id = uuid.uuid4().hex[:12]
        self.urls = list(urls)
        self.out = out
        self.limit = limit
        self.threads = threads
        self.headers = dict(headers or {})
        self.hashes = dict(hashes or {})

        self.state = state
        self.error = ""
        self.done = done
        self.size = size
        self.speed = 0.0
        self.live = 0
        self.elapsed = 0.0
        self.peak = 0.0
        self.elapsed = elapsed

        self.nf = None
        self.worker: threading.Thread | None = None

    # ---- 持久化
    def to_dict(self):
        return {"id": self.id, "urls": self.urls, "out": self.out,
                "limit": self.limit, "threads": self.threads,
                "headers": self.headers, "hashes": self.hashes,
                "size": self.size, "done": self.done, "elapsed": self.elapsed,
                "state": "queued" if self.state == "running" else self.state}

    @classmethod
    def from_dict(cls, d):
        return cls(d["urls"], d["out"], limit=d.get("limit", 0),
                   threads=d.get("threads", 16), headers=d.get("headers"),
                   hashes=d.get("hashes"), state=d.get("state", "paused"),
                   size=d.get("size", -1), done=d.get("done", 0),
                   elapsed=d.get("elapsed", 0.0))

    @property
    def name(self) -> str:
        return os.path.basename(self.out)

    @property
    def pct(self) -> float:
        if self.size and self.size > 0:
            return min(self.done / self.size, 1.0)
        return 0.0


# ------------------------------------------------------------------ 小组件
class Bar(tk.Canvas):
    """进度条：底色 + 填充 + 圆角，右侧叠百分比。"""

    H = 12

    def __init__(self, master, **kw):
        super().__init__(master, height=self.H, bg=CARD, highlightthickness=0, **kw)
        self.value = 0.0
        self.color = ACCENT
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, value: float, color: str | None = None):
        self.value = max(0.0, min(1.0, value))
        if color:
            self.color = color
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or 1
        h = self.H
        r = h / 2
        self._round(0, 0, w, h, r, "#e4ecf7")
        filled = max(h, w * self.value)
        if self.value > 0:
            self._round(0, 0, filled, h, r, self.color)

    def _round(self, x1, y1, x2, y2, r, fill):
        if x2 - x1 < 2 * r:
            r = max(0, (x2 - x1) / 2)
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        self.create_polygon(pts, smooth=True, fill=fill, outline="")


class TaskCard(tk.Frame):
    def __init__(self, master, task: Task, app: "App"):
        super().__init__(master, bg=CARD, highlightbackground=LINE,
                         highlightthickness=1, bd=0)
        self.task = task
        self.app = app
        self._build()

    def _build(self):
        pad = {"padx": 16}
        top = tk.Frame(self, bg=CARD)
        top.pack(fill="x", pady=(12, 6), **pad)

        self.lbl_name = tk.Label(top, text=self.task.name, bg=CARD, fg=TEXT,
                                 font=FONT_B, anchor="w")
        self.lbl_name.pack(side="left")

        self.lbl_state = tk.Label(top, text="", bg=CARD, fg=IDLE, font=FONT_S)
        self.lbl_state.pack(side="right")

        self.bar = Bar(self)
        self.bar.pack(fill="x", pady=(0, 6), **pad)

        meta = tk.Frame(self, bg=CARD)
        meta.pack(fill="x", **pad)
        self.lbl_meta = tk.Label(meta, text="", bg=CARD, fg=SUB, font=FONT_S, anchor="w")
        self.lbl_meta.pack(side="left")
        self.lbl_right = tk.Label(meta, text="", bg=CARD, fg=SUB, font=FONT_S, anchor="e")
        self.lbl_right.pack(side="right")

        btns = tk.Frame(self, bg=CARD)
        btns.pack(fill="x", pady=(10, 12), **pad)
        self.btn_main = self._btn(btns, "开始", self._on_main, primary=True)
        self.btn_open = self._btn(btns, "打开文件夹", self._on_open)
        self.btn_del = self._btn(btns, "删除", self._on_delete)
        self.btn_main.grid(row=0, column=0, padx=(0, 8))
        self.btn_open.grid(row=0, column=1, padx=(0, 8))
        self.btn_del.grid(row=0, column=2)
        self.lbl_err = tk.Label(btns, text="", bg=CARD, fg=ERR, font=FONT_S, anchor="w")
        self.lbl_err.grid(row=0, column=3, padx=(10, 0), sticky="w")

    def _btn(self, parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd, font=FONT_S, bd=0,
                         relief="flat", cursor="hand2", padx=12, pady=4,
                         bg=ACCENT if primary else "#eef2f8",
                         fg="#ffffff" if primary else TEXT,
                         activebackground=ACCENT_D if primary else "#e2e8f2",
                         activeforeground="#ffffff" if primary else TEXT)

    # ---- 交互
    def _on_main(self):
        t = self.task
        if t.state == "running":
            self.app.pause(t)
        elif t.state in ("paused", "failed", "queued"):
            self.app.start(t)
        elif t.state == "done":
            self.app.open_folder(t)

    def _on_open(self):
        self.app.open_folder(self.task)

    def _on_delete(self):
        self.app.remove(self.task)

    # ---- 刷新
    def refresh(self):
        t = self.task
        self.lbl_name.config(text=t.name)
        self.lbl_state.config(text=STATE_TEXT.get(t.state, t.state),
                              fg=STATE_COLOR.get(t.state, IDLE))
        color = {"failed": ERR, "done": OK, "paused": WARN}.get(t.state, ACCENT)
        self.bar.set(t.pct, color)

        if t.size > 0:
            left = f"{human(t.done)} / {human(t.size)}"
        elif t.done:
            left = human(t.done)
        else:
            left = "等待响应…"

        if t.state == "running":
            eta = (t.size - t.done) / t.speed if t.speed > 1 and t.size > 0 else 0
            self.lbl_meta.config(text=f"{left}    {human(t.speed)}/s    剩余 {fmt_eta(eta)}")
            self.lbl_right.config(text=f"{t.pct * 100:5.1f}%    线程 {t.live}")
        elif t.state == "done":
            self.lbl_meta.config(text=f"{human(t.size if t.size > 0 else t.done)}    用时 {t.elapsed:.1f}s")
            self.lbl_right.config(text="100.0%")
        elif t.state in ("paused", "queued") and not t.done:
            self.lbl_meta.config(text="")
            self.lbl_right.config(text="")
        elif t.state == "failed":
            self.lbl_meta.config(text=left)
            self.lbl_right.config(text="")
        else:
            self.lbl_meta.config(text=left)
            p = f"{t.pct * 100:5.1f}%" if t.size > 0 else ""
            self.lbl_right.config(text=p)

        self.lbl_err.config(text=t.error[:60] if t.state == "failed" else "")

        txt = {"running": "暂停", "done": "打开文件夹"}.get(t.state, "开始")
        if self.btn_main.cget("text") != txt:
            self.btn_main.config(text=txt)
        # 完成态下主按钮已经能打开目录，隐藏重复的那个
        if t.state == "done":
            self.btn_open.grid_remove()
        else:
            self.btn_open.grid()


# ------------------------------------------------------------------ 新建任务
class NewTaskDialog(tk.Toplevel):
    def __init__(self, master, app: "App"):
        super().__init__(master)
        self.app = app
        self.title("新建任务")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        box = tk.Frame(self, bg=CARD)
        box.pack(fill="both", expand=True, padx=20, pady=18)

        tk.Label(box, text="下载地址（每行一个，多行为同一文件的镜像）", bg=CARD,
                 fg=SUB, font=FONT_S).grid(row=0, column=0, columnspan=3, sticky="w")
        self.txt_url = tk.Text(box, height=3, width=62, font=FONT, bd=1,
                               relief="solid", highlightthickness=0,
                               highlightbackground=LINE)
        self.txt_url.grid(row=1, column=0, columnspan=3, sticky="we", pady=(4, 12))

        tk.Label(box, text="保存到", bg=CARD, fg=SUB, font=FONT_S).grid(
            row=2, column=0, sticky="w")
        self.var_dir = tk.StringVar(value=self.app.settings.get(
            "last_dir", os.path.join(os.path.expanduser("~"), "Downloads")))
        tk.Entry(box, textvariable=self.var_dir, font=FONT, width=48, bd=1,
                 relief="solid").grid(row=3, column=0, columnspan=2, sticky="we", pady=(4, 12))
        tk.Button(box, text="浏览", command=self._browse, font=FONT_S, bd=0,
                  bg="#eef2f8", relief="flat", padx=12, pady=4).grid(
            row=3, column=2, sticky="e", padx=(8, 0))

        row = tk.Frame(box, bg=CARD)
        row.grid(row=4, column=0, columnspan=3, sticky="we", pady=(0, 12))
        tk.Label(row, text="线程", bg=CARD, fg=SUB, font=FONT_S).pack(side="left")
        self.var_threads = tk.StringVar(value=str(self.app.settings.get("threads", 16)))
        tk.Spinbox(row, from_=1, to=64, width=5, textvariable=self.var_threads,
                   font=FONT, bd=1, relief="solid").pack(side="left", padx=(6, 18))
        tk.Label(row, text="限速", bg=CARD, fg=SUB, font=FONT_S).pack(side="left")
        self.var_limit = tk.StringVar(value=self.app.settings.get("limit", "0"))
        tk.Entry(row, textvariable=self.var_limit, width=8, font=FONT, bd=1,
                 relief="solid").pack(side="left", padx=(6, 4))
        tk.Label(row, text="如 5M，0 为不限", bg=CARD, fg=SUB, font=FONT_S).pack(side="left")

        tk.Label(box, text="高级（可选）", bg=CARD, fg=SUB, font=FONT_S).grid(
            row=5, column=0, sticky="w")
        adv = tk.Frame(box, bg=CARD)
        adv.grid(row=6, column=0, columnspan=3, sticky="we", pady=(4, 12))
        tk.Label(adv, text="Referer", bg=CARD, fg=SUB, font=FONT_S).grid(row=0, column=0, sticky="w")
        self.var_ref = tk.StringVar()
        tk.Entry(adv, textvariable=self.var_ref, width=40, font=FONT, bd=1,
                 relief="solid").grid(row=0, column=1, sticky="we", padx=(6, 0), pady=3)
        tk.Label(adv, text="Cookie", bg=CARD, fg=SUB, font=FONT_S).grid(row=1, column=0, sticky="w")
        self.var_cookie = tk.StringVar()
        tk.Entry(adv, textvariable=self.var_cookie, width=40, font=FONT, bd=1,
                 relief="solid").grid(row=1, column=1, sticky="we", padx=(6, 0), pady=3)
        tk.Label(adv, text="SHA256", bg=CARD, fg=SUB, font=FONT_S).grid(row=2, column=0, sticky="w")
        self.var_sha = tk.StringVar()
        tk.Entry(adv, textvariable=self.var_sha, width=40, font=FONT, bd=1,
                 relief="solid").grid(row=2, column=1, sticky="we", padx=(6, 0), pady=3)

        btns = tk.Frame(box, bg=CARD)
        btns.grid(row=7, column=0, columnspan=3, sticky="e", pady=(6, 0))
        tk.Button(btns, text="取消", command=self.destroy, font=FONT_S, bd=0,
                  bg="#eef2f8", relief="flat", padx=16, pady=5).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="开始下载", command=self._ok, font=FONT_S, bd=0,
                  bg=ACCENT, fg="#ffffff", activebackground=ACCENT_D,
                  activeforeground="#ffffff", relief="flat", padx=16, pady=5).pack(side="left")

        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        self._center(master)
        self.txt_url.focus_set()

    def _center(self, master):
        w, h = self.winfo_width(), self.winfo_height()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or None)
        if d:
            self.var_dir.set(d)

    def _ok(self):
        urls = [u.strip() for u in self.txt_url.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning("提示", "请先填写下载地址", parent=self)
            return
        if not urls[0].lower().startswith(("http://", "https://")):
            messagebox.showwarning("提示", "地址需要以 http:// 或 https:// 开头", parent=self)
            return
        folder = self.var_dir.get().strip()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            messagebox.showerror("提示", f"目录不可用：{e}", parent=self)
            return

        name = pcldl.derive_name(urls[0])
        out = os.path.join(folder, name)
        if os.path.exists(out) and not os.path.exists(os.path.join(
                folder, ".pcl_tmp", name + ".json")):
            if not messagebox.askyesno("文件已存在", f"{name} 已存在，是否覆盖？", parent=self):
                return

        try:
            limit = pcldl.parse_size(self.var_limit.get() or "0")
        except Exception:
            messagebox.showwarning("提示", "限速格式不对，例如 5M / 512K / 0", parent=self)
            return

        headers = {}
        if self.var_ref.get().strip():
            headers["Referer"] = self.var_ref.get().strip()
        if self.var_cookie.get().strip():
            headers["Cookie"] = self.var_cookie.get().strip()

        self.app.add_task(Task(
            urls, out,
            limit=limit,
            threads=max(1, int(self.var_threads.get() or 16)),
            headers=headers,
            hashes={"sha256": self.var_sha.get().strip()} if self.var_sha.get().strip() else None,
        ))
        self.app.settings["last_dir"] = folder
        self.app.save_settings()
        self.destroy()


# ------------------------------------------------------------------ 主窗口
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"pcldl {VERSION}")
        self.configure(bg=BG)
        self.geometry("880x620")
        self.minsize(720, 460)

        self.settings = self._load_settings()
        self.tasks: list[Task] = []
        self.cards: dict[str, TaskCard] = {}
        self._closing = False

        self._build()
        self._load_tasks()
        self.after(150, self._tick)
        self.after(600, self._pump_queue)      # 上次没下完的任务自动继续
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 配置
    def _load_settings(self) -> dict:
        try:
            with open(CONF_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            d = {}
        d.setdefault("threads", 16)
        d.setdefault("limit", "0")
        d.setdefault("parallel", 3)
        return d

    def save_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            with open(CONF_FILE, "w", encoding="utf-8") as fh:
                json.dump(self.settings, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load_tasks(self):
        try:
            with open(TASK_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        for d in data:
            try:
                t = Task.from_dict(d)
            except Exception:
                continue
            self.tasks.append(t)
            self._add_card(t)
        self._refresh_empty()

    def save_tasks(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            with open(TASK_FILE, "w", encoding="utf-8") as fh:
                json.dump([t.to_dict() for t in self.tasks], fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------------- 布局
    def _build(self):
        head = tk.Frame(self, bg=CARD, height=64)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Frame(self, bg=LINE, height=1).pack(fill="x")

        tk.Label(head, text="pcldl", bg=CARD, fg=ACCENT, font=FONT_T).pack(
            side="left", padx=(20, 8))
        tk.Label(head, text="PCL2 下载引擎", bg=CARD, fg=SUB, font=FONT_S).pack(
            side="left", pady=(6, 0))

        default_btn = tk.Button(head, text="默认参数", command=self._open_settings,
                                font=FONT_S, bd=0, relief="flat", bg="#eef2f8",
                                fg=TEXT, padx=14, pady=6, cursor="hand2",
                                activebackground="#e2e8f2")
        default_btn.pack(side="right", padx=(8, 20), pady=14)
        add_btn = tk.Button(head, text="+ 新建任务", command=self._new_task,
                            font=FONT_S, bd=0, relief="flat", bg=ACCENT, fg="#ffffff",
                            padx=16, pady=6, cursor="hand2",
                            activebackground=ACCENT_D, activeforeground="#ffffff")
        add_btn.pack(side="right", pady=14)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=14)

        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        self.scroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        self.empty = tk.Frame(self.inner, bg=BG)
        tk.Label(self.empty, text="还没有任务", bg=BG, fg=SUB, font=(FONT[0], 13)).pack(pady=(70, 6))
        tk.Label(self.empty, text="点右上角「+ 新建任务」粘贴链接即可开始",
                 bg=BG, fg=IDLE, font=FONT_S).pack()

        self.status = tk.Label(self, text="", bg=CARD, fg=SUB, font=FONT_S, anchor="w")
        self.status.pack(fill="x", side="bottom", ipady=5, padx=0)

    def _on_wheel(self, event):
        if self.inner.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _refresh_empty(self):
        if not self.tasks:
            self.empty.pack(fill="x")
        else:
            self.empty.pack_forget()

    def _add_card(self, t: Task):
        card = TaskCard(self.inner, t, self)
        card.pack(fill="x", pady=(0, 10), padx=2)
        card.refresh()
        self.cards[t.id] = card
        self._refresh_empty()

    # ---------------- 任务
    def _new_task(self):
        NewTaskDialog(self, self)

    def add_task(self, t: Task):
        self.tasks.append(t)
        self._add_card(t)
        self.save_tasks()
        self._pump_queue()

    def start(self, t: Task):
        if t.state == "running":
            return
        t.error = ""
        t.state = "queued"
        self.save_tasks()
        self._pump_queue()

    def pause(self, t: Task):
        if t.nf is not None:
            t.nf.request_stop()
        t.state = "paused"
        self.save_tasks()
        self._refresh_cards()

    def remove(self, t: Task):
        if t.state == "running" and t.nf is not None:
            if not messagebox.askyesno("确认", f"「{t.name}」正在下载，删除会中断，继续？", parent=self):
                return
            t.nf.request_stop()
        self.tasks = [x for x in self.tasks if x.id != t.id]
        c = self.cards.pop(t.id, None)
        if c:
            c.destroy()
        self._refresh_empty()
        self.save_tasks()

    def open_folder(self, t: Task):
        folder = os.path.dirname(t.out)
        try:
            if os.name == "nt":
                os.startfile(folder)          # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception:
            pass

    def running_count(self) -> int:
        return sum(1 for t in self.tasks if t.state == "running")

    def _pump_queue(self):
        limit = max(1, int(self.settings.get("parallel", 3)))
        for t in self.tasks:
            if self.running_count() >= limit:
                break
            if t.state == "queued" and t.worker is None:
                self._launch(t)

    def _launch(self, t: Task):
        t.state = "running"
        t.worker = threading.Thread(target=self._worker, args=(t,), daemon=True)
        t.worker.start()
        self._refresh_cards()

    def _worker(self, t: Task):
        mgr = pcldl.NetManager(limit_high=t.limit, thread_limit=t.threads)
        nf = pcldl.NetFile(t.urls, t.out, mgr, headers=t.headers,
                           quiet=True, hashes=t.hashes)
        t.nf = nf
        t.started = time.monotonic()
        try:
            nf.run()
            t.state = "done" if nf.state == pcldl.NetState.Finished else "paused"
        except BaseException as e:                 # noqa: BLE001
            t.error = str(e) or e.__class__.__name__
            t.state = "failed"
        finally:
            t.elapsed = max(time.monotonic() - t.started, 0.001)
            t.done = nf.download_done
            t.size = nf.file_size
            t.peak = mgr.peak
            try:
                mgr.stop()
            except Exception:
                pass
            t.nf = None
            t.worker = None
            self.after(0, self._after_task)

    def _after_task(self):
        self.save_tasks()
        self._refresh_cards()
        self._pump_queue()

    # ---------------- 刷新
    def _tick(self):
        if self._closing:
            return
        self._sync()
        self._refresh_cards()
        self.after(200, self._tick)

    def _sync(self):
        for t in self.tasks:
            nf = t.nf
            if nf is None:
                continue
            t.done = nf.download_done
            t.size = nf.file_size
            t.speed = nf.manager.speed if nf.manager else 0.0
            t.live = sum(1 for x in list(nf.threads) if x.th and x.th.is_alive())

    def _refresh_cards(self):
        for t in self.tasks:
            c = self.cards.get(t.id)
            if c:
                c.refresh()
        run = self.running_count()
        total = len(self.tasks)
        if total:
            self.status.config(text=f"  {total} 个任务 · {run} 个进行中 · "
                                    f"并发上限 {self.settings.get('parallel', 3)}")
        else:
            self.status.config(text="  就绪")

    def _open_settings(self):
        w = tk.Toplevel(self)
        w.title("默认参数")
        w.configure(bg=CARD)
        w.resizable(False, False)
        w.transient(self)
        w.grab_set()
        box = tk.Frame(w, bg=CARD)
        box.pack(padx=20, pady=18)

        tk.Label(box, text="同时下载任务数", bg=CARD, fg=TEXT, font=FONT).grid(
            row=0, column=0, sticky="w", pady=4)
        v_par = tk.StringVar(value=str(self.settings.get("parallel", 3)))
        tk.Spinbox(box, from_=1, to=10, width=6, textvariable=v_par,
                   font=FONT, bd=1, relief="solid").grid(row=0, column=1, padx=(12, 0))

        tk.Label(box, text="默认线程上限", bg=CARD, fg=TEXT, font=FONT).grid(
            row=1, column=0, sticky="w", pady=4)
        v_th = tk.StringVar(value=str(self.settings.get("threads", 16)))
        tk.Spinbox(box, from_=1, to=64, width=6, textvariable=v_th,
                   font=FONT, bd=1, relief="solid").grid(row=1, column=1, padx=(12, 0))

        tk.Label(box, text="默认限速", bg=CARD, fg=TEXT, font=FONT).grid(
            row=2, column=0, sticky="w", pady=4)
        v_li = tk.StringVar(value=str(self.settings.get("limit", "0")))
        tk.Entry(box, textvariable=v_li, width=8, font=FONT, bd=1,
                 relief="solid").grid(row=2, column=1, padx=(12, 0), sticky="w")

        def ok():
            self.settings["parallel"] = max(1, int(v_par.get() or 3))
            self.settings["threads"] = max(1, int(v_th.get() or 16))
            self.settings["limit"] = v_li.get().strip() or "0"
            self.save_settings()
            self._refresh_cards()
            w.destroy()

        tk.Button(box, text="保存", command=ok, font=FONT_S, bd=0, relief="flat",
                  bg=ACCENT, fg="#ffffff", padx=18, pady=5).grid(
            row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))

    # ---------------- 关闭
    def _on_close(self):
        running = [t for t in self.tasks if t.state == "running"]
        if running:
            if not messagebox.askyesno("确认退出", f"还有 {len(running)} 个任务在下载，"
                                                  "退出后下次打开会从断点继续。继续？", parent=self):
                return
        self._closing = True
        for t in running:
            if t.nf is not None:
                t.nf.request_stop()
            t.state = "queued"
        self.save_tasks()
        time.sleep(0.2)
        self.destroy()


# ------------------------------------------------------------------ 无界面自检
def selftest(url: str) -> int:
    info = {"state": "init", "done": 0, "size": -1, "error": ""}
    out = os.path.join(os.environ.get("TEMP", "."), "pcldl-selftest.bin")

    def work():
        mgr = pcldl.NetManager()
        nf = pcldl.NetFile([url], out, mgr, quiet=True)
        try:
            nf.run()
            info["state"] = "done"
        except Exception as e:                     # noqa: BLE001
            info["state"] = "failed"
            info["error"] = str(e)
        info["done"] = nf.download_done
        info["size"] = nf.file_size

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(120)
    print(f"selftest: state={info['state']} done={info['done']} size={info['size']} "
          f"err={info['error']}")
    return 0 if info["state"] == "done" else 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--selftest":
        return selftest(argv[1] if len(argv) > 1 else "https://example.com/x")
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
