#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wavmerge.py — 把同一音源产生的多个 WAV 顺序拼成一个 WAV（v4.24）。

## 为什么需要

NSF / GBS / HES 这类芯片音源**一个文件里就有几十上百轨**：转 WAV 时每轨
一个文件（`01_xxx.wav`、`02_xxx.wav`…）。用户想当整张专辑连着听，就得手动
一个个拖进播放器。这个模块把「同一个源产出的那批 WAV」按顺序拼成一个文件。

## 规则（都是硬约定，改之前先想清楚）

* **格式必须一致**（PCM / 声道数 / 采样率 / 位深）。本项目产物恒为
  44100Hz / 16bit / 立体声，但这里**不假设** —— 读每个文件的 `fmt ` 块校验，
  不一致就整批拒绝并说明原因（宁可不动，也不要拼出一个坏文件）。
* **只拼 `data` 块**：`LIST` / `fact` / `cue ` 这些元数据对拼接没有意义，
  一律丢弃；`fmt ` 块原样沿用第一个文件的（可能是 16/18/40 字节，
  WAVE_FORMAT_EXTENSIBLE 也照抄，不自作聪明重写）。
* **曲间插静音**（默认 0.5s）：不留间隔的话，上一首的余响会直接钻进下一首
  的开头，听感上像"卡了一下"。
* **流式复制**（1MB 块）：256 轨 HES 合出来能到几个 GB，**绝不能**整段读进
  内存。实测 44 首 / 61MB 合并约 0.2 秒（纯磁盘拷贝，无重编码）。
* **失败不留垃圾**：先写 `<out>.part`，成功才 `os.replace` 到目标名。

## 不做什么

* 不重采样、不改音量、不做淡入淡出 —— 那些是解码阶段的事
  （`spc_wav` / `chip_decode` 已经按 policy 处理过）。
