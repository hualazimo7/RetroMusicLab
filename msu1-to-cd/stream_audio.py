#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stream_audio.py — 流式音频（一条文件就是一首成品）→ WAV。

## 为什么单独一个模块

本项目里「一条文件 = 一首曲子」的音源已经有三类，各自走不同的解码器：

* **芯片音源**（.spc/.nsf/.vgm…）→ :mod:`chip_decode`，靠 libgme 实时合成；
* **PSF 系**（.psf/.minipsf）→ :mod:`psf_native`，靠 psf2wav.exe 模拟 CPU+SPU；
* **CD 音轨**（.flac/.ogg…）→ :mod:`cd_audio`，靠 PyAV 直接解压。

现在多出来的一类既不是合成也不是 CD 音轨，而是**游戏自己存好的流式音频**
（预录 BGM）。它们不需要模拟器，也不能当 CD 音轨对待（采样率、声道数、
分块规则都不同），所以单开一个模块，接口与 :mod:`cd_audio` 完全同形。

⚠ 第三类 ``.mod`` 其实**不是流式音频**，是 ProTracker 乐谱 + 采样。放这里
是因为它的使用场景与流式音频完全一样（一个文件 = 一首成品、逐文件入队、
不走芯片模拟），而本模块已经把「命名 / 编号 / 落盘 / 淡出 / 记账」全做完了。
真正的回放逻辑在 :mod:`mod_audio`，这里只是转手。

## 覆盖的格式

### 1. CRI ADX（``.adx``）—— Dreamcast / PS2 / 街机

魔数 ``0x8000``，CRI 自家的 4bit ADPCM。**PyAV(FFmpeg) 原生支持**
（``adpcm_adx`` 解码器 + ``adx`` 解复用器），所以这一种**一行都不用自己写**，
直接转手给 :func:`cd_audio.convert_one`。

### 1b. PS3 / PSP / Wii / 3DS 的预录流（v5.9.11）

这一批是「先查有没有现成方案」查出来的 —— **五种全部是 FFmpeg 原生支持的**，
一行解码代码都不用写，和 ``.adx`` 一样直接转手 :mod:`cd_audio`：

=========  ====================  =========================================
扩展名      机种                  实测（PyAV 报的）
=========  ====================  =========================================
``.XVAG``   PS3                   魔数 ``XVAG``，``fmat`` 块 + MPEG 帧 → mp3float
``.AT3``    PS3 / PSP             RIFF/WAVE，``fmt`` 0xFFFE + ATRAC3plus
``.hca``    PSP                   ``HCA\\0`` + ``fmt``/``dec``/``ath``/``ciph``
``.brstm``  Wii / GameCube        ``RSTM`` + adpcm_thp
``.laac``   Nintendo 3DS          裸 AAC（ADTS 同步字 ``fff0``），配 ``.txth``
=========  ====================  =========================================

实测 310 个真实文件（30 XVAG + 1 AT3 + 29 HCA + 31 BRSTM + 219 LAAC）
**全部成功、零失败**，产物 RMS / 时长都正常，见 ``vendor/_probe_new6_pyav.py``。

⚠ 三个易错点：

1. **``.psf`` 不在这里**。GameCube 的《Conflict》系列也用 ``.psf``，但那是
   Pivotal 自家的流式音频（PSX-ADPCM / NGC-DSP），和 PS1/PS2 的 PSF 只是
   撞了扩展名。它在 :mod:`pivotal_psf`，入口是 ``.txtp`` 索引。
2. **``.hca`` 要看 ``ciph`` 块**。实测这一批 29 个全是 ``ciph 00 00``（未加密），
   所以能直接解；加密的 HCA 需要 64bit 密钥，本工具不处理。
3. **``.laac`` 是裸 AAC 没有容器**，靠 ADTS 同步字识别。FFmpeg 的 ``aac``
   解复用器能嗅出来，实测 219 个全过。

### 2. Vicarious Visions HWAS（``.hwas``）—— NDS

NDS 的《007 量子危机》《蜘蛛侠 3》《托尼霍克》等用的容器。结构如下
（全部小端；字段定义照 vgmstream r2117 的 ``src/meta/hwas.c`` 与
``src/layout/blocked_hwas.c`` 逐条核对）：

===========  ==========================================================
偏移          含义
===========  ==========================================================
0x00         ``sawh`` 魔数（是 ``hwas`` 的字节序反转写法）
0x04         u32 分块大小，实测 0x2000 / 0x4000 / 0x8000
0x08         s32 采样率，实测 19854~24356（NDS 定时器分频出来的怪值）
0x0C         s32 声道数，**只见过 1**（>1 的布局 vgmstream 也直接放弃）
0x10         u32 循环起点（单位是**字节**，不是样本）
0x14         u32 采样数据长度（单位是**字节**）
0x18         u32 循环终点（单位是字节）
0x200        音频数据起点
===========  ==========================================================

编码：**裸 IMA ADPCM 单声道、低半字节在前、每块开头把预测值与步长索引
清零**。vgmstream 对「每块清零」的注释是 *otherwise there are wild volume
swings*（不清零会有剧烈的音量摆动）。

#### 上一轮为什么判成「负载未解」—— 复盘

不是格式特殊，是**两个 bug 叠在一起**：

1. ``_IMA_INDEX`` 后半段写成 ``-2,-4,-6,-8``（正确是 ``2,4,6,8``）；
2. 半字节顺序取了「高先」（HWAS 是「低先」）。

第 1 条是致命的：负向半字节的步长会一路收缩到 0，输出就卡在一个值附近，
**表现成 DC 漂移**（ac1=1.000、质心 4 Hz、>1kHz 能量 0.0%）。

当时还据此写了「ADPCM 在数学上被排除」的推理：半字节分布对称 + 索引表对
正负相消 → 任何对称流做 ADPCM 积分都退化成随机游走。**这个推理本身没错，
但前提是错的** —— 真实的半字节流并不对称，是错的索引表把它变成了对称的。

教训：``ac1 ≈ 1.0`` 只能说明「解出来是慢变信号」，不能说明「编码不是
ADPCM」。**先怀疑自己的实现，再怀疑格式。**

修正后与 vgmstream r2117 逐样本对比：**29 个 .hwas 全部 100% 一致、最大差 0**。

### 3. Nintendo SWAV（``.swav``）—— NDS

GBATEK 的「Sound Bank / SWAV」一节，头部 0x24 字节：

===========  ==========================================================
偏移          含义
===========  ==========================================================
0x00         ``SWAV`` 魔数
0x04         ``FF FE``（小端 BOM）
0x08         u32 文件长度（用来判单/双声道）
0x10         ``DATA``
0x14         u32 块长度（样本数就是从这里推的）
0x18         u8  编码：0=PCM8 / 1=PCM16 / 2=IMA-ADPCM
0x19         u8  循环标志
0x1A         u16 采样率（**直接是 Hz**，实测 24050）
0x1E         u16 循环起点（单位 4 字节）
0x20         u32 非循环段长度（单位 4 字节）
0x24         ..  数据
===========  ==========================================================

⚠ IMA 的头部**只有一份**，就在 0x24 那 4 个字节（``s16 初值`` + ``s16 步长
索引``），后面全是连续的半字节流，**没有「每 36 字节一个帧头」**。

上一轮就是照「4 字节头 + 32 字节数据」的帧结构解的 —— 每 36 字节跳过 4
字节，等于把音频数据当成头扔掉，解出来自然是 DC 漂移。加上同一张错的
``_IMA_INDEX``，两个错误互相掩护，看起来很像「编码特殊」。

修正后：**4 个 .swav 全部与 vgmstream 100% 一致、最大差 0**。

### 4. Konami DVI.（``.dvi``）—— Saturn / 街机

《恶魔城月下夜想曲》Saturn 版的 BGM 就是这种。头部 0x800 字节，**大端**：

===========  ==========================================================
偏移          含义
===========  ==========================================================
0x00         ``DVI.`` 魔数
0x04         s32be 数据起点（实测 0x800）
0x08         s32be 总样本数（**每声道**）
0x0C         s32be 循环起点（``-1`` 表示不循环）
0x10 / 0x20  可能是 ADPCM 的循环历史（+0x00 值 / +0x17 步长），未使用
===========  ==========================================================

* 采样率固定 **44100**、声道固定 **2**（没有单声道的文件）。
* 编码是 **DVI IMA**（与标准 IMA 同一套步长表，唯一区别是**高半字节先**）。
* 交错单位 **4 字节**，而且**右声道在前** —— vgmstream 注释说是拿 Saturn
  实机与 PS/OST 版对比后确定的，所以解完要把两个声道换回来。

上一轮失败的原因：① 把 0x200（512）当成头长，真正起点是 0x800（2048）；
② 试的是标准 IMA（低半字节先）+ 没处理 4 字节交错。

