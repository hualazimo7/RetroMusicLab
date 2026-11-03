# -*- coding: utf-8 -*-
"""功能冒烟（v4.32 更新）：通过换肤后的控件跑一遍真实交互，确认零功能回归。

⚠ 历史包袱：本脚本写于 v3（列表还是 `tk.Listbox`），v4.8 起主窗改成
`ttk.Treeview`（`app.tree`）、v4.28 起主按钮改 `Hero.TButton`，脚本没跟上就
**一直是坏的门禁**（MEMORY 里记过「_smoke_ui.py 已过期，别当门禁」）。
2026-10-20 按当前 API 重写：不再硬编码索引条数，改「结构断言」。

跑法：python _smoke_ui.py        （会真开窗口，约 3~5 秒）
"""
import os
import sys
import tkinter as tk

PROJ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "msu1-to-cd")
sys.path.insert(0, PROJ)
os.chdir(PROJ)
sys.argv = ["msu1_gui.py", "--no-drop"]

import msu1_gui as g  # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + f"{label}: {got!r}"
          + ("" if ok else f" (want {want!r})"), flush=True)
    if not ok:
        FAIL.append(label)


def check_true(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f": {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAIL.append(label)


def rows(app):
    """主窗音源行数。

    ⚠ 主窗有**两种列表模式**（`App.LIST_MODE`，默认 `"tree"`）：
    tree 模式下控件是 `app.src_tree`（**不是** `app.tree` —— 那是下载窗的），
    list 模式回退到 `app.listbox`。两种都要能数。
    """
    if getattr(app, "src_tree", None) is not None:
        return len(app.src_tree.get_children())
    return app.listbox.size()


def select_first(app):
    """选中第一行（tree 模式按 iid，list 模式按下标）。"""
    if getattr(app, "src_tree", None) is not None:
        app.src_tree.selection_set(app.sources[0]["iid"])
    else:
        app.listbox.selection_set(0)


root = tk.Tk()
app = g.App(root)
root.update_idletasks()

# ① 列表 + 计数
sample = os.path.join(PROJ, "test")
check("add_source", app.add_source(sample, "MSU-1", "♪"), True)
check("add_source dedup", app.add_source(sample, "MSU-1", "♪"), False)
check("tree rows", rows(app), 1)
check("sources len", len(app.sources), 1)
app._refresh_info()
check_true("var_info", "1" in app.var_info.get(), app.var_info.get())
app.log("冒烟: 日志写入正常")
check("log tail", app.logw.get("end-2l", "end-1c").strip(), "冒烟: 日志写入正常")

# 选中/移除/清空（选中走列表抽象层，不直接假设 Listbox 下标）
select_first(app)
app.remove_selected()
check("remove_selected", (len(app.sources), rows(app)), (0, 0))
app.add_source(sample, "MSU-1", "♪")
app.clear_sources()
check("clear_sources", (len(app.sources), rows(app)), (0, 0))

# 选项变量 -> 预估
check("var_pct", app.var_pct.get(), "0%")
check_true("status 就绪", app.var_status.get().startswith("就绪"), app.var_status.get())
app.var_max.set("30")
app.var_speed.set("110")
check("_max_secs", app._max_secs(), 30.0)
check("_speed", round(app._speed(), 3), 1.1)

# 按钮命令仍绑定
check("btn_start cmd", bool(app.btn_start.cget("command")), True)
check("btn_cancel state", str(app.btn_cancel.cget("state")), "disabled")
# v4.28 起主操作是 Hero.TButton（一屏只有一个）
check("btn_start style", str(app.btn_start.cget("style")), "Hero.TButton")

# 进度条
app._set_progress(42.0, "冒烟 42%")
root.update_idletasks()
check("progress value", int(float(app.progress.cget("value"))), 42)
check("pct label", app.var_pct.get(), "42%")

# 下载窗口（索引条数不写死：断言"表格行数 == entries 数"）
app.open_download()
dlg = app._dlg
dlg._load_platform("spc")          # 选中机种才会填表（内置索引，同步）
dlg.update_idletasks()
check_true("dlg banner exists", bool(dlg.banner.winfo_exists()))
n = len(dlg.entries)
check_true("dlg entries > 0", n > 0, f"{n} 条")
check("dlg rows == entries", len(dlg.tree.get_children()), n)
dlg._select_all()
check("dlg select all", len(dlg.sel), n)
check_true("dlg count text", f"{n:,}" in dlg.var_count.get(), dlg.var_count.get())
dlg._invert_selection()
check("dlg invert", len(dlg.sel), 0)
dlg.var_search.set("super mario")
dlg._run_filter()
check_true("dlg filter < all", len(dlg.visible) < n, f"{len(dlg.visible)} / {n}")
check("dlg entry btn style", str(dlg.btn_start.cget("style")), "Hero.TButton")
check("dlg stop style", str(dlg.btn_stop.cget("style")), "Small.TButton")

dlg._shutdown()
check("dlg destroyed", bool(dlg.winfo_exists()), False)
app._on_close()
print("SMOKE " + ("PASS" if not FAIL else "FAIL: " + ", ".join(FAIL)), flush=True)
sys.exit(0 if not FAIL else 1)
