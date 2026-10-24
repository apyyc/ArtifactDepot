#!/usr/bin/env python3
# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""ArtifactDepot 桌面客户端：Web 端主要功能的桌面版。

功能：
  - 文件浏览：bucket、目录导航、上传（多文件/并发/分片）、下载文件（分块并发、进度/速度、
    可取消、下载并发 1–16）、下载本目录（ZIP 单流）、新建目录、删除文件/空目录、
    批量删除（files/all）、签名链接；
  - 签名链接：查看、复制、作废；
  - Token 管理：注册表、显示明文、移除、DataHub 同步、配置重载、按权限点生成 Token、
    bucket/前缀资源范围、角色预设；
  - 审计：按 bucket/key/actor/since 查询。

依赖：只依赖标准库 tkinter + 同目录 artifactdepot_upload.py。
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:  # 可选依赖，没装也能用文件选择
    TkinterDnD = None
    DND_FILES = None
    HAS_DND = False

import artifactdepot_upload as core

# 深蓝配色
BG = "#050816"
BG2 = "#071027"
PANEL = "#0a1530"
PANEL2 = "#0e1d42"
INPUT = "#060f24"
BORDER = "#1d3b70"
TEXT = "#dbe9ff"
MUTED = "#8ba6d9"
ACCENT = "#3b82f6"
ACCENT2 = "#22d3ee"
DANGER = "#fb7185"
SUCCESS = "#34d399"
WARN = "#fbbf24"
SELECT = "#1d4ed8"

FONT = "TkDefaultFont"


def pick_font(root) -> str:
    families = set(tkfont.families(root))
    for name in ("Noto Sans CJK SC", "Microsoft YaHei UI", "PingFang SC",
                 "Noto Sans", "DejaVu Sans"):
        if name in families:
            return name
    return "TkDefaultFont"


def configure_theme(root) -> str:
    global FONT
    FONT = pick_font(root)
    base = (FONT, 10)
    root.option_add("*Font", base)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=INPUT,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    font=base, insertcolor=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("Panel2.TFrame", background=PANEL2)
    style.configure("Header.TFrame", background=PANEL2)
    style.configure("HeaderTitle.TLabel", background=PANEL2, foreground="#eef6ff",
                    font=(FONT, 18, "bold"))
    style.configure("HeaderSub.TLabel", background=PANEL2, foreground=MUTED,
                    font=(FONT, 10))
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
    style.configure("Title.TLabel", background=PANEL, foreground="#eef6ff",
                    font=(FONT, 16, "bold"))
    style.configure("Sub.TLabel", background=PANEL, foreground=ACCENT2,
                    font=(FONT, 10))
    style.configure("Status.TLabel", background=PANEL2, foreground=MUTED,
                    font=(FONT, 9))
    style.configure("TButton", background=PANEL2, foreground=TEXT,
                    bordercolor=BORDER, padding=(10, 5), focuscolor=ACCENT)
    style.map("TButton",
              background=[("active", ACCENT), ("disabled", PANEL2)],
              foreground=[("disabled", "#52658e")],
              bordercolor=[("active", ACCENT2)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    bordercolor=ACCENT, font=(FONT, 10, "bold"), padding=(12, 6))
    style.map("Accent.TButton", background=[("active", "#60a5fa")])
    style.configure("Danger.TButton", background="#6b1c36", foreground="#ffe4ec",
                    bordercolor="#9f3558", padding=(10, 5))
    style.map("Danger.TButton", background=[("active", DANGER), ("disabled", PANEL2)],
              foreground=[("active", "#1a0710"), ("disabled", "#52658e")])
    style.configure("Ghost.TButton", background=PANEL, foreground=ACCENT2,
                    bordercolor=BORDER, padding=(9, 4))
    style.map("Ghost.TButton", background=[("active", PANEL2)])
    style.configure("Small.TButton", padding=(6, 3), font=(FONT, 9))
    style.configure("TEntry", fieldbackground=INPUT, foreground=TEXT,
                    insertcolor=TEXT, bordercolor=BORDER)
    style.map("TEntry", fieldbackground=[("disabled", PANEL2), ("readonly", PANEL2)],
              foreground=[("disabled", MUTED), ("readonly", TEXT)])
    style.configure("TCombobox", fieldbackground=INPUT, background=PANEL2,
                    foreground=TEXT, arrowcolor=ACCENT2, bordercolor=BORDER,
                    selectbackground=SELECT, selectforeground="#fff")
    style.map("TCombobox", fieldbackground=[("readonly", INPUT)],
              foreground=[("readonly", TEXT)])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(10, 8, 10, 0))
    style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                    padding=(18, 8), font=(FONT, 10, "bold"))
    style.map("TNotebook.Tab",
              background=[("selected", PANEL2)],
              foreground=[("selected", ACCENT2)])
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=TEXT, bordercolor=BORDER, rowheight=27,
                    font=(FONT, 10))
    style.configure("Treeview.Heading", background=PANEL2, foreground=ACCENT2,
                    bordercolor=BORDER, font=(FONT, 10, "bold"), padding=(4, 6))
    style.map("Treeview", background=[("selected", SELECT)],
              foreground=[("selected", "#ffffff")])
    style.configure("TCheckbutton", background=BG, foreground=TEXT)
    style.map("TCheckbutton", background=[("active", BG)],
              foreground=[("disabled", MUTED)])
    style.configure("Panel.TCheckbutton", background=PANEL, foreground=TEXT)
    style.map("Panel.TCheckbutton", background=[("active", PANEL)])
    style.configure("TRadiobutton", background=BG, foreground=TEXT)
    style.map("TRadiobutton", background=[("active", BG)])
    style.configure("TProgressbar", troughcolor=INPUT, background=ACCENT2,
                    bordercolor=BORDER, lightcolor=ACCENT2, darkcolor=ACCENT2)
    style.configure("TScrollbar", background=PANEL2, troughcolor=BG,
                    arrowcolor=MUTED, bordercolor=BORDER)
    style.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=BG,
                    arrowcolor=MUTED, bordercolor=BORDER)
    style.configure("Horizontal.TScrollbar", background=PANEL2, troughcolor=BG,
                    arrowcolor=MUTED, bordercolor=BORDER)
    style.configure("TLabelframe", background=PANEL, bordercolor=BORDER,
                    foreground=ACCENT2)
    style.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT2,
                    font=(FONT, 10, "bold"))
    return FONT


def fmt_time(value: str) -> str:
    s = str(value or "")
    if not s:
        return ""
    if s.endswith("Z"):
        s = s[:-1]
    return s.replace("T", " ")


def fmt_size(n) -> str:
    return core.format_bytes(int(n or 0))


def clamp_download_concurrency(value, default=None) -> int:
    """下载并发数夹紧到 1–16（与服务端 data.download 的合法区间一致）。"""
    if default is None:
        default = core.DEFAULT_DOWNLOAD_CONCURRENCY
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = int(default)
    return max(1, min(core.DOWNLOAD_CONCURRENCY_MAX, n))


def parse_list(text: str) -> list:
    return [x.strip() for x in re.split(r"[,，\s]+", text or "") if x.strip()]


def parse_prefixes(text: str) -> dict:
    out = {}
    for item in parse_list(text):
        if ":" not in item:
            continue
        bucket, prefix = item.split(":", 1)
        bucket, prefix = bucket.strip(), prefix.strip()
        if bucket and prefix:
            out.setdefault(bucket, []).append(prefix)
    return out


def range_text(record: dict) -> str:
    buckets = [str(x) for x in (record.get("allow_buckets") or [])]
    prefixes = record.get("allow_prefixes") or {}
    parts = []
    if buckets:
        parts.append("bucket=" + ",".join(buckets))
    if prefixes:
        parts.append("前缀=" + "; ".join(
            f"{b}: {' / '.join(str(x) for x in arr)}" for b, arr in prefixes.items()))
    return "；".join(parts) if parts else "⚠️ 全部 bucket"


def make_scrollbar(parent, command, orient="vertical"):
    """深色经典滚动条；Tk 8.6 的 ttk 滚动条在该 X11 环境会渲染成白色。"""
    return tk.Scrollbar(parent, orient=orient, command=command,
                        bg=PANEL2, troughcolor=BG, activebackground=SELECT,
                        highlightthickness=0, borderwidth=0,
                        elementborderwidth=0, width=12, takefocus=0)


def center_window(win) -> None:
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth() - w) // 2
    y = (win.winfo_screenheight() - h) // 2
    win.geometry(f"+{max(0, x)}+{max(0, y)}")


