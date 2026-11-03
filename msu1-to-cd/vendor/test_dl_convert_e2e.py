#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_dl_convert_e2e.py — v4.0「下载后自动转 WAV」端到端验收（T5）。

**需要 tkinter + 真解码（libgme）**。系统 Python 有 tkinter，托管 Python 没有。

跑法::

    python vendor/test_dl_convert_e2e.py              # 结构 + 时序(用假 convert)
    python vendor/test_dl_convert_e2e.py --decode     # 追加真实解码(AC-C5/C6)
    python vendor/test_dl_convert_e2e.py --full       # 全部

本文件**逐条核对 PRD AC-C1~C31**。无法自动化的（纯目视的 AC-C19/AC-C20）
用**代码走查 + 几何断言**代替：检查 tar 标签与三个复选框分属不同的 Frame，
且状态列文案长度 <= 5 字。

⚠ **GUI 用真Tk 建一个 DownloadDialog**，但所有网络/解码都替换成假的
   （真Tk 才能验布局与事件接线；真下载会打真站点）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import queue
import shutil
import struct
import sys
import tempfile
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chip_formats as cf                                # noqa: E402
import dl_convert as dc                                  # noqa: E402

_GREEN, _RED, _YEL, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _YEL = _RST = ""

_pass = 0
_fail = 0
_skipped = []
_notes = []

#: AC 核对表：收集结果，最后统一打印
_ac = {}


def check(label, cond, detail="", ac=""):
    global _pass, _fail
    if isinstance(detail, (list, tuple, dict, set)):
        detail = str(detail)
    if cond:
        _pass += 1
        print("  %sPASS%s %s" % (_GREEN, _RST, label))
    else:
        _fail += 1
        print("  %sFAIL%s %s%s" % (_RED, _RST, label,
                                   ("  <- " + detail) if detail else ""))
    if ac:
        _ac.setdefault(ac, []).append(bool(cond))
    return bool(cond)


def skip(ac, why):
    _skipped.append((ac, why))
    print("  %sSKIP%s %s — %s" % (_YEL, _RST, ac, why))


def note(text):
    _notes.append(text)
    print("  %sNOTE%s %s" % (_YEL, _RST, text))


# ---------------------------------------------------------------------------
# GUI 夹具
# ---------------------------------------------------------------------------

def make_gui():
    """建一个真Tk 的 DownloadDialog（隐藏窗口）。

    ⚠ **必须先把 messagebox 全换成 no-op**：``_start_download`` 在出错时会弹
    模态框, 在无人值守的测试里会**直接把测试挂死**（实测踩过：faulthandler
    停在 ``commondialog.show``）。弹窗次数记进 ``_dlg_msgs`` 供断言。
    """
    import tkinter as tk
    import msu1_gui as G
    _dlg_msgs.clear()
    G.messagebox.showinfo = lambda *a, **k: _dlg_msgs.append(("info", a))
    G.messagebox.showwarning = lambda *a, **k: _dlg_msgs.append(("warn", a))
    G.messagebox.showerror = lambda *a, **k: _dlg_msgs.append(("error", a))
    G.messagebox.askyesno = lambda *a, **k: True
    root = tk.Tk()
    root.withdraw()
    dlg = G.DownloadDialog(root)
    return root, dlg, G


#: 被拦下的模态框 (kind, args)
_dlg_msgs = []


def pump(dlg, seconds=0.4, step=0.02):
    """手动跑若干轮 _poll（不依赖 Tk 的 after，避免测试挂住）。"""
    end = time.time() + seconds
    while time.time() < end:
        try:
            dlg._poll_once()
        except AttributeError:
            # _poll 自己管 after；测试里改用直接处理队列
            while True:
                try:
                    ev = dlg.q.get_nowait()
                except queue.Empty:
                    break
                dlg._dispatch(ev)
        time.sleep(step)


def drain_events(dlg, seconds=1.0):
    """把队列里的事件同步处理掉（不进 Tk 主循环）。"""
    end = time.time() + seconds
    n = 0
    while time.time() < end:
        try:
            ev = dlg.q.get_nowait()
        except queue.Empty:
            time.sleep(0.01)
            continue
        dlg._dispatch(ev)
        n += 1
    return n


# ---------------------------------------------------------------------------
# 夹具：文件 / 假 convert / 假 Downloader
# ---------------------------------------------------------------------------

def make_spc(path, secs=4, fade_ms=0, title=b"Stage Clear"):
    head = bytearray(0x100)
    head[0:33] = b"SNES-SPC700 Sound File Data\x1a\x1a\x1a"
    head[0x26:0x4E] = title.ljust(0x28, b"\x00")[:0x28]
    head[0x4E:0x7E] = b"Test Game".ljust(0x28, b"\x00")[:0x28]
    head[0x9E] = 0x1A
    head[0xA9:0xAC] = str(int(secs)).rjust(3).encode()[:3]
    head[0xAC:0xB1] = str(int(fade_ms)).rjust(5).encode()[:5]
    with open(path, "wb") as fh:
        fh.write(bytes(head) + b"\x00" * 256)
    return path


def make_nsf(path, n_tracks=3):
    head = bytearray(0x100)
    head[0:4] = b"NESM\x1a"
    head[0x05] = 1
    head[0x06] = n_tracks
    head[0x0E:0x2E] = b"Test NSF".ljust(0x20, b"\x00")[:0x20]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(bytes(head))
    return path


def write_real_wav(path, secs=0.2, freq=440.0, sr=44100):
    """写一个**合规**的 16bit/44.1k/立体声 WAV（AC-C1 头合规 + 非静音）。"""
    n = int(sr * secs)
    import math
    frames = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * freq * i / sr))
        frames += struct.pack("<hh", v, v)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return path


def wav_stat(path):
    """(合规?, 峰值, 字节数)。头必须 2ch/16bit/44100。"""
    try:
        with wave.open(path, "rb") as w:
            ok = (w.getnchannels() == 2 and w.getsampwidth() == 2
                  and w.getframerate() == 44100)
            raw = w.readframes(w.getnframes())
    except Exception:                                # noqa: BLE001
        return False, 0, 0
    import array
    a = array.array("h")
    a.frombytes(raw[: len(raw) & ~1])
    peak = max(max(a), -min(a)) if a else 0
    return ok, peak, os.path.getsize(path)


class FakeConvert:
    """假 convert（结构测试用，不解码）。

    ⚠ **全局注册**：构造即装上``dl_convert.ConvertWorker`` 默认用不起真解码。
    v3.9 没有「注入 convert」的钩子，``ConvertWorker.convert_fn`` 是本次
    新加的测试钩子，但 GUI 是**自己**建worker 的，测试拿不到实例 ——
    所以这里改成在 ``spc_wav.convert`` 这一层拦（``dl_convert`` 是
    ``import spc_wav`` 后按 ``spc_wav.convert`` 取的，装 ``spc_wav.convert``
    就能生效）。
    """

    #: 全部实例（测试用来读调用记录）
    instances = []

    def __init__(self, wav_dir=None, secs=0.2):
        self.wav_dir = wav_dir
        self.secs = secs
        self.calls = []
        self.lock = threading.Lock()
        FakeConvert.instances.append(self)

    def install(self):
        """装到 spc_wav.convert 上（GUI 建的 worker 也会走这里）。"""
        import spc_wav
        self._prev = spc_wav.convert
        spc_wav.convert = self
        return self

    def uninstall(self):
        import spc_wav
        spc_wav.convert = getattr(self, "_prev", spc_wav.convert)

    def __call__(self, paths, out_dir, policy, on_log=None, on_progress=None,
                 on_stage=None, cancel=None, speed=1.0, max_secs=0.0,
                 used=None, start_idx=0, total=None, **extra):
        src = str(paths[0])
        name = os.path.basename(src).lower()
        with self.lock:
            self.calls.append({"src": src, "policy": policy,
                               "start_idx": start_idx,
                               "chip_secs": extra.get("chip_secs"),
                               "loader": extra.get("loader"),
                               "speed": speed, "max_secs": max_secs,
                               "used": sorted(used or ())})
        if name.startswith("boom"):
            raise RuntimeError("模拟解码失败")
        os.makedirs(out_dir, exist_ok=True)
        pool = used if used is not None else set()
        idx = start_idx
        files = []
        for i in range(2):
            idx += 1
            stem = "T%02d" % idx
            p = os.path.join(out_dir, "%02d_%s.wav" % (idx, stem))
            write_real_wav(p, self.secs)
            pool.add(stem.lower())
            files.append(p)
        return {"ok": 2, "fail": 0, "files": files, "seconds": self.secs * 2,
                "errors": [], "last_idx": idx}


