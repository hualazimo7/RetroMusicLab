#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chip_formats.py — 芯片音乐格式识别与元信息（纯解析层）。

⚠ 绝对不要在这里 import 原生扩展绑定 / 动态库 / 核心解码模块。
  本模块会在界面线程、worker 子进程、命令行三处被调用，
  必须保持零重依赖、零副作用、绝不抛异常。

设计契约见 deliverables/software-company/multiformat-design-2026-10-18.md §2/§3。
本文件所有偏移量均以真实样本（joshw 素材，见 vendor/fetch_samples.py）逐一实测校正，
实测结论与设计文档不一致处见每处「实测」注释，并在 README 的「与设计文档的偏差」小节汇总。

⚠⚠ 三条硬约束（实测结论，勿再验证、勿再"优化"）：
  1. 禁止调用 DLL 的 identify_file / identify_header 接口：本机该库上必段错误。
     格式识别只走「扩展名 + 自写魔数嗅探」。
  2. gme_set_autoload_playback_limit() 不产生 ended 事件，不能用于截断时长。
     时长一律自算（见 plan_duration）。
  3. 偏移量/字段值必须**整条结构自洽**才算验证通过，不能"读几个字节像"就采信。
     GBS 循环点读在标题字符串里把jingle 算成 520 秒、NSFE 错三轮，都是这条。

P1 四格式（KSS/GYM/SAP/AY）已于 2026-10-18 启用：素材来自 joshw KSS 站 +
archive.org（GYM/SAP/AY），魔数与偏移以 libgme 源码 + 真实样本 hexdump 双向
验证，可复跑 vendor/probe_p1_offsets.py与 vendor/verify_p1_e2e.py。
"""

import gzip
import io
import os
import re
import struct

__all__ = [
    "P0_ENABLED", "P1_ENABLED", "EXT_TABLE", "MAGIC_TABLE", "SYSTEM_NAMES", "P2_EXTS",
    "PSF_EXTS", "PSF1_EXTS", "PSF2_EXTS", "PSF_LIB_EXTS",
    "QSF_EXTS", "QSF_LIB_EXTS",
    "CHIP_EXTS", "P0_CHIP_EXTS", "ALL_EXTS", "GBS_FRAME_RATE", "CHIP_MAX_TRACKS",
    "CHIP_DEFAULT_SECS_DEFAULT", "CHIP_FADE_MS", "CHIP_MAX_SECS",
    "EXACT_SOURCES", "EXACT_OR_LOOP_SOURCES",
    "KSS_HEADER_SIZE", "GYM_HEADER_SIZE", "AY_HEADER_SIZE", "S98_EXTS",
    "ChipInfo", "Duration", "identify", "read_magic", "probe", "plan_duration",
    "unsupported_reason", "human_size", "collect_files", "collect_supported",
    "fmt_system", "is_chip_ext", "decode_text",
]

# ---------------------------------------------------------------------------
# 常量与注册表（设计 §2.2）
# ---------------------------------------------------------------------------

P0_ENABLED = ("NSF", "NSFE", "GBS", "VGM", "VGZ", "HES")

# P1 已启用的四种（2026-10-18 起：素材与libgme 均已实测通过，见下方各 probe 函数）
P1_ENABLED = ("KSS", "GYM", "SAP", "AY")

EXT_TABLE = {          # 扩展名 -> 格式 id
    ".spc": "SPC", ".nsf": "NSF", ".nsfe": "NSFE", ".gbs": "GBS",
    ".vgm": "VGM", ".vgz": "VGZ", ".hes": "HES",
    ".kss": "KSS", ".gym": "GYM", ".sap": "SAP", ".ay": "AY",
    # v5.4：SGC（Sega Game Gear / Master System / Coleco 的快照）。
    # 本工程原来的 libgme.dll 是官方 0.6.x 构建，**不含 SGC**，所以一直被
    # 归到「不支持」。改用 kode54/Game_Music_Emu 重编后（Sgc_Emu + Sms_Apu
    # + Z80_Cpu），它和别的 libgme 格式走完全一样的路径。
    ".sgc": "SGC",
    # v5.5：WSR（WonderSwan Sound Rip）。
    # ⚠ **唯一一个不进 libgme 的条目** —— libgme 没有 WonderSwan 机种。
    #   登记在这里只是为了让它进 CHIP_EXTS / ALL_EXTS（界面收集、拖放过滤、
    #   格式识别都靠这两份清单），真正的解码在 chip_decode.load_chip_paths
    #   里被单独分流到 wsr_native（→ wsr2wav.exe，beetle-wswan 核）。
    #   所以 decode_path 那条 libgme 路径**永远不会**看到 .wsr。
    ".wsr": "WSR",
    # v5.9.13：S98（PC-98 / PC-88 的 YM2608 寄存器写入日志）。
    # ⚠ 和 .wsr 一样**不进 libgme** —— 但理由不同，这里更彻底：
    #   libgme 的 VGM 引擎（gme/Vgm_Emu_Impl.cpp）**根本没有 YM2608 分支**，
    #   只实现 cmd_gg_stereo / cmd_psg / cmd_ym2413 / cmd_ym2612_port0,1
    #   这 5 个芯片指令，其余一律 `default:` 静默跳过。所以哪怕把 .s98 转成
    #   合法的 .vgm 交给它，YM2608 的写入（VGM 0x56/0x57）也全被丢掉 ——
    #   输出精确静音且不报错。真实解码走 s98_native（→ s982wav.exe，ymfm 核）。
    ".s98": "S98",
}

#: WSR（WonderSwan Sound Rip）—— 走外挂 wsr2wav.exe，不吃 libgme。
WSR_EXTS = (".wsr",)

#: KSS —— **v5.8 起走外挂 kss2wav.exe（libkss）**，不再交给 libgme。
#:
#: libgme 的 KSS 实现只映射 0x0000-0x3FFF 与 0x8000-0xBFFF，**不映射
#: 0x4000-0x7FFF**。ROM 落在这一段里的 KSS（32 KB 的 Game Gear rip 最典型，
#: `load_addr` 在 0x3E10 一带）会丢 16 KB 代码，Z80 跑飞 —— 而 libgme
#: **不报错**，只是每轨精确静音。实测 Zophar 的 Game Gear 栏 12 个包里
#: 3 个这样（Puyo Puyo / Nazo Puyo / Madou Monogatari）。
#:
#: `.sgc` 不在此列：它是独立容器，libgme 支持良好（抽样 6/6 正常）。
KSS_EXTS = (".kss",)

#: S98（PC-98 / PC-88 的 YM2608 音源）—— **v5.9.13 起走外挂 s982wav.exe（ymfm）**。
#:
#: 为什么不能交给 libgme（读源码确认，不是猜的）：
#:   ``gme/Vgm_Emu_Impl.cpp`` 的 ``run_commands()`` 只实现
#:   ``cmd_gg_stereo(0x4F) / cmd_psg(0x50) / cmd_ym2413(0x51) /
#:   cmd_ym2612_port0(0x52) / cmd_ym2612_port1(0x53)`` 五条芯片指令。
#:   YM2608 的 VGM 指令是 0x56/0x57，会落到 ``default:`` 分支被
#:   ``pos += command_len(cmd) - 1`` **静默跳过**（只挂一句
#:   "Unknown stream event" 警告）。也就是说「.s98 → .vgm → libgme」这条路
#:   能跑通但**输出精确静音**，属于最坏的那种失败（不报错）。
#:
#: ymfm（Aaron Giles，BSD-3，MAME 在用的 OPN 家族核心）完整实现了
#: YM2608：FM 6 通道 4 算子 + SSG（AY-3-8910 兼容）+ ADPCM-A/B。
S98_EXTS = (".s98",)


# 顺序敏感：长/特异者在前。
# 实测：真实 GBS 文件头是 b"GBS\x01"，**不是**设计文档写的 b"GBSX"（那只存在于
# 某些老工具的描述里）。三个样本 3/3 均为 47 42 53 01，故以实测为准，
# 同时保留 b"GBSX" 兼容万一存在的变体。
#
# ⚠ P1 四种的魔数全部**以 libgme 源码 + 真实样本 hexdump 双向验证过**，
#   不是照抄公开规范 —— 见每个 probe 函数的 docstring。
#   特别是AY：任务书里给的 b"AYM"/b"EMAY" **是错的**，libgme 只认 b"ZXAYEMUL"。
MAGIC_TABLE = (
    (b"SNES-SPC700 Sound File Data", "SPC"),
    (b"NESM\x1a", "NSF"),
    (b"NSFE", "NSFE"),
    (b"GBS\x01", "GBS"),
    (b"GBSX", "GBS"),
    (b"Vgm ", "VGM"),
    (b"VgM", "VGM"),
    (b"\x1f\x8b", "VGZ"),          # gzip，必须放在最后以免误吞别的
    (b"HESM", "HES"),
    # ---- P1（实测）----
    (b"KSCC", "KSS"),        # MSX KSS，真实样本 4b 53 43 43
    (b"KSSX", "KSS"),        # MSX KSS 扩展头变体（libgme 同样接受）
    (b"GYMX", "GYM"),        # Sega Genesis，真实样本 47 59 4d 58
    (b"SAP\x0d\x0a", "SAP"),  # Atari，真实样本 53 41 50 0d 0a —— **5 字节**
    (b"ZXAYEMUL", "AY"),      # ZX Spectrum，真实样本 5a 58 41 59 45 4d 55 4c
    # v5.4：SGC（Game Gear / Master System / Coleco 快照）。
    # 头 0xA0 字节：tag(4) + vers + rate + reserved + load/init/play/stack +
    # rst 向量 + mapping + first_song + song_count + system + 32×3 文本。
    # 魔数就是前 4 字节（实测 T-81048.sgc = 53 47 43 1a）。
    (b"SGC\x1a", "SGC"),
    # v5.9.13：S98（PC-98 / PC-88）。前 3 字节 "S98"，第 4 字节是版本字符
    # '0'..'3'（不是二进制 0..3）。实测 Ys 包 18 个文件全是 "S981" = v1。
    (b"S98", "S98"),
)

SYSTEM_NAMES = {       # 格式 id -> 机种名（不支持提示文案用）
    "NSF": "NES / FC", "NSFE": "NES / FC", "GBS": "Game Boy",
    "VGM": "Mega Drive / Sega CD", "VGZ": "Mega Drive / Sega CD",
    "HES": "PC Engine / TurboGrafx", "SPC": "SNES",
    "KSS": "MSX / Master System / Game Gear",
    "SGC": "Sega Game Gear / Master System",
    "GYM": "Sega Genesis", "SAP": "Atari 8-bit", "AY": "ZX Spectrum",
    # v5.9.13：PC-98 / PC-88 的 YM2608（OPNA）。
    "S98": "PC-98 / PC-88",
    "USF": "Nintendo 64", "SSF": "Sega Saturn", "DSF": "Dreamcast",
    "2SF": "Nintendo DS", "AIF": "3DO", "WSR": "WonderSwan", "GSF": "Game Boy Advance",
    # v4.40: PS1/PS2 的 PSF 家族。原先 SYSTEM_NAMES 里没有这两项，
    # `.minipsf` 会拼出「暂不支持 PSF（）——」这种空括号文案。
    "PSF": "PlayStation", "PSF2": "PlayStation 2",
    # v5.9.10: Capcom QSound 街机（CPS1 / CPS2 / CP-ZN1 / CP-ZN2）。
    # 也是 PSF 容器（版本字节 0x41），但装的是 Z80 程序 + 采样 ROM 的块列表，
    # 不是内存快照。见 qsf_native 的模块注释。
    "QSF": "Capcom QSound 街机",
    # v5.9.11：PS3 / PSP / Wii / GameCube / 3DS 的**预录流**。
    # 这几种都不走 libgme、也不需要模拟器 —— 前四个由 FFmpeg 直接解，
    # GameCube 的 Pivotal PSF 由 pivotal_psf 原生解。
    # 登记在这里是为了「万一解码失败」时文案能说出是哪个机种，
    # 而不是拼出「暂不支持 XVAG（）」这种空括号。
    "XVAG": "PlayStation 3", "AT3": "PlayStation 3 / PSP",
    "HCA": "PSP", "BRSTM": "Wii / GameCube",
    "LAAC": "Nintendo 3DS", "PIVOTAL": "GameCube",
    # v5.9.11 补：PS Vita。原先以为这个机种「包里没有音频」而放弃，
    # 实际是判断错了 —— 60 个 .at9 + 2 个 .vag 都在，FFmpeg 两个都认。
    "AT9": "PlayStation Vita", "VAG": "PlayStation Vita / PlayStation",
    # v5.9.12：Xbox 360 / PS4 / Xbox / Wii U 的预录流与容器。
    # 同样是**转手 FFmpeg**（wem 除外，它走 vgmstream），登记在这里只为
    # 「万一解码失败」时文案能说出机种，而不是拼出「暂不支持 XMA（）」。
    "XMA": "Xbox 360", "BIK": "PlayStation 4（Bink 视频音轨）",
    "ARAW": "Xbox", "WEM": "PlayStation 4 / Wwise",
    "FSB": "Wii U / FMOD", "BTSND": "Wii U",
}

P2_EXTS = {           # 已知但本工具不支持 —— FR-4.2 第一类文案
    ".usf": "USF", ".ssf": "SSF", ".dsf": "DSF", ".2sf": "2SF",
    ".gsf": "GSF",
    # v5.6: **`.aif` 已移出本表** —— 3DO 的 AIFF/AIFC 现在原生解码
    #   （:mod:`aif_audio`，SDX2 / 8bit / 16bit PCM），走 ``stream_audio``
    #   那条路，不需要整机模拟。留在这里会把能转的说成「需要该机种的模拟器」。
    # ⚠ 仍然不支持的是 **4bit AIFF**（XA-ADPCM，Road Rash 那类）——
    #   它在 aif_audio 里会明确报「本工具还没接」，不会静默出静音。
    # ⚠ joshw 的包实际用的是 **mini 前缀**的变体（一个包几十首、每首一个文件），
    #   只登记 .usf/.gsf 这些主名的话，用户下回来的 .miniusf 会被判成
    #   「不认识的扩展名」，给不出「这是 N64 格式、需要模拟器」的正确提示。
    ".miniusf": "USF", ".minigsf": "GSF", ".minissf": "SSF",
    ".minidsf": "DSF", ".mini2sf": "2SF",
    # v4.40: PS1/PS2 的 PSF 家族把主名与 psf1/psf2 变体补齐。
    #   原先只登记了 `.minipsf` 一个，导致 `.psf` / `.psf2` 落到
    #   「不认识的扩展名」分支，文案完全误导（用户下回来的 PS1 包主名就是 .psf）。
    #
    # v5.2: **PS1 的四个后缀已移出本表** —— 它们现在能原生解码了，
    #   见 PSF1_EXTS。留在这里会被判成「不支持」，把已经能转的包拒之门外。
    # v5.4: **PS2 的两个后缀也已移出本表** —— psf2wav.exe 已支持 version 2
    #   （psf2fs VFS 装载 + IOP 模拟，48000Hz），见 PSF2_EXTS。
}

#: PSF 家族（PS1 的 .psf/.minipsf、PS2 的 .psf2/.minipsf2）—— 单独列一份，
#: 因为它们的**处置方式**和别的 P2 格式不同。
#:
#: v5.4（2026-10-24）：PS2 的两个后缀也已**原生支持** —— 同一把
#: ``psf2wav.exe`` 走 psf2fs VFS 装载 + IOP 模拟，采样率 48000Hz，
#: 与上游 plugin.cc 逐样本一致（``vendor/_check_psf2_ref.py`` 54/54 最大差 0）。
#: 所以它们也从 P2_EXTS 挪到了 :data:`PSF2_EXTS`，并进了 :data:`ALL_EXTS`。
#:
#: ⚠ 但 PSF **不进 EXT_TABLE**：EXT_TABLE 是 libgme 的解码清单，
#:   libgme 根本不吃 PSF，混进去会让 ``core.load_source`` 走错路。
#:   它们由 ``convert_job`` 单独分支到 ``psf_native``。
PSF_EXTS = (".psf", ".psf1", ".minipsf", ".minipsf1", ".psf2", ".minipsf2")
#: 已支持：PS1 的 PSF（原生解码，需要一份 BIOS，见 psf_native）
PSF1_EXTS = (".psf", ".psf1", ".minipsf", ".minipsf1")
#: 已支持：PS2 的 PSF2（v5.4 起。同样走 psf2wav.exe，IOP ELF 装载 +
#: psf2fs 虚拟文件系统，48000Hz。见 ``vendor/_check_psf2_ref.py``）
PSF2_EXTS = (".psf2", ".minipsf2")
#: 已支持：Capcom QSound 的 QSF（v5.9.10 起）—— **同样不在 EXT_TABLE 里**，
#: 走 ``qsf_native`` → ``qsf2wav.exe``（psflib + highly_quixotic + zlib）。
#:
#: 它和 PSF 是**同一个容器**（PSF magic + 版本字节），区别只在版本字节
#: （0x41 = QSound）和 program section 的内容：PSF 放可执行程序，QSF 放
#: 「11 字节块头 + 载荷」的块列表（Z80 程序 ROM / SMP 采样 ROM / KEY 密钥）。
#:
#: ⚠ 包里的 ``.qsflib`` 是**全曲共用的库**（4MB+ 的采样 ROM），本身不是曲目，
#:   所以它进 :data:`QSF_LIB_EXTS` 而不是这里 —— 和 ``.psflib`` 一个道理。
QSF_EXTS = (".qsf", ".miniqsf")
#: QSF 的库文件后缀（不是曲目）
QSF_LIB_EXTS = (".qsflib",)
#: PSF 的库文件后缀（不是曲目，一个包里通常只有一两个）
PSF_LIB_EXTS = (".psflib", ".psf2lib", ".qsflib")


# P1 扩展名（已启用：素材与 libgme 均已实测，见 vendor/probe_p1_offsets.py）
P1_EXTS = {".kss": "KSS", ".gym": "GYM", ".sap": "SAP", ".ay": "AY"}

#: 芯片音源扩展名（含 SPC，因 SPC 也是芯片音源且已支持）
CHIP_EXTS = tuple(EXT_TABLE.keys())
#: P0 芯片音源扩展名（不含 SPC）
P0_CHIP_EXTS = tuple(e for e, f in EXT_TABLE.items() if f in P0_ENABLED)
#: 全部"可解码音源"扩展名 —— 界面 SUPPORTED_EXTS / collect_supported 共用
#:
#: ⚠ PSF 家族是**后加的**：它不走 libgme（所以不在 EXT_TABLE / CHIP_EXTS 里），
#:   但确实能被解码（``psf_native``），所以必须进这份清单 —— 否则用户拖进来
#:   的 .psf / .psf2 根本不会被收集，界面上看就是「没反应」。
#:   QSF 同理（v5.9.10）：``qsf_native`` 能解，但 libgme 不吃。
#:
#: v5.9.11：补上**流式音频**与 ``.txtp`` 轨道索引。原先这两族只登记在
#:   ``convert_job.STREAM_EXTS`` / ``dl_convert`` 里，结果文件对话框的筛选
#:   根本列不出 ``.adx`` / ``.XVAG`` / ``.hca`` —— 用户只能靠「拖整个文件夹」
#:   进来，单选文件时选不到。
#:   ⚠ 清单本身**不在这里重写一份**：单一事实源仍是
#:     ``stream_audio.EXT_KIND``，这里只做转发（与 ``convert_job`` 同一个口径）。
try:
    import stream_audio as _stream_audio
    _STREAM_EXTS = tuple(_stream_audio.ALL_EXTS)
except Exception:                                    # noqa: BLE001
    # 缺 numpy 之类的极端情况下退化 —— 宁可少列，也别让整个模块 import 失败
    _STREAM_EXTS = (".adx", ".mod", ".hwas", ".swav", ".dvi", ".strm", ".npsf",
                    ".aif", ".aiff", ".aifc", ".xvag", ".at3", ".hca",
                    ".brstm", ".laac")

#: ``.txtp`` 是「轨道索引」，本身不含音频，但它是 JoshW 包（PS1 / GameCube）
#: 的正常入口 —— 不进清单的话，一个 PS1 包里 23 个 .txtp 全被丢掉。
TXTP_EXT = ".txtp"

ALL_EXTS = (CHIP_EXTS + (".cue", ".bin", ".mp3") + PSF_EXTS + QSF_EXTS
            + _STREAM_EXTS + (TXTP_EXT,))


#: 「文件自带时长」的合法来源。**单一真源**：`exact` 属性、plan_duration
#: 的 lead_sil 闸门、界面的 * 标记都以此为准。加新来源时只改这一处。
#:
#: gym_frames / ay_frames 是 P1 新增（GYM 帧数 ×1000/60、AY SongLength×20ms），
#: 二者均已与 libgme track_info().length 逐样本比对一致（见 vendor/probe_p1_offsets.py）。
#: gbs_loop 不是「文件自带」而是「库算出的循环点」，故只参与 lead_sil 闸门，
#: 不进exact（界面不把它标成确切时长，与 GBS 那轮的决定一致）。
EXACT_SOURCES = ("id666", "nsfe_length", "vgm_samples", "gym_frames", "ay_frames",
                 # v5.9.13：S98 的数据流以 0xFD 显式结束，tick 数 × tickMult/tickDiv
                 # 就是精确时长（实测 18/18 文件都带 0xFD）。
                 "s98_ticks")

#: 时长"精确"口径（含 gbs_loop）：决定是否关闭静音检测/前置静音砍除。
EXACT_OR_LOOP_SOURCES = EXACT_SOURCES + ("gbs_loop",)

GBS_FRAME_RATE = 59.7275        # 4194304 / 70224, loop_end 换算成秒
CHIP_MAX_TRACKS = 256           # HES 格式上限; 超过视为损坏并截断
CHIP_DEFAULT_SECS_DEFAULT = 150.0   # UI-5 Spinbox 默认, 范围 30~600
CHIP_MAX_SECS = 600             # 与 core._SPC_MAX_SECS 一致, 单一真源
CHIP_FADE_MS = 1500             # 与 spc_wav._limit_len 的 1500 一致

#: probe() 的读盘硬上限。NSFE section 表 / VGM GD3 可能更靠后，按需追加读，
#: 但总量受此约束，保证界面线程拖入上百文件也不卡（H15）。
PROBE_MAX_READ = 8192

#: S98 的 probe 会扫完整条数据流来拿精确时长（见 :func:`_probe_s98`）。
#: 超过这个大小就放弃扫描、退回「时长未知」—— 不能让一个坏文件把界面拖死。
#: 参考量级：10 分钟的音乐 ≈ 2 MB，32 MB 已经相当于 2.5 小时。
S98_SCAN_MAX = 32 * 1024 * 1024
#: VGZ 解压后允许读取的头长度（VGM 头本身 0x100，GD3 在 0x14 指向处）
_VGZ_HEAD_MAX = 0x4000
#: GBS loop_end 换算后超过此秒数视为"没填"（很多 GBS 填 0xFFFF）
_GBS_LOOP_MAX_SECS = 3600.0


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def decode_text(raw):
    """字节串 -> 干净文本。Shift-JIS 优先（VGM GD3 是 Shift-JIS），失败退 latin-1。

    返回 "" 而非 None，便于直接 `or` 兜底。
    """
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    b = bytes(raw).split(b"\x00", 1)[0]
    if not b:
        return ""
    for enc in ("shift_jis", "utf-8"):
        try:
            return b.decode(enc).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("latin-1", "replace").strip()


def human_size(n):
    """字节数 -> 人类可读字符串。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def fmt_system(fmt):
    """格式 id -> 机种名，未知返回空串。"""
    return SYSTEM_NAMES.get(fmt or "", "")


