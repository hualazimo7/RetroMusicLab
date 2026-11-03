#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_disc_gui.py — 光盘镜像「真实窗口拖放 → 实际转换」验收。

不是模拟 UI：走系统级 WM_DROPFILES，再 `btn_start.invoke()` 触发真实转换，
全程截图（屏幕 BitBlt，手写 PNG，不依赖 Pillow）。

两段：
  ① 源码 GUI：拖桌面真实 PS1 文件夹 → 点「开始转换」→ 校验产出 + 三张截图
  ② 打包 exe：启动成品，发同一个拖放消息，截图证明**成品也能接住**（不点按钮）

跑法：python vendor/test_disc_gui.py            # 两段都跑
      python vendor/test_disc_gui.py --no-exe   # 只跑 ①
"""

import ctypes
import os
import subprocess
import tempfile
import sys
import time
import tkinter as tk
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))   # .../msu1-to-cd/vendor
PROJ = os.path.dirname(HERE)                        # .../msu1-to-cd（代码根）
ROOT = os.path.dirname(PROJ)                        # 项目根（_shot_theme.py 在这）
_ARGS = sys.argv[1:]                                # 先记下自己的参数（下面要覆盖 sys.argv）
sys.path.insert(0, ROOT)
sys.path.insert(0, PROJ)
os.chdir(PROJ)

# 拖放辅助（复用现成的真实 HDROP 构造）
import test_drop                                  # noqa: E402
# 截图辅助（GDI 屏幕抓取 + 手写 PNG）
import _shot_theme as shotmod                     # noqa: E402

# ⚠ 绝不能带 --no-drop，否则拖放压根不挂
sys.argv = ["msu1_gui.py"]

import msu1_gui as g                              # noqa: E402


class _FakeBox:
    """把模态对话框换成「只记录、不阻塞」。

    转换完成时 ``msu1_gui`` 会弹 ``messagebox.showinfo("完成", ...)``。
    模态框自带一个事件循环，门禁里的 ``root.update()`` 会一直卡在它身上
    —— 表现为测试跑到「点开始转换」之后就不动了（v4.43 排查过一次）。
    换成假对象后：既不会卡，又能断言「确实弹过完成框」。
    """

    def __init__(self):
        self.calls = []

    def _rec(self, kind, title="", msg="", **kw):
        self.calls.append((kind, title, msg))
        return True

    def showinfo(self, title="", msg="", **kw):
        return self._rec("info", title, msg, **kw)

    def showwarning(self, title="", msg="", **kw):
        return self._rec("warn", title, msg, **kw)

    def showerror(self, title="", msg="", **kw):
        return self._rec("error", title, msg, **kw)

    def askyesno(self, title="", msg="", **kw):
        self._rec("ask", title, msg, **kw)
        return True


BOX = _FakeBox()
g.messagebox = BOX

SRC = r"C:\Users\xqh\Desktop\Sangokushi Eiketsuden (Japan)SLPS-00309"
# 这张盘是 v4.36 的判例：CD-DA 轨**存在但整轨静音**，音乐全在数据轨的
# XA-ADPCM 里。所以：
#   · CD-DA 名义长度 212.3467s（15926 扇区 / 75）—— 静音占位轨，没有声音
#   · 真正产出的时长是 XA 的 15233 扇区 × 2016 / 37800 = 812.4267s
# v4.43 起列表三列也改报 XA 的数字（之前报 212.3s，和产出对不上），
# 两个数都写死在这里，谁改坏了都会挂。
CDDA_SECS = 212.3467                              # 15926 扇区 / 75（静音轨）
XA_SECTORS = 15233
EXPECT_SECS = XA_SECTORS * 2016 / 37800.0         # ≈812.4267s

_green, _red, _rst = "\033[32m", "\033[31m", "\033[0m"
if not sys.stdout.isatty():
    _green = _red = _rst = ""
_p = _f = 0


def check(name, cond, detail=""):
    global _p, _f
    if cond:
        _p += 1
        print(f"  {_green}PASS{_rst} {name}" + (f"  {detail}" if detail else ""))
    else:
        _f += 1
        print(f"  {_red}FAIL{_rst} {name}  {detail}")
    return bool(cond)


def pump(root, ms=600):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def main():
    if not os.path.isdir(SRC):
        print("找不到样本目录:", SRC)
        return 2

    # 上一轮 CLI 产出的旧文件先挪开，避免和 GUI 产出混在一起
    out_dir = SRC + "output"
    old = os.path.join(out_dir, "01_track_02.wav")
    if os.path.exists(old):
        os.replace(old, old + ".cli-bak")
        print("  已把旧 CLI 产出改名为 01_track_02.wav.cli-bak")

    g.release_session_lock()
    root = tk.Tk()
    app = g.App(root)
    root.update()
    root.lift()
    root.attributes("-topmost", True)
    time.sleep(0.8)
    root.update()

    print("\n=== ① 源码 GUI：真实拖放 + 真实转换 ===")
    check("拖放已挂载", bool(app.drop_enabled))
    shotmod.shot(root, "ui_disc_01_before.png")

    hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2) or root.winfo_id()
    print("  发送 WM_DROPFILES:", SRC)
    test_drop.drop(hwnd, [SRC])
    pump(root, 1200)

    n = len(app.sources)
    kind = app.sources[0]["kind"] if app.sources else "-"
    check("拖入后音源 1 项", n == 1, f"实际 {n}")
    check("识别为光盘镜像", kind == "光盘镜像", kind)
    check("输出目录自动设为 <源文件夹>output",
          os.path.normpath(app.var_out.get()) == os.path.normpath(out_dir),
          app.var_out.get())
    print("  预算行:", app.var_info.get())
    # v4.43：列表三列改报 XA（这张盘的 CD-DA 是静音占位轨，报它等于骗人）
    check(f"列表时长 = XA {EXPECT_SECS:.1f}s（不再报静音 CD-DA 的 "
          f"{CDDA_SECS:.1f}s）",
          abs(float(app.sources[0].get("total_secs") or 0) - EXPECT_SECS) < 1.0,
          f"{app.sources[0].get('total_secs')}")
    shotmod.shot(root, "ui_disc_02_dropped.png")

    # 真实按钮点击
    print("  点击「开始转换」…")
    t0 = time.time()
    app.btn_start.invoke()
    deadline = time.time() + 120
    while time.time() < deadline:
        root.update()
        time.sleep(0.02)
        w = getattr(app, "worker", None)
        if w is not None and not w.is_alive():
            break
    pump(root, 600)
    dt = time.time() - t0
    check("转换线程已结束", getattr(app, "worker", None) is None or
          not app.worker.is_alive(), f"{dt:.1f}s")
    # 截图前先看一眼窗口状态：万一被最小化/藏起来，GetWindowRect 会拿到怪尺寸
    print("  窗口状态: state=%s mapped=%s geom=%s" % (
        root.state(), root.winfo_ismapped(), root.winfo_geometry()))
    root.deiconify()
    root.lift()
    pump(root, 300)
    shotmod.shot(root, "ui_disc_03_done.png")

    # 日志区在窗口下方（980px 高看不到），直接把文本读出来断言 —— 比截图可靠
    log_text = app.logw.get("1.0", "end")
    tail = [ln for ln in log_text.splitlines() if ln.strip()][-12:]
    print("  ---- 界面日志尾部 ----")
    for ln in tail:
        print("   ", ln)
    check("日志有镜像策略行", "光盘镜像" in log_text and "CD-DA" in log_text)
    check("日志报告 WAV 完成 1 首", "✅ WAV 完成 1 首" in log_text,
          [ln for ln in tail if "WAV 完成" in ln][:1])
    done_boxes = [c for c in BOX.calls if c[0] == "info" and c[1] == "完成"]
    check("转换结束弹了「完成」框（已被门禁拦成非阻塞）", len(done_boxes) == 1,
          str(BOX.calls[:3]))

    wavs = sorted(f for f in os.listdir(out_dir) if f.lower().endswith(".wav")
                  and not f.lower().endswith(".cli-bak"))
    print("  产出:", wavs)
    check("产出 1 个 WAV", len(wavs) == 1, str(wavs))
    if wavs:
        import wave
        p = os.path.join(out_dir, wavs[0])
        with wave.open(p, "rb") as h:
            secs = h.getnframes() / h.getframerate()
            fmt = (h.getnchannels(), h.getsampwidth() * 8, h.getframerate())
        # v4.36：这张盘 CD-DA 整轨静音 -> 自动改抽 XA，轨名后缀是 "- XA"
        check("文件名 = 镜像名 + XA（静音盘改抽 XA）",
              "Sangokushi Eiketsuden (Japan) - XA" in wavs[0], wavs[0])
        check(f"时长 {EXPECT_SECS:.3f}s（XA {XA_SECTORS} 扇区）",
              abs(secs - EXPECT_SECS) < 0.01, f"{secs:.3f}s")
        check("日志说明改抽了 XA", "XA" in log_text and "CD-DA" in log_text,
              [ln for ln in tail if "XA" in ln][:1])
        check("格式 2ch/16bit/44.1k", fmt == (2, 16, 44100), str(fmt))
        print("  文件大小: %.2f MB" % (os.path.getsize(p) / 1048576))

    root.attributes("-topmost", False)
    app._on_close()
    g.release_session_lock()

    # ---------------------------------------------------------------
    if "--no-exe" in _ARGS:
        pass
    else:
        print("\n=== ② 打包 exe：转换 + 拖放挂载 ===")
        exe = os.path.join(PROJ, "dist", "MSU1Converter.exe")
        tmp_out = os.path.join(tempfile.mkdtemp(prefix="exe_disc_"), "out")
        os.makedirs(tmp_out)
        # (a) exe 的 CLI 实跑：成品级端到端（命名 + 时长都要对）
        r = subprocess.run([exe, "--convert", tmp_out, SRC],
                           capture_output=True, text=True, errors="replace",
                           timeout=300)
        made = [f for f in os.listdir(tmp_out) if f.lower().endswith(".wav")]
        check("exe CLI 返回 0", r.returncode == 0, f"rc={r.returncode}")
        check("exe 产出 1 个 WAV", len(made) == 1, str(made))
        if made:
            import wave
            with wave.open(os.path.join(tmp_out, made[0]), "rb") as h:
                secs = h.getnframes() / h.getframerate()
            check("exe 产出按「镜像名 + XA」命名",
                  "Sangokushi Eiketsuden (Japan) - XA" in made[0], made[0])
            check(f"exe 产出时长 {EXPECT_SECS:.3f}s",
                  abs(secs - EXPECT_SECS) < 0.01, f"{secs:.3f}s")

        # (b) exe 起窗口：证明成品把拖放挂上了（DragAcceptFiles 成功）
        # ⚠ 这里**不能**用 WM_DROPFILES 跨进程注入来验证拖放：HDROP 是发送进程
        #   的全局内存句柄，跨进程不编组，目标进程 DragQueryFile 拿不到路径 ——
        #   那会得到一个假的"拖放坏了"。拖放的真实行为由 ① 在源码进程内验证。
        proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
        try:
            hwnd_exe = None
            for _ in range(60):
                time.sleep(0.5)
                hwnd_exe = _find_tk_window(proc.pid)
                if hwnd_exe:
                    break
            check("exe 主窗已出现", bool(hwnd_exe), f"hwnd={hwnd_exe}")
            if hwnd_exe:
                time.sleep(2.0)          # 等 App.__init__ 把拖放挂完
                ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(hwnd_exe))
                time.sleep(0.6)
                _grab_hwnd(hwnd_exe, "ui_disc_04_exe.png")
                logf = os.path.join(os.path.dirname(exe), "crash.log")
                mounted = False
                try:
                    with open(logf, "r", encoding="utf-8", errors="replace") as fh:
                        mounted = "拖放挂载: True" in fh.read()[-4000:]
                except OSError:
                    pass
                check("exe 把拖放挂上了（日志 拖放挂载: True）", mounted)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            g.release_session_lock()

    # ---------------------------------------------------------------
    # ③ v4.37：纯数据盘（CUE 里只有 TRACK 01 MODE2/2352）在主窗里的识别。
    # 只验「加进来 → 列表三列 / 计划行对不对」，**不真转换** —— 那张盘要
    # 200 条轨 / 645MB / 2 分钟，放门禁里太重。转换本身由 exe CLI 与
    # test_xa.py ⑦ 各自覆盖。
    # ---------------------------------------------------------------
    PURE = r"C:\Users\xqh\Desktop\Silent Hill (USA)"
    if os.path.isdir(PURE):
        print("\n=== ③ 纯数据盘：主窗识别 + 列表元信息（v4.37）===")
        t0 = time.time()
        kind = g.App._kind_of_dir(PURE)
        check("纯数据盘判为光盘镜像", kind == "光盘镜像", kind)
        meta = g.core.probe_disc_audio(PURE)
        check("probe 报出 200 条 XA 流", meta["n_tracks"] == 200, str(meta))
        check("probe 报出 63.8 分钟",
              abs(meta["total_secs"] - 3830.99) < 1.0,
              f"{meta['total_secs']:.2f}s")
        plan = g.ConvertWorker._plan_line(None, {
            "kind": kind, "n_tracks": meta["n_tracks"],
            "total_secs": meta["total_secs"]})
        check("计划行写的是 ×200 条 + 合计 1:03:51",
              "×200 条" in plan and "1:03:51" in plan, plan)
        print(f"  （探元信息耗时 {time.time() - t0:.1f}s）")
    else:
        print(f"\n=== ③ 跳过：找不到 {PURE} ===")

    print("\n" + "=" * 62)
    print(f"结果: {_p} 通过 / {_f} 失败")
    return 0 if _f == 0 else 1


def _find_tk_window(pid):
    """找主窗句柄。

    ⚠ **不能只按 pid 匹配**：PyInstaller onefile 的 exe 会再 fork 一个子进程跑
    Python，Tk 窗口属于**子进程**，父进程只有 `PyInstallerOnefileHiddenWindow`
    （实测：父 pid 名下只有那个隐藏窗 + IME）。所以按「类名 TkTopLevel +
    标题含 RetroMusic Lab + 可见」找，pid 只作为兜底。
    """
    u32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if not u32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "TkTopLevel":
            return True
        title = ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, title, 256)
        if "RetroMusic Lab" in title.value:
            found.append(hwnd)
            return False
        return True

    u32.EnumWindows(cb, 0)
    return found[0] if found else None


def _grab_hwnd(hwnd, name):
    u32 = ctypes.windll.user32
    rect = wintypes.RECT()
    u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        print("  !! 窗口矩形异常", w, h)
        return
    rgb = shotmod.grab_screen(rect.left, rect.top, w, h)
    out = os.path.join(PROJ, "vendor", name)
    shotmod.write_png(out, w, h, rgb)
    print("  -> %s (%dx%d, %d KB)" % (name, w, h, os.path.getsize(out) // 1024))


if __name__ == "__main__":
    sys.exit(main())