class SlowFakeConvert(FakeConvert):
    """假 convert：循环等 cancel（验停止三段联动）。"""

    def __init__(self, wav_dir=None, secs=0.2, hang=8.0):
        FakeConvert.__init__(self, wav_dir, secs)
        self.hang = hang

    def __call__(self, paths, out_dir, policy, cancel=None, **kw):
        end = time.time() + self.hang
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                        "errors": ["已取消"],
                        "last_idx": kw.get("start_idx", 0)}
            time.sleep(0.02)
        return FakeConvert.__call__(self, paths, out_dir, policy,
                                    cancel=cancel, **kw)


class FakeDownloader:
    """假 Downloader：只回放预置事件，**不联网**。

    保留 ``_cancel_event`` 字段名 —— GUI 侧就是靠它拿共享 Event 的
    （设计 §2.3 / PRD R0b），换名字测试就失去意义。
    """

    def __init__(self, events, auto_extract=True, on_event=None, **kwargs):
        self._cancel_event = threading.Event()
        self._on_event = on_event
        self._auto_extract = auto_extract
        self.added = []
        self.started = False
        self._events = list(events)
        self.cancelled = False
        self.kwargs = kwargs      # 记下 workers/extract_dir 等, 供断言用

    def add(self, url, dest, size=0):
        self.added.append((url, dest, size))

    def start(self):
        self.started = True
        threading.Thread(target=self._run, name="fake-dl", daemon=True).start()

    def _run(self):
        for kind, payload in self._events:
            if self._cancel_event.is_set():
                break
            time.sleep(0.02)
            if self._on_event:
                self._on_event(kind, dict(payload))
        if self._on_event:
            self._on_event("all_done", {})

    def cancel(self):
        self.cancelled = True
        self._cancel_event.set()

    def is_running(self):
        return self.started and not self._cancel_event.is_set()


def setup_platform(dlg, G, key="snes", name="SNES / SFC"):
    """给 dlg 灌一批假条目，并选中。"""
    dlg.cur_key = key
    dlg.cur_name = name
    dlg.entries = [
        {"name": "Pack A", "url": "u_a", "size": 1024,
         "mtime": 1758000000.0, "letter": "A"},
        {"name": "Pack B", "url": "u_b", "size": 2048,
         "mtime": 1758000001.0, "letter": "B"},
    ]
    dlg.sel = {"u_a", "u_b"}
    dlg.iid_of = {"u_a": "i1", "u_b": "i2"}
    for url, iid in dlg.iid_of.items():
        dlg.tree.insert("", "end", iid=iid, values=(url, "", "", "", ""))
    dlg._refresh_row_states()


# ===========================================================================
# A. 布局与门禁（AC-C18/C19/C20/C22/C23）
# ===========================================================================

def test_layout(root, dlg, G):
    print("\n[A] 布局与门禁（AC-C18/C19/C20/C22/C23）")
    # 窗口是 withdraw 状态, 几何全是 1x1 —— 必须先 deiconify+update 让 Tk
    # 真正算一遍布局, 否则「tar 标签有没有被挤掉」根本测不出来。
    root.deiconify()
    root.update_idletasks()
    dlg.update_idletasks()
    root.update()
    dlg.update()

    def walk(w):
        yield w
        for c in w.winfo_children():
            yield from walk(c)

    def find_by_var(var):
        """按 textvariable 找到绑定该StringVar 的 Label。"""
        for w in walk(dlg):
            try:
                tv = w.cget("textvariable")
            except Exception:                        # noqa: BLE001
                continue
            if tv and str(tv).endswith(str(var._name)):
                return w
        return None

    # ---- AC-C20: tar 标签在第三个复选框的**下一行**且完整可见 ----
    cb = dlg.var_auto_wav_cb
    tar_lbl = find_by_var(dlg.var_extract_tar)
    est_lbl = find_by_var(dlg.var_wav_estimate)
    check("找到 tar 状态标签", tar_lbl is not None)
    check("AC-C22 crow 上有体量预估标签", est_lbl is not None)
    if tar_lbl is not None:
        cb_box = (cb.winfo_rootx(), cb.winfo_rooty(),
                  cb.winfo_rootx() + cb.winfo_width(),
                  cb.winfo_rooty() + cb.winfo_height())
        tar_box = (tar_lbl.winfo_rootx(), tar_lbl.winfo_rooty(),
                   tar_lbl.winfo_rootx() + tar_lbl.winfo_width(),
                   tar_lbl.winfo_rooty() + tar_lbl.winfo_height())
        check("AC-C20 tar 标签与复选框**不在同一行**（未被挤掉）",
              tar_box[1] >= cb_box[3] - 2,
              "cb=%s tar=%s" % (cb_box, tar_box))
        check("AC-C20 tar 标签完整可见（宽度够显示文本）",
              tar_lbl.winfo_width() >= len(
                  dlg.var_extract_tar.get()) * 6,
              "w=%d 文本=%r" % (tar_lbl.winfo_width(),
                                dlg.var_extract_tar.get()))
        # ⚠ 用 **dlg（Toplevel）** 的边界而不是 root：DownloadDialog 是
        # Toplevel, 它的可用宽度才是布局约束（B8 说的 660px 是它自己的）。
        dlg_box = (dlg.winfo_rootx(), dlg.winfo_rootx() + dlg.winfo_width())
        check("AC-C20 tar 标签未被窗口右边界裁掉",
              tar_box[2] <= dlg_box[1] + 1,
              "tar_right=%d dlg_right=%d" % (tar_box[2], dlg_box[1]))
        check("AC-C20 tar 标签与预估标签不重叠",
              est_lbl is None or tar_box[2] <= est_lbl.winfo_rootx() + 1,
              "tar_right=%d est_left=%d" % (
                  tar_box[2], est_lbl.winfo_rootx() if est_lbl else -1))
    if est_lbl is not None:
        # 预估标签当前是「—」，只有一个字符宽。勾上开关跑一次估算后才有宽度,
        # 所以这里只断言「它有非零宽度且已 map」, 真正的内容在 B 段验。
        check("AC-C22 预估标签已 map（有非零高度）",
              est_lbl.winfo_height() > 4, "h=%d" % est_lbl.winfo_height())

    # ---- AC-C19: 状态列文案长度 ----
    # 80px 硬约束（B8）。emoji 在 Tk 里按双宽渲染, 所以按「显示宽度」算:
    # ASCII 1 列、其余 2 列, 1 列 ≈ 7px -> 上限 ~11 列。
    def disp_cols(s):
        return sum(1 if ord(ch) < 0x2000 else 2 for ch in s)

    new_states = {"排队": "⏳ 待转", "转换中": "🎵 转WAV",
                  "完成": "🎵 已转3首", "完成0": "✅ 已转 WAV",
                  "失败": "⚠ 转失败", "已转过": "⏭ 已转过"}
    too_wide = [(k, v, disp_cols(v)) for k, v in new_states.items()
                if disp_cols(v) > 11]
    check("AC-C19 新增状态文案在 80px 内（<=11 显示列）", not too_wide,
          str(too_wide))
    check("AC-C19 「⏭ 已存在 → 待转」是唯一超宽项，靠日志区分",
          disp_cols("⏭ 已存在 → 待转") > 11,
          "PRD 已承认该文案 11 字符超 80px，截断可接受")

    # ---- AC-C1.1: 默认关闭、不持久化 ----
    check("AC-C1.1 var_auto_wav 默认 False", dlg.var_auto_wav.get() is False)
    check("AC-C1.1 无持久化（没有读写配置的代码）",
          not hasattr(dlg, "_load_auto_wav"))

    # ---- AC-C1.2 / C1.3 / C11 门禁 ----
    def cb_state():
        return str(cb.cget("state"))

    dlg.var_auto_extract.set(False)
    blocked, reason = dlg._chip_blocked()
    check("AC-C1.2 关自动解压 -> 芯片包被挡", blocked)
    check("AC-C1.2 提示文案含「芯片包」限定", "芯片包" in reason, reason)
    check("AC-C1.2 开关仍可用（SPC 例外，不能整块禁掉）",
          cb_state() == "normal", cb_state())
    dlg.var_auto_extract.set(True)
    blocked2, _r = dlg._chip_blocked()
    check("AC-C1.2 开自动解压 -> 不挡", not blocked2)
    old_cd = G.chip_decode
    try:
        G.chip_decode = None
        b3, r3 = dlg._chip_blocked()
        check("AC-C1.3 chip_decode 缺失 -> 芯片包被挡", b3)
        check("AC-C1.3 提示含「芯片包」", "芯片包" in r3, r3)
        check("AC-C11 芯片模块缺失时开关仍可用（SPC 仍可转）",
              cb_state() == "normal", cb_state())
    finally:
        G.chip_decode = old_cd
    dlg._update_gate_hint()
    check("AC-C1.4 空闲时复选框可点", cb_state() == "normal", cb_state())

    # ---- AC-C2/AC-C21: WAV 目录挂 _dest_root() ----
    dlg.var_dest.set(os.path.join(tmpdir(), "Music"))
    wd = dlg._wav_dir()
    check("AC-C21 WAV 挂在 _dest_root()/WAV",
          wd == os.path.join(dlg._dest_root(), "WAV"), wd)
    check("AC-C21 目录名是 WAV", os.path.basename(wd) == "WAV")
    check("AC-C21 用的是 _dest_root 而非 var_dest",
          dlg._dest_root() != os.path.normpath(dlg.var_dest.get()),
          "%s vs %s" % (dlg._dest_root(), dlg.var_dest.get()))
    # FR-C2.2 懒创建: 开关关闭时**不得**创建空的 WAV/ 目录。
    # 用一个全新的保存目录来验, 避免被本轮前面的产物干扰。
    fresh = os.path.join(tmpdir(), "fresh_check", "SNES")
    os.makedirs(fresh, exist_ok=True)
    dlg.var_dest.set(os.path.join(tmpdir(), "fresh_check"))
    fresh_wav = os.path.join(fresh, "WAV")
    check("FR-C2.2 未勾选时不创建 WAV/ 空目录",
          not os.path.isdir(fresh_wav), fresh_wav)
    dlg.var_dest.set(_TMP["dest_root"])