def _u8(buf, off):
    return buf[off] if 0 <= off < len(buf) else 0


def _u16le(buf, off):
    return struct.unpack_from("<H", buf, off)[0] if 0 <= off + 2 <= len(buf) else 0


def _u32le(buf, off):
    return struct.unpack_from("<I", buf, off)[0] if 0 <= off + 4 <= len(buf) else 0


def _u16be(buf, off):
    return struct.unpack_from(">H", buf, off)[0] if 0 <= off + 2 <= len(buf) else 0


# ---------------------------------------------------------------------------
# ChipInfo（设计 §2.3）
# ---------------------------------------------------------------------------

class ChipInfo:
    """一个芯片音乐文件的全部可用元信息。**永远返回本对象，从不 None。**

    用 __slots__ 而非 dataclass：避免老版本 Python 兼容问题，且省内存
    （界面拖入上百文件时每个实例都常驻缓存）。
    """

    __slots__ = ("path", "fmt", "ext_declared", "conflict", "size",
                 "n_tracks", "n_tracks_used",
                 "title", "author", "system",
                 "exact_secs", "exact_src", "loop_end_secs",
                 "track_secs", "track_names", "ok", "reason",
                 "track_ids", "track_lengths", "track_titles",
                 "cd_tracks", "m3u_path")

    def __init__(self, path="", fmt=None, ext_declared="", conflict=False,
                 size=0, n_tracks=0, title="", author="", system="",
                 exact_secs=0.0, exact_src="", loop_end_secs=0.0,
                 track_secs=None, track_names=None, ok=True, reason="",
                 n_tracks_used=0, track_ids=None, track_lengths=None,
                 track_titles=None, cd_tracks=None, m3u_path=""):
        self.path = path
        self.fmt = fmt                    # 识别出的格式 id; 无法识别为 None
        self.ext_declared = ext_declared  # 声明的扩展名（小写，含点）
        self.conflict = conflict          # 魔数与扩展名不符
        self.size = size
        self.n_tracks = n_tracks          # 0 = 头里读不出（交给 worker 用 gme_track_count）
        # v4.2: head[0x15] = **容器尺寸字段**（实测 size==(0x15+1)×256，7/7 成立），
        #   **与有声轨数的关系未确认** —— 真解码实测占用率 18%~238%，
        #   **既可能高估也可能大幅低估**。
        #   ⚠⚠ **禁止用于任何估算/门禁/时间预测**（会低估 2.38 倍 -> 撞磁盘门禁）。
        #   `dl_convert.estimate_files` 已回滚为只用 n_tracks。
        #   保留本字段仅为记录该偏移量的**实测语义**，供后续调研（见 _probe_hes 详注）。
        self.n_tracks_used = n_tracks_used  # 0 = 头里读不出/不可靠
        self.title = title
        self.author = author
        self.system = system
        self.exact_secs = exact_secs      # 文件自带时长（秒），0 = 无
        self.exact_src = exact_src        # "nsfe_length" / "vgm_samples" / "id666"
        self.loop_end_secs = loop_end_secs  # GBS loop_end 换算秒，0 = 无
        self.track_secs = track_secs if track_secs is not None else []
        self.track_names = track_names if track_names is not None else []
        # ---- v4.29: 同目录 .m3u 给出的「真实曲目表」（仅 HES 使用）--------
        # 为什么需要：PCE-CD 的 HES rip 里，`gme_track_count` 恒为 256，
        # 其中**绝大部分是预分配的空槽位/音效残留**；真正的曲目表只写在
        # 同目录 `<包名>.m3u` 里（`<包名>.hes::HES,$04,Overture,3:14,-,10`）。
        # 不读它就只能 256 槽全扫 —— 实测 KMCD4007 扫出 196 首 / 2532 秒，
        # 而 m3u 只列了 178 首；HCD9005 更离谱（63 首 / 8334 秒 / 1.4 GB，
        # 真实曲目平均 40 秒）。所以：
        #   track_ids     = m3u 里 `HES,$NN` 的 NN 列表（**真实曲号**，可能不连续）
        #   track_lengths = {NN: 秒}（m3u 第 4 列，`0:39` / `39`；0 = 未标注）
        #   cd_tracks     = m3u 里的 CD 音轨（`.ape` 行）文件名列表 —— 本工具不转，
        #                   但**必须让用户看见**，否则会以为「转换少了东西」
        #   m3u_path      = 用到的 m3u 绝对路径（诊断/日志用）
        self.track_ids = list(track_ids) if track_ids else []
        self.track_lengths = dict(track_lengths) if track_lengths else {}
        self.track_titles = dict(track_titles) if track_titles else {}
        self.cd_tracks = list(cd_tracks) if cd_tracks else []
        self.m3u_path = m3u_path or ""
        self.ok = ok                      # True = 可交给解码器
        self.reason = reason              # ok=False 时的用户可执行文案

    # -- 便捷派生属性（供界面/估算使用，均不抛异常）--------------------------

    @property
    def is_multi(self):
        """是否多轨（>1 轨）。未知轨数（0）不算多轨。"""
        return self.n_tracks > 1

    @property
    def total_secs(self):
        """全文件预计总音频秒数（多轨 = 单轨 × 轨数）。未知时 0。"""
        if self.n_tracks > 1 and self.track_secs:
            return sum(self.track_secs)
        if self.exact_secs > 0:
            return self.exact_secs
        if self.loop_end_secs > 0:
            return self.loop_end_secs
        return 0.0

    @property
    def per_track_secs(self):
        """单轨预计秒数（用于按总时长切批次）。"""
        # v4.29: m3u 给了每轨真实长度时，按**最长的一轨**切批 —— 切批只关心
        # 「一个 run 最多装多少音频」，用平均值会让含长曲的 run 超预算。
        if self.track_ids and self.track_lengths:
            vals = [v for v in self.track_lengths.values() if v > 0]
            if vals:
                return float(max(vals))
        if self.track_secs:
            return float(self.track_secs[0])
        if self.exact_secs > 0:
            return float(self.exact_secs)
        if self.loop_end_secs > 0:
            return float(self.loop_end_secs)
        return float(CHIP_DEFAULT_SECS_DEFAULT)

    @property
    def exact(self):
        """时长是否来自文件自带（决定界面时长列是否加 * 标记）。"""
        return self.exact_src in EXACT_SOURCES

    def __repr__(self):
        return (f"<ChipInfo {os.path.basename(self.path)} fmt={self.fmt} "
                f"n={self.n_tracks} exact={self.exact_secs:.2f}"
                f" loop={self.loop_end_secs:.2f} ok={self.ok}>")


