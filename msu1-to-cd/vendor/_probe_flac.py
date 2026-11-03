#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_probe_flac.py — v5.9.7 FLAC 输出的专项验证（两页共用一条链路）。

## 为什么单独一个探针

FLAC 是「只换容器、不换音频」的改动，看起来风险很低，但它踩中的全是
**只在真实转换时才暴露**的点：

  1. ``spc_wav.write_tracks`` 是主界面和下载页的**唯一写盘收口**。它同时
     被位置传参调用（``write_tracks(t, out, secs, log, prog, cancel, used,
     start, total)``）—— 新加的 ``fmt`` 一旦插在中间，log 会被当成 cancel
     传进去，症状是「转换跑完但没有日志、取消按钮失灵」。
  2. 下载页的 ``wav_scan_numbering`` 只扫 ``.wav``。FLAC 模式下它返回
     ``(0, set())``，于是第二次点下载时编号从 1 重来、重名不避让 ——
     **逐个覆盖上一轮转好的文件**（这正是 R7 那条 bug 的原样复发）。
  3. ``ConvertWorker`` 的 ``merge_wav`` 用的是标准库 ``wave``，喂 .flac
     会抛「不是 WAV」。不强制关掉就是每首合并失败刷一屏红字。
  4. 磁盘门禁 ``_estimate_bytes`` 若按 WAV 口径估 FLAC，会高估约 3 倍 →
     盘够却判「空间不足」→ 连续 3 轮后 ``cv_abandoned`` 放弃剩余曲目。
  5. 无损是**可验证的硬指标**：FLAC 解回来必须与原始 PCM 逐样本相同。
     ``sf.read(..., dtype="int16")`` 中间过一趟 float32 就会被加 dither，
     那时文件能播、时长也对，只是**不等于原文件**了。

用法::

    py -3.14 vendor/_probe_flac.py

⚠ 会真写盘（几 MB），全部落在 ``%TEMP%`` 下，跑完删掉。
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

_PASS = 0
_FAIL = 0


def mark(cond, label, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [OK] %s%s" % (label, ("  " + str(detail)) if detail else ""),
              flush=True)
    else:
        _FAIL += 1
        print("  [!!] %s%s" % (label, ("  " + str(detail)) if detail else ""),
              flush=True)
    return bool(cond)


def _stereo_pcm(secs=2.0, seed=5):
    """造一段**有真实频谱**的立体声 PCM（正弦 + 噪声），int16 LE 交织。

    纯静音或全零数据测不出压缩率，也测不出 dither 问题 —— 必须带噪声。
    """
    import numpy as np
    n = int(44100 * secs)
    t = np.arange(n, dtype=np.float64) / 44100.0
    x = (np.sin(2 * np.pi * 330 * t) * 8200
         + np.sin(2 * np.pi * 82 * t) * 4200
         + np.random.RandomState(seed).randint(-700, 700, n)).astype(np.int16)
    st = np.stack([x, x], axis=1)
    return st, st.astype("<i2").tobytes()