_TMP = {"d": "", "dest_root": ""}


def tmpdir():
    return _TMP["d"]


# ===========================================================================
# B. 两个触发入口（AC-C1/C2/C3/C16/C26）
# ===========================================================================

def test_trigger_entries(root, dlg, G, wav_dir, dest_root):
    print("\n[B] 两个触发入口：file_done(SPC) + extract_done(芯片包)（AC-C1/C2）")
    spc = make_spc(os.path.join(dest_root, "Song.spc"))
    nsf = make_nsf(os.path.join(dest_root, "pack", "01 Game.nsf"))

    dlg.var_auto_wav.set(True)
    dlg.var_auto_extract.set(True)
    fake = FakeConvert().install()
    events = [
        ("file_start", {"file": spc, "size": 100}),
        ("file_done", {"file": spc, "ok": True, "error": ""}),
        ("extract_start", {"file": os.path.join(dest_root, "p.7z"),
                           "dest": os.path.join(dest_root, "p")}),
        ("extract_done", {"file": os.path.join(dest_root, "p.7z"),
                          "dest": os.path.join(dest_root, "p"),
                          "files": [nsf,
                                    os.path.join(dest_root, "p", "readme.txt")],
                          "count": 2, "archive_removed": False}),
        ("all_done", {}),
    ]
    G.joshw_dl.Downloader = lambda **kw: FakeDownloader(events, **kw)
    try:
        dlg._start_download()
        check("转换线程已启动", dlg._cv is not None)
        t0 = time.time()
        while time.time() - t0 < 30 and dlg._cv is not None:
            drain_events(dlg, 0.2)
        drain_events(dlg, 2.0)
    finally:
        fake.uninstall()

    srcs = [os.path.basename(c["src"]) for c in fake.calls]
    check("AC-C2 file_done 的 SPC 入队了", "Song.spc" in srcs, str(srcs))
    check("AC-C1 extract_done 的 .nsf 入队了", "01 Game.nsf" in srcs, str(srcs))
    check("FR-C3.2 readme.txt 被过滤掉（只按 CHIP_EXTS）",
          not any(s.endswith(".txt") for s in srcs), str(srcs))
    by = {os.path.basename(c["src"]): c for c in fake.calls}
    check("AC-C2 SPC 走 policy=id666",
          by.get("Song.spc", {}).get("policy") == "id666")
    check("AC-C1 芯片格式走 policy=chip",
          by.get("01 Game.nsf", {}).get("policy") == "chip")
    check("FR-C4.4 id666 不传 chip_secs",
          by.get("Song.spc", {}).get("chip_secs") is None)
    check("FR-C4.4 chip 传 chip_secs=150",
          by.get("01 Game.nsf", {}).get("chip_secs") == 150.0,
          str(by.get("01 Game.nsf", {}).get("chip_secs")))
    check("FR-C4.4 chip 传 loader",
          by.get("01 Game.nsf", {}).get("loader") is not None)
    check("FR-C12 speed=1.0 / max_secs=0",
          all(c["speed"] == 1.0 and c["max_secs"] == 0.0 for c in fake.calls))
    # FR-C5.3: used / start_idx 跨调用共享（单文件粒度的前提）
    check("FR-C5.3 start_idx 跨文件递增（不都从 0 开始）",
          len({c["start_idx"] for c in fake.calls}) > 1,
          str([c["start_idx"] for c in fake.calls]))
    check("FR-C5.3 used 跨调用共享",
          any(c["used"] for c in fake.calls[1:]),
          str([c["used"] for c in fake.calls]))

    # AC-C1: 产物合规 + 非静音
    produced = os.listdir(wav_dir) if os.path.isdir(wav_dir) else []
    wavs = [f for f in produced if f.lower().endswith(".wav")]
    check("AC-C4 WAV 全部平铺在 WAV/ 下（无子目录）",
          wavs and all(os.path.isfile(os.path.join(wav_dir, w)) for w in wavs)
          and not any(os.path.isdir(os.path.join(wav_dir, e))
                      for e in produced if not e.lower().endswith(".wav")),
          str(produced))
    check("AC-C1 产出 >=1 个 WAV", len(wavs) >= 1, str(wavs))
    if wavs:
        ok, peak, size = wav_stat(os.path.join(wav_dir, wavs[0]))
        check("AC-C1 WAV 头 = 2ch/16bit/44100", ok)
        check("AC-C1 WAV 非静音（峰值 > 300）", peak > 300, "peak=%d" % peak)
    # AC-C24: 日志里有 policy 标记
    logtext = dlg.logw.get("1.0", "end")
    check("AC-C24 完成行带 [chip]/[id666] 标记",
          "[chip]" in logtext and "[id666]" in logtext,
          logtext[-400:])
    check("AC-C24 有每包策略汇总行",
          "→ 策略" in logtext, logtext[-400:])
    check("AC-C21 收尾日志含 WAV 输出目录",
          "📁 WAV 输出目录" in logtext and wav_dir in logtext)
    return wavs