# ---------------------------------------------------------------------------
# 魔数嗅探（设计 §2.4）
# ---------------------------------------------------------------------------

def read_magic(path, n=64):
    """读文件前 n 字节；**任何异常返回 b""**（文件正在写入/无权限/是目录）。"""
    try:
        with open(path, "rb") as f:
            return f.read(max(1, int(n)))
    except (OSError, ValueError, TypeError):
        return b""


def _sniff_magic(head):
    """魔数 -> 格式 id；不认识返回 None。

    与 EXT_TABLE 的顺序无关，只看内容。
    """
    if not head:
        return None
    for magic, fmt in MAGIC_TABLE:
        if head.startswith(magic):
            return fmt
    # gzip 魔数只认前 2 字节（\x1f\x8b），但要排除掉明显不是压缩包的极短文件
    return None


def identify(path, head=None):
    """返回 (格式 id, 是否魔数与扩展名冲突)。

    判定顺序（FR-1.3）：魔数嗅探 → 扩展名 → 都不认出则 None。
    **冲突时以魔数为准**。

    head: 可选的前若干字节（供调用方复用已读数据，避免二次读盘）。
    """
    ext = os.path.splitext(path or "")[1].lower()
    if head is None:
        head = read_magic(path, 64)
    # WSR 的魔数（"WSRF"）在**文件最后 32 字节**，头部根本读不到，所以这个
    # 后缀只能信扩展名 —— 否则会按头部字节去猜，偶尔撞上别的格式的魔数。
    if ext == ".wsr":
        return "WSR", False
    by_magic = _sniff_magic(head)
    by_ext = EXT_TABLE.get(ext)

    if by_magic is not None:
        # 魔数优先。冲突 = 扩展名指向了**另一个**已知格式。
        return by_magic, bool(by_ext and by_ext != by_magic)
    # 魔数不认识/读不到（空文件、正在写入）→ 退回扩展名（FR-1.3 第三级）
    return by_ext, False


def is_chip_ext(ext):
    """扩展名（可带点可不带）是否属于芯片音源。"""
    e = (ext or "").lower()
    if not e.startswith("."):
        e = "." + e
    return e in CHIP_EXTS


# ---------------------------------------------------------------------------
# 各格式头解析（设计 §2.5）
#
# ⚠ 下面的偏移量全部经过真实样本实测校正，与设计文档的偏差已在注释里标注。
# ---------------------------------------------------------------------------

def _probe_nsf(head, info):
    """NSF：轨数 u8@0x06 / song 0x0E:0x2E / artist 0x2E:0x4E / copyright 0x4E:0x6E。

    实测（3 个样本，轨数 17/8/15 与解码库 gme_track_count 完全一致）：
      0x05 = 起始轨, 0x06 = 总轨数, 0x0E = song 起点。
    设计文档写 song `0x0E:0x2E` 是对的，但 0x0D 处有 1 字节非文本前缀
    （实测 0xFB/0xB9/0xF2 = 银行切换标志），从 0x0E 起才是干净曲名。
    """
    info.n_tracks = _u8(head, 0x06)
    info.title = decode_text(head[0x0E:0x2E])
    info.author = decode_text(head[0x2E:0x4E])
    # copyright 0x4E:0x6E 只在 artist 空时作为标题兜底（很多 NSF 只填 song）
    if not info.title:
        info.title = decode_text(head[0x4E:0x6E])
    # NTSC speed u16 @0x6E 仅记录不用（设计 §2.5）


def _probe_nsfe(head, info):
    """NSFE：轨数 u8@0x05 / name 0x0D:0x2D / artist 0x2D:0x4D / section 0x10 逐轨时长。

    section `0x10` 是唯一"逐轨带标题 + 时长"的格式（P0 最高价值）。
    """
    info.n_tracks = _u8(head, 0x05)
    info.title = decode_text(head[0x0D:0x2D])
    info.author = decode_text(head[0x2D:0x4D])


def _nsfe_chunk_names(blob):
    """解析 NSFE 的 `tlbl` chunk（4 字节 tag + NUL 分隔的曲名，无长度前缀）。

    ⚠ 实测（1 个真实样本）：真实 NSFE 用的是**分块（chunk）元数据**布局，
      不是设计 §2.5 描述的 section 表：
        0x08 "INFO" + … + "auth"块(artist/copyright/dumper)
                    + u32 总轨数 + "plst"块(播放顺序) + "tlbl"块(逐轨曲名)
      设计写的 name 偏移 0x0D 在本样本里读出的是 0x80/0x03/0x80（地址字节，
      即 INFO 块的 load/init/play 地址），不是曲名。

    返回逐轨曲名列表；遇到非可打印字节即停（tlbl 之后紧跟二进制数据，
    必须严格校验，否则会把 DATA 块当曲名 —— 实测会解析出 138 个"曲名"）。
    """
    idx = blob.find(b"tlbl")
    if idx < 0 or idx + 4 > len(blob):
        return []
    off = idx + 4
    names = []
    while off < len(blob) and len(names) < CHIP_MAX_TRACKS:
        end = blob.find(b"\x00", off)
        if end <= off:
            break
        s = blob[off:end]
        if not all(32 <= c < 127 for c in s):
            break                      # 进入二进制区，停
        names.append(s.decode("latin-1"))
        off = end + 1
    return names


def _parse_nsfe_sections(head, info):
    """解析 NSFE 元数据，提取逐轨标题与时长。**永不抛异常**。

    两条路径（真实样本两种都存在，必须都支持）：
      A. chunk 布局（实测样本走这条）：`tlbl` 块给逐轨曲名；总轨数取
         `plst` 块之前的 u32（实测 15，与解码库 gme_track_count=15 一致）。
         这种布局**没有** length 段 -> exact_secs 保持 0，退回默认单遍。
      B. section 表布局（设计 §2.5 描述）：0x80 起 sid(u8)+soff(u32le)，
         section 0x10 段首 u32 = 全曲总长(ms)，其后每轨 100B。
    读不够就返回已解析部分，ok 保持 True（交给解码库，设计 H14）。
    """
    names, secs = [], []
    length_ms = 0

    # ---- A. chunk 布局 ----
    chunk_names = _nsfe_chunk_names(head)
    if chunk_names:
        names = chunk_names
        p = head.find(b"plst")
        if p >= 4:
            cnt = _u32le(head, p - 4)
            if 1 <= cnt <= CHIP_MAX_TRACKS:
                info.n_tracks = cnt
        if not info.n_tracks:
            info.n_tracks = len(names)
        # artist 从 auth 块取（"auth" + NUL 分隔: artist / copyright / dumper）
        a = head.find(b"auth")
        if a >= 0:
            off = a + 4
            parts = []
            while off < len(head) and len(parts) < 3:
                end = head.find(b"\x00", off)
                if end <= off:
                    break
                s = head[off:end]
                if not all(32 <= c < 127 for c in s):
                    break
                parts.append(s.decode("latin-1"))
                off = end + 1
            if parts and not info.author:
                info.author = parts[0]
        if names and not info.title:
            info.title = names[0]

    # ---- B. section 表布局 ----
    if not names:
        marks = []
        off = 0x80
        while off + 5 <= len(head):
            sid = head[off]
            soff = _u32le(head, off + 1)
            off += 5
            if soff == 0 or soff == 0xFFFFFFFF:
                break
            if sid != 0x10:
                continue
            length_ms = _u32le(head, soff)
            p = soff + 4
            # 每轨 100 字节: u32 起始 ms + 32B title + 32B artist + 32B copyright
            while p + 100 <= len(head):
                marks.append(_u32le(head, p))
                nm = decode_text(head[p + 4:p + 36])
                names.append(nm)
                p += 100
                if len(names) >= CHIP_MAX_TRACKS:
                    break
            break

    if length_ms > 0:
        info.exact_secs = length_ms / 1000.0
        info.exact_src = "nsfe_length"
        if marks:
            secs = []
            for i in range(len(marks)):
                end = marks[i + 1] if i + 1 < len(marks) else length_ms
                secs.append(max(0.0, (end - marks[i]) / 1000.0))
            info.track_secs = secs[:CHIP_MAX_TRACKS]
    if names:
        info.track_names = names[:CHIP_MAX_TRACKS]
        if not info.title:
            info.title = names[0]


def _probe_gbs(head, info):
    """GBS：轨数 u8@0x04 / title 0x39:0x59 / author 0x59:0x79 / coder 0x79:0x99。

    轨数与文本区是实测校正过的（轨数在 0x04 才是真轨数，0x05 是起始轨号；
    文本区在 0x39/0x59/0x79，实测读出的作者名与 m3u 一致）。

    ⚠ **循环点本期一律不采信**（2026-10-18 主理人复核后决定）。
    这里曾经读 0x14/0x16，而 0x10 起是 32 字节标题 —— 那是在**标题字符串里取值**，
    结果 DMG-AWA 被算出 520 秒的"时长"，一首 jingle 会变成 8 分半的音频。

    复核了 6 个真实样本，0x0CC / 0x10C / 0x16C / 0x170 四个候选位置**没有一个自洽**：
      DMG-AWA 四个位置都是 0xFFFF（"无循环点"哨兵）；
      DMG-F4-USA 在 0x16C 读出 37824/32768（后者是 0x8000，明显是垃圾）；
      DMG-F9-USA 在 0x170 读出 823 秒的"jingle"。
    即偏移对了值也未必可用 —— 与 GBS 门禁脚本里的结论一致。

    所以这里只保留字段占位（`loop_end_secs` 恒为 0），时长链自动回落到默认 150 秒。
    真要启用必须先在更多样本上验证"循环点可信"，而不是照规范猜偏移。
    """
    info.n_tracks = _u8(head, 0x04)
    info.title = decode_text(head[0x39:0x59])
    info.author = decode_text(head[0x59:0x79])
    coder = decode_text(head[0x79:0x99])
    if not info.author and coder:
        info.author = coder
    # 循环点不可信 → 不读、不填（见 docstring）。保留字段供将来启用。
    info.loop_end_secs = 0.0


