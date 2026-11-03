#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证拖放: 构造真实 HDROP 并发送 WM_DROPFILES 给主窗口。

不是模拟 UI, 而是走系统级消息: 只有 WndProc 子类化正确、DragQueryFile
解析正确、DragFinish 正确释放, 才能拿到路径。同时验证:
  * 文件夹被识别为 SPC / MSU-1
  * 拖入文件夹后输出目录自动变为 "<父目录>/<文件夹名>output"
  * 纯文件拖放不改输出目录
  * **在 Tk 事件处理期间收到消息(重入场景)也不会崩** —— 这是 v3.1 闪退的
    根因所在, 窗口过程一旦调用 Tcl 就重入解释器, 进程直接消失。
"""
import ctypes
import os
import struct
import sys
import time
import tkinter as tk
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import msu1_gui as g  # noqa: E402

WM_DROPFILES = 0x0233
k32 = ctypes.windll.kernel32
u32 = ctypes.windll.user32

k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
k32.GlobalFree.argtypes = [ctypes.c_void_p]
u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM]
u32.SendMessageW.restype = ctypes.c_ssize_t


def make_hdrop(paths):
    """按 Windows DROPFILES 结构构造 HDROP (fWide=1, UTF-16LE 双 \\0 结尾)。"""
    blob = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    hdr = 20                                    # DWORD+POINT+BOOL+BOOL
    raw = struct.pack("<IiiII", hdr, 0, 0, 0, 1) + blob
    h = k32.GlobalAlloc(0x0042, len(raw))       # GHND
    p = k32.GlobalLock(h)
    ctypes.memmove(p, raw, len(raw))
    k32.GlobalUnlock(h)
    return h


def drop(hwnd, paths):
    h = make_hdrop(paths)
    u32.SendMessageW(wintypes.HWND(hwnd), WM_DROPFILES,
                     wintypes.WPARAM(h), 0)
    k32.GlobalFree(h)


def pump(root, ms=500):
    """跑 ms 毫秒的事件循环, 让 after(120) 的拖放轮询有机会执行。"""
    end = time.time() + ms / 1000.0
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def main():
    spc_dir = os.path.join(os.path.expanduser("~"), "Desktop",
                             "Chrono Trigger (EMU).zophar")
    spc_one = os.path.join(spc_dir, "101 Presentiment.spc")

    # 这条门禁要真拖一个 SPC 目录进去，素材不在本机时直接跳过
    # （以前路径写死成 C:/Users/MY-XQH/…，换台机器就成了「假失败」）。
    if not os.path.isdir(spc_dir):
        print(f"SKIP 源目录不存在: {spc_dir}")
        return 0

    # 场景 0: 上次异常退出时**仍应挂载拖放**，只在日志里说一句
    # ⚠ v4.31 起「异常退出 -> 本次不挂拖放」这条降级被**故意停用**了
    # （见 msu1_gui.take_session_lock 的注释：提示只写日志、用户不看，而且
    #  不会自愈，反而把拖放永久钉死）。老断言查的是「关闭拖放」那句文案，
    # 早就随降级一起删掉了 —— 这里改成断言当前口径。
    g.release_session_lock()
    lock = g._session_lock_path()
    with open(lock, "w", encoding="utf-8") as f:
        f.write("99999999")            # 一个不存在的 pid = 上次非正常退出
    r0 = tk.Tk()
    a0 = g.App(r0)
    r0.update()
    degrade_ok = a0.drop_enabled is True
    print(f"[0] 上次异常退出 -> drop_enabled={a0.drop_enabled}（应为 True）")
    a0._on_close()
    g.release_session_lock()

    root = tk.Tk()
    app = g.App(root)
    root.update()
    if not app.drop_enabled:
        print("FAIL 拖放挂载失败")
        return 1
    print("拖放挂载: OK")
    hwnd = u32.GetAncestor(root.winfo_id(), 2) or root.winfo_id()
    fails = []
    if not degrade_ok:
        fails.append("上次异常退出后拖放被误关（v4.31 起不该再降级）")

    # --- 1. 拖入整个文件夹 (含 92 首, 最接近用户的实际操作) ---
    out_before = app.var_out.get()
    drop(hwnd, [spc_dir])
    pump(root, 800)
    ok_dir = bool(app.sources) and app.sources[0]["kind"] == "SPC"
    print(f"[1] 拖入文件夹 -> 音源 {len(app.sources)} 项, "
          f"kind={app.sources[0]['kind'] if app.sources else '-'}")
    expect_out = os.path.join(os.path.dirname(os.path.normpath(spc_dir)),
                              os.path.basename(os.path.normpath(spc_dir)) + "output")
    print(f"    输出目录: {app.var_out.get()}")
    print(f"    预算行  : {app.var_info.get()}")
    if not ok_dir:
        fails.append("文件夹未识别为 SPC")
    if os.path.normpath(app.var_out.get()) != os.path.normpath(expect_out):
        fails.append("输出目录未设为 源文件夹名+output")
    if app.var_out.get() == out_before:
        fails.append("输出目录没有变化")

    # --- 2. 拖入单个文件: 不复位输出目录 ---
    out_keep = app.var_out.get()
    drop(hwnd, [spc_one])
    pump(root, 500)
    print(f"[2] 拖入单文件 -> 音源 {len(app.sources)} 项, "
          f"最后一项: {app.sources[-1]['label']}")
    if len(app.sources) != 2:
        fails.append(f"单文件未添加, 当前 {len(app.sources)} 项")
    if app.var_out.get() != out_keep:
        fails.append("拖入文件时不应改动输出目录")
    if app.sources and app.sources[-1]["label"].lower().endswith(".spc"):
        fails.append("单文件 label 未去掉扩展名")

    # --- 3. 重复拖入同一项: 不重复添加 ---
    drop(hwnd, [spc_one])
    pump(root, 400)
    print(f"[3] 重复拖同一文件 -> 音源 {len(app.sources)} 项 (应仍为 2)")
    if len(app.sources) != 2:
        fails.append("重复项被重复添加")

    # --- 4. 不支持的类型被忽略 ---
    txt = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "x.txt")
    os.makedirs(os.path.dirname(txt), exist_ok=True)
    with open(txt, "w") as f:
        f.write("hi")
    drop(hwnd, [txt])
    pump(root, 400)
    print(f"[4] 拖入 .txt -> 音源 {len(app.sources)} 项 (应仍为 2)")
    if len(app.sources) != 2:
        fails.append(".txt 被错误添加")
    os.remove(txt)

    # --- 5. 重入场景: 在 Tk 自己的 after 回调里收到 drop ---
    # 此时 Tk 的 notifier 还在栈上, 旧实现会在窗口过程里调 root.after ->
    # 重入 Tcl 解释器 -> 进程闪退。新实现只入队, 因此这里必须存活。
    print("[5] 重入测试: 在 after 回调内发送 WM_DROPFILES …")
    holder = {}

    def _reenter():
        drop(hwnd, [spc_one])          # 同步进 WndProc, 栈里坐着 Tk notifier
        holder["sent"] = True

    root.after(10, _reenter)
    pump(root, 600)
    print(f"    存活: {holder.get('sent') is True}, 音源 {len(app.sources)} 项")
    if not holder.get("sent"):
        fails.append("重入场景未执行")
    if len(app.sources) != 2:
        fails.append("重入场景下音源数异常")

    # --- 6. 一次拖入多个混合项 (全部是已存在的, 不应重复) ---
    drop(hwnd, [spc_dir, spc_one, spc_one])
    pump(root, 700)
    print(f"[6] 混合拖入 -> 音源 {len(app.sources)} 项 (应仍为 2)")
    if len(app.sources) != 2:
        fails.append("混合拖入出现了重复或漏项")

    root.destroy()
    g.release_session_lock()
    print()
    if fails:
        for f in fails:
            print("FAIL", f)
        return 1
    print("DROP TEST PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
