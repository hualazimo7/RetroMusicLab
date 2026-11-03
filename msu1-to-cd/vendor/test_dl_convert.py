#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_dl_convert.py — dl_convert 转换基座（T1）的离线自检。

**不调任何 UI、不起线程、不解码**：本文件只验纯逻辑，因此可以在没有
tkinter / 界面 的环境里秒跑完（T1 的核心价值就是把风险挡在 UI 之前）。

跑法::

    python vendor/test_dl_convert.py
    python vendor/test_dl_convert.py --dir C:/temp/gmev2   # 追加真实样本
    python vendor/test_dl_convert.py --decode              # 追加真实解码（需 DLL）

覆盖（对应设计 §8.3 T1 验收点 ①~⑥）：

1. :func:`dl_convert.policy_for` —— 11 种扩展名全覆盖分流；
2. :func:`dl_convert.is_convertible` —— 只认 ``CHIP_EXTS``（含 .cue/.bin/.mp3 反例）；
3. :class:`dl_convert.ConvertManifest` —— 记账 / 双校验 / 删 WAV 后重转 /
   坏文件 / 源被改 / 落盘失败不中断；
4. :func:`dl_convert.wav_scan_numbering` —— 续编号 + ``used`` 存不含 ``.wav``
   的 stem（**R7 的核心**：二次扫描不得覆盖用户已有 WAV）；
5. :func:`dl_convert.estimate_files` —— 逐文件 try/except，坏文件按 1 首计入；
6. **分流真的生效**（``--decode``）：同一 SPC 走两条 policy 产出 PCM 不一致。

测试目录**按 PID 隔离**（``dl_convert_test_<pid>``）：两个进程并发跑会互删
文件，报出像「解码坏了」的 FileNotFoundError。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chip_formats as cf                                # noqa: E402
import dl_convert as dc                                  # noqa: E402

_GREEN, _RED, _YEL, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _YEL = _RST = ""

_pass = 0
_fail = 0
_notes: list = []


def check(label, cond, detail=""):
    """记一条断言结果。"""
    global _pass, _fail
    if cond:
        _pass += 1
        print("  %sPASS%s %s" % (_GREEN, _RST, label))
    else:
        _fail += 1
        print("  %sFAIL%s %s%s" % (_RED, _RST, label,
                                   ("  <- " + detail) if detail else ""))
    return bool(cond)


def note(text):
    _notes.append(text)
    print("  %sNOTE%s %s" % (_YEL, _RST, text))


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _fake_track(name, secs=0.05):
    """造一条最小归一化音轨字典，够 :func:`spc_wav.write_tracks` 写盘。

    ``little_endian`` 必填 —— :func:`msu1_to_cd.pcm_le` 会读它，缺了就 KeyError。
    """
    raw = b"\x00\x01" * int(44100 * 2 * secs)
    return {"name": name, "pcm": raw, "little_endian": True, "sr": 44100}


def _touch_wav(path, body=b"RIFF"):
    """写一个最小 WAV 占位（只验记账，不校验音频内容）。"""
    with open(path, "wb") as fh:
        fh.write(body + b"\x00" * 40)


def make_spc(path, secs=4, fade_ms=0):
    """造一个最小的合法 SPC（SPC700 头 + ID666）。

    只需让 :func:`msu1_to_cd._spc_id666_title` 读得出 secs/fade —— 解码走
    libgme 时才会真正出声，本函数造的文件在 --decode 模式下会被跳过。
    """
    head = bytearray(0x100)
    head[0:33] = b"SNES-SPC700 Sound File Data\x1a\x1a\x1a"
    head[0x23:0x25] = struct.pack("<H", 0x00A5)      # 26 为奇数 -> reset 2A, x:A5
    head[0x25] = 0x00
    head[0x26:0x4E] = b"Test Track".ljust(0x28, b"\x00")[:0x28]
    head[0x4E:0x7E] = b"Test Game".ljust(0x28, b"\x00")[:0x28]
    head[0x7E:0x9E] = b"dumper".ljust(0x28, b"\x00")[:0x28]
    head[0x9E] = 0x1A                                   # 注释段标记
    head[0xA0:0xA9] = b"comment".ljust(0x28, b"\x00")[:0x28]
    head[0xA9:0xAC] = str(int(secs)).rjust(3).encode()[:3]
    head[0xAC:0xB1] = str(int(fade_ms)).rjust(5).encode()[:5]
    head[0xB1:0xB2] = b"0"
    head[0x2E:0x30] = struct.pack("<H", 0x00FF)
    with open(path, "wb") as fh:
        fh.write(bytes(head))
        fh.write(b"\x00" * 256)
    return path


def make_nsf(path, n_tracks=8):
    """造一个最小 NSF（解码一定失败没关系，本测试只验纯逻辑）。"""
    head = bytearray(0x100)
    head[0:4] = b"NESM\x1a"
    head[0x05] = 1
    head[0x06] = n_tracks
    head[0x0E:0x2E] = b"Test NSF".ljust(0x20, b"\x00")[:0x20]
    head[0x2E:0x4E] = b"tester".ljust(0x20, b"\x00")[:0x20]
    with open(path, "wb") as fh:
        fh.write(bytes(head))
    return path


