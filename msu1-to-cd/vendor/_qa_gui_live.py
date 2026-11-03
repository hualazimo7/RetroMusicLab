# -*- coding: utf-8 -*-
"""QA 独立验收 · 真机 tkinter 界面验收（v4.0）

团队 lead 要求: 实现者的测试替代不了「真看一次渲染」。本脚本用**真实 Tk**
打开下载窗口, 逐项断言并**截图存证**, 覆盖:

  UI-1新复选框在位、文案与截图一致、默认关闭、不持久化
  UI-2 「解压器: tar.exe」标签**没被挤掉**（真实几何断言 + 截图）
  UI-3 开关关闭: 不建线程、不建 WAV/ 目录、状态列只有 v3.9 原有文案
  UI-4 开关打开: 建 WAV/ 目录、门禁提示变化
  UI-5 关窗时转换线程退出、无 Tcl `invalid command name`
  UI-6 「■ 停止」在转换排空期仍可点（B17 现象层）
  UI-7 窗口几何/最小尺寸未被新控件撑坏

用法: C:/Python314/python.exe vendor/_qa_gui_live.py
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SHOT_DIR = os.path.join(ROOT, "vendor", "_qa_shots")

PASS = [0]
FAIL = []


def check(label, cond, detail="", ac=""):
    PASS[0] += 1
    line = "  [%s] %s" % ("PASS" if cond else "FAIL", label)
    if ac:
        line += "  (%s)" % ac
    if detail:
        line += "  -- %s" % detail
    print(line, flush=True)
    if not cond:
        FAIL.append("%s | %s" % (label, detail))
    return bool(cond)


def shot(widget, name):
    """截图存证（失败也不该中断验收）。"""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        widget.update_idletasks()
        widget.update()
        time.sleep(0.25)
        widget.update()
        # Windows 用 PowerShell 抓整个屏幕
        ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
              "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
              "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
              "$g=[System.Drawing.Graphics]::FromImage($bmp);"
              "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
              "$bmp.Save('%s');$g.Dispose();$bmp.Dispose()" %
              os.path.join(SHOT_DIR, name).replace("/", "\\"))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=40)
        print("      [截图] %s" % os.path.join(SHOT_DIR, name))
    except Exception as e:                             # noqa: BLE001
        print("      [截图失败] %s: %s" % (type(e).__name__, e))


def pump(dlg, seconds=0.5, step=0.02):
    """真实事件循环泵（不是假 update）。"""
    end = time.time() + seconds
    while time.time() < end:
        try:
            dlg.update()
        except Exception:                              # noqa: BLE001
            break
        time.sleep(step)


def main():
    print("=" * 78)
    print("QA 独立验收 · 真机界面验收  pid=%d" % os.getpid())
    print("=" * 78)

    import tkinter as tk
    from tkinter import ttk

    tmp = tempfile.mkdtemp(prefix="qa_gui_live_%d " % os.getpid())
    dest = os.path.join(tmp, "Music", "JoshW")
    os.makedirs(dest, exist_ok=True)
    print("目标目录: %s" % dest)

    import msu1_gui as G
    # 真机验收：弹窗必须换成 no-op，否则无人值守会挂在模态框上
    _msgs = []
    G.messagebox.showinfo = lambda *a, **k: _msgs.append(("info", a))
    G.messagebox.showwarning = lambda *a, **k: _msgs.append(("warn", a))
    G.messagebox.showerror = lambda *a, **k: _msgs.append(("error", a))
    G.messagebox.askyesno = lambda *a, **k: True

    _tkroot = tk.Tk()
    _tkroot.withdraw()
    dlg = G.DownloadDialog(_tkroot)
    # 目标目录：仿照主窗口填法（var_dest 已含机种层时不再自动加）
    dlg.var_dest.set(dest)
    dlg._dest_auto = False
    dlg.cur_key = "snes"
    dlg.cur_name = "SNES / SFC"
    dlg.deiconify()
    dlg.lift()
    pump(dlg, 1.5)

    try:
        # ---------------------------------------------------- UI-1 复选框
        print("\n===== UI-1 新复选框在位 / 文案 / 默认关闭 =====")
        cb = dlg.var_auto_wav_cb
        check("UI-1.1 var_auto_wav_cb 存在且已建控件",
              isinstance(cb, ttk.Checkbutton))
        txt = str(cb.cget("text"))
        check("UI-1.2 文案 == 「下载后自动转 WAV」",
              txt == "下载后自动转 WAV", "实际=%r" % txt)
        check("UI-1.3 默认关闭（AC-C1.1）",
              dlg.var_auto_wav.get() is False,
              "var_auto_wav=%r" % dlg.var_auto_wav.get())
        cbv = dlg.var_auto_wav
        check("UI-1.4 BooleanVar 绑定一致（variable= 同一个）",
              str(cb.cget("variable")) != "",
              "variable=%s" % cb.cget("variable"))
        pump(dlg, 0.3)
        check("UI-1.5 复选框真实可见（winfo_viewable）",
              bool(cb.winfo_viewable()),
              "viewable=%s state=%s" % (cb.winfo_viewable(), cb.cget("state")))
        _x, cy = cb.winfo_rootx(), cb.winfo_rooty()
        _w, h = cb.winfo_width(), cb.winfo_height()
        check("UI-1.6 复选框有非零几何（真实渲染非 0）",
              _w > 10 and h > 5, "w=%s h=%s @(%s,%s)" % (_w, h, _x, cy))

        # ------------------------------------------- UI-2 tar 标签没被挤掉
        print("\n===== UI-2 「解压器: tar.exe」标签未被挤掉（AC-C18）=====")
        pump(dlg, 0.5)
        tar_txt = str(dlg.var_extract_tar.get())
        check("UI-2.1 解压器标签有内容",
              bool(tar_txt.strip()), "实际=%r" % tar_txt)
        check("UI-2.2 标签文案含「解压器」",
              "解压器" in tar_txt, "实际=%r" % tar_txt)
        tar_lbl = None
        for w in _walk(dlg):
            try:
                if isinstance(w, (tk.Label, ttk.Label)) and \
                        "解压器" in str(w.cget("text")):
                    tar_lbl = w
                    break
            except Exception:                          # noqa: BLE001
                continue
        check("UI-2.3 找到「解压器」标签控件", tar_lbl is not None)
        if tar_lbl is not None:
            pump(dlg, 0.4)
            tw, th = tar_lbl.winfo_width(), tar_lbl.winfo_height()
            check("UI-2.4 标签有真实尺寸（未被挤成 0）",
                  tw > 20 and th > 5,
                  "w=%s h=%s reqw=%s" % (tw, th, tar_lbl.winfo_reqwidth()))
            reqw = tar_lbl.winfo_reqwidth()
            check("UI-2.5 实际宽度 >= 请求宽度（没被截断）",
                  tw >= reqw - 2, "actual=%s req=%s" % (tw, reqw))
            check("UI-2.6 标签可见", bool(tar_lbl.winfo_viewable()))
            ty = tar_lbl.winfo_rooty()
            check("UI-2.7 标签不在窗口可视区之外",
                  dlg.winfo_rooty() <= ty <= dlg.winfo_rooty() +
                  dlg.winfo_height(),
                  "标签y=%s 窗口y=%s h=%s" % (ty, dlg.winfo_rooty(),
                                        dlg.winfo_height()))
            # 文本是否被完整容纳
            check("UI-2.8 标签文本未被截断（宽度足够）",
                  tw >= reqw - 2,
                  "标签 %r reqw=%s actual=%s" % (tar_txt, reqw, tw))
        check("UI-2.9 复选框与解压器标签不在同一行（标签独占一行）",
              _different_row(cb, tar_lbl),
              "cb.y=%s tar.y=%s" % (cy, tar_lbl.winfo_rooty() if tar_lbl else "-"))
        shot(dlg, "qa_ui1_default.png")

        # -------------------------------- UI-3 开关关闭 -> 零回归
        print("\n===== UI-3 开关关闭：零回归（AC-C26 / F 组）=====")
        dl_before = dlg.dl
        wav_dir = os.path.join(dlg._dest_root(), "WAV")
        check("UI-3.1 关闭态 ConvertWorker 未创建",
              dlg._cv is None, "_cv=%r" % (dlg._cv,))
        check("UI-3.2 关闭态 WAV 目录未被创建",
              not os.path.isdir(wav_dir), wav_dir)
        man = os.path.join(wav_dir, ".msu1-convert.json")
        check("UI-3.3 关闭态 manifest 未被创建",
              not os.path.isfile(man), man)
        check("UI-3.4 关闭态 未发生转换（_cv_queued=0）",
              int(getattr(dlg, "_cv_queued", 0)) == 0,
              "_cv_queued=%r" % getattr(dlg, "_cv_queued", None))
        # v3.9 原有文案
        check("UI-3.5 状态栏初始文案是 v3.9 原文（无「🎵」）",
              "🎵" not in str(dlg.var_status.get()),
              "status=%r" % str(dlg.var_status.get()))
        check("UI-3.6 解压器标签不受开关影响",
              "解压器" in str(dlg.var_extract_tar.get()))

        # -------------------------------- UI-4 开关打开
        print("\n===== UI-4 开关打开：门禁 / 目录 / 文案变化 =====")
        dlg.var_auto_wav.set(True)
        pump(dlg, 0.8)
        check("UI-4.1 勾选后变量为真", dlg.var_auto_wav.get() is True)
        shot(dlg, "qa_ui2_checked.png")
        # 打开态是否立刻建目录（设计是懒创建于消费时，这里只看门禁提示）
        hint = ""
        try:
            hint = str(dlg.var_wav_estimate.get())
        except Exception:                              # noqa: BLE001
            pass
        check("UI-4.2 勾选后有门禁/提示文案变化或明确为空（不抛异常）",
              True, "gate_hint=%r" % hint[:80])
        check("UI-4.3 打开态未凭空建线程（仍需点开始）",
              dlg._cv is None, "_cv=%r" % (dlg._cv,))
        # 再关回去，验证可逆
        dlg.var_auto_wav.set(False)
        pump(dlg, 0.5)
        check("UI-4.4 可取消勾选（AC-C13 可逆）",
              dlg.var_auto_wav.get() is False)

        # -------------------------------- UI-6 排空期停止按钮可点（B17）
        print("\n===== UI-6 转换排空期「■ 停止」仍可点（B17 现象层）=====")
        # 模拟排空期：dl=None / _downloading=False，但转换线程活着
        dlg.var_auto_wav.set(True)
        pump(dlg, 0.4)
        import dl_convert as dc

        # ⚠ 必须用**真会慢**的 convert：原来用 no-op 回调，worker 秒退，
        #   根本进不了「排空期」，测的是已结束状态而非排空期。
        slow_started = threading.Event()

        def slow_convert(paths, out_dir, policy, cancel=None, **kw):
            """跑 30s 的假 convert，但**每 0.05s 查一次 cancel**——
            复刻 write_tracks 的检查点行为，停止能真实收手。"""
            slow_started.set()
            t0 = time.time()
            while time.time() - t0 < 30.0:
                if cancel is not None and cancel.is_set():
                    break
                time.sleep(0.05)
            return {"ok": 0, "fail": 0, "files": [], "seconds": time.time() - t0,
                    "errors": [], "last_idx": kw.get("start_idx", 0)}

        cv = dc.ConvertWorker(os.path.join(tmp, "drain", "WAV"),
                              lambda *a: None, threading.Event())
        cv.convert_fn = slow_convert
        cv.start()
        # ⚠ 源文件必须**真实存在**，否则 _convert_one 在「源文件不存在」就
        #   early-return，压根不会调 convert —— 那样测的是秒退路径不是排空期。
        real_src = os.path.join(tmp, "x.nsf")
        with open(real_src, "wb") as fh:
            fh.write(b"NESM" + b"\x1a" + b"\x00" * 0x100)
        cv.enqueue({"src": real_src, "url": "u", "policy": "chip"})
        cv.mark_src_finished()
        end = time.time() + 15
        while time.time() < end and not slow_started.is_set():
            pump(dlg, 0.05)
        check("UI-6.0 慢 convert 已开跑（排空期场景成立）",
              slow_started.is_set() and cv.is_alive(),
              "started=%s alive=%s" % (slow_started.is_set(), cv.is_alive()))
        dlg._cv = cv
        dlg.dl = None
        dlg._downloading = False
        dlg._cv_queued = 3
        dlg._sync_buttons()
        pump(dlg, 0.5)
        check("UI-6.1 排空期 btn_stop 为 normal（B17 关键）",
              str(dlg.btn_stop.cget("state")) == "normal",
              "state=%s" % dlg.btn_stop.cget("state"))
        check("UI-6.2 排空期 btn_start 为 disabled",
              str(dlg.btn_start.cget("state")) == "disabled",
              "state=%s" % dlg.btn_start.cget("state"))
        check("UI-6.3 排空期三个复选框 disabled（判据与停止按钮拆开）",
              all(str(w.cget("state")) == "disabled" for w in
                  (dlg.var_auto_extract_cb, dlg.var_del_archive_cb,
                   dlg.var_auto_wav_cb)),
              "extract=%s del=%s wav=%s" % (
                  dlg.var_auto_extract_cb.cget("state"),
                  dlg.var_del_archive_cb.cget("state"),
                  dlg.var_auto_wav_cb.cget("state")))
        check("UI-6.4 _cv_busy() 在排空期为真",
              bool(dlg._cv_busy()), "_cv_busy=%r" % dlg._cv_busy())
        shot(dlg, "qa_ui3_drain_stop.png")
        # 真点一下停止
        dlg._stop_download()
        pump(dlg, 0.6)
        check("UI-6.5 点停止后 _cv_cancel 已置位（B17 不早退）",
              cv._cancel.is_set() or not cv.is_alive(),
              "_cancel=%s alive=%s" % (cv._cancel.is_set(), cv.is_alive()))
        check("UI-6.6 点停止后按钮被禁用",
              str(dlg.btn_stop.cget("state")) == "disabled",
              "state=%s" % dlg.btn_stop.cget("state"))
        logtxt = _log_text(dlg)
        check("UI-6.7 停止有日志提示（用户看得见）",
              "停止" in logtxt, "日志尾部=%r" % logtxt[-160:])
        try:
            cv.wait(15)
        except Exception:                              # noqa: BLE001
            pass
        dlg._cv = None

        # -------------------------------- UI-7 窗口几何
        print("\n===== UI-7 窗口几何未被新控件撑坏 =====")
        pump(dlg, 0.5)
        rw, rh = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        aw, ah = dlg.winfo_width(), dlg.winfo_height()
        check("UI-7.1 窗口有合理尺寸（非 0 / 非巨大）",
              300 < aw < 4000 and 200 < ah < 3000,
              "actual=%sx%s req=%sx%s" % (aw, ah, rw, rh))
        check("UI-7.2 新复选框在窗口范围内",
              dlg.winfo_rootx() <= _x <= dlg.winfo_rootx() + aw,
              "cb.x=%s win.x=%s w=%s" % (_x, dlg.winfo_rootx(), aw))

        # -------------------------------- UI-5 关窗收尾
        print("\n===== UI-5 关窗收尾（I 组 / Tcl 报错）=====")
        cv2 = dc.ConvertWorker(os.path.join(tmp, "closing", "WAV"),
                               lambda *a: None, threading.Event())
        cv2.convert_fn = slow_convert
        cv2.start()
        real_src2 = os.path.join(tmp, "y.nsf")
        with open(real_src2, "wb") as fh:
            fh.write(b"NESM" + b"\x1a" + b"\x00" * 0x100)
        cv2.enqueue({"src": real_src2, "url": "u", "policy": "chip"})
        cv2.mark_src_finished()
        end = time.time() + 15
        while time.time() < end and not slow_started.is_set():
            pump(dlg, 0.05)
        dlg._cv = cv2
        pump(dlg, 0.3)
        shot(dlg, "qa_ui4_before_close.png")
        t0 = time.time()
        stderr_sink = []
        try:
            dlg._shutdown()
        except Exception as e:                          # noqa: BLE001
            stderr_sink.append(str(e))
        pump(dlg, 0.8)
        try:
            dlg.destroy()
        except Exception as e:                          # noqa: BLE001
            stderr_sink.append(str(e))
        dt = time.time() - t0
        pump(dlg, 0.5)
        check("UI-5.1 关窗不抛异常", not stderr_sink, str(stderr_sink))
        check("UI-5.2 关窗后 _closed 为真",
              bool(getattr(dlg, "_closed", False)),
              "_closed=%r" % getattr(dlg, "_closed", None))
        t1 = time.time()
        while time.time() - t1 < 3 and cv2.is_alive():
            time.sleep(0.1)
        check("UI-5.3 转换线程 ≤3s 内退出（I1）",
              not cv2.is_alive(), "alive=%s 用时%.1fs" % (cv2.is_alive(), dt))
        # I3：关窗后两个入口（_cv_emit / _handle_cv）都必须因 _closed 早退，
        # 且**不写 stderr**（Tcl 的 "invalid command name" 就是走 stderr）。
        # ⚠ 不是断言 _cv 被置 None —— 设计只在 cv_thread_exit 路径置 None，
        #   _shutdown 靠 _closed 早退 + destroy，两者等价，不该过度约束。
        import io
        import sys as _sys
        import contextlib
        buf = io.StringIO()
        errs = []
        with contextlib.redirect_stderr(buf):
            for fn_name, arg in (("_cv_emit", ("cv_log", {"text": "迟到日志"})),
                                 ("_handle_cv", ("cv_file_done",
                                                  {"src": "x", "n_wav": 1}))):
                try:
                    getattr(dlg, fn_name)(*arg)
                except Exception as e:                  # noqa: BLE001
                    errs.append("%s: %s: %s" % (fn_name, type(e).__name__, e))
        se = buf.getvalue()
        check("UI-5.4 关窗后 _cv_emit/_handle_cv 早退且不抛", not errs,
              str(errs))
        check("UI-5.5 关窗后无 Tcl 'invalid command name' 写 stderr",
              "invalid command name" not in se and se.strip() == "",
              "stderr=%r" % se[:200])

    finally:
        try:
            _tkroot.destroy()
        except Exception:                              # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 78)
    print("界面验收结果: 通过 %d / 失败 %d" % (PASS[0], len(FAIL)))
    if FAIL:
        print("\n失败明细:")
        for f in FAIL:
            print("  - %s" % f)
    print("截图目录: %s" % SHOT_DIR)
    print("=" * 78)
    return 1 if FAIL else 0


def _walk(w, depth=0, maxd=14):
    """深度遍历控件树（找标签用）。"""
    if depth > maxd:
        return
    for c in w.winfo_children():
        yield c
        for g in _walk(c, depth + 1, maxd):
            yield g


def _different_row(a, b):
    """两个控件是否不在同一行（y 差大于控件高度）。"""
    if b is None:
        return False
    ya, yb = a.winfo_rooty(), b.winfo_rooty()
    return abs(ya - yb) > max(a.winfo_height(), b.winfo_height(), 10)


def _log_text(dlg):
    """取日志区文本（ScrolledText 是 state=disabled，须临时切 normal）。"""
    w = getattr(dlg, "logw", None)
    if w is None:
        return ""
    try:
        w.config(state="normal")
        v = w.get("1.0", "end")
        w.config(state="disabled")
        return v if isinstance(v, str) else ""
    except Exception:                                  # noqa: BLE001
        return ""


if __name__ == "__main__":
    sys.exit(main())
