# -*- coding: utf-8 -*-
"""XA-ADPCM 抽取门禁（v4.36）。

为什么需要这一套：
  很多 PS1 盘没有 CD-DA 音乐，音乐全在数据轨的 Form2 扇区里，用 XA-ADPCM
  编码；它们的 CD-DA 轨往往是**整轨静音**。只抽 CD-DA 会得到一条长静音，
  用户听不到原声（《三国志英杰传》SLPS-00309 就是这样）。

这里不依赖任何外部镜像，全部用合成扇区，并用**解析式**钉住解码：
  filter=0 时预测项为 0，于是 s = (nib << 12) >> shift
    shift=12 -> s 正好等于有符号 nibble (-8..7)
    shift=0  -> s = nib*4096
  这样 nibble 顺序（低半字节=左/子帧 2k、高半字节=右/子帧 2k+1）、
  符号扩展、shift 方向三件事都被钉死。

⚠ v4.42：合成器按**真实布局**编码（18 组 x 112 帧、每帧 t = win*28 + j）。
  旧版把 112B 当「4 unit x 28B」且相邻字节当相邻时刻，编出来的东西自己
  解得回去、但和真实 XA 完全不是一回事 —— 所以门禁全绿而用户听到的全是糊的。
  这一节现在逐值比对，就是为了防止再退回那个自洽但错误的模型。
"""
import array
import math
import os
import struct
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)

import msu1_to_cd as core  # noqa: E402

SECTOR = 2352
RATE = 44100
FRAME = 75

_pass = 0
_fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  PASS {name}  {detail}")
    else:
        _fail += 1
        print(f"  FAIL {name}  {detail}")
    return bool(cond)


# ---------------------------------------------------------------------------
# 合成 XA 扇区
# ---------------------------------------------------------------------------
SYNC = bytes([0x00] + [0xFF] * 10 + [0x00])


def make_xa_sector(samples_l, samples_r, shift=12, filt=0, file_no=1, channel=1):
    """把给定的 L/R 有符号 nibble 列表（长度须为 2016）编成一个 XA 扇区。

    按**真实布局**编码（见 msu1_to_cd.py 上方注释）：
      18 个 128B sound group，每组 112 帧 = 4 个 28 帧窗口；
      帧序号 t = win*28 + j；
      16B 头 = [sf0..sf3] + 同一份重复 + [sf4..sf7] + 同一份重复；
      112B 数据 = 28 个 word，word[j] 的第 k 字节 =
                 低 nibble 给子帧 2k（左）、高 nibble 给子帧 2k+1（右）。

    只支持 filter=0（纯 shift）——本测试就是要用这种情形做解析对照。
    """
    assert len(samples_l) == 2016 and len(samples_r) == 2016
    assert 0 <= filt <= 4
    par = ((filt & 0x0F) << 4) | (shift & 0x0F)
    payload = bytearray()
    for g in range(18):
        # 16B 头：[sf0..3][重复][sf4..7][重复] —— 本测试 8 个子帧参数相同
        hdr = bytearray([par] * 16)
        data = bytearray(112)
        for win in range(4):
            for j in range(28):
                # 第 g 组覆盖整扇区的第 g*112 .. g*112+111 帧
                t = g * 112 + win * 28 + j
                data[j * 4 + win] = (((samples_r[t] & 0x0F) << 4)
                                     | (samples_l[t] & 0x0F))
        payload += hdr + data
    assert len(payload) == 2304

    sub = bytes([file_no, channel, 0x64, 0x01]) * 2      # Form2|Audio, coding=01 立体声
    body = SYNC + bytes([0, 2, 0, 2]) + sub + bytes(payload)
    body += b"\x00" * (SECTOR - len(body))
    assert len(body) == SECTOR
    return body


def nibble_pattern(n=2016, freq=7.0):
    """一个有符号 nibble 序列（-8..7），不追求好听，只要能逐值比对。"""
    return [int(round(6.5 * math.sin(2 * math.pi * freq * i / n))) for i in range(n)]