修正后：**45 个 .dvi 全部与 vgmstream 100% 一致、最大差 0**（最长的一首
《Wood Carving Partita》2,656,256 帧 / 60.2 秒）。

### 5. ProTracker MOD（``.mod``）—— GBA 等

GBA 的《007 夜火》整个 BGM 就是 10 个标准 ``.MOD``（魔数 ``M.K.``、31 个
采样、4 声道）。它是**乐谱 + 8bit 采样**，既不用模拟 CPU 也不用解 ADPCM，
只要按 tick 走序列、按效果改音高音量、按 Amiga 周期表算速率、把 4 个声道
混成一路就行 —— 纯算术。回放器在 :mod:`mod_audio`，本模块只负责转手。

音高换算验过：造一个 8287 Hz 采样的 100 Hz 正弦，分别用周期 1712/856/428/
214/113 起音，实测输出 24.2/51.1/99.6/199.2/379.5 Hz，与
``7093789.2 / (2 × period)`` 的理论值全部落在 1 个 FFT bin 之内。

## 和 ``.mini2sf`` 的区别（重要）

NDS 的 JoshW 包是「一堆 1 KB 的 ``.mini2sf`` + 一个 ``.2sflib``」——
前者只是**状态快照**（寄存器值 + 要播放的序列号），声音在后者里，
必须靠 NDS 的声音模拟器（ARM7 + SPU）跑起来才有声。
``.hwas`` 不一样：它是**已经压好的音频流**，解出来就是成品 PCM。
"""

import os
import struct
import wave
from typing import Callable, Optional, Set

import numpy as np

#: 输出规格 —— 与全工程一致（MSU-1 只认这个）
OUT_RATE = 44100
OUT_CH = 2
OUT_WIDTH = 2

#: SWR（PyAV 的 AudioResampler）能吃下的**最少**输入帧数。
#: 实测 16 帧 → 输出 0 帧，64 帧 → 正常。取 256 留足余量（约 6ms）。
#: 见 :func:`_resample_to_out` —— 短于此的输入要先补静音再裁回。
_SWR_MIN_FRAMES = 256

#: 源声道数 → PyAV 的 layout 名。**降混到立体声时声道布局必须说对**，
#: 否则 SWR 会按错的顺序解释交错数据（不是"少混了两个声道"那么简单，
#: 是整段数据错位）。目前真正用到的是 1 / 2 / 4（Wwise 的 FL FR BL BR）。
_SRC_LAYOUT = {
    2: "stereo",
    3: "2.1",
    4: "quad",
    5: "5.0",
    6: "5.1",
    8: "7.1",
}

#: 格式 → 解码器。三种新格式（.hwas/.swav/.dvi）都是**纯 Python + numpy**，
#: 零外部依赖 —— 参数照 vgmstream r2117 的源码逐条核对，输出与它**逐样本
#: 完全一致**（78 个真实文件全部 100%，最大差 0）。对照脚本见
#: ``vendor/_check_vgm_ref.py``。
EXT_KIND = {
    ".adx": "adx",        # CRI ADX —— 转手 PyAV
    ".mod": "mod",        # ProTracker —— mod_audio
    ".hwas": "hwas",      # Vicarious Visions HWAS（NDS 流式）
    ".swav": "swav",      # Nintendo SWAV（NDS 单音色）
    ".dvi": "dvi",        # Konami DVI.（Saturn / 街机）
    ".strm": "strm",      # NDS STRM（流式 BGM，IMA-ADPCM）
    ".npsf": "npsf",      # Namco NPSF（Ace Combat 5/Zero 的 BGM，PSX ADPCM）
    # 3DO（v5.6）：AIFF / AIFC 容器，编码是 SDX2 或 8/16bit PCM。
    # 解析与解码在 aif_audio，这里只是转手 —— 和 .mod 的处理方式一样。
    # ⚠ .aif 是个很泛的扩展名，别的包里可能塞 WAV 冒名；aif_audio 会先验
    #   FORM 头，验不过就当「不是我们的格式」。
    ".aif": "aif",
    ".aiff": "aif",
    ".aifc": "aif",
    # ── v5.9.11：PS3 / PSP / Wii / 3DS 的预录流 ────────────────────────
    # 全部是**现成方案**：FFmpeg 自带解复用器 + 解码器，一行解码代码都不用写。
    # 实测 310 个真实文件（30 XVAG + 1 AT3 + 29 HCA + 31 BRSTM + 219 LAAC）
    # **100% 成功**，见 ``vendor/_probe_new6_pyav.py``。
    #
    # ⚠ 它们和 ``.adx`` 是同一个处置方式（:data:`PYAV_KINDS`）——
    #   直接转手 ``cd_audio``，那边「解码→重采样→落盘→截断淡出」已经做完了。
    ".xvag": "xvag",      # PS3 的 XVAG（MPEG 帧，实测 mp3float / 48000）
    ".at3": "at3",        # PS3/PSP 的 ATRAC3 / ATRAC3plus（RIFF 容器）
    ".hca": "hca",        # CRI HCA（PSP / 手游，实测 44100）
    ".brstm": "brstm",    # Wii / GC 的 BRSTM（RSTM + adpcm_thp，实测 44100）
    ".laac": "laac",      # 3DS 的裸 AAC（ADTS 同步字，实测 48000）
    # PS Vita（v5.9.11 补）：原本以为这个机种转不了，是**判断错了** ——
    # 包里确实有 60 个 .at9（ATRAC9）和 2 个 .vag（PSX-ADPCM），
    # FFmpeg 两个都认（实测 atrac9 / 48000、adpcm_psx / 44100）。
    # ⚠ 那些 .txtp 索引指向的就是这些文件，所以两个后缀必须同时登记，
    #   否则「索引能读到、目标说转不了」。
    ".at9": "at9",        # PS Vita 的 ATRAC9（实测 48000）
    ".vag": "vag",        # PS Vita / PS1 的 VAGp（adpcm_psx，实测 44100/22050）
    # ── v5.9.12：Xbox 360 / PS4 / Xbox 的三种预录流 ────────────────────
    # 同样是**现成方案**，全部转手 PyAV。实测数据见
    # ``vendor/_probe_v5912_platforms.py``。
    #
    # ⚠ ``.bik`` 是**视频**容器（Bink Video），但音频轨是独立的
    #   ``binkaudio_dct`` —— joshw 的 PS4 包里 .bik 就摆在 ``videos/`` 下，
    #   一条视频一条音轨，转换时只要音轨。实测 5 个里 3 个有声音、
    #   2 个本来就是静音轨（peak 恰好 0），不是解码失败。
    # ⚠ ``.araw`` 名字像「裸 PCM」，但 joshw 给的这几个其实是**带头的
    #   RIFF/WAVE**（pcm_s16le / 44100 / 2ch）。别按无头裸流去猜参数 ——
    #   让 FFmpeg 按内容判，两种都能吃。
    # ⚠ ``.xma`` 的**头里没有总时长**（``container.duration`` 读出来是 0），
    #   所以 ``probe`` 拿不到 secs，只能靠解码实测。这是格式本身的性质
    #   （XMA 是帧序列，没有 RIFF 级的 sample count），不是读错了字段。
    ".xma": "xma",        # Xbox 360 的 XMA（实测 xma1 / 22047 / 14.77s）
    ".bik": "bik",        # Bink Video 的音轨（实测 binkaudio_dct / 48000）
    ".araw": "araw",      # Xbox 的「裸」音频，实为 RIFF/WAVE（实测 44100/2ch）
    # ── v5.9.12：FFmpeg **不支持**的两族，走 vgmstream ──────────────────
    # ⚠ 这两个**不在** PYAV_KINDS 里 —— 它们由 vgm_audio 调
    #   vgmstream-cli.exe 解，见 :data:`VGM_KINDS`。
    ".wem": "wem",        # Audiokinetic Wwise（PS4，实测 Custom Vorbis 48k）
    ".fsb": "fsb",        # FMOD FSB5（Wii U，实测 Nintendo DSP ADPCM 44.1k）
    # ── v5.9.12：Wii U 的启动音，原生解 ────────────────────────────────
    ".btsnd": "btsnd",    # 8 字节头 + PCM16 **大端** 48000/2ch
}

#: 走 ``vgm_audio``（vgmstream-cli.exe）的 kind。
#:
#: 为什么不并进 ``PYAV_KINDS``：那两个的 ``convert_one`` / ``probe`` 都直接
#: 转手 ``cd_audio``（PyAV），而这两个 FFmpeg 根本解不了 —— 混进去只会
#: 得到「能 probe 出参数、一解码就报错」，和 ``.vag`` 同一个坑。
VGM_KINDS = ("wem", "fsb")

#: 直接转手 PyAV 的那些 kind —— FFmpeg 原生认得，**零解码代码**。
#: ``cd_audio`` 那条路已经跑通了「解码 → 44100/16bit/立体声 → 截断淡出 → 落盘」，
#: 再写一份只是把同一个坑踩第二遍。
#:
#: ⚠ ``.vag`` **不在这里**：FFmpeg 的 vag 解复用器能 probe 出参数，但解码时
#:   ``avcodec_send_packet()`` 报 InvalidDataError（实测 2 个样本全这样）。
#:   它走本模块的原生 PSX-ADPCM 解码器，见 :func:`decode_vag`。
PYAV_KINDS = ("adx", "xvag", "at3", "hca", "brstm", "laac", "at9",
              "xma", "bik", "araw")

#: 供 ``convert_job`` / ``web_ui`` 判断「这算不算我们的音源」
ALL_EXTS = tuple(EXT_KIND)

#: HWAS 魔数与数据起点
HWAS_MAGIC = b"sawh"
HWAS_DATA_OFF = 0x200

#: STRM（NDS 流式音频）魔数与布局。GBATEK "DS Sound Files: STRM"。
#: 0x00 "STRM" | 0x04 0xFEFF | 0x06 ver | 0x08 file_size | 0x0C hdr(0x10)
#: 0x0E blocks | 0x10 "HEAD" | 0x14 blk_size(0x50) | 0x18 codec | 0x1A loop
#: 0x1C rate(u16) | 0x20 time | 0x24 loop_start | 0x28 loop_len
#: 0x30 block(0x200) | 0x34 samples/block | 0x60 "DATA" | 0x64 size | data
STRM_MAGIC = b"STRM"
STRM_BLOCK_SIZE = 0x200
#: IMA-ADPCM 的 codec id（0=PCM8 1=PCM16 2=IMA）
STRM_CODEC_IMA = 2

#: NPSF（Namco Production Sound File）—— Ace Combat 5 / Zero 的 BGM 容器。
#: 头字段**照抄 vgmstream ``src/meta/npsf.c``**：
#:   0x00 "NPSF" | 0x04 version(0x1000) | 0x08 channel_size(每声道字节数)
#:   0x0C channels | 0x10 data_offset | 0x14 loop_start(samples, -1=不循环)
#:   0x18 sample_rate | 0x1C volume | 0x20 flags | 0x24 flag | 0x30 0x40
#:   0x34 name（实测 "m01a.aif" —— 只是原始文件名，**不是** AIFF 数据）
#: ⚠ 编码是 **coding_PSX**（PS1 的 XA ADPCM，4bit / 16 字节块 = 28 samples），
#:   **不是** PS2 的 SPU2/VAG。interleave 固定 0x800。
NPSF_MAGIC = b"NPSF"
NPSF_INTERLEAVE = 0x800
#: PSX / VAG ADPCM 的 5 组预测系数（与 msu1_to_cd._XA_K0/_XA_K1 同一张表）
_PSX_K0 = (0, 60, 115, 98, 122)
_PSX_K1 = (0, 0, -52, -55, -60)

#: 标准 IMA ADPCM 步长表与索引调整表（IMA/DVI ADPCM 规范，不是魔改）
_IMA_STEP = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
    598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707,
    1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871,
    5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635,
    13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)
#: ⚠⚠ 必须 **16 项**，而且后半段是 ``2, 4, 6, 8``，**不是** ``-2, -4, -6, -8``。
#:
#: 半字节的 bit3 只是**符号位**，步长索引要按「幅度」调整：code 8~15 的幅度
#: 与 code 0~7 完全一样（都是 0~7），所以调整量也必须一样。写成负数会让
#: 负向半字节的步长一路收缩到 0 —— 输出就卡在一个值附近，表现成 **DC 漂移**
#: （实测 ac1=1.000、频谱质心 4 Hz、>1kHz 能量 0.0%）。
#:
#: **这就是 HWAS / SWAV 上一轮被判成「负载编码没破」的真正原因** —— 编码
#: 一点都不特殊，是这张表写错了。修正后与 vgmstream r2117 的输出**逐样本
#: 完全一致**（29 个 .hwas + 4 个 .swav 全部 100%，最大差 0）。
#:
#: 还踩过一个：曾经写成 8 项 + ``nib & 7``，那会让 8~15 也去查 0~7 的行，
#: 等于把符号位丢掉再查表，同样解不对。
_IMA_INDEX = (-1, -1, -1, -1, 2, 4, 6, 8,
              -1, -1, -1, -1, 2, 4, 6, 8)


class _Cancelled(Exception):
    """用户点了停止。**不计入失败**（与 cd_audio 同一个口径）。"""


# ══════════════════════════════════════════════════════════════════════
#  格式判定
# ══════════════════════════════════════════════════════════════════════
def kind_of(path) -> str:
    """``.adx`` → ``"adx"``，``.hwas`` → ``"hwas"``，``.mod`` → ``"mod"``。"""
    return EXT_KIND.get(os.path.splitext(str(path or ""))[1].lower(), "")


def is_stream(path) -> bool:
    """是不是本模块负责的格式。"""
    return bool(kind_of(path))


# ══════════════════════════════════════════════════════════════════════
#  探测
# ══════════════════════════════════════════════════════════════════════
def probe(path) -> dict:
    """时长 / 采样率 / 声道。**永不抛异常**（与 chip_formats.probe 同契约）。"""
    out = {"ok": False, "secs": 0.0, "rate": 0, "channels": 0,
           "codec": "", "title": "", "error": ""}
    p = str(path or "")
    kind = kind_of(p)
    try:
        from cd_audio import _title_of
        out["title"] = _title_of(p)
    except Exception:                                # noqa: BLE001
        out["title"] = os.path.splitext(os.path.basename(p))[0]
    if not kind:
        out["error"] = "不是流式音频格式"
        return out
    if not os.path.isfile(p):
        out["error"] = "文件不存在"
        return out

    if kind in PYAV_KINDS:
        import cd_audio
        info = cd_audio.probe(p)
        out.update({"ok": info.get("ok", False),
                    "secs": info.get("secs", 0.0),
                    "rate": info.get("rate", 0),
                    "channels": info.get("channels", 0),
                    "codec": info.get("codec", "") or kind,
                    "error": info.get("error", "")})
        return out

    if kind == "mod":
        # ⚠ ``.mod`` 是个太通用的扩展名，别的音源包里也会混进同名的非 MOD
        #   文件；``probe`` 的契约是「永不抛异常」，所以这里必须兜住。
        try:
            import mod_audio
            info = mod_audio.probe(p)
        except Exception as exc:                     # noqa: BLE001
            out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            return out
        out.update({"ok": True, "rate": info["rate"], "channels": 2,
                    "codec": "ProTracker MOD", "secs": info["secs"]})
        return out

    if kind == "npsf":
        try:
            h = read_npsf_header(p)
            out.update({"rate": h["rate"], "channels": h["channels"],
                        "codec": "PSX ADPCM (NPSF)", "secs": h["secs"]})
            out["ok"] = True
        except Exception as exc:                     # noqa: BLE001
            out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return out

    if kind == "vag":
        # PS Vita / PS1 的 VAGp。⚠ 不要改成 cd_audio.probe：它能探出参数，
        #   但解码会失败（见 decode_vag 的注释），探到了也没用。
        try:
            h = read_vag_header(p)
            out.update({"rate": h["rate"], "channels": h["channels"],
                        "codec": "PSX ADPCM (VAG)", "secs": h["secs"]})
            out["ok"] = True
        except Exception as exc:                     # noqa: BLE001
            out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return out

    if kind == "aif":
        # 3DO 的 AIFF/AIFC。⚠ 扩展名太泛（.aif 到处都有），所以这里必须兜异常：
        #   不是 FORM 头就当「这不是 3DO 的音源」，别让整批转换因为一个冒名
        #   文件炸掉。
        try:
            import aif_audio
            info = aif_audio.probe(p)
        except Exception as exc:                     # noqa: BLE001
            out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            return out
        out.update({"ok": info.get("ok", False), "secs": info.get("secs", 0.0),
                    "rate": info.get("rate", 0), "channels": info.get("channels", 0),
                    "codec": info.get("codec", ""), "error": info.get("error", "")})
        return out

    if kind == "strm":
        try:
            h = read_strm_header(p)
            out.update({"rate": h["rate"], "channels": 1,
                        "codec": "IMA ADPCM (STRM)", "secs": h["secs"]})
            out["ok"] = True
        except Exception as exc:                     # noqa: BLE001
            out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return out

    if kind in VGM_KINDS:
        # Wwise / FSB5 —— 问 vgmstream 要元信息（``-I`` 输出 JSON，不解码）。
        # ⚠ 这是**唯一**能拿到时长的途径：FFmpeg 连流都建不起来，
        #   ``cd_audio.probe`` 只会回一句 codec=None。
        try:
            import vgm_audio
            info = vgm_audio.probe(p)
        except Exception as exc:                     # noqa: BLE001
            out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            return out
        out.update({"ok": info.get("ok", False), "secs": info.get("secs", 0.0),
                    "rate": info.get("rate", 0),
                    "channels": info.get("channels", 0),
                    "codec": info.get("codec", "") or kind,
                    "error": info.get("error", "")})
        return out

    if kind == "btsnd":
        try:
            h = read_btsnd_header(p)
            out.update({"rate": h["rate"], "channels": h["channels"],
                        "codec": "PCM 16bit BE (BTSND)", "secs": h["secs"]})
            out["ok"] = True
        except Exception as exc:                     # noqa: BLE001
            out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return out

    if kind in ("hwas", "swav", "dvi"):
        try:
            if kind == "hwas":
                h = read_hwas_header(p)
                out.update({"rate": h["rate"], "channels": 1,
                            "codec": "IMA ADPCM (HWAS)", "secs": h["secs"]})
            elif kind == "swav":
                h = read_swav_header(p)
                names = {_SWAV_PCM8: "PCM8", _SWAV_PCM16: "PCM16",
                         _SWAV_IMA: "IMA ADPCM"}
                out.update({"rate": h["rate"], "channels": 1,
                            "codec": "%s (SWAV)"
                                     % names.get(h["wavetype"], "?"),
                            "secs": h["secs"]})
            else:
                h = read_dvi_header(p)
                out.update({"rate": h["rate"], "channels": h["channels"],
                            "codec": "DVI IMA (DVI.)", "secs": h["secs"]})
            out["ok"] = True
        except Exception as exc:                     # noqa: BLE001
            out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return out

    out["error"] = "格式 %r 还没有探测实现" % kind
    return out


def read_strm_header(path) -> dict:
    """读 NDS STRM 头，返回 ``{rate, channels, block, nbytes, data_off,
    secs, codec}``。

    实测 ``STRM_MAINMENU.strm``：codec=2(IMA)、rate=**32728**（不是 32768）、
    block=0x200、samples/block=1016（= 508 字节 × 2）。

    ⚠ 只实现 **codec=2（IMA-ADPCM）**。PCM8/PCM16 的 STRM 在 joshw 包里
    没见过，遇到会明确报错而不是产出噪音。
    """
    with open(str(path), "rb") as f:
        head = f.read(0x60 + 8)
    if len(head) < 0x60 + 8 or head[:4] != STRM_MAGIC:
        raise ValueError("不是 STRM（魔数不是 STRM）")
    codec = struct.unpack_from("<H", head, 0x18)[0]
    if codec != STRM_CODEC_IMA:
        raise ValueError("STRM 编码 %d 不是 IMA-ADPCM（只实现了 codec=2）"
                         % codec)
    rate = struct.unpack_from("<H", head, 0x1C)[0]
    if rate <= 0:
        raise ValueError("采样率非法：%d" % rate)
    block = struct.unpack_from("<I", head, 0x30)[0] or STRM_BLOCK_SIZE
    if block <= 4:
        block = STRM_BLOCK_SIZE
    i = head.find(b"DATA", 0x10)
    if i < 0:
        raise ValueError("找不到 DATA 块")
    nbytes = struct.unpack_from("<I", head, i + 4)[0]
    data_off = i + 8
    try:
        size = os.path.getsize(str(path))
    except OSError:
        size = data_off + nbytes
    avail = max(0, size - data_off)
    if nbytes <= 0 or nbytes > avail:
        nbytes = avail                          # 头里的长度不可信时以实际为准
    nblocks = nbytes // block
    samples = nblocks * (block - 4) * 2         # 4bit 单声道，扣掉每块 4 字节头
    return {"block": block, "rate": rate, "channels": 1, "nbytes": nbytes,
            "data_off": data_off, "codec": codec,
            "secs": samples / float(rate) if rate else 0.0}


def decode_strm(path, cancel=None, on_progress: Optional[Callable] = None):
    """NDS STRM → ``(int16 单声道, rate)``。

    ⚠ **必须逐块解**：每块 ``block`` 字节，前 4 字节是**该块自己的**
    ``s16 hist + u16 step_index``。实测块 1 的头 hist=0，而块 0 的末样本是
    7500 —— 每块独立初始化，按整条流连续解会在每个块边界上爆音。
    """
    h = read_strm_header(path)
    rate = h["rate"]
    block = h["block"]
    with open(str(path), "rb") as f:
        f.seek(h["data_off"])
        data = f.read(h["nbytes"])
    parts = []
    nblocks = len(data) // block
    for bi in range(nblocks):
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        blk = data[bi * block:(bi + 1) * block]
        if len(blk) < 5:
            break
        hist = struct.unpack_from("<h", blk, 0)[0]
        step = struct.unpack_from("<H", blk, 2)[0]
        pcm, _ = _ima_decode(blk[4:], rate, hist=hist, step_index=step)
        parts.append(pcm)
        if on_progress and nblocks > 8 and bi % 64 == 0:
            try:
                on_progress(bi / float(nblocks))
            except Exception:                # noqa: BLE001
                pass
    if not parts:
        return np.zeros(0, np.int16), rate
    return np.concatenate(parts).astype(np.int16, copy=False), rate


def read_hwas_header(path) -> dict:
    """读 HWAS 头，返回 ``{rate, channels, block, nbytes, data_off, secs}``。"""
    with open(str(path), "rb") as f:
        head = f.read(HWAS_DATA_OFF)
    if len(head) < 0x1C or head[:4] != HWAS_MAGIC:
        raise ValueError("不是 HWAS（魔数不是 sawh）")
    block = struct.unpack_from("<I", head, 0x04)[0]
    rate = struct.unpack_from("<i", head, 0x08)[0]
    channels = struct.unpack_from("<i", head, 0x0C)[0]
    nbytes = struct.unpack_from("<I", head, 0x14)[0]
    if channels != 1:
        raise ValueError("HWAS 只见过单声道（本文件是 %d）" % channels)
    if rate <= 0:
        raise ValueError("采样率非法：%d" % rate)
    if block <= 0:
        raise ValueError("分块大小非法：%d" % block)
    try:
        size = os.path.getsize(str(path))
    except OSError:
        size = HWAS_DATA_OFF + nbytes
    avail = max(0, size - HWAS_DATA_OFF)
    if nbytes <= 0 or nbytes > avail:
        nbytes = avail                              # 头里的长度不可信时以实际为准
    # 4bit 单声道：一字节两个样本
    return {"block": block, "rate": rate, "channels": 1, "nbytes": nbytes,
            "data_off": HWAS_DATA_OFF,
            "secs": (nbytes * 2) / float(rate) if rate else 0.0}


# ══════════════════════════════════════════════════════════════════════
#  HWAS 解码
# ══════════════════════════════════════════════════════════════════════
def _ima_decode(data, rate, *, hist=0, step_index=0, high_first=False,
                count=None, cancel=None,
                on_progress: Optional[Callable] = None):
    """IMA ADPCM 半字节流 → ``(int16 单声道, rate)``。

    三种格式共用这一段。HWAS / SWAV 用「低半字节先」，DVI 用「高半字节先」，
    其余（步长表、索引调整、钳位）**完全相同** —— 这也正是 vgmstream 里
    ``coding_IMA`` 与 ``coding_DVI_IMA`` 的全部差别。

    Args:
        data: bytes，半字节流。
        rate: 采样率，原样带回（本函数不做重采样）。
        hist: 初始预测值。
        step_index: 初始步长索引（0~88，越界会被夹住）。
        high_first: True = 先解高 4 位（DVI IMA）；False = 先解低 4 位（标准 IMA）。
        count: 只解这么多个样本；None 表示全解。
        cancel: ``threading.Event``，置位时抛 :class:`_Cancelled`。
        on_progress: 进度回调，参数 0.0~1.0。

    Returns:
        ``(np.int16 一维数组, rate)``。
    """
    n = len(data) * 2
    if count is not None:
        n = min(n, int(count))
    if n <= 0:
        return np.zeros(0, dtype=np.int16), rate

    b = np.frombuffer(data, dtype=np.uint8)
    nib = np.empty(len(b) * 2, dtype=np.uint8)
    if high_first:
        nib[0::2] = b >> 4
        nib[1::2] = b & 0x0F
    else:
        nib[0::2] = b & 0x0F
        nib[1::2] = b >> 4
    nib = nib[:n]

    step_tab = _IMA_STEP
    idx_tab = _IMA_INDEX
    out = np.empty(n, dtype=np.int16)
    h = int(hist)
    si = int(step_index)
    if si < 0:
        si = 0
    elif si > 88:
        si = 88
    mask = 0xFFFF                    # 每 65536 个样本查一次 cancel / 报一次进度
    for i in range(n):
        if (i & mask) == 0:
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            if on_progress is not None and i:
                on_progress(i / float(n))
        code = int(nib[i])
        step = step_tab[si]
        delta = step >> 3
        if code & 1:
            delta += step >> 2
        if code & 2:
            delta += step >> 1
        if code & 4:
            delta += step
        if code & 8:
            delta = -delta
        h += delta
        if h > 32767:
            h = 32767
        elif h < -32768:
            h = -32768
        si += idx_tab[code]
        if si < 0:
            si = 0
        elif si > 88:
            si = 88
        out[i] = h
    if on_progress is not None:
        on_progress(1.0)
    return out, rate


def decode_hwas(path, cancel=None, on_progress: Optional[Callable] = None):
    """Vicarious Visions HWAS → ``(int16 单声道, rate)``。

    **每块开头重置 ADPCM 状态**（hist=0、step_index=0）。vgmstream 的注释
    说得很直白：不重置会有 *wild volume swings*。块大小就是头里 0x04 的值，
    块与块之间没有块头。

    与 vgmstream r2117 逐样本对照：29 个真实文件全部 100%、最大差 0。
    """
    h = read_hwas_header(path)
    rate, block, nbytes = h["rate"], h["block"], h["nbytes"]
    with open(str(path), "rb") as f:
        f.seek(HWAS_DATA_OFF)
        data = f.read(nbytes)

    chunks = []
    total = max(1, len(data))
    for off in range(0, len(data), max(1, block)):
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        blk = data[off:off + block]
        pcm, _ = _ima_decode(blk, rate, hist=0, step_index=0,
                             high_first=False)
        chunks.append(pcm)
        if on_progress is not None:
            on_progress(min(1.0, (off + len(blk)) / float(total)))
    if not chunks:
        return np.zeros(0, dtype=np.int16), rate
    return np.concatenate(chunks), rate


# ══════════════════════════════════════════════════════════════════════
#  Nintendo SWAV（.swav）
# ══════════════════════════════════════════════════════════════════════
SWAV_MAGIC = b"SWAV"
SWAV_DATA_OFF = 0x24

#: 0=PCM8（有符号）, 1=PCM16（小端有符号）, 2=IMA-ADPCM
_SWAV_PCM8, _SWAV_PCM16, _SWAV_IMA = 0, 1, 2


def read_swav_header(path) -> dict:
    """读 SWAV 头，返回 ``{wavetype, rate, loop, nbytes, data_off, secs}``。

    结构（全部小端，照 GBATEK 的 DS 文件格式「Sound Bank / SWAV」一节）：

    ======  ====  ================================================
    偏移    长度  含义
    ======  ====  ================================================
    0x00     4    ``SWAV`` 魔数
    0x04     2    BOM（``FF FE`` 表示小端）
    0x06     2    版本
    0x08     4    文件长度
    0x0C     2    头长度（0x10）
    0x0E     2    块数（1）
    0x10     4    ``DATA``
    0x14     4    块长度
    0x18     1    编码：0=PCM8 / 1=PCM16 / 2=IMA-ADPCM
    0x19     1    循环标志
    0x1A     2    采样率（**直接是 Hz**，不是定时器重装值）
    0x1C     2    Time
    0x1E     2    循环起点（单位 4 字节）
    0x20     4    非循环段长度（单位 4 字节）
    0x24    ..    采样数据
    ======  ====  ================================================

    ⚠ 「采样率直接是 Hz」这一条是**实测定的**，不是照抄：4 个样本的该字段
      都是 24050，而 ``nonLoopLength×4`` 与数据区实际长度逐个吻合，按
      24050 Hz 算出来的时长（1.6 / 3.1 / 3.3 秒）正好是「你死了 / 你赢了」
      这类短过场的合理长度；若当成 ARM7 定时器重装值（33513982/24050 ≈
      1393 Hz）则会变成 27 秒的长曲，显然不对。
    """
    with open(str(path), "rb") as f:
        head = f.read(SWAV_DATA_OFF)
    if len(head) < SWAV_DATA_OFF or head[:4] != SWAV_MAGIC:
        raise ValueError("不是 SWAV（魔数不是 SWAV）")
    if head[0x10:0x14] != b"DATA":
        raise ValueError("SWAV 里没有 DATA 块")
    wtype = head[0x18]
    if wtype not in (_SWAV_PCM8, _SWAV_PCM16, _SWAV_IMA):
        raise ValueError("SWAV 编码 %d 不认识（只认 0/1/2）" % wtype)
    rate = struct.unpack_from("<H", head, 0x1A)[0]
    if rate <= 0:
        raise ValueError("SWAV 采样率为 0")
    loop = bool(head[0x19])
    nonloop = struct.unpack_from("<I", head, 0x20)[0] * 4
    loop_off = struct.unpack_from("<H", head, 0x1E)[0] * 4

    try:
        size = os.path.getsize(str(path))
    except OSError:
        size = SWAV_DATA_OFF + nonloop
    avail = max(0, size - SWAV_DATA_OFF)
    nbytes = nonloop if 0 < nonloop <= avail else avail
    if loop and loop_off + nonloop < avail:
        nbytes = avail                     # 循环段也算进时长，一次放完

    if wtype == _SWAV_IMA:
        # 开头那 4 字节是初值 + 步长索引，不是样本
        secs = max(0, nbytes - 4) * 2 / float(rate)
    elif wtype == _SWAV_PCM16:
        secs = (nbytes // 2) / float(rate)
    else:
        secs = nbytes / float(rate)
    return {"wavetype": wtype, "rate": rate, "loop": loop, "loop_off": loop_off,
            "nbytes": nbytes, "data_off": SWAV_DATA_OFF, "secs": secs}


def decode_swav(path, cancel=None, on_progress: Optional[Callable] = None):
    """Nintendo SWAV → ``(int16 单声道, rate)``。

    IMA 分支的关键：**整个文件只有开头一个 4 字节头**（``s16 初值`` +
    ``s16 步长索引``），之后全是连续半字节流，**没有「每 36 字节一个帧头」**。
    上一轮就是按帧头解的 —— 每 36 字节跳过 4 字节，等于把音频数据当头部扔掉。

    与 vgmstream r2117 逐样本对照：4 个真实文件全部 100%、最大差 0。
    """
    h = read_swav_header(path)
    rate, wtype, nbytes = h["rate"], h["wavetype"], h["nbytes"]
    with open(str(path), "rb") as f:
        f.seek(SWAV_DATA_OFF)
        data = f.read(nbytes)

    if wtype == _SWAV_PCM8:
        return np.frombuffer(data, dtype=np.int8).astype(np.int16) * 256, rate
    if wtype == _SWAV_PCM16:
        n = len(data) // 2 * 2
        return np.frombuffer(data[:n], dtype="<i2").astype(np.int16), rate

    # ---- IMA-ADPCM ----
    if len(data) < 5:
        raise ValueError("SWAV 的 IMA 数据太短")
    hist = struct.unpack_from("<h", data, 0)[0]
    si = struct.unpack_from("<h", data, 2)[0]
    body = data[4:]
    return _ima_decode(body, rate, hist=hist, step_index=si,
                       high_first=False, count=len(body) * 2,
                       cancel=cancel, on_progress=on_progress)


# ══════════════════════════════════════════════════════════════════════
#  Konami DVI.（.dvi）—— Saturn / 街机
# ══════════════════════════════════════════════════════════════════════
DVI_MAGIC = b"DVI."
#: 交错单位：4 字节。**不是**按样本交错，也不是 0x10 那种整块。
DVI_INTERLEAVE = 4


def read_dvi_header(path) -> dict:
    """读 DVI 头，返回 ``{rate, channels, samples, loop_start, data_off, secs}``。

    结构（**大端**，照 vgmstream ``src/meta/dvi.c``）：

    ======  ======  ==============================================
    偏移    类型    含义
    ======  ======  ==============================================
    0x00    4       ``DVI.`` 魔数
    0x04    s32be   数据起点（实测 0x800 = 2048）
    0x08    s32be   总样本数（**每声道**）
    0x0C    s32be   循环起点；``-1`` 表示不循环
    ======  ======  ==============================================

    ⚠ 上一轮把 0x200（512）当成头长，读到的一直是零区 —— 真正的负载从
      0x800 起。修正后 45 个文件的「大端长度字段」与「文件长 − 2048」
      全部相等。
    """
    with open(str(path), "rb") as f:
        head = f.read(0x20)
    if len(head) < 0x10 or head[:4] != DVI_MAGIC:
        raise ValueError("不是 DVI（魔数不是 DVI.）")
    data_off = struct.unpack_from(">i", head, 0x04)[0]
    samples = struct.unpack_from(">i", head, 0x08)[0]
    loop_start = struct.unpack_from(">i", head, 0x0C)[0]
    if samples <= 0:
        raise ValueError("DVI 样本数非法：%d" % samples)
    try:
        size = os.path.getsize(str(path))
    except OSError:
        size = data_off
    if not (0 < data_off < size):
        raise ValueError("DVI 数据起点非法：%d（文件 %d 字节）" % (data_off, size))
    # 采样率恒为 44100、声道恒为 2（vgmstream 原话：no mono files seem to exist）
    return {"rate": 44100, "channels": 2, "samples": samples,
            "loop_start": loop_start, "data_off": data_off,
            "secs": samples / 44100.0}


def decode_dvi(path, cancel=None, on_progress: Optional[Callable] = None):
    """Konami DVI. → ``(int16 立体声交错, 44100)``。

    三个要点，缺一个就解不出音乐：

    1. **交错单位 4 字节**，且**右声道在前** —— vgmstream 是拿 Saturn 实机
       与 PS/OST 版对比后确定的，解完要把两个声道换回来。
    2. 编码是 **DVI IMA（高半字节先）**，不是标准 IMA。
    3. 起始 ADPCM 状态为 0（头里 0x10/0x20 那两个字段是循环历史，不是初值）。

    与 vgmstream r2117 逐样本对照：45 个真实文件全部 100%、最大差 0
    （最长的一首 2,656,256 帧 / 60.2 秒）。
    """
    h = read_dvi_header(path)
    samples = h["samples"]
    with open(str(path), "rb") as f:
        f.seek(h["data_off"])
        body = f.read()

    ilv = DVI_INTERLEAVE
    nblk = (len(body) + ilv - 1) // ilv
    left, right = [], []
    for i in range(nblk):
        blk = body[i * ilv:(i + 1) * ilv]
        # 偶数块 = 右声道，奇数块 = 左声道
        (right if i % 2 == 0 else left).append(blk)
        if (i & 0x3FFF) == 0 and cancel is not None and cancel.is_set():
            raise _Cancelled()

    lp, _ = _ima_decode(b"".join(left), h["rate"], high_first=True,
                        count=samples, cancel=cancel)
    rp, _ = _ima_decode(b"".join(right), h["rate"], high_first=True,
                        count=samples, cancel=cancel)
    n = min(len(lp), len(rp))
    st = np.empty(n * 2, dtype=np.int16)
    st[0::2] = lp[:n]
    st[1::2] = rp[:n]
    if on_progress is not None:
        on_progress(1.0)
    return st, h["rate"]


def read_npsf_header(path) -> dict:
    """读 NPSF 头，返回 ``{ch_size, channels, data_off, loop_start, rate,
    samples, secs}``。

    实测 ``BGM_E3_00000000.npsf``（Ace Combat 5）：ch_size=2,487,728、
    channels=2、data_off=**0x800**、loop_start=1,968,680、rate=44100
    → ``ps_bytes_to_samples(ch_size,1)`` = 4,353,524 samples / 98.72 秒。
    """
    with open(str(path), "rb") as f:
        head = f.read(0x40)
    if len(head) < 0x40 or head[:4] != NPSF_MAGIC:
        raise ValueError("不是 NPSF（魔数不是 NPSF）")
    ch_size = struct.unpack_from("<I", head, 0x08)[0]
    channels = struct.unpack_from("<I", head, 0x0C)[0]
    data_off = struct.unpack_from("<I", head, 0x10)[0]
    loop_start = struct.unpack_from("<i", head, 0x14)[0]
    rate = struct.unpack_from("<I", head, 0x18)[0]
    if channels < 1 or channels > 8:
        raise ValueError("声道数非法：%d" % channels)
    if rate <= 0 or rate > 192000:
        raise ValueError("采样率非法：%d" % rate)
    if ch_size <= 0 or data_off <= 0:
        raise ValueError("channel_size/data_offset 非法：%d/%d"
                         % (ch_size, data_off))
    samples = (ch_size // 16) * 28          # PSX ADPCM：16 字节块 = 28 samples
    return {"ch_size": ch_size, "channels": channels, "data_off": data_off,
            "loop_start": loop_start, "rate": rate, "samples": samples,
            "secs": samples / float(rate) if rate else 0.0}


def _psx_adpcm_decode(data):
    """PSX/XA ADPCM **连续流** → int32 数组。

    与 ``msu1_to_cd._xa_decode_group`` 的 nibble 逻辑**逐字一致**
    （两者都照抄 vgmstream ``coding_PSX``）：
      * ``shift > 12`` 夹到 9（PSX 的约定，VAG 也一样）
      * ``+32`` 做四舍五入再 ``>> 6``
      * **预测器历史用未钳位的值**，只有输出钳位
    这三点漏任何一个都会在长音频上累积误差。
    """
    n = len(data) // 16
    out = np.zeros(n * 28, np.int32)
    h1 = h2 = 0
    oi = 0
    for bi in range(n):
        off = bi * 16
        sp = data[off]
        sh = sp & 0x0F
        if sh > 12:
            sh = 9
        fi = sp >> 4
        if fi > 4:
            fi = 0
        c0 = _PSX_K0[fi]
        c1 = _PSX_K1[fi]
        for i in range(14):
            b = data[off + 2 + i]
            for nib in (b & 0x0F, b >> 4):       # 低半字节先
                nn = nib if nib < 8 else nib - 16
                s = (nn << 12) >> sh
                s += (c0 * h1 + c1 * h2 + 32) >> 6
                h2, h1 = h1, s                    # 历史不钳位
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                out[oi] = s
                oi += 1
    return out[:oi]


def decode_npsf(path, cancel=None, on_progress: Optional[Callable] = None):
    """NPSF → ``(int16 交错立体声, rate)``。

    实测 RMS 21.2% FS、分段 RMS 有起伏（6831/9477/7767/6447…）—— 正常音乐。
    """
    h = read_npsf_header(path)
    nch = h["channels"]
    with open(str(path), "rb") as f:
        f.seek(h["data_off"])
        body = f.read(h["ch_size"] * nch)
    # 按 0x800 字节交错分声道
    chunks = [[] for _ in range(nch)]
    pos = 0
    ci = 0
    while pos < len(body):
        chunks[ci].append(body[pos:pos + NPSF_INTERLEAVE])
        pos += NPSF_INTERLEAVE
        ci = (ci + 1) % nch
    chans = []
    for c in range(nch):
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        chans.append(_psx_adpcm_decode(
            b"".join(chunks[c])[:h["ch_size"]]))
        if on_progress is not None:
            try:
                on_progress((c + 1) / float(nch))
            except Exception:                    # noqa: BLE001
                pass
    n = min(len(x) for x in chans)
    if nch == 1:
        return chans[0][:n].astype(np.int16, copy=False), h["rate"]
    st = np.empty(n * 2, dtype=np.int16)
    st[0::2] = chans[0][:n]
    st[1::2] = chans[1][:n]
    return st, h["rate"]


#: VAG（PlayStation / PS Vita）的魔数
VAG_MAGIC = b"VAGp"


def read_vag_header(path) -> dict:
    """读 VAGp 头。返回 ``{ch_size, channels, data_off, rate, samples, secs}``。

    布局（全部大端）::

        0x00 "VAGp"      0x04 版本      0x0C 数据字节数
        0x10 采样率       0x20 名称（16 字节）
        0x30 数据起点

    ⚠ **数据长度优先信文件大小，不信头里的字段** —— 见过头里写 0 的包。
    ⚠ 声道数**不在头里**（VAGp 只定义单声道）。多声道变体（.vag 的立体声版）
      是按块交错，本工具没遇到，一律按单声道处理。
    """
    with open(str(path), "rb") as f:
        head = f.read(0x30)
        if len(head) < 0x30 or head[:4] != VAG_MAGIC:
            raise ValueError("不是 VAGp（魔数不是 VAGp）")
        rate = int.from_bytes(head[0x10:0x14], "big")
        size = int.from_bytes(head[0x0C:0x10], "big")
        f.seek(0, 2)
        total = f.tell() - 0x30
    if size <= 0 or size > total:
        size = max(0, total)
    if rate <= 0 or rate > 192000:
        raise ValueError("VAG 采样率非法：%d" % rate)
    samples = (size // 16) * 28          # PSX ADPCM：16 字节块 = 28 samples
    return {"ch_size": size, "channels": 1, "data_off": 0x30, "rate": rate,
            "samples": samples, "secs": samples / float(rate)}


def decode_vag(path, cancel=None, on_progress: Optional[Callable] = None):
    """VAG → ``(int16, rate)``（单声道）。

    为什么不走 PyAV：FFmpeg 的 ``vag`` 解复用器认得出头（probe 能给出
    44100/单声道/11 秒），但**解码时会 `avcodec_send_packet()` 报
    InvalidDataError** —— 实测 2 个样本都这样。而 VAG 的数据就是标准
    PSX-ADPCM（28 样本/块），本模块已经有逐字对照过 vgmstream 的解码器
    :func:`_psx_adpcm_decode`，直接用它更稳。
    """
    h = read_vag_header(path)
    with open(str(path), "rb") as f:
        f.seek(h["data_off"])
        body = f.read(h["ch_size"])
    if cancel is not None and cancel.is_set():
        raise _Cancelled()
    if on_progress is not None:
        try:
            on_progress(0.5)
        except Exception:                        # noqa: BLE001
            pass
    pcm = _psx_adpcm_decode(body)
    if on_progress is not None:
        try:
            on_progress(1.0)
        except Exception:                        # noqa: BLE001
            pass
    return pcm.astype(np.int16, copy=False), h["rate"]


def _resample_to_out(pcm, rate, nch=1):
    """任意采样率 int16（交错）→ 44100 立体声交错 int16（走 PyAV/SWR）。

    Args:
        pcm: int16 一维数组（多声道按 L,R,L,R… 交错）。
        rate: 源采样率。
        nch: 源声道数（1 或 2）。DVI 是 2，其余是 1。

    Returns:
        int16 一维数组，44100 Hz、2 声道交错。
    """
    import av
    if nch >= 2:
        src = np.ascontiguousarray(pcm, dtype=np.int16).reshape(1, -1)
        if nch == 2 and rate == OUT_RATE:
            return src.reshape(-1)
        # ⚠ **不能**一律用 "stereo"：Wwise 的 .wem 实测 195/224 个是
        #   4 声道（FL FR BL BR），按 stereo 声明会让 SWR 把前两个样本
        #   当左右、后两个当下一帧 —— 输出速度翻倍、音调全错。
        #   SWR 认得 "quad"，会按 FL+BL→L / FR+BR→R 正确降混。
        layout = _SRC_LAYOUT.get(nch, "stereo")
    else:
        src = np.ascontiguousarray(pcm, dtype=np.int16).reshape(1, -1)
        if rate == OUT_RATE:
            # 同率也要上混到立体声：直接复制一份左声道
            return np.repeat(src.reshape(-1, 1), 2, axis=1).reshape(-1)
        layout = "mono"

    # ⚠ SWR 会**整体吞掉**极短输入：实测 ≤16 帧（22050Hz 下约 0.7ms）时
    #   输出 0 帧，64 帧才正常。3DO 的 .aif 里真有几百帧的音效（按键声、
    #   菜单提示），一旦短到 SWR 的下限就会产出**空 WAV** —— 界面上表现为
    #   「转换成功但文件是 0 秒」，比报错还难查。
    #   做法：先补静音到安全长度，重采样后按**理论帧数**裁回去。
    #   补的是零、裁掉的也正是补的那些，不改变原有长音频的任何样本。
    n_in = src.size // nch
    need_out = int(round(n_in * OUT_RATE / float(rate)))
    padded = n_in < _SWR_MIN_FRAMES
    if padded:
        src = np.concatenate(
            [src.reshape(-1),
             np.zeros(_SWR_MIN_FRAMES * nch - src.size, dtype=np.int16)]
        ).reshape(1, -1)

    fr = av.AudioFrame.from_ndarray(src, format="s16", layout=layout)
    fr.sample_rate = int(rate)
    res = av.AudioResampler(format="s16", layout="stereo", rate=OUT_RATE)
    parts = []
    for f in list(res.resample(fr)) + list(res.resample(None)):
        arr = f.to_ndarray()
        # ⚠ 按 samples 切片：PyAV 的 plane 缓冲区带对齐填充，
        #   整块 tobytes() 会多出尾部几十毫秒脏样本（cd_audio 踩过同一个坑）。
        need = f.samples * OUT_CH
        parts.append(arr.reshape(-1)[:need])
    if not parts:
        return np.zeros(0, dtype=np.int16)
    out = np.concatenate(parts).astype(np.int16, copy=False)
    if padded:
        # 裁回「这段输入本来该有的长度」；SWR 万一少给了就原样返回，别补零
        out = out[:need_out * OUT_CH]
    return out


# ══════════════════════════════════════════════════════════════════════
#  BTSND（Wii U 每个游戏的启动音，v5.9.12）
# ══════════════════════════════════════════════════════════════════════
#
# 布局**照抄 vgmstream ``src/meta/btsnd.c``**，八字节头之后就是裸 PCM：
#
#     0x00 u32be type        0 或 2（2 = 一定循环）
#     0x04 s32be loop_start  不循环时是 0 或小于总样本数的值
#     0x08 起 PCM16 **大端**，2 声道，48000 Hz，interleave 0x02
#
# ⚠ 采样率/声道/位宽**都不在文件里** —— 这是格式自带的常量（Wii U 的
#   系统启动音只有这一种规格），vgmstream 也是写死 48000/2ch/PCM16BE。
#   所以这里不是「猜」，是照格式常量来。真要变了，判据是文件大小算出来
#   的样本数不是整数（``(size-8) % 4 != 0``）。
# ⚠ 本机只有 1 个样本，头部 8 字节恰好全是 0（type=0、loop_start=0），
#   所以没法用「type 必须是 0/2」当识别依据 —— 识别只靠扩展名。
BTSND_HEAD = 0x08
BTSND_RATE = 48000
BTSND_CH = 2


def read_btsnd_header(path) -> dict:
    """读 BTSND 头。返回 ``{rate, channels, samples, secs, loop_start}``。"""
    with open(str(path), "rb") as f:
        head = f.read(BTSND_HEAD)
        if len(head) < BTSND_HEAD:
            raise ValueError("BTSND 文件不足 %d 字节" % BTSND_HEAD)
        f.seek(0, 2)
        total = f.tell()
    body = total - BTSND_HEAD
    if body <= 0 or body % (2 * BTSND_CH) != 0:
        raise ValueError("BTSND 数据长度不是 4 的倍数（%d 字节）" % body)
    samples = body // (2 * BTSND_CH)
    loop_start = int.from_bytes(head[0x04:0x08], "big", signed=True)
    return {"rate": BTSND_RATE, "channels": BTSND_CH, "samples": samples,
            "secs": samples / float(BTSND_RATE), "loop_start": loop_start}


def decode_btsnd(path, cancel=None, on_progress: Optional[Callable] = None):
    """BTSND → ``(int16 交错, 48000)``（立体声）。

    大端转小端用 numpy 的 ``byteswap``，比逐样本 ``struct`` 快两个数量级。
    """
    h = read_btsnd_header(path)
    with open(str(path), "rb") as f:
        f.seek(BTSND_HEAD)
        body = f.read(h["samples"] * 2 * BTSND_CH)
    if cancel is not None and cancel.is_set():
        raise _Cancelled()
    if on_progress is not None:
        try:
            on_progress(1.0)
        except Exception:                            # noqa: BLE001
            pass
    arr = np.frombuffer(body, dtype=">i2")
    return arr.astype("<i2", copy=False), BTSND_RATE


def decode_any(path, cancel=None, on_progress: Optional[Callable] = None):
    """按扩展名解成 ``(int16 交错, rate, 声道数)``。

    只覆盖「原生解码」的三种（HWAS / SWAV / DVI）。``.adx`` 走 PyAV、
    ``.mod`` 走 :mod:`mod_audio`，各有各的入口，不经过这里。
    """
    kind = kind_of(path)
    if kind == "hwas":
        pcm, rate = decode_hwas(path, cancel, on_progress)
        return pcm, rate, 1
    if kind == "swav":
        pcm, rate = decode_swav(path, cancel, on_progress)
        return pcm, rate, 1
    if kind == "dvi":
        pcm, rate = decode_dvi(path, cancel, on_progress)
        return pcm, rate, 2
    if kind == "strm":
        pcm, rate = decode_strm(path, cancel, on_progress)
        return pcm, rate, 1
    if kind == "npsf":
        pcm, rate = decode_npsf(path, cancel, on_progress)
        return pcm, rate, 2
    if kind == "vag":
        pcm, rate = decode_vag(path, cancel, on_progress)
        return pcm, rate, 1
    if kind == "btsnd":
        pcm, rate = decode_btsnd(path, cancel, on_progress)
        return pcm, rate, BTSND_CH
    if kind in VGM_KINDS:
        # Wwise（.wem）/ FMOD FSB5（.fsb）—— FFmpeg 认不了，走 vgmstream。
        # ⚠ 它按**原生**采样率/声道出，所以这里必须把 nch 一起带出去：
        #   Wwise 实测 195/224 个是 4 声道（FL FR BL BR），
        #   当成 2 声道去重采样会得到「左右各混了两个声道」的错声像。
        import vgm_audio
        r = vgm_audio.decode_pcm(path, cancel=cancel, on_progress=on_progress)
        if not r["ok"]:
            raise RuntimeError(r["error"] or "vgmstream 解码失败")
        return (np.frombuffer(r["pcm"], dtype="<i2"), r["rate"], r["channels"])
    if kind == "aif":
        import aif_audio
        pcm, rate = aif_audio.decode_pcm(path, cancel, on_progress)
        # 声道数从文件头拿：3DO 见到的都是单声道，但 16bit 立体声 AIFF
        # 理论上也存在，别写死。
        try:
            nch = aif_audio.read_header(path)["channels"]
        except Exception:                        # noqa: BLE001
            nch = 1
        return pcm, rate, nch
    raise ValueError("格式 %r 没有原生解码器" % kind)


# ══════════════════════════════════════════════════════════════════════
#  裸 PCM（给「添加音源 → 转换」那条手动流程用）
# ══════════════════════════════════════════════════════════════════════
def decode_pcm(path, cancel=None, on_progress: Optional[Callable] = None) -> bytes:
    """任意流式音频 → 立体声 16bit **小端** PCM 字节（44.1k）。

    为什么需要这个：``msu1_to_cd.load_source`` 那条路要的是**裸 PCM 字节**
    （音轨字典里的 ``pcm``），不是落盘的 WAV。``convert_one`` 只负责落盘，
    所以这里单独给一条「解到内存」的入口 —— 两条路共用同一套解码器，
    不会出现「下载页能转、手动加音源不能转」的分裂。

    实测踩过的坑：``msu1_to_cd.load_source`` 原来只认 MSU-1/CUE/MP3/SPC/
    芯片音源，手动加一个 ``.mod`` 进去会在转换线程里抛 ``SystemExit``
    （界面上就是「一直转、永远不结束」）。
    """
    src = str(path or "")
    kind = kind_of(src)
    if not kind:
        raise ValueError("不是流式音频格式")
    if not os.path.isfile(src):
        raise RuntimeError("源文件不存在")

    if kind == "mod":
        import mod_audio
        pcm, rate = mod_audio.render(src, cancel=cancel, on_progress=on_progress)
        return _resample_to_out(pcm, rate).tobytes()

    if kind in ("hwas", "swav", "dvi", "strm", "npsf", "aif", "vag",
                "btsnd") or kind in VGM_KINDS:
        pcm, rate, nch = decode_any(src, cancel, on_progress)
        return _resample_to_out(pcm, rate, nch).tobytes()

    # ---- 其余 PyAV 格式：直接出 s16 立体声 ----
    return _decode_pyav_bytes(src, cancel)


def _decode_pyav_bytes(src, cancel=None) -> bytes:
    """PyAV 解码 → 立体声 16bit 小端 PCM（44.1k）。**不认扩展名**。

    抽出来是因为有第二个调用方：``.txtp`` 索引可能指向 ``.at9`` / ``.vag`` /
    ``.wav``（PS Vita 的包就是），那些扩展名不一定登记在 :data:`EXT_KIND` 里，
    但「用 PyAV 解」这件事完全一样。
    """
    import cd_audio
    av = cd_audio._av()
    out = []
    with av.open(src) as container:
        if not container.streams.audio:
            raise RuntimeError("文件里没有音频流")
        res = av.AudioResampler(format="s16", layout="stereo", rate=OUT_RATE)
        for frame in container.decode(container.streams.audio[0]):
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            for rf in res.resample(frame):
                # ⚠ 按 rf.samples 切片，理由同 cd_audio.convert_one：plane
                #   缓冲区带对齐填充，整块 bytes() 会多出脏样本。
                out.append(bytes(rf.planes[0])[:rf.samples * OUT_CH * OUT_WIDTH])
        for rf in res.resample(None):
            out.append(bytes(rf.planes[0])[:rf.samples * OUT_CH * OUT_WIDTH])
    return b"".join(out)


def decode_with_av(path, cancel=None) -> bytes:
    """不管扩展名，用 PyAV 解成 44100/16bit/立体声（给 ``.txtp`` 的目标用）。

    为什么单独开一个入口：``decode_pcm`` 会先过 :func:`kind_of`，而 ``.wav``
    这种扩展名**故意不登记**在 :data:`EXT_KIND` 里（登记了会让「加一个 .wav
    当音源」变成全局行为，改动面太大）。``.txtp`` 指到它时又要能解，
    所以这条只做「PyAV 解码」这一件事。
    """
    src = str(path or "")
    if not os.path.isfile(src):
        raise RuntimeError("源文件不存在：%s" % src)
    return _decode_pyav_bytes(src, cancel)


# ══════════════════════════════════════════════════════════════════════
#  转换（契约与 cd_audio.convert_one 完全一致）
# ══════════════════════════════════════════════════════════════════════
def convert_one(src, out_dir, *, used: Optional[Set[str]] = None,
                start_idx: int = 0, max_secs: float = 0.0,
                cancel=None, on_log: Optional[Callable[[str], None]] = None,
                on_progress: Optional[Callable[..., None]] = None) -> dict:
    """解一首流式音频 -> ``NN_标题.wav``（44.1k/16bit/2ch）。

    返回结构与 :func:`cd_audio.convert_one` 同形（``ok/fail/files/seconds/
    errors/last_idx``），这样 ``dl_convert`` 的记账逻辑一行都不用改。
    """
    log = on_log or (lambda _t: None)
    prog = on_progress or (lambda *_a: None)
    stat = {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
            "errors": [], "last_idx": int(start_idx or 0)}
    src = str(src or "")
    if not src or not os.path.isfile(src):
        stat["errors"] = ["源文件不存在"]
        return stat
    kind = kind_of(src)
    if not kind:
        stat["errors"] = ["不是流式音频格式"]
        return stat

    # ADX / XVAG / AT3 / HCA / BRSTM / LAAC 直接转手 —— FFmpeg 原生就能解，
    # 整条「解码→重采样→落盘→截断淡出」cd_audio 已经跑通了。
    if kind in PYAV_KINDS:
        import cd_audio
        return cd_audio.convert_one(
            src, out_dir, used=used, start_idx=start_idx, max_secs=max_secs,
            cancel=cancel, on_log=on_log, on_progress=on_progress)

    import msu1_to_cd as core
    from cd_audio import _title_of
    os.makedirs(out_dir, exist_ok=True)
    used = set() if used is None else used
    idx = int(start_idx or 0) + 1
    title = _title_of(src)
    try:
        max_secs = max(0.0, float(max_secs or 0.0))
    except (TypeError, ValueError):
        max_secs = 0.0

    base = title
    name = base
    n = 2
    while name.lower() in used:
        name = "%s (%d)" % (base, n)
        n += 1
    final = os.path.join(out_dir, "%02d_%s.wav" % (idx, name))
    part = final + ".part"

    try:
        if kind == "mod":
            import mod_audio
            pcm, rate = mod_audio.render(src, cancel=cancel,
                                         on_progress=on_progress)
            nch = 1
        else:
            # hwas / swav / dvi —— 纯 Python 解码，见 decode_any
            pcm, rate, nch = decode_any(src, cancel, on_progress)
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        stereo = _resample_to_out(pcm, rate, nch)
        frames = stereo.size // OUT_CH
        limit = int(max_secs * OUT_RATE) if max_secs > 0 else 0
        cut = bool(limit and frames > limit)
        if cut:
            stereo = stereo[:limit * OUT_CH]
            frames = limit
        raw = stereo.tobytes()
        with wave.open(part, "wb") as w:
            w.setnchannels(OUT_CH)
            w.setsampwidth(OUT_WIDTH)
            w.setframerate(OUT_RATE)
            w.writeframes(raw)
        if cut:
            from cd_audio import _fade_file
            _fade_file(part, core, OUT_RATE)
        os.replace(part, final)
    except _Cancelled:
        _unlink(part)
        stat["fail"] = 0
        stat["errors"] = []
        return stat
    except Exception as exc:                        # noqa: BLE001
        _unlink(part)
        msg = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        stat["errors"] = [msg]
        log("流式音频失败 %s — %s" % (os.path.basename(src), msg))
        return stat

    secs = frames / float(OUT_RATE)
    used.add(name.lower())
    stat.update({"ok": 1, "fail": 0, "files": [final], "seconds": secs,
                 "last_idx": idx, "errors": []})
    log("  WAV 流式: %s  %d:%04.1f  %.1fMB"
        % (os.path.basename(final), int(secs // 60), secs % 60,
           os.path.getsize(final) / 1048576.0))
    prog(secs)
    return stat


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass
