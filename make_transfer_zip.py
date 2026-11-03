#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_transfer_zip.py —— 把「换台电脑继续开发」需要的东西打成一个 zip。

## 为什么要有这个脚本

工程目录现在 **2.9 GB**，但其中 2.1 GB 是 `_pyi_unpack_tmp/`（PyInstaller
onefile 每次启动解包出来的临时目录，纯垃圾）、359 MB 是 `output/`（转出来的
WAV）、142 MB `build/`、135 MB `dist/`、68 MB `test_psf2.wav`。
**真正的源码 + 素材 + 原生解码器只有 ~35 MB。**

手工挑容易漏（漏 `tools/*/src` 就重建不了 `wsr2wav.exe`，漏
`.workbuddy-ai/memory` 就丢了这一路的踩坑记录），所以用白名单脚本，
而不是黑名单 `exclude` —— 新增的垃圾目录不会意外被打进去。

## 用法

    py -3.14 make_transfer_zip.py                 # ~35 MB，源码 + 素材 + 原生 exe/dll
    py -3.14 make_transfer_zip.py --with-exe      # 再加已打包的 63 MB exe（到新机器可立刻验证）
    py -3.14 make_transfer_zip.py --out D:\\x.zip  # 指定输出路径
    py -3.14 make_transfer_zip.py --dry           # 只列清单不写文件

产物默认落在工程根目录：`MSU1Converter-transfer-YYYYMMDD.zip`。
"""

import argparse
import datetime
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "msu1-to-cd")

#: 工程根下要带的文件 / 目录
#: （踩坑全集已移入 docs/，由 TOP_DIRS 覆盖，不再单列）
TOP_FILES = ("先看这里.txt", "换机转移说明.md", "make_transfer_zip.py")
TOP_DIRS = ("docs", "design-system", os.path.join(".workbuddy-ai", "memory"))

#: msu1-to-cd 下要带的目录（**白名单**：不在名单里的一律不带）
PKG_DIRS = ("web_ui", "qt_ui", "assets", "bios", "index", "vendor", "tools", "test")

#: msu1-to-cd 根目录下按扩展名挑文件
PKG_ROOT_EXTS = (".py", ".spec", ".md", ".txt", ".json", ".dll", ".exe",
                 ".bat", ".ps1", ".cfg", ".ini")

#: 任何层级都跳过的目录名
SKIP_DIRS = {"__pycache__", "build", "dist", "_pyi_unpack_tmp", "output",
             "obj", "tmp", "_gui_srcoutput", "_qa_shots", ".git",
             ".pytest_cache", "node_modules"}

#: 任何层级都跳过的文件
SKIP_EXTS = {".pyc", ".pyo", ".pdb", ".log", ".7z", ".zip", ".rar", ".bak",
             ".tmp", ".mp3", ".flac", ".ogg"}

#: 这些后缀只在大目录里跳过（vendor 下的截图、根目录的测试 WAV）
SKIP_BIG = {".wav", ".png", ".jpg", ".jpeg"}

#: 已打包的 exe（--with-exe 才带）
EXE_REL = os.path.join("msu1-to-cd", "build", "MSU1Converter-web-pyi",
                       "MSU1Converter-web.exe")


def _skip_file(name, rel):
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext in SKIP_EXTS:
        return True
    # 大文件类：vendor/ 下的截图、工程根下的 test_*.wav 都不带
    if ext in SKIP_BIG:
        if "vendor" in rel or rel.count(os.sep) <= 1:
            return True
    # 体积超过 20 MB 的单个文件一律不带（宁可漏，也不要莫名其妙打进去几百兆）
    try:
        if os.path.getsize(os.path.join(HERE, rel)) > 20 * 1024 * 1024:
            return True
    except OSError:
        return True
    return False


def collect():
    """返回 [(绝对路径, zip 内相对路径), …]。"""
    out = []

    for name in TOP_FILES:
        p = os.path.join(HERE, name)
        if os.path.isfile(p):
            out.append((p, name))

    for name in TOP_DIRS:
        base = os.path.join(HERE, name)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, HERE)
                if not _skip_file(f, rel):
                    out.append((p, rel))

    # msu1-to-cd 根目录：按扩展名挑
    if os.path.isdir(PKG):
        for f in sorted(os.listdir(PKG)):
            p = os.path.join(PKG, f)
            if not os.path.isfile(p):
                continue
            if os.path.splitext(f.lower())[1] not in PKG_ROOT_EXTS:
                continue
            rel = os.path.relpath(p, HERE)
            if not _skip_file(f, rel):
                out.append((p, rel))

        for name in PKG_DIRS:
            base = os.path.join(PKG, name)
            if not os.path.isdir(base):
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in files:
                    p = os.path.join(root, f)
                    rel = os.path.relpath(p, HERE)
                    if not _skip_file(f, rel):
                        out.append((p, rel))

    out.sort(key=lambda kv: kv[1])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--with-exe", action="store_true",
                    help="把已打包的 MSU1Converter-web.exe 也带上（+63 MB）")
    ap.add_argument("--dry", action="store_true", help="只列清单，不写 zip")
    args = ap.parse_args()

    items = collect()
    if args.with_exe:
        p = os.path.join(HERE, EXE_REL)
        if os.path.isfile(p):
            items.append((p, EXE_REL))
        else:
            print("⚠ 没找到已打包的 exe：%s（先跑 build_web_pyi.py）" % EXE_REL,
                  file=sys.stderr)

    total = sum(os.path.getsize(p) for p, _ in items)
    print("待打包 %d 个文件，原始体积 %.1f MB" % (len(items), total / 1048576.0))
    if args.dry:
        by_top = {}
        for p, rel in items:
            top = rel.split(os.sep)[0] if os.sep in rel else "(根文件)"
            by_top[top] = by_top.get(top, 0) + os.path.getsize(p)
        for k in sorted(by_top, key=lambda x: -by_top[x]):
            print("  %-28s %7.1f MB" % (k, by_top[k] / 1048576.0))
        return 0

    out = args.out or os.path.join(
        HERE, "MSU1Converter-transfer-%s.zip"
              % datetime.datetime.now().strftime("%Y%m%d"))
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    print("写到 %s" % out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, (p, rel) in enumerate(items, 1):
            # zip 规范要求正斜杠；Windows 的 os.sep 是反斜杠，
            # 直接写进去部分解压工具会造出带反斜杠的怪文件名
            zf.write(p, rel.replace(os.sep, "/"))
            if i % 200 == 0:
                print("  … %d/%d" % (i, len(items)), flush=True)
    size = os.path.getsize(out)
    print("\n完成：%s（%.1f MB，压缩后 / 原始 %.1f MB）"
          % (out, size / 1048576.0, total / 1048576.0))
    print("拷到新电脑解压，然后照 换机转移说明.md 走。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
