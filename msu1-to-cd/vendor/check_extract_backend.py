#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_extract_backend.py — 7z 解压后端门禁（bsdtar ↔ py7zr 双路一致性）。

## 为什么要这个门禁

安卓端**没有** ``%SystemRoot%\\System32\\tar.exe``（`sys.platform == "linux"`，
分区存储也不允许把可执行文件解到数据目录去跑），下载后的自动解压只能走
``joshw_dl._extract_with_py7zr``。桌面版继续走 bsdtar。

两条路必须产出**同样的文件名、同样的字节**，否则同一个包在两端解出来不一样，
而且这种偏差只在真机上才会暴露 —— 所以必须在本机就把两条路都跑一遍。

## 覆盖项

1. ``unsafe_members()`` 对绝对路径 / 盘符 / ``..`` 的拦截（含反斜杠变体）
2. bsdtar 路：解出文件集合与源一致
3. py7zr 路：**强制** ``find_tar() -> None``，解出集合与 bsdtar 路逐项相同
4. 两侧 sha256 全等（不是比大小，是比内容）
5. 取消：cancel 预置 → 返回失败 + 原因含「取消」，且**原包仍在**
6. 超时：timeout 设 0.05s → 返回失败 + 原因含「超时」，且**原包仍在**
7. py7zr 缺失：``_import_py7zr -> None`` → 错误消息里同时点到 bsdtar 与 py7zr
8. 重复解压同一包：``created`` 不为空（回退成「目录内全部文件」）

## 用法

    python vendor/check_extract_backend.py

py7zr 默认从 ``%LOCALAPPDATA%\\MSU1Converter\\_testlibs`` 加载（避免污染
工程与系统 site-packages）；也可用环境变量 ``PY7ZR_LIBS`` 指定。
装法::

    python -m pip install --target "%LOCALAPPDATA%\\MSU1Converter\\_testlibs" \\
        --only-binary :all: py7zr

⚠ **找不到 py7zr 时整份 py7zr 分支会 SKIP 并 rc=0**，同时打印醒目提示。
   桌面版不需要 py7zr，所以跳过不算失败；但**发布安卓版前必须看到 SKIP=0**。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import traceback

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)


def _find_py7zr_libs():
    """把可能装着 py7zr 的目录加到 sys.path 前面，返回所用目录（或 None）。"""
    candidates = []
    env = os.environ.get("PY7ZR_LIBS")
    if env:
        candidates.append(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "MSU1Converter", "_testlibs"))
    candidates.append("/tmp/pylibs")
    for path in candidates:
        if path and os.path.isdir(path) and os.path.isdir(os.path.join(path, "py7zr")):
            if path not in sys.path:
                sys.path.insert(0, path)
            return path
    return None


_LIBS = _find_py7zr_libs()

import joshw_dl as jd  # noqa: E402  - 必须在 sys.path 调整之后

RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label, detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label,
                       ("  [" + detail + "]") if detail else ""))
    return bool(ok)


def skip(label, reason):
    RESULTS.append((None, label, reason))
    print("SKIP  %s  [%s]" % (label, reason))


def sha_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def snap(root):
    """目录 -> {相对路径(小写): sha256}，用来逐项比对两条后端。"""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out[rel.lower()] = sha_of(full)
    return out


def snap_payload(root, strip_top):
    """剥掉归档自带的顶层目录再快照。

    打包时会把「被压缩的那个目录名」写成第一层，所以解出来是
    ``<target>/_src/a.nsf`` 而不是 ``<target>/a.nsf``。比对时必须剥掉这一层，
    否则每一条都会因为路径前缀对不上而判差异（第一版就是栽在这里）。
    """
    inner = os.path.join(root, strip_top) if strip_top else None
    if inner and os.path.isdir(inner):
        return snap(inner)
    return snap(root)