def _probe_vgm_head(head, info, gd3_base=0):
    """VGM/VGZ：轨数恒 1；总采样数 u32le@0x18 ÷ 44100；GD3 标签 u32le@0x14。

    实测（6 个 joshw 样本）：809405/44100=18.354s，与 PRD §FR-3.1 完全一致。
    """
    info.n_tracks = 1
    samples = _u32le(head, 0x18)
    # 0xFFFFFFFF = 未标注
    if samples and samples != 0xFFFFFFFF:
        info.exact_secs = samples / 44100.0
        info.exact_src = "vgm_samples"

    gd3_off = _u32le(head, 0x14)
    if gd3_off and gd3_base + gd3_off + 12 <= len(head):
        _read_gd3(head[gd3_base + gd3_off:], info)


def _read_gd3(blob, info):
    """解析 GD3 标签：'Gd3 ' + u32 ver + u32 len + (title\\0 game\\0 system\\0 dumper\\0)…

    Shift-JIS，失败退 latin-1（设计 A4）。
    """
    if not blob.startswith(b"Gd3 "):
        return
    glen = _u32le(blob, 8)
    if glen <= 0:
        return
    body = blob[12:12 + glen]
    parts = body.split(b"\x00")
    if parts:
        title = decode_text(parts[0])
        if title and not info.title:
            info.title = title
    if len(parts) > 1 and not info.author:
        info.author = decode_text(parts[1])   # game name


# ---------------------------------------------------------------------------
# v4.29: 同目录 .m3u —— PCE-CD / HES rip 的「真实曲目表」
# ---------------------------------------------------------------------------
#
# ============================ 为什么必须读它 ============================
# HES 的 `gme_track_count` **恒为 256**（预分配槽位），真实曲目表只写在
# 同目录 `<包名>.m3u` 里，形如::
#
#     KMCD3005.hes::HES,$04,Overture,3:14,-,10
#     KMCD3005_09 Vampire Killer.ape
#
# 也就是：**芯片音源（HES 行）与 CD 音轨（.ape 行）混在同一张表里** —— 这正是
# PCE-CD 包的形态。不读它的后果（实测）：
#
#   | 样本 | 256 槽扫描 | m3u 真实曲目 | 体积 |
#   |---|---|---|---|
#   | KMCD3005（血之轮回） | 96 首 / 337s / 224MB | 13 首 / 89s | 16MB |
#   | KMCD4007（心跳回忆） | 196 首 / 2532s | 178 首（含 CD 轨） | — |
#   | HCD9005（天外魔境） | 63 首 / 8334s / 1.4GB | 40 首 / ~20min | ~210MB |
#
# 更麻烦的是**估算**：`n_tracks=256 × 150s = 6.3 GB/包` 会撞磁盘门禁
# （`dl_convert._disk_ok`），磁盘剩不到 7GB 时转换**根本不会开始** ——
# 用户看到的就是「下载完了但转换失败/没反应」。按 m3u 收敛后估算回到真实量级。
#
# ⚠ 读取纪律：m3u 是**纯文本、可能带 BOM / Shift-JIS / 坏行**。任何异常都
#   只能退化为「不读」，绝不能影响 probe 的「永不抛异常」契约。

#: m3u 里算作 CD 音轨（本工具不转，但要让用户看见）的扩展名
M3U_CD_EXTS = (".ape", ".wav", ".flac", ".mp3", ".ogg", ".m4a", ".wv", ".tta")


def _m3u_secs(text):
    """``0:39`` / ``1:09`` / ``39`` / ``-`` / ``""`` -> 秒（读不出返回 0.0）。"""
    s = str(text or "").strip()
    if not s or s in ("-", "--"):
        return 0.0
    try:
        total = 0.0
        for part in s.split(":"):
            total = total * 60.0 + float(part)
        return max(0.0, total)
    except (TypeError, ValueError):
        return 0.0


def _m3u_track_no(text):
    """``$0A`` / ``0x0A`` / ``10`` -> 10；读不出返回 None。"""
    s = str(text or "").strip()
    if not s:
        return None
    try:
        if s.startswith("$"):
            return int(s[1:], 16)
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s)
    except (TypeError, ValueError):
        return None


def _m3u_text(path):
    """按 utf-8-sig -> cp932 -> latin-1 依次尝试解码（m3u 没有编码声明）。

    ⚠ **v5.8 修：cp932 几乎不会失败，不能「能解就用」**。
    单字节 0x80-0xFF 里有一大片在 cp932 下映射成**半角片假名**
    （``0xA9 -> ｩ``），于是 latin-1 的包永远轮不到第三个分支 ——
    实测 zophar 的 Game Gear m3u 就是这样：``©1993`` 解成 ``ｩ1993``，
    曲名尾巴挂个假名，还会顺着进文件名。

    判据：**半角片假名紧贴 ASCII 字母/数字**。真正的日文 Shift-JIS 里
    半角片假名是一整段连续的（``ｱﾙﾌｧ``），不会夹在英文单词中间；
    夹在中间只可能是「单字节高位字符被误当假名」。命中就改用 latin-1。
    """
    try:
        raw = open(path, "rb").read()
    except OSError:
        return ""
    try:
        return raw.decode("utf-8-sig")
    except (UnicodeDecodeError, LookupError):
        pass
    try:
        txt = raw.decode("cp932")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("latin-1", "replace")
    # 半角片假名 U+FF61..U+FF9F 与 ASCII 字母数字相邻 -> 多半是 latin-1 误判
    if re.search(r"[0-9A-Za-z][\uff61-\uff9f]|[\uff61-\uff9f][0-9A-Za-z]", txt):
        try:
            return raw.decode("latin-1")
        except (UnicodeDecodeError, LookupError):
            pass
    return txt



def _m3u_candidates(src):
    """给出 ``src`` 可能配套的 m3u 路径（**按可信度排序**）。

    ① 同名：``KMCD3005.hes`` -> ``KMCD3005.m3u``（joshw / 2ch 的固定约定）
    ② 目录里唯一一个**正文引用了本文件名**的 m3u（多包同目录时的兜底）

    ② 的「引用了本文件名」是硬条件：一个目录里可能并排放着好几套 rip，
    随手拿第一个 m3u 会把 A 包的曲目表套到 B 包上 —— 那比不读更糟。
    """
    out = []
    folder = os.path.dirname(os.path.abspath(src)) or os.curdir
    stem = os.path.splitext(os.path.basename(src))[0]
    base = os.path.basename(src).lower()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for n in names:
        if n.lower() == (stem + ".m3u").lower():
            p = os.path.join(folder, n)
            if os.path.isfile(p):
                out.append(p)
    for n in names:
        if not n.lower().endswith(".m3u"):
            continue
        p = os.path.join(folder, n)
        if p in out or not os.path.isfile(p):
            continue
        try:
            txt = _m3u_text(p).lower()
        except Exception:                        # noqa: BLE001
            continue
        if base in txt:
            out.append(p)
    return out


def _parse_m3u(path):
    """解析 m3u -> ``(hes, cd)``。

    Returns:
        ``hes``：``[(曲号 int, 标题 str, 秒 float)]``，按 m3u 出现顺序；
        ``cd``： CD 音轨文件名列表（``.ape`` 等，**本工具不转**）。

    兼容 joshw / 2ch 的两种写法：
    * ``<文件>::HES,$NN,标题,长度,循环,淡出``
    * 纯文件名行（CD 音轨）
    """
    hes, cd = [], []
    text = _m3u_text(path)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "::" not in line:
            low = line.lower()
            if low.endswith(M3U_CD_EXTS):
                cd.append(os.path.basename(line))
            continue
        _ref, spec = line.split("::", 1)
        # ⚠ **不逐段 strip**：标题里可能有逗号（"Poison, Mind"），逐段 strip
        #   后 join 回来会把 ", " 变成 ","（实测被自己的测试抓到）。
        parts = spec.split(",")
        if not parts or parts[0].strip().upper() != "HES":
            continue
        if len(parts) < 2:
            continue
        no = _m3u_track_no(parts[1])
        if no is None or not (0 <= no < CHIP_MAX_TRACKS):
            continue
        # 尾部三列是「长度, 循环, 淡出」；标题里可能有逗号，所以用负索引取
        if len(parts) >= 6:
            secs = _m3u_secs(parts[-3])
            title = ",".join(parts[2:-3]).strip()
        elif len(parts) >= 4:
            # 截断写法（缺 fade/loop）：只剩「标题, 长度」
            secs = _m3u_secs(parts[-1])
            title = ",".join(parts[2:-1]).strip()
        else:
            secs, title = 0.0, ""
        hes.append((no, title, secs))
    return hes, cd


def _apply_hes_m3u(info):
    """把同目录 m3u 的真实曲目表灌进 ``info``（读不到就什么都不做）。"""
    src = getattr(info, "path", "") or ""
    if not src:
        return
    for cand in _m3u_candidates(src):
        try:
            hes, cd = _parse_m3u(cand)
        except Exception:                        # noqa: BLE001
            continue
        if not hes:
            # 只有 CD 轨（纯 CD-DA 包）也要记下来：让调用方能明确告诉用户
            # 「这不是坏了，是这个包本来就没有芯片音源」。
            if cd:
                info.cd_tracks = cd
                info.m3u_path = cand
            continue
        ids = [n for n, _t, _s in hes]
        lens = {n: s for n, _t, s in hes if 0.0 < s <= 3600.0}
        info.track_ids = ids
        info.track_lengths = lens
        info.track_titles = {n: t for n, t, _s in hes if t}
        info.cd_tracks = cd
        info.m3u_path = cand
        # ⚠ n_tracks 的语义是「解码区间要覆盖到哪」：父进程按
        #   ``range(0, n_tracks)`` 切片，所以必须覆盖到**最大曲号**，
        #   而不是曲目条数（Tokimeki 的曲号是 0/4/2/0x65/0xB1… 散开的）。
        try:
            cover = max(ids) + 1
        except ValueError:
            cover = CHIP_MAX_TRACKS
        info.n_tracks = min(CHIP_MAX_TRACKS, max(1, cover))
        return