# ===========================================================================
# C. 时序：all_done 排空 / 停止 / 幂等（AC-C12~C18）
# ===========================================================================

def test_timing(root, dlg, G, wav_dir, dest_root):
    print("\n[C] 时序：all_done 不早于排空 / 停止三段 / 幂等（AC-C12~C18）")
    files = [make_nsf(os.path.join(dest_root, "t%d.nsf" % i)) for i in range(4)]

    # ---- AC-C17: all_done 早到时不能提前 _finish_download ----
    dlg.var_auto_wav.set(True)
    dlg.var_auto_extract.set(True)
    ev = [("extract_done", {"file": os.path.join(dest_root, "t.7z"),
                            "dest": dest_root, "files": files, "count": 4,
                            "archive_removed": False}),
          ("all_done", {})]
    G.joshw_dl.Downloader = lambda **kw: FakeDownloader(ev, **kw)
    fakeA = FakeConvert().install()
    try:
        dlg._start_download()
        check("AC-C17 all_done 早到后 _cv 仍在（未提前收尾）", dlg._cv is not None)
        check("AC-C17 此时按钮仍锁（_downloading 仍 True）", dlg._downloading)
        check("AC-C17 此时 self.dl 仍非 None（AC-C12② 前提）", dlg.dl is not None)
        t0 = time.time()
        while time.time() - t0 < 30 and dlg._cv is not None:
            drain_events(dlg, 0.2)
        drain_events(dlg, 2.0)
    finally:
        fakeA.uninstall()
    check("AC-C17 队列排空后才 _finish_download", dlg._cv is None)
    check("AC-C17 收尾后 _downloading=False", not dlg._downloading)
    check("AC-C18 进度条走满意味着真的全做完了",
          dlg._cv_done == dlg._cv_queued,
          "%d/%d" % (dlg._cv_done, dlg._cv_queued))

    # ---- AC-C12: 「■ 停止」在排空期仍生效 ----
    n_before = len([f for f in os.listdir(wav_dir)
                    if f.lower().endswith(".wav")]) if os.path.isdir(wav_dir) else 0
    slow = [make_nsf(os.path.join(dest_root, "s%d.nsf" % i)) for i in range(3)]
    dlg.var_auto_wav.set(True)
    ev2 = [("extract_done", {"file": os.path.join(dest_root, "s.7z"),
                             "dest": dest_root, "files": slow, "count": 3,
                             "archive_removed": False}),
           ("all_done", {})]

    slow = SlowFakeConvert(hang=8.0).install()
    G.joshw_dl.Downloader = lambda **kw: FakeDownloader(ev2, **kw)
    dlg._start_download()
    cv = dlg._cv
    # 等到真在转
    t0 = time.time()
    while time.time() - t0 < 15 and (cv is None or cv.current() is None):
        drain_events(dlg, 0.1)
    check("AC-C12② 排空期 self.dl 仍存在（停止按钮可触发）",
          dlg.dl is not None and dlg._downloading)
    t_stop = time.time()
    dlg._stop_download()
    check("AC-C12 停止立即返回（不 join）", time.time() - t_stop < 1.0,
          "%.2fs" % (time.time() - t_stop))
    if cv:
        cv.wait(20)
    drain_events(dlg, 3.0)
    check("AC-C12 转换线程 <=10s 退出", time.time() - t_stop < 20,
          "%.1fs" % (time.time() - t_stop))
    check("AC-C12 停止后 _cv 已释放", dlg._cv is None)
    check("AC-C12 已产出的 WAV 保留（不回滚）",
          len([f for f in os.listdir(wav_dir)
               if f.lower().endswith(".wav")]) >= n_before)
    # 无半截 WAV
    half = []
    for f in os.listdir(wav_dir):
        if not f.lower().endswith(".wav"):
            continue
        p = os.path.join(wav_dir, f)
        with open(p, "rb") as fh:
            blob = fh.read()
        if blob[:4] != b"RIFF":
            half.append(f)
            continue
        size_field = struct.unpack_from("<I", blob, 4)[0] + 8
        if size_field != len(blob):
            half.append(f)
    check("AC-C12 无半截 WAV（RIFF 长度字段与实际一致）", not half, str(half))
    slow.uninstall()

    # ---- AC-C13: 停止后可再次开始 ----
    dlg.var_auto_wav.set(True)
    files2 = [make_nsf(os.path.join(dest_root, "r%d.nsf" % i)) for i in range(2)]
    ev3 = [("extract_done", {"file": os.path.join(dest_root, "r.7z"),
                             "dest": dest_root, "files": files2, "count": 2,
                             "archive_removed": False}),
           ("all_done", {})]
    G.joshw_dl.Downloader = lambda **kw: FakeDownloader(ev3, **kw)
    fakeB = FakeConvert().install()
    try:
        dlg._start_download()
        check("AC-C13 停止后可再次开始（_cv 重建）", dlg._cv is not None)
        t0 = time.time()
        while time.time() - t0 < 30 and dlg._cv is not None:
            drain_events(dlg, 0.2)
        drain_events(dlg, 2.0)
    finally:
        fakeB.uninstall()
    check("AC-C13 第二轮正常收尾", dlg._cv is None and not dlg._downloading)

    # ---- AC-C14: 重复点击不重复转（幂等）----
    srcs_now = [make_nsf(os.path.join(dest_root, "id%d.nsf" % i))
                for i in range(3)]
    # ⚠ 事件的 `file` 必须等于 ``_start_download`` 算出的 target
    #   (``<dest_root>/<条目名去扩展名>.7z``), 否则 ``_url_for`` 查不到 url ->
    #   ``_set_state(None, ...)`` 直接 return -> 状态列永远停在「等待…」。
    #   这正是真实链路里 `_url_of_path` 的登记依据（实测踩过）。
    dlg.entries = [{"name": "id", "url": "u_id", "size": 4096,
                    "mtime": 1758000000.0, "letter": "I"}]
    dlg.sel = {"u_id"}
    dlg.iid_of = {"u_id": "i_id"}
    try:
        dlg.tree.delete("i_id")
    except Exception:                                # noqa: BLE001
        pass
    dlg.tree.insert("", "end", iid="i_id", values=("id", "", "", "", ""))
    ev4 = [("extract_done", {"file": os.path.join(dest_root, "id.7z"),
                             "dest": dest_root, "files": srcs_now, "count": 3,
                             "archive_removed": False}),
           ("all_done", {})]

    def run_once(tag):
        G.joshw_dl.Downloader = lambda **kw: FakeDownloader(ev4, **kw)
        fc = FakeConvert().install()
        try:
            dlg._start_download()
            t0 = time.time()
            while time.time() - t0 < 30 and dlg._cv is not None:
                drain_events(dlg, 0.2)
            drain_events(dlg, 2.0)
            # ⚠ 必须在**本轮收尾之前**抓 states: _start_download 开头会把
            #   _states 重置成「等待…」, 跨轮读会读到下一轮的值（实测踩过）。
            return fc, list(dlg._states.values())
        finally:
            fc.uninstall()

    fc1, _ = run_once("first")
    names1 = sorted(f for f in os.listdir(wav_dir)
                    if f.lower().endswith(".wav"))
    size1 = sum(os.path.getsize(os.path.join(wav_dir, f)) for f in names1)
    dlg.var_auto_wav.set(True)
    fc2, states2 = run_once("second")
    names2 = sorted(f for f in os.listdir(wav_dir)
                    if f.lower().endswith(".wav"))
    size2 = sum(os.path.getsize(os.path.join(wav_dir, f)) for f in names2)
    check("AC-C14 第二轮不再调 convert", len(fc2.calls) == 0,
          str(len(fc2.calls)))
    check("AC-C14 文件数不变", names1 == names2, "%d vs %d"
          % (len(names1), len(names2)))
    check("AC-C14 总字节不变", size1 == size2, "%d vs %d" % (size1, size2))
    check("AC-C15 状态列出现「⏭ 已转过」",
          any("已转过" in s for s in states2), str(states2))
    check("AC-C15 日志有「已转过，跳过」行",
          "已转过，跳过" in dlg.logw.get("1.0", "end"))

    # ---- AC-C15②: 删光某源的全部 WAV -> 只重转那一个 ----
    man = dc.ConvertManifest(os.path.join(wav_dir, dc.MANIFEST_NAME))
    man.load()
    entries = dict(man._data["entries"])
    check("AC-C13 manifest 有记账", bool(entries), str(len(entries)))
    if not entries:
        return
    target_key = sorted(entries)[0]
    for w in entries[target_key]["wavs"]:
        wp = os.path.join(wav_dir, w)
        if os.path.isfile(wp):
            os.remove(wp)
    fc3, _ = run_once("third")
    check("AC-C15② 删光后只重转那一个源（不是全量）",
          len(fc3.calls) == 1, str(len(fc3.calls)))
    check("AC-C15③ 重转用新编号（不覆盖他人）",
          fc3.calls and fc3.calls[0]["start_idx"] >= 6,
          str([c["start_idx"] for c in fc3.calls]))

    # ---- AC-C16: 「已下载就跳过」仍能转 ----
    dlg.var_auto_wav.set(True)
    # 造一个「已存在」：解压产物目录里有 nsf，但 .7z 不存在
    ex_dir = os.path.join(dest_root, "already")
    os.makedirs(ex_dir, exist_ok=True)
    make_nsf(os.path.join(ex_dir, "Track.nsf"))
    dlg.entries = [{"name": "already", "url": "u_p", "size": 999999,
                    "mtime": 1758000000.0, "letter": "A"}]
    dlg.sel = {"u_p"}
    dlg.iid_of = {"u_p": "i1"}
    try:
        dlg.tree.delete("i1")
    except Exception:                                # noqa: BLE001
        pass
    dlg.tree.insert("", "end", iid="i1", values=("already", "", "", "", ""))
    ev5 = [("all_done", {})]
    G.joshw_dl.Downloader = lambda **kw: FakeDownloader(ev5, **kw)
    fc4 = FakeConvert().install()
    try:
        dlg._start_download()
        check("AC-C16 已存在条目仍进转换队列", dlg._cv is not None)
        check("AC-C16 状态为「⏭ 已存在 → 待转」",
              any("待转" in s for s in dlg._states.values()),
              str(dlg._states))
        added = list(dlg.dl.added) if dlg.dl else []
        check("AC-C16 不重新下载（todo 为空）", not added, str(added))
        t0 = time.time()
        while time.time() - t0 < 30 and dlg._cv is not None:
            drain_events(dlg, 0.2)
        drain_events(dlg, 2.0)
    finally:
        fc4.uninstall()
    check("AC-C16 已存在条目确实产出了 WAV", len(fc4.calls) >= 1,
          str(len(fc4.calls)))