class ScrollFrame(ttk.Frame):
    """带垂直滚动条的容器。"""

    def __init__(self, parent, height=260):
        super().__init__(parent, style="Panel.TFrame")
        self.canvas = tk.Canvas(self, height=height, bg=PANEL, highlightthickness=0)
        self.vs = make_scrollbar(self, self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vs.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vs.pack(side="right", fill="y")
        self.inner = ttk.Frame(self.canvas, style="Panel.TFrame")
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _wheel(self, event):
        delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")


class InputDialog(tk.Toplevel):
    def __init__(self, parent, title: str, prompt: str, initial: str = "",
                 show: str | None = None, width: int = 54):
        super().__init__(parent, bg=BG)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        frame = ttk.Frame(self, style="Panel.TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=prompt, style="Panel.TLabel", wraplength=width * 8).pack(
            anchor="w", pady=(0, 8))
        self.var = tk.StringVar(value=initial)
        entry = ttk.Entry(frame, textvariable=self.var, width=width, show=show or "")
        entry.pack(fill="x")
        entry.focus_set()
        row = ttk.Frame(frame, style="Panel.TFrame")
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="确定", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(row, text="取消", command=self._cancel).pack(side="right", padx=(0, 8))
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.update_idletasks()
        center_window(self)

    def _ok(self):
        self.result = self.var.get()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def ask_text(parent, title, prompt, initial="", show=None) -> str | None:
    dlg = InputDialog(parent, title, prompt, initial=initial, show=show)
    parent.wait_window(dlg)
    return dlg.result


class PresignDialog(tk.Toplevel):
    def __init__(self, parent, app, bucket: str, key: str):
        super().__init__(parent, bg=BG)
        self.title("生成签名链接")
        self.transient(parent)
        self.grab_set()
        self.app = app
        self.bucket = bucket
        self.key = key
        self.result_url = ""
        self.client = app.client()
        self.cfg = {"count_min": 1, "count_max": 10,
                    "expire_min_seconds": 60, "expire_max_seconds": 604800}
        try:
            self.cfg.update(self.client.signed_link_config())
        except Exception:
            pass
        frame = ttk.Frame(self, style="Panel.TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"{bucket} / {key}", style="PanelMuted.TLabel",
                  wraplength=520).pack(anchor="w")
        self.mode = tk.StringVar(value="time")
        self.mode_row = ttk.Frame(frame, style="Panel.TFrame")
        self.mode_row.pack(fill="x", pady=(10, 0))
        for val, text in (("time", "按时效"), ("count", "按次数"), ("permanent", "永久")):
            ttk.Radiobutton(self.mode_row, text=text, value=val, variable=self.mode,
                            command=self._mode).pack(side="left", padx=(0, 12))
        self.time_row = ttk.Frame(frame, style="Panel.TFrame")
        self.time_row.pack(fill="x", pady=(8, 0))
        ttk.Label(self.time_row, text="有效期（小时）", style="Panel.TLabel").pack(side="left")
        self.hours = tk.StringVar(value="1")
        ttk.Entry(self.time_row, textvariable=self.hours, width=10).pack(side="left", padx=8)
        self.hint = ttk.Label(self.time_row, text="", style="PanelMuted.TLabel")
        self.hint.pack(side="left")
        self.count_row = ttk.Frame(frame, style="Panel.TFrame")
        self.count_row.pack(fill="x", pady=(8, 0))
        ttk.Label(self.count_row, text="次数", style="Panel.TLabel").pack(side="left")
        self.count = tk.StringVar(value=str(self.cfg.get("count_min", 1)))
        ttk.Entry(self.count_row, textvariable=self.count, width=10).pack(side="left", padx=8)
        ttk.Label(self.count_row, text=f"{self.cfg.get('count_min')} - {self.cfg.get('count_max')} 次",
                  style="PanelMuted.TLabel").pack(side="left")
        self.result_var = tk.StringVar()
        self.result_label = ttk.Label(frame, text="链接", style="Panel.TLabel")
        self.result_label.pack(anchor="w", pady=(12, 2))
        self.result_entry = ttk.Entry(frame, textvariable=self.result_var, width=68)
        self.result_entry.pack(fill="x")
        btns = ttk.Frame(frame, style="Panel.TFrame")
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="生成", style="Accent.TButton", command=self._generate).pack(side="left")
        ttk.Button(btns, text="复制", command=self._copy).pack(side="left", padx=8)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")
        self._mode()
        self.update_idletasks()
        center_window(self)

    def _mode(self):
        mode = self.mode.get()
        self.time_row.pack_forget()
        self.count_row.pack_forget()
        if mode == "time":
            self.time_row.pack(fill="x", pady=(8, 0), before=self.result_label)
        elif mode == "count":
            self.count_row.pack(fill="x", pady=(8, 0), before=self.result_label)
        self.hint.config(text=f"{int(self.cfg.get('expire_min_seconds', 60)) // 60} 分钟 - "
                              f"{int(self.cfg.get('expire_max_seconds', 604800)) // 3600} 小时")

    def _generate(self):
        try:
            mode = self.mode.get()
            payload = {"mode": mode}
            if mode == "time":
                secs = int(float(self.hours.get()) * 3600)
                if secs < int(self.cfg.get("expire_min_seconds", 60)) or secs > int(self.cfg.get("expire_max_seconds", 604800)):
                    raise ValueError("有效期超出服务端允许范围")
                payload["expires"] = secs
            elif mode == "count":
                count = int(self.count.get())
                if count < int(self.cfg.get("count_min", 1)) or count > int(self.cfg.get("count_max", 10)):
                    raise ValueError("次数超出服务端允许范围")
                payload["count"] = count
            data = self.client.create_signed_link(self.bucket, self.key, **payload)
            relative = data.get("url") or ""
            self.result_url = relative if relative.startswith("http") else self.app.server_var.get().rstrip("/") + relative
            self.result_var.set(self.result_url)
        except Exception as e:
            messagebox.showerror("生成失败", str(e), parent=self)

    def _copy(self):
        if not self.result_url:
            return
        self.app.copy_text(self.result_url)
        self.app.set_status("签名链接已复制")


class BulkDeleteDialog(tk.Toplevel):
    def __init__(self, parent, app, bucket: str, prefix: str, mode: str):
        super().__init__(parent, bg=BG)
        self.title("批量删除")
        self.transient(parent)
        self.grab_set()
        self.app = app
        self.client = app.client()
        self.bucket = bucket
        self.prefix = (prefix or "").strip().strip("/")
        self.mode = mode
        self.target_name = self.prefix.split("/")[-1] if self.prefix else bucket
        self.ok = False
        self.preview_ok = False
        self.busy = False
        frame = ttk.Frame(self, style="Panel.TFrame", padding=18)
        frame.pack(fill="both", expand=True)
        label = "删除本目录全部内容" if mode == "all" else "清空本目录文件"
        ttk.Label(frame, text="⚠️ " + label, style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text=f"{bucket} / {self.prefix + '/' if self.prefix else ''}",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 10))
        self.preview = ttk.Label(frame, text="正在统计…", style="Panel.TLabel",
                                 wraplength=560, justify="left")
        self.preview.pack(anchor="w", pady=(0, 8))
        warning = ("递归删除当前目录下全部文件与子目录（当前目录保留；bucket 根清单受保护）。"
                   if mode == "all" else
                   "只删除当前目录下的直接文件；子目录、隐藏文件和 .keep 会保留。")
        ttk.Label(frame, text=warning, foreground=DANGER, background=PANEL,
                  wraplength=560, justify="left").pack(anchor="w", pady=(0, 10))
        ttk.Label(frame, text=f"请输入目录名「{self.target_name}」确认：",
                  style="Panel.TLabel").pack(anchor="w")
        self.confirm_var = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.confirm_var, width=48)
        self.entry.pack(fill="x", pady=(4, 4))
        self.hint = ttk.Label(frame, text="", style="PanelMuted.TLabel")
        self.hint.pack(anchor="w")
        row = ttk.Frame(frame, style="Panel.TFrame")
        row.pack(fill="x", pady=(14, 0))
        self.do_btn = ttk.Button(row, text="确认删除", style="Danger.TButton",
                                 state="disabled", command=self._execute)
        self.do_btn.pack(side="left")
        ttk.Button(row, text="取消", command=self.destroy).pack(side="right")
        self.confirm_var.trace_add("write", lambda *a: self._update_btn())
        self.entry.focus_set()
        self._preview()
        self.update_idletasks()
        center_window(self)

    def _preview(self):
        try:
            data = self.client.bulk_delete(self.bucket, self.prefix, self.mode, True)
            files = int(data.get("files") or 0)
            dirs = int(data.get("dirs") or 0)
            size = fmt_size(data.get("bytes"))
            self.preview.config(text=f"将删除 {files} 个文件" +
                                     (f"、{dirs} 个目录" if self.mode == "all" else "") +
                                     f"，共 {size}")
            self.preview_ok = True
        except Exception as e:
            self.preview.config(text=f"预览失败：{e}")
            self.preview_ok = False
        self._update_btn()

    def _update_btn(self):
        match = self.confirm_var.get().strip() == self.target_name
        self.do_btn.config(state="normal" if (match and self.preview_ok and not self.busy) else "disabled")
        if not self.confirm_var.get().strip():
            self.hint.config(text=f"请输入：{self.target_name}")
        elif match:
            self.hint.config(text="名称匹配，可以执行", foreground=SUCCESS)
        else:
            self.hint.config(text="名称不匹配", foreground=DANGER)

    def _execute(self):
        if self.busy:
            return
        self.busy = True
        self._update_btn()
        try:
            data = self.client.bulk_delete(self.bucket, self.prefix, self.mode,
                                           False, self.confirm_var.get().strip())
            self.ok = True
            self.app.set_status(
                f"已删除 {data.get('files', 0)} 个文件、{data.get('dirs', 0)} 个目录")
            if data.get("errors"):
                messagebox.showwarning("部分失败", f"有 {len(data['errors'])} 项失败，详见审计/日志",
                                       parent=self)
            self.destroy()
        except Exception as e:
            self.busy = False
            self._update_btn()
            messagebox.showerror("删除失败", str(e), parent=self)