def _probe_hes(head, info):
    """HES：无标题/无时长；轨数按解码库口径恒为 256。

    ⚠ 实测校正（3 个样本）：设计文档 §2.5 写的"轨数 u8@0x03"是错的。
      字节 0x03 实测恒为 0x4D（'M'，即 "HESM" 的第 4 个字节），根本不是轨数。
      真实字段在 0x15（实测 95/191/127），但 **gme_track_count 恒返回 256**
      ——解码库把 HES 一律当 256 轨，多出来的空轨靠"首帧峰值 0"识别
      （PRD T8 实测 HC63013 track 4 / HC91048 track 0 峰值 0）。

      因此 n_tracks 固定取 256：AC-4 要求"产出行数 == gme_track_count − 空轨数"，
      若这里报 95 而实际解码 256 轨，估算分母就错了（FR-5.2 明确禁止按 1 算）。
      空轨由解码前的峰值检测跳过，不影响正确性。
    """
    # ⚠ 原先此处写「轨偏移表在 0x1FE 起 256×u16be」—— **该说法已被实测证伪，已删除**：
    #   HC63013（0x15=95，24576B）与 HC91048（0x15=191，49152B）容量差一倍，
    #   但 0x1FE 起 512 字节的 SHA256 完全相同（810a403744f1c4e9）——
    #   两个布局本应不同的文件读出同一段数据，说明那是**噪声而非偏移表**。
    #   「留着+标未验证」只适用于「不知道对不对」，不适用于「已知道是错的」。
    #
    #
    # ⚠⚠ `n_tracks = 256` 是**解码分母**，一行都不能动（AC-4 / FR-5.2）。
    info.n_tracks = CHIP_MAX_TRACKS

    # ---- v4.2: head[0x15] = **容器尺寸字段**（⚠ 不是轨数，禁止用于估算）----
    # ============================ 实测结论（2026-10-18）============================
    # ① **7/7 真实样本满足 `size == (head[0x15] + 1) × 256`** —— 每槽固定 256 字节
    #    **预分配**；若它是「实际占用」，文件大小不可能与之严格成正比。
    # ② **真解码数产出（7 样本，`chip_decode` 实跑）**：
    #    | 样本 | 0x15 | 产出WAV | 0x15/产出 |
    #    |---|---|---|---|
    #    | AKCD3001 | 127 | 23 | 5.52 |
    #    | HC63013 | 95 | 183 | **0.52** |
    #    | HC91048 | 191 | 101 | 1.89 |
    #    | HE93003 | 127 | 112 | 1.13 |
    #    | JC63011 | 95 | 18 | 5.28 |
    #    | PV1004 | 63 | 150 | **0.42** |
    #    | TP02011 | 63 | 116 | **0.54** |
    #    | **合计** | **761** | **703** | **1.08** |
    #
    # ③ **结论（这就是 ① 被否决的实测依据）**：
    #    · **总量上准**（1.08 倍，几乎完美）；
    #    · **单文件上极不稳**（0.42 ~ 5.52，跨度 13 倍；7 个里 3 个**低估**）；
    #    · 要保证「绝不低估」就得乘安全系数，**临界点 = 1792/761 = 2.355**
    #      （取整成 2.4 会让人复算不出临界点，验收时对不上数）：
    #      **761×2.355 = 1792 ≈ 256 口径的 1792 -> 收益归零**；再往上是负收益。
    #    → 所以**估算继续用 n_tracks(=256)**（恒高估 2.55 倍，方向安全），
    #      **本字段不得用于任何估算/门禁/时间预测**。
    #    ⚠ 注意 ③ 与「总量 1.08」并不矛盾：**总量准 ≠ 单文件可用**，
    #      估算必须逐文件给数字，所以 13 倍的单文件跨度才是决定性的。
    #
    # ⚠ 三条读取纪律（probe 契约「永不抛异常」）：
    #   1. 读不到 / 越界 -> 静默留 0；2. `0 < v <= CHIP_MAX_TRACKS` 越界即丢弃；
    #   3. **绝不动 n_tracks**（AC-4 / FR-5.2 的解码分母）。
    try:
        if len(head) > 0x15:
            v = int(head[0x15])
            if 0 < v <= CHIP_MAX_TRACKS:
                info.n_tracks_used = v
    except (IndexError, TypeError, ValueError):
        pass

    # ---- v4.29: 同目录 .m3u 才是真实曲目表（见 _apply_hes_m3u 的详注）----
    # 读不到 / 解析失败 -> 保持 256 槽全扫（旧行为，零回归）。
    # ⚠ PCE-CD 包里 m3u 同时列出 HES 曲目与 CD 音轨（.ape），后者本工具不转，
    #   但会记进 info.cd_tracks 供界面明示 —— 否则用户以为「转换少了东西」。
    _apply_hes_m3u(info)


def _probe_spc_id666(head, info):
    """SPC：复用 core 的 ID666 解析（本模块不 import core，故此处自解析同一布局）。

    与核心模块 _spc_id666_title 的偏移一致：曲名 0x2E(26) / 游戏名 0x48(32)
    / 秒数 0xA9(3 位 ASCII) / 淡出 0xAC(5 位 ASCII)。
    """
    info.n_tracks = 1
    info.title = decode_text(head[0x2E:0x48])
    game = decode_text(head[0x48:0x68])
    if game and not info.author:
        info.author = game
    secs = _id666_num(head, 0xA9, 0xAC, 1, 3600)
    if secs:
        info.exact_secs = float(secs)
        info.exact_src = "id666"


def _id666_num(head, lo, hi, minv, maxv):
    """ID666 定长十进制文本字段 -> 整数（非数字/越界返回 0）。"""
    txt = head[lo:hi].decode("latin-1", "replace").strip().strip("\x00").strip()
    if not txt.isdigit():
        return 0
    v = int(txt)
    return v if minv <= v <= maxv else 0


# ---------------------------------------------------------------------------
# P1 四种（KSS / GYM / SAP / AY）
#
# 全部偏移都以**真实样本 + libgme 双向自洽**为准（vendor/probe_p1_offsets.py
# 可复跑）。凡是"看起来像但不自洽"的字段一律不采信 —— 这条纪律来自上一版
# GBS 把循环点读在标题字符串里、把一首 jingle 算成 520 秒的教训。
# ---------------------------------------------------------------------------

#: KSS 头长度（libgme Kss_Emu::header_size）
KSS_HEADER_SIZE = 0x40
#: GYM 头长度（libgme Gym_Emu::header_size = 428）
GYM_HEADER_SIZE = 428
#: AY 头长度（libgme Ay_Emu::header_size = 0x14）
AY_HEADER_SIZE = 0x14


def _parse_m3u_time(s):
    """``"1:11"`` -> 71.0；``"1:02:03"`` -> 3723.0；空/异常 -> 0.0。"""
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        parts = s.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return (int(parts[0]) * 3600 + int(parts[1]) * 60
                    + float(parts[2]))
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _parse_sidecar_m3u(path):
    """读同目录 m3u 的曲目表，返回 ``(titles, ids, lengths)``。

    joshw 的两种写法**曲号进制不同，必须都认**：

        KSS:  T-81087.kss::KSS,0,BGM #01,1:11,,10          ← 曲号十进制
        SGC:  T-81048.sgc::KSS,$00,20th Century Fox...,0:17,,1  ← 十六进制带 $

    字段：``文件名::格式,曲号,曲名,时长,fade,循环``。

    ⚠ 为什么必须有它：libgme 把 KSS 一律当 **256 轨**（与内容无关），
    没有 m3u 就要解 256 × 150s ≈ 10 小时 —— 用户看到的是「卡在解码」。
    实测 ``Addams Family [GG]/T-81087.kss`` 只有 11 首，m3u 里写得很清楚。

    v5.7：**m3u 的文件名有两种约定，都要认**。

    * joshw：``<包名>.m3u``（一个文件装全套曲目）或 ``!playlist.m3u``；
    * zophar：**一首一个 m3u**，叫 ``01 BGM #01.m3u`` / ``02 BGM #02.m3u`` …
      —— 名字里根本没有源文件名。

    只认前者的话，zophar 的 Game Gear 包里 ``T-81087.kss`` 会被当成 256 轨
    （实测解出 256 个 WAV、6.6 GB，而真实曲目只有 11 首）。
    所以第三轮兜底：扫目录里**所有** ``*.m3u``，但要求正文里出现本源文件名
    —— 一个目录里并排放好几套 rip 时，这条「引用自己」的硬条件能防止
    把 A 包的曲目表套到 B 包上（那比不读更糟）。
    """
    d = os.path.dirname(os.path.abspath(str(path or "")))
    stem = os.path.splitext(os.path.basename(str(path or "")))[0]
    base = os.path.basename(str(path or "")).lower()
    titles, ids, lengths = {}, [], {}
    tried = set()

    def _absorb(p):
        """读一个 m3u，把曲目并进 titles/ids/lengths。返回读到几行。

        ⚠ 两个必须处理的历史坑（v5.7 修）：

        1. **编码**：m3u 没有编码声明，实测 zophar 的包是 latin-1（``©`` 写成
           ``0xA9``），joshw 的包有 Shift-JIS。以前直接 ``open(utf-8)``，
           曲名里的 ``©`` 会变成 ``\\ufffd``。改用 :func:`_m3u_text` 的三级回退。
        2. **曲名里带逗号**：``BGM #01 - ? - The Addams Family - ©1993 Arc
           Developments\\, Flying Edge`` —— 逗号被转义成 ``\\,``。不认这个转义，
           按 ``,`` 硬切会把字段整体错位：曲名截断、时长读成 0（表现为
           「明明有 m3u 却不知道每首多长」）。
        """
        got = 0
        text = _m3u_text(p)
        if not text:
            return 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "::" not in line:
                continue
            body = line.split("::", 1)[1]
            # 先把转义逗号换成占位符，切完再换回来
            parts = [x.replace("\x00", ",")
                     for x in body.replace("\\,", "\x00").split(",")]
            if len(parts) < 2:
                continue
            raw = parts[1].strip()
            try:
                idx = (int(raw[1:], 16) if raw.startswith("$")
                       else int(raw, 10))
            except (TypeError, ValueError):
                continue
            title = parts[2].strip() if len(parts) > 2 else ""
            secs = _parse_m3u_time(parts[3]) if len(parts) > 3 else 0.0
            if title:
                titles[idx] = title
            if secs > 0:
                lengths[idx] = secs
            if idx not in ids:
                ids.append(idx)
            got += 1
        return got

    for name in (stem + ".m3u", "!playlist.m3u"):
        p = os.path.join(d, name)
        if p in tried or not os.path.isfile(p):
            continue
        tried.add(p)
        if _absorb(p):
            return titles, ids, lengths

    # v5.7 兜底：zophar 那种「一首一个 m3u」。按文件名排序读，保证曲号顺序稳定。
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return titles, ids, lengths
    for name in names:
        if not name.lower().endswith(".m3u"):
            continue
        p = os.path.join(d, name)
        if p in tried or not os.path.isfile(p):
            continue
        tried.add(p)
        try:
            raw = open(p, "rb").read()
        except OSError:
            continue
        # 「引用自己」的硬条件（防止同目录多套 rip 互相串味）
        if base.encode("utf-8", "ignore").lower() not in raw.lower():
            continue
        _absorb(p)
    return titles, ids, lengths


def _probe_kss(head, info):
    """KSS：轨数恒 256；无标题/无时长字段。

    魔数（实测 hexdump）：真实 MSX KSS 头是 `4b 53 43 43` = **"KSCC"**。
    libgme `Kss_Emu::check_kss_header` 只接受 `KSCC` 与 `KSSX` 两种tag。

    ⚠ **KSS 没有可靠的统一魔数可用来"认出内容"**，所以识别分两路：
      1. 内容里出现 KSCC/KSSX -> 直接判KSS（最可靠，见上）；
      2. 否则退回扩展名 `.kss`（FR-1.3 第三级）。
    误判风险：`.kss` 只是扩展名约定，任何改名成 .kss 的文件都会被当KSS 交给
    解码器 —— 但那正是**扩展名识别的固有语义**，且解码失败会被 chip_decode
    记成一条 skip（AC-19），不会影响整批。

    ⚠ 另一个实测坑：KSS 站（kss.joshw.info）里 Game Gear 一类的包给的是
    `.sgc` 文件（头 `53 47 43 1a` = "SGC\\x1a"）。**libgme 拒绝它**
      （Wrong file type for this emulator，实测），所以 `.sgc` 不在本工具
      支持范围内，也**不能**并入 KSS 魔数 —— 那会产生"识别成 KSS 但必然
      解码失败"的假支持。该样本已隔离到 vendor/samples/_rejected/。

    轨数：libgme 把 KSS 一律当 **256 轨**（`gme_kss_type_` 里写死的 256），
    与文件内容无关。这里同样固定 256：若报别的数，FR-5.2 的估算分母就错了。
    多出来的空轨由解码前的首帧峰值检测跳过（同 HES 的处理）。
    时长：KSS 头里没有可信的时长字段，`gme_track_info().length` 实测恒为
    **-1（未知）**，所以 exact_secs 保持 0，时长链回落到默认 150 秒 + 静音检测。
    """
    # v5.4: 同目录 m3u 有真实曲目表 -> 只解那些曲号。
    #   libgme 把 KSS 一律当 256 轨，没有 m3u 就要解 256 × 150s ≈ 10 小时
    #   （界面表现：「卡在解码」）。实测 Addams Family[GG] 只有 11 首。
    _titles, _ids, _lens = _parse_sidecar_m3u(info.path)
    if _ids:
        info.track_ids = list(_ids)
        info.track_titles = dict(_titles)
        info.track_lengths = dict(_lens)
        info.n_tracks = len(_ids)
    else:
        info.n_tracks = CHIP_MAX_TRACKS       # 没有 m3u 才退回 256
    info.title = ""
    info.author = ""


def _gym_frame_count(head, start):
    """遍历 GYM 指令流数帧数（1/60 秒/帧）。

    指令（libgme `Gym_Emu.cpp: gym_track_length()` 逐行一致）：
        0x00            空转一帧
        0x01 rr vv      YM port0 写（3 字节）
        0x02 rr vv      YM port1 写（3 字节）
        0x03 vv         PSG 写   （2 字节）
        其它            **跳过 1 字节后继续**（libgme 的 switch 没有 default，
                      `*p++` 已经前进，什么都不做就继续循环）
    ⚠ 最后这条是实测踩出来的：第一版写成"遇到未知 opcode 就停"，BGM 1 只数出
    7 帧（116ms），与 libgme 报的 46366ms 差 400 倍。改对之后帧数完全吻合。

    返回 (帧数, 是否扫完整条流)。读不够（head被PROBE_MAX_READ 截断）时返回
    已数到的帧数，交给调用方判断能不能用。
    """
    p = start
    n = len(head)
    frames = 0
    while p < n:
        op = head[p]
        p += 1
        if op == 0:
            frames += 1
        elif op in (1, 2):
            if p + 2 > n:
                return frames, False
            p += 2
        elif op == 3:
            if p + 1 > n:
                return frames, False
            p += 1
        # 其它 opcode：跳过这 1 字节继续（与 libgme 一致）
    return frames, True