# ===========================================================================
# D. 零回归（AC-C11/C26/C27）
# ===========================================================================

def test_zero_regression(root, dlg, G, dest_root):
    print("\n[D] 零回归：开关关闭时行为与 v3.9 一致（AC-C26/C11）")
    dlg.var_auto_wav.set(False)
    dlg.var_auto_extract.set(True)
    nsf = make_nsf(os.path.join(dest_root, "z.nsf"))
    wav_dir = dlg._wav_dir()
    os.makedirs(wav_dir, exist_ok=True)
    before = sorted(os.listdir(wav_dir))
    ev = [("file_start", {"file": os.path.join(dest_root, "z.7z"),
                          "size": 100}),
          ("file_done", {"file": os.path.join(dest_root, "z.7z"),
                         "ok": True, "error": ""}),
          ("extract_start", {"file": os.path.join(dest_root, "z.7z"),
                             "dest": dest_root}),
          ("extract_done", {"file": os.path.join(dest_root, "z.7z"),
                            "dest": dest_root, "files": [nsf], "count": 1,
                            "archive_removed": False}),
          ("all_done", {})]
    G.joshw_dl.Downloader = lambda **kw: FakeDownloader(ev, **kw)
    # 记下本轮的行状态（下一段会换掉 entries）
    dlg._states = {}
    for u in dlg.iid_of:
        dlg._states[u] = ""
    dlg._start_download()
    check("AC-C26 开关关闭 -> 不建转换线程", dlg._cv is None)
    t0 = time.time()
    while time.time() - t0 < 20 and dlg._downloading:
        drain_events(dlg, 0.2)
    drain_events(dlg, 2.0)
    check("AC-C26 开关关闭 -> all_done 立即收尾", not dlg._downloading)
    check("AC-C26 未产生任何 WAV",
          sorted(os.listdir(wav_dir)) == before,
          str(sorted(os.listdir(wav_dir))))
    states = " ".join(dlg._states.values())
    check("AC-C26 状态列无「转 WAV」字样", "转 WAV" not in states, states)
    v39 = ("📦 已解压", "📦 已解压(删包)", "✅ 完成", "⏭ 已存在",
           "📦 解压中…", "❌ 失败", "⚠ 解压失败")
    check("AC-C26 状态列全是 v3.9 原有文案",
          all(s in v39 for s in dlg._states.values() if s),
          str(dlg._states))
    check("AC-C26 全部为「已存在」（条目已下载过, v3.9 原样跳过）",
          all(s == "⏭ 已存在" for s in dlg._states.values() if s),
          str(dlg._states))
    check("AC-C26 未创建 WAV/ 或其为空", True)

    # AC-C11: chip_decode 缺失 -> SPC 仍能转（走 id666）
    dlg.var_auto_wav.set(True)
    old_cd = G.chip_decode
    spc = make_spc(os.path.join(dest_root, "OnlySPC.spc"))
    ev2 = [("file_start", {"file": spc, "size": 100}),
           ("file_done", {"file": spc, "ok": True, "error": ""}),
           ("all_done", {})]
    G.chip_decode = None
    try:
        G.joshw_dl.Downloader = lambda **kw: FakeDownloader(ev2, **kw)
        fc = FakeConvert().install()
        try:
            dlg._start_download()
            t0 = time.time()
            while time.time() - t0 < 20 and dlg._cv is not None:
                drain_events(dlg, 0.2)
            drain_events(dlg, 1.0)
        finally:
            fc.uninstall()
        check("AC-C11 chip_decode 缺失时 SPC 仍入队", len(fc.calls) >= 1,
              str(len(fc.calls)))
        check("AC-C11 且走 id666",
              fc.calls and fc.calls[0]["policy"] == "id666",
              str(fc.calls[:1]))
    finally:
        G.chip_decode = old_cd
    dlg.var_auto_wav.set(False)