* 不删源文件。合并只是**多产出一个文件**，分轨一个不动。
"""

import os
import struct

#: 默认曲间静音（毫秒）。0 = 严格首尾相接。
DEFAULT_GAP_MS = 500

#: 合并文件名的后缀标记（``<源名>（合并）.wav``）。
#: ⚠ 命名规则只有这一处定义：上传云盘时要靠它把「合并文件」和「分轨」区分开
#: （两者内容重复，全传会占双倍空间），见 ``msu1_gui.plan_cloud_upload``。
MERGE_TAG = "（合并）"

#: 拷贝块大小。太小 syscall 多，太大对 SSD 没意义。
_COPY_CHUNK = 1 << 20

#: 允许的 WAV format tag：1 = PCM，0xFFFE = WAVE_FORMAT_EXTENSIBLE
_PCM_TAGS = (1, 0xFFFE)


class _Cancelled(Exception):
    """内部用：合并过程中被取消。"""


def _parse(fh):
    """解析一个已打开的 WAV，返回 ``(fmt_body, [(offset, size)…], info)``。

    ``data`` 段可能不止一个（少见但合法），全部按出现顺序返回。
    ``RIFF`` 里的长度字段不可信（有工具写成 0/0xFFFFFFFF），一律以**真实
    文件长度**为上限截断，免得 seek 到文件尾之后读出空数据还当成成功。
    """
    fh.seek(0)
    head = fh.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise ValueError("不是 RIFF/WAVE 文件")
    total = fh.seek(0, os.SEEK_END)
    fmt_body = b""
    datas = []
    pos = 12
    while pos + 8 <= total:
        fh.seek(pos)
        hdr = fh.read(8)
        if len(hdr) < 8:
            break
        cid, csize = hdr[:4], struct.unpack("<I", hdr[4:8])[0]
        body = pos + 8
        if cid == b"fmt " and not fmt_body:
            fh.seek(body)
            fmt_body = fh.read(min(csize, 64))
        elif cid == b"data":
            datas.append((body, max(0, min(csize, total - body))))
        # chunk 尾部按偶数对齐（不足补 1 字节）
        pos = body + csize + (csize & 1)
    if len(fmt_body) < 16:
        raise ValueError("缺少 fmt 块")
    if not datas:
        raise ValueError("缺少 data 块")
    tag, ch, rate, _brate, align, bits = struct.unpack("<HHIIHH", fmt_body[:16])
    if tag not in _PCM_TAGS:
        raise ValueError("不是 PCM（format tag=%d）" % tag)
    if ch <= 0 or rate <= 0 or bits <= 0:
        raise ValueError("fmt 块字段非法（ch=%d rate=%d bits=%d）"
                         % (ch, rate, bits))
    if align <= 0:
        align = max(1, ch * bits // 8)
    return fmt_body, datas, {"channels": ch, "rate": rate, "bits": bits,
                             "align": align}


def probe(path):
    """读一个 WAV 的参数与时长（秒）。读不了返回 ``None``，**不抛异常**。"""
    try:
        with open(path, "rb") as fh:
            _fmt, datas, info = _parse(fh)
    except (OSError, ValueError):
        return None
    nbytes = sum(n for _o, n in datas)
    out = dict(info)
    out["bytes"] = nbytes
    out["seconds"] = nbytes / float(max(1, info["align"] * info["rate"]))
    return out


def merge_target(out_dir, stem, tag=MERGE_TAG):
    """合并文件的固定路径 ``<out_dir>/<stem><tag>.wav``。

    ⚠ **故意不做重名规避**（原 v4.24 初版用 ``unique_path`` 加 (2)，实测
    重转一次就多一个「（合并） 2.wav」，越积越多）。这里与分轨的写盘语义
    保持一致：**同名即覆盖**。同一个源重转时，分轨和合并文件一起被刷新，
    目录里不会长出新垃圾。
    """
    return os.path.join(out_dir, "%s%s.wav" % (stem, tag))


def merge_wavs(paths, out_path, gap_ms=DEFAULT_GAP_MS, on_log=None,
               cancel=None):
    """把 ``paths`` 按给定顺序拼成一个 WAV。

    Args:
        paths: 源 WAV 绝对路径列表（顺序即拼接顺序）。少于 2 个直接拒绝。
        out_path: 输出路径（目录不存在会自动建）。
        gap_ms: 曲间静音毫秒，0 = 不加。
        on_log: 可选的日志回调（一行字）。
        cancel: 可选的 ``threading.Event``；置位后**放弃**并删掉半成品。

    Returns:
        ``{"ok": True, "path":…, "files":n, "frames":N, "bytes":B,
        "seconds":S, "gap_ms":g}``
        或 ``{"ok": False, "error": "人话原因"}``。**不抛异常。**
    """
    log = on_log or (lambda _t: None)
    paths = [str(p) for p in (paths or ()) if p]
    if len(paths) < 2:
        return {"ok": False, "error": "至少需要 2 个 WAV 才能合并"}

    fmt0 = b""
    base = None
    datas = []                     # [(path, [(offset, size)…])]
    for p in paths:
        try:
            with open(p, "rb") as fh:
                fmt, ds, info = _parse(fh)
        except OSError as exc:
            return {"ok": False,
                    "error": "%s：%s" % (os.path.basename(p), exc)}
        except ValueError as exc:
            return {"ok": False,
                    "error": "%s：%s" % (os.path.basename(p), exc)}
        if base is None:
            base, fmt0 = info, fmt
        else:
            for key, label in (("channels", "声道数"), ("rate", "采样率"),
                               ("bits", "位深")):
                if info[key] != base[key]:
                    return {"ok": False,
                            "error": "格式不一致（%s）：%s 是 %s，%s 是 %s"
                                     % (label, os.path.basename(paths[0]),
                                        base[key], os.path.basename(p),
                                        info[key])}
        datas.append((p, ds))

    align = base["align"]
    try:
        gap_ms = max(0.0, float(gap_ms or 0))
    except (TypeError, ValueError):
        gap_ms = 0.0
    gap_bytes = int(round(base["rate"] * gap_ms / 1000.0)) * align
    total = sum(n for _p, ds in datas for _o, n in ds) \
        + gap_bytes * (len(datas) - 1)

    out_dir = os.path.dirname(os.path.abspath(out_path))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": "建目录失败：%s" % exc}

    tmp = out_path + ".part"
    fmt_pad = fmt0 if len(fmt0) % 2 == 0 else fmt0 + b"\x00"
    try:
        with open(tmp, "wb") as w:
            # RIFF size = "WAVE" + fmt chunk + data chunk(含 payload)
            w.write(b"RIFF")
            w.write(struct.pack("<I", 4 + 8 + len(fmt_pad) + 8 + total))
            w.write(b"WAVE")
            w.write(b"fmt " + struct.pack("<I", len(fmt0)) + fmt_pad)
            w.write(b"data" + struct.pack("<I", total))
            silence = b"\x00" * gap_bytes if gap_bytes else b""
            for i, (p, ds) in enumerate(datas):
                if cancel is not None and cancel.is_set():
                    raise _Cancelled()
                if i and silence:
                    w.write(silence)
                with open(p, "rb") as fh:
                    for off, size in ds:
                        fh.seek(off)
                        left = size
                        while left > 0:
                            if cancel is not None and cancel.is_set():
                                raise _Cancelled()
                            buf = fh.read(min(_COPY_CHUNK, left))
                            if not buf:
                                raise OSError("源文件提前结束：%s"
                                              % os.path.basename(p))
                            w.write(buf)
                            left -= len(buf)
        os.replace(tmp, out_path)
    except _Cancelled:
        _silent_remove(tmp)
        return {"ok": False, "error": "已取消"}
    except OSError as exc:
        _silent_remove(tmp)
        return {"ok": False, "error": "写盘失败：%s" % exc}

    frames = total // max(1, align)
    res = {"ok": True, "path": out_path, "files": len(datas),
           "frames": frames, "bytes": total,
           "seconds": frames / float(max(1, base["rate"])),
           "gap_ms": gap_ms}
    log("🔗 已合并 %d 首 → %s（%s）"
        % (len(datas), os.path.basename(out_path),
           _human_secs(res["seconds"])))
    return res


def _silent_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _human_secs(secs):
    secs = max(0.0, float(secs or 0))
    if secs >= 3600:
        return "%d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60, secs % 60)
    return "%d:%04.1f" % (secs // 60, secs % 60)


if __name__ == "__main__":        # 手动试用: python wavmerge.py 出合并.wav a.wav b.wav…
    import sys
    if len(sys.argv) < 4:
        print("用法: python wavmerge.py <输出.wav> <输入1.wav> <输入2.wav> …")
        raise SystemExit(2)
    r = merge_wavs(sys.argv[2:], sys.argv[1], on_log=print)
    print(r)
    raise SystemExit(0 if r.get("ok") else 1)
