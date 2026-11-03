#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_disc_cue.py — 光盘镜像（CUE+BIN）→ WAV 全链路验证。

覆盖的是**主窗口真实链路**：
``App._kind_of_file`` → ``ConvertWorker._do_other`` → ``core.load_source``
→ ``extract_disc_audio`` → ``spc_wav.write_tracks``。

不需要真实 PS1 镜像：现造合成镜像（2352 字节/扇区、大端 CD-DA、正弦波），
轨长已知，因此可以逐轨核对秒数。

⚠ 这条路径此前**零测试覆盖**（原有测试只断言 dl_convert 拒绝 .cue）。
   2026-10-20 补：顺带修掉「光盘镜像音轨被『单曲最长』截断」——
   抽出来的是完整曲目，不是循环 BGM，截断等于毁数据。

跑法：python vendor/test_disc_cue.py
"""

import math
import os
import queue
import shutil
import struct
import sys
import tempfile
import threading
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
# 主窗 import 时会读 sys.argv 里的开关，别让测试参数漏进去
sys.argv = ["msu1_gui.py", "--no-drop"]

import msu1_gui as g          # noqa: E402
import msu1_to_cd as core     # noqa: E402

_GREEN, _RED, _RST = "\033[32m", "\033[31m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _RST = ""

_pass = 0
_fail = 0

SECTOR = 2352
RATE = 44100
FRAMES_PER_SECTOR = SECTOR // 4          # 588 帧/扇区
FRAME = 75                               # CD 的「帧」= 1/75 秒
SECTOR_SECS = FRAMES_PER_SECTOR / RATE   # 1 扇区 = 13.3ms（轨长量化的最小单位）


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  {_GREEN}PASS{_RST} {name}" + (f"  {detail}" if detail else ""))
    else:
        _fail += 1
        print(f"  {_RED}FAIL{_RST} {name}  {detail}")
    return bool(cond)


# ---------------------------------------------------------------------------
# 合成素材
# ---------------------------------------------------------------------------
def write_audio_sectors(fh, secs, freq=440):
    """往 fh 追加一段大端 16bit/44.1k/立体声 CD-DA，按扇区对齐。"""
    n = int(RATE * secs)
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * freq * i / RATE))
        fh.write(struct.pack(">hh", v, v))
    for _ in range((-n) % FRAMES_PER_SECTOR):
        fh.write(b"\x00" * 4)
    return n


def write_data_sectors(fh, secs):
    """往 fh 追加一段「数据轨」扇区（内容无所谓，只要不是音频）。"""
    n = int(secs * FRAME)
    for _ in range(n):
        fh.write(b"\x00" * SECTOR)
    return n


_CD_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def write_xa_sectors(fh, secs, file_no=1, channel=1, coding=0x01):
    """往 fh 追加 XA-ADPCM 扇区（MODE2/2352），返回扇区数。

    内容是**全零 nibble**（filter=0 / shift=12 -> 采样恒为 0），所以解出来
    是纯静音 —— 这里只为验证「扇区头能不能被认出是 XA」，不需要真音频。
    """
    n = max(1, int(round(secs / core._XA_SECTOR_SECS)))
    sub = bytes([file_no, channel, 0x64, coding])      # 0x64 = Form2 + Audio
    group = bytes([0x0C]) * 16 + bytes(112)            # filter0 / shift12
    body = group * 18 + bytes(2324 - 18 * 128)
    for _ in range(n):
        # 4B 头 = 分:秒:帧:模式；第 4 字节必须是 2（MODE2），否则扫不出来
        fh.write(_CD_SYNC + bytes([0x00, 0x00, 0x01, 0x02]) + sub + sub)
        fh.write(body + b"\x00" * 4)
    return n


def msf(sectors):
    return f"{sectors // (60 * FRAME):02d}:{sectors // FRAME % 60:02d}:{sectors % FRAME:02d}"


def make_cue(path, entries):
    """entries: [(file_name, [(track_num, type, start_sector, title)])]"""
    lines = []
    for fname, tracks in entries:
        lines.append(f'FILE "{fname}" BINARY')
        for num, typ, start, title in tracks:
            lines.append(f"  TRACK {num:02d} {typ}")
            if title:
                lines.append(f'    TITLE "{title}"')
            lines.append(f"    INDEX 01 {msf(start)}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def wav_info(path):
    with wave.open(path, "rb") as h:
        return {
            "secs": h.getnframes() / h.getframerate(),
            "ch": h.getnchannels(),
            "bits": h.getsampwidth() * 8,
            "rate": h.getframerate(),
            "peak": _peak(path),
        }


def _peak(path):
    with wave.open(path, "rb") as h:
        n = h.getnframes()
        h.setpos(0)
        raw = h.readframes(min(n, RATE * 2))
    if not raw:
        return 0
    vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
    return max(abs(v) for v in vals)


def run_worker(src_path, kind, out_dir, max_secs=0.0, do_cd=False):
    """按主窗真实参数跑一遍 ConvertWorker（同步，不起线程）。"""
    q = queue.Queue()
    opts = {
        "do_cd": do_cd, "do_wav": True, "out_dir": out_dir,
        "album": "", "performer": "", "gap": 2, "verify": False,
        "cd_wav": False, "policy": "id666", "policy_label": "id666",
        "speed": 1.0, "max_secs": max_secs, "chip_secs": 150.0,
        "total_tracks": 0, "est_secs": 0.0, "cur_kind": "", "merge_wav": False,
    }
    src = [{"label": os.path.splitext(os.path.basename(src_path))[0],
            "path": src_path, "kind": kind, "iid": "s1", "prefix": "💿",
            "fmt": "", "n_tracks": 0, "est_secs": 0.0, "total_secs": 0.0,
            "exact": True, "dur_src": "", "size": 0, "ok": True,
            "warn": False, "cd_tracks": 0, "cd_secs": 0.0}]
    w = g.ConvertWorker(src, opts, q, threading.Event())
    w.run()
    wavs = sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir)
                  if f.lower().endswith(".wav"))
    return wavs, [m for m in list(q.queue)]


def main():
    tmp = tempfile.mkdtemp(prefix="disc_cue_test_")
    try:
        # =============================================================
        print("\n=== ① 类型判定：.cue / .bin 归「光盘镜像」 ===")
        binp = os.path.join(tmp, "game.bin")
        cuep = os.path.join(tmp, "game.cue")
        with open(binp, "wb") as fh:
            write_audio_sectors(fh, 2.0)
            write_audio_sectors(fh, 1.0)
        make_cue(cuep, [("game.bin", [(1, "AUDIO", 0, "Title A"),
                                      (2, "AUDIO", 150, "Title B")])])
        check(".cue 判为光盘镜像", g.App._kind_of_file(cuep)[0] == "光盘镜像")
        check(".bin 判为光盘镜像", g.App._kind_of_file(binp)[0] == "光盘镜像")
        check("前缀是 💿", g.App._kind_of_file(cuep)[1] == "💿")

        # =============================================================
        print("\n=== ② 抽轨：只取 AUDIO，轨长/字节序正确 ===")
        tracks = core.load_source(cuep)
        check("抽出 2 条音轨", len(tracks) == 2, f"实际 {len(tracks)}")
        secs = [len(t["pcm"]) / 4 / RATE for t in tracks]
        check("轨 1 = 2.000s", abs(secs[0] - 2.0) < 0.001, f"{secs[0]:.4f}s")
        check("轨 2 = 1.000s", abs(secs[1] - 1.0) < 0.001, f"{secs[1]:.4f}s")
        check("标记为大端（CD-DA 原生）", all(not t["little_endian"] for t in tracks))
        check("曲名来自 CUE TITLE",
              [t["name"] for t in tracks] == ["Title A", "Title B"],
              str([t["name"] for t in tracks]))

        # CUE 没有 TITLE 时，轨名必须是「镜像名 + 轨号」（v4.35），
        # 不能是 track_02 这种看不出是哪张盘的名字
        # （放子目录，避免污染 tmp 根目录里「多 CUE 目录」那条断言）
        naming = os.path.join(tmp, "naming")
        os.makedirs(naming)
        with open(os.path.join(naming, "Sangokushi (Japan) (Track 2).bin"), "wb") as fh:
            write_audio_sectors(fh, 1.0)
        plain_cue = os.path.join(naming, "Sangokushi (Japan).cue")
        make_cue(plain_cue, [("Sangokushi (Japan) (Track 2).bin",
                              [(2, "AUDIO", 0, "")])])
        pt = core.load_source(plain_cue)
        check("无 TITLE 时轨名 = 镜像名 + 轨号",
              len(pt) == 1 and pt[0]["name"] == "Sangokushi (Japan) - 02",
              str([t["name"] for t in pt]))
        out = os.path.join(tmp, "out_named")
        os.makedirs(out)
        wavs, _ = run_worker(plain_cue, "光盘镜像", out)
        check("产出文件名带镜像名与轨号",
              len(wavs) == 1 and "Sangokushi (Japan) - 02" in os.path.basename(wavs[0]),
              str([os.path.basename(w) for w in wavs]))

        # =============================================================
        print("\n=== ③ 主窗全链路 → WAV ===")
        out = os.path.join(tmp, "out1")
        os.makedirs(out)
        wavs, msgs = run_worker(cuep, "光盘镜像", out)
        check("产出 2 个 WAV", len(wavs) == 2, str([os.path.basename(w) for w in wavs]))
        check("日志没有「未找到音轨」",
              not any("未找到音轨" in str(m) for m in msgs))
        infos = [wav_info(w) for w in wavs]
        check("轨 1 时长 2.000s", abs(infos[0]["secs"] - 2.0) < 0.01,
              f"{infos[0]['secs']:.3f}s")
        check("轨 2 时长 1.000s", abs(infos[1]["secs"] - 1.0) < 0.01,
              f"{infos[1]['secs']:.3f}s")
        check("WAV = 2ch/16bit/44100Hz",
              all(i["ch"] == 2 and i["bits"] == 16 and i["rate"] == 44100
                  for i in infos),
              str([(i["ch"], i["bits"], i["rate"]) for i in infos]))
        check("非静音（峰值 > 300）", all(i["peak"] > 300 for i in infos),
              str([i["peak"] for i in infos]))

        # =============================================================
        print("\n=== ④ 「单曲最长」不得截断镜像音轨（本次修的缺陷）===")
        out = os.path.join(tmp, "out2")
        os.makedirs(out)
        wavs, _ = run_worker(cuep, "光盘镜像", out, max_secs=1.0)
        got = [wav_info(w)["secs"] for w in wavs]
        check("设 1.0s 上限后轨 1 仍是 2.000s",
              len(got) == 2 and abs(got[0] - 2.0) < 0.01,
              f"实际 {[round(x, 3) for x in got]}")
        check("设 1.0s 上限后轨 2 仍是 1.000s",
              len(got) == 2 and abs(got[1] - 1.0) < 0.01)

        # =============================================================
        print("\n=== ⑤ 混合镜像：数据轨跳过，只取 CD-DA ===")
        mixbin = os.path.join(tmp, "mix.bin")
        mixcue = os.path.join(tmp, "mix.cue")
        with open(mixbin, "wb") as fh:
            write_data_sectors(fh, 2.0)              # 150 扇区数据轨
            write_audio_sectors(fh, 1.5)             # 音频轨
        make_cue(mixcue, [("mix.bin", [(1, "MODE1/2352", 0, "Data"),
                                       (2, "AUDIO", 150, "BGM")])])
        mt = core.load_source(mixcue)
        check("只抽出 1 条（数据轨被跳过）", len(mt) == 1, f"实际 {len(mt)}")
        # ⚠ CD 是**扇区**（2352 字节 = 588 帧）为单位的，轨尾必须补齐到整扇区，
        #   所以非整扇区的样本会比标称长最多 1 扇区（588/44100 = 13.3ms）。
        #   断言给 1 扇区余量，别按毫秒卡死。
        got = len(mt[0]["pcm"]) / 4 / RATE if mt else 0
        check("音频轨 ≈1.500s（起点按 INDEX 01 对齐，±1 扇区）",
              mt and abs(got - 1.5) <= SECTOR_SECS + 1e-6, f"{got:.4f}s")

        # =============================================================
        print("\n=== ⑥ 多文件 CUE（每轨一个 BIN）===")
        b1 = os.path.join(tmp, "t1.bin")
        b2 = os.path.join(tmp, "t2.bin")
        with open(b1, "wb") as fh:
            write_audio_sectors(fh, 1.0)
        with open(b2, "wb") as fh:
            write_audio_sectors(fh, 0.5, freq=660)
        mcue = os.path.join(tmp, "multi.cue")
        make_cue(mcue, [("t1.bin", [(1, "AUDIO", 0, "One")]),
                        ("t2.bin", [(2, "AUDIO", 0, "Two")])])
        mt = core.load_source(mcue)
        check("多文件 CUE 抽出 2 条", len(mt) == 2, f"实际 {len(mt)}")
        got = [len(t["pcm"]) / 4 / RATE for t in mt]
        check("轨长分别 ≈1.0s / ≈0.5s（±1 扇区）",
              len(mt) == 2 and abs(got[0] - 1.0) <= SECTOR_SECS + 1e-6
              and abs(got[1] - 0.5) <= SECTOR_SECS + 1e-6,
              str([round(x, 4) for x in got]))

        # =============================================================
        print("\n=== ⑦ 裸 .bin 无同名 .cue → 明确报错 ===")
        lonely = os.path.join(tmp, "lonely.bin")
        shutil.copyfile(binp, lonely)
        err = ""
        try:
            core.load_source(lonely)
        except Exception as e:                       # noqa: BLE001
            err = str(e)
        check("抛出可读错误", "cue" in err.lower() or "CUE" in err, err[:80])

        # =============================================================
        print("\n=== ⑧ 文件夹形态（v4.34 起支持）===")
        # 单独一个只放一套 cue+bin 的目录（PS1 dump 的典型形态）
        solo = os.path.join(tmp, "solo")
        os.makedirs(solo)
        shutil.copyfile(cuep, os.path.join(solo, "game.cue"))
        shutil.copyfile(binp, os.path.join(solo, "game.bin"))
        check("含 cue+bin 的目录判为光盘镜像",
              g.App._kind_of_dir(solo) == "光盘镜像", g.App._kind_of_dir(solo))
        dir_tracks = core.load_source(solo)
        check("目录 load_source 抽出 2 条", len(dir_tracks) == 2,
              f"实际 {len(dir_tracks)}")
        out = os.path.join(tmp, "out3")
        os.makedirs(out)
        wavs, _ = run_worker(solo, "光盘镜像", out)
        check("拖文件夹也能出 2 个 WAV", len(wavs) == 2,
              str([os.path.basename(w) for w in wavs]))
        check("拖文件夹产出时长正确",
              len(wavs) == 2 and abs(wav_info(wavs[0])["secs"] - 2.0) < 0.01,
              f"{wav_info(wavs[0])['secs']:.3f}s" if wavs else "无")

        # 一个目录里有多套 CUE（多张盘）→ 全部抽出来，不是只取第一张
        multi_dir_tracks = core.load_source(tmp)
        check("多 CUE 目录把每张盘的音轨都抽出来（此处 5 条）",
              len(multi_dir_tracks) == 5, f"实际 {len(multi_dir_tracks)}")

        # 只有 .cue 没有 .bin 的目录：必须**不**判成光盘镜像（那是描述 .ape 的
        # 播放列表形态，得落回 CD 音轨分支）
        onlycue = os.path.join(tmp, "onlycue")
        os.makedirs(onlycue)
        with open(os.path.join(onlycue, "list.cue"), "w", encoding="utf-8") as fh:
            fh.write('FILE "a.ape" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
        check("只有 .cue 的目录不判光盘镜像",
              g.App._kind_of_dir(onlycue) != "光盘镜像",
              g.App._kind_of_dir(onlycue))

        # =============================================================
        print("\n=== ⑨ CLI 入口（--convert）===")
        out = os.path.join(tmp, "out4")
        os.makedirs(out)
        rc = g.run_spc_wav(["--convert", out, cuep])
        cli_wavs = [f for f in os.listdir(out) if f.lower().endswith(".wav")]
        check("CLI 吃 .cue 并返回 0", rc == 0, f"rc={rc}")
        check("CLI 产出 2 个 WAV", len(cli_wavs) == 2, str(cli_wavs))
        check("_looks_like_disc 认 cue 文件", g._looks_like_disc(cuep))
        check("_looks_like_disc 认 cue+bin 目录", g._looks_like_disc(tmp))
        check("_looks_like_disc 拒绝 mp3", not g._looks_like_disc(
            os.path.join(tmp, "x.mp3")))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # =================================================================
    # ⑩ 字节序自动判定 + CUE 文件名兜底（v4.36）
    #
    # 背景：Red Book 规定 CD-DA 是大端，但现实里的 BIN 有相当一部分是小端
    # （抓轨/转档工具按本机字节序写出）。按大端硬读小端文件 = 把低字节的
    # 随机性搬到高位 = 满量程白噪，用户反馈的「刺耳的吱吱声」就是它。
    # 这里用小段合成素材把三条判定路径都钉住。
    # =================================================================
    print("\n=== ⑩ 字节序自动判定 / CUE 文件名兜底 ===")

    def build_bin(path, little, secs=2.0, freq=440, amp=12000, silence=False):
        """写一段指定字节序的 CD-DA（前面 150 扇区前导静音）。"""
        with open(path, "wb") as fh:
            fh.write(b"\x00" * (150 * SECTOR))
            n = int(RATE * secs)
            for i in range(n):
                v = 0 if silence else int(amp * math.sin(2 * math.pi * freq * i / RATE))
                fh.write(struct.pack("<hh" if little else ">hh", v, v))
            for _ in range((-n) % FRAMES_PER_SECTOR):
                fh.write(b"\x00" * 4)

    def smoothness(wav_path):
        """写出的 WAV 的「平均相邻差分 / RMS」：真音频远小于 1，白噪接近 1。"""
        with wave.open(wav_path, "rb") as h:
            n = h.getnframes()
            raw = h.readframes(min(n, RATE))
        vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
        if not vals:
            return 9.9
        rms = math.sqrt(sum(v * v for v in vals) / len(vals))
        d = sum(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))) / (len(vals) - 1)
        return d / rms if rms > 0 else 9.9

    with tempfile.TemporaryDirectory() as t2:
        # --- 小端镜像：必须判定为小端，且解出来是「音乐」而不是噪声 ---
        le_dir = os.path.join(t2, "le")
        os.makedirs(le_dir)
        build_bin(os.path.join(le_dir, "le.bin"), little=True)
        le_cue = os.path.join(le_dir, "le.cue")
        make_cue(le_cue, [("le.bin", [(1, "AUDIO", 150, "")])])
        le_tracks = core.extract_disc_audio(le_cue)
        check("小端镜像判定为小端", le_tracks[0]["little_endian"] is True)
        le_out = os.path.join(le_dir, "out")
        os.makedirs(le_out)
        wavs, _ = run_worker(le_cue, "光盘镜像", le_out)
        check("小端镜像产出 1 个 WAV", len(wavs) == 1, str(wavs))
        sm = smoothness(wavs[0]) if wavs else 9.9
        check("小端镜像解出的是音频（平滑度 < 0.35，不是白噪）", sm < 0.35, f"{sm:.3f}")
        pk = wav_info(wavs[0])["peak"] if wavs else 0
        check("小端镜像峰值与源一致（12000/32767≈0.366）",
              wavs and abs(pk / 32767.0 - 12000 / 32767.0) < 0.02,
              f"{pk/32767.0:.3f}")

        # --- 大端镜像：不能被误判成小端（回归） ---
        be_dir = os.path.join(t2, "be")
        os.makedirs(be_dir)
        build_bin(os.path.join(be_dir, "be.bin"), little=False)
        be_cue = os.path.join(be_dir, "be.cue")
        make_cue(be_cue, [("be.bin", [(1, "AUDIO", 150, "")])])
        be_tracks = core.extract_disc_audio(be_cue)
        check("大端镜像仍判定为大端", be_tracks[0]["little_endian"] is False)
        be_out = os.path.join(be_dir, "out")
        os.makedirs(be_out)
        wavs, _ = run_worker(be_cue, "光盘镜像", be_out)
        sm = smoothness(wavs[0]) if wavs else 9.9
        check("大端镜像解出的也是音频", sm < 0.35, f"{sm:.3f}")

        # --- 静音轨（真实 PS1 盘那种：一个字节恒 0xFF）---
        # 两种解释的自相关一样，只能靠 |DC| 裁决；关键是**不能**放出白噪
        sil_dir = os.path.join(t2, "sil")
        os.makedirs(sil_dir)
        sil_bin = os.path.join(sil_dir, "sil.bin")
        with open(sil_bin, "wb") as fh:
            fh.write(b"\x00" * (150 * SECTOR))
            rnd = 0
            for _ in range(int(RATE * 2)):
                rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
                lo = 0xF0 + (rnd % 16)          # 低字节抖动
                fh.write(bytes([lo, 0xFF, lo, 0xFF]))   # 高字节恒 0xFF（小端）
        sil_cue = os.path.join(sil_dir, "sil.cue")
        make_cue(sil_cue, [("sil.bin", [(1, "AUDIO", 150, "")])])
        sil_tracks = core.extract_disc_audio(sil_cue)
        check("静音轨按 |DC| 判为小端", sil_tracks[0]["little_endian"] is True)
        sil_out = os.path.join(sil_dir, "out")
        os.makedirs(sil_out)
        wavs, _ = run_worker(sil_cue, "光盘镜像", sil_out)
        spk = wav_info(wavs[0])["peak"] / 32767.0 if wavs else 1.0
        check("静音轨输出仍是静音（峰值 < 0.001，不是满量程白噪）",
              spk < 0.001, f"{spk:.5f}")

        # --- CUE 的 FILE 名与实际文件不同名 ---
        mm_dir = os.path.join(t2, "mismatch")
        os.makedirs(mm_dir)
        build_bin(os.path.join(mm_dir, "游戏音乐cd.bin"), little=True, secs=1.0)
        mm_cue = os.path.join(mm_dir, "游戏音乐cd.cue")
        make_cue(mm_cue, [("Image.bin", [(1, "AUDIO", 150, "A")])])
        check("CUE 写 Image.bin / 实际叫别的名，仍能定位",
              len(core.disc_track_plan(mm_cue)) == 1,
              f"实际 {len(core.disc_track_plan(mm_cue))}")
        check("错名兜底后仍抽出音轨", len(core.extract_disc_audio(mm_cue)) == 1)

        # 目录里有两个 bin 时，优先取与 CUE 同名的那个
        mm2 = os.path.join(t2, "mismatch2")
        os.makedirs(mm2)
        build_bin(os.path.join(mm2, "别的盘.bin"), little=True, secs=0.5)
        build_bin(os.path.join(mm2, "这张盘.bin"), little=True, secs=1.0)
        mm2_cue = os.path.join(mm2, "这张盘.cue")
        make_cue(mm2_cue, [("Image.bin", [(1, "AUDIO", 150, "A")])])
        plan2 = core.disc_track_plan(mm2_cue)
        check("多 bin 时取与 CUE 同名的那一个",
              len(plan2) == 1 and plan2[0]["path"].endswith("这张盘.bin"),
              plan2[0]["path"] if plan2 else "无")

        # =================================================================
        print("\n=== ⑪ 单文件 .iso 镜像：音频与数据轨交织（v4.38）===")
        # PCE-CD 的主流形态：一个 .iso 里按 CUE 交织存放音频轨和数据轨。
        # 两个必须同时成立的结论：
        #   ① 扩展名 .iso 也要被认成光盘镜像（以前只认 .bin，整包被判成
        #      MSU-1，最后报「未找到音轨」）；
        #   ② 切点必须按**下一轨（含数据轨）**算 —— 按「下一首 AUDIO」算，
        #      会把夹在中间的整条数据轨当音频读（听感：曲子中间插一段强噪声）。
        iso_dir = os.path.join(t2, "pcecd")
        os.makedirs(iso_dir)
        iso_path = os.path.join(iso_dir, "game.iso")      # 小写，CUE 里写大写
        rnd = 12345

        def _rnd_bytes(n):
            nonlocal rnd
            out = bytearray()
            for _ in range(n):
                rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
                out.append((rnd >> 16) & 0xFF)
            return bytes(out)

        with open(iso_path, "wb") as fh:
            write_audio_sectors(fh, 2.0, freq=440)        # 0..150   AUDIO
            fh.write(_rnd_bytes(150 * SECTOR))            # 150..300 MODE1（随机=噪声）
            write_audio_sectors(fh, 2.0, freq=660)        # 300..450 AUDIO
            fh.write(_rnd_bytes(150 * SECTOR))            # 450..600 MODE1
        iso_cue = os.path.join(iso_dir, "game.cue")
        with open(iso_cue, "w", encoding="utf-8") as fh:
            fh.write('FILE "game.ISO" BINARY\n'           # 大写，验大小写不敏感
                     "  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n"
                     "  TRACK 02 MODE1/2352\n    INDEX 01 00:02:00\n"
                     "  TRACK 03 AUDIO\n"
                     "    INDEX 00 00:03:50\n"              # pregap 起点在数据区里
                     "    INDEX 01 00:04:00\n"
                     "  TRACK 04 MODE1/2352\n    INDEX 01 00:06:00\n")

        check("目录（.cue + .iso）判为光盘镜像",
              g.App._kind_of_dir(iso_dir) == "光盘镜像",
              g.App._kind_of_dir(iso_dir))
        # CLI 的 --convert 走的是另一套判定（_looks_like_disc），别再漏一次
        check("CLI 认 .cue + .iso 文件夹", g._looks_like_disc(iso_dir) is True)
        check("CLI 认单个 .iso 文件", g._looks_like_disc(iso_path) is True)
        iplan = core.disc_track_plan(iso_cue)
        check("单文件镜像抽出 2 条 AUDIO 轨", len(iplan) == 2,
              str([(p["num"], p["start"], p["end"]) for p in iplan]))
        if len(iplan) == 2:
            check("轨 1 的结束点 = 下一轨(数据)起点 150，不是下一首 AUDIO 的 300",
                  iplan[0]["end"] == 150, f"end={iplan[0]['end']}")
            check("轨 3 的结束点 = 下一轨(数据)起点 450，不是文件尾 600",
                  iplan[1]["end"] == 450, f"end={iplan[1]['end']}")
            check("轨 3 从 INDEX 01=300 开始（INDEX 00=275 只是 pregap）",
                  iplan[1]["start"] == 300, f"start={iplan[1]['start']}")

        itracks = core.extract_disc_audio(iso_cue)
        check("抽出 2 条音轨", len(itracks) == 2, str(len(itracks)))
        if len(itracks) == 2:
            for i, t in enumerate(itracks, 1):
                nsec = len(t["pcm"]) // SECTOR
                check(f"轨 {i} 正好 2.0s（150 扇区，没混进数据轨）",
                      nsec == 150, f"{nsec} 扇区")
        # 目录形态：load_source 走同一条路
        check("拖文件夹也抽出 2 条", len(core.load_source(iso_dir)) == 2,
              str(len(core.load_source(iso_dir))))
        check("probe 报 2 轨 / 4.0s",
              core.probe_disc_audio(iso_dir)["n_tracks"] == 2
              and abs(core.probe_disc_audio(iso_dir)["total_secs"] - 4.0) < 0.1,
              str(core.probe_disc_audio(iso_dir)))

        # 端到端：写出的 WAV 里不能出现「数据噪声段」
        iso_out = os.path.join(iso_dir, "out")
        os.makedirs(iso_out)
        iwavs, _ = run_worker(iso_cue, "光盘镜像", iso_out)
        check("产出 2 个 WAV", len(iwavs) == 2, str([os.path.basename(w) for w in iwavs]))
        if iwavs:
            infos = [wav_info(w) for w in sorted(iwavs)]
            check("两个 WAV 都是 2.0s",
                  all(abs(i["secs"] - 2.0) < 0.02 for i in infos),
                  str([round(i["secs"], 3) for i in infos]))
            # 正弦峰值 12000/32767≈0.366；若混进随机数据，峰值会到 ~1.0
            check("峰值仍是正弦幅度（没混进满量程数据噪声）",
                  all(0.30 < i["peak"] / 32767.0 < 0.45 for i in infos),
                  str([round(i["peak"] / 32767.0, 3) for i in infos]))

        # =============================================================
        # ⑨ v4.43：**有 AUDIO 轨不等于有声音**。
        # PS1 盘常把音乐塞进数据轨的 XA，CD-DA 轨是整轨静音占位
        # （Sangokushi VI 实测 212.3s / 峰值 17）。以前列表按 CD-DA 报
        # 212.3s / 37.8MB，实际产出 276.7s / 46.6MB —— 两边对不上。
        # 这里钉住：静音 CD-DA 盘的估算必须改报 XA，且与实产一致。
        # =============================================================
        print("\n=== ⑨ 静音 CD-DA 盘的列表估算（v4.43）===")

        def _silent_audio(fh, secs, value=0):
            """整轨静音的 CD-DA（value=17 模拟真实占位轨的极小残值）。"""
            n = int(RATE * secs)
            for _ in range(n):
                fh.write(struct.pack(">hh", value, value))
            for _ in range((-n) % FRAMES_PER_SECTOR):
                fh.write(b"\x00" * 4)

        # 盘 A：有声 CD-DA（老口径必须不变）
        pa_dir = os.path.join(tmp, "probe_a")
        os.makedirs(pa_dir)
        with open(os.path.join(pa_dir, "a.bin"), "wb") as fh:
            write_audio_sectors(fh, 2.0)
        pa_cue = os.path.join(pa_dir, "a.cue")
        make_cue(pa_cue, [("a.bin", [(1, "AUDIO", 0, "Song")])])
        info_a = core.probe_disc_audio(pa_cue)
        check("有声 CD-DA 仍报 CD-DA（1 轨 / 2.0s）",
              info_a["n_tracks"] == 1 and abs(info_a["total_secs"] - 2.0) < 0.1,
              str(info_a))
        check("有声 CD-DA 的大小 = 该音轨文件字节数",
              info_a["size"] == os.path.getsize(os.path.join(pa_dir, "a.bin")),
              str(info_a["size"]))

        # 盘 B：数据轨带 4s XA + CD-DA 整轨静音 -> 实际产出的是 XA
        pb_dir = os.path.join(tmp, "probe_b")
        os.makedirs(pb_dir)
        pb_data = os.path.join(pb_dir, "b_data.bin")
        with open(pb_data, "wb") as fh:
            write_xa_sectors(fh, 4.0)
            write_data_sectors(fh, 1.0)
        with open(os.path.join(pb_dir, "b_audio.bin"), "wb") as fh:
            _silent_audio(fh, 2.0, value=17)
        pb_cue = os.path.join(pb_dir, "b.cue")
        make_cue(pb_cue, [("b_data.bin", [(1, "MODE2/2352", 0, "")]),
                          ("b_audio.bin", [(2, "AUDIO", 0, "")])])
        info_b = core.probe_disc_audio(pb_cue)
        xa_b = core.probe_xa_audio(pb_cue)
        check("静音 CD-DA 盘改报 XA 时长（不是 2.0s）",
              abs(info_b["total_secs"] - xa_b["total_secs"]) < 0.1,
              f"{info_b['total_secs']:.2f} vs XA {xa_b['total_secs']:.2f}")
        check("静音 CD-DA 盘改报 XA 流数（不是 1 条 AUDIO）",
              info_b["n_tracks"] == xa_b["groups"],
              f"{info_b['n_tracks']} vs {xa_b['groups']}")
        # 最要紧的一条：列表数字必须等于实际产出
        tb = core.load_source(pb_cue)
        real_secs = sum(len(t["pcm"]) for t in tb) / 4.0 / RATE
        check("列表时长 == 实际产出时长",
              abs(real_secs - info_b["total_secs"]) < 0.1,
              f"实产 {real_secs:.2f}s / 列表 {info_b['total_secs']:.2f}s")

        # 盘 C：静音 CD-DA 且数据轨里没有 XA -> 只能照旧报 CD-DA（不能报 0）
        pc_dir = os.path.join(tmp, "probe_c")
        os.makedirs(pc_dir)
        with open(os.path.join(pc_dir, "c_data.bin"), "wb") as fh:
            write_data_sectors(fh, 1.0)
        with open(os.path.join(pc_dir, "c_audio.bin"), "wb") as fh:
            _silent_audio(fh, 2.0, value=17)
        pc_cue = os.path.join(pc_dir, "c.cue")
        make_cue(pc_cue, [("c_data.bin", [(1, "MODE2/2352", 0, "")]),
                          ("c_audio.bin", [(2, "AUDIO", 0, "")])])
        info_c = core.probe_disc_audio(pc_cue)
        check("静音且无 XA 时报 CD-DA 数字（不报 0）",
              info_c["n_tracks"] == 1 and abs(info_c["total_secs"] - 2.0) < 0.1,
              str(info_c))

        # 字节序陷阱：静音轨的残值 17 按小端读会变成 4352（0.13 满刻度），
        # 那样静音轨会被误判成「有声轨」。抽块估峰值必须先判字节序。
        plan_c = core.disc_track_plan(pc_cue)
        pk_c = core._cdda_peak_quick(plan_c)
        check("静音轨抽块估峰值不被字节序骗（<0.005）",
              pk_c is not None and pk_c < core._CDDA_SILENT_PEAK,
              f"{pk_c}")

    print("\n" + "=" * 62)
    print(f"结果: {_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