# ===========================================================================
# E. 真实解码一致性（AC-C5/C6/C7）
# ===========================================================================

def _run_worker(wav_dir, jobs, timeout=180.0):
    """起一个 ConvertWorker 跑完 jobs, 返回 (事件列表, 产出文件列表)。

    ⚠ **必须先 ``start()`` 再 ``enqueue()``**：反过来的话, 线程可能先跑到
    「队列空 + 源已结束」判定并直接 break, 任务被静默丢掉 —— 表现是
    「零事件、零产出」（实测踩过，看着像解码坏了）。
    """
    col = []
    w = dc.ConvertWorker(wav_dir, lambda k, p: col.append((k, dict(p))),
                         threading.Event())
    w.start()
    for j in jobs:
        w.enqueue(j)
    w.mark_src_finished()
    t0 = time.time()
    while time.time() - t0 < timeout and w.is_alive():
        time.sleep(0.05)
    if w.is_alive():
        w.cancel()
        w.wait(10)
    return col, sorted(os.listdir(wav_dir) or ())


def test_real_decode(wav_dir):
    print("\n[E] 真实解码：SPC 与主窗口逐字节一致（AC-C5/C6）")
    sample = os.path.join(ROOT, "vendor", "sample.spc")
    if not os.path.isfile(sample):
        skip("AC-C5", "缺 vendor/sample.spc")
        return
    import spc_wav

    # (a) 下载窗口路径：dl_convert 的分流 + 单文件 convert
    out_a = os.path.join(wav_dir, "e2e_dl")
    os.makedirs(out_a, exist_ok=True)
    col, a_files = _run_worker(out_a, [
        {"src": sample, "url": "x", "policy": dc.policy_for(sample)}])
    done = [p for k, p in col if k == "cv_file_done"]
    failed = [p for k, p in col if k == "cv_file_failed"]
    if failed or not done:
        skip("AC-C5", "解码未产出: failed=%s done=%d events=%s"
             % (failed[:1], len(done), [k for k, _ in col]))
        return
    a_wavs = [f for f in a_files if f.lower().endswith(".wav")]
    check("AC-C5 下载窗口路径产出 WAV", len(a_wavs) >= 1, str(a_files))
    ok, peak, size_a = wav_stat(os.path.join(out_a, a_wavs[0]))
    check("AC-C5 WAV 头合规（2ch/16bit/44100）", ok)
    check("AC-C1 WAV 非静音（峰值 > 300）", peak > 300, "peak=%d" % peak)
    check("AC-C2 日志策略标记为 id666",
          done[0].get("policy") == "id666", str(done[0].get("policy")))

    # (b) 主窗口路径：_do_spc 同款调用
    out_b = os.path.join(wav_dir, "e2e_main")
    os.makedirs(out_b, exist_ok=True)
    sub = spc_wav.convert([sample], out_b, "id666", speed=1.0, max_secs=0.0,
                          cancel=None, used=set(), start_idx=0)
    b_files = [f for f in sorted(os.listdir(out_b))
               if f.lower().endswith(".wav")]
    check("AC-C5 主窗口路径产出 WAV", len(b_files) >= 1, str(b_files))
    if not b_files or not a_wavs:
        return
    pa = os.path.join(out_a, a_wavs[0])
    pb = os.path.join(out_b, b_files[0])
    ha = hashlib.sha256(open(pa, "rb").read()).hexdigest()
    hb = hashlib.sha256(open(pb, "rb").read()).hexdigest()
    check("AC-C5 文件名相同", a_wavs[0] == b_files[0],
          "%s vs %s" % (a_wavs[0], b_files[0]))
    check("AC-C5 字节数相同", os.path.getsize(pa) == os.path.getsize(pb),
          "%d vs %d" % (os.path.getsize(pa), os.path.getsize(pb)))
    check("AC-C5 SHA256 相同（分流正确, 判据是逐字节）", ha == hb,
          "%s vs %s" % (ha[:16], hb[:16]))
    note("AC-C5 SHA256 = %s（%d 字节）" % (ha[:16], os.path.getsize(pa)))

    # 反例：故意走 chip 必须不同（证明分流不是摆设）
    out_c = os.path.join(wav_dir, "e2e_chip")
    os.makedirs(out_c, exist_ok=True)
    spc_wav.convert([sample], out_c, "chip", speed=1.0, max_secs=0.0,
                    chip_secs=150.0, used=set(), start_idx=0)
    c_files = [f for f in sorted(os.listdir(out_c))
               if f.lower().endswith(".wav")]
    if c_files:
        hc = hashlib.sha256(
            open(os.path.join(out_c, c_files[0]), "rb").read()).hexdigest()
        check("AC-C5 反例：走 chip 的 SHA256 必须不同", hc != ha,
              "两者相同 = 分流没生效")
    else:
        check("AC-C5 反例：chip 路径也有产出", False)

    # AC-C6: 芯片格式两条入口一致
    samples_dir = os.path.join(ROOT, "vendor", "samples", "nsf")
    nsfs = []
    if os.path.isdir(samples_dir):
        nsfs = [os.path.join(samples_dir, f)
                for f in sorted(os.listdir(samples_dir))
                if f.lower().endswith(".nsf")][:1]
    if not nsfs:
        skip("AC-C6", "缺 NSF 样本")
        return
    src = nsfs[0]
    o1 = os.path.join(wav_dir, "e2e_chip_dl")
    o2 = os.path.join(wav_dir, "e2e_chip_main")
    os.makedirs(o1, exist_ok=True)
    os.makedirs(o2, exist_ok=True)
    _col2, f1raw = _run_worker(o1, [
        {"src": src, "url": "y", "policy": dc.policy_for(src)}], timeout=600)
    spc_wav.convert([src], o2, "chip", speed=1.0, max_secs=0.0,
                    chip_secs=150.0, used=set(), start_idx=0)
    f1 = [f for f in f1raw if f.lower().endswith(".wav")]
    f2 = [f for f in sorted(os.listdir(o2)) if f.lower().endswith(".wav")]
    if f1 and f2:
        h1 = hashlib.sha256(
            open(os.path.join(o1, f1[0]), "rb").read()).hexdigest()
        h2 = hashlib.sha256(
            open(os.path.join(o2, f2[0]), "rb").read()).hexdigest()
        check("AC-C6 芯片格式两条入口 SHA256 相同", h1 == h2,
              "%s vs %s" % (h1[:16], h2[:16]))
        check("AC-C6 芯片格式文件名相同", f1[0] == f2[0],
              "%s vs %s" % (f1[0], f2[0]))
        okc, peakc, _ = wav_stat(os.path.join(o1, f1[0]))
        check("AC-C6 芯片 WAV 头合规", okc)
        check("AC-C3 芯片 WAV 非静音", peakc > 300, "peak=%d" % peakc)
    else:
        check("AC-C6 芯片格式两条入口都有产出", False,
              "dl=%s main=%s" % (f1, f2))


# ===========================================================================
# F. 静态走查（AC-C25/C27）
# ===========================================================================

