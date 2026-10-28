#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情监控 — 桌面透明悬浮窗。

监控 A 股自选股、伦敦金现、伦铜。
界面只保留行情文字：无标题栏、无底部提示、灰色字体、透明背景。
在文字上拖动移动，右键打开设置/菜单。
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox

from config import LOG_FILE, STARTUP_FILE, load_config, save_config
from quotes import fetch_quotes, normalize_symbol

APP_NAME = "行情监控"
FONT = "Microsoft YaHei UI"
WIDGET_FONT = "宋体"
TRANSPARENT_KEY = "#010203"

C_BG = "#15171c"
C_BORDER = "#2b3038"
C_TEXT = "#e6edf3"
C_MUTED = "#8b949e"
C_ACCENT = "#e6b45e"
C_GRAY = "#808080"


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


def monitor_work_area(x: int, y: int) -> tuple[int, int, int, int]:
    """返回坐标所在显示器的可用区域（已扣除任务栏），支持多显示器。"""
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [_POINT, ctypes.c_ulong]
        monitor = user32.MonitorFromPoint(_POINT(int(x), int(y)), 2)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcWork
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080


def short_code(code: str) -> str:
    """只显示代码后 4 位；外盘显示 XAU / CAD。"""
    if code.startswith("hf_"):
        return code.split("_", 1)[-1][-4:].upper()
    return code[-4:]


# ---------------------------------------------------------------------------
# 窗口 / 线程
# ---------------------------------------------------------------------------

class Fetcher(threading.Thread):
    """后台抓取线程：每 interval 秒抓一次，结果放进队列由 UI 线程消费。"""

    def __init__(self, app: "MarketApp"):
        super().__init__(daemon=True, name="quote-fetcher")
        self.app = app
        self.codes = list(app.cfg["symbols"])
        self.interval = int(app.cfg["interval"])
        self._stop_evt = threading.Event()
        self._wake_evt = threading.Event()

    def update(self, codes: list[str], interval: int) -> None:
        self.codes = list(codes)
        self.interval = int(interval)
        self._wake_evt.set()

    def request(self) -> None:
        self._wake_evt.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake_evt.set()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            codes = list(self.codes)
            started = time.monotonic()
            try:
                quotes = fetch_quotes(codes)
                error = None
            except Exception as exc:  # 网络错误时保留上一次报价，不退出
                quotes = {}
                error = str(exc) or exc.__class__.__name__
            if self._stop_evt.is_set():
                break
            try:
                self.app.queue.put_nowait((quotes, error, time.time()))
            except Exception:
                pass
            wait = max(1.0, self.interval - (time.monotonic() - started))
            self._wake_evt.wait(wait)
            self._wake_evt.clear()


def fmt_price(quote) -> str:
    text = f"{quote.price:,.2f}"
    return f"${text}" if quote.currency == "USD" else text


def fmt_pct(quote) -> str:
    pct = 0.0 if abs(quote.pct) < 0.005 else quote.pct
    return f"{pct:+.2f}%"


# ---------------------------------------------------------------------------
# 设置窗口
# ---------------------------------------------------------------------------

