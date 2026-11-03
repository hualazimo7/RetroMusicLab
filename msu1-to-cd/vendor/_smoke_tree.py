#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 源码模式回归自检 —— T03 界面线(Treeview + ③ 区 + CD 跳过)。

用法(需要带 tkinter 的解释器, 例如 `py -3`):

    py -3 vendor/_smoke_tree.py [样本根目录]

样本根目录默认 ``C:\\temp\\gmev2``, 里面应含 nsf/ gbs/ smd/ hes/ 子目录。
chip_formats / chip_decode 就位时跑**多格式**断言; 缺失时自动降级成
"只验 SPC 老路径 + 降级不崩"。两条路径都必须全绿。
"""

import os
import shutil
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chip_formats as cf         # noqa: E402
import msu1_gui as gui           # noqa: E402
import spc_wav                   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(HERE, "_tree_scratch")
FAILS = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL {label}")


def tcl_str(value):
    """ttk 的 cget 返回 Tcl_Obj 包装, 统一取出文本。"""
    if isinstance(value, (tuple, list)):
        value = value[0] if value else ""
    return str(value)


def pick(sub, ext, n=1):
    """样本子目录里取 n 个指定扩展名的文件。"""
    d = os.path.join(SRCROOT, sub)
    try:
        names = sorted(f for f in os.listdir(d) if f.lower().endswith(ext))
    except OSError:
        return []
    return [os.path.join(d, x) for x in names[:n]]


def make_spc_dir(name, n=2):
    d = os.path.join(SCRATCH, name)
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        shutil.copy2(SAMPLE_SPC, os.path.join(d, f"song{i + 1}.spc"))
    return d


SRCROOT = ""
SAMPLE_SPC = ""


def rollback_checks(app, make_spc_dir, samples):
    """LIST_MODE="list" 时的回滚路径验证(设计 §8.2)。

    回滚只该换掉"列表怎么渲染", self.sources / add_paths / _refresh_info /
    _estimate / ConvertWorker 一行都不动, 所以这里复用同一批断言, 只是把
    "Treeview 的行"换成"Listbox 的行"。
    """
    print("== R. LIST_MODE=list 回滚路径 ==")
    check(app.src_tree is None, "回滚模式下 src_tree 为 None")
    check(app.listbox is not None, "回滚模式下 Listbox 已创建")
    d = make_spc_dir("rb", 2)
    app.add_paths([d])
    check(len(app.sources) == 1, "回滚: SPC 文件夹加入 1 项")
    check(app.listbox.size() == 1, f"回滚: Listbox 有 1 行({app.listbox.size()})")
    text = app.listbox.get(0)
    check(text.endswith("(SPC)"), f"回滚: 行文本保留旧格式 {text!r}")
    # 加 4 个目录后删中间一个, 验证下标没漂
    for i in range(4):
        app.add_paths([make_spc_dir(f"rb{i}", 1)])
    total = len(app.sources)
    app.listbox.selection_clear(0, "end")
    app.listbox.selection_set(1)
    app.remove_selected()
    check(len(app.sources) == total - 1, "回滚: 删中间行数量正确")
    check(app.listbox.size() == total - 1, "回滚: Listbox 行数同步")
    check(app._idx_of_iid == {e["iid"]: i for i, e in enumerate(app.sources)},
          "回滚: iid 映射仍正确")
    if samples.get("NSF"):
        app.add_paths([samples["NSF"][0]])
        check(app.sources[-1]["kind"] == "CHIP", "回滚: 芯片音源仍能加入")
        app._refresh_info()
        check(app.var_info.get().startswith("共 "), "回滚: 汇总文案正常")
    app.clear_sources()
    check(app.listbox.size() == 0, "回滚: 清空生效")
    return True


def main():
    global SRCROOT, SAMPLE_SPC
    SRCROOT = sys.argv[1] if len(sys.argv) > 1 else r"C:\temp\gmev2"
    SAMPLE_SPC = os.path.join(HERE, "sample.spc")
    gui.messagebox.showinfo = lambda *a, **k: None
    gui.messagebox.showwarning = lambda *a, **k: None
    gui.messagebox.askyesno = lambda *a, **k: True

    root = tk.Tk()
    app = gui.App(root)
    app.drop_enabled = False
    root.update()

    # 回滚模式只跑 Listbox 那一套断言, 避免 Treeview 专属检查误报
    if gui.App.LIST_MODE != "tree":
        rollback_checks(app, make_spc_dir, {
            "NSF": pick("nsf", ".nsf", 1)})
        app._on_close()
        print()
        if FAILS:
            print(f"ROLLBACK FAIL ({len(FAILS)}): " + " | ".join(FAILS))
            return 1
        print("ROLLBACK PASS")
        return 0

    print("== 1. 控件形态 (Treeview 替代 Listbox) ==")
    check(app.src_tree is not None, "Treeview 已创建")
    check(app.listbox is None, "Listbox 已不再被直接使用")
    check(tuple(app.src_tree["columns"]) == ("name", "fmt", "tracks", "size",
                                             "secs"),
          "五列 name/fmt/tracks/size/secs")
    # ttk 的 cget 返回 Tcl_Obj 包装, 用 str() 取实际文本
    check(tcl_str(app.src_tree.cget("show")) == "headings",
          f'show="headings"(实际 {tcl_str(app.src_tree.cget("show"))!r})')
    check(tcl_str(app.src_tree.cget("selectmode")) == "extended",
          f'selectmode="extended"(实际 {tcl_str(app.src_tree.cget("selectmode"))!r})')
    check(app.list_sb is not None, "滚动条已绑定")

    print("== 2. SPC 文件夹 -> 行内容 ==")
    spc_dir = make_spc_dir("spcpkg")
    app.add_paths([spc_dir])
    check(len(app.sources) == 1 and app.sources[0]["kind"] == "SPC",
          "SPC 文件夹加入 1 项, kind=SPC")
    row = app.src_tree.get_children()[0]
    vals = app.src_tree.item(row, "values")
    check(str(vals[0]).endswith("spcpkg"), f"名称列 {vals[0]!r}")
    check(vals[1] == "SPC", f"格式列 {vals[1]!r}")
    check(str(vals[3]).endswith(("B", "KB", "MB")), f"大小列 {vals[3]!r}")

    print("== 3. 去重(同一文件夹拖两次) ==")
    n0 = len(app.sources)
    app.add_paths([spc_dir])
    check(len(app.sources) == n0, "重复文件夹被跳过")

    print("== 4. 五种格式各加一个真实文件 ==")
    samples = {
        "NSF": pick("nsf", ".nsf", 1),
        "GBS": pick("gbs", ".gbs", 1),
        "VGM": pick("smd", ".vgm", 1),
        "HES": pick("hes", ".hes", 1),
        "SPC": [os.path.join(spc_dir, "song1.spc")],
    }
    before = len(app.sources)
    for label, paths in samples.items():
        if not paths:
            print(f"  skip {label}: 样本目录里没有")
            continue
        n1 = len(app.sources)
        app.add_paths([paths[0]])
        got = app.sources[-1] if len(app.sources) > n1 else None
        if label == "SPC":
            check(got is not None and got["kind"] == "SPC", "SPC 识别为 SPC")
            continue
        check(got is not None and got["kind"] == "CHIP",
              f"{label} 识别为 CHIP (得到 {got and got['kind']})")
        if got is None:
            continue
        # 表格四列
        iid = got["iid"]
        v = app.src_tree.item(iid, "values")
        info = cf.probe(paths[0])
        check(v[1].startswith(info.fmt),
              f"{label} 格式列显示真实格式: {v[1]!r} (期望 {info.fmt})")
        n = int(info.n_tracks or 0)
        if n > 1:
            check(f"×{n}" in v[1], f"{label} 多轨在格式列显示 ×{n}: {v[1]!r}")
            check(v[2] == str(n), f"{label} 曲目数列 = {v[2]!r}")
        check(str(v[4]).rstrip("*◂") not in ("", "—"),
              f"{label} 时长列有值: {v[4]!r}")
        # 时长列的来源标记(设计 §3.5): 精确无后缀 / gbs_loop 加 ◂ / 估算加 *
        secs, exact = gui.Chip.plan(info, 150.0, 0.0)
        loop = 0.2 < float(info.loop_end_secs or 0) <= 3600
        if exact:
            check(not str(v[4]).endswith(("*", "◂")),
                  f"{label} 精确时长不加标记(实际 {v[4]!r})")
        elif loop:
            check(str(v[4]).endswith("◂"),
                  f"{label} GBS 循环点加 ◂(实际 {v[4]!r})")
        else:
            check(str(v[4]).endswith("*"),
                  f"{label} 估算时长加 *(实际 {v[4]!r})")

    print("== 5. 不支持格式: 不进列表 + 有可执行文案 (FR-4.1/4.2) ==")
    junk = os.path.join(SCRATCH, "x.qqq")
    with open(junk, "wb") as f:
        f.write(b"definitely not a chip file")
    n0 = len(app.sources)
    app.add_paths([junk])
    check(len(app.sources) == n0, "未知扩展名不进列表")
    check(gui.Chip.reason(junk, None).startswith("⚠"), "未知扩展名有 ⚠ 文案")
    # USF 是 FR-4.2 第一类: 已知却不支持。内容必须带真实 USF 魔数(SUS\0),
    # 否则 chip_formats 会按扩展名走第三级, 文案就退化成"扩展名不认识"。
    usf = os.path.join(SCRATCH, "song.usf")
    with open(usf, "wb") as f:
        f.write(b"SUS\x00" + b"\x00" * 100)
    n1 = len(app.sources)
    app.add_paths([usf])
    check(len(app.sources) == n1, "USF 不进列表")
    r = gui.Chip.reason(usf, cf.probe(usf))
    check("Nintendo 64" in r and "WAV" in r,
          f"USF 文案含机种与替代方案: {r[:70]}")

    print("== 6. 扩展名与内容不符 (FR-1.3 / AC-11) ==")
    if samples["NSF"]:
        fake = os.path.join(SCRATCH, "renamed.spc")
        shutil.copy2(samples["NSF"][0], fake)
        n1 = len(app.sources)
        app.add_paths([fake])
        # .spc 扩展名 -> _kind_of_file 先按 .spc 短路; 冲突提示由 probe 记录
        check(len(app.sources) == n1 or app.sources[-1]["fmt"] in ("NSF", "SPC"),
              "改名文件不崩且按内容/扩展名给出合理 kind")
    # .vgm 里其实是 gzip(VGZ): joshw 的已知行为, 应给出冲突提示但不报错
    if samples["VGM"]:
        info = cf.probe(samples["VGM"][0])
        check(info.fmt == "VGZ" and info.conflict,
              f"vgm 实为 VGZ 且 conflict=True (fmt={info.fmt})")
        note = gui.Chip.conflict_note(samples["VGM"][0], info)
        check("扩展名与实际格式不符" in note, f"冲突提示文案: {note[:50]}")

    print("== 7. 汇总文案 (FR-5.1) ==")
    app._refresh_info()
    txt = app.var_info.get()
    print(f"       实际: {txt}")
    check(txt.startswith(f"共 {len(app.sources)} 个音源"),
          f"汇总以「共 N 个音源」开头")
    check("NSF" in txt and "首)" in txt, "按格式分组且带首数")

    print("== 8. 估算 (FR-5.2 / AC-13) ==")
    n_tracks, est = app._estimate()
    check(n_tracks > 0, f"总曲数被真实解析出来 = {n_tracks}")
    check(est > 0, f"总音频秒数被估算出来 = {est:.0f}s")
    gb = app._estimate_bytes() / 1024 ** 3
    print(f"       预估体积 {gb:.2f} GB")

    print("== 9. SPC-only 分支逐字保留 (AC-14) ==")
    saved = list(app.sources)
    app.sources = [e for e in app.sources
                   if e["kind"] != "CHIP"][:1]      # 只留第一个 SPC 文件夹
    app._reindex()
    app._refresh_info()
    spc_only = app.var_info.get()
    print(f"       实际: {spc_only}")
    check(spc_only.startswith("共 1 个音源（其中 SPC"),
          f"SPC-only 文案与改造前同形: {spc_only[:50]!r}")
    n2, e2 = app._estimate()
    files = spc_wav.collect_spc([app.sources[0]["path"]])
    check(n2 == len(files), f"SPC-only 总曲数 == 实际 spc 数 ({n2})")
    check(e2 > 0, f"SPC-only 总秒数 > 0 ({e2:.1f}s)")
    app.sources = saved
    app._reindex()
    app._refresh_info()

    print("== 10. 移除 / 下标平移 ==")
    app.clear_sources()
    dirs = [make_spc_dir(f"d{i}", 1) for i in range(4)]
    for d in dirs:
        app.add_paths([d])
    if samples["NSF"]:
        app.add_paths([samples["NSF"][0]])       # 混一个非 SPC 行
    total = len(app.sources)
    check(total == 5, f"共 5 行(实际 {total})")
    check(len(app.src_tree.get_children()) == total, "Treeview 行数一致")
    rows = list(app.src_tree.get_children())
    labels_before = [str(app.src_tree.item(r, "values")[0]) for r in rows]
    app.src_tree.selection_set(rows[1])
    app.remove_selected()
    check(len(app.sources) == 4, "删中间行后剩 4")
    check(len(app.src_tree.get_children()) == 4, "Treeview 同步剩 4")
    check(app._idx_of_iid == {e["iid"]: i for i, e in enumerate(app.sources)},
          "iid->下标映射已重建")
    rows2 = list(app.src_tree.get_children())
    labels_after = [str(app.src_tree.item(r, "values")[0]) for r in rows2]
    check(labels_after == [x for x in labels_before if x != labels_before[1]],
          "sources 顺序 == 显示顺序, 删中间行没错位")
    # 再删一次, 确认反复删除不漂移
    app.src_tree.selection_set(rows2[0])
    app.remove_selected()
    check(app._idx_of_iid == {e["iid"]: i for i, e in enumerate(app.sources)},
          "连续两次删除后映射仍正确")

    print("== 11. 多选移除 ==")
    rows = list(app.src_tree.get_children())
    app.src_tree.selection_set(*rows[:2])
    check(len(app._ui_list_selected_entries()) == 2, "多选取到 2 项")
    app.remove_selected()
    check(len(app.sources) == len(rows) - 2, "多选移除数量正确")
    check(app._idx_of_iid == {e["iid"]: i for i, e in enumerate(app.sources)},
          "多选删除后映射正确")

    print("== 12. 清空 ==")
    app.clear_sources()
    check(app.sources == [] and app._idx_of_iid == {}, "sources/映射都空")
    check(app.src_tree.get_children() == (), "Treeview 无行")
    app.clear_sources()
    check(True, "空列表重复 clear 不 TclError")
    app._refresh_info()
    check(app.var_info.get() == "尚未添加音源", "空列表文案")

    print("== 13. ③ 区控件与启用联动 ==")
    app.var_chip_secs.set("90")
    check(app._chip_default_secs() == 90.0, "默认单遍读 90")
    app.var_chip_secs.set("9999")
    check(app._chip_default_secs() == float(gui.CHIP_SECS_MAX), "钳到 600")
    app.var_chip_secs.set("abc")
    check(app._chip_default_secs() == gui.CHIP_DEFAULT_SECS, "非法回落 150")
    app.var_chip_secs.set("150")
    app._sync_chip_controls()
    check(str(app.spin_chip_secs["state"]) == "disabled",
          "无 CHIP 时「默认单遍」置灰")
    check(str(app.cb_policy["state"]) == "disabled",
          "无 SPC 时「SPC 播放长度」置灰")
    # 只加 NSF -> 「默认单遍」应可用, SPC 策略仍置灰
    if samples["NSF"]:
        app.add_paths([samples["NSF"][0]])
        app._sync_chip_controls()
        check(str(app.spin_chip_secs["state"]) == "normal",
              "有 CHIP 时「默认单遍」可用")
        check(str(app.cb_policy["state"]) == "disabled",
              "只有 CHIP 时 SPC 策略仍置灰")
        app._refresh_info()
        check(app.var_plan.get().startswith("策略："),
              f"③ 区逐源策略文案: {app.var_plan.get()[:60]!r}")
        app.clear_sources()

    print("== 14. 目录型芯片音源 (FR-1.4) ==")
    chipdir = os.path.join(SCRATCH, "chipdir")
    shutil.rmtree(chipdir, ignore_errors=True)
    os.makedirs(chipdir)
    got = 0
    for label in ("NSF", "GBS", "VGM", "HES"):
        if samples[label]:
            shutil.copy2(samples[label][0], chipdir)
            got += 1
    if got:
        app.add_paths([chipdir])
        e = app.sources[-1]
        check(e["kind"] == "CHIP", f"混合芯片目录判为 CHIP(得到 {e['kind']})")
        check(e["n_tracks"] > 1, f"目录轨数被汇总 = {e['n_tracks']}")
        check(e["est_secs"] > 0, f"目录时长被汇总 = {e['est_secs']:.0f}s")
        v = app.src_tree.item(e["iid"], "values")
        check("📁" in str(v[0]), f"目录行带文件夹图标: {v[0]!r}")
        app.clear_sources()

    print("== 15. CD 镜像对 CHIP 源跳过 + 汇总 (PRD §1.2) ==")
    import queue as _q
    import threading as _th
    if samples["NSF"]:
        app.add_paths([samples["NSF"][0]])
        app.var_cd.set(True)
        app.var_wav.set(True)
        opts = gui._default_opts(os.path.join(SCRATCH, "out"))
        opts.update({"do_cd": True, "do_wav": True,
                     "out_dir": os.path.join(SCRATCH, "out")})
        q = _q.Queue()
        w = gui.ConvertWorker(list(app.sources), opts, q, _th.Event())
        w.run()                      # chip_decode 未接完时会记日志跳过, 不崩
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        kinds = [m[0] for m in msgs]
        check("chip_summary" in kinds, "发出了 chip_summary 汇总消息")
        logs = [m[1] for m in msgs if m[0] == "log"]
        check(any("不参与 CD 镜像" in x for x in logs),
              "逐条日志说明该源仅输出 WAV")
        summary = [m[1] for m in msgs if m[0] == "chip_summary"][0]
        check(summary[1] >= 1, f"汇总里的跳过数 >= 1 (得到 {summary})")
        print(f"       chip_summary={summary}")
        app.var_cd.set(False)

    print("== 16. 输出目录自动设置 ==")
    app.add_paths([os.path.join(SCRATCH, "d0")])
    check(app.var_out.get().endswith("d0output"),
          f"含文件夹时自动设置输出目录: {app.var_out.get()}")
    app.clear_sources()

    print("== 17. 关闭无残留 after ==")
    pending_before = set(root.tk.call("after", "info"))
    check(len(pending_before) > 0, f"关闭前有 {len(pending_before)} 个 after")
    app._on_close()
    check(getattr(app, "_after_drop", None) is None, "_after_drop 已清空")
    check(getattr(app, "_after_msg", None) is None, "_after_msg 已清空")
    check(getattr(app, "_info_job", None) is None, "_info_job 已清空")

    print()
    if FAILS:
        print(f"TREE_SMOKE FAIL ({len(FAILS)}):")
        for f in FAILS:
            print("   -", f)
        return 1
    print("TREE_SMOKE PASS")
    return 0


if __name__ == "__main__":
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    try:
        code = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(code)