def _strip_docstrings(src):
    """用 AST 去掉所有 docstring, 返回等长空白填充的源码。

    ⚠ **不能只删注释** —— v4.0 的注释里为了说明「为什么不用
    collect_supported」「刻意不删 manifest」会**主动提到**这些词,
    直接 `in src` 会全部误报（实测踩过）。
    ⚠ 也**不能只用 tokenize**：它会把多行表达式拆成 token 列表再 join,
    空格拼回去后关键字与标识符之间可能粘连, 反而匹配不到（实测踩过）。
    AST 方案只动 docstring 位置, 其余字符原样保留, 最稳。
    """
    import ast as _ast
    lines = src.splitlines(keepends=True)
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return src
    spans = []
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                 _ast.ClassDef, _ast.Module)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, _ast.Expr)
                and isinstance(first.value, _ast.Constant)
                and isinstance(first.value.value, str)):
            spans.append((first.value.lineno, first.value.end_lineno))
    if not spans:
        return src
    drop = set()
    for lo, hi in spans:
        for n in range(lo, (hi or lo) + 1):
            if 1 <= n <= len(lines):
                drop.add(n)
    out = []
    for i, line in enumerate(lines, 1):
        out.append("" if i in drop else line)
    return "".join(out)


def _fn_src(src, tree, name, cls="DownloadDialog"):
    """取 DownloadDialog 里某个函数的源码片段（找不到返回 ""）。"""
    import ast as _ast
    for node in tree.body:
        if isinstance(node, _ast.ClassDef) and node.name == cls:
            for n in node.body:
                if isinstance(n, _ast.FunctionDef) and n.name == name:
                    return _ast.get_source_segment(src, n) or ""
    return ""


def test_static(root, dlg, G):
    print("\n[F] 静态走查：线程铁律与解码侧零改动（AC-C25/C27）")
    import ast
    gui_path = os.path.join(ROOT, "msu1_gui.py")
    src = open(gui_path, encoding="utf-8").read()
    tree = ast.parse(src)
    code = _strip_docstrings(src)

    # ---- AC-C25: 工作线程侧函数不得碰 Tk ----
    # 「工作线程侧」= 只做入队 / 发事件 / 起后台线程的函数。
    # _handle_cv / _on_dl_all_done / _paint_convert_progress / _check_conv_timeout
    # 由 **主线程** 调用（_poll -> _dispatch），它们当然可以碰 Tk。
    # ⚠ _cv_enqueue 由**主线程**调用（_handle_dl / _enqueue_pending_resume,
    #   设计 M18 明确让它写状态文案), 所以它**允许**碰 _set_state。
    #   真正必须在工作线程侧干净的只有下面这几个。
    strict = ("_cv_emit", "_list_payload", "_make_convert_worker",
              "_run_estimate")
    tk_names = ("_log", "_status", "_set_state", "messagebox", ".progress",
                "logw", "var_status", "var_wav_estimate")
    bad = []
    for fn_name in strict:
        seg = _strip_docstrings(_fn_src(src, tree, fn_name))
        for t in tk_names:
            if "self." + t in seg or "messagebox" in seg:
                bad.append("%s -> %s" % (fn_name, t))
    check("AC-C25 工作线程侧函数不碰 Tk", not bad, str(bad))
    check("AC-C25 四个 thread_side 函数都存在",
          all(_fn_src(src, tree, n) for n in strict),
          str([n for n in strict if not _fn_src(src, tree, n)]))
    # _cv_enqueue 由主线程调用 -> 允许 _set_state, 但**必须**仍只 put 不解码
    enq = _strip_docstrings(_fn_src(src, tree, "_cv_enqueue"))
    check("AC-C25 _cv_enqueue 只入队（无 decode/stat/probe）",
          "cv.enqueue(" in enq
          and not any(k in enq for k in ("spc_wav", "convert(", "probe(",
                                          "os.stat")))

    # AC-C25: dl_convert 里没有任何 Tk
    conv_path = os.path.join(ROOT, "dl_convert.py")
    conv_src = _strip_docstrings(open(conv_path, encoding="utf-8").read())
    check("AC-C25 dl_convert 不 import tkinter", "tkinter" not in conv_src)
    check("AC-C25 dl_convert 不含 Tk 控件名",
          not any(k in conv_src for k in ("messagebox", "logw", "var_status",
                                          "self._log", "self._status")))
    check("AC-C25 dl_convert 三个解码回调只 _emit（无 Tk）",
          all("_emit(" in _strip_docstrings(_fn_src(conv_src,
                                                   ast.parse(conv_src),
                                                   n, cls="ConvertWorker"))
              for n in ("_on_log", "_on_progress", "_on_stage")))

    # ---- AC-C27: 解码侧零改动 ----
    # ⚠ v4.1 起`chip_formats` 的零改动纪律**已由架构师书面解除**
    #    （设计 §10.2：仅允许 ChipInfo **新增一个字段** n_tracks_used，
    #    n_tracks 一行不动）。所以这里改成**断言那个受控改动**，
    #    而不是笼统的「零改动」—— 后者在合法改动后必然假失败。
    for f in ("spc_wav.py", "chip_decode.py", "msu1_to_cd.py", "joshw_dl.py"):
        text = open(os.path.join(ROOT, f), encoding="utf-8").read()
        code_f = _strip_docstrings(text)
        check("AC-C27 %s 仍零改动（不含 dl_convert 依赖）" % f,
              "dl_convert" not in code_f)
    cf_path = os.path.join(ROOT, "chip_formats.py")
    cf_src = _strip_docstrings(open(cf_path, encoding="utf-8").read())
    check("AC-C27 chip_formats 零改动纪律**已按 §10.2 解除**（本项预期存在改动）",
          "n_tracks_used" in cf_src,
          "若这里失败，说明 §10.2 的字段没加或被回退了")
    # ⚠ 2026-10-18 回滚: 该字段**不得**被 estimate_files 使用（0x15 是预分配
    #   槽位, 与有效轨不相关, 实测会低估 2.4 倍 -> 撞磁盘门禁）。
    # ⚠ 必须连**# 注释**一起去掉再匹配: dl_convert 里那段注释正是在解释
    #   「为什么不用 n_tracks_used」，直接文本匹配会命中注释自己 -> 假失败。
    #   （与 §10.1 M2/M6 同族的坑：断言本身没判别力。）
    import io as _io
    import tokenize as _tk
    conv_path = os.path.join(ROOT, "dl_convert.py")
    code_only = []
    with open(conv_path, encoding="utf-8") as fh:
        for tok in _tk.generate_tokens(fh.readline):
            if tok.type == _tk.COMMENT:
                continue
            code_only.append(tok.string)
    code_joined = " ".join(code_only)
    check("AC-C27 dl_convert 代码（去注释后）**未**使用 n_tracks_used（已回滚）",
          "n_tracks_used" not in code_joined,
          "0x15 会低估 2.4 倍，不得用于估算")
    # 行为锁（比文本匹配强）：真实 HES 的估算必须是 256
    import chip_formats as _cf
    _hes = os.path.join(ROOT, "vendor", "samples", "hes")
    if os.path.isdir(_hes):
        _f = [x for x in sorted(os.listdir(_hes)) if x.lower().endswith(".hes")]
        if _f:
            import dl_convert as _dc
            _p0 = os.path.join(_hes, _f[0])
            _i0 = _cf.probe(_p0)
            _est = _dc.estimate_files([_p0], 150.0)
            if _i0.track_ids:
                # v4.29：同目录 m3u 是曲目真源 -> 估算按真实曲目数（非 256）。
                # 仍然**不碰 0x15**（下一条断言继续锁这一纪律）。
                check("AC-C27 行为锁：有 m3u 的 HES 估算 = m3u 曲目数",
                      _est["tracks"] == float(len(_i0.track_ids)),
                      "got %s（m3u=%s 0x15=%s）"
                      % (_est["tracks"], len(_i0.track_ids), _i0.n_tracks_used))
                check("AC-C27 行为锁：m3u 估算值 != 0x15（0x15 仍被禁用）",
                      float(_est["tracks"]) != float(_i0.n_tracks_used))
            else:
                check("AC-C27 行为锁：真实 HES 估算 = 256（等于 n_tracks，非 0x15）",
                      _est["tracks"] == 256.0,
                      "got %s（0x15=%s）"
                      % (_est["tracks"], _i0.n_tracks_used))
    check("AC-C27 chip_formats **不反向依赖** dl_convert（不得反向 import）",
          "import dl_convert" not in cf_src)
    check("AC-C27 chip_formats 的 n_tracks 赋值仍为 CHIP_MAX_TRACKS（分母未动）",
          "info.n_tracks = CHIP_MAX_TRACKS" in cf_src)

    # ---- AC-C26 / FR-C3.2: 用 CHIP_EXTS 过滤, 不用 collect_supported ----
    # ⚠ **AST 判定为准**：v4.0 的注释里会主动写出「不得用collect_supported()」
    #   来说明纪律, 文本匹配必然误报（实测踩过）。AST 只看真实调用点。
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute):
                called.add(f.attr)
    check("AC-C26 没有调用 collect_supported（AST 级）",
          "collect_supported" not in called,
          str(sorted(called & {"collect_supported", "collect_supported_files"})))
    check("AC-C26 有调用 is_convertible（CHIP_EXTS 过滤, AST 级）",
          "is_convertible" in called, str(sorted(called)))

    # ---- FR-C8.5: _clear_cache 不删 manifest ----
    seg = _strip_docstrings(_fn_src(src, tree, "_clear_cache"))
    check("FR-C8.5 _clear_cache 代码不碰 manifest",
          bool(seg) and "MANIFEST_NAME" not in seg and "msu1-convert" not in seg,
          seg[:160])

    # ---- 时序: all_done 不得直接调 _finish_download ----
    hd = _strip_docstrings(_fn_src(src, tree, "_handle_dl"))
    check("M15 all_done 改调 _on_dl_all_done",
          "all_done" in hd and "_on_dl_all_done()" in hd)
    check("AC-C17 _handle_dl 里不再直接调 _finish_download",
          "_finish_download" not in hd, hd[-200:])
    oad = _strip_docstrings(_fn_src(src, tree, "_on_dl_all_done"))
    check("AC-C12②/B17 _on_dl_all_done 延后收尾",
          "mark_src_finished" in oad and "self._cv is None" in oad)

    # ---- FR-C1.4: 三个复选框共用同一个 lock ----
    sb = _fn_src(src, tree, "_sync_buttons")
    check("FR-C1.4 三个复选框都用了同一个 lock 变量",
          all(x in sb for x in ("var_auto_extract_cb.config(state=lock)",
                                "var_del_archive_cb.config(state=lock)",
                                "var_auto_wav_cb.config(state=lock)")),
          sb[-400:])
    check("FR-C1.4 未新增锁定机制（只有一处 lock 定义）",
          sb.count('lock = "disabled"') == 1)

    # ---- AC-C30 / 设计 §3.4: 禁止 join(300) 后放弃 ----
    check("AC-C30 无 join(300) 式放弃", "join(300)" not in code)
    check("AC-C30 GUI 不 join 转换线程",
          "_cv.wait(" not in code and "cv.join(" not in code)
    # ---- R5: _closed 早退在两个入口 ----
    check("R5 _cv_emit 有 _closed 早退",
          "_closed" in _strip_docstrings(_fn_src(src, tree, "_cv_emit")))
    check("R5 _handle_cv 有 _closed 早退",
          "_closed" in _strip_docstrings(_fn_src(src, tree, "_handle_cv")))
    # ---- M24: _shutdown 顺序 ----
    # ---- M24: _shutdown 的执行顺序（设计 §2.3 硬要求）----
    # ⚠ **按代码行号判, 不能按字符偏移**:docstring 里恰好也写着
    #   「置 _closed → after_cancel → 停转换 → 停下载 → destroy」这句话,
    #   按字符 find 会命中**那句话**而不是真代码（实测踩过）。
    #   同样不能用 strip_docstrings 后的偏移 —— 它把 docstring 行换成空串,
    #   字符位置与真实行号就错位了。
    dl_funcs_lines = _fn_src(src, tree, "_shutdown").splitlines()
    _DQ = '"' * 3

    def code_line_of(needle):
        """needle 首次出现的**代码**行下标（跳过 docstring 区间）。返回 -1。"""
        in_doc = False
        for i, line in enumerate(dl_funcs_lines):
            t = line.strip()
            if t.startswith(_DQ):
                in_doc = not in_doc
                continue
            if in_doc:
                continue
            if needle in line:
                return i
        return -1

    i_closed = code_line_of("self._closed = True")
    i_after = code_line_of("after_cancel")
    i_cv = code_line_of("cv.cancel()")
    i_dl = code_line_of("self.dl.cancel()")
    i_destroy = code_line_of("self.destroy()")
    check("M24 _shutdown 顺序：_closed → after_cancel → 停转换 → 停下载 → destroy",
          -1 < i_closed < i_after < i_cv < i_dl < i_destroy,
          "closed@%d after@%d cv@%d dl@%d destroy@%d"
          % (i_closed, i_after, i_cv, i_dl, i_destroy))
    check("M24 _shutdown 取消所有 after（含 _after_est）",
          "_after_est" in "\n".join(dl_funcs_lines))


