#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_fix_longpath.py — 把超过 MAX_PATH 的深层目录**逐层改名缩短**。

为什么需要：

    门禁里有一条「超长路径处理」的测试，会故意造出
    ``gate_v5912/lp/dddd…(41字符)×6/LLLL…(200字符).txtp`` 这种 470 多字符的
    路径。清临时目录时 ``SHFileOperation``（回收站）**不支持超过 260 字符的
    路径**，碰到它就整批中止 —— 实测 ``D:\\MSU1Temp`` 22 GB 只回收了 5.1 GB
    就停了，原因就是这个文件。

    这里不改内容、不删数据，只把每一层目录名从 41 个字符缩到 2~3 个，
    每层省 39 个字符，6 层下来 473 → 239，就落回 MAX_PATH 以内，
    之后照常走回收站。

⚠ 必须用 ``\\\\?\\`` 前缀 + **全反斜杠**：Windows 的扩展长度路径语法不认
正斜杠，混用会直接 ``WinError 3 系统找不到指定的路径``。

用法::

    py -3.14 vendor/_fix_longpath.py "D:\\MSU1Temp" [--apply]
"""
import argparse
import os
import sys

#: Windows 扩展长度路径前缀。必须是 4 个字符：反斜杠 反斜杠 问号 反斜杠。
EXT = "\\\\?\\"


def real_len(p):
    """去掉 ``\\\\?\\`` 之后的真实长度（那 4 个字符不计入 MAX_PATH）。"""
    return len(p) - 4 if p.startswith(EXT) else len(p)


def shorten_tree(root, apply=False, limit=280):
    """把 root 下所有超长路径逐层缩短。返回改动列表。"""
    changed = []
    stack = [EXT + os.path.abspath(root)]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError as exc:
            print("  [跳过] 读不了 %s（%s）" % (cur, exc))
            continue
        for e in entries:
            p = os.path.join(cur, e.name)
            if e.is_dir(follow_symlinks=False):
                # 目录本身没超，但里面有超长的：先试着重命名这一层
                if real_len(p) > 200 and len(e.name) > 12:
                    short = os.path.join(cur, "d%d" % len(changed))
                    changed.append((p, short))
                    if apply:
                        try:
                            os.rename(p, short)
                            print("  目录 %d → %d 字符" % (real_len(p), real_len(short)))
                            p = short
                        except OSError as exc:
                            print("  [失败] 目录改名 %s（%s）" % (e.name[:20], exc))
                stack.append(p)
            elif e.is_file(follow_symlinks=False):
                if real_len(p) > limit:
                    ext = os.path.splitext(e.name)[1][:8] or ".bin"
                    short = os.path.join(cur, "L" + ext)
                    changed.append((p, short))
                    if apply:
                        try:
                            os.rename(p, short)
                            print("  文件 %d → %d 字符" % (real_len(p), real_len(short)))
                        except OSError as exc:
                            print("  [失败] 文件改名 %s…（%s）" % (e.name[:20], exc))
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--apply", action="store_true", help="真改；不加只列出")
    args = ap.parse_args(argv)

    if not os.path.isdir(EXT + os.path.abspath(args.root)):
        print("目录不存在:", args.root)
        return 2
    print("扫描 %s（%s）" % (args.root, "执行改名" if args.apply else "只列出"))
    changed = shorten_tree(args.root, apply=args.apply)
    if not changed:
        print("没有超过 MAX_PATH 的路径")
        return 0
    print("共 %d 项需要缩短" % len(changed))
    if not args.apply:
        for a, b in changed[:10]:
            print("  %d → %d 字符" % (real_len(a), real_len(b)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
