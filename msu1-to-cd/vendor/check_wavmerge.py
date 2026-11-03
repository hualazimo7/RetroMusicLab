#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_wavmerge.py — WAV 多轨合并门禁（纯离线，自己造样本）。

验的是**合并结果的字节级正确性**，不是"函数没崩"：
* 帧数 = 各源之和 + 间隔×（n-1），且用标准库 `wave` 独立复核；
* data 区逐字节等于「源data + 静音 + 源data…」；
* 间隔 0 时**严格等于**朴素拼接（这是最容易写错 1 个 chunk 头的地方）；
* 格式不一致 / 非 WAV / 少于 2 个 → 明确拒绝，**绝不产出半成品**；
* 取消后不留 `.part`；
* 源文件一个不许删。
"""

import os
import struct
import sys
import tempfile
import threading
import wave

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import wavmerge as wm      # noqa: E402

RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label,
                       ("  [" + str(detail) + "]") if detail else ""))
    return ok


def make_wav(path, frames, ch=2, rate=44100, bits=16, fill=None, extra_list=False):
    """造一个 PCM WAV；``fill`` 给 (左,右) 或单值，用于字节级比对。"""
    width = bits // 8
    if fill is None:
        fill = 1
    if isinstance(fill, int):
        vals = [fill] * (frames * ch)
    else:
        vals = [fill[i % len(fill)] for i in range(frames * ch)]
    if bits == 16:
        data = b"".join(struct.pack("<h", max(-32768, min(32767, v)))
                        for v in vals)
    else:
        data = b"".join(struct.pack("<B", v & 0xFF) for v in vals)
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(data)
    if extra_list:
        # 在 data 之后追加一个 LIST chunk（合法但没人看），验证解析会跳过
        with open(path, "rb") as fh:
            raw = fh.read()
        body = b"INFOISFT" + struct.pack("<I", 6) + b"smoke\x00\x00"
        chunk = b"LIST" + struct.pack("<I", len(body)) + body
        raw = raw + chunk
        raw = raw[:4] + struct.pack("<I", len(raw) - 8) + raw[8:]
        with open(path, "wb") as fh:
            fh.write(raw)
    return path


def read_data(path):
    """取 WAV 的 data 区（原始字节，不做任何解释）。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    pos = 12
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        if cid == b"data":
            return raw[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    return b""


def main():
    tmp = tempfile.mkdtemp(prefix="wavmerge_check_")
    a = make_wav(os.path.join(tmp, "a.wav"), 1000, fill=100)
    b = make_wav(os.path.join(tmp, "b.wav"), 1500, fill=-200)
    c = make_wav(os.path.join(tmp, "c.wav"), 700, fill=(300, -300),
                 extra_list=True)

    # ---- 1. probe --------------------------------------------------------
    p = wm.probe(a)
    check(p and p["channels"] == 2 and p["rate"] == 44100
          and p["bits"] == 16 and p["bytes"] == 1000 * 4,
          "1.1 probe 读出参数与字节数", p)
    check(abs(p["seconds"] - 1000 / 44100.0) < 1e-6,
          "1.2 probe 时长按帧数算", p["seconds"])
    check(wm.probe(os.path.join(tmp, "没有.wav")) is None,
          "1.3 probe 读不到返回 None 不抛")

    # ---- 2. 默认 0.5s 间隔 ----------------------------------------------
    out = os.path.join(tmp, "out", "merged.wav")
    r = wm.merge_wavs([a, b, c], out, gap_ms=500)
    check(r.get("ok") is True, "2.1 三个文件合并成功", r)
    gap_frames = int(round(44100 * 0.5))
    want_frames = 1000 + 1500 + 700 + gap_frames * 2
    check(r.get("frames") == want_frames, "2.2 帧数 = 各源帧数 + 间隔×2",
          "%s / %s" % (r.get("frames"), want_frames))
    with wave.open(out, "rb") as w:
        check(w.getnchannels() == 2 and w.getframerate() == 44100
              and w.getsampwidth() == 2, "2.3 头参数沿用源文件")
        check(w.getnframes() == want_frames, "2.4 wave 模块独立复核帧数",
              w.getnframes())

    # ---- 3. 字节级内容 ---------------------------------------------------
    data = read_data(out)
    da, db, dc = read_data(a), read_data(b), read_data(c)
    silence = b"\x00" * (gap_frames * 4)
    want = da + silence + db + silence + dc
    check(data == want, "3.1 data 逐字节 = 源+静音+源+静音+源",
          "%d B vs %d B" % (len(data), len(want)))
    check(os.path.getsize(out) == 44 + len(want),
          "3.2 RIFF 头长度自洽（44B + data）",
          "%d vs %d" % (os.path.getsize(out), 44 + len(want)))

    # ---- 4. gap=0 必须等于朴素拼接 ---------------------------------------
    out0 = os.path.join(tmp, "out", "nogap.wav")
    r0 = wm.merge_wavs([a, b], out0, gap_ms=0)
    check(r0.get("ok") and read_data(out0) == da + db,
          "4.1 间隔 0 时严等于直接拼接", r0.get("frames"))
    check(r0.get("frames") == 2500, "4.2 间隔 0 帧数 = 1000+1500",
          r0.get("frames"))

    # ---- 5. 拒绝坏输入 ---------------------------------------------------
    r2 = wm.merge_wavs([a], out, gap_ms=0)
    check(r2.get("ok") is False and "2 个" in r2.get("error", ""),
          "5.1 少于 2 个直接拒绝", r2.get("error"))
    mono = make_wav(os.path.join(tmp, "mono.wav"), 500, ch=1, fill=50)
    r3 = wm.merge_wavs([a, mono], os.path.join(tmp, "out", "bad.wav"))
    check(r3.get("ok") is False and "格式不一致" in r3.get("error", ""),
          "5.2 声道数不一致被拒", r3.get("error"))
    rate2 = make_wav(os.path.join(tmp, "r22k.wav"), 500, rate=22050)
    r4 = wm.merge_wavs([a, rate2], os.path.join(tmp, "out", "bad2.wav"))
    check(r4.get("ok") is False and "采样率" in r4.get("error", ""),
          "5.3 采样率不一致被拒", r4.get("error"))
    junk = os.path.join(tmp, "junk.wav")
    with open(junk, "wb") as fh:
        fh.write(b"NOT A WAV AT ALL")
    r5 = wm.merge_wavs([a, junk], os.path.join(tmp, "out", "bad3.wav"))
    check(r5.get("ok") is False and "RIFF" in r5.get("error", ""),
          "5.4 非 WAV 被拒且不抛", r5.get("error"))
    check(not os.path.exists(os.path.join(tmp, "out", "bad3.wav")),
          "5.5 拒绝时不留输出文件")
    check(not [n for n in os.listdir(os.path.join(tmp, "out"))
               if n.endswith(".part")],
          "5.6 拒绝时不留 .part 半成品")

    # ---- 6. merge_target：固定名 + 重复合并即覆盖 -------------------------
    udir = os.path.join(tmp, "u")
    os.makedirs(udir, exist_ok=True)
    t1 = wm.merge_target(udir, "Pack")
    check(os.path.basename(t1) == "Pack（合并）.wav",
          "6.1 合并名固定为「源名（合并）.wav」", os.path.basename(t1))
    ra = wm.merge_wavs([a, b], t1)
    nb1 = len(os.listdir(udir))
    rb = wm.merge_wavs([a, c], t1)
    nb2 = len(os.listdir(udir))
    check(ra.get("ok") and rb.get("ok") and nb1 == nb2 == 1,
          "6.2 再合并一次是覆盖，不长出「（合并） 2.wav」",
          "%d 个文件 / %s" % (nb2, os.listdir(udir)))
    check(rb.get("frames") == 1000 + 700 + int(round(44100 * 0.5)),
          "6.3 覆盖后内容是第二次的（帧数变了）", rb.get("frames"))

    # ---- 7. 取消 ---------------------------------------------------------
    ev = threading.Event()
    ev.set()
    r6 = wm.merge_wavs([a, b], os.path.join(tmp, "out", "cancel.wav"), cancel=ev)
    check(r6.get("ok") is False and "取消" in r6.get("error", ""),
          "7.1 已置位 cancel 直接取消", r6.get("error"))
    check(not os.path.exists(os.path.join(tmp, "out", "cancel.wav")),
          "7.2 取消后不留输出")

    # ---- 8. 源文件必须原样保留 -------------------------------------------
    check(os.path.isfile(a) and os.path.getsize(a) == 4000 + 44,
          "8.1 合并不删、不改源文件", os.path.getsize(a))

    # ---- 9. 多 data 块 ---------------------------------------------------
    # 手写一个含两个 data chunk 的 WAV，验证两个段都会被拼进去
    multi = os.path.join(tmp, "multi.wav")
    fmt = struct.pack("<HHIIHH", 1, 2, 44100, 44100 * 4, 4, 16)
    d1 = struct.pack("<4h", 1, 2, 3, 4)
    d2 = struct.pack("<4h", -1, -2, -3, -4)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(d1)) + d1
            + b"data" + struct.pack("<I", len(d2)) + d2)
    with open(multi, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", len(body)) + body)
    pm = wm.probe(multi)
    check(pm and pm["bytes"] == 16, "9.1 多个 data 块被合并计数", pm)

    # ---- 10. 清理 --------------------------------------------------------
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    bad = RESULTS.count(False)
    print("-" * 60)
    print("结果: %d 通过 / %d 失败" % (RESULTS.count(True), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