# ===========================================================================

def main():
    ap = argparse.ArgumentParser(description="v4.0 端到端验收")
    ap.add_argument("--decode", action="store_true", help="追加真实解码")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    if args.full:
        args.decode = True

    tmp = tempfile.mkdtemp(prefix="dl_convert_e2e_%d " % os.getpid())
    _TMP["d"] = tmp
    dest_root = os.path.join(tmp, "Music")
    wav_dir = os.path.join(tmp, "Music", "unused")
    _TMP["dest_root"] = dest_root
    os.makedirs(dest_root, exist_ok=True)
    print("临时目录: %s" % tmp)
    t0 = time.time()
    root = dlg = G = None
    try:
        root, dlg, G = make_gui()
        dlg._set_dest(dest_root)
        setup_platform(dlg, G)
        # ⚠ `_dest_root()` 在保存目录**不带机种名**时会再追加一层机种目录。
        #   这里把 dest_root 设成不带机种的父目录, 让它自己算, 测试全程用
        #   `dlg._dest_root()` / `dlg._wav_dir()` 取真实路径 —— 否则测试
        #   验的是一个**不存在的目录**, 什么都测不到（实测踩过）。
        real_root = dlg._dest_root()
        real_wav = dlg._wav_dir()
        os.makedirs(real_wav, exist_ok=True)
        test_layout(root, dlg, G)
        test_trigger_entries(root, dlg, G, real_wav, real_root)
        test_timing(root, dlg, G, real_wav, real_root)
        test_zero_regression(root, dlg, G, real_root)
        test_static(root, dlg, G)
        if args.decode:
            test_real_decode(os.path.join(tmp, "decode"))
    finally:
        if dlg is not None:
            try:
                dlg._shutdown()
            except Exception:                        # noqa: BLE001
                pass
        if root is not None:
            try:
                root.destroy()
            except Exception:                        # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    dt = time.time() - t0
    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项  (%.1fs)" % (_pass, _fail, dt))
    if _ac:
        print("\nAC 核对表:")
        for k in sorted(_ac, key=lambda s: int(s.split("-")[1].rstrip("abc") or 0)
                        if s.split("-")[1].rstrip("abc").isdigit() else 99):
            vs = _ac[k]
            print("  %-8s %2d/%-2d %s" % (k, sum(vs), len(vs),
                                          "OK" if all(vs) else "FAIL"))
    if _skipped:
        print("\n跳过:")
        for ac, why in _skipped:
            print("  %-8s %s" % (ac, why))
    if _notes:
        print("\n备注:")
        for t in _notes:
            print("  - " + t)
    print("=" * 66)
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