def make_spc_many(path, n, start=1):
    """造 n 个不同名的小 SPC（验多文件续编号）。"""
    out = []
    for i in range(n):
        out.append(make_spc(os.path.join(path, "T%02d.spc" % (start + i))))
    return out


# ---------------------------------------------------------------------------
# 1) policy_for / is_convertible
# ---------------------------------------------------------------------------

def test_policy_for():
    print("\n[1] policy_for 分流（H1）与 is_convertible 过滤")
    check(".spc -> id666", dc.policy_for("x.spc") == "id666")
    check(".SPC(大写) -> id666", dc.policy_for("X.SPC") == "id666")
    for ext in (".nsf", ".nsfe", ".gbs", ".vgm", ".vgz",
                ".hes", ".kss", ".gym", ".sap", ".ay"):
        check("%-6s -> chip" % ext, dc.policy_for("x" + ext) == "chip",
              "got %s" % dc.policy_for("x" + ext))
    # 11 种扩展名全覆盖
    all_chip = set(cf.CHIP_EXTS)
    wrong = [e for e in all_chip if dc.policy_for("x" + e) !=
             ("id666" if e == ".spc" else "chip")]
    check("CHIP_EXTS 全覆盖分流", not wrong, "错: %s" % wrong)
    check("空路径不抛异常", dc.policy_for("") == "chip")
    check("None 不抛异常", dc.policy_for(None) == "chip")

    check("is_convertible(.nsf)", dc.is_convertible("a.nsf"))
    check("is_convertible(.spc)", dc.is_convertible("a.spc"))
    # 反例：绝不能用 collect_supported()（ALL_EXTS 含 cue/bin/mp3）
    for bad in ("a.cue", "a.bin", "a.mp3", "a.7z", "a.txt", "a", "a.iso"):
        check("is_convertible 拒绝 %s" % bad, not dc.is_convertible(bad))
    check("is_convertible 拒绝 None", not dc.is_convertible(None))

    # v5.1-web-psf：PSF 系必须**既分到 "psf" 又放行**。
    # 这两条锁在一起，因为只改一处会出现最难受的两种失败：
    #   只改 policy_for → 文件根本不入队（下载了不转，什么都不说）
    #   只改 is_convertible → 入队了但走 chip 分支 → libgme 报「不支持」
    # v5.4: PS2 的 `.psf2` / `.minipsf2` 也走同一套（psf2wav.exe 已支持
    # version 2，48000Hz，见 vendor/_check_psf2_ref.py 54/54 最大差 0）。
    for ext in (".psf", ".minipsf", ".psf1", ".minipsf1",
                ".psf2", ".minipsf2"):
        check("%-9s -> psf" % ext, dc.policy_for("x" + ext) == "psf",
              "got %s" % dc.policy_for("x" + ext))
        check("%-9s 放行" % ext, dc.is_convertible("x" + ext))

    # v5.4：外挂解码器四家族（USF / 2SF / SSF-DSF / GSF）—— 三件事必须同时成立，
    # 缺任何一件都会重现同一个真 bug：
    #   ① policy_for 返回对应 policy（否则文件不入队）
    #   ② is_convertible 放行（否则下载页说「转不了」）
    #   ③ ConvertWorker 的分派表里**真有这个分支**
    # ③ 是最容易被漏掉的：v5.4 之前 ① ② 都在，但 `_convert_one_inner` 的分派
    # 只有 cd/txtp/xa/psf/stream/id666，四个 policy 全落到 `else` → 被当成
    # libgme 的芯片格式喂进 spc_wav.convert → 零产出 → 用户看到
    # 「未产出任何 WAV（解码零产出，文件可能损坏或格式不受支持）」。
    # 实测 Akko ni Omakase! Brain Shock 的 8 个 .mini2sf 全中招，而
    # vio_native.decode_pcm 单独调是好的、vio2wav.exe 直接跑也出完整 WAV。
    emu_cases = (
        (".miniusf", "usf"), (".usf", "usf"),
        (".mini2sf", "vio"), (".2sf", "vio"),
        (".ssf", "ssf"), (".minissf", "ssf"),
        (".dsf", "ssf"), (".minidsf", "ssf"),
        (".gsf", "gsf"), (".minigsf", "gsf"),
    )
    for ext, want in emu_cases:
        check("%-10s -> %s" % (ext, want),
              dc.policy_for("x" + ext) == want,
              "got %s" % dc.policy_for("x" + ext))
        check("%-10s 放行" % ext, dc.is_convertible("x" + ext))

    # 分派表完整性（静态检查源码，不需要真实素材）
    import inspect as _inspect
    try:
        _disp_src = _inspect.getsource(dc.ConvertWorker._convert_one_inner)
    except (OSError, TypeError):
        _disp_src = ""
    check("能读到 _convert_one_inner 源码", bool(_disp_src))
    for pol in ("usf", "vio", "ssf", "gsf"):
        check('分派表含 policy == "%s"' % pol,
              ('policy == "%s"' % pol) in _disp_src)
    check("ConvertWorker._convert_emu 存在",
          hasattr(dc.ConvertWorker, "_convert_emu"))


