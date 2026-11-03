#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_trash.py — 把文件/目录送进**回收站**（可恢复），不做永久删除。

为什么不直接 ``shutil.rmtree``：

    清理脚本最怕「删错了但捞不回来」。Windows 的回收站是唯一既省事又
    可逆的机制，而 Python 标准库没有封装它。``send2trash`` 要额外装包，
    这里直接用 ``ctypes`` 调 ``SHFileOperationW``（send2trash 在 Windows
    上做的也是这件事），零依赖。

为什么要 ``FOF_ALLOWUNDO``：

    不传这个标志，``SHFileOperation`` 就是**永久删除**。传了才进回收站。

为什么要 ``FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI``：

    批量删除时每一项都弹一次确认框没法自动化；但 ``FOF_ALLOWUNDO`` 仍然
    生效 —— 只是不问你，删完还在回收站里。

⚠ 回收站有容量上限（默认约为所在分区容量的 5%）。超过上限时 Windows 会
**永久删除**而不是进回收站。批量清大目录前先确认目标分区剩余空间够。

用法::

    py -3.14 vendor/_trash.py <路径> [更多路径...]
    py -3.14 vendor/_trash.py --dry <路径>      # 只报会删什么，不动手
    py -3.14 vendor/_trash.py --list file.txt   # 从文本文件读路径（一行一个，# 开头跳过）
"""
import argparse
import ctypes
import os
import sys
from ctypes import wintypes

# --- SHFileOperationW 的结构与常量 ---
FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # ← 进回收站的关键
FOF_NOERRORUI = 0x0400
FOF_NOCONFIRMMKDIR = 0x0200


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _double_null(paths):
    """SHFileOperation 要「每个路径以 \\0 结尾，整个串再以 \\0 结尾」。"""
    return "\0".join(paths) + "\0\0"


def trash(paths, dry=False):
    """把一批路径送进回收站。返回 ``(成功列表, 失败列表)``。

    每个失败项是 ``(路径, 原因)``。**绝不抛异常** —— 清理脚本不该因为
    一个文件被占用就整批中断。
    """
    ok, bad = [], []
    todo = []
    for p in paths:
        ap = os.path.abspath(p)
        if not os.path.exists(ap):
            bad.append((ap, "不存在"))
            continue
        todo.append(ap)

    if dry:
        return todo, bad
    if not todo:
        return ok, bad

    # SHFileOperation 一次能收多个路径，但**失败时说不清是哪个**。
    # 清理脚本要的是「逐个确认」，所以一个一个来。
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pTo = None
    op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION |
                 FOF_SILENT | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR)
    op.hNameMappings = None
    op.lpszProgressTitle = None

    for ap in todo:
        op.pFrom = _double_null([ap])
        op.fAnyOperationsAborted = False
        try:
            rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        except Exception as exc:                  # noqa: BLE001
            bad.append((ap, "调用失败：%s" % exc))
            continue
        # ⚠ 判据是「路径还在不在」，不是「返回值是不是 0」。
        #   SHFileOperation 在**目录已经被搬空**的情况下会返回 2
        #   （ERROR_FILE_NOT_FOUND），但删除其实成功了 —— 实测 build/dist/
        #   output/tmp 四个目录都是返回 2 却真的没了。只看返回值会误报一堆
        #   「未清」，把报告搞脏。
        if os.path.exists(ap):
            if op.fAnyOperationsAborted:
                bad.append((ap, "被系统中止"))
            else:
                bad.append((ap, "SHFileOperation 返回 %d，路径仍在" % rc))
            continue
        ok.append(ap)
    return ok, bad


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--dry", action="store_true", help="只列出，不删除")
    ap.add_argument("--list", default="", help="从文本文件读路径")
    args = ap.parse_args(argv)

    targets = list(args.paths)
    if args.list:
        with open(args.list, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    targets.append(s)
    if not targets:
        print("没有目标")
        return 2

    ok, bad = trash(targets, dry=args.dry)
    if args.dry:
        print("将要进回收站（%d 项）:" % len(ok))
        for p in ok:
            print("  ", p)
    else:
        print("已进回收站 %d 项" % len(ok))
    for p, why in bad:
        print("  [跳过] %s  —— %s" % (p, why))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
