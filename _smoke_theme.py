# -*- coding: utf-8 -*-
"""临时冒烟：带音源的窗口逐套切主题，确认列表 tag / 日志 / 播放条都不炸。

不截图，只看断言与异常。
"""
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.join(HERE, "msu1-to-cd")
sys.path.insert(0, PROJ)
os.chdir(PROJ)


def main():
    sys.argv = ["msu1_gui.py", "--no-drop"]
    import msu1_gui as g
    import retro_theme as rt

    failed = []

    def check(cond, msg):
        print(("  PASS " if cond else "  FAIL ") + msg)
        if not cond:
            failed.append(msg)

    root = tk.Tk()
    app = g.App(root)
    root.update()

    # 加两个真音源（一个 SPC、一个多轨 NSF），让列表里有行、有 tag
    srcs = [p for p in ("vendor/sample.spc", "vendor/sample.nsf") if os.path.exists(p)]
    check(bool(srcs), "找到样本音源 %s" % srcs)
    if srcs:
        app.add_paths(srcs)
        root.update()
    rows = len(app.src_tree.get_children()) if app.src_tree else 0
    check(rows >= len(srcs), "列表有 %d 行" % rows)

    # 逐套切主题
    for key in rt.theme_keys():
        app.var_theme.set(rt.theme_label(key))
        app._on_theme_pick()
        root.update()
        tree_bg = app.style.lookup("Treeview", "background")
        log_bg = app.logw.cget("bg")
        # 关键：列表用 current 主题的 card 色，日志底色也跟着变
        check(tree_bg == rt.PALETTE["card"],
              "%s 列表底色 %s == card %s" % (key, tree_bg, rt.PALETTE["card"]))
        check(log_bg == g.log_colors()[0],
              "%s 日志底色 %s" % (key, log_bg))
        check(app.banner.winfo_exists() == 1, "%s 横幅还活着" % key)
        # tag 颜色也要跟着换（红白机的 amber 与 NES 的 amber 不同）
        # tag_configure(tag, "foreground") 返回的是 Tk 的
        # (option, dbName, dbClass, value) 四元组，取最后一项才是颜色
        try:
            raw = app.src_tree.tag_configure("warn", "foreground")
        except tk.TclError:
            raw = None
        if isinstance(raw, tuple):
            raw = raw[-1]
        check(str(raw) == str(rt.PALETTE["amber"]),
              "%s warn tag = %s (期望 %s)" % (key, raw, rt.PALETTE["amber"]))

    # 换主题后再切回，确认没有累积副作用
    rt.apply_theme("famicom")
    app.var_theme.set(rt.theme_label("famicom"))
    app._on_theme_pick()
    root.update()
    check(rt.current_theme() == "famicom", "能切回红白机")

    # 落盘的偏好应该是最新一次选择
    check(g.load_theme_key() == rt.current_theme(),
          "偏好已落盘 = %s" % g.load_theme_key())

    app._on_close()
    g.save_theme_key(rt.DEFAULT_THEME)
    print("\n结果: %d 项失败" % len(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