# ---------------------------------------------------------------------------
# 2) ConvertManifest
# ---------------------------------------------------------------------------

def test_manifest(tmp):
    print("\n[2] ConvertManifest 双校验记账（FR-C8.2 / AC-C15）")
    wav_dir = os.path.join(tmp, "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src = make_spc(os.path.join(tmp, "src.spc"))
    wav1 = os.path.join(wav_dir, "01_Stage Clear.wav")
    with open(wav1, "wb") as fh:
        fh.write(b"RIFF" + b"\x00" * 40)

    man = dc.ConvertManifest(os.path.join(wav_dir, dc.MANIFEST_NAME))
    check("load() 空 manifest 返回 0", man.load() == 0)
    check("未 record -> already_done False", not man.already_done(src))
    check("record -> True", man.record(src, ["01_Stage Clear.wav"], "id666"))
    check("record 后 already_done True", man.already_done(src))

    # 落盘 -> 新实例能读回
    man2 = dc.ConvertManifest(os.path.join(wav_dir, dc.MANIFEST_NAME))
    n = man2.load()
    check("重新 load() 读到 1 条", n == 1, "got %d" % n)
    check("重新 load() 后 already_done True", man2.already_done(src))

    # ③ 删掉记录的 WAV -> 重转
    os.remove(wav1)
    check("删掉 WAV 后 already_done False", not man2.already_done(src))

    # ② 只改 mtime、内容不变 -> 重转（宁多转不误用）
    _touch_wav(wav1)
    man2.record(src, ["01_Stage Clear.wav"], "id666")
    check("重新 record 后 True", man2.already_done(src))
    os.utime(src, (time.time() + 10, time.time() + 10))
    check("改源文件 mtime 后 False", not man2.already_done(src))

    # ① 源被重下（size 变）-> 必须重转
    _touch_wav(wav1)
    man2.record(src, ["01_Stage Clear.wav"], "id666")
    check("重新 record 后 True(2)", man2.already_done(src))
    with open(src, "ab") as fh:
        fh.write(b"X" * 128)
    check("源文件 size 变后 False", not man2.already_done(src))

    # 源文件不存在 -> False
    check("源不存在 -> False",
          not man2.already_done(os.path.join(tmp, "nope.spc")))
    check("already_done(None) 不抛", man2.already_done(None) is False)

    # 多 WAV：全量探 —— 删掉一个也仍然 True（不重复转）
    wavs = ["01_A.wav", "02_B.wav", "03_C.wav"]
    for w in wavs:
        _touch_wav(os.path.join(wav_dir, w))
    src2 = make_spc(os.path.join(tmp, "multi.spc"))
    man2.record(src2, wavs, "chip")
    os.remove(os.path.join(wav_dir, "02_B.wav"))
    check("多 WAV 删中间一个仍 True（全量探，设计 §4.3）", man2.already_done(src2))
    for w in ("03_C.wav",):
        os.remove(os.path.join(wav_dir, w))
    check("多 WAV 只剩首个仍 True", man2.already_done(src2))
    os.remove(os.path.join(wav_dir, "01_A.wav"))
    check("多 WAV 全删 -> False（重转）", not man2.already_done(src2))

    # ---- B24: 三种写法的行为对照（本实现 = any-exists，且必须先判空）----
    # 另一种常见写法是 all-exists（全部 WAV 都在才算转过）。它有个致命坑：
    #     all([]) == True      <- Python 的空列表 all() 恒为 True
    # 若不先判 `wavs` 为空，any-exists/all-exists 都会把「一条记账都没有」
    # 判成「已转过」-> **永不重试**。本实现有两道防线：
    #   ① 指纹（size/mtime）不符先 return False
    #   ② wavs 非 list/tuple 或为空 -> return False
    man3 = dc.ConvertManifest(os.path.join(wav_dir, "m3.json"))
    man3._data["entries"]["ghost"] = {"mtime": 1.0, "size": -1,
                                     "wavs": [], "n_wav": 0}
    check("B24 空 wavs 列表不会被当成已转过（all([])==True 陷阱）",
          not man3.already_done("ghost"), "会永不重试")
    man3._data["entries"]["ghost2"] = {"mtime": 1.0, "size": -1,
                                      "wavs": None, "n_wav": 0}
    check("B24 wavs=None 也不会被当成已转过", not man3.already_done("ghost2"))
    check("B24 all([]) 确实是 True（所以必须显式判空）", all([]) is True)
    # 记账字段类型异常也不崩
    man3._data["entries"]["ghost3"] = "notadict"
    check("B24 entries 值类型异常时安全返回 False",
          not man3.already_done("ghost3"))

    # 坏 JSON / 版本不符 / 字段缺失 -> 全部未转过，且不抛
    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    m3 = dc.ConvertManifest(bad)
    check("坏 JSON load 返回 0", m3.load() == 0)
    check("坏 JSON 后 already_done False", not m3.already_done(src))
    with open(bad, "w", encoding="utf-8") as fh:
        json.dump({"version": 99, "entries": {"a": {}}}, fh)
    check("版本不符 load 返回 0", m3.load() == 0)
    with open(bad, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "entries": "notdict"}, fh)
    check("entries 非 dict 返回 0", m3.load() == 0)
    with open(bad, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "entries": {"a": 5, "b": {"wavs": []}}}, fh)
    check("混合类型 entries 容忍", m3.load() == 1)

    # 落盘失败只计数不抛（FR-C8.2）
    m4 = dc.ConvertManifest(os.path.join(tmp, "no_such_dir", "m.json"))
    check("落盘失败 record 不抛", m4.record(src, ["x.wav"], "chip") is True)
    check("落盘失败 errors 计数 >=1", m4.errors >= 1, "got %d" % m4.errors)
    check("无临时文件残留",
          not any(n.endswith(".tmp") for n in os.listdir(tmp)))

    # 源不存在时 record 返回 False（不记无源之账）
    m5 = dc.ConvertManifest(os.path.join(tmp, "m5.json"))
    check("源不存在 record 返回 False",
          not m5.record(os.path.join(tmp, "ghost.spc"), ["x.wav"]))

    # 键归一化：大小写不同视为同一条（Windows 不区分大小写）
    # ⚠ manifest 必须放在 **WAV 目录里** —— already_done 用 manifest 所在目录
    #    当 WAV 基目录（设计 §7.3 `base = dirname(self.path)`）。
    m6 = dc.ConvertManifest(os.path.join(wav_dir, "m6.json"))
    _touch_wav(os.path.join(wav_dir, "01_Stage Clear.wav"))
    m6.record(src, ["01_Stage Clear.wav"], "id666")
    check("大小写归一化命中", m6.already_done(src.upper()))
    check("__len__ 生效", len(m6) == 1)


