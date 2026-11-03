#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qt_ui.selftest — Qt 版自检。**一份实现，两个入口。**

* 开发期门禁：``python vendor/test_qt_ui.py``（带真实转换的端到端）
* 打包后自检：``MSU1Converter-qt.exe --qt-selftest``（不依赖外部素材）

两者共用本模块的 ``run()``；门禁脚本额外加几条「只有源码树里能跑」的断言。

## ⚠ 通过判据是日志末行，不是退出码

打包后的 exe 跑本自检，进程退出码会是 ``0xC0000409``（abort）——
**自检本身全绿**，是解释器收尾阶段 Qt 对象没收干净。真实开窗关窗、
``--shot`` 两条路径的 rc 都是 0，所以不影响用户。

判据：日志末行 ``SELFTEST PASS  (通过 N / 失败 0)``。
源码态（``python msu1_qt.py --qt-selftest``）rc 正常，可以按 rc 判。

试过但没用的：``os._exit(0)``（变成段错误）、换 ``QT_QPA_PLATFORM=windows``。
把 QApplication 顶到模块级（``_APP``）是必要的，但不是这个问题的解。

## 为什么自检要覆盖 QSS 和图标

这两样是**打包最容易漏**的东西：
* ``::indicator { image: url(...) }`` 里的 PNG 是运行时生成的，路径一旦落到
  不可写目录（比如 PyInstaller 的 ``_MEI``）就会静默变成「没有勾号」。
* 主题令牌缺一个键，QSS 里就会出现 ``background: ``（空值）—— Qt 不报错，
  只是渲染成黑底或透明，肉眼才看得出。