def _probe_gym(head, info):
    """GYM：轨数恒 1；时长 = 帧数 × 1000/60 ms（**可信，走exact 链**）。

    头布局（libgme `Gym_Emu::header_t`，共 428 字节）：
        0x000 tag[4]="GYMX"   0x004 song[32]   0x024 game[32]
        0x044 copyright[32]   0x064 emulator[32]  0x084 dumper[32]
        0x0A4 comment[256]    0x1A4 loop_start u32le   0x1A8 packed u32le
        0x1AC 起= 指令流

    实测（真实样本 BGM 1.GYM / BGM 2.gym）：
        帧数 2782 -> 46366 ms，libgme track_info().length 报 46366ms —— 完全一致；
        帧数  586 ->  9766 ms，libgme 报 9766 ms —— 完全一致。
    这是 P1 四种里**唯一**时长链上真正可信的文件自带时长（与 NSFE length /
    VGM samples 同级），所以填 exact_secs + exact_src="gym_frames"。

    ⚠ 无头 GYM（老Genecyst 原始格式）：libgme 允许 `*in <= 3` 即视为裸指令流。
      这种文件没有 428 字节头，指令流从头 0x00 开始，整条流都是 0x00 时帧数
      会等于文件字节数 —— 实测一个 187735 字节的全零文件被算成 3128916ms
      （52 分钟）。所以**只对有 GYMX 头的情况**填 exact_secs；无头的一律
      回落默认 150 秒。这也是本轮唯一的时长语义闸门。
    """
    info.n_tracks = 1
    if len(head) < 4 or head[:4] != b"GYMX":
        # 无头 GYM：识别成立但时长不可信（见 docstring）
        return

    if len(head) < GYM_HEADER_SIZE:
        return                       # 头被截断，读到的字段不可信，全部留空

    info.title = decode_text(head[0x04:0x24])          # song
    info.author = decode_text(head[0x44:0x64])         # copyright
    # game / dumper 只在 title/author 为空时兜底
    if not info.title:
        info.title = decode_text(head[0x24:0x44])      # game
    if not info.author:
        author = decode_text(head[0x84:0xA4])          # dumper
        if author:
            info.author = author

    # packed != 0 表示后续数据是压缩的，libgme 直接报
    # "Packed GYM file not supported" —— 这时时长也不可信，别填。
    packed = _u32le(head, 0x1A8)
    if packed:
        return

    frames, complete = _gym_frame_count(head, GYM_HEADER_SIZE)
    if not complete:
        # 指令流被PROBE_MAX_READ(8KB) 截断 -> 帧数只是下限，不作数
        return
    # ⚠ 闸门：上限。真值可能极大 —— 实测 BGM 1.GYM 有 2782 帧 = **46366 秒**
    #   （13 小时，原始驱动 dump、无循环点，libgme 也报 length=46366）。
    #   这种"跑不完"的文件必须回落到默认 150 秒，而不是把 13 小时当成单曲时长。
    #   注意 frames -> 秒的换算是 ÷60（不是 ×1000，那是毫秒）。
    secs = frames / 60.0
    if 0.2 < secs <= CHIP_MAX_SECS:
        info.exact_secs = secs
        info.exact_src = "gym_frames"
        info.track_secs = [secs]


def _sap_header_tags(head):
    """解析 SAP 的 CRLF 文本头 -> {TAG: value}。**永不抛异常**。

    SAP 头是 `SAP\\x0D\\x0A` 之后的一串 `TAG value\\x0D\\x0A` 行，遇到
    `\\xFF\\xFF` 结束，之后是二进制。

    ⚠ 两个实测坑：
      1. 行尾是 **CRLF**。按 0x0D 切行后，下一行开头会残留一个 0x0A；
         不去掉的话标签会变成 "\\nNAME" 而匹配不上（实测所有标签解析为空）。
      2. 二进制区里可能再出现 0x0D。必须**在遇到 0xFF 0xFF 时立刻停**，
         否则会把二进制当标签解析（实测会解析出几KB 乱码）。
    """
    tags = {}
    if len(head) < 5 or head[:5] != b"SAP\x0d\x0a":
        return tags
    pos = 5
    end = len(head)
    while pos < end:
        if head[pos] == 0xFF and head[pos + 1:pos + 2] == b"\xFF":
            break                                   # 二进制区开始
        line_end = head.find(b"\x0d", pos)
        if line_end < 0:
            break
        raw = head[pos:line_end]
        if raw[:1] == b"\x0a":                      # 去掉上一行 CRLF 的 LF
            raw = raw[1:]
        # 只收可打印 ASCII 行，二进制误入时自然被丢弃
        if raw and all(32 <= c < 127 for c in raw):
            text = raw.decode("latin-1")
            sp = text.find(" ")
            if sp > 0:
                tags[text[:sp].strip().upper()] = text[sp + 1:].strip()
            else:
                tags[text.strip().upper()] = ""
        pos = line_end + 1
    return tags


def _sap_int(tags, key):
    """SAP 标签的十六进制/十进制值 -> int；解析失败返回 None。

    注意 SONGS 是**十进制**，INIT/PLAYER/MUSIC 是**十六进制**，
    且值后面可能跟 `;`注释。
    """
    raw = (tags.get(key) or "").split(";")[0].strip()
    if not raw:
        return None
    try:
        if key == "SONGS":
            return int(raw, 10)
        return int(raw, 16)
    except ValueError:
        return None


def _probe_sap(head, info):
    """SAP：轨数来自 `SONGS` 标签（缺省 1）；标题/作者来自 NAME/AUTHOR 标签。

    实测（7 个真实样本）：`SONGS` 与 libgme `gme_track_count` **7/7 全部一致**
    （Playful_Professor 5 轨，其余 1 轨）。

    ⚠ 时长：SAP 头里有个 `TIME 01:32.41 LOOP` 这样的**自由文本**标签，
      libgme 自己都不解析它（`gme_track_info().length` 实测恒为 -1）。
      格式未定义其语义（LOOP 表示循环、冒号是分秒、精度到百分秒），各工具
      写法不统一，**照字符串猜时长必然出错**（与 GBS 循环点同类问题）。
      所以 exact_secs 保持 0，走默认 150 秒 + 静音检测。
    """
    info.n_tracks = 1
    tags = _sap_header_tags(head)
    if not tags:
        return

    songs = _sap_int(tags, "SONGS")
    if songs and 1 <= songs <= CHIP_MAX_TRACKS:
        info.n_tracks = songs
    else:
        # 真实样本里SONGS 常常缺失；libgme 的 Sap_Emu 也是缺省 track_count=1
        info.n_tracks = 1

    name = (tags.get("NAME") or "").strip().strip('"')
    author = (tags.get("AUTHOR") or "").strip().strip('"')
    info.title = name
    info.author = author

    # TYPE D = Digimusic，libgme 的 parse_info 明确 return "Digimusic not
    # supported"（实测：某个样本 gme_open_file 直接失败）。这类文件识别为 SAP
    # 但必然解码失败 —— 提前告知用户，比让它在 worker 里报错更清楚。
    if (tags.get("TYPE") or "").strip().upper().startswith("D"):
        info.ok = False
        info.reason = "这是 Digimusic (SAP TYPE D) 文件，解码库不支持"


def _ay_be16(head, off):
    """AY 用**大端**（Motorola）字。越界返回 None。"""
    if off < 0 or off + 2 > len(head):
        return None
    return struct.unpack_from(">H", head, off)[0]


def _ay_rel(head, pos, need=1):
    """AY 的指针是**相对当前记录位置的带符号偏移**（大端 16 位）。

    返回 (绝对 offset 或 None)。越界返回 None —— 这是格式自带的边界检查，
    不是"猜"，照做即可（libgme `Ay_Emu.cpp: get_data()` 同款逻辑）。
    """
    rel = _ay_be16(head, pos)
    if rel is None:
        return None
    if rel >= 0x8000:
        rel -= 0x10000
    tgt = pos + rel
    if tgt < 0 or tgt + need > len(head):
        return None
    return tgt


def _ay_cstr(head, pos):
    """从 pos 读 NUL 结束的字符串（可打印才返回，否则""）。"""
    if pos is None or pos < 0 or pos >= len(head):
        return ""
    end = head.find(b"\x00", pos)
    if end <= pos:
        return ""
    raw = head[pos:end]
    if not all(32 <= c < 127 for c in raw):
        return ""
    return raw.decode("latin-1")


def _probe_ay(head, info):
    """AY：轨数 = `max_track` + 1；逐轨时长来自 SongLength（1/50 秒）。

    头布局（libgme `Ay_Emu::header_t`，共 0x14 字节）：
        0x00 tag[8]="ZXAYEMUL"  0x08 vers  0x09 player  0x0A unused[2]
        0x0C PAuthor(相对)      0x0E PMisc(相对)
        0x10 max_track         0x11 first_track
        0x12 PSongsStructure(相对) -> 逐轨 {PSongName, PSongData} 各 2 字节
    每轨 SongData 布局：+0 AChan +1 BChan +2 CChan +3 Noise
                        +4 SongLength(大端, 单位 1/50 秒) +6 FadeLength
                        +8 HiReg +9 LoReg +10 PPoints +12 PAddresses

    实测（7 个真实样本）：轨数与 libgme `gme_track_count` **7/7 一致**
    （AlienStorm 7 轨、AlteredBeast 5 轨、其余 1 轨）；
    逐轨 SongLength×20ms 与 libgme `length` **逐轨完全一致**
    （65280/32640/4000/4400/1800/15000/4400 ms全部吻合）。

    所以 AY 的时长**可信**，走exact 链，source="ay_frames"。
    ⚠ 但仍加一道闸门：只要任一轨的指针越界（文件被PROBE_MAX_READ 截断，
      或本身损坏），就**整体不填exact**，退回默认 150 秒 —— 半截的指针表算
      出来的时长必然是错的。
    """
    info.n_tracks = 1
    if len(head) < AY_HEADER_SIZE or head[:8] != b"ZXAYEMUL":
        return

    max_track = _u8(head, 0x10)
    n_tracks = max_track + 1
    info.n_tracks = n_tracks if 1 <= n_tracks <= CHIP_MAX_TRACKS else 1

    info.author = _ay_cstr(head, _ay_rel(head, 0x0C, 1))
    misc = _ay_cstr(head, _ay_rel(head, 0x0E, 1))

    songs_off = _ay_rel(head, 0x12, n_tracks * 4)
    if songs_off is None:
        return                       # 指针表读不到 -> 轨数仍可用，时长不填

    names, secs = [], []
    complete = True
    for i in range(n_tracks):
        rec = songs_off + i * 4
        nm = _ay_cstr(head, _ay_rel(head, rec, 1))
        data_off = _ay_rel(head, rec + 2, 6)
        if data_off is None:
            complete = False
            names.append(nm)
            secs.append(0.0)
            continue
        frames = _ay_be16(head, data_off + 4)
        # SongLength == 0 表示"长度未知/无限循环"，不是 0 秒
        secs.append(float(frames) * 1000.0 / 50.0 if frames else 0.0)
        names.append(nm)

    if names:
        info.track_names = [n for n in names[:CHIP_MAX_TRACKS]]
        if not info.title:
            info.title = next((n for n in names if n), "")
    if not info.author and misc:
        info.author = misc

    if complete and secs and all(s > 0 for s in secs) and \
            all(0.2 < s / 1000.0 <= CHIP_MAX_SECS for s in secs):
        # 注意 secs 的单位是**毫秒**（见上文实测 65280ms = 65.28 秒），
        # 与 CHIP_MAX_SECS（秒）比较时必须换算 —— 写成 `s <= CHIP_MAX_SECS`
        # 会把 65 秒的曲子误判成超限，实测踩过。
        info.track_secs = [s / 1000.0 for s in secs[:CHIP_MAX_TRACKS]]
        info.exact_secs = max(secs) / 1000.0
        info.exact_src = "ay_frames"


# ---------------------------------------------------------------------------
# probe（设计 §2.4）
# ---------------------------------------------------------------------------

def _read_head(path, size):
    """安全读取前 size 字节，失败返回 b""。"""
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except (OSError, ValueError, TypeError):
        return b""


def _probe_vgz(path, info):
    """VGZ：gzip 压缩的 VGM。用 stdlib gzip 只解压头部若干字节（微秒级）。"""
    head = b""
    try:
        with gzip.open(path, "rb") as f:
            head = f.read(_VGZ_HEAD_MAX)
    except (OSError, EOFError, ValueError):
        # 不是合法 gzip（或截断）→ 仍按扩展名当 VGM 试一次裸解析
        head = _read_head(path, PROBE_MAX_READ)
        if not head.startswith(b"Vgm "):
            info.reason = "VGZ 解压失败，文件可能已损坏或不是 gzip 格式"
            return
    _probe_vgm_head(head, info)