class SettingsDialog:
    def __init__(self, app: "MarketApp"):
        self.app = app
        self.cfg = app.cfg
        self.result: dict | None = None
        self.items = list(app.cfg["symbols"])

        self.dlg = tk.Toplevel(app.root)
        self.dlg.title("行情监控 · 设置")
        self.dlg.configure(bg=C_BG)
        self.dlg.resizable(False, False)
        self.dlg.attributes("-topmost", True)
        self.dlg.transient(app.root)
        self._build()
        self._center()
        self.dlg.protocol("WM_DELETE_WINDOW", self.dlg.destroy)
        self.dlg.bind("<Escape>", lambda e: self.dlg.destroy())
        self.dlg.grab_set()
        self.dlg.wait_window()

    # -- 布局 ---------------------------------------------------------------
    def _build(self) -> None:
        fs = int(self.cfg["font_size"])
        body = tk.Frame(self.dlg, bg=C_BG, padx=16, pady=14)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="监控代码", font=(FONT, 10), fg=C_TEXT, bg=C_BG).pack(anchor="w")
        tk.Label(
            body,
            text="A股填 6 位代码；指数写 sh000001；外盘用 hf_XAU / hf_CAD。",
            font=(FONT, 8), fg=C_MUTED, bg=C_BG,
        ).pack(anchor="w", pady=(1, 6))

        list_wrap = tk.Frame(body, bg=C_BG)
        list_wrap.pack(fill="x")
        self.lb = tk.Listbox(
            list_wrap, height=6, width=40, font=("Consolas", 10),
            bg="#1b1e24", fg=C_TEXT, selectbackground="#33506b", selectforeground="#ffffff",
            highlightthickness=1, highlightbackground=C_BORDER,
            activestyle="none", relief="flat", bd=0,
        )
        scroll = tk.Scrollbar(list_wrap, command=self.lb.yview, width=12)
        self.lb.configure(yscrollcommand=scroll.set)
        self.lb.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for code in self.items:
            self.lb.insert("end", code)

        entry_row = tk.Frame(body, bg=C_BG)
        entry_row.pack(fill="x", pady=(8, 4))
        self.entry = tk.Entry(
            entry_row, font=("Consolas", 10), bg="#1b1e24", fg=C_TEXT,
            insertbackground=C_TEXT, relief="flat", highlightthickness=1,
            highlightbackground=C_BORDER, highlightcolor=C_ACCENT,
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda e: self._add())
        self._button(entry_row, "添加", self._add).pack(side="left", padx=(6, 0))

        action_row = tk.Frame(body, bg=C_BG)
        action_row.pack(fill="x", pady=(2, 0))
        self._button(action_row, "删除", self._delete).pack(side="left")
        self._button(action_row, "上移", lambda: self._move(-1)).pack(side="left", padx=(6, 0))
        self._button(action_row, "下移", lambda: self._move(1)).pack(side="left", padx=(6, 0))

        tk.Frame(body, bg=C_BORDER, height=1).pack(fill="x", pady=12)

        opt = tk.Frame(body, bg=C_BG)
        opt.pack(fill="x")
        tk.Label(opt, text="刷新间隔", font=(FONT, 9), fg=C_TEXT, bg=C_BG).grid(row=0, column=0, sticky="w")
        self.interval_var = tk.StringVar(value=str(self.cfg["interval"]))
        tk.Spinbox(
            opt, from_=2, to=300, width=5, textvariable=self.interval_var,
            font=(FONT, 9), bg="#1b1e24", fg=C_TEXT, buttonbackground="#262b33",
            insertbackground=C_TEXT, relief="flat", highlightthickness=1,
            highlightbackground=C_BORDER, justify="center",
        ).grid(row=0, column=1, padx=(6, 4))
        tk.Label(opt, text="秒", font=(FONT, 9), fg=C_MUTED, bg=C_BG).grid(row=0, column=2, sticky="w")

        tk.Label(opt, text="字号", font=(FONT, 9), fg=C_TEXT, bg=C_BG).grid(row=0, column=3, padx=(24, 0), sticky="w")
        self.font_var = tk.StringVar(value=str(self.cfg["font_size"]))
        tk.Spinbox(
            opt, from_=8, to=16, width=4, textvariable=self.font_var,
            font=(FONT, 9), bg="#1b1e24", fg=C_TEXT, buttonbackground="#262b33",
            insertbackground=C_TEXT, relief="flat", highlightthickness=1,
            highlightbackground=C_BORDER, justify="center",
        ).grid(row=0, column=4, padx=(6, 0))

        checks = tk.Frame(body, bg=C_BG)
        checks.pack(fill="x", pady=(10, 0))
        self.top_var = tk.BooleanVar(value=bool(self.cfg["topmost"]))
        self._check(checks, "窗口始终置顶", self.top_var).pack(anchor="w")

        tk.Frame(body, bg=C_BORDER, height=1).pack(fill="x", pady=12)

        foot = tk.Frame(body, bg=C_BG)
        foot.pack(fill="x")
        self._button(foot, "保存", self._save, accent=True).pack(side="right")
        self._button(foot, "取消", self.dlg.destroy).pack(side="right", padx=(0, 8))

        self.dlg.update_idletasks()

    def _button(self, parent, text, command, accent: bool = False) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command,
            font=(FONT, 9),
            bg=C_ACCENT if accent else "#262b33",
            fg="#1c1c1c" if accent else C_TEXT,
            activebackground="#f0c674" if accent else "#313842",
            activeforeground="#1c1c1c" if accent else "#ffffff",
            relief="flat", bd=0, padx=14, pady=4, cursor="hand2",
        )

    def _check(self, parent, text, var) -> tk.Checkbutton:
        return tk.Checkbutton(
            parent, text=text, variable=var, font=(FONT, 9),
            bg=C_BG, fg=C_TEXT, activebackground=C_BG, activeforeground=C_ACCENT,
            selectcolor="#1b1e24", highlightthickness=0, bd=0, anchor="w",
        )

    def _center(self) -> None:
        self.dlg.update_idletasks()
        w = self.dlg.winfo_reqwidth()
        h = self.dlg.winfo_reqheight()
        root = self.app.root
        x = root.winfo_x() + (root.winfo_width() - w) // 2
        y = root.winfo_y() + (root.winfo_height() - h) // 2
        sw, sh = self.dlg.winfo_screenwidth(), self.dlg.winfo_screenheight()
        x = max(10, min(x, sw - w - 10))
        y = max(10, min(y, sh - h - 40))
        self.dlg.geometry(f"+{x}+{y}")

    # -- 操作 ---------------------------------------------------------------
    def _add(self) -> None:
        raw = self.entry.get().strip()
        if not raw:
            return
        raw = raw.replace("，", ",").replace("；", ",").replace(";", ",")
        parts = [p for p in raw.replace(",", " ").split() if p]
        added = []
        for part in parts:
            try:
                code = normalize_symbol(part)
            except ValueError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=self.dlg)
                continue
            if code in self.items:
                continue
            self.items.append(code)
            self.lb.insert("end", code)
            self.lb.selection_clear(0, "end")
            self.lb.selection_set("end")
            self.lb.see("end")
            added.append(code)
        if added:
            self.entry.delete(0, "end")

    def _delete(self) -> None:
        selection = list(self.lb.curselection())
        for index in reversed(selection):
            self.lb.delete(index)
            del self.items[index]

    def _move(self, delta: int) -> None:
        selection = self.lb.curselection()
        if not selection:
            return
        index = selection[0]
        new_index = index + delta
        if not 0 <= new_index < len(self.items):
            return
        self.items[index], self.items[new_index] = self.items[new_index], self.items[index]
        self.lb.delete(index)
        self.lb.insert(new_index, self.items[new_index])
        self.lb.selection_set(new_index)

    def _save(self) -> None:
        codes: list[str] = []
        for item in self.items:
            try:
                code = normalize_symbol(item)
            except ValueError:
                continue
            if code not in codes:
                codes.append(code)
        if not codes:
            messagebox.showerror(APP_NAME, "至少保留一个监控代码。", parent=self.dlg)
            return
        try:
            interval = int(float(self.interval_var.get()))
        except Exception:
            interval = 3
        try:
            font_size = int(float(self.font_var.get()))
        except Exception:
            font_size = 10
        self.result = {
            "symbols": codes,
            "interval": max(2, min(300, interval)),
            "font_size": max(8, min(16, font_size)),
            "topmost": bool(self.top_var.get()),
        }
        self.dlg.destroy()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class MarketApp:
    def __init__(self):
        self.cfg = load_config()
        self._clean_symbols()

        self._alive = True
        self._last_quotes: dict = {}
        self._drag: tuple[int, int] | None = None
        self.queue: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
        self.root.attributes("-alpha", 1.0)

        # 透明窗口只有文字像素能接收鼠标，空隙会穿透到桌面。
        # 加一层几乎不可见的“命中层”，让整个行情区域都能拖动/右键。
        self.hit = tk.Toplevel(self.root)
        self.hit.withdraw()
        self.hit.overrideredirect(True)
        self.hit.configure(bg="#808080")
        self.hit.attributes("-alpha", 0.004)
        self.hit.attributes("-topmost", bool(self.cfg["topmost"]))

        self.var_top = tk.BooleanVar(value=bool(self.cfg["topmost"]))
        self.var_lock = tk.BooleanVar(value=bool(self.cfg["locked"]))
        self.var_startup = tk.BooleanVar(value=STARTUP_FILE.exists())

        self._apply_window_attrs()
        self._build()
        self.root.update_idletasks()
        self._fit_window()
        self._build_menu()

        self.fetcher = Fetcher(self)
        self.fetcher.start()

        for widget in (self.root, self.hit):
            widget.bind("<ButtonPress-1>", self._press)
            widget.bind("<B1-Motion>", self._motion)
            widget.bind("<ButtonRelease-1>", self._release)
            widget.bind("<Button-3>", self._show_menu)
            widget.bind("<F5>", lambda e: self.refresh())
            widget.bind("<Control-q>", lambda e: self.quit())

        self.root.deiconify()
        self.root.attributes("-topmost", bool(self.cfg["topmost"]))
        self._make_root_click_through()
        self._poll()

    # -- 初始化 -------------------------------------------------------------
    def _clean_symbols(self) -> None:
        codes: list[str] = []
        for raw in self.cfg["symbols"]:
            try:
                code = normalize_symbol(raw)
            except ValueError:
                continue
            if code not in codes:
                codes.append(code)
        if not codes:
            codes = ["sh000001", "sh600362", "hf_XAU", "hf_CAD"]
        if codes != self.cfg["symbols"]:
            self.cfg["symbols"] = codes
            save_config(self.cfg)

    @property
    def bg(self) -> str:
        return TRANSPARENT_KEY

    def _build(self) -> None:
        bg = self.bg

        self.outer = tk.Frame(self.root, bg=bg)
        self.outer.pack(fill="both", expand=True)
        self.container = tk.Frame(self.outer, bg=bg, padx=8, pady=3)
        self.container.pack()

        self.rows = tk.Frame(self.container, bg=bg)
        self.rows.pack(fill="x")
        self.rows.grid_columnconfigure(0, minsize=44)
        self.rows.grid_columnconfigure(1, minsize=84)
        self.rows.grid_columnconfigure(2, minsize=58)
        self.row_widgets: dict[str, dict] = {}
        for code in self.cfg["symbols"]:
            self._add_row(code)

    def _add_row(self, code: str) -> None:
        bg = self.bg
        fs = int(self.cfg["font_size"])
        index = len(self.row_widgets)

        tag = tk.Label(
            self.rows, text=short_code(code), font=(WIDGET_FONT, fs + 1),
            fg=C_GRAY, bg=bg, anchor="w",
        )
        tag.grid(row=index, column=0, sticky="w", padx=(0, 8), pady=1)

        price = tk.Label(
            self.rows, text="--", font=(WIDGET_FONT, fs + 1),
            fg=C_GRAY, bg=bg, anchor="e",
        )
        price.grid(row=index, column=1, sticky="e", padx=(0, 8), pady=1)

        pct = tk.Label(
            self.rows, text="", font=(WIDGET_FONT, fs + 1),
            fg=C_GRAY, bg=bg, anchor="e",
        )
        pct.grid(row=index, column=2, sticky="e", pady=1)

        self.row_widgets[code] = {"name": tag, "price": price, "pct": pct}

    def _build_menu(self) -> None:
        self.menu = tk.Menu(
            self.hit, tearoff=0, font=(FONT, 9),
            bg="#1d2026", fg=C_TEXT, activebackground="#2d333d",
            activeforeground="#ffffff", bd=0, relief="flat",
        )
        self.menu.add_command(label="刷新        F5", command=self.refresh)
        self.menu.add_command(label="设置…", command=self.open_settings)
        self.menu.add_separator()
        self.menu.add_checkbutton(
            label="窗口置顶", variable=self.var_top, command=self._toggle_top,
            selectcolor="#1d2026",
        )
        self.menu.add_checkbutton(
            label="锁定位置", variable=self.var_lock, command=self._toggle_lock,
            selectcolor="#1d2026",
        )

        self.menu.add_separator()
        self.menu.add_checkbutton(
            label="开机自启", variable=self.var_startup, command=self._toggle_startup,
            selectcolor="#1d2026",
        )
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.quit)

    def _make_root_click_through(self) -> None:
        """文字窗口整体点击穿透；只有第一列的命中层接收鼠标。"""
        try:
            user32 = ctypes.windll.user32
            gwl_exstyle = -20
            ws_ex_transparent = 0x20
            handles = {self.root.winfo_id()}
            try:
                frame = self.root.wm_frame()
                if frame:
                    handles.add(int(str(frame), 0))
            except Exception:
                pass
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.restype = ctypes.c_long
            for handle in handles:
                style = user32.GetWindowLongW(handle, gwl_exstyle)
                user32.SetWindowLongW(handle, gwl_exstyle, style | ws_ex_transparent)
        except Exception:
            pass

    def _fit_window(self, x: int | None = None, y: int | None = None) -> None:
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth() + 14
        h = self.root.winfo_reqheight()
        if x is None:
            x = self.cfg.get("x")
        if y is None:
            y = self.cfg.get("y")

        left, top, right, bottom = monitor_work_area(0, 0)
        if x is None or y is None:
            x, y = right - w - 44, top + 44

        left, top, right, bottom = monitor_work_area(x, y)
        x = int(max(left, min(x, right - w - 4)))
        y = int(max(top, min(y, bottom - h - 4)))
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.update_idletasks()
        hit_w = 52
        hit_x = x + 8
        try:
            last_row = max(0, len(self.cfg["symbols"]) - 1)
            bbox = self.rows.grid_bbox(0, 0, 0, last_row)
            if bbox and bbox[2] >= 10:
                hit_w = int(bbox[2])
                hit_x = x + 8 + int(bbox[0])
        except Exception:
            pass
        self.hit.geometry(f"{hit_w}x{h}+{hit_x}+{y}")
        self.hit.deiconify()
        self.hit.lift()

    def _apply_window_attrs(self) -> None:
        try:
            topmost = bool(self.cfg["topmost"])
            self.root.attributes("-topmost", topmost)
            self.root.attributes("-alpha", 1.0)
            self.hit.attributes("-topmost", topmost)
            self.hit.attributes("-alpha", 0.004)
        except tk.TclError:
            pass

    # -- 交互 ---------------------------------------------------------------
    def _press(self, event) -> None:
        if self.cfg["locked"]:
            return
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _motion(self, event) -> None:
        if not self._drag or self.cfg["locked"]:
            return
        x = event.x_root - self._drag[0]
        y = event.y_root - self._drag[1]
        self.root.geometry(f"+{x}+{y}")
        self.hit.geometry(f"+{x}+{y}")

    def _release(self, event) -> None:
        if self._drag:
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
            save_config(self.cfg)
        self._drag = None

    def _show_menu(self, event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.result:
            self.apply_settings(dialog.result)

    def apply_settings(self, data: dict) -> None:
        self.cfg.update(data)
        save_config(self.cfg)
        self.var_top.set(bool(self.cfg["topmost"]))
        self._rebuild()
        self.fetcher.update(self.cfg["symbols"], self.cfg["interval"])
        self.refresh()

    def _rebuild(self) -> None:
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.outer.destroy()
        self._build()
        self._apply_window_attrs()
        self._fit_window(x, y)

    def _toggle_top(self) -> None:
        self.cfg["topmost"] = bool(self.var_top.get())
        self._apply_window_attrs()
        save_config(self.cfg)

    def _toggle_lock(self) -> None:
        self.cfg["locked"] = bool(self.var_lock.get())
        save_config(self.cfg)

    def _toggle_startup(self) -> None:
        want = bool(self.var_startup.get())
        try:
            if want:
                STARTUP_FILE.parent.mkdir(parents=True, exist_ok=True)
                if getattr(sys, "frozen", False):
                    command = f'"{sys.executable}"'
                else:
                    python = Path(sys.executable)
                    if python.name.lower() == "python.exe":
                        pythonw = python.with_name("pythonw.exe")
                        if pythonw.exists():
                            python = pythonw
                    command = f'"{python}" "{Path(__file__).resolve()}"'
                STARTUP_FILE.write_text(
                    f'@echo off\r\nstart "" {command}\r\n',
                    encoding="mbcs",
                )
            else:
                STARTUP_FILE.unlink(missing_ok=True)
            save_config(self.cfg)
        except Exception as exc:
            self.var_startup.set(not want)
            messagebox.showerror(APP_NAME, f"设置开机自启失败：{exc}")

    # -- 数据 ---------------------------------------------------------------
    def _poll(self) -> None:
        if not self._alive:
            return
        try:
            while True:
                quotes, error, ts = self.queue.get_nowait()
                self._apply(quotes, error, ts)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _apply(self, quotes: dict, error: str | None, ts: float) -> None:
        if error:
            display = self._last_quotes
        else:
            self._last_quotes = quotes
            display = quotes
            for code, quote in quotes.items():
                if quote.name and not code.startswith("hf_"):
                    self.cfg["names"][code] = quote.name

        for code, widgets in self.row_widgets.items():
            quote = display.get(code)
            tag_text = short_code(code)
            if quote and quote.ok:
                widgets["name"].config(text=tag_text, fg=C_GRAY)
                widgets["price"].config(text=fmt_price(quote), fg=C_GRAY)
                widgets["pct"].config(text=fmt_pct(quote), fg=C_GRAY)
            else:
                widgets["name"].config(text=tag_text, fg=C_GRAY)
                widgets["price"].config(text="—" if quote else "--", fg=C_GRAY)
                widgets["pct"].config(text="", fg=C_GRAY)

    def refresh(self) -> None:
        self.fetcher.request()

    def quit(self) -> None:
        self._alive = False
        try:
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
        except Exception:
            pass
        save_config(self.cfg)
        self.fetcher.stop()
        try:
            self.hit.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

_MUTEX = None


def _acquire_single_instance() -> bool:
    global _MUTEX
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _MUTEX = kernel32.CreateMutexW(None, False, "Local\\GoldInfoMonitor.SingleInstance")
    return kernel32.GetLastError() != 183


def _enable_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _setup_logging() -> None:
    try:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=200_000, backupCount=1, encoding="utf-8")
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[handler],
        )
    except Exception:
        pass


def main() -> None:
    _enable_dpi()
    _setup_logging()
    if not _acquire_single_instance():
        ctypes.windll.user32.MessageBoxW(
            0,
            "行情监控已经在运行。\n\n"
            "如果桌面上看不到窗口，可能是之前的窗口被拖到了屏幕外。"
            "请先在任务管理器中结束 pythonw.exe，删除程序目录下的 config.json，"
            "再重新启动即可恢复默认位置。",
            APP_NAME,
            0x40,
        )
        return
    try:
        app = MarketApp()
        if "--settings" in sys.argv:
            app.root.after(400, app.open_settings)
        app.run()
    except Exception as exc:
        logging.exception("程序异常退出")
        ctypes.windll.user32.MessageBoxW(
            0,
            f"行情监控启动失败：\n{exc}\n\n日志文件：{LOG_FILE}",
            APP_NAME,
            0x10,
        )


if __name__ == "__main__":
    main()
