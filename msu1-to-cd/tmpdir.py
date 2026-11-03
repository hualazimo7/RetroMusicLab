#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmpdir.py — 统一的临时目录（默认 D 盘，别再把 C 盘塞满）。

## 为什么要有这个模块

v4.43 之前所有临时 PCM 落盘都走系统 ``%TEMP%``（C 盘）。XA / 芯片音源解码
在超过阈值时会**逐轨落盘**（``_SpoolPcm``），一张 PS1 盘的 XA 能到几百 MB，
跑几十轮门禁就堆出十几 GB —— 2026-10-22 实测 C 盘 ``%TEMP%`` 里积了
**21 GB**（xa_pcm 9.0G + chip_pcm 5.7G + _MEI 4.3G + exe_disc 1.4G），
用户 C 盘直接从 60 GB 可用掉到 27 GB。

用户要求：以后所有临时文件放 D 盘。

## 优先级（换机也不会坏）

1. 环境变量 ``MSU1_TEMP``（想放别处 / 多台机器统一时用）
2. ``D:\\MSU1Temp``（Windows 且 D 盘可写）
3. ``tempfile.gettempdir()``（兜底；C 盘或任何系统 temp）

## 用法

模块顶部 ``import tmpdir; tmpdir.init()`` 一次即可。之后**所有**
``tempfile.mkdtemp()`` / ``NamedTemporaryFile()`` 自动落在该目录，
不需要逐个改调用点。

``init()`` 还会顺手清掉 24 小时前的旧临时项 —— 被 ``taskkill /F`` 强杀
的进程留下的目录，下次启动就回收，不会无限堆积。
"""

import os
import shutil
import tempfile
import time

#: 临时根目录的目录名（放在盘符根下，如 ``D:\MSU1Temp``）
DIR_NAME = "MSU1Temp"

#: 本工程产生的临时项前缀。``sweep()`` **只动这些**，绝不碰别人的东西。
#: 新增落盘前缀时记得加进来，否则强杀残留不会被自动回收。
PREFIXES = (
    "xa_pcm_", "chip_pcm_", "spc_pcm_",
    # ⚠ 下面三个是 `NamedTemporaryFile` 的**前缀**。它们原本没传 prefix，
    # 生成的名字是 `tmpXXXX.spcbin` —— 前缀白名单里写 "spcbin" 永远匹配不到，
    # 强杀后的残留就永久留在目录里（v5.0 实测发现）。现在创建时统一传
    # prefix="spcbin_" / "chipbin_" / "chiperr_"，这里才对得上。
    "chipbin_", "chiperr_", "spcbin_", "spcerr_",
    "msu1_", "cloud_st_", "cdp_st_", "cdout_", "cdmain_", "cdtest_",
    "psf_", "txtp_", "chipfmt_", "disc_cue_", "exe_disc_",
    "dl_convert", "nonascii_", "wavmerge_", "ext_backend_",
    "ncm_gate_", "ckcdp_", "cksw_", "snap_trk_", "put_", "ord_",
    # v5.0-qt 门禁（qt_ui/selftest.py 里的临时目录）
    "qt_gate_", "qt_ui_icons",
)

_root = None
_fallback = False        # root() 是否回落到了系统 temp
_inited = False


def _writable(path):
    """目录能否建出来并真的写进去（只看 exists 不够，权限可能只读）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-probe")
        with open(probe, "wb") as fh:
            fh.write(b"1")
        os.remove(probe)
        return True
    except OSError:
        return False


def root():
    """临时根目录（幂等；不改 ``tempfile`` 的全局设置）。"""
    global _root, _fallback
    if _root:
        return _root
    cands = []
    env = (os.environ.get("MSU1_TEMP") or "").strip().strip('"')
    if env:
        cands.append(env)
    if os.name == "nt":
        cands.append("D:\\" + DIR_NAME)
    for c in cands:
        if _writable(c):
            _root = c
            return c
    _root = tempfile.gettempdir()
    _fallback = True
    return _root


def is_fallback():
    """是否回落到了系统 temp（说明 D 盘不可用）。"""
    root()
    return _fallback


def _dir_size(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def sweep(max_age_h=24.0):
    """删掉本工程遗留的旧临时项。返回 ``(个数, 字节)``。

    三条安全线：
      * 只在**专属目录**里动手（回落到系统 temp 时直接返回，绝不乱删）
      * 只删 ``PREFIXES`` 开头的名字
      * 只删 mtime 超过 ``max_age_h`` 小时的（正在跑的进程不碰）
    """
    if is_fallback():
        return (0, 0)
    base = root()
    cutoff = time.time() - max_age_h * 3600.0
    n = 0
    freed = 0
    try:
        names = os.listdir(base)
    except OSError:
        return (0, 0)
    for name in names:
        if not name.startswith(PREFIXES):
            continue
        p = os.path.join(base, name)
        try:
            if os.path.getmtime(p) > cutoff:
                continue
            if os.path.isdir(p):
                sz = _dir_size(p)
                shutil.rmtree(p, ignore_errors=True)
                if os.path.exists(p):
                    continue
            else:
                sz = os.path.getsize(p)
                os.remove(p)
            n += 1
            freed += sz
        except OSError:
            continue
    return (n, freed)


def init(sweep_old=True):
    """把 ``tempfile`` 的默认目录指到 ``root()``，并清理旧残留。

    幂等；返回值是实际使用的目录，方便调用方打日志。
    """
    global _inited
    r = root()
    if sweep_old and not _inited:
        _inited = True
        try:
            sweep()
        except Exception:                     # noqa: BLE001 清理失败不影响主流程
            pass
    tempfile.tempdir = r
    return r


def describe():
    """一行说明，给自检 / 日志用。"""
    r = init(sweep_old=False)
    tail = "（D 盘不可写，已回落系统 temp）" if _fallback else ""
    return "临时目录: %s%s" % (r, tail)