class BrowseTab(ttk.Frame):
    """文件浏览 + 上传 + 下载 + 目录管理 + 批量删除。"""

    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self.bucket_var = tk.StringVar()
        self.prefix = ""
        self.items = []
        self.offset = 0
        self.total = 0
        self.truncated = False
        self.cancel_event = threading.Event()
        self.uploading = False
        self.queue_lock = threading.Lock()
        self.queue = []  # {path, name, size, pct, status}
        self.source_var = tk.StringVar()
        self.concurrency_var = tk.StringVar(value="3")
        self.overwrite_var = tk.BooleanVar(value=True)
        self.resume_var = tk.BooleanVar(value=True)
        # 下载：并发可调、可取消；进度/速度由 root.after 合并刷新（>=150ms 一次）
        saved_dl = core.load_ui_state().get("download_concurrency")
        self.downloading = False
        self.download_cancel = threading.Event()
        self.download_concurrency_var = tk.StringVar(
            value=str(clamp_download_concurrency(saved_dl)))
        self._dl_lock = threading.Lock()
        self._dl_latest = None
        self._dl_scheduled = False
        self._dl_cancelling = False
        self._dl_label = ""
        self._dl_last_time = 0.0
        self._dl_last_bytes = 0
        self._build()
        self.load_buckets()
        self.load_upload_config()

    # ---------------- UI ----------------
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        bar = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(bar, text="存储桶", style="Panel.TLabel").pack(side="left")
        self.bucket_combo = ttk.Combobox(bar, textvariable=self.bucket_var, width=24)
        self.bucket_combo.pack(side="left", padx=(8, 8))
        self.bucket_combo.bind("<Return>", lambda e: self.open_bucket())
        ttk.Button(bar, text="进入 / 刷新", style="Accent.TButton",
                   command=self.open_bucket).pack(side="left")
        ttk.Button(bar, text="列出存储桶", style="Ghost.TButton",
                   command=self.load_buckets).pack(side="left", padx=6)
        ttk.Button(bar, text="刷新目录", style="Ghost.TButton",
                   command=self.refresh).pack(side="left")
        self.bucket_info = ttk.Label(bar, text="", style="PanelMuted.TLabel")
        self.bucket_info.pack(side="right")

        nav = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        nav.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.path_label = ttk.Label(nav, text="（未选择 bucket）", style="Panel.TLabel")
        self.path_label.pack(side="left")
        ttk.Button(nav, text="上一级", style="Ghost.TButton",
                   command=self.go_up).pack(side="left", padx=(12, 0))
        ttk.Button(nav, text="新建目录", style="Ghost.TButton",
                   command=self.mkdir).pack(side="left", padx=6)
        ttk.Button(nav, text="下载本目录", style="Ghost.TButton",
                   command=self.download_current).pack(side="left")
        ttk.Button(nav, text="清空本目录文件", style="Danger.TButton",
                   command=lambda: self.bulk_delete("files")).pack(side="left", padx=(10, 0))
        ttk.Button(nav, text="删除本目录全部内容", style="Danger.TButton",
                   command=lambda: self.bulk_delete("all")).pack(side="left", padx=6)

        up = ttk.LabelFrame(self, text=" 上传文件 ", padding=10)
        up.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        up.columnconfigure(1, weight=1)
        ttk.Label(up, text="来源 URL", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(up, textvariable=self.source_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(up, text="并发数", style="Panel.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(up, from_=1, to=10, width=5,
                    textvariable=self.concurrency_var).grid(row=0, column=3, padx=(6, 10))
        ttk.Button(up, text="选择文件…", command=self.choose_files).grid(row=0, column=4)
        ttk.Button(up, text="开始上传", style="Accent.TButton",
                   command=self.start_upload).grid(row=0, column=5, padx=6)
        ttk.Button(up, text="取消", style="Ghost.TButton",
                   command=self.cancel_upload).grid(row=0, column=6)
        ttk.Button(up, text="清空队列", style="Ghost.TButton",
                   command=self.clear_queue).grid(row=0, column=7, padx=(6, 0))
        opts = ttk.Frame(up, style="Panel.TFrame")
        opts.grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 4))
        ttk.Checkbutton(opts, text="覆盖同名文件", variable=self.overwrite_var,
                        style="Panel.TCheckbutton").pack(side="left")
        ttk.Checkbutton(opts, text="断点续传", variable=self.resume_var,
                        style="Panel.TCheckbutton").pack(side="left", padx=14)
        self.upload_hint = ttk.Label(opts, text="支持多选；大文件自动分片",
                                     style="PanelMuted.TLabel")
        self.upload_hint.pack(side="left", padx=12)
        self.queue_tree = ttk.Treeview(up, columns=("name", "size", "progress", "status"),
                                       show="headings", height=3)
        for col, text, width in (("name", "文件", 360), ("size", "大小", 100),
                                 ("progress", "进度", 80), ("status", "状态", 220)):
            self.queue_tree.heading(col, text=text)
            self.queue_tree.column(col, width=width, anchor="w")
        self.queue_tree.grid(row=2, column=0, columnspan=8, sticky="ew")
        self.upload_progress = ttk.Progressbar(up, mode="determinate", maximum=100)
        self.upload_progress.grid(row=3, column=0, columnspan=8, sticky="ew", pady=(6, 0))

        table_box = ttk.Frame(self, style="Panel.TFrame")
        table_box.grid(row=3, column=0, sticky="nsew")
        table_box.columnconfigure(0, weight=1)
        table_box.rowconfigure(0, weight=1)
        cols = ("name", "size", "time", "source", "uploader")
        self.tree = ttk.Treeview(table_box, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor in (
                ("name", "名称", 400, "w"), ("size", "大小", 110, "e"),
                ("time", "时间", 160, "w"), ("source", "来源", 210, "w"),
                ("uploader", "上传者", 120, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs = make_scrollbar(table_box, self.tree.yview)
        vs.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.tag_configure("dir", foreground=ACCENT2)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_actions())
        self.tree.bind("<Double-1>", lambda e: self._on_double())

        actions = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        actions.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.enter_btn = ttk.Button(actions, text="进入目录", style="Ghost.TButton",
                                    command=self.enter_selected, state="disabled")
        self.enter_btn.pack(side="left")
        self.download_btn = ttk.Button(actions, text="下载", style="Ghost.TButton",
                                       command=self.download_selected, state="disabled")
        self.download_btn.pack(side="left", padx=6)
        ttk.Label(actions, text="下载并发", style="PanelMuted.TLabel").pack(
            side="left", padx=(4, 0))
        self.download_conc_spin = ttk.Spinbox(
            actions, from_=1, to=16, width=4,
            textvariable=self.download_concurrency_var)
        self.download_conc_spin.pack(side="left", padx=(4, 8))
        self.download_cancel_btn = ttk.Button(
            actions, text="取消下载", style="Ghost.TButton",
            command=self.cancel_download, state="disabled")
        self.download_cancel_btn.pack(side="left", padx=(0, 6))
        self.presign_btn = ttk.Button(actions, text="签名链接", style="Ghost.TButton",
                                      command=self.presign_selected, state="disabled")
        self.presign_btn.pack(side="left")
        self.delete_btn = ttk.Button(actions, text="删除", style="Danger.TButton",
                                     command=self.delete_selected, state="disabled")
        self.delete_btn.pack(side="left", padx=6)
        ttk.Button(actions, text="加载更多", style="Ghost.TButton",
                   command=self.load_more).pack(side="left")
        self.page_info = ttk.Label(actions, text="", style="PanelMuted.TLabel")
        self.page_info.pack(side="right")

    # ---------------- 基础数据 ----------------
    def client(self):
        return self.app.client()

    def load_upload_config(self):
        client = self.client()
        def work():
            data = {}
            try:
                data = client.permissions() or {}
                up = data.get("upload") or {}
                conc = int(up.get("file_concurrency") or 0)
                if conc:
                    self.app.root.after(0, lambda: self.concurrency_var.set(str(min(10, max(1, conc)))))
            except Exception:
                pass
            # 下载并发默认值：服务端 data.download 优先，其次本地上次值，最后 4
            download = data.get("download") if isinstance(data, dict) else None
            server_value = 0
            if isinstance(download, dict):
                try:
                    server_value = int(download.get("chunk_concurrency") or 0)
                except (TypeError, ValueError):
                    server_value = 0
            self.app.root.after(0, self._apply_download_concurrency, server_value)
        threading.Thread(target=work, daemon=True).start()

    def _apply_download_concurrency(self, server_value: int = 0) -> None:
        """下载并发默认值：服务端下发 > 本地上次值 > 默认 4，夹紧 1–16。"""
        saved = None
        try:
            saved = core.load_ui_state().get("download_concurrency")
        except Exception:
            saved = None
        value = server_value or saved or core.DEFAULT_DOWNLOAD_CONCURRENCY
        self.download_concurrency_var.set(str(clamp_download_concurrency(value)))

    def load_buckets(self):
        client = self.client()
        def work():
            try:
                buckets = client.list_buckets()
            except Exception as e:
                self.app.root.after(0, lambda: self.app.set_status(f"加载 bucket 失败：{e}"))
                return
            def apply():
                self.bucket_combo["values"] = buckets
                if not self.bucket_var.get().strip() and buckets:
                    self.bucket_var.set(buckets[0])
                self.bucket_info.config(text=f"{len(buckets)} 个 bucket")
            self.app.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def open_bucket(self):
        bucket = self.bucket_var.get().strip()
        if not bucket:
            messagebox.showwarning("缺少 bucket", "请填写或选择 bucket", parent=self)
            return
        self.prefix = ""
        self.refresh()

    def refresh(self):
        bucket = self.bucket_var.get().strip()
        if not bucket:
            self.app.set_status("请先选择 bucket")
            return
        self.offset = 0
        self.items = []
        self.load_page(reset=True)

    def load_page(self, reset=False):
        bucket = self.bucket_var.get().strip()
        if not bucket:
            return
        limit = 200
        offset = 0 if reset else len(self.items)
        client = self.client()
        prefix = self.prefix

        def work():
            try:
                data = client.list_objects(bucket, prefix, limit=limit, offset=offset)
            except Exception as e:
                self.app.root.after(0, self.app.show_error, e)
                return
            self.app.root.after(0, self._apply_page, data, reset)
        threading.Thread(target=work, daemon=True).start()

    def _apply_page(self, data: dict, reset: bool):
        if reset:
            self.items = []
            self.tree.delete(*self.tree.get_children())
        for item in (data.get("items") or []):
            self.items.append(item)
            key = str(item.get("key") or "")
            iid = ("d:" if item.get("is_dir") else "f:") + key
            name = str(item.get("name") or "")
            if item.get("is_dir"):
                name += "/"
            self.tree.insert("", "end", iid=iid, values=(
                name, "" if item.get("is_dir") else fmt_size(item.get("size")),
                fmt_time(item.get("mtime")), item.get("source_url") or "",
                item.get("uploader") or ""), tags=("dir",) if item.get("is_dir") else ())
        self.total = int(data.get("total") or len(self.items))
        self.truncated = bool(data.get("truncated"))
        self.prefix = str(data.get("prefix") if data.get("prefix") is not None else self.prefix)
        self.path_label.config(text=f"{self.bucket_var.get().strip()} / {self.prefix + '/' if self.prefix else ''}")
        shown = len(self.items)
        self.page_info.config(text=f"已显示 {shown} / {self.total}" + ("（可加载更多）" if self.truncated else ""))
        self._update_actions()
        self.app.set_status(f"{self.bucket_var.get().strip()} / {self.prefix or '/'}：{shown} 项")

    def load_more(self):
        if self.truncated:
            self.load_page(reset=False)

    # ---------------- 选择/导航 ----------------
    def selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        key = iid[2:]
        for item in self.items:
            if str(item.get("key")) == key:
                return item
        return None

    def _update_actions(self):
        item = self.selected()
        is_dir = bool(item and item.get("is_dir"))
        is_file = bool(item and not item.get("is_dir"))
        self.enter_btn.config(state="normal" if is_dir else "disabled")
        self.download_btn.config(
            state="normal" if (item and not self.downloading) else "disabled")
        self.presign_btn.config(state="normal" if is_file else "disabled")
        self.delete_btn.config(state="normal" if item else "disabled")

    def _on_double(self):
        item = self.selected()
        if not item:
            return
        if item.get("is_dir"):
            self.enter_dir(item.get("key"))
        else:
            self.download_selected()

    def enter_selected(self):
        item = self.selected()
        if item and item.get("is_dir"):
            self.enter_dir(item.get("key"))

    def enter_dir(self, key):
        self.prefix = str(key or "").rstrip("/")
        self.refresh()

    def go_up(self):
        parts = [p for p in self.prefix.split("/") if p]
        if parts:
            parts.pop()
        self.prefix = "/".join(parts)
        self.refresh()

    # ---------------- 目录操作 ----------------
    def mkdir(self):
        bucket = self.bucket_var.get().strip()
        if not bucket:
            messagebox.showwarning("缺少 bucket", "请先选择 bucket", parent=self)
            return
        name = ask_text(self, "新建目录", "目录名（可含 / 创建多级）")
        if not name:
            return
        key = core.make_key(self.prefix, name)
        try:
            self.client().mkdir(bucket, key)
            self.app.set_status(f"已创建目录：{key}")
            self.refresh()
        except Exception as e:
            self.app.show_error(e)

    def delete_selected(self):
        item = self.selected()
        if not item:
            return
        bucket = self.bucket_var.get().strip()
        key = str(item.get("key") or "")
        kind = "目录" if item.get("is_dir") else "文件"
        if not messagebox.askyesno("确认删除", f"确定删除{kind}？\n{bucket} / {key}\n删除不可恢复。",
                                   parent=self):
            return
        try:
            self.client().delete_object(bucket, key)
            self.app.set_status(f"已删除：{key}")
            self.refresh()
        except Exception as e:
            self.app.show_error(e)

    def bulk_delete(self, mode):
        bucket = self.bucket_var.get().strip()
        if not bucket:
            messagebox.showwarning("缺少 bucket", "请先选择 bucket", parent=self)
            return
        dlg = BulkDeleteDialog(self, self.app, bucket, self.prefix, mode)
        self.app.wait_window(dlg)
        if dlg.ok:
            self.refresh()

    # ---------------- 签名 ----------------
    def presign_selected(self):
        item = self.selected()
        if not item or item.get("is_dir"):
            return
        dlg = PresignDialog(self, self.app, self.bucket_var.get().strip(),
                            str(item.get("key") or ""))
        self.app.wait_window(dlg)

    # ---------------- 下载 ----------------
    def download_selected(self):
        item = self.selected()
        if not item:
            return
        bucket = self.bucket_var.get().strip()
        key = str(item.get("key") or "")
        if item.get("is_dir"):
            directory = filedialog.askdirectory(parent=self, title="选择下载目录（整个目录将打包为 ZIP）")
            if not directory:
                return
            path = "/api/objects/download-bulk?" + urllib.parse.urlencode(
                {"bucket": bucket, "prefix": key.rstrip("/")})
            # 目录打包是 ZIP 流（无 Content-Length），保持单流，不并发
            self._download(path, Path(directory), f"{key} 目录", parallel=False)
            return
        save = filedialog.asksaveasfilename(parent=self, title="保存文件",
                                           initialfile=Path(key).name)
        if not save:
            return
        path = "/api/objects/download?" + urllib.parse.urlencode({"bucket": bucket, "key": key})
        self._download(path, Path(save), key, parallel=True)

    def download_current(self):
        bucket = self.bucket_var.get().strip()
        if not bucket:
            messagebox.showwarning("缺少 bucket", "请先选择 bucket", parent=self)
            return
        directory = filedialog.askdirectory(parent=self, title="选择下载目录（当前目录）")
        if not directory:
            return
        path = "/api/objects/download-bulk?" + urllib.parse.urlencode(
            {"bucket": bucket, "prefix": self.prefix})
        self._download(path, Path(directory), self.prefix or bucket, parallel=False)

    def _download(self, path, dest: Path, label: str, parallel: bool = True):
        """启动下载线程：单文件可分块并发，目录 ZIP 单流；可取消、进度合并刷新。"""
        if self.downloading:
            messagebox.showwarning(
                "正在下载", "已有下载任务在进行，请先等待完成或点击「取消下载」。",
                parent=self)
            return
        concurrency = clamp_download_concurrency(self.download_concurrency_var.get())
        self.download_concurrency_var.set(str(concurrency))
        parallel = bool(parallel) and concurrency > 1
        if parallel:
            # 记住本地上次使用的并发数（非敏感，写入客户端 UI 状态文件）
            try:
                core.save_ui_state(download_concurrency=concurrency)
            except Exception:
                pass

        client = self.client()
        cancel = threading.Event()
        self.downloading = True
        self.download_cancel = cancel
        self.download_cancel_btn.config(state="normal")
        self._update_actions()
        self._dl_reset(label)
        self.app.set_status(f"开始下载：{label}")

        def progress(done, total):
            self._on_download_progress(label, done, total)

        def work():
            try:
                saved = client.download_to(
                    path, dest, progress=progress, cancel=cancel,
                    concurrency=concurrency if parallel else 1)
            except core.Cancelled:
                self.app.root.after(0, self._download_cancelled, label)
            except Exception as e:
                self.app.root.after(0, self._download_failed, e)
            else:
                self.app.root.after(0, self._download_done, saved)

        threading.Thread(target=work, daemon=True).start()

    # -- 下载进度（worker 线程只更新共享状态，UI 线程合并刷新）--
    def _dl_reset(self, label: str) -> None:
        with self._dl_lock:
            self._dl_latest = None
            self._dl_scheduled = False
        self._dl_cancelling = False
        self._dl_label = label
        self._dl_last_time = time.monotonic()
        self._dl_last_bytes = 0

    def _on_download_progress(self, label: str, done: int, total: int) -> None:
        """worker 线程调用：记录最新进度，保证 UI 线程 >=150ms 才刷新一次。"""
        with self._dl_lock:
            self._dl_latest = (int(done), int(total))
            if self._dl_scheduled:
                return
            self._dl_scheduled = True
        try:
            self.app.root.after(150, self._flush_download_progress)
        except Exception:
            with self._dl_lock:
                self._dl_scheduled = False

    def _flush_download_progress(self) -> None:
        """UI 线程：合并刷新「已下载 x / y（p%） · 速度 z/s」。"""
        with self._dl_lock:
            latest = self._dl_latest
            self._dl_scheduled = False
        if latest is None or not self.downloading or self._dl_cancelling:
            return
        done, total = latest
        now = time.monotonic()
        elapsed = max(0.001, now - self._dl_last_time)
        speed = max(0.0, (done - self._dl_last_bytes) / elapsed)
        self._dl_last_time = now
        self._dl_last_bytes = done
        speed_text = core.format_speed(speed)
        if total:
            pct = done / total * 100
            text = (f"下载 {self._dl_label}：已下载 {fmt_size(done)} / {fmt_size(total)}"
                    f"（{pct:.1f}%） · {speed_text}")
        else:
            text = f"下载 {self._dl_label}：已下载 {fmt_size(done)} · {speed_text}"
        self.app.set_status(text)

    def _download_finish_ui(self) -> None:
        self.downloading = False
        self._dl_cancelling = False
        self.download_cancel_btn.config(state="disabled")
        self._update_actions()

    def _download_done(self, saved: Path) -> None:
        self._download_finish_ui()
        self.app.set_status(f"下载完成：{saved}")
        messagebox.showinfo("下载完成", f"已保存到：\n{saved}", parent=self)

    def _download_cancelled(self, label: str) -> None:
        self._download_finish_ui()
        self.app.set_status(f"已取消下载：{label}（临时文件已清理）")

    def _download_failed(self, exc: Exception) -> None:
        self._download_finish_ui()
        self.app.show_error(exc)

    def cancel_download(self) -> None:
        if not self.downloading:
            return
        self._dl_cancelling = True
        try:
            self.download_cancel.set()
        except Exception:
            pass
        self.download_cancel_btn.config(state="disabled")
        self.app.set_status("正在取消下载（等待当前分片结束）…")

    # ---------------- 上传 ----------------
    def choose_files(self):
        files = filedialog.askopenfilenames(parent=self, title="选择要上传的文件")
        if files:
            self.add_files(list(files))

    def add_files(self, files):
        """文件选择 / 拖拽共用。"""
        added = 0
        with self.queue_lock:
            for f in files:
                p = Path(f)
                if not p.is_file():
                    continue
                if any(q["path"] == str(p) for q in self.queue):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                self.queue.append({"path": str(p), "name": p.name, "size": size,
                                   "pct": 0, "status": "排队中"})
                added += 1
        if added:
            self._render_queue()
            self.app.set_status(f"已加入 {added} 个文件到上传队列")

    def clear_queue(self):
        if self.uploading:
            return
        with self.queue_lock:
            self.queue = [q for q in self.queue if q["status"] not in ("成功", "失败", "已取消")]
        self._render_queue()

    def _render_queue(self):
        self.queue_tree.delete(*self.queue_tree.get_children())
        with self.queue_lock:
            for q in self.queue:
                self.queue_tree.insert("", "end", iid=q["path"], values=(
                    q["name"], fmt_size(q["size"]), f"{q['pct']}%", q["status"]))

    def _queue_update(self, q):
        try:
            self.queue_tree.item(q["path"], values=(
                q["name"], fmt_size(q["size"]), f"{q['pct']}%", q["status"]))
        except Exception:
            self._render_queue()

    def start_upload(self):
        if self.uploading:
            return
        bucket = self.bucket_var.get().strip()
        token = self.app.token_var.get().strip()
        if not bucket:
            messagebox.showwarning("缺少 bucket", "请先选择 bucket", parent=self)
            return
        if not token:
            messagebox.showwarning("缺少令牌", "请先在顶部填写访问令牌", parent=self)
            return
        with self.queue_lock:
            pending = [q for q in self.queue if q["status"] in ("排队中", "失败", "已取消")]
        if not pending:
            messagebox.showinfo("上传队列", "没有待上传文件", parent=self)
            return
        try:
            concurrency = max(1, min(10, int(self.concurrency_var.get())))
        except ValueError:
            concurrency = 3
        self.uploading = True
        self.cancel_event.clear()
        self.upload_progress["value"] = 0
        self.app.set_status(f"开始上传 {len(pending)} 个文件，目标 {bucket} / {self.prefix or '/'}")

        client = self.client()
        source_url = self.source_var.get().strip()
        overwrite = self.overwrite_var.get()
        resume = self.resume_var.get()

        def work():
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = []
                for q in pending:
                    q["status"] = "等待中"
                    q["pct"] = 0
                    key = core.make_key(self.prefix, q["name"])
                    futures.append(pool.submit(self._upload_one, client, q, bucket, key,
                                               source_url, overwrite, resume))
                for _ in futures:
                    pass
            self.app.root.after(0, self._upload_finished)

        threading.Thread(target=work, daemon=True).start()

    def _upload_one(self, client, q, bucket, key, source_url, overwrite, resume):
        def progress(done, total, speed, msg):
            pct = int(done * 100 / total) if total else 0
            q["pct"] = pct
            q["status"] = msg
            self.app.root.after(0, self._queue_update, q)
        q["status"] = "上传中"
        self.app.root.after(0, self._queue_update, q)
        try:
            core.upload_file(client, q["path"], bucket, key,
                             overwrite=overwrite, resume=resume,
                             progress=progress, log=None,
                             cancel_event=self.cancel_event,
                             source_url=source_url)
            q["pct"] = 100
            q["status"] = "成功"
        except core.Cancelled:
            q["status"] = "已取消"
        except Exception as e:
            q["status"] = f"失败：{e}"
        self.app.root.after(0, self._queue_update, q)

    def _upload_finished(self):
        self.uploading = False
        self.upload_progress["value"] = 0
        with self.queue_lock:
            ok = sum(1 for q in self.queue if q["status"] == "成功")
            bad = sum(1 for q in self.queue if str(q["status"]).startswith("失败"))
        self.app.set_status(f"上传结束：成功 {ok}，失败 {bad}")
        self.refresh()

    def cancel_upload(self):
        if self.uploading:
            self.cancel_event.set()
            self.app.set_status("已请求取消上传（当前分片完成后停止）")


class LinksTab(ttk.Frame):
    """签名链接管理。"""

    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self.links = []
        self._build()
        # 不自动查询：没有令牌时查询会弹错误框；用户点“刷新”即可

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(bar, text="签名链接", style="Title.TLabel").pack(side="left")
        ttk.Label(bar, text="使用顶部令牌查询；管理员可见全部，普通用户仅见范围内/本人链接",
                  style="PanelMuted.TLabel").pack(side="left", padx=14)
        ttk.Button(bar, text="刷新", style="Accent.TButton",
                   command=self.refresh).pack(side="right")
        box = ttk.Frame(self, style="Panel.TFrame")
        box.grid(row=1, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        cols = ("creator", "file", "mode", "remaining", "created", "status", "url")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor in (
                ("creator", "创建者", 110, "w"), ("file", "文件", 330, "w"),
                ("mode", "类型", 110, "w"), ("remaining", "剩余", 90, "w"),
                ("created", "创建时间", 170, "w"), ("status", "状态", 100, "w"),
                ("url", "链接", 260, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs = make_scrollbar(box, self.tree.yview)
        vs.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.tag_configure("revoked", foreground="#7f93c4")
        actions = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="复制链接", style="Ghost.TButton",
                   command=self.copy_selected).pack(side="left")
        ttk.Button(actions, text="作废链接", style="Danger.TButton",
                   command=self.revoke_selected).pack(side="left", padx=8)
        self.info = ttk.Label(actions, text="", style="PanelMuted.TLabel")
        self.info.pack(side="right")

    def refresh(self):
        client = self.app.client()
        def work():
            try:
                links = client.list_signed_links()
            except Exception as e:
                self.app.root.after(0, self.app.show_error, e)
                return
            self.app.root.after(0, self._apply, links)
        threading.Thread(target=work, daemon=True).start()

    def _apply(self, links):
        self.links = links
        self.tree.delete(*self.tree.get_children())
        for idx, link in enumerate(links):
            mode = link.get("mode")
            if mode == "count":
                mode_text = f"次数 {link.get('max_uses')}"
                remaining = f"{link.get('remaining', 0)} 次"
            elif mode == "time":
                mode_text = "按时效"
                remaining = "—"
            else:
                mode_text = "永久"
                remaining = "—"
            if link.get("revoked"):
                status = "已作废"
            elif mode == "count" and int(link.get("remaining") or 0) <= 0:
                status = "已用完"
            else:
                status = "有效"
            url = link.get("url") or ""
            self.tree.insert("", "end", iid=f"l{idx}", values=(
                link.get("created_by") or "", f"{link.get('bucket')}/{link.get('key')}",
                mode_text, remaining, fmt_time(link.get("created_at") or ""),
                status, url or "-"), tags=("revoked",) if link.get("revoked") else ())
        self.info.config(text=f"{len(links)} 条链接")

    def selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return self.links[int(sel[0][1:])]
        except Exception:
            return None

    def copy_selected(self):
        link = self.selected()
        if not link:
            return
        url = link.get("url") or ""
        if not url:
            messagebox.showinfo("链接不可见", "完整链接仅管理员/创建者可见", parent=self)
            return
        full = url if url.startswith("http") else self.app.server_var.get().rstrip("/") + url
        self.app.copy_text(full)
        self.app.set_status("链接已复制")

    def revoke_selected(self):
        link = self.selected()
        if not link:
            return
        if not messagebox.askyesno("作废链接", "作废后该链接立即失效，确定？", parent=self):
            return
        try:
            self.app.client().revoke_signed_link(str(link.get("id") or ""))
            self.app.set_status("链接已作废")
            self.refresh()
        except Exception as e:
            self.app.show_error(e)


class AuditTab(ttk.Frame):
    """审计日志查询。"""

    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self._build()

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(bar, text="审计日志", style="Title.TLabel").pack(side="left")
        ttk.Label(bar, text="管理员令牌", style="PanelMuted.TLabel").pack(side="left", padx=(16, 4))
        self.admin_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.admin_var, width=24, show="*").pack(side="left")
        self.bucket_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.actor_var = tk.StringVar()
        self.since_var = tk.StringVar()
        for var, ph in ((self.bucket_var, "bucket"), (self.key_var, "key"),
                        (self.actor_var, "操作者"), (self.since_var, "起始时间")):
            ttk.Label(bar, text=ph, style="PanelMuted.TLabel").pack(side="left", padx=(6, 2))
            ttk.Entry(bar, textvariable=var, width=14).pack(side="left", padx=2)
        ttk.Button(bar, text="查询", style="Accent.TButton", command=self.query).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", style="Ghost.TButton", command=self.clear).pack(side="left")
        box = ttk.Frame(self, style="Panel.TFrame")
        box.grid(row=1, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        cols = ("time", "action", "bucket", "key", "actor", "ip", "public_ip", "size")
        self.tree = ttk.Treeview(box, columns=cols, show="headings")
        for col, text, width, anchor in (
                ("time", "时间", 170, "w"), ("action", "动作", 100, "w"),
                ("bucket", "Bucket", 110, "w"), ("key", "Key", 320, "w"),
                ("actor", "操作者", 110, "w"), ("ip", "IP", 120, "w"),
                ("public_ip", "代理 IP", 110, "w"), ("size", "大小", 100, "e")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs = make_scrollbar(box, self.tree.yview)
        vs.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vs.set)
        self.info = ttk.Label(self, text="填写管理员令牌后查询（默认最近 500 条，倒序显示）",
                              style="Muted.TLabel")
        self.info.grid(row=2, column=0, sticky="w", pady=(6, 0))

    def clear(self):
        for var in (self.bucket_var, self.key_var, self.actor_var, self.since_var):
            var.set("")
        self.tree.delete(*self.tree.get_children())
        self.info.config(text="已清空")

    def query(self):
        token = self.admin_var.get().strip() or self.app.token_var.get().strip()
        if not token:
            messagebox.showwarning("缺少令牌", "请填写管理员令牌", parent=self)
            return
        client = self.app.client(token)
        def work():
            try:
                rows = client.query_audit(
                    self.bucket_var.get().strip(), self.key_var.get().strip(),
                    self.actor_var.get().strip(), self.since_var.get().strip(), 500)
            except Exception as e:
                self.app.root.after(0, self.app.show_error, e)
                return
            self.app.root.after(0, self._apply, rows)
        threading.Thread(target=work, daemon=True).start()

    def _apply(self, rows):
        rows = list(rows)
        rows.reverse()
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert("", "end", values=(
                fmt_time(row.get("time") or ""), row.get("action") or "",
                row.get("bucket") or "", row.get("key") or "",
                row.get("actor") or "", row.get("ip") or "",
                row.get("public_ip") or "", fmt_size(row.get("size"))))
        self.info.config(text=f"共 {len(rows)} 条")


class TokensTab(ttk.Frame):
    """Token 注册表 + 生成 Token（权限点/资源范围/角色预设）。"""

    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self.catalog = {}
        self.roles = []
        self.permissions = []
        self.scope_vars = {}
        self.bucket_vars = {}
        self.token_rows = []
        self.admin_var = tk.StringVar()
        self.info_var = tk.StringVar(value="填写管理员令牌后查询/生成")
        self.reveal_var = tk.BooleanVar(value=False)
        self.user_var = tk.StringVar()
        self.role_var = tk.StringVar()
        self.expires_var = tk.StringVar()
        self.desc_var = tk.StringVar()
        self.buckets_manual_var = tk.StringVar()
        self.all_buckets_var = tk.BooleanVar(value=True)
        self.prefixes_var = tk.StringVar()
        self.range_var = tk.StringVar(value="生效范围：⚠️ 全部 bucket")
        self.result_var = tk.StringVar()
        self._build()
        self.load_catalog()

    # ---------------- UI ----------------
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(bar, text="Token 管理", style="Title.TLabel").pack(side="left")
        ttk.Label(bar, text="管理员令牌", style="PanelMuted.TLabel").pack(side="left", padx=(16, 4))
        ttk.Entry(bar, textvariable=self.admin_var, width=24, show="*").pack(side="left")
        ttk.Button(bar, text="查询注册表", style="Accent.TButton",
                   command=self.load_tokens).pack(side="left", padx=6)
        ttk.Button(bar, text="从 DataHub 同步", style="Ghost.TButton",
                   command=self.sync_tokens).pack(side="left")
        ttk.Button(bar, text="重新加载配置", style="Ghost.TButton",
                   command=self.reload_config).pack(side="left", padx=6)
        ttk.Checkbutton(bar, text="显示明文", variable=self.reveal_var,
                        command=self.load_tokens).pack(side="left", padx=(10, 0))
        ttk.Label(bar, textvariable=self.info_var, style="PanelMuted.TLabel").pack(side="left", padx=12)

        self.inner = ttk.Notebook(self)
        self.inner.grid(row=1, column=0, sticky="nsew")
        reg = ttk.Frame(self.inner, padding=8)
        gen = ttk.Frame(self.inner, padding=8)
        self.inner.add(reg, text="注册表")
        self.inner.add(gen, text="生成 Token")
        self._build_registry(reg)
        self._build_generator(gen)

    def _build_registry(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        cols = ("user", "token", "role", "scopes", "range", "expires", "status")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor in (
                ("user", "用户", 120, "w"), ("token", "Token", 220, "w"),
                ("role", "角色", 100, "w"), ("scopes", "权限点", 70, "center"),
                ("range", "资源范围", 330, "w"), ("expires", "过期", 150, "w"),
                ("status", "状态", 90, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs = make_scrollbar(parent, self.tree.yview)
        vs.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())
        detail_box = ttk.Frame(parent, style="Panel.TFrame")
        detail_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        detail_box.columnconfigure(0, weight=1)
        self.detail = ttk.Label(detail_box, text="", style="Panel.TLabel",
                                justify="left", wraplength=1060)
        self.detail.grid(row=0, column=0, sticky="ew", pady=4)
        ttk.Button(parent, text="移除选中 Token", style="Danger.TButton",
                   command=self.remove_selected).grid(row=2, column=0, sticky="w", pady=(8, 0))

    def _build_generator(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        scroll = ScrollFrame(parent, height=430)
        scroll.grid(row=0, column=0, sticky="nsew")
        g = scroll.inner
        g.columnconfigure(1, weight=1)

        ttk.Label(g, text="用户名", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(g, textvariable=self.user_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(g, text="角色预设", style="Panel.TLabel").grid(row=0, column=2, sticky="w")
        self.role_combo = ttk.Combobox(g, textvariable=self.role_var, width=18, state="readonly")
        self.role_combo.grid(row=0, column=3, padx=6)
        self.role_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_role())
        ttk.Label(g, text="过期", style="Panel.TLabel").grid(row=0, column=4, sticky="w", padx=(8, 0))
        ttk.Entry(g, textvariable=self.expires_var, width=16).grid(row=0, column=5, padx=6)

        ttk.Label(g, text="说明", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(g, textvariable=self.desc_var).grid(row=1, column=1, columnspan=3, sticky="ew", padx=8)

        bucket_box = ttk.LabelFrame(g, text=" 资源范围 · Bucket 白名单 ", padding=8)
        bucket_box.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        bucket_box.columnconfigure(0, weight=1)
        top = ttk.Frame(bucket_box, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(top, text="全部 bucket（不限制）", variable=self.all_buckets_var,
                        style="Panel.TCheckbutton",
                        command=self._all_buckets).pack(side="left")
        ttk.Label(top, text="手填", style="Panel.TLabel").pack(side="left", padx=(12, 4))
        ttk.Entry(top, textvariable=self.buckets_manual_var, width=30).pack(side="left")
        ttk.Button(top, text="刷新现有 bucket", style="Ghost.TButton",
                   command=self.load_buckets).pack(side="left", padx=6)
        self.bucket_checks = ttk.Frame(bucket_box, style="Panel.TFrame")
        self.bucket_checks.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(bucket_box, text="路径前缀白名单（格式 bucket:前缀，逗号分隔；留空=前缀不限）",
                  style="PanelMuted.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 2))
        ttk.Entry(bucket_box, textvariable=self.prefixes_var).grid(row=3, column=0, sticky="ew")
        ttk.Label(bucket_box, textvariable=self.range_var, style="Panel.TLabel",
                  foreground=WARN).grid(row=4, column=0, sticky="w", pady=(6, 0))

        toolbar = ttk.Frame(g, style="Panel.TFrame")
        toolbar.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(10, 4))
        ttk.Label(toolbar, text="权限点", style="Panel.TLabel").pack(side="left")
        ttk.Button(toolbar, text="全选", style="Ghost.TButton",
                   command=lambda: self.select_all(True)).pack(side="left", padx=(10, 4))
        ttk.Button(toolbar, text="全不选", style="Ghost.TButton",
                   command=lambda: self.select_all(False)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="仅勾选读", style="Ghost.TButton",
                   command=lambda: self.select_write(False)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="仅勾选写", style="Ghost.TButton",
                   command=lambda: self.select_write(True)).pack(side="left", padx=4)

        self.scope_box = ttk.Frame(g, style="Panel.TFrame")
        self.scope_box.grid(row=4, column=0, columnspan=6, sticky="ew")
        self.scope_box.columnconfigure(0, weight=1)

        result_box = ttk.Frame(g, style="Panel.TFrame")
        result_box.grid(row=5, column=0, columnspan=6, sticky="ew", pady=(12, 0))
        ttk.Button(result_box, text="生成本轮 Token", style="Accent.TButton",
                   command=self.generate).pack(side="left")
        ttk.Button(result_box, text="复制新 Token", style="Ghost.TButton",
                   command=self.copy_result).pack(side="left", padx=8)
        ttk.Entry(g, textvariable=self.result_var, state="readonly").grid(
            row=6, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ttk.Label(g, text="Token 仅生成后展示一次，请立即复制保存。",
                  style="PanelMuted.TLabel").grid(row=7, column=0, columnspan=6, sticky="w", pady=(4, 0))

        self.buckets_manual_var.trace_add("write", lambda *a: self._range_changed())
        self.prefixes_var.trace_add("write", lambda *a: self._range_changed())
        self.all_buckets_var.trace_add("write", lambda *a: self._range_changed())

    # ---------------- catalog ----------------
    def load_catalog(self):
        client = self.app.client(self.admin_var.get().strip() or None)
        def work():
            try:
                data = client.permissions()
            except Exception as e:
                self.app.root.after(0, lambda: self.info_var.set(f"权限目录加载失败：{e}"))
                return
            self.app.root.after(0, self._apply_catalog, data)
        threading.Thread(target=work, daemon=True).start()

    def _apply_catalog(self, data):
        self.catalog = data or {}
        self.permissions = list(self.catalog.get("permissions") or [])
        self.roles = list(self.catalog.get("roles") or [])
        values = [str(r.get("key") or "") for r in self.roles]
        self.role_combo["values"] = values
        default_key = self.catalog.get("default_role") or "user"
        for idx, r in enumerate(self.roles):
            if r.get("key") == default_key:
                self.role_combo.current(idx)
                self.role_var.set(values[idx])
                break
        else:
            if values:
                self.role_combo.current(0)
                self.role_var.set(values[0])
        # scope 分组
        for w in self.scope_box.winfo_children():
            w.destroy()
        self.scope_vars = {}
        groups = {}
        for p in self.permissions:
            groups.setdefault(str(p.get("group") or "其他"), []).append(p)
        row = 0
        for group, items in groups.items():
            ttk.Label(self.scope_box, text=group, style="Panel.TLabel",
                      font=(FONT, 10, "bold"), foreground=ACCENT2).grid(
                row=row, column=0, sticky="w", pady=(8, 2))
            row += 1
            cols = 2
            for i, p in enumerate(items):
                key = str(p.get("key") or "")
                var = tk.BooleanVar(value=False)
                self.scope_vars[key] = var
                text = f"{p.get('label')}  ·  {key}"
                ttk.Checkbutton(self.scope_box, text=text, variable=var,
                                style="Panel.TCheckbutton").grid(
                    row=row + i // cols, column=i % cols, sticky="w", padx=(4, 24), pady=1)
            row += (len(items) + cols - 1) // cols
        self.apply_role()
        self.load_buckets()
        self._range_changed()

    def _role_key(self):
        text = self.role_var.get()
        return text.split(" — ", 1)[0] if " — " in text else text.strip()

    def apply_role(self):
        key = self._role_key()
        preset = next((r for r in self.roles if r.get("key") == key), None)
        scopes = set((preset or {}).get("scopes") or [])
        for k, var in self.scope_vars.items():
            var.set(k in scopes)
        self._range_changed()

    def select_all(self, on):
        for var in self.scope_vars.values():
            var.set(bool(on))

    def select_write(self, write):
        for p in self.permissions:
            key = str(p.get("key") or "")
            if key in self.scope_vars:
                self.scope_vars[key].set(bool(p.get("write")) == bool(write))

    # ---------------- buckets / ranges ----------------
    def load_buckets(self):
        client = self.app.client(self.admin_var.get().strip() or None)
        def work():
            try:
                buckets = client.list_buckets()
            except Exception:
                return
            self.app.root.after(0, self._render_buckets, buckets)
        threading.Thread(target=work, daemon=True).start()

    def _render_buckets(self, buckets):
        for w in self.bucket_checks.winfo_children():
            w.destroy()
        old = {k: v.get() for k, v in self.bucket_vars.items()}
        self.bucket_vars = {}
        for idx, name in enumerate(buckets):
            var = tk.BooleanVar(value=old.get(name, False))
            self.bucket_vars[name] = var
            var.trace_add("write", lambda *a: self._range_changed())
            ttk.Checkbutton(self.bucket_checks, text=name, variable=var,
                            style="Panel.TCheckbutton").grid(
                row=idx // 5, column=idx % 5, sticky="w", padx=(0, 16), pady=2)
        self._range_changed()

    def _all_buckets(self):
        if self.all_buckets_var.get():
            for var in self.bucket_vars.values():
                var.set(False)
            self.buckets_manual_var.set("")
        self._range_changed()

    def collect_buckets(self) -> list:
        if self.all_buckets_var.get():
            return []
        out = [name for name, var in self.bucket_vars.items() if var.get()]
        for name in parse_list(self.buckets_manual_var.get()):
            if name not in out:
                out.append(name)
        return out

    def _range_changed(self):
        prefixes = parse_prefixes(self.prefixes_var.get())
        buckets = self.collect_buckets()
        outside = [b for b in prefixes if buckets and b not in buckets]
        text = "生效范围：" + range_text({"allow_buckets": buckets, "allow_prefixes": prefixes})
        if outside:
            text += "　⚠️ 前缀 bucket 不在白名单：" + "、".join(outside)
        self.range_var.set(text)

    # ---------------- registry ----------------
    def _admin_client(self):
        token = self.admin_var.get().strip()
        if not token:
            raise ValueError("请填写管理员令牌")
        return self.app.client(token)

    def load_tokens(self):
        try:
            client = self._admin_client()
        except Exception as e:
            self.app.show_error(e)
            return
        reveal = self.reveal_var.get()
        def work():
            try:
                mapping = client.list_tokens(reveal=reveal)
            except Exception as e:
                self.app.root.after(0, self.app.show_error, e)
                return
            self.app.root.after(0, self._apply_tokens, mapping)
        threading.Thread(target=work, daemon=True).start()

    def _apply_tokens(self, mapping):
        self.tree.delete(*self.tree.get_children())
        self.token_rows = sorted(mapping.values(), key=lambda r: str(r.get("user") or ""))
        for idx, rec in enumerate(self.token_rows):
            if rec.get("expired"):
                status = "已过期"
            elif rec.get("enabled") is False:
                status = "已禁用"
            else:
                status = "有效"
            self.tree.insert("", "end", iid=f"t{idx}", values=(
                rec.get("user") or "", rec.get("token") or "", rec.get("role") or "",
                len(rec.get("scopes") or []), range_text(rec),
                rec.get("expires_at") or "永久", status))
        self.info_var.set(f"Token 共 {len(self.token_rows)} 条")

    def _show_detail(self):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            rec = self.token_rows[int(sel[0][1:])]
        except Exception:
            return
        text = "权限点：" + ("  ".join(rec.get("scopes") or []) or "（无）")
        if rec.get("description"):
            text += "\n说明：" + str(rec.get("description"))
        text += "\n范围：" + range_text(rec)
        self.detail.config(text=text)

    def remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("移除 Token", "请先选择一条记录", parent=self)
            return
        try:
            rec = self.token_rows[int(sel[0][1:])]
        except Exception:
            return
        token = str(rec.get("token") or "")
        if "****" in token:
            token = ask_text(self, "移除 Token", "该行已脱敏，请输入完整 token 以移除：") or ""
            if not token:
                return
        if not messagebox.askyesno("移除 Token", f"确定移除用户 {rec.get('user')} 的 token？", parent=self):
            return
        try:
            self._admin_client().delete_token(token)
            self.app.set_status("Token 已移除")
            self.load_tokens()
        except Exception as e:
            self.app.show_error(e)

    def sync_tokens(self):
        try:
            client = self._admin_client()
        except Exception as e:
            self.app.show_error(e)
            return
        def work():
            try:
                data = client.sync_tokens()
            except Exception as e:
                self.app.root.after(0, self.app.show_error, e)
                return
            def apply():
                n = len(data.get("mapping") or {})
                if data.get("ok"):
                    self.info_var.set(f"已从 {data.get('datahub_url') or '?'} 同步 {n} 个 token")
                else:
                    self.info_var.set(f"同步失败：{data.get('error') or 'DataHub 不可达'}")
                self.load_tokens()
            self.app.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def reload_config(self):
        try:
            client = self._admin_client()
        except Exception as e:
            self.app.show_error(e)
            return
        def work():
            try:
                data = client.reload_config()
            except Exception as e:
                self.app.root.after(0, self.app.show_error, e)
                return
            def apply():
                parts = []
                if data.get("changed"):
                    parts.append("已生效：" + "、".join(data["changed"]))
                if data.get("needs_restart"):
                    parts.append("需重启：" + "、".join(data["needs_restart"]))
                if data.get("warnings"):
                    parts.append("提示：" + "；".join(data["warnings"]))
                self.info_var.set("配置重载：" + ("；".join(parts) if parts else "无变化"))
                self.load_catalog()
            self.app.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    # ---------------- generate ----------------
    def generate(self):
        try:
            client = self._admin_client()
        except Exception as e:
            self.app.show_error(e)
            return
        user = self.user_var.get().strip()
        if not user:
            messagebox.showwarning("缺少用户名", "请填写用户名", parent=self)
            return
        scopes = [k for k, var in self.scope_vars.items() if var.get()]
        role = self._role_key()
        if not scopes and role != "custom":
            if not messagebox.askyesno("权限为空", "未勾选任何权限点，生成的 token 将无任何权限。继续？",
                                       parent=self):
                return
        buckets = self.collect_buckets()
        prefixes = parse_prefixes(self.prefixes_var.get())
        prefix_buckets = list(prefixes.keys())
        if buckets:
            outside = [b for b in prefix_buckets if b not in buckets]
            if outside:
                messagebox.showwarning("范围不一致",
                                       "前缀白名单里的 bucket 不在 Bucket 白名单内：" + "、".join(outside),
                                       parent=self)
                return
        if not buckets and not prefix_buckets:
            if not messagebox.askyesno("范围为空", "该 token 可访问全部 bucket（含未来新建）。确认继续？",
                                       parent=self):
                return
        payload = {
            "user": user,
            "role": role,
            "scopes": scopes,
            "allow_buckets": buckets,
            "allow_prefixes": prefixes,
            "expires_at": self.expires_var.get().strip(),
            "description": self.desc_var.get().strip(),
        }
        def work():
            try:
                data = client.create_token(payload)
            except Exception as e:
                self.app.root.after(0, self.app.show_error, e)
                return
            self.app.root.after(0, lambda: self._generated(data))
        threading.Thread(target=work, daemon=True).start()

    def _generated(self, data):
        self.result_var.set(data.get("token") or "")
        self.info_var.set("已生成 Token（仅展示本次）；范围：" + range_text(data))
        self.app.set_status("Token 已生成，请立即复制")
        self.load_tokens()

    def copy_result(self):
        value = self.result_var.get().strip()
        if not value:
            return
        self.app.copy_text(value)
        self.app.set_status("Token 已复制")


class App:
    def __init__(self):
        self.dnd_ok = False
        if HAS_DND:
            try:
                self.root = TkinterDnD.Tk()
                self.dnd_ok = True
            except Exception:
                self.root = tk.Tk()
        else:
            self.root = tk.Tk()
        configure_theme(self.root)
        self.root.title("ArtifactDepot")
        self.root.geometry("1180x820")
        self.root.minsize(1000, 680)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        saved = core.load_ui_state()
        self.server_var = tk.StringVar(value=core.normalize_server(
            os.environ.get("ARTIFACTDEPOT_SERVER") or saved.get("server") or core.DEFAULT_SERVER))
        self.token_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")

        self._build_header()
        self._build_conn()
        ttk.Label(self.root, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w", padding=(16, 6)).pack(side="bottom", fill="x")
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(4, 0))
        self.browse = BrowseTab(self.notebook, self)
        self.links = LinksTab(self.notebook, self)
        self.tokens = TokensTab(self.notebook, self)
        self.audit = AuditTab(self.notebook, self)
        self.notebook.add(self.browse, text=" 文件浏览 ")
        self.notebook.add(self.links, text=" 签名链接 ")
        self.notebook.add(self.tokens, text=" Token 管理 ")
        self.notebook.add(self.audit, text=" 审计日志 ")
        self.notebook.select(self.browse)
        if self.dnd_ok:
            try:
                self.root.drop_target_register(DND_FILES)
                self.root.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                self.dnd_ok = False

    # ---------------- header ----------------
    def _build_header(self):
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(20, 12))
        header.pack(fill="x")
        ttk.Label(header, text="ArtifactDepot", style="HeaderTitle.TLabel").pack(side="left")
        ttk.Label(header, text="对象仓库", style="HeaderSub.TLabel").pack(
            side="left", padx=(14, 0), pady=(6, 0))

    # ---------------- connection ----------------
    def _build_conn(self):
        bar = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 9))
        bar.pack(fill="x")
        ttk.Label(bar, text="服务地址", style="PanelMuted.TLabel").pack(side="left")
        server = ttk.Entry(bar, textvariable=self.server_var, width=30)
        server.pack(side="left", padx=(6, 12))
        server.bind("<Return>", lambda e: self.verify())
        ttk.Label(bar, text="访问令牌", style="PanelMuted.TLabel").pack(side="left")
        token = ttk.Entry(bar, textvariable=self.token_var, width=38)
        token.pack(side="left", padx=(6, 10))
        token.bind("<Return>", lambda e: self.verify())
        ttk.Button(bar, text="校验令牌", style="Accent.TButton",
                   command=self.verify).pack(side="left")
        self.conn_info = ttk.Label(bar, text="", style="PanelMuted.TLabel")
        self.conn_info.pack(side="left", padx=12)


    def _login_help(self):
        messagebox.showinfo(
            "令牌说明",
            "访问令牌填 api_token 或管理员共享 access_token。\n\n"
            "文件浏览/签名链接用顶部令牌；\n"
            "Token 管理与审计在各自页签填管理员令牌。\n\n"
            "Token 只保存在内存，不写入磁盘。",
            parent=self.root)

    def client(self, token=None):
        server = self.server_var.get().strip() or core.DEFAULT_SERVER
        tok = self.token_var.get().strip() if token is None else (token or "").strip()
        return core.ArtifactDepotClient(server, tok)

    def verify(self):
        client = self.client()
        self.conn_info.config(text="校验中…", foreground=MUTED)
        def work():
            try:
                data = client.check()
            except Exception as e:
                self.root.after(0, lambda: self.conn_info.config(
                    text=f"连接失败：{e}", foreground=DANGER))
                return
            if data.get("valid"):
                text = f"已连接：{data.get('actor') or '?'}（{'管理员' if data.get('is_admin') else data.get('role') or '用户'}）"
                color = SUCCESS
            else:
                text = "令牌无效或已过期"
                color = DANGER
            self.root.after(0, lambda: (
                self.conn_info.config(text=text, foreground=color),
                self.set_status(text)))
        threading.Thread(target=work, daemon=True).start()

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self.browse.add_files([str(p) for p in paths])
        self.set_status(f"已加入 {len(paths)} 个文件")

    # ---------------- utils ----------------
    def set_status(self, text: str) -> None:
        self.status_var.set(str(text))

    def show_error(self, exc: Exception) -> None:
        text = str(exc)
        self.set_status("错误：" + text)
        messagebox.showerror("操作失败", text, parent=self.root)

    def copy_text(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def wait_window(self, win) -> None:
        self.root.wait_window(win)

    def _on_close(self):
        try:
            core.save_ui_state(server=core.normalize_server(self.server_var.get()))
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_gui() -> int:
    try:
        app = App()
    except Exception as e:
        print(f"无法启动图形界面：{e}", file=sys.stderr)
        print("可改用 CLI：--file <本地文件> --bucket <bucket> --path <目录> --token <token>",
              file=sys.stderr)
        return 2
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_gui())