def build_sample(dest_dir):
    """造一个含中文名 / 子目录 / 二进制 / 不可压缩数据的 7z。

    ``blob.bin`` 特意给到 4 MB：包太小的话 py7zr 几十毫秒就解完了，
    「超时中断」这一项永远触发不到（第一版用 300 KB，0.05s 都跑得完）。
    """
    src = os.path.join(dest_dir, "_src")
    os.makedirs(os.path.join(src, "sub"), exist_ok=True)
    payload = {
        "a.nsf": b"NESM\x1a" + b"A" * 4096,
        "sub/b.spc": b"SNES-SPC700" + b"B" * 8192,
        "中文名字.nsf": b"NESM\x1a" + b"C" * 2048,
        "blob.bin": os.urandom(4_000_000),
    }
    for rel, data in payload.items():
        full = os.path.join(src, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
    return src, payload


def make_archive(work, src):
    """优先用 bsdtar 造包（真实场景）；没有就用 py7zr 造，至少能测通解压路径。"""
    archive = os.path.join(work, "sample.7z")
    tar = jd.find_tar()
    if tar:
        import subprocess
        proc = subprocess.run(
            [tar, "-a", "-cf", archive, "--format=7zip",
             os.path.basename(src)],
            cwd=os.path.dirname(src), shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        if proc.returncode == 0 and os.path.isfile(archive):
            return archive, "bsdtar"
    py7zr = jd._import_py7zr()
    if py7zr is None:
        return None, "none"
    with py7zr.SevenZipFile(archive, "w") as zf:
        zf.writeall(src, os.path.basename(src))
    return archive, "py7zr"


def main():
    print("=" * 70)
    print("7z 解压后端门禁  (bsdtar ↔ py7zr)")
    print("=" * 70)
    print("py7zr libs : %s" % (_LIBS or "NOT FOUND"))
    print("find_tar() : %s" % (jd.find_tar() or "None（桌面版会走 py7zr）"))
    print("-" * 70)

    # ---- 1. 非法路径 -------------------------------------------------------
    bad = jd.unsafe_members([
        "a/b.nsf",                      # 正常
        "../evil.nsf",                  # 上跳
        "sub/../../evil",               # 深层上跳
        "/etc/passwd",                  # 绝对路径
        "C:/Windows/x.dll",             # 盘符
        "..\\win\\evil",                # 反斜杠变体
    ])
    check(len(bad) == 5, "1.1 unsafe_members 拦住 5 个非法项", "got=%r" % (bad,))
    check(jd.unsafe_members(["ok/a.nsf", "b.nsf"]) == [],
          "1.2 unsafe_members 不误伤正常条目")

    # ---- 2. 造样本 ---------------------------------------------------------
    work = tempfile.mkdtemp(prefix="ext_backend_", dir=PROJ)
    try:
        src, payload = build_sample(work)
        archive, builder = make_archive(work, src)
        if archive is None:
            skip("2.x 样本构造", "既无 bsdtar 也无 py7zr")
            return _report()
        check(os.path.isfile(archive), "2.1 样本包已生成", "%s by %s (%dB)"
              % (os.path.basename(archive), builder, os.path.getsize(archive)))
        src_snap = snap(src)

        # ---- 3. bsdtar 路 --------------------------------------------------
        tar_available = bool(jd.find_tar())
        tar_snap = None
        if tar_available:
            out_tar = os.path.join(work, "out_tar")
            ok, files, err = jd.extract_archive(archive, out_tar)
            check(ok, "3.1 bsdtar 解压成功", err or "%d 个文件" % len(files))
            tar_snap = snap_payload(out_tar, os.path.basename(src))
            check(tar_snap == src_snap, "3.2 bsdtar 产物字节与源一致",
                  "差异 %d 项" % len(set(tar_snap.items()) ^ set(src_snap.items())))
        else:
            skip("3.x bsdtar 路", "本机无 bsdtar（Android 场景，属正常）")

        # ---- 4. py7zr 路（强制 find_tar -> None） --------------------------
        if not _LIBS:
            skip("4.x py7zr 路", "未安装 py7zr（发布安卓版前必须补跑）")
        else:
            real_find = jd.find_tar
            jd.find_tar = lambda: None
            try:
                out_py = os.path.join(work, "out_py7zr")
                ok, files, err = jd.extract_archive(archive, out_py)
                check(ok, "4.1 py7zr 解压成功", err or "%d 个文件" % len(files))
                py_snap = snap_payload(out_py, os.path.basename(src))
                check(py_snap == src_snap, "4.2 py7zr 产物字节与源一致",
                      "差异 %d 项" % len(set(py_snap.items()) ^ set(src_snap.items())))
                if tar_snap is not None:
                    check(py_snap == tar_snap,
                          "4.3 两条后端产物**逐字节相同**",
                          "差异 %d 项" % len(set(py_snap.items()) ^ set(tar_snap.items())))
                else:
                    skip("4.3 双后端比对", "本机无 bsdtar 基线")
                check(any("中文" in k for k in py_snap),
                      "4.4 py7zr 正确解出中文文件名")

                # ---- 5. 取消 -----------------------------------------------
                ev = threading.Event()
                ev.set()
                out_cancel = os.path.join(work, "out_cancel")
                ok2, files2, err2 = jd.extract_archive(archive, out_cancel, cancel=ev)
                check((not ok2) and files2 == [] and "取消" in err2,
                      "5.1 已取消时返回失败且无产物", err2)
                check(os.path.isfile(archive), "5.2 取消后原包仍在")

                # ---- 6. 超时 -----------------------------------------------
                out_to = os.path.join(work, "out_timeout")
                ok3, files3, err3 = jd.extract_archive(archive, out_to, timeout=0.02)
                check((not ok3) and "超时" in err3, "6.1 超时被正确中断", err3)
                check(os.path.isfile(archive), "6.2 超时后原包仍在")

                # ---- 7. 重复解压 -------------------------------------------
                out_again = os.path.join(work, "out_again")
                jd.extract_archive(archive, out_again)
                ok4, files4, err4 = jd.extract_archive(archive, out_again)
                check(ok4 and len(files4) > 0,
                      "7.1 重复解压同包仍返回文件列表（不空）",
                      "%d 个" % len(files4))
            finally:
                jd.find_tar = real_find

            # ---- 8. py7zr 缺失时的错误文案 ------------------------------
            real_imp = jd._import_py7zr
            jd._import_py7zr = lambda: None
            jd.find_tar = lambda: None
            try:
                ok5, files5, err5 = jd.extract_archive(
                    archive, os.path.join(work, "out_nolib"))
                check((not ok5) and "py7zr" in err5 and "tar.exe" in err5,
                      "8.1 双后端都缺时文案同时点到 tar.exe 与 py7zr", err5)
            finally:
                jd._import_py7zr = real_imp
                jd.find_tar = real_find
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return _report()


def _report():
    failed = [r for r in RESULTS if r[0] is False]
    skipped = [r for r in RESULTS if r[0] is None]
    passed = [r for r in RESULTS if r[0] is True]
    print("-" * 70)
    print("结果: %d 通过 / %d 失败 / %d 跳过" % (len(passed), len(failed), len(skipped)))
    if skipped:
        print("⚠ 有 %d 项跳过 —— 桌面版可接受，**发布安卓版前必须清零**" % len(skipped))
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