def _probe_sgc(head, info):
    """SGC（Sega Game Gear / Master System / Coleco 快照）头解析（v5.4）。

    头固定 **0xA0（160）字节**，字段定义见 kode54 的
    ``Game_Music_Emu/gme/Sgc_Impl.h::header_t``：

        0-3    tag "SGC\\x1a"
        4      vers
        5      rate          0=NTSC 1=PAL
        6-7    reserved1
        8-9    load_addr
        10-11  init_addr
        12-13  play_addr
        14-15  stack_ptr
        16-17  reserved2
        18-31  rst_addrs[7]（每项 2 字节）
        32-35  mapping（Sega only）
        36     first_song
        37     song_count
        38     first_effect
        39     last_effect
        40     system        0=Master System 1=Game Gear 2=ColecoVision
        41-63  reserved3
        64-95  game[32]
        96-127 author[32]
        128-159 copyright[32]

    实测 T-81048.sgc（Alien 3 / Game Gear）：song_count=13（与 m3u 的 13 首
    完全一致）、system=1、game="Alien 3"、author="Matt Furniss"、
    copyright="1992 Arena"、load=init=0x7F40、play=0x7F80、stack=0xDFEE。

    ⚠ 别按「13 字节头」去解 —— 我第一版就是这么猜的，全错（start 指向的
    0x4000 处是图形数据不是代码）。头是 **0xA0**，data 从 0xA0 开始。
    """
    if len(head) < 0xA0:
        info.ok = False
        info.reason = ("SGC 头不足 0xA0 字节（只有 %d），文件可能被截断"
                       % len(head))
        return
    song_count = head[37]
    info.n_tracks = song_count if song_count else 1
    info.title = decode_text(head[64:96])
    info.author = decode_text(head[96:128])
    sys_id = head[40]
    sys_name = {0: "Master System", 1: "Game Gear",
                2: "ColecoVision"}.get(sys_id)
    if sys_name:
        info.system = sys_name
    # m3u 曲名/时长（SGC 的曲号是十六进制，带 $ 前缀）
    _titles, _ids, _lens = _parse_sidecar_m3u(info.path)
    if _ids:
        info.track_ids = list(_ids)
        info.track_titles = dict(_titles)
        info.track_lengths = dict(_lens)
        info.n_tracks = len(_ids)
    info.ok = True


def _probe_wsr(path, info):
    """WSR（WonderSwan Sound Rip）—— 头在**文件尾部**，曲目表在**同目录 m3u**。

    头是最后 32 字节（atwiki「soundfile」WSR 页）::

        0xFFFE0  4B  "WSRF"
        0xFFFE4  1B  Version
        0xFFFE5  1B  最初の曲番号（first song number）
        0xFFFE6 10B  予約領域
        0xFFFF0  5B  x86 スタートアップ（far JMP）
        0xFFFFA  1B  ROM容量

    曲目数：**先看 m3u**（joshw 的 WonderSwan 包一定带，格式和 KSS/SGC 一样
    ``file::WSR,曲号,曲名,时长,fade,循环``）—— 里面有曲号、曲名、真实时长。
    没有 m3u 才回落到「逐曲号跑 1 秒看有没有声音」的实测扫描。

    ⚠ **扫描会多报，不能当权威**。实测 ``SWJ-BAN01A.wsr``
    （Card Captor Sakura）：m3u 列 33 首，扫描报 **121 首** —— 多出来的是
    噪声/残响。按 121 首 × 150 秒渲染 = 5 小时音频、2.1 GB，界面又没有任何
    进度，用户看到的就是「卡在转换中，一直无进度」。
    """
    try:
        import wsr_native
    except Exception as e:                              # noqa: BLE001
        info.ok = False
        info.reason = "缺少 wsr_native 模块：%s" % e
        return
    foot = wsr_native.read_footer(path)
    if not foot["ok"]:
        info.ok = False
        info.reason = foot["error"] or "不是 WSR 文件"
        return
    ok, why = wsr_native.decoder_ready()
    if not ok:
        info.ok = False
        info.reason = why
        return
    info.title = os.path.splitext(os.path.basename(path))[0]

    # 1) m3u 曲目表 —— 权威且零成本
    playlist = wsr_native.read_playlist(path)
    if playlist:
        info.n_tracks = len(playlist)
        info.track_ids = [no for no, _t, _s in playlist]
        info.track_titles = {no: t for no, t, _s in playlist if t}
        info.track_lengths = {no: s for no, _t, s in playlist if s > 0}
        info.ok = True
        return

    # 2) 没有 m3u 才实测扫描（200 个曲号约 1.7 秒，结果按 path+mtime+size 缓存）
    hits = wsr_native.scan_tracks(path, secs=wsr_native.PROBE_SECS,
                                  max_tracks=200)
    info.n_tracks = len(hits) or 1
    if hits:
        info.track_ids = list(hits)
    info.ok = True


def _s98_walk(path, data_ofs):
    """扫一遍 S98 数据流，返回 ``(ticks, n_writes, ended)``。

    编码规则照 libvgm ``player/s98player.cpp`` 的 ``CalcSongLength()`` 抄，
    实测 18 个真实文件全部解析到 0xFD 收尾：

        0xFF        -> 前进 1 tick
        0xFE <var>  -> 前进 2 + var tick（var 是 LEB128：低位在前，bit7 = 续位）
        0xFD        -> 文件结束
        其它 cmd    -> cmd 的 bit0 是端口号、bit1.. 是设备号，后面跟 reg, data

    ⚠ 别把数据流当成「固定 4 字节一组 [wait][port][reg][val]」。等待是
      **单独的 0xFF 命令**，不是每条记录里的字段 —— 我第一版就是按固定
      宽度穷举的，四种字节序全试过，全错。
    """
    ticks = 0
    writes = 0
    ended = False
    with open(path, "rb") as f:
        f.seek(data_ofs)
        buf = f.read()
    n = len(buf)
    i = 0
    while i < n:
        c = buf[i]
        i += 1
        if c == 0xFF:
            ticks += 1
        elif c == 0xFE:
            v = 0
            sh = 0
            while i < n:
                b = buf[i]
                i += 1
                v |= (b & 0x7F) << sh
                sh += 7
                if not (b & 0x80) or sh > 28:
                    break
            ticks += 2 + v
        elif c == 0xFD:
            ended = True
            break
        else:
            i += 2
            writes += 1
    return ticks, writes, ended


def _probe_s98(path, head, info):
    """S98（PC-98 / PC-88 的 YM2608 寄存器写入日志）。

    头布局（libvgm ``s98player.cpp`` 的 ``LoadFile()``，实测 18/18 一致）::

        0x00  3B  "S98"
        0x03  1B  版本**字符**（'0'..'3'，不是二进制 0..3）
        0x04  4B  tickMult（计时器分子，LE）
        0x08  4B  tickDiv （计时器分母）
        0x0C  4B  compression（0 = 未压缩）
        0x10  4B  tagOfs  （曲名等标签偏移，0 = 无）
        0x14  4B  dataOfs （数据流起始）
        0x18  4B  loopOfs （循环点，0 = 不循环）
        0x1C  4B  仅 v3：设备数
        0x20      v2/v3：设备表，每项 0x10 {type, clock, pan, app_spec}

    v0/v1 **没有设备表**：芯片写死 YM2608（S98DEV_OPNA=4）、时钟写死
    7987200 Hz；tickMult/tickDiv 为 0 时兜底 10/1000（= 100 Hz，10 ms/tick）。
    实测 joshw 的 Ys 包 18 个文件**全是 v1**，tag 里直接写着曲名。

    ⚠ 时长要扫完整条数据流才知道（0xFD 是结束标记）。这里**突破了
      ``PROBE_MAX_READ`` 的 8 KB 上限**，理由是：s98 是寄存器日志，
      10 分钟的音乐也只有 ~2 MB，纯 Python 扫一遍 ~5 ms；换来的是
      **精确时长**（界面能显示真实长度、按长度切批不会估错）。
      上限 :data:`S98_SCAN_MAX` 兜底，超了就退回「时长未知」。
    """
    if len(head) < 0x20:
        info.ok = False
        info.reason = "S98 头不足 0x20 字节，文件可能被截断"
        return
    ver = head[0x03] - 0x30
    if ver < 0 or ver > 3:
        info.ok = False
        info.reason = "不认识的 S98 版本字节 0x%02X" % head[0x03]
        return
    tick_mult = int.from_bytes(head[0x04:0x08], "little")
    tick_div = int.from_bytes(head[0x08:0x0C], "little")
    comp = int.from_bytes(head[0x0C:0x10], "little")
    tag_ofs = int.from_bytes(head[0x10:0x14], "little")
    data_ofs = int.from_bytes(head[0x14:0x18], "little")

    if comp:
        info.ok = False
        info.reason = ("这个 .s98 是压缩的（compression=%d），"
                       "本工程只支持未压缩的 S98" % comp)
        return
    if not tick_mult:
        tick_mult = 10
    if not tick_div:
        tick_div = 1000

    # 曲名：tag 区第一个 '\0' 结尾的串（v0/v1 是纯文本，v2+ 是 key=value）
    if tag_ofs and 0 < tag_ofs < info.size:
        try:
            with open(path, "rb") as f:
                f.seek(tag_ofs)
                raw = f.read(200)
            t = raw.split(b"\x00", 1)[0]
            t = t.split(b"\n", 1)[0].split(b"\r", 1)[0]
            t = decode_text(t).strip()
            if t:
                info.title = t
        except OSError:
            pass
    if not info.title:
        info.title = os.path.splitext(os.path.basename(path))[0]

    info.n_tracks = 1
    if not data_ofs or data_ofs >= info.size:
        info.ok = False
        info.reason = "数据流偏移越界（dataOfs=0x%X）" % data_ofs
        return
    if info.size > S98_SCAN_MAX:
        # 太大就不扫了：轨数已知，时长交给解码器自己算
        info.ok = True
        return
    try:
        ticks, _writes, ended = _s98_walk(path, data_ofs)
    except OSError as e:
        info.ok = False
        info.reason = "读取数据流失败：%s" % e
        return
    if ticks <= 0:
        info.ok = False
        info.reason = "数据流里没有任何 tick，文件可能是空的"
        return
    info.exact_secs = ticks * float(tick_mult) / float(tick_div)
    info.exact_src = "s98_ticks"
    info.loop_end_secs = 0.0
    if not ended:
        # 没有 0xFD 不是错误：有些抓轨工具不写结束标记，按文件尾算
        info.reason = "数据流没有结束标记 0xFD，时长按文件尾估算"
    info.ok = True


def probe(path):
    """完整元信息：格式 + 轨数 + 标题 + 时长来源。**永不抛异常**。

    读盘量硬上限 PROBE_MAX_READ (8 KB)；NSFE section 表与 VGZ 头按需追加读，
    但总量有界（H15：界面线程拖入上百文件也不能卡）。
    """
    info = ChipInfo(path=path)
    try:
        info.ext_declared = os.path.splitext(path or "")[1].lower()
    except (TypeError, ValueError):
        info.ext_declared = ""

    try:
        info.size = os.path.getsize(path)
    except OSError:
        info.size = 0

    head = _read_head(path, PROBE_MAX_READ)
    fmt, conflict = identify(path, head)
    info.fmt = fmt
    info.conflict = conflict
    info.system = fmt_system(fmt)

    if not head:
        # 0 字节 / 读不到：无法校验内容，按扩展名处理（FR-1.3 第三级，H9）
        info.ok = bool(fmt)
        info.reason = "" if info.ok else "文件为空或无法读取"
        return info

    try:
        if fmt == "VGZ":
            _probe_vgz(path, info)
        elif fmt == "VGM":
            # .vgm 但内容是 gzip（joshw 实测如此）→ 当 VGZ 解；解不开再按裸 VGM
            if head.startswith(b"\x1f\x8b"):
                _probe_vgz(path, info)
            else:
                _probe_vgm_head(head, info)
        elif fmt == "NSF":
            _probe_nsf(head, info)
            if info.ext_declared == ".nsfe":
                _parse_nsfe_sections(head, info)
        elif fmt == "NSFE":
            _probe_nsfe(head, info)
            _parse_nsfe_sections(head, info)
        elif fmt == "GBS":
            _probe_gbs(head, info)
        elif fmt == "HES":
            _probe_hes(head, info)
        elif fmt == "SPC":
            _probe_spc_id666(head, info)
        elif fmt == "KSS":
            _probe_kss(head, info)
        elif fmt == "GYM":
            _probe_gym(head, info)
        elif fmt == "SAP":
            _probe_sap(head, info)
        elif fmt == "AY":
            _probe_ay(head, info)
        elif fmt == "SGC":
            _probe_sgc(head, info)
        elif fmt == "WSR":
            _probe_wsr(path, info)
        elif fmt == "S98":
            _probe_s98(path, head, info)
        else:
            info.ok = False
            info.reason = "无法识别的格式"
            return info
    except Exception:                                    # noqa: BLE001
        # 解析层任何意外都不该中断整批（设计约束 6）
        info.ok = False
        info.reason = f"文件头解析失败（{type(Exception).__name__}），可尝试直接转换"
        return info

    # 轨数上限保护（R2：HES 头部损坏时 clamp 到 256 并记日志）
    if info.n_tracks > CHIP_MAX_TRACKS:
        info.reason = (f"头部轨数 {info.n_tracks} 超出格式上限，"
                       f"已按 {CHIP_MAX_TRACKS} 处理")
        info.n_tracks = CHIP_MAX_TRACKS

    # 各probe 已可能主动判定不可解码（如 SAP TYPE D = Digimusic），
    # 这里**不要**无条件覆盖 ok，否则那条 reason 就被吞掉了。
    if info.reason and not info.ok:
        return info
    info.ok = True
    return info