def main():
    tmp = tempfile.mkdtemp(prefix="xa_test_")
    try:
        # =================================================================
        print("=== ① 解码器解析对照（filter=0，shift=12 -> 输出就是 nibble）===")
        L = nibble_pattern(2016, 7.0)
        R = [-v for v in L]
        sec = make_xa_sector(L, R, shift=12, filt=0)
        binp = os.path.join(tmp, "one.bin")
        with open(binp, "wb") as fh:
            fh.write(sec)
        groups = core.xa_sector_groups(binp)
        check("扫到 1 个 XA 组", len(groups) == 1, str(list(groups)))
        check("组键 = (file, channel)", list(groups)[0] == (1, 1), str(list(groups)))
        check("扇区偏移 = 0", groups[(1, 1)] == [0], str(groups.get((1, 1))))

        left, right = core._xa_decode_group(binp, groups[(1, 1)])
        check("每扇区每声道 2016 采样", len(left) == 2016, f"实际 {len(left)}")
        check("左声道逐采样 == nibble（钉住 nibble 顺序/符号/shift）",
              list(left) == L, f"前8 {list(left[:8])} vs {L[:8]}")
        check("右声道取高半字节",
              list(right) == R, f"前8 {list(right[:8])} vs {R[:8]}")

        # ★ v4.42 的核心回归点：**相邻 word 才是相邻时刻**，不是相邻字节。
        #   造一个「每个 28 帧窗口只有第 0 帧有值」的信号。按旧布局（把 112B
        #   当 4 unit x 28B、字节即时刻）解，非零采样会散在 0/4/8/12…
        #   这类位置；按真实布局解，它们只能落在 0/28/56/84。
        spike = [0] * 2016
        for win in range(4):
            spike[win * 28] = 7
        bsp = os.path.join(tmp, "spike.bin")
        with open(bsp, "wb") as fh:
            fh.write(make_xa_sector(spike, spike, shift=12, filt=0))
        lsp, _ = core._xa_decode_group(bsp, [0])
        pos = [i for i, v in enumerate(lsp) if v != 0]
        check("非零采样只落在每个 28 帧窗口的第 0 帧（钉住 word/字节顺序）",
              pos == [0, 28, 56, 84], str(pos[:8]))

        # shift=0 时幅度应放大 4096 倍
        sec0 = make_xa_sector([1] * 2016, [-1] * 2016, shift=0, filt=0)
        b0 = os.path.join(tmp, "zero.bin")
        open(b0, "wb").write(sec0)
        l0, r0 = core._xa_decode_group(b0, [0])
        check("shift=0 -> nib*4096", l0[0] == 4096 and r0[0] == -4096,
              f"{l0[0]} / {r0[0]}")

        # filter=1 的递推：s[n] = nib<<12 + (prev*60 + 32)>>6
        secf = make_xa_sector([1] * 2016, [1] * 2016, shift=12, filt=1)
        bf = os.path.join(tmp, "f1.bin")
        open(bf, "wb").write(secf)
        lf, _ = core._xa_decode_group(bf, [0])
        want = [1]
        for _ in range(4):
            # 注意优先级：预测项先右移 6 位，再与 nib 项相加；
            # v4.42 起按 vgmstream 加 +32 舍入（原先截断，低频会偏暗）
            want.append(1 + ((want[-1] * 60 + 32) >> 6))
        check("filter=1 预测器递推正确", list(lf[:5]) == want,
              f"{list(lf[:5])} vs {want}")

        # =================================================================
        print("\n=== ② 时长/元信息 ===")
        # 3 个 XA 扇区 -> 3*2016/37800 秒
        b3 = os.path.join(tmp, "three.bin")
        with open(b3, "wb") as fh:
            for _ in range(3):
                fh.write(sec)
        check("每扇区 0.0533s", abs(core._XA_SECTOR_SECS - 2016 / 37800) < 1e-9,
              f"{core._XA_SECTOR_SECS:.5f}")

        # =================================================================
        print("\n=== ③ 重采样 37800 -> 44100 ===")
        left2, right2 = core._xa_decode_group(b3, [0, SECTOR, SECTOR * 2])
        pcm = core._xa_to_pcm(left2, right2)
        n_out = len(pcm) // 4
        want_n = int(len(left2) * 44100 / 37800)
        check("输出采样数 = n*44100/37800", abs(n_out - want_n) <= 1, f"{n_out} vs {want_n}")
        check("输出是 16bit 立体声（4 字节/帧）", len(pcm) % 4 == 0)

        # =================================================================
        print("\n=== ④ 静音 CD-DA + 有 XA -> 自动改抽 XA ===")
        # 用 shift=0 的响信号（nib*4096，±6 -> ±24576），这样端到端写出的
        # WAV 幅度足够大，才能验证「不是白噪」而不是在量 LSB 噪声
        loud_L = nibble_pattern(2016, 7.0)
        loud_R = [-v for v in loud_L]
        sec_loud = make_xa_sector(loud_L, loud_R, shift=0, filt=0)
        data_dir = os.path.join(tmp, "disc")
        os.makedirs(data_dir)
        # 数据轨：前 10 个扇区填充，然后 2 个 XA 扇区
        data_bin = os.path.join(data_dir, "game (Track 1).bin")
        with open(data_bin, "wb") as fh:
            for _ in range(10):
                fh.write(b"\x00" * SECTOR)
            fh.write(sec_loud)
            fh.write(sec_loud)
        # 音频轨：整轨静音（模拟真实的「静音 CD-DA 轨」）
        aud_bin = os.path.join(data_dir, "game (Track 2).bin")
        with open(aud_bin, "wb") as fh:
            fh.write(b"\x00" * (150 * SECTOR))
            for _ in range(RATE * 2):
                fh.write(b"\x00" * 4)
        cuep = os.path.join(data_dir, "game.cue")
        with open(cuep, "w", encoding="utf-8") as fh:
            fh.write('FILE "game (Track 1).bin" BINARY\n'
                     "  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n"
                     'FILE "game (Track 2).bin" BINARY\n'
                     "  TRACK 02 AUDIO\n    INDEX 01 00:02:00\n")

        cdda = core.extract_disc_audio(cuep)
        check("先抽 CD-DA 得到 1 条（静音）", len(cdda) == 1, str(len(cdda)))
        check("静音轨峰值确实很低", core.tracks_peak(cdda) < 0.005,
              f"{core.tracks_peak(cdda):.6f}")

        tracks = core.load_source(cuep)
        check("load_source 改抽 XA", len(tracks) == 1 and " - XA" in tracks[0]["name"],
              str([t["name"] for t in tracks]))
        check("XA 轨标记为小端", tracks[0]["little_endian"] is True)
        check("XA 轨时长 = 2 扇区", abs(tracks[0]["xa_secs"] - 2 * 2016 / 37800) < 0.01,
              f"{tracks[0].get('xa_secs'):.3f}s")

        probe = core.probe_xa_audio(cuep)
        check("probe 报出 2 个扇区", probe["sectors"] == 2, str(probe))

        # 目录形态也要走同一条路
        dir_tracks = core.load_source(data_dir)
        check("拖文件夹同样改抽 XA",
              len(dir_tracks) == 1 and " - XA" in dir_tracks[0]["name"],
              str([t["name"] for t in dir_tracks]))

        # =================================================================
        print("\n=== ⑤ 有真实 CD-DA 音乐时不改抽 XA（回归）===")
        loud_dir = os.path.join(tmp, "loud")
        os.makedirs(loud_dir)
        with open(os.path.join(loud_dir, "g (Track 1).bin"), "wb") as fh:
            fh.write(b"\x00" * SECTOR)
            fh.write(sec)
        with open(os.path.join(loud_dir, "g (Track 2).bin"), "wb") as fh:
            fh.write(b"\x00" * (150 * SECTOR))
            n = RATE * 2
            for i in range(n):
                v = int(12000 * math.sin(2 * math.pi * 440 * i / RATE))
                fh.write(struct.pack(">hh", v, v))
            for _ in range((-n) % 588):
                fh.write(b"\x00" * 4)
        loud_cue = os.path.join(loud_dir, "g.cue")
        with open(loud_cue, "w", encoding="utf-8") as fh:
            fh.write('FILE "g (Track 1).bin" BINARY\n'
                     "  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n"
                     'FILE "g (Track 2).bin" BINARY\n'
                     "  TRACK 02 AUDIO\n    INDEX 01 00:02:00\n")
        lt = core.load_source(loud_cue)
        check("有音乐的盘仍返回 CD-DA（不改成 XA）",
              len(lt) == 1 and " - XA" not in lt[0]["name"],
              str([t["name"] for t in lt]))

        # =================================================================
        print("\n=== ⑥ 端到端：写出的 WAV 真的是音频 ===")
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        import spc_wav
        stat = spc_wav.write_tracks(tracks, out, 0.0)
        files = [f for f in os.listdir(out) if f.lower().endswith(".wav")]
        check("产出 1 个 WAV", len(files) == 1, str(files))
        if files:
            with wave.open(os.path.join(out, files[0]), "rb") as h:
                nf = h.getnframes()
                ch = h.getnchannels()
                sw = h.getsampwidth()
                srate = h.getframerate()
                raw = h.readframes(min(nf, RATE))
            check("WAV 为 44.1k/16bit/立体声",
                  ch == 2 and sw == 2 and srate == 44100,
                  f"{ch}ch/{sw*8}bit/{srate}Hz")
            vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
            mono = vals[0::2]                 # 只看左声道（交错后 L/R 会互相干扰）
            rms = math.sqrt(sum(v * v for v in mono) / len(mono))
            d = sum(abs(mono[i] - mono[i - 1]) for i in range(1, len(mono))) / (len(mono) - 1)
            sm = d / rms if rms else 9.9
            check("写出的 WAV 有内容且平滑（不是白噪）", rms > 1000 and sm < 0.5,
                  f"RMS={rms:.0f} 平滑度={sm:.3f}")

        # =================================================================
        # v4.37：纯数据盘。CUE 里**一条 AUDIO 轨都没有**（Silent Hill 那种，
        # 只有 TRACK 01 MODE2/2352）。以前 extract_disc_audio 抛 RuntimeError，
        # load_source 把它 continue 掉，最后落到 MP3 分支返回空列表 —— 用户看到
        # 的就是「转换不出东西」。这条路径必须单独钉住：④ 只覆盖了「有静音
        # AUDIO 轨」，覆盖不到「完全没有 AUDIO 轨」。
        # =================================================================
        print("\n=== ⑦ 纯数据盘：CUE 里没有 AUDIO 轨 -> 仍抽 XA（v4.37）===")
        pure_dir = os.path.join(tmp, "pure")
        os.makedirs(pure_dir)
        pure_bin = os.path.join(pure_dir, "silent (Track 1).bin")
        with open(pure_bin, "wb") as fh:
            for _ in range(10):
                fh.write(b"\x00" * SECTOR)
            fh.write(sec_loud)
            fh.write(sec_loud)
        pure_cue = os.path.join(pure_dir, "silent.cue")
        with open(pure_cue, "w", encoding="utf-8") as fh:
            fh.write('FILE "silent (Track 1).bin" BINARY\n'
                     "  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n")

        # 先确认「旧故障」真的还在：这条 CUE 抽不出 CD-DA
        try:
            core.extract_disc_audio(pure_cue)
            raised = False
        except RuntimeError:
            raised = True
        check("extract_disc_audio 对纯数据盘抛错（旧故障前提）", raised)

        pt = core.load_source(pure_cue)
        check("纯数据盘 load_source 抽出了 XA",
              len(pt) == 1 and " - XA" in pt[0]["name"],
              str([t["name"] for t in pt]))
        check("纯数据盘 XA 轨时长 = 2 扇区",
              abs(pt[0]["xa_secs"] - 2 * 2016 / 37800) < 0.01,
              f"{pt[0].get('xa_secs'):.3f}s")

        # 拖文件夹形态（GUI 最常见入口）
        ptd = core.load_source(pure_dir)
        check("纯数据盘拖文件夹也抽出 XA",
              len(ptd) == 1 and " - XA" in ptd[0]["name"],
              str([t["name"] for t in ptd]))

        # 列表列不能空白（probe 兜底）。返回结构与 CD-DA 分支保持一致：
        # n_tracks / total_secs / size / disc，不含 sectors。
        pr = core.probe_disc_audio(pure_cue)
        check("probe_disc_audio 对纯数据盘报出 1 条 XA 流", pr["n_tracks"] == 1, str(pr))
        check("probe_disc_audio 报出 XA 总时长",
              abs(pr["total_secs"] - 2 * 2016 / 37800) < 0.01,
              f"{pr.get('total_secs'):.3f}s")
        check("probe_disc_audio 报出数据轨体积（非 0）", pr["size"] > 0, str(pr["size"]))

        # 进度回调必须走到 1.0，否则 GUI 上几十秒没有反馈像卡死
        seen = []
        core.load_source(pure_cue, on_progress=lambda f: seen.append(f))
        check("进度回调被调用且收尾到 1.0",
              bool(seen) and abs(seen[-1] - 1.0) < 1e-9,
              str([round(v, 3) for v in seen]))

        # =================================================================
        # ⑧ 大体积落盘：超过阈值时 PCM 变成 _SpoolPcm，且内容与内存版逐字节相同
        # 真实阈值 128MB 没法在门禁里造，这里临时改小，只为验证分支走通。
        # =================================================================
        print("\n=== ⑧ 大体积 PCM 落盘（_SpoolPcm）===")
        mem = core.extract_xa_audio(pure_cue)
        mem_bytes = bytes(mem[0]["pcm"])
        old_thr = core._XA_SPOOL_BYTES
        core._XA_SPOOL_BYTES = 1          # 强制落盘
        try:
            spooled = core.extract_xa_audio(pure_cue)
            sp = spooled[0]["pcm"]
            check("超过阈值时 PCM 落盘为 _SpoolPcm",
                  isinstance(sp, core._SpoolPcm), type(sp).__name__)
            check("落盘后 len() 与内存版一致",
                  len(sp) == len(mem_bytes), f"{len(sp)} vs {len(mem_bytes)}")
            check("落盘后内容逐字节相同", bytes(sp) == mem_bytes)
            check("落盘对象可被 tracks_peak 读（read_all 存在）",
                  core.tracks_peak(spooled) > 0.1,
                  f"{core.tracks_peak(spooled):.3f}")
            # 落盘轨也能正常写出 WAV
            out2 = os.path.join(tmp, "out2")
            os.makedirs(out2)
            import spc_wav as _w
            _w.write_tracks(spooled, out2, 0.0)
            f2 = [f for f in os.listdir(out2) if f.lower().endswith(".wav")]
            check("落盘轨能写出 WAV", len(f2) == 1, str(f2))
        finally:
            core._XA_SPOOL_BYTES = old_thr

        # 清理：落盘目录要被 cleanup_spc_temp / cleanup 收掉，不能留在临时区
        for t in spooled:
            p = t["pcm"]
            if isinstance(p, core._SpoolPcm):
                p.cleanup()
        check("落盘目录可被清理",
              not any(isinstance(t["pcm"], core._SpoolPcm)
                      and os.path.exists(t["pcm"]._path) for t in spooled))

        # =================================================================
        # ⑨ v4.39：XA 池里混着 BGM / 音效 / 语音，只导音乐要把后两者滤掉。
        # 判据 = 时长 + 立体声宽度（见 core._XA_MUSIC_* 注释）。这里既钉纯函数
        # 的边界，也用合成盘验证端到端筛选（含「长单声道 BGM 不能被误杀」）。
        # =================================================================
        print("\n=== ⑨ XA 只导音乐（v4.39）===")
        check("≥60s 一律算音乐（单声道 BGM 不能误杀）",
              core.xa_is_music(65.0, 0.95) is True)
        check("20~60s 且左右相关低 -> 音乐", core.xa_is_music(30.0, 0.30) is True)
        check("20~60s 但近单声道 -> 音效/语音",
              core.xa_is_music(30.0, 0.80) is False)
        check("短于 20s 一律算音效（哪怕相关为 0）",
              core.xa_is_music(10.0, 0.05) is False)
        check("阈值本身：20s 边界算音乐",
              core.xa_is_music(core._XA_MUSIC_MIN_SECS, 0.10) is True)
        check("阈值本身：19.9s 不算音乐",
              core.xa_is_music(core._XA_MUSIC_MIN_SECS - 0.1, 0.10) is False)

        mo_dir = os.path.join(tmp, "musiconly")
        os.makedirs(mo_dir)
        # 25s 立体声（L/R 不同频率 -> 相关≈0）：音乐
        Lm = [int(round(6.0 * math.sin(2 * math.pi * 3.0 * i / 2016)))
              for i in range(2016)]
        Rm = [int(round(6.0 * math.sin(2 * math.pi * 11.0 * i / 2016)))
              for i in range(2016)]
        sec_st = make_xa_sector(Lm, Rm, shift=0, filt=0, file_no=1, channel=1)
        # 3s 单声道（L==R -> 相关=1）：音效/语音
        sec_mono = make_xa_sector(Lm, Lm, shift=0, filt=0, file_no=1, channel=2)
        # 65s 单声道：长音频一律算音乐（模拟真实存在的单声道 BGM）
        sec_long = make_xa_sector(Lm, Lm, shift=0, filt=0, file_no=1, channel=3)
        mo_bin = os.path.join(mo_dir, "mo.bin")
        with open(mo_bin, "wb") as fh:
            for _ in range(int(25 / core._XA_SECTOR_SECS)):
                fh.write(sec_st)
            for _ in range(int(3 / core._XA_SECTOR_SECS)):
                fh.write(sec_mono)
            for _ in range(int(65 / core._XA_SECTOR_SECS)):
                fh.write(sec_long)
        mo_cue = os.path.join(mo_dir, "mo.cue")
        with open(mo_cue, "w", encoding="utf-8") as fh:
            fh.write('FILE "mo.bin" BINARY\n'
                     "  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n")

        allt = core.extract_xa_audio(mo_cue)
        check("不筛时 3 条流全出", len(allt) == 3, str(len(allt)))
        st2 = {}
        mt = core.extract_xa_audio(mo_cue, music_only=True, stats=st2)
        names = [t["name"] for t in mt]
        check("只导音乐时只剩 2 条（立体声 + 长单声道）", len(mt) == 2, str(names))
        check("3s 单声道音效被滤掉", all("XA01-02" not in n for n in names), str(names))
        check("25s 立体声留下", any("XA01-01" in n for n in names), str(names))
        check("65s 单声道 BGM 留下", any("XA01-03" in n for n in names), str(names))
        check("stats 报出总数/保留/滤掉",
              st2 == {"total": 3, "kept": 2, "skipped": 1}, str(st2))
        # 整盘都是音效时要给可读错误，而不是静默返回空列表
        sfx_dir = os.path.join(tmp, "sfxonly")
        os.makedirs(sfx_dir)
        with open(os.path.join(sfx_dir, "s.bin"), "wb") as fh:
            for _ in range(int(3 / core._XA_SECTOR_SECS)):
                fh.write(sec_mono)
        sfx_cue = os.path.join(sfx_dir, "s.cue")
        with open(sfx_cue, "w", encoding="utf-8") as fh:
            fh.write('FILE "s.bin" BINARY\n'
                     "  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n")
        try:
            core.extract_xa_audio(sfx_cue, music_only=True)
            msg = ""
        except RuntimeError as e:
            msg = str(e)
        check("全是音效时报可读错误（不是空列表）",
              "没有判定为音乐" in msg, msg[:40])

        # =================================================================
        # ⑩ v4.43：XA 里没有 EOR 曲界标记，一首接一首只靠中间留静音。
        # 「按静音切曲」就是找静音段、在静音中点下刀。这里钉住四件事：
        #   ① 切点确实落在静音里（不在波形中间断 -> 不爆音）
        #   ② 段与段拼起来 == 原始长度（一个采样都不能丢）
        #   ③ 太短的静音不切、太短的段并到邻居（不切碎）
        #   ④ 没有 numpy 的兜底实现结论一致（打包环境可能有/没有）
        # =================================================================
        print("\n=== ⑩ XA 按静音切曲（v4.43）===")

        def _tone(secs, amp=8000, freq=440):
            n = int(secs * RATE)
            a = array.array("h")
            for i in range(n):
                v = int(amp * math.sin(2 * math.pi * freq * i / RATE))
                a.append(v)
                a.append(v)
            return a.tobytes()

        def _sil(secs):
            return b"\x00" * (int(secs * RATE) * 4)

        # 三段 6s 音乐 + 两段 2.5s 静音
        pcm3 = _tone(6) + _sil(2.5) + _tone(6) + _sil(2.5) + _tone(6)
        segs = core.pcm_split_points(pcm3)
        total_frames = len(pcm3) // 4
        check("三段音乐被切成 3 段", len(segs) == 3, str([(s, e) for s, e in segs]))
        check("切段拼起来等于原始长度（不丢采样）",
              sum(e - s for s, e in segs) == total_frames,
              f"{sum(e - s for s, e in segs)} vs {total_frames}")

        # 切点必须落在静音里：切点前后 50ms 的最大绝对值应当很小
        a_all = array.array("h")
        a_all.frombytes(pcm3)
        ok_silent_cut = True
        worst = 0
        for _, e in segs[:-1]:
            w = a_all[max(0, (e - 2205) * 2):(e + 2205) * 2]
            m = max((abs(v) for v in w), default=0)
            worst = max(worst, m)
            if m > 500:
                ok_silent_cut = False
        check("切点落在静音里（±50ms 峰值 <500）", ok_silent_cut, f"最大 {worst}")

        # 0.5s 的短停顿不当曲界
        pcm_short = _tone(6) + _sil(0.5) + _tone(6)
        check("0.5s 停顿不切（min_gap=1.5s）",
              len(core.pcm_split_points(pcm_short)) == 1,
              str(len(core.pcm_split_points(pcm_short))))

        # 尾部只有 1s 音频 -> 太短，并到前一段
        pcm_tail = _tone(6) + _sil(2.5) + _tone(1)
        check("末尾 1s 残段并进前一段（min_track=5s）",
              len(core.pcm_split_points(pcm_tail)) == 1,
              str(len(core.pcm_split_points(pcm_tail))))

        # 全静音 / 极短：都得返回 1 段，不能返回空（否则调用方会丢整条流）
        check("整条静音也返回 1 段（不返回空）",
              core.pcm_split_points(_sil(10)) == [(0, 10 * RATE)],
              str(core.pcm_split_points(_sil(10))))
        check("极短 PCM 返回 1 段",
              core.pcm_split_points(_tone(0.2)) == [(0, int(0.2 * RATE))],
              str(core.pcm_split_points(_tone(0.2))))

        # 无 numpy 的兜底实现必须给出一致的静音判定
        if core._HAVE_NUMPY:
            win_f = int(core._XA_SPLIT_WIN * RATE)
            n_win = (len(pcm3) // 4) // win_f
            flags_np = core._pcm_quiet_flags(pcm3, win_f, n_win, core._XA_SPLIT_RMS)
            saved = core._HAVE_NUMPY
            try:
                core._HAVE_NUMPY = False
                flags_py = core._pcm_quiet_flags(pcm3, win_f, n_win,
                                                 core._XA_SPLIT_RMS)
            finally:
                core._HAVE_NUMPY = saved
            check("无 numpy 兜底与 numpy 结论一致",
                  flags_np == flags_py,
                  f"{sum(flags_np)} vs {sum(flags_py)} 个静音窗")

        # 端到端：合成一张「三段音乐 + 两段静音」的 XA 盘
        sp_dir = os.path.join(tmp, "split")
        os.makedirs(sp_dir)
        music = [6 if (i // 8) % 2 == 0 else -6 for i in range(2016)]
        zero = [0] * 2016
        sec_mus = make_xa_sector(music, music, shift=0, filt=0,
                                 file_no=1, channel=1)
        sec_sil = make_xa_sector(zero, zero, shift=0, filt=0,
                                 file_no=1, channel=1)
        n_mus = int(round(6 / core._XA_SECTOR_SECS))
        n_sil = int(round(2.5 / core._XA_SECTOR_SECS))
        sp_bin = os.path.join(sp_dir, "sp.bin")
        with open(sp_bin, "wb") as fh:
            for k in range(3):
                if k:
                    for _ in range(n_sil):
                        fh.write(sec_sil)
                for _ in range(n_mus):
                    fh.write(sec_mus)
        sp_cue = os.path.join(sp_dir, "sp.cue")
        with open(sp_cue, "w", encoding="utf-8") as fh:
            fh.write('FILE "sp.bin" BINARY\n'
                     "  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n")

        whole = core.extract_xa_audio(sp_cue)
        check("默认不切：仍是 1 条流", len(whole) == 1, str(len(whole)))
        check("不切时名字保持「镜像名 - XA」",
              whole and whole[0]["name"] == "sp - XA",
              whole[0]["name"] if whole else "")
        whole_bytes = sum(len(t["pcm"]) for t in whole)

        st3 = {}
        cut = core.extract_xa_audio(sp_cue, split_silence=True, stats=st3)
        check("开切后切成 3 段", len(cut) == 3, str(len(cut)))
        check("段名带序号 01/02/03",
              [t["name"] for t in cut] == ["sp - XA 01", "sp - XA 02", "sp - XA 03"],
              str([t["name"] for t in cut]))
        check("切分后总字节数与不切一致",
              sum(len(t["pcm"]) for t in cut) == whole_bytes,
              f"{sum(len(t['pcm']) for t in cut)} vs {whole_bytes}")
        check("stats 报出被切的流数与段数",
              st3.get("split_streams") == 1 and st3.get("segments") == 3,
              str(st3))
        check("每段 xa_secs 之和 == 整条时长",
              abs(sum(t["xa_secs"] for t in cut)
                  - whole[0]["xa_secs"]) < 0.01,
              f"{sum(t['xa_secs'] for t in cut):.3f} vs {whole[0]['xa_secs']:.3f}")
        # 不开切分时 stats 里不该多出这两个键（老调用方按字典相等断言）
        st4 = {}
        core.extract_xa_audio(sp_cue, stats=st4)
        check("不切时 stats 不多键（保持老口径）",
              set(st4) == {"total", "kept", "skipped"}, str(sorted(st4)))
        for t in whole + cut:
            if isinstance(t["pcm"], core._SpoolPcm):
                t["pcm"].cleanup()

    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 62)
    print(f"结果: {_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
