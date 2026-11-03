# -*- coding: utf-8 -*-
"""临时截图脚本：逐套主题抓主窗（+ 下载窗），用于肉眼验收 v4.18 换肤。

关键验证点：
  1. 启动时能读到上次选择的主题（写 ui_theme.json → 建 App → 看 _theme_key）；
  2. 运行中经下拉框切换能真的换色（不是只改常量）；
  3. 横幅标题是 RetroMusic Lab + 等宽字体，换主题后颜色跟着变。

用法：python _shot_themes.py [all|quick]
"""
import os
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.join(HERE, "msu1-to-cd")
sys.path.insert(0, HERE)

from _shot_theme import shot  # noqa: E402  复用屏幕 BitBlt + 手写 PNG


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    sys.path.insert(0, PROJ)
    os.chdir(PROJ)
    sys.argv = ["msu1_gui.py", "--no-drop"]
    import msu1_gui as g
    import retro_theme as rt

    # ---- 1) 启动路径：预置主题文件，确认 App 读到了 ----
    g.save_theme_key("nes")
    root = tk.Tk()
    app = g.App(root)
    root.update()
    root.lift()
    root.attributes("-topmost", True)
    time.sleep(0.9)
    root.update()
    got = getattr(app, "_theme_key", None)
    print("  启动读到主题:", got, "| 标签:", rt.theme_label(got))
    assert got == "nes", "启动没读回 ui_theme.json 里的 nes"
    shot(root, "ui_theme_nes.png")

    # ---- 2) 运行中切换：走下拉框 → _on_theme_pick ----
    order = [k for k in rt.theme_keys() if k != "nes"]
    if mode == "quick":
        order = order[:1]
    for key in order:
        app.var_theme.set(rt.theme_label(key))
        app._on_theme_pick()
        root.update()
        time.sleep(0.6)
        root.update()
        print("  切到 %-10s bg=%s cyan=%s" % (key, rt.PALETTE["bg"], rt.PALETTE["cyan"]))
        shot(root, "ui_theme_%s.png" % key)

    # ---- 3) 下载窗也换一套看看（三列 + 60px 横幅）----
    app.var_theme.set(rt.theme_label("gameboy"))
    app._on_theme_pick()
    app.open_download()
    dlg = getattr(app, "_dlg", None)
    if dlg is None:
        print("  !! 下载窗没开起来")
    else:
        dlg.update()
        dlg.attributes("-topmost", True)
        try:
            dlg._load_platform("spc")
        except Exception as exc:                          # noqa: BLE001
            print("  装载 spc 列表失败:", exc)
        time.sleep(0.9)
        dlg.update()
        shot(dlg, "ui_theme_download_gameboy.png")
        # 下载窗开着时再切主题（验证它的 skin 钩子）
        app.var_theme.set(rt.theme_label("sfc"))
        app._on_theme_pick()
        dlg.update()
        time.sleep(0.6)
        dlg.update()
        shot(dlg, "ui_theme_download_sfc.png")
        dlg._shutdown()

    root.attributes("-topmost", False)
    try:
        app._on_close()
    except Exception:                                     # noqa: BLE001
        pass
    g.save_theme_key("famicom")   # 恢复默认，别把测试偏好留给用户
    print("SHOT OK")


if __name__ == "__main__":
    main()