# ---------------------------------------------------------------------------
# 3) wav_scan_numbering（R7）
# ---------------------------------------------------------------------------

def test_scan_numbering(tmp):
    print("\n[3] wav_scan_numbering 续编号 + used 键对齐（R7 / AC-C15）")
    wav_dir = os.path.join(tmp, "scanWAV")
    os.makedirs(wav_dir, exist_ok=True)
    check("空目录 -> (0, set())", dc.wav_scan_numbering(wav_dir) == (0, set()))
    check("不存在目录 -> (0, set())",
          dc.wav_scan_numbering(os.path.join(tmp, "nope")) == (0, set()))

    # 模拟用户第一次会话的产物：01_ ~ 37_
    for i in (1, 2, 9, 37):
        with open(os.path.join(wav_dir, "%02d_Song.wav" % i), "wb") as fh:
            fh.write(b"RIFF")
    # 无编号的裸名（write_tracks 只在有 track 名时产出，但用户可能手动放的）
    with open(os.path.join(wav_dir, "Bonus.wav"), "wb") as fh:
        fh.write(b"RIFF")
    # 非 wav，必须被忽略
    with open(os.path.join(wav_dir, dc.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        fh.write("{}")
    with open(os.path.join(wav_dir, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")

    max_idx, used = dc.wav_scan_numbering(wav_dir)
    check("max_idx = 37", max_idx == 37, "got %d" % max_idx)
    # ⚠⚠ 下面几条是「not X」形态 —— 如果 used 恰好为空, 它们会**全部恒真**。
    #   所以先用「有键」把非空性钉死, 后面的 not 断言才是真结论。
    #   （QA 对账时提出的同类问题: not 断言必须有正向对照/非空前提）
    # ⚠ 键会**合并**: 4 个 01_/02_/09_/37_Song.wav 剥掉编号后都是 "song"。
    #   所以是 2 个键不是 5 个 —— 这本身就是「used 存裸曲名」的直接证据。
    check("前提：used 非空（否则下面几条 not 断言会恒真）",
          len(used) == 2, "got %d: %s" % (len(used), sorted(used)))
    check("前提：4 个不同编号的同名 WAV 合并为 1 个键（证明剥了 NN_）",
          used == {"song", "bonus"}, str(sorted(used)))
    # ⚠⚠ 关键: used 必须是**裸曲名** —— 既不含 ".wav" 也**不含 "NN_" 前缀**
    #   （对齐 spc_wav.py:154 的 used.add(name.lower())，那里 name 是裸曲名）。
    #   架构师实测: 注入 '01_stage clear' -> 'Stage Clear' 查不到 -> 不避让 ✗
    #              注入 'stage clear'   -> 查到             -> 避让  ✓
    #   两种错写都不报错，只让重名避让静默失效。
    check("used 含裸曲名 'song'（序号+扩展名都要剥）", "song" in used,
          str(sorted(used)))
    check("used 不含 '01_song'（带序号前缀 = 错）", "01_song" not in used,
          str(sorted(used)))
    check("used 不含 '01_song.wav'（带扩展名 = 错）",
          "01_song.wav" not in used, str(sorted(used)))
    check("used 全部不含序号前缀",
          not any(u[0].isdigit() for u in used), str(sorted(used)))
    check("used 全部不含 .wav", not any(u.endswith(".wav") for u in used),
          str(sorted(used)))
    check("used 含裸名 'bonus'", "bonus" in used)
    check("used 不含 manifest 文件名", dc.MANIFEST_NAME.lower() not in used)
    check("used 不含 txt", not any(u.endswith(".txt") for u in used))

    # ---- R7 核心：二次转换不得覆盖第一次的产物 ----
    import spc_wav
    src = make_spc(os.path.join(tmp, "n2.spc"))
    sentinel = os.path.join(wav_dir, "01_Song.wav")
    # 故意用 25 字节的"用户旧数据"占位，模拟 team-lead 实测复现
    with open(sentinel, "wb") as fh:
        fh.write(b"OLD-USER-DATA-25BYTE")
    before = os.path.getsize(sentinel)
    before_blob = open(sentinel, "rb").read()
    max_idx, used = dc.wav_scan_numbering(wav_dir)
    sub = spc_wav.write_tracks([_fake_track("Song")], wav_dir, 0.0, None, None,
                               None, set(used), max_idx, None)
    check("续编号从 38 开始", sub["last_idx"] == 38,
          "got %s" % sub["last_idx"])
    # ---- C1（最小复现）: 哨兵**逐字节**未变 + 新文件**不得是 01_** ----
    check("C1 哨兵内容逐字节未变",
          open(sentinel, "rb").read() == before_blob)
    check("C1 哨兵大小未变", os.path.getsize(sentinel) == before,
          "%d -> %d" % (before, os.path.getsize(sentinel)))
    new_files = [os.path.basename(p) for p in sub["files"]]
    check("C1 新文件不得是 01_ 开头",
          all(not n.startswith("01_") for n in new_files), str(new_files))
    check("C1 新文件带 38_ 前缀",
          new_files and all(n.startswith("38_") for n in new_files),
          str(new_files))

    # ---- C2: 产出**必须**是 "NN_Stage Clear (2).wav"（文件名写死）----
    # ⚠ 为什么不能只断言「没覆盖」: 带序号注入这个错误**不会**导致覆盖,
    #   只让输出是 "02_Stage Clear.wav" —— 只断言「没覆盖」会让它溜过去。
    #   所以必须把期望文件名写死。
    sentinel2 = os.path.join(wav_dir, "02_Stage Clear.wav")
    with open(sentinel2, "wb") as fh:
        fh.write(b"OLD-USER-DATA-25BYTE-B")      # 22 字节
    before2 = open(sentinel2, "rb").read()
    max_idx2, used2 = dc.wav_scan_numbering(wav_dir)
    sub2 = spc_wav.write_tracks([_fake_track("Stage Clear")], wav_dir, 0.0,
                                None, None, None, set(used2), max_idx2, None)
    names2 = [os.path.basename(p) for p in sub2["files"]]
    check("C2 同名必须避让为 '(2)'",
          any(n.endswith("_Stage Clear (2).wav") for n in names2),
          "got %s（带序号注入的错误实现会产出 02_/39_Stage Clear.wav）"
          % names2)
    check("C2 哨兵 02_ 逐字节未变",
          open(sentinel2, "rb").read() == before2,
          "%d bytes" % len(before2))
    check("C2 编号续在 39（本文件拿到 39）", sub2["last_idx"] == 39,
          "got %s" % sub2["last_idx"])

    # ---- 同轮内两个同名轨 -> "(3)" / "(4)"（因为盘上已有裸名 "(2)"）----
    max_idx3, used3 = dc.wav_scan_numbering(wav_dir)
    sub3 = spc_wav.write_tracks(
        [_fake_track("Stage Clear"), _fake_track("Stage Clear")], wav_dir,
        0.0, None, None, None, set(used3), max_idx3, None)
    names3 = [os.path.basename(p) for p in sub3["files"]]
    check("同轮第一个同名 -> '(3)'",
          any(n.endswith("_Stage Clear (3).wav") for n in names3),
          str(names3))
    check("同轮第二个同名 -> '(4)'",
          any(n.endswith("_Stage Clear (4).wav") for n in names3),
          str(names3))
    check("本轮两轨编号连续（40/41）",
          sorted(int(n[:2]) for n in names3) == [40, 41], str(names3))


# ---------------------------------------------------------------------------
# 4) estimate_files
# ---------------------------------------------------------------------------

def test_estimate(tmp):
    print("\n[4] estimate_files 体量预估（FR-C2.4 / C2.5）")
    nsf = make_nsf(os.path.join(tmp, "a.nsf"), n_tracks=8)
    spc = make_spc(os.path.join(tmp, "b.spc"), secs=10)

    r = dc.estimate_files([nsf, spc], 150.0)
    check("统计 2 个文件", int(r["files"]) == 2, str(r))
    # SPC: ID666 10s × 1 轨；NSF: 无 exact -> 150s × 8 轨
    check("总秒数 ≈ 10 + 1200", abs(r["secs"] - 1210.0) < 1.0,
          "got %.1f" % r["secs"])
    check("总首数 ≈ 9", abs(r["tracks"] - 9.0) < 0.5, "got %.1f" % r["tracks"])
    check("体积 = 秒 × 176400",
          abs(r["bytes"] - r["secs"] * dc.BYTES_PER_SEC) < 1.0)

    # 反例：.mp3 / 无扩展名 / 目录不得计入（**绝不能用 collect_supported()**）
    r2 = dc.estimate_files(["x.mp3", "x.cue", "x.bin", "noext", tmp], 150.0)
    check("mp3/cue/bin/无扩展名/目录不计入", int(r2["files"]) == 0, str(r2))
    # 不存在的 .nsf：probe 不抛异常（chip_formats 内部已兜底），按 1 首计入。
    # GUI 预估时文件还没下完，所以「文件不存在」不能被跳过。
    r2b = dc.estimate_files([os.path.join(tmp, "ghost.nsf")], 150.0)
    check("不存在的 .nsf 计入 1 个 / 1 首",
          int(r2b["files"]) == 1 and r2b["tracks"] == 1.0, str(r2b))

    # HES 头虽只有 36 字节，probe 仍按协议报 256 轨（v3.9 实测口径）
    broken = os.path.join(tmp, "broken.hes")
    with open(broken, "wb") as fh:
        fh.write(b"HESM" + b"\x00" * 32)
    real = os.path.join(tmp, "real_dir")
    os.makedirs(real, exist_ok=True)
    shutil.copy(nsf, os.path.join(real, "ok.nsf"))
    r3 = dc.estimate_files([os.path.join(real, "ok.nsf"), broken], 150.0)
    check("坏文件不中断整批（后续文件仍统计）", int(r3["files"]) == 2, str(r3))
    check("HES 按 256 轨计（不为省事按 1 轨）",
          abs(r3["tracks"] - 264.0) < 0.5, str(r3))

    # probe() 抛异常 -> 按 1 首计入且**不中断整批**（FR-C2.5）
    ok2 = os.path.join(real, "ok2.nsf")
    shutil.copy(nsf, ok2)
    orig_probe = cf.probe
    calls = {"n": 0}

    def flaky_probe(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟 probe 崩溃")
        return orig_probe(path)

    cf.probe = flaky_probe
    try:
        r4 = dc.estimate_files([os.path.join(real, "ok.nsf"), ok2], 150.0)
    finally:
        cf.probe = orig_probe
    check("probe 抛异常被计入 bad", r4["bad"] == 1.0, str(r4))
    check("probe 抛异常按 1 首计入", r4["tracks"] == 9.0, str(r4))
    check("probe 抛异常不中断后续文件",
          int(r4["files"]) == 2 and abs(r4["secs"] - 1350.0) < 1.0, str(r4))

    check("空列表不抛", dc.estimate_files([], 150.0)["files"] == 0)
    check("None 不抛", dc.estimate_files(None, 150.0)["files"] == 0)
    check("chip_secs 非法不抛",
          dc.estimate_files([spc], -5)["files"] == 1)

    # probe_worker 只发事件、不碰 Tk
    got = []
    dc.probe_worker([nsf, spc], 150.0, lambda k, p: got.append((k, p)), "T1")
    check("probe_worker 发 cv_estimate", got and got[0][0] == "cv_estimate")
    check("probe_worker 回传 token", got and got[0][1].get("token") == "T1")

    def boom(_k, _p):
        raise RuntimeError("GUI 回调炸了")

    dc.probe_worker([nsf], 150.0, boom, None)          # 不得抛出
    check("probe_worker 吞掉回调异常", True)


# ---------------------------------------------------------------------------
# T6: HES 估算字段 n_tracks_used（架构师批准契约 v4.1）
# ---------------------------------------------------------------------------

def make_hes(path, slots=95, head_len=0x100):
    """造一个最小 HES。`slots` 写进 head[0x15]。

⚠ head[0x15] 是**容器尺寸字段**（每槽 256 字节），**不是轨数**。
参数名刻意叫 `slots` 而不叫 `tracks`/`used_tracks` —— 叫后者会重新引入
「0x15 = 占用轨数」这个已被实测推翻的错误语义。
"""
    h = bytearray(head_len)
    h[0:4] = b"HESM"
    if head_len > 0x15:
        h[0x15] = slots & 0xFF
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(bytes(h))
    return path


def test_n_tracks_used(tmp):
    print("\n[T6] head[0x15] 语义澄清：**预分配槽位，非轨数，禁止用于估算**")
    import chip_formats as cfmt

    # ---- 事实①: size == (0x15+1) × 256，7/7 成立 → 0x15 是预分配槽位数 ----
    real_dir = os.path.join(ROOT, "vendor", "samples", "hes")
    names = []
    if os.path.isdir(real_dir):
        names = [f for f in sorted(os.listdir(real_dir))
                 if f.lower().endswith(".hes")]
    check("存在真实 HES 样本用于验证", bool(names), "vendor/samples/hes 无 .hes")
    exact = 0
    for f in names:
        p = os.path.join(real_dir, f)
        i = cfmt.probe(p)
        if os.path.getsize(p) == (i.n_tracks_used + 1) * 256:
            exact += 1
    check("事实① size==(0x15+1)×256 全部成立（证明是预分配槽位）",
          names and exact == len(names),
          "%d/%d" % (exact, len(names)))

    # ---- 不变量: 无 m3u 时 n_tracks 仍是 256（解码分母）；
    #      v4.29 起有 m3u 时按「最大曲号+1」覆盖解码区间 ----
    for f in names[:3]:
        i = cfmt.probe(os.path.join(real_dir, f))
        if i.track_ids:
            check("有 m3u：n_tracks = 最大曲号+1（覆盖解码区间）",
                  i.n_tracks == min(256, max(i.track_ids) + 1),
                  "got %s ids=%s" % (i.n_tracks, i.track_ids[:6]))
        else:
            check("不变量：真实 HES 的 n_tracks 仍为 256",
                  i.n_tracks == 256, "got %s" % i.n_tracks)

    # ---- 事实②: 0x15 与有效轨数**不相关**（合成头仅测读取，不测估算）----
    for want in (63, 95, 127, 191, 255):
        p = make_hes(os.path.join(tmp, "hes", "h%d.hes" % want), want)
        i2 = cfmt.probe(p)
        check("0x15=%d 可读出" % want, i2.n_tracks_used == want,
              "got %s" % i2.n_tracks_used)
    p0 = make_hes(os.path.join(tmp, "hes", "zero.hes"), 0)
    check("0x15=0 -> 丢弃，字段留 0", cfmt.probe(p0).n_tracks_used == 0)
    pshort = make_hes(os.path.join(tmp, "hes", "short.hes"), 95, head_len=8)
    try:
        s2 = cfmt.probe(pshort)
        check("8 字节短头（0x15 越界）-> 留 0 且不抛（probe 契约）",
              s2.n_tracks_used == 0, "got %s" % s2.n_tracks_used)
    except Exception as e:                             # noqa: BLE001
        check("8 字节短头不抛异常", False,
              "%s: %s" % (type(e).__name__, e))

    # ---- 关键: 估算**必须**走 n_tracks(256)，不得用 n_tracks_used ----
    # 这条是回滚的回归锁。若有人再把 0x15 接回估算，本条立刻红。
    if names:
        p = os.path.join(real_dir, names[0])
        info = cfmt.probe(p)
        r = dc.estimate_files([p], 150.0)
        if info.track_ids:
            # v4.29：m3u 是曲目真源 -> 估算按真实曲目数，**仍然不碰 0x15**。
            check("关键回归锁：有 m3u 时按真实曲目数估（**不用** 0x15）",
                  r["tracks"] == float(len(info.track_ids)),
                  "got %s m3u=%s 0x15=%s"
                  % (r["tracks"], len(info.track_ids), info.n_tracks_used))
            check("有 m3u 时估算值 != 0x15（0x15 仍不得参与估算）",
                  float(r["tracks"]) != float(info.n_tracks_used),
                  "est=%s 0x15=%s" % (r["tracks"], info.n_tracks_used))
        else:
            check("关键回归锁：HES 估算走 n_tracks=256（**不用** 0x15）",
                  r["tracks"] == 256.0,
                  "got %s —— 若等于 n_tracks_used 说明 0x15 又被接回估算" % r["tracks"])
            # 方向性断言: 估算必须 >= 真实有效轨（宁可高估不可低估）
            check("方向性：估算值 >= 0x15（0x15 可能是有效轨的 2.4 倍）",
                  r["tracks"] >= info.n_tracks_used,
                  "est=%s 0x15=%s" % (r["tracks"], info.n_tracks_used))
    p2 = make_hes(os.path.join(tmp, "est", "x.hes"), 63)
    check("合成 HES 估算同样是 256（与 0x15=63 无关）",
          dc.estimate_files([p2], 150.0)["tracks"] == 256.0)
    nsf = make_nsf(os.path.join(tmp, "est", "a.nsf"), n_tracks=8)
    check("非 HES 格式不受影响（NSF 仍按 8 轨）",
          dc.estimate_files([nsf], 150.0)["tracks"] == 8.0)


def _raises(fn):
    """执行 fn，返回其抛出的异常类型（不抛则返回 None）。"""
    try:
        fn()
    except Exception as e:                             # noqa: BLE001
        return type(e)
    return None


# ---------------------------------------------------------------------------
# 5) 真实样本（可选）
# ---------------------------------------------------------------------------

def test_real_samples(sample_dir):
    print("\n[5] 真实样本分流（--dir）")
    if not sample_dir or not os.path.isdir(sample_dir):
        note("样本目录不存在，跳过: %s" % sample_dir)
        return
    n = 0
    bad = []
    for dirpath, dirnames, filenames in os.walk(sample_dir):
        dirnames[:] = [d for d in dirnames if d != "_rejected"]
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext not in cf.CHIP_EXTS:
                continue
            path = os.path.join(dirpath, f)
            n += 1
            want = "id666" if ext == ".spc" else "chip"
            got = dc.policy_for(path)
            if got != want:
                bad.append("%s: %s != %s" % (f, got, want))
            if not dc.is_convertible(path):
                bad.append("%s: is_convertible False" % f)
    check("真实样本 %d 个分流全部正确" % n, not bad, "; ".join(bad[:5]))
    # ⚠ 前提: n>0。「not bad」在扫到 0 个样本时是恒真的(bad=[]) —— 目录不存在
    #   会早退+note, 但「目录存在却没有受支持文件」这条路必须堵住, 否则
    #   「一个样本都没验到」会被当成「全部通过」。
    check("前提：确实扫到了真实样本（否则 not bad 恒真）", n > 0,
          "扫到 %d 个 CHIP_EXTS 样本" % n)
    sample = os.path.join(ROOT, "vendor", "sample.spc")
    if os.path.isfile(sample):
        info = cf.probe(sample)
        check("vendor/sample.spc 可读 ID666", info is not None
              and info.fmt == "SPC", repr(info))


# ---------------------------------------------------------------------------
# 6) 分流真的生效（--decode，需 libgme）
# ---------------------------------------------------------------------------

def test_policy_split_decodes(tmp):
    print("\n[6] 分流真的生效：同 SPC 两条 policy 产出 PCM 不一致（AC-C5）")
    src = os.path.join(ROOT, "vendor", "sample.spc")
    if not os.path.isfile(src):
        note("缺 vendor/sample.spc，跳过解码用例")
        return
    import spc_wav
    res = {}
    for policy in ("id666", "chip"):
        out = os.path.join(tmp, "split_%s" % policy)
        os.makedirs(out, exist_ok=True)
        try:
            sub = spc_wav.convert([src], out, policy, speed=1.0, max_secs=0.0,
                                  chip_secs=150.0)
        except Exception as exc:                       # noqa: BLE001
            note("%s 转换异常: %s" % (policy, exc))
            return
        if not sub.get("files"):
            note("%s 无产出（解码失败?）: %s" % (policy, sub.get("errors")))
            return
        with open(sub["files"][0], "rb") as fh:
            blob = fh.read()
        head, body = blob[:44], blob[44:]
        res[policy] = (os.path.basename(sub["files"][0]),
                       os.path.getsize(sub["files"][0]), head, body)
    if len(res) != 2:
        return
    a, b = res["id666"], res["chip"]
    check("两条 policy 文件名相同", a[0] == b[0], "%s vs %s" % (a[0], b[0]))
    check("两条 policy 字节数相同", a[1] == b[1], "%d vs %d" % (a[1], b[1]))
    check("两条 policy 头部 = RIFF/WAVE", a[2][:4] == b"RIFF"
          and a[2][8:12] == b"WAVE")
    n = min(len(a[3]), len(b[3]))
    check("PCM 长度足够比较", n > 1000, "n=%d" % n)
    diff = sum(1 for i in range(0, n - 1, 2) if a[3][i:i + 2] != b[3][i:i + 2])
    check("PCM 存在差异（证明分流生效）", diff > 0,
          "逐样本差异 %d 处" % diff)
    note("PCM 差异样本数 = %d / %d（预期 >0；=0 说明分流没生效）"
         % (diff, n // 2))
    # id666 走 ID666 标签的 fade（sample.spc 标 fade=0 -> 不淡出），
    # chip 走 chip_decode.py:234 的无条件 1500ms 淡出 -> **末尾应更小**。
    tail = int(dc.BYTES_PER_SEC * 1.0)          # 末 1 秒
    check("id666 末秒峰值 > chip 末秒峰值（不淡出 vs 无条件淡出）",
          _peak(a[3][-tail:]) > _peak(b[3][-tail:]),
          "id666=%d chip=%d" % (_peak(a[3][-tail:]), _peak(b[3][-tail:])))


def _peak(raw):
    """int16 小端字节流的峰值。Python 3.13 起 audioop 已移出标准库，自算。"""
    import array
    a = array.array("h")
    a.frombytes(raw[: len(raw) & ~1])
    return max(max(a), -min(a)) if a else 0


def main():
    ap = argparse.ArgumentParser(description="dl_convert 基座自检")
    ap.add_argument("--dir", default="", help="追加真实样本目录")
    ap.add_argument("--decode", action="store_true",
                    help="追加真实解码用例（需 libgme + vendor/sample.spc）")
    args = ap.parse_args()

    # 测试目录按 PID 隔离：两个进程并发跑会互删文件，
    # 报出像「解码坏了」的 FileNotFoundError。
    tmp = tempfile.mkdtemp(prefix="dl_convert_test_%d " % os.getpid())
    t0 = time.time()
    print("临时目录: %s" % tmp)
    try:
        test_policy_for()
        test_manifest(tmp)
        test_scan_numbering(tmp)
        test_estimate(tmp)
        test_n_tracks_used(tmp)
        if args.dir or args.decode:
            test_real_samples(args.dir)
        if args.decode:
            test_policy_split_decodes(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    dt = time.time() - t0
    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项  (%.1fs)" % (_pass, _fail, dt))
    if _notes:
        print("备注:")
        for t in _notes:
            print("  - " + t)
    print("=" * 60)
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