# ---------------------------------------------------------------------------
# 时长决策链（设计 §3）
# ---------------------------------------------------------------------------

class Duration:
    """时长决策结果。source 只用于日志，不参与控制流。"""

    __slots__ = ("secs", "source", "stop_on_silence")

    def __init__(self, secs, source, stop_on_silence=False):
        self.secs = float(secs)
        self.source = source
        self.stop_on_silence = bool(stop_on_silence)

    @property
    def lead_sil_limit(self):
        """开头静音上限：已知确切时长时必须传 0，否则 VGM 前 10s 静音会被误砍。"""
        return 0.0 if self.source in EXACT_OR_LOOP_SOURCES else 10.0

    def __iter__(self):
        # 允许 (secs, source) = plan_duration(...) 的老式解包
        yield self.secs
        yield self.source

    def __repr__(self):
        return (f"<Duration {self.secs:.2f}s src={self.source} "
                f"sil={self.stop_on_silence}>")


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def plan_duration(info, *, user_max_secs=0.0, default_secs=0.0,
                  hard_cap_secs=CHIP_MAX_SECS, stop_on_silence_default=False):
    """时长决策链（FR-3.1）。返回 :class:`Duration`。**永不抛异常**。

    优先级（从上往下取第一个可用的）：
      1 用户显式填写的「单曲最长」        -> source="user"     （永远最高，且必开静音检测 R4）
      2 文件自带时长（NSFE length/VGM samples/SPC ID666）-> source=info.exact_src（关静音检测）
      4 GBS loop_end ÷ 59.7275           -> source="gbs_loop"
      5 「默认单遍 N 秒」                 -> source="default"（**v4.4 起关静音检测**）
      6 硬顶 hard_cap (600)             -> source="hardcap"（5 已含 clamp，实际不可达）

    ⚠⚠ **v4.4 修复（用户报障「NSF 转出来的 WAV 声音不对」）**：
    第 5 条原本是 ``stop_on_silence=True``，即「连续 2 秒静音就判定曲子结束」。
    这对**循环型游戏音乐**（NSF/NSFE/GBS/HES/KSS/SAP）是**原理性错误** ——
    这些格式的 BGM 本来就是无限循环的，**没有「自然结尾」这回事**，
    而游戏音乐里 2 秒以上的停顿（音效间隔、乐句休止）极其常见。

    实测后果（``10-Yard Fight``，请求 150s）：**17/17 轨全部被截断**，
    长度 2.14~8.36 秒，最短只有 2.14s —— 听感就是「每首曲子响两秒就断了」。
    同一 bug 影响 6 种格式（NSF/NSFE/GBS/HES/KSS/SAP 全走第 5 条）。

    故改为 ``stop_on_silence=False``：**按用户设定的「单曲最长」老老实实截断**。
    ✅ 空轨仍会被跳过 —— ``_spc_decode_track`` 里「从未发声且超过 lead_sil_limit
       (10s) 就放弃」的分支与 ``stop_on_silence`` 无关，空包不会白跑满时长。
    ⚠ 副作用：真正很短的曲子（如 5 秒音效）也会转满设定时长 ——
       这是「宁可长不可断」的正确取舍（长可剪，断不可补）。
    """
    try:
        cap = float(hard_cap_secs) if hard_cap_secs else float(CHIP_MAX_SECS)
    except (TypeError, ValueError):
        cap = float(CHIP_MAX_SECS)
    cap = max(1.0, cap)

    try:
        user_max = float(user_max_secs or 0)
    except (TypeError, ValueError):
        user_max = 0.0

    # 1 用户上限 —— 永远最高。
    # ⚠ v4.4 与第 5 条同步修复：原先这里也是 ``stop_on_silence=True``，
    #   而它与第 5 条是**同一个 bug** —— 用户把「单曲最长」设成 120s，
    #   NSF 照样会在第一个 2 秒停顿处被截断（实测 3 秒），设置形同虚设。
    #   「单曲最长」的语义是**上限**，不是「精确时长」；对循环型 BGM，
    #   唯一的可靠截断依据就是用户给的这个上限本身。
    if user_max > 0:
        return Duration(_clamp(user_max, 1.0, cap), "user", stop_on_silence=False)

    # 2/3 文件自带时长 —— 精确，不做静音检测
    #    （否则 AC-6/AC-8 的 ±0.05/±0.5s 会挂）
    try:
        exact = float(getattr(info, "exact_secs", 0.0) or 0.0)
    except (TypeError, ValueError):
        exact = 0.0
    if exact > 0.2:
        return Duration(_clamp(exact, 1.0, cap),
                        getattr(info, "exact_src", "") or "exact",
                        stop_on_silence=False)

    # 4 GBS 循环点当单遍长度（GBS 永不 ended，只能靠帧数截）
    try:
        loop = float(getattr(info, "loop_end_secs", 0.0) or 0.0)
    except (TypeError, ValueError):
        loop = 0.0
    if 0.2 < loop <= _GBS_LOOP_MAX_SECS:
        return Duration(loop, "gbs_loop", stop_on_silence=False)

    # 5 默认单遍 —— 时长上限与静音检测同时生效（FR-3.2 + R4）
    try:
        d = float(default_secs or 0)
    except (TypeError, ValueError):
        d = 0.0
    d = d if d > 0 else CHIP_DEFAULT_SECS_DEFAULT
    return Duration(_clamp(d, 1.0, cap), "default",
                    stop_on_silence=bool(stop_on_silence_default))


# ---------------------------------------------------------------------------
# 不支持格式的原因文案（FR-4.2）
# ---------------------------------------------------------------------------

def unsupported_reason(path, info=None):
    """不支持/不可解码的原因文案（FR-4.2 三类）。**永不抛异常**。

    三类：
      1 已知但暂不支持（USF/SSF/…）  -> 「暂不支持 XXX（机种）—— 需要模拟器…」
      2 扩展名不认识                -> 「不认识的扩展名 .xxx —— 请反馈」
      3 魔数与扩展名不符且魔数也不支持 -> 「内容不是本工具支持的芯片音乐格式（识别为 XXX）」
    """
    name = os.path.basename(path or "")
    ext = (os.path.splitext(path or "")[1] or "").lower()
    if info is None:
        try:
            info = probe(path)
        except Exception:                                # noqa: BLE001
            info = None

    fmt = getattr(info, "fmt", None) if info is not None else None

    # ⓪ PSF 家族扩展名：已**原生支持**（psf_native → psf2wav.exe），
    #    不属于「不支持」列表。probe 不认 PSF 魔数（MAGIC_TABLE 里没有），
    #    走到这里说明是**解码失败**而不是「不认识的格式」—— 措辞必须让
    #    用户知道这是 PSF、已经能转，而不是被误导成「程序不认这个格式」。
    if ext in PSF_EXTS:
        if fmt and fmt not in ("PSF", "PSF2"):
            return (f"{name}: 内容不是本工具支持的芯片音乐格式"
                    f"（扩展名是 PSF 但识别为 {fmt}）。")
        return (f"{name}: 是 PSF（{SYSTEM_NAMES.get('PSF', '')} / "
                f"{SYSTEM_NAMES.get('PSF2', '')}）芯片音源，本工具已原生支持，"
                f"但这份文件解码失败 —— 请检查文件是否完整、"
                f"解码组件（psf_native / psf2wav.exe / BIOS）是否齐全。")

    # ⓪b QSF（Capcom QSound，v5.9.10）：同样是**已原生支持**的格式。
    #    ⚠ 必须放在 PSF 之后、P2_EXTS 之前。QSF 和 PSF 共用一个容器魔数
    #      （``PSF`` + 版本字节），probe 的 MAGIC_TABLE 只认前 4 字节，
    #      所以它同样识别不出 QSF —— 走到这里说明是**解码失败**。
    #    ⚠ 文案里**不能**提 BIOS：QSound 是街机板上的独立声音子系统，
    #      没有主机 BIOS 这回事。照抄 PSF 那句会让用户去翻 BIOS 目录白忙。
    if ext in QSF_EXTS:
        if fmt and fmt not in ("PSF", "QSF"):
            return (f"{name}: 内容不是本工具支持的芯片音乐格式"
                    f"（扩展名是 QSF 但识别为 {fmt}）。")
        return (f"{name}: 是 QSF（{SYSTEM_NAMES.get('QSF', '')}）芯片音源，"
                f"本工具已原生支持，但这份文件解码失败 —— 请检查文件是否完整、"
                f"同目录的 .qsflib 在不在、解码组件（qsf_native / "
                f"qsf2wav.exe）是否齐全。")

    # ① 已知但暂不支持（先判扩展名，别让 probe 的泛化 reason 抢在前面）
    if ext in P2_EXTS:
        pid = P2_EXTS[ext]
        return (f"{name}: 暂不支持 {pid}（{SYSTEM_NAMES.get(pid, '')}）—— "
                f"需要该机种的模拟器，本工具尚未内置。"
                f"请先用 foobar2000 等工具转成 WAV 再拖入。")

    # ① P1 扩展名：目前只有 SAP TYPE D 会走到这里（probe 判 ok=False 且带原因）。
    #    其余情况 P1 都已支持，不该落到 unsupported_reason —— 直接放行，
    #    免得对**完全正常**的 KSS 文件说"文件头异常，可能已损坏"。
    if ext in P1_EXTS and info is not None and not getattr(info, "ok", True):
        pid = P1_EXTS[ext]
        detail = getattr(info, "reason", "") or "文件头异常，可能已损坏"
        return f"{name}: {detail}（{pid} / {SYSTEM_NAMES.get(pid, '')}）"

    # ③ 魔数识别为 P2 但扩展名是 P0/P1 → 内容不是芯片音乐
    if fmt in P2_EXTS.values():
        return (f"{name}: 内容不是本工具支持的芯片音乐格式（识别为 {fmt} / "
                f"{SYSTEM_NAMES.get(fmt, '')}）。")

    # probe 给出了**具体的**失败原因（不是泛化文案）→ 用它
    if info is not None and getattr(info, "reason", "") \
            and not getattr(info, "ok", True):
        r = info.reason
        if r and r != "无法识别的格式":
            return f"{name}: {r}"

    # ④ 魔数与扩展名互相印证、内容确实符合该格式 -> 已支持。
    #    只在"魔数本身认出来了"时才这么说；纯靠扩展名蒙对的（内容其实是别的）
    #    继续走 ② 的文案 —— 那才是"内容不是芯片音乐"的场景。
    #    （实测踩过：x.nsf 里塞 "this is not a chip file" 时，若这里只看
    #     fmt/ok 就会误报"已支持"，盖掉本该给出的 ③ 类提示。）
    if info is not None and getattr(info, "ok", False) and fmt \
            and not getattr(info, "conflict", False) \
            and _sniff_magic(read_magic(path, 64)) == fmt:
        return (f"{name}: {fmt}（{SYSTEM_NAMES.get(fmt, '')}）是已支持的格式，"
                f"不属于「不支持」列表。")

    # ② 扩展名不认识
    if not ext:
        return f"{name}: 没有扩展名，无法判断格式 —— 请确认这是芯片音乐文件。"
    return f"{name}: 不认识的扩展名 {ext} —— 如果这确实是芯片音乐格式，请反馈给我们补充支持。"


# ---------------------------------------------------------------------------
# 文件收集（FR-1.2 / 设计 §1.2）
# ---------------------------------------------------------------------------

def _natural_key(name):
    """自然排序：让 "2 xxx" 排在 "10 xxx" 前面。"""
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def collect_supported(paths, exts=None):
    """文件/文件夹混合列表 -> 去重后的文件列表（按名称自然排序）。

    exts: 扩展名元组，默认 :data:`ALL_EXTS`。目录**只扫一层**（与现有
    collect_spc 一致，设计 A8）。
    """
    exts = tuple(exts) if exts else ALL_EXTS
    exts = tuple(e.lower() for e in exts)
    out, seen = [], set()

    def add(p):
        ap = os.path.abspath(p)
        key = ap.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(ap)

    for p in paths or ():
        try:
            if os.path.isdir(p):
                for f in sorted(os.listdir(p), key=_natural_key):
                    if f.lower().endswith(exts):
                        add(os.path.join(p, f))
            elif p.lower().endswith(exts) and os.path.exists(p):
                add(p)
        except OSError:
            continue
    return out


def collect_files(paths):
    """收集**芯片音源**（不含 cue/bin/mp3），供 chip 解码管线使用。"""
    return collect_supported(paths, CHIP_EXTS)


if __name__ == "__main__":  # 手动自检: python chip_formats.py <文件...>
    import sys
    for p in sys.argv[1:]:
        i = probe(p)
        d = plan_duration(i, default_secs=CHIP_DEFAULT_SECS_DEFAULT)
        print(f"{os.path.basename(p)}\n"
              f"   fmt={i.fmt} n={i.n_tracks} conflict={i.conflict} ok={i.ok}\n"
              f"   title={i.title!r} author={i.author!r}\n"
              f"   exact={i.exact_secs:.3f}({i.exact_src}) loop={i.loop_end_secs:.3f}"
              f" -> plan {d.secs:.2f}s src={d.source} sil={d.stop_on_silence}")