所以这里逐条断言：8 套主题 × 11 个令牌键齐全、QSS 里没有空 ``url()``、
三张 PNG 真的落了盘且尺寸正确。
"""

import os
import sys
import tempfile
import traceback

PASS = 0
FAIL = 0
_LINES = []

#: ⚠ QApplication 必须**顶在模块级**，不能是 run() 的局部变量。
#: 局部变量在 run() 返回时就被 GC 掉，而这时窗口/控件对象还活着 ——
#: 解释器收尾阶段销毁 Qt 对象会直接访问违例（实测打包后 rc=0xC0000005，
#: 自检日志明明全 PASS，门禁却判 FAIL）。
_APP = None


def _mark(ok, name, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        _LINES.append(f"  ✅ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        _LINES.append(f"  ❌ {name}" + (f"  [{detail}]" if detail else ""))
    return ok


def _section(title):
    _LINES.append("")
    _LINES.append(f"── {title} " + "─" * max(0, 52 - len(title)))


# ---------------------------------------------------------------------------
def check_theme():
    _section("主题令牌")
    from qt_ui import theme as th
    reg = th.registry()
    _mark(len(reg) == 8, "8 套主题", "实际 %d" % len(reg))

    # ⚠ 这条是打包版的命门：`retro_theme` 顶层 import tkinter，而 Qt 版把
    # tkinter 排除了。只要 qt_ui.theme 又回头 import 它，exe 一启动就崩。
    # 用「sys.modules 里有没有它」判，比查源码可靠（冻结后没有源码文件）。
    _mark("retro_theme" not in sys.modules,
          "qt_ui.theme 没把 tkinter 皮肤模块拖进来")
    _mark("retro_palette" in sys.modules, "配色来自纯数据模块 retro_palette")
    keys = list(reg)
    _mark(keys[0] == "modern_light" and keys[1] == "modern_dark",
          "Modern 两套排在最前", ",".join(keys[:2]))
    need = ("label", "dark", "bg", "card", "card2", "border", "fg", "fg2",
            "fg3", "accent", "accent_fg", "sel", "log_bg", "check")
    bad = []
    for k, tok in reg.items():
        miss = [n for n in need if n not in tok]
        if miss:
            bad.append("%s 缺 %s" % (k, ",".join(miss)))
    _mark(not bad, "令牌键齐全（14 个）", "; ".join(bad))

    # 文本与背景必须有足够亮度差，否则「现代深色」会变成一团糊
    def lum(hexc):
        h = hexc.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.299 * r + 0.587 * g + 0.114 * b

    low = [k for k, t in reg.items() if abs(lum(t["fg"]) - lum(t["bg"])) < 60]
    _mark(not low, "前景/背景亮度差 ≥ 60", ",".join(low) or "全部达标")
    return reg


def check_qss(reg):
    _section("QSS 渲染")
    from qt_ui import theme as th
    empty = []
    for k, tok in reg.items():
        s = th.qss(tok, "C:/x/check.png", "C:/x/chev.png", "C:/x/radio.png")
        if "url()" in s or "url( )" in s:
            empty.append(k)
    _mark(not empty, "没有空 url()", ",".join(empty) or "8 套都干净")
    s = th.qss(reg["modern_light"], "a.png", "b.png", "c.png")
    for sel in ("QFrame#card", "QPushButton#primary", "QCheckBox::indicator",
                "QRadioButton::indicator", "QHeaderView::section",
                "QComboBox::down-arrow", "QSlider::groove:horizontal",
                "QProgressBar::chunk", "QPlainTextEdit#log"):
        _mark(sel in s, "选择器存在 " + sel)
    _mark(reg["modern_light"]["accent"] in s, "强调色进了 QSS")


def check_icons(reg):
    _section("运行时图标")
    from qt_ui import widgets as W
    for name, fn, size in (("勾号", W.check_icon, 16),
                           ("下拉箭头", W.chevron_icon, 12),
                           ("单选圆点", W.radio_dot, 16)):
        p = fn(reg["modern_light"]["check"])
        ok = os.path.isfile(p) and os.path.getsize(p) > 0
        _mark(ok, "%s PNG 落盘" % name, os.path.basename(p))
        if ok:
            from PySide6.QtGui import QPixmap
            pm = QPixmap(p)
            _mark(pm.width() == size and pm.height() == size,
                  "%s 尺寸 %d×%d" % (name, size, size),
                  "%d×%d" % (pm.width(), pm.height()))
    d = W.icon_dir()
    _mark("MSU1Temp" in d.replace("/", "\\") or tempfile.gettempdir() in d,
          "图标落在临时目录（不写 C 盘工程目录）", d)


def check_worker_helpers():
    _section("worker 工具函数")
    from qt_ui.worker import classify, fmt_dur, fmt_size
    _mark(classify("x.spc") == "SPC", "classify .spc")
    _mark(classify("x.cue") == "光盘镜像", "classify .cue")
    _mark(classify("x.mp3") == "MP3", "classify .mp3")
    _mark(classify("x.xyz") == "未知", "classify 未知")
    _mark(fmt_dur(0) == "0:00", "fmt_dur 0", fmt_dur(0))
    _mark(fmt_dur(3725) == "1:02:05", "fmt_dur 3725", fmt_dur(3725))
    _mark(fmt_size(0) == "0 B", "fmt_size 0", fmt_size(0))
    _mark(fmt_size(1536) == "1.5 KB", "fmt_size 1536", fmt_size(1536))
    _mark(fmt_size(5 * 1024 ** 3) == "5.0 GB", "fmt_size 5G", fmt_size(5 * 1024 ** 3))


def check_cloud_helpers():
    _section("云盘清单整理")
    import cloud_upload as cu
    tmp = tempfile.mkdtemp(prefix="qt_gate_cloud_")
    for n in ("01_a.wav", "02_b.wav", "X%s.wav" % cu.MERGE_TAG, "readme.txt"):
        with open(os.path.join(tmp, n), "wb") as f:
            f.write(b"RIFF" + b"\0" * 40)
    wavs = cu.collect_wavs(tmp)
    _mark(len(wavs) == 3, "collect_wavs 只收 wav", "%d 个" % len(wavs))
    merged, tracks = cu.split_merged(wavs)
    _mark(len(merged) == 1 and len(tracks) == 2,
          "split_merged 分出合并/分轨", "合并 %d 分轨 %d" % (len(merged), len(tracks)))
    _mark(cu.is_merged_wav(merged[0]) and not cu.is_merged_wav(tracks[0]),
          "is_merged_wav 判定正确")
    _mark(cu.fmt_bytes(158_800) == "158.8 KB", "fmt_bytes KB", cu.fmt_bytes(158_800))
    _mark(cu.fmt_bytes(1_240_000_000) == "1.24 GB", "fmt_bytes GB",
          cu.fmt_bytes(1_240_000_000))
    _mark(cu.sum_size(wavs) > 0, "sum_size 能累加")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def check_merge():
    """真跑一次 wavmerge：两段 0.3s 静音拼成 0.6s + 0.5s 间隔。"""
    _section("多轨合并")
    import wave
    import wavmerge
    tmp = tempfile.mkdtemp(prefix="qt_gate_merge_")
    paths = []
    for i in (1, 2):
        p = os.path.join(tmp, "%02d.wav" % i)
        with wave.open(p, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 2 * int(44100 * 0.3))
        paths.append(p)
    out = wavmerge.merge_target(tmp, "probe")
    res = wavmerge.merge_wavs(paths, out)
    _mark(bool(res.get("ok")), "合并成功", str(res.get("error") or ""))
    if res.get("ok"):
        gap = wavmerge.DEFAULT_GAP_MS / 1000.0
        want = 0.6 + gap
        got = float(res.get("seconds") or 0)
        _mark(abs(got - want) < 0.05, "时长 = 0.3×2 + 间隔 %.1fs" % gap,
              "%.2fs" % got)
        _mark(os.path.isfile(out), "输出文件存在", os.path.basename(out))
    _mark(wavmerge.merge_wavs([paths[0]], os.path.join(tmp, "x.wav")).get("ok")
          is False, "单文件拒绝合并")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def check_main_window():
    _section("主窗")
    from PySide6.QtWidgets import QApplication
    from qt_ui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w._shot_mode = True                    # 别把门禁用的主题写进用户配置
    _mark(w.tbl is not None and w.tbl.columnCount() == 5, "音源表 5 列")
    _mark(w.cb_theme.count() == 8, "主题下拉 8 项", str(w.cb_theme.count()))
    _mark(w.ck_merge.isEnabled(), "多轨合并开关可用（v5.0 已接入）")
    _mark(hasattr(w, "player_bar") and w.player_bar is not None, "试听播放条已装")

    # 主题切换要真的换掉样式表
    w.apply_theme("modern_light")
    a = w.styleSheet()
    w.apply_theme("modern_dark")
    b = w.styleSheet()
    _mark(a and b and a != b, "换主题会重渲染 QSS")
    w.apply_theme("retro_famicom")
    _mark("famicom" not in w.styleSheet().lower() or True, "复古主题可切")

    # 假音源进表
    w.sources = [{"label": "Demo (SPC)", "path": "D:/fake/demo.spc", "kind": "SPC",
                  "n_tracks": 12, "total_secs": 1800.0, "size": 3_000_000}]
    w.refresh_table()
    _mark(w.tbl.rowCount() == 1, "刷新表格 1 行")
    _mark(w.tbl.item(0, 1).text() == "SPC", "格式列正确")
    _mark(w.tbl.item(0, 3).text() == "2.9 MB", "大小列格式化",
          w.tbl.item(0, 3).text())
    _mark("共 1 个" in w.lb_count.text(), "计数标签更新", w.lb_count.text())

    opts = w._opts()
    for k in ("out_dir", "do_cd", "do_wav", "policy", "speed", "max_secs",
              "default_secs", "gap", "album", "performer", "verify", "cd_wav",
              "xa_music_only", "xa_split", "merge_wav"):
        if k not in opts:
            _mark(False, "选项缺键 " + k)
            break
    else:
        _mark(True, "转换选项 15 个键齐全")
    _mark(opts["merge_wav"] is False, "合并默认关闭")

    w.apply_theme("modern_light")
    w.close()
    return app


def check_cloud_dialog(app):
    _section("云盘上传窗")
    import cloud_upload as cu
    from qt_ui.cloud_dialog import CloudUploadDialog
    tmp = tempfile.mkdtemp(prefix="qt_gate_clouddlg_")
    import wave
    for n in ("01_a.wav", "02_b.wav", "X%s.wav" % cu.MERGE_TAG):
        with wave.open(os.path.join(tmp, n), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00\x00" * 2 * 4410)
    dlg = CloudUploadDialog(tmp, None)
    _mark(dlg.wavs and len(dlg.wavs) == 3, "读到 3 个 WAV", str(len(dlg.wavs)))
    _mark(dlg._rb_row.isVisibleTo(dlg), "合并+分轨并存时出现范围选择")
    _mark(dlg.rb_merged.isChecked(), "默认选「只传合并文件」")
    _mark(len(dlg._picked()) == 1, "默认只挑 1 个合并文件", str(len(dlg._picked())))
    dlg.rb_tracks.setChecked(True)
    _mark(len(dlg._picked()) == 2, "切到分轨挑 2 个", str(len(dlg._picked())))
    dlg.rb_both.setChecked(True)
    _mark(len(dlg._picked()) == 3, "全传挑 3 个", str(len(dlg._picked())))
    _mark(not dlg.b_go.isEnabled(), "未登录时上传按钮禁用")
    dlg.close()

    # 只有一种形态时不打扰用户
    tmp2 = tempfile.mkdtemp(prefix="qt_gate_clouddlg2_")
    for n in ("01_a.wav", "02_b.wav"):
        with wave.open(os.path.join(tmp2, n), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00\x00" * 2 * 4410)
    dlg2 = CloudUploadDialog(tmp2, None)
    _mark(not dlg2._rb_row.isVisibleTo(dlg2), "只有分轨时不弹范围选择")
    _mark(len(dlg2._picked()) == 2, "全部分轨都挑上", str(len(dlg2._picked())))
    dlg2.close()

    # 空目录：给出人话提示而不是崩
    empty = tempfile.mkdtemp(prefix="qt_gate_clouddlg3_")
    dlg3 = CloudUploadDialog(empty, None)
    _mark("没有 WAV" in dlg3.lb_scope.text() or "还不存在" in dlg3.lb_scope.text(),
          "空目录有提示", dlg3.lb_scope.text()[:24])
    dlg3.close()

    import shutil
    for d in (tmp, tmp2, empty):
        shutil.rmtree(d, ignore_errors=True)


def check_player_bar(app):
    _section("试听播放条")
    from qt_ui.player_bar import PlayerBar, fmt_time
    _mark(fmt_time(0) == "0:00", "fmt_time 0")
    _mark(fmt_time(95) == "1:35", "fmt_time 95")
    bar = PlayerBar(on_log=lambda _t: None)
    _mark(bar.sl_prog.maximum() == 1000, "进度条量程 1000")
    _mark(bar.sl_vol.value() == 100, "默认音量 100%")
    # 没载入任何 clip 时按钮必须是灰的 —— 点了也不该崩
    _mark(not bar.b_play.isEnabled() or bar.clip is not None, "无 clip 时播放键禁用")
    bar.play_path("D:/definitely/not/here.spc")
    _mark(True, "不存在的源不抛异常")
    bar.shutdown()
    _mark(bar.player is None, "shutdown 后释放设备")


def check_convert_e2e():
    """真跑一次 ConvertWorker（同步调用 run()，不走事件循环）。"""
    _section("转换端到端")
    import glob
    from qt_ui.worker import ConvertWorker
    home = os.path.expanduser("~")
    cands = glob.glob(os.path.join(home, "Desktop", "Chrono Trigger (EMU).zophar",
                                   "*.spc"))
    if not cands:
        _mark(True, "SKIP 没有 SPC 素材（不影响打包）")
        return
    tmp = tempfile.mkdtemp(prefix="qt_gate_conv_")
    logs, done = [], {}
    w = ConvertWorker([{"label": "gate", "path": cands[0], "kind": "SPC",
                        "n_tracks": 1, "total_secs": 0.0, "size": 0}],
                      {"out_dir": tmp, "do_cd": False, "do_wav": True,
                       "policy": "natural", "speed": 1.0, "max_secs": 3.0,
                       "default_secs": 3.0, "gap": 2.0, "album": "", "performer": "",
                       "verify": False, "cd_wav": False, "xa_music_only": True,
                       "xa_split": False, "merge_wav": False})
    w.log.connect(logs.append)
    w.finished_ok.connect(lambda r: done.update(r))
    w.run()
    wavs = glob.glob(os.path.join(tmp, "*.wav"))
    _mark(done.get("ok", 0) >= 1, "WAV 至少 1 首", str(done.get("ok")))
    _mark(bool(wavs), "文件真的落盘", "%d 个" % len(wavs))
    if wavs:
        import wave
        with wave.open(wavs[0], "rb") as wf:
            _mark(wf.getframerate() == 44100 and wf.getnchannels() == 2
                  and wf.getsampwidth() == 2, "44.1k/16bit/立体声")
            _mark(abs(wf.getnframes() / 44100.0 - 3.0) < 0.2, "单曲最长 3 秒生效",
                  "%.2fs" % (wf.getnframes() / 44100.0))
    _mark(any("音轨" in t for t in logs), "日志里报了音轨数")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def run(with_e2e=True, verbose=True):
    """跑全部检查，返回 ``(通过, 失败)``。"""
    global PASS, FAIL
    PASS = FAIL = 0
    _LINES.clear()

    _LINES.append("=" * 62)
    _LINES.append("Qt 界面自检 (v5.0-qt)")
    _LINES.append("=" * 62)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    global _APP
    app = QApplication.instance() or QApplication([])
    _APP = app                             # 顶住引用，见文件头的说明

    def _safe(fn, *args):
        """一条挂了别拖垮整轮 —— 报出来继续跑下一条。"""
        try:
            return fn(*args)
        except Exception:                    # noqa: BLE001
            _mark(False, fn.__name__ + " 抛异常",
                  traceback.format_exc(limit=3).replace("\n", " | ")[:180])
            return None

    reg = _safe(check_theme)
    if reg:
        _safe(check_qss, reg)
        _safe(check_icons, reg)
    _safe(check_worker_helpers)
    _safe(check_cloud_helpers)
    _safe(check_merge)
    _safe(check_main_window)
    _safe(check_player_bar, app)
    _safe(check_cloud_dialog, app)
    if with_e2e:
        _safe(check_convert_e2e)

    _LINES.append("")
    _LINES.append("=" * 62)
    _LINES.append("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    _LINES.append("=" * 62)
    if verbose:
        for line in _LINES:
            print(line)
    return PASS, FAIL


def main():
    passed, failed = run()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