def main():
    import numpy as np
    import flac_out
    import spc_wav
    import dl_convert as dc

    tmp = tempfile.mkdtemp(prefix="flacprobe_")
    try:
        # ---- ① 编码器可用 ----
        print("\n[1] 编码器", flush=True)
        ok, why = flac_out.available()
        mark(ok, "flac_out.available() 为真", why)

        st, raw = _stereo_pcm()
        tracks = [{"name": "曲 A", "pcm": raw, "little_endian": True},
                  {"name": "曲 A", "pcm": raw, "little_endian": True}]

        # ---- ② write_tracks 默认仍是 WAV（向后兼容） ----
        print("\n[2] write_tracks 向后兼容", flush=True)
        dw = os.path.join(tmp, "wav")
        rw = spc_wav.write_tracks(tracks, dw, 0.0, None, None, None, None, 0,
                                  None)
        mark(rw["ok"] == 2 and all(p.endswith(".wav") for p in rw["files"]),
             "不传 fmt 仍出 2 个 .wav", [os.path.basename(p) for p in rw["files"]])

        # ---- ③ write_tracks(fmt="flac") ----
        print("\n[3] write_tracks(fmt=flac)", flush=True)
        df = os.path.join(tmp, "flac")
        rf = spc_wav.write_tracks(tracks, df, 0.0, None, None, None, None, 0,
                                  None, fmt="flac")
        mark(rf["ok"] == 2 and rf["fail"] == 0, "2 首全部写出",
             "ok=%d fail=%d" % (rf["ok"], rf["fail"]))
        mark(all(p.endswith(".flac") for p in rf["files"]),
             "扩展名是 .flac", [os.path.basename(p) for p in rf["files"]])
        mark(len({p.lower() for p in rf["files"]}) == 2,
             "同名曲目自动避让（不覆盖）",
             [os.path.basename(p) for p in rf["files"]])
        mark(any("(2)" in os.path.basename(p) for p in rf["files"]),
             "避让命名带 (2)", "")

        # ---- ④ 无损：解回来必须与原始 PCM 逐样本相同 ----
        print("\n[4] 无损判据", flush=True)
        import soundfile as sf
        first = sorted(rf["files"])[0]
        info = flac_out.probe_flac(first)
        mark(info is not None, "probe_flac 能读", info)
        if info:
            mark(info[0] == st.shape[0], "帧数与原始一致",
                 "%d vs %d" % (info[0], st.shape[0]))
            mark(info[1] == 44100 and info[2] == 2, "44100Hz / 2ch",
                 "%dHz %dch" % (info[1], info[2]))
        back, _sr = sf.read(first, dtype="int16", always_2d=True)
        mark(np.array_equal(back, st), "FLAC 解回 == 原始 PCM（逐样本）", "")

        # 与 WAV 解回也一致（证明「只换容器」）
        wav_first = sorted(rw["files"])[0]
        back_w, _ = sf.read(wav_first, dtype="int16", always_2d=True)
        mark(np.array_equal(back, back_w), "FLAC 解回 == WAV 解回", "")

        sz_f = os.path.getsize(first)
        sz_w = os.path.getsize(wav_first)
        mark(0.2 < sz_f / float(sz_w) < 0.6, "体积约为 WAV 的 35%",
             "%.1f%%" % (100.0 * sz_f / sz_w))

        # ---- ⑤ wav_scan_numbering 必须认 .flac（R7 复发点） ----
        print("\n[5] 编号续编（FLAC）", flush=True)
        mx, used = dc.wav_scan_numbering(df)
        mark(mx == 2, "max_idx 读到 2（否则第二轮从 1 重来）", mx)
        mark("曲 a" in used, "used 里有裸曲名（去序号去扩展名）",
             sorted(used))
        # 再写一批：编号必须接着 03 起，且不覆盖前两个
        before = {os.path.basename(p): os.path.getsize(p) for p in rf["files"]}
        rf2 = spc_wav.write_tracks(tracks, df, 0.0, None, None, None, used,
                                   mx, None, fmt="flac")
        names2 = [os.path.basename(p) for p in rf2["files"]]
        mark(all(n.startswith("03_") or n.startswith("04_") for n in names2),
             "第二批从 03 起编号", names2)
        after = {os.path.basename(p): os.path.getsize(p) for p in rf["files"]}
        mark(before == after, "第一批文件没被覆盖", "")

        # ---- ⑥ ConvertWorker：fmt 与 merge 的联动 ----
        print("\n[6] ConvertWorker", flush=True)
        wf = dc.ConvertWorker(os.path.join(tmp, "cv_flac"),
                              lambda *a: None, None,
                              merge_wav=True, fmt="flac")
        mark(wf._fmt == "flac", "_fmt = flac", wf._fmt)
        mark(wf.merge_wav is False,
             "FLAC 下 merge_wav 被强制关掉（wavmerge 读不了 flac）", wf.merge_wav)
        ww = dc.ConvertWorker(os.path.join(tmp, "cv_wav"),
                              lambda *a: None, None,
                              merge_wav=True, fmt="wav")
        mark(ww._fmt == "wav", "不传时 _fmt = wav", ww._fmt)
        mark(ww.merge_wav is True, "WAV 下 merge 保持用户选择", ww.merge_wav)
        wx = dc.ConvertWorker(os.path.join(tmp, "cv_bad"),
                              lambda *a: None, None, fmt="mp3")
        mark(wx._fmt == "wav", "未知格式退回 wav（不崩）", wx._fmt)

        # ---- ⑦ 磁盘门禁的体量预估要折算 ----
        print("\n[7] 体量预估", flush=True)
        est_w = dc.estimate_files([], 150.0)
        mark(est_w["bytes"] == 0.0, "空列表估 0", est_w["bytes"])
        # 用 _estimate_bytes 走真实路径：造一个 spc 让 probe 有东西可读
        spc = os.path.join(tmp, "e.spc")
        head = bytearray(0x100)
        head[0:33] = b"SNES-SPC700 Sound File Data\x1a\x1a\x1a"
        head[0x23:0x25] = b"\xa5\x00"
        head[0x4E:0x7E] = b"G".ljust(0x28, b"\x00")
        head[0x9E] = 0x1A
        head[0xA9:0xAC] = b"010"
        head[0xAC:0xB1] = b"00000"
        head[0xB1:0xB2] = b"0"
        head[0x2E:0x30] = b"\xff\x00"
        with open(spc, "wb") as fh:
            fh.write(bytes(head))
            fh.write(b"\x00" * 256)
        ww2 = dc.ConvertWorker(os.path.join(tmp, "cv_e"), lambda *a: None, None,
                               fmt="wav")
        wf2 = dc.ConvertWorker(os.path.join(tmp, "cv_e2"), lambda *a: None, None,
                               fmt="flac")
        bw = ww2._estimate_bytes(spc)
        bf = wf2._estimate_bytes(spc)
        mark(bw > 0, "WAV 预估 > 0", "%d B" % bw)
        mark(0 < bf < bw, "FLAC 预估小于 WAV（不被高估 3 倍）",
             "%d B vs %d B（%.0f%%）" % (bf, bw, 100.0 * bf / max(1, bw)))

        # ---- ⑧ spc_wav.convert 的 fmt 透传 ----
        print("\n[8] convert(fmt=) 透传", flush=True)
        seen = {}

        def fake_loader(paths, **kw):
            return [{"name": "L", "pcm": raw, "little_endian": True}]

        r = spc_wav.convert([spc], os.path.join(tmp, "cv2"), policy="chip",
                            fmt="flac", loader=fake_loader)
        mark(r["ok"] == 1, "chip 分支写出 1 首", "ok=%d" % r["ok"])
        mark(bool(r["files"]) and r["files"][0].endswith(".flac"),
             "convert 把 fmt 传到了 write_tracks",
             [os.path.basename(p) for p in r["files"]])

        # ---- ⑨ 异常路径：空 PCM 必须失败，且不留残留 ----
        print("\n[9] 失败路径", flush=True)
        bad = os.path.join(tmp, "empty.flac")
        ok3, _p, why3 = flac_out.encode_pcm(b"", bad)
        mark(not ok3, "空 PCM 报失败", why3)
        mark(not os.path.exists(bad), "失败时不留目标文件", "")
        mark(not os.path.exists(bad + ".part"), "也不留 .part", "")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60, flush=True)
    print("通过 %d 项，失败 %d 项" % (_PASS, _FAIL), flush=True)
    print("=" * 60, flush=True)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
