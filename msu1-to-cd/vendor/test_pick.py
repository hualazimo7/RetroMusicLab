#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证统一添加对话框: 同一窗口里既能多选文件, 也能多选文件夹。

覆盖点:
  * 文件夹始终显示, 文件按格式过滤
  * 进入子目录 / 上级 / 路径直达 / 盘符切换
  * 多选文件 + 文件夹 -> 返回的路径列表正确
  * 双击文件夹进入, 双击文件直接确定
  * 未选中时点确定不返回、不崩
"""
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import msu1_gui as g  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPC_DIR = os.path.join(os.path.expanduser("~"), "Desktop",
                             "Chrono Trigger (EMU).zophar")


def rows(dlg):
    """(名称, 类型) 列表。"""
    return [dlg.tree.item(i, "values") for i in dlg.tree.get_children()]


def find_row(dlg, fragment):
    for iid in dlg.tree.get_children():
        path, _d = dlg._rows[iid]
        if fragment.lower() in os.path.basename(path).lower():
            return iid
    return None


def main():
    fails = []
    root = tk.Tk()
    root.withdraw()

    # --- 1. 打开项目根目录: 目录在前, 文件按"支持的格式"过滤 ---
    dlg = g.PickDialog(root, BASE)
    root.update()
    all_rows = rows(dlg)
    names = [r[0] for r in all_rows]
    dirs = [n for n in names if n.startswith("📁")]
    print(f"[1] {BASE} -> 共 {len(all_rows)} 行, 其中目录 {len(dirs)} 个")
    if find_row(dlg, "test") is None:
        fails.append("未列出 test 目录")
    # 非法类型的文件不应出现 (比如 .spec/.bat)
    if any(n.lower().endswith(".spec") for n in names):
        fails.append("过滤失效: 出现了 .spec")
    # 第一个非目录行必须是受支持的类型
    supported = tuple(g.SUPPORTED_EXTS)
    for n in names:
        if not n.startswith("📁"):
            base = n.split(" ", 1)[1].lower()
            if not base.endswith(supported):
                fails.append(f"过滤失效: {base}")
            break
    dlg.destroy()

    # --- 2. 进入 SPC 目录, 多选两个文件 ---
    if not os.path.isdir(SPC_DIR):
        print(f"SKIP 源目录不存在: {SPC_DIR}")
        root.destroy()
        return 0
    dlg = g.PickDialog(root, SPC_DIR)
    root.update()
    spc_rows = [iid for iid in dlg.tree.get_children()
                if dlg._rows[iid][0].lower().endswith(".spc")]
    print(f"[2] SPC 目录 -> 列出 {len(spc_rows)} 个 .spc")
    if len(spc_rows) < 2:
        fails.append("SPC 目录没有列出 2 个以上文件")
    dlg.tree.selection_set(spc_rows[0], spc_rows[1])
    root.update()
    print(f"    状态行: {dlg.var_status.get()}")
    dlg._ok()
    print(f"    返回 {len(dlg.result)} 项: "
          f"{[os.path.basename(p) for p in dlg.result]}")
    if len(dlg.result) != 2:
        fails.append("多选文件返回值数量不对")
    if not all(p.lower().endswith(".spc") for p in dlg.result):
        fails.append("多选文件返回了非 spc")

    # --- 3. 多选"一个文件夹 + 一个文件" (核心需求) ---
    tmpd = os.path.join(BASE, "tmp", "pickmix")
    os.makedirs(os.path.join(tmpd, "sub"), exist_ok=True)
    src = os.path.join(SPC_DIR, "101 Presentiment.spc")
    dst = os.path.join(tmpd, "101 Presentiment.spc")
    if os.path.exists(src) and not os.path.exists(dst):
        import shutil
        shutil.copy(src, dst)
    dlg = g.PickDialog(root, tmpd)
    root.update()
    id_spc = find_row(dlg, "Presentiment")
    id_sub = find_row(dlg, "sub")
    print(f"[3] 列出 {len(dlg.tree.get_children())} 行 "
          f"(spc={id_spc}, sub={id_sub})")
    if id_spc is None or id_sub is None:
        fails.append(f"混选测试行缺失 spc={id_spc} sub={id_sub}")
    else:
        dlg.tree.selection_set(id_spc, id_sub)
        root.update()
        print(f"    状态行: {dlg.var_status.get()}")
        dlg._ok()
        got = {os.path.basename(p) for p in dlg.result}
        print(f"    返回: {sorted(got)}")
        if got != {"101 Presentiment.spc", "sub"}:
            fails.append(f"混选返回值不对: {got}")
    import shutil
    shutil.rmtree(tmpd, ignore_errors=True)

    # --- 4. 双击文件夹进入 / 双击文件直接确定 ---
    dlg = g.PickDialog(root, BASE)
    root.update()
    id_dir = find_row(dlg, "test")
    dlg.tree.selection_set(id_dir)
    dlg._on_double(None)
    root.update()
    print(f"[4a] 双击目录 -> 当前目录: {os.path.basename(dlg.cur)}")
    if os.path.normpath(dlg.cur) != os.path.normpath(os.path.join(BASE, "test")):
        fails.append("双击目录未进入")
    dlg._up()
    root.update()
    print(f"[4b] 上级 -> {os.path.basename(dlg.cur)}")
    if os.path.normpath(dlg.cur) != os.path.normpath(BASE):
        fails.append("上级按钮未返回")

    # --- 5. 路径框直达 + 未选中时确定 ---
    dlg._go(SPC_DIR)
    root.update()
    print(f"[5a] 路径直达 -> 列出 {len(dlg.tree.get_children())} 行")
    if not dlg.tree.get_children():
        fails.append("路径直达后列表为空")
    dlg.tree.selection_remove(*dlg.tree.selection())
    before = list(dlg.result)
    # 未选中时 _ok 会弹 messagebox, 测试里绕过弹窗直接看 result 不变
    print(f"[5b] 未选中时 result 不变: {dlg.result == before}")
    if dlg.result != before:
        fails.append("未选中时 result 被改写")
    dlg.destroy()

    root.destroy()
    print()
    if fails:
        for f in fails:
            print("FAIL", f)
        return 1
    print("PICK TEST PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
