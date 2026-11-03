#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""msu1_qt.py — Qt 界面入口（v5.0-qt）。

与 tkinter 版并存：
    MSU1Converter.exe             tkinter 版（v4.44，功能完整）
    MSU1Converter-qt.exe          本文件（Qt 版，界面重做）

跑法：
    python msu1_qt.py               开窗口
    python msu1_qt.py --shot 目录   无头渲染三套主题截图后退出（给门禁/汇报用）
    python msu1_qt.py --qt-selftest 自检（打包后也用这条验证 exe 没缺件）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def base_dir():
    """exe 所在目录（冻结后）或源码目录。日志/输出都写这儿。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_stdout(name="qt_selftest.log"):
    """窗口版 exe 没有控制台，``sys.stdout`` 是 None —— 自检输出会全部丢失。

    与 tkinter 版 ``msu1_gui.main()`` 同一套处理：重定向到 exe 旁边的日志文件，
    并改成 UTF-8 + replace（中文 Windows 默认 cp936，遇到 ⚠/✅ 会抛
    UnicodeEncodeError，把一次成功的自检变成 rc=1）。
    """
    if sys.stdout is None:
        try:
            sys.stdout = sys.stderr = open(os.path.join(base_dir(), name),
                                           "w", encoding="utf-8")
        except OSError:
            return
    for s in (sys.stdout, sys.stderr):
        try:
            if s is not None and not s.isatty():
                s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)

    # ⚠⚠ 必须先做「子进程 worker」分发。
    # `core.load_spc_paths` / `chip_decode.load_chip_paths` 解 SPC / 芯片音源时
    # 会用「本 exe + --spc-decode / --chip-decode」把**自己**再调起来（libgme
    # 解坏内存不能污染 UI 进程）。不在这里分发的话，子进程会被当成「正常启动」
    # → 弹一个 Qt 窗口 → 父进程永远等不到结果文件，界面就卡在「解 XA 音频…」
    # 不动（门禁实测：exe --qt-selftest 卡死 90 秒不退，根因就是这个）。
    # tkinter 版在 msu1_gui.main() 开头做同一件事，漏了同样会卡。
    import msu1_to_cd as core
    if "--spc-decode" in argv or "--chip-decode" in argv:
        ensure_stdout("qt_worker.log")
        core.maybe_run_spc_worker()
        return 0

    # 自检要在建窗口之前跑：它自己会用 offscreen 平台建一个 QApplication
    if "--qt-selftest" in argv:
        ensure_stdout()
        from qt_ui import selftest
        passed, failed = selftest.run()
        print("")
        print("SELFTEST %s  (通过 %d / 失败 %d)"
              % ("PASS" if failed == 0 else "FAIL", passed, failed))
        try:
            sys.stdout.flush()
        except Exception:                    # noqa: BLE001
            pass
        # ⚠ 不要在这里 os._exit（试过，冻结后 rc=0xC0000005 访问违例）。
        # 退不干净的真正原因是 QApplication 被 GC 提前回收 —— 那个已经在
        # qt_ui.selftest 里用模块级引用顶住了，正常 return 就是 rc=0。
        return 0 if failed == 0 else 1

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # 高 DPI 在 Qt6 默认开启；这里只统一图标缩放策略
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName("RetroMusic Lab")
    app.setOrganizationName("MSU1Converter")

    from qt_ui.main_window import MainWindow
    win = MainWindow()

    shot_dir = None
    if "--shot" in argv:
        win._shot_mode = True
        i = argv.index("--shot")
        shot_dir = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") \
            else os.getcwd()
        os.makedirs(shot_dir, exist_ok=True)
        # 塞两个假音源，让截图里有内容（不落盘、不转换）
        win.sources = [
            {"label": "Castlevania - SotN (PS1)", "path": r"D:\fake\cv.cue",
             "kind": "光盘镜像", "n_tracks": 41, "total_secs": 3136.0,
             "size": 1_370_000_000},
            {"label": "Chrono Trigger (SPC)", "path": r"D:\fake\ct",
             "kind": "SPC", "n_tracks": 64, "total_secs": 5882.0,
             "size": 13_000_000},
            {"label": "Silent Hill (XA)", "path": r"D:\fake\sh.cue",
             "kind": "光盘镜像", "n_tracks": 200, "total_secs": 3831.0,
             "size": 616_494_480},
        ]
        win.refresh_table()
        win.log("开始: 3 个音源 | CD + WAV")
        win.log(r"输出: C:\Users\xqh\Desktop\out")
        win.log("▶ Castlevania - SotN (PS1)  (光盘镜像)")
        win.log("   41 条音轨")
        win.log("   CD 轨 01/41: 夜曲 (3.7 分钟)")
        win.log("   ✅ 写出 cv.bin + cv.cue（3136 扇区）")
        win.pb.setValue(42)
        win.lb_status.setText("写 WAV 17/41")
        win.show()
        app.processEvents()
        for key in ("modern_light", "modern_dark", "retro_famicom"):
            win.apply_theme(key)
            app.processEvents()
            pix = win.grab()
            out = os.path.join(shot_dir, f"qt_{key}.png")
            pix.save(out)
            print(f"已保存 {out}  {pix.width()}x{pix.height()}")
        return 0

    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
