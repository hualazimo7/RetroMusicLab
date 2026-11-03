# -*- coding: utf-8 -*-
"""PS1 的「.txtp 包」→ WAV：**完全原生解码，不需要 foobar2000**。

包长什么样
----------
JoshW 的 PS1 音源有两种形态，这一种是 vgmstream 风格的：

```
<包名>/
  !tags.m3u                 # 曲名表：`# %TITLE xxx` 下一行是文件名
  XA_STR1-0042_0a05.txtp    # 每首一个：内容是一行
  sound/
    XA_STR1-0042_0a05.xa    # 2352 字节扇区的 XA-ADPCM 流
    CD1[jp].DA              # 裸 PCM16LE / 44100 / 立体声
    .DA.TXTH                # 上面那种裸 PCM 的参数说明
    SOTN.psflib / *.psf     # 音序音乐用的库（本模块**不处理**，见下）
```

`.txtp` 的内容就是一行，例如：

```
sound/XA_STR1-0000_0000.xa #I 2153088
```

`#I` 是 vgmstream 的 **install-loop**（强制循环点，单位是采样数），
**不是**起点偏移 —— 早先按"起点"算会得到负时长。转 WAV 时忽略它
（用户要的是 OST，单遍即可）。

为什么这个能原生解
------------------
`.xa` 是标准 CD-XA 扇区（12B sync + 4B header + 8B subheader + 2324B data），
和光盘镜像里的 XA 一模一样 —— 直接复用 ``msu1_to_cd`` 那套已验证的解码器
（``xa_sector_groups`` / ``_xa_decode_group`` / ``_xa_to_pcm``）。
`.DA` 更简单：``.DA.TXTH`` 写明是 ``PCM16LE / 2ch / 44100``，读出来就是成品。

⚠ 实测坑：这些 `.xa` 的 coding 字节是 **0x01**，按 CD-XA 规范读像是
"8bit 单声道"，但实际数据是 **4bit 立体声**。判据是解码后的平滑度：
4bit 立体声解释 → 0.988（音乐），8bit 单声道解释 → 0.088（噪声）。
所以这里**不看 coding 字节**，固定按 4bit 立体声解。

不处理的部分
------------
包里那两三个 `.psf` / `.psflib` 是音序音乐的库文件（要 PS1 模拟器），
但**所有 `.txtp` 都不指向它们** —— 实测恶魔城包 41 个 `.txtp` 全部指向
`.xa` / `.DA`。所以这里只管 `.txtp`。

PSF 本身由 :mod:`psf_native` 负责（v5.1-web-psf 起原生解码，外挂
``psf2wav.exe`` + 一份 BIOS）—— 那是**另一条路**，别往这个模块里塞。

没有 `.txtp` 的包（v5.2.4）
--------------------------
JoshW 的另一种形态是**平铺**的，一个索引文件都没有：

```
Armored Core (1997-07-10)(From Software)[PS1]/
  01 Menu BGM.PSF                    # 菜单曲（PSF，走 psf_native）
  driver.psflib                      # PSF 的库
  SCUS-94182_BGM01.XA … BGM14.XA     # 15 个独立 XA 流
  SCUS-94182_MOVIE - STAFF.XA
  ArmoredCore.txt                    # 说明
```

以前这种包**只转得出那一个 .PSF**，15 个 `.XA` 全被丢掉 —— 因为
``.xa`` 既不在 ``CHIP_EXTS`` 里（libgme 不吃），也没人把「裸 .xa」当成
一个音源。现在 :func:`is_xa` / :func:`probe_xa` / :func:`decode_xa_tracks`
把它们当**独立音源**处理：一个 ``.XA`` 里如果只有一条流就是一轨，有多条
子流就每条一轨。

⚠ 这些 ``.XA`` 带 **44 字节 RIFF/CDXA 头**（``RIFF <size> CDXA`` +
``fmt `` + ``data``），不是从 0 开始的裸扇区流。偏移算错会一个扇区都匹配
不上 —— 详见 ``msu1_to_cd._xa_scan_start``。
"""

import array
import os
import re
import threading
import wave

import msu1_to_cd as core

__all__ = [
    "TXTP_EXT", "XA_EXT", "parse_txtp", "load_titles", "probe_txtp", "probe_xa",
    "convert", "is_txtp", "is_xa", "list_txtp", "list_xa", "decode_xa_tracks",
]

TXTP_EXT = ".txtp"
#: 独立 CD-XA 音频流（JoshW 的 PS1 平铺包里就是这种）
XA_EXT = ".xa"
#: 裸 PCM 的参数兜底（正常情况下读 ``.DA.TXTH``）
_DA_RATE = 44100
_DA_CH = 2

#: ``.txtp`` 可以指向的「普通音频文件」—— 一律交给 PyAV / 流式解码（v5.9.11）。
#:
#: 为什么需要这一组：PS Vita 的包是**66 个 ``.txtp`` 索引**，指向
#: ``root/assets_psvita/sk1sound.arc/bgm119.at9`` 这类文件。索引本身不含音频，
#: 目标才是。原来这里只认 ``.xa`` / ``.DA`` / ``.psf``，于是整个机种报
#: 「不支持的目标类型 .at9」—— 明明 FFmpeg 一打开就认。
#:
#: ⚠ ``.wav`` / ``.mp3`` / ``.flac`` **故意不登记**进 ``stream_audio.EXT_KIND``
#:   （那会改变「什么算可加音源」的全局口径，改动面太大），所以这两个后缀走
#:   ``stream_audio.decode_with_av()`` —— 那个入口不查扩展名，只做解码。
_AV_TARGET_EXTS = (".at9", ".vag", ".wav", ".flac", ".mp3", ".ogg")

#: ``.txtp`` 可以指向的 **Wwise / FMOD** 目标（v5.9.12）。
#:
#: PS4 的包（Assassin's Creed IV）是 **186 个 ``.txtp`` + 224 个 ``.wem``**：
#: 那些 ``.txtp`` 是 WWISER 从 Wwise SoundBank 里导出的**事件/序列定义**，
#: 形如::
#:
#:      wem/591727928.wem #i #b 44.37 #r 9.50 #P 35.87
#:      wem/591727928.wem #i #p 35.12 #b 108.58 #r 72.95
#:     group = -L2 #@layer-v #@loop
#:
#: 也就是**同一段 wem 被裁成两段、分层叠起来再循环** —— 裸 wem 只是素材，
#: txtp 才是成品曲目。所以这一族**必须把 txtp 本身**交给 vgmstream
#: （它认得 ``#i`` / ``#b`` / ``#r`` / ``#p`` / ``#@layer-v`` / ``#@loop``
#: 全套命令，会自己拼），而不是把 ``tgt`` 交给它 —— 那就只剩一段了。
#:
#: ⚠ 这也是为什么这一族**不能**并进 ``_AV_TARGET_EXTS``：那一组的语义是
#:   「解 tgt」，而这里要解的是「txtp + 它描述的拼装关系」。
_VGM_TARGET_EXTS = (".wem", ".fsb")



def _decode_target_audio(tgt, cancel=None):
    """解一个「普通音频」目标（``.at9`` / ``.vag`` / ``.wav`` …）。

    分两条：**登记过的**（``.at9`` 走 PyAV、``.vag`` 走原生 PSX-ADPCM）走
    ``stream_audio.decode_pcm``；**没登记的**（``.wav`` 等）走 ``decode_with_av``。

    ⚠ 不要图省事一律用 ``decode_with_av``：``.vag`` 的 FFmpeg 解码器会报
      ``InvalidDataError``（能 probe、不能解），必须走原生那条。
    """
    import stream_audio
    if stream_audio.kind_of(tgt):
        return stream_audio.decode_pcm(tgt, cancel=cancel)
    return stream_audio.decode_with_av(tgt, cancel=cancel)


def _probe_target_audio(tgt):
    """探一个「普通音频」目标。契约同 :func:`probe_txtp`（不抛异常）。"""
    import stream_audio
    if stream_audio.kind_of(tgt):
        return stream_audio.probe(tgt)
    import cd_audio
    return cd_audio.probe(tgt)

#: ``(abspath, size, mtime) -> {子流键: 扇区数}``。
#: 一个包里 20 多个 ``.txtp`` 指向**同一个** ``.XA``，每个都扫一遍文件的话
#: 光是列出曲目就要扫 20 遍 50MB。扫一次、记住，后面全是查表。
_GROUP_CACHE = {}
_GROUP_LOCK = threading.Lock()


def _group_counts(target):
    """``.xa`` 里每条子流的扇区数 ``{(file, channel): n}``（带缓存）。"""
    try:
        st = os.stat(target)
    except OSError:
        return {}
    ck = (os.path.abspath(target), int(st.st_size), int(st.st_mtime))
    with _GROUP_LOCK:
        hit = _GROUP_CACHE.get(ck)
    if hit is not None:
        return hit
    try:
        groups = core.xa_sector_groups(target)
    except Exception:                                # noqa: BLE001 扫描失败按「没有」处理
        groups = {}
    counts = dict((k, len(v)) for k, v in groups.items())
    with _GROUP_LOCK:
        if len(_GROUP_CACHE) > 64:                   # 别无限涨
            _GROUP_CACHE.clear()
        _GROUP_CACHE[ck] = counts
    return counts


def is_txtp(path):
    """是不是 ``.txtp`` 轨道索引文件。"""
    return str(path or "").lower().endswith(TXTP_EXT)


def list_txtp(folder):
    """列出文件夹里的 ``.txtp``（只扫一层，按名排序）。"""
    out = []
    try:
        # v5.9.12：目录本身也可能超长（包名 + 子目录层层嵌套）。
        # ``os.listdir`` 在那种路径上直接抛 OSError，被下面吞掉就变成
        # 「这个文件夹里没有 txtp」—— 又是一次静默少东西。
        names = os.listdir(core.long_path(folder))
    except OSError:
        return out
    for n in sorted(names):
        if n.lower().endswith(TXTP_EXT):
            p = os.path.join(folder, n)
            # v5.9.12：``os.path.isfile`` 在长路径上会返回 False（不是抛异常），
            # 于是这些 .txtp **静默消失** —— 界面少 38 首、不报错、日志里
            # 也没痕迹。PS4 的 AC4 包里就有 38 个 263~315 字符的路径。
            if os.path.isfile(core.long_path(p)):
                out.append(p)
    return out


def is_xa(path):
    """是不是一个独立的 CD-XA 音频流文件（``.XA``）。"""
    return str(path or "").lower().endswith(XA_EXT)


def list_xa(folder):
    """列出文件夹里的 ``.XA``（只扫一层，按自然序排序）。"""
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    try:
        key = core.natural_track_key
    except AttributeError:                       # 老版本没有这个函数
        key = str.lower
    for n in sorted(names, key=key):
        if n.lower().endswith(XA_EXT):
            p = os.path.join(folder, n)
            if os.path.isfile(p):
                out.append(p)
    return out


def probe_xa(path):
    """``.XA`` 元信息：子流数 / 总时长 / 体积。**只扫扇区头，不读音频**。

    返回 ``dict``：``{ok, streams, seconds, kind, size, error}``。
    ``seconds`` 是**所有子流之和**（单流文件就是它自己的时长）。
    """
    res = {"ok": False, "streams": 0, "seconds": 0.0, "kind": "XA",
           "size": 0, "error": ""}
    try:
        res["size"] = os.path.getsize(path)
    except OSError as exc:
        res["error"] = "读不到文件：%s" % exc
        return res
    try:
        groups = core.xa_sector_groups(path)
    except Exception as exc:                     # noqa: BLE001 交调用方报
        res["error"] = "扫描失败：%s" % exc
        return res
    if not groups:
        res["error"] = "文件里没有 XA 音频扇区（可能是被截断或不是 XA 流）"
        return res
    nsec = sum(len(v) for v in groups.values())
    res["streams"] = len(groups)
    res["seconds"] = nsec * core._XA_SECTOR_SECS
    res["kind"] = "XA" if len(groups) == 1 else "XA×%d" % len(groups)
    res["ok"] = True
    return res


def parse_txtp(path):
    """解析一个 ``.txtp``。

    返回 ``dict``：``{ok, target, loop, stream, error}``。

    ``target`` 是**绝对路径**（``.txtp`` 里写的是相对包目录的路径，
    分隔符可能是 ``/``，Windows 上要换掉）。
    ``loop`` 是 ``#I`` 的采样数（循环点），转换时不用，仅作诊断保留。
    ``stream`` 是**子流序号**（1 起），0 表示「整个文件」。

    两种 ``.txtp`` 都要认
    --------------------
    1. **内容式**（早期见到的）：文件里一行 ``sound/xxx.xa #I 2153088``。
    2. **文件名式**（本函数新增）：文件是**空的**，信息全在名字里 ——
       ``BGM.XA#01.txtp`` 表示「``BGM.XA`` 的第 1 条子流」。一个包里
       20 多个 0 字节的 ``.txtp`` 全是这种，早先直接报「.txtp 是空的」，
       整包 PS1 音源全部转不出来。
    """
    res = {"ok": False, "target": "", "loop": 0, "stream": 0, "error": ""}
    try:
        # v5.9.12：长路径。PS4 包里有 38 个 .txtp 的完整路径 263~315 字符，
        # 超过 Windows MAX_PATH(260)，直接 open 会报 FileNotFoundError。
        with open(core.long_path(path), "r", encoding="utf-8",
                  errors="replace") as fh:
            line = fh.read().strip()
    except OSError as e:
        res["error"] = "读不了 .txtp：%s" % e
        return res
    if not line:
        return _parse_txtp_name(path, res)
    # 一行里可能带 #I / #sN 之类的命令，目标就是第一个 # 之前那段
    head, _, tail = line.partition("#")
    tgt = head.strip()
    # ⚠ **行首的 ``?`` 是 vgmstream 的「可选文件」标记，不是文件名的一部分**
    #   （v5.9.12）。WWISER 在「这个事件引用的 wem 可能不在银行里」时就会写成
    #   ``?wem/163303038.wem #i #b 30.0``，意思是「缺了就跳过这一段」。
    #   不去掉的话拼出来的目标是 ``\?wem\163303038.wem``，
    #   于是「目标不存在」—— 实测 AC4 的 4 首 Plantations_Day 就是这么挂的，
    #   而那 4 个 wem 其实好端端躺在 wem/ 里。
    if tgt.startswith("?"):
        tgt = tgt.lstrip("? \t").strip()
    if not tgt:
        res["error"] = ".txtp 里没有目标文件"
        return res
    m = re.search(r"#I\s+(\d+)", line)
    if m:
        res["loop"] = int(m.group(1))
    # v5.9.11：裸 ``#<数字>`` = **子流号**（vgmstream 的写法）。
    #   GameCube 的 Pivotal 包里是 ``stream/STREAM__004.psf#005`` ——
    #   一个 .psf 里 165 条子流，靠这个号选。``#I`` / ``#E`` 之类以字母
    #   开头，不会被这条正则吃到。
    m = re.search(r"#\s*(\d+)(?![0-9])", line)
    if m:
        res["stream"] = int(m.group(1))
    base = os.path.dirname(os.path.abspath(path))
    res["target"] = os.path.normpath(os.path.join(base, tgt.replace("/", os.sep)))
    res["ok"] = True
    return res


def _parse_txtp_name(path, res):
    """空 ``.txtp``：从**文件名**解析 ``<目标>#<子流号>``。

    vgmstream 把「一个 XA 里的第 N 条流」写成 ``BGM.XA#01.txtp``（0 字节）。
    规则与内容式一致：``#`` 前是目标文件，``#`` 后是子流号。
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    head, sep, tail = stem.partition("#")
    tgt = head.strip()
    if not tgt:
        res["error"] = ".txtp 是空的，文件名里也没有目标（应为「文件#子流号.txtp」）"
        return res
    base = os.path.dirname(os.path.abspath(path))
    full = os.path.normpath(os.path.join(base, tgt.replace("/", os.sep)))
    if not os.path.isfile(core.long_path(full)):
        res["error"] = ("空 .txtp 指向的文件不存在：%s"
                        "（按文件名解析，应放在同名文件旁边）" % tgt)
        return res
    if sep:
        m = re.match(r"\s*(\d+)", tail)
        if m:
            res["stream"] = int(m.group(1))
    res["target"] = full
    res["ok"] = True
    return res


def load_titles(pack_dir):
    """读 ``!tags.m3u``，返回 ``{txtp 文件名: 曲名}``。

    格式是「``# %TITLE 曲名`` 下一行跟文件名」；也可能用 ``# @TITLE``。
    没有这个文件时返回空 dict（此时退回用文件名当曲名）。
    """
    out = {}
    p = os.path.join(pack_dir, "!tags.m3u")
    if not os.path.isfile(p):
        return out
    cur = None
    try:
        fh = open(p, "r", encoding="utf-8", errors="replace")
    except OSError:
        return out
    try:
        for raw in fh:
            ln = raw.strip()
            if not ln:
                continue
            low = ln.lower()
            if low.startswith("# %title") or low.startswith("# @title"):
                cur = ln.split(None, 2)[2].strip() if len(ln.split(None, 2)) > 2 else ""
                continue
            if ln.startswith("#"):
                continue
            if cur is not None:
                out[ln] = cur
                cur = None
    finally:
        try:
            fh.close()
        except OSError:
            pass
    return out


def _da_format(target):
    """读 ``.DA.TXTH`` 拿裸 PCM 参数；读不到就用兜底值。"""
    th = os.path.join(os.path.dirname(target), ".DA.TXTH")
    rate, ch = _DA_RATE, _DA_CH
    try:
        txt = open(th, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return rate, ch
    m = re.search(r"sample_rate\s*=\s*(\d+)", txt)
    if m:
        rate = int(m.group(1))
    m = re.search(r"channels\s*=\s*(\d+)", txt)
    if m:
        ch = int(m.group(1))
    return rate, ch


def probe_txtp(path):
    """这个 ``.txtp`` 会转出多长、多大。**不读音频**，只 stat + 算。

    返回 ``dict``：``{ok, kind, seconds, size, target, stream, error}``。

    子流式（``BGM.XA#01.txtp``）的时长只算**那一条流**的扇区，不是整个
    ``.XA`` —— 否则界面上 23 首全是同一个时长，用户会以为程序没区分。

    直接传一个 ``.XA`` 也认（v5.2.4）：转给 :func:`probe_xa`，返回同名字段，
    这样界面那条「PSX 音频」的探测分支不用改。
    """
    res = {"ok": False, "kind": "", "seconds": 0.0, "size": 0,
           "target": "", "stream": 0, "error": ""}
    if is_xa(path):
        xr = probe_xa(path)
        res.update({"ok": xr["ok"], "kind": xr["kind"],
                    "seconds": xr["seconds"], "size": xr["size"],
                    "target": str(path), "stream": xr["streams"],
                    "error": xr["error"]})
        return res
    info = parse_txtp(path)
    if not info["ok"]:
        res["error"] = info["error"]
        return res
    tgt = info["target"]
    stream = int(info.get("stream") or 0)
    res["target"] = tgt
    res["stream"] = stream
    if not os.path.isfile(core.long_path(tgt)):
        res["error"] = "目标文件不存在：%s" % os.path.basename(tgt)
        return res
    ext = os.path.splitext(tgt)[1].lower()
    try:
        size = os.path.getsize(tgt)
    except OSError:
        size = 0
    res["size"] = size
    if ext == ".xa":
        # 4bit 立体声：每扇区每声道 2016 采样，37800Hz
        nsec = 0
        if stream:
            counts = _group_counts(tgt)
            keys = sorted(counts)
            if 1 <= stream <= len(keys):
                nsec = counts[keys[stream - 1]]
            else:
                res["error"] = ("子流 #%d 超出范围（这个文件只有 %d 条子流）"
                                % (stream, len(keys)))
                return res
        else:
            nsec = size // core.SECTOR
        res["seconds"] = nsec * 2016.0 / 37800.0
        res["kind"] = ("XA#%d" % stream) if stream else "XA"
        res["ok"] = True
    elif ext == ".da":
        rate, ch = _da_format(tgt)
        res["seconds"] = size / float(rate * ch * 2) if rate and ch else 0.0
        res["kind"] = "CD-DA"
        res["ok"] = True
    elif ext == ".psf":
        # v5.9.11：GameCube 的 Pivotal PSF。时长要**走一遍段序列**才算得准
        # （分段文件的段是链式的，不是简单地把字节数除以码率）。
        import pivotal_psf
        pr = pivotal_psf.probe(tgt, subsong=(stream or 1))
        res.update({"ok": pr["ok"], "seconds": pr["secs"], "kind": pr["kind"],
                    "error": pr["error"]})
        if not pr["ok"]:
            return res
    elif ext in _VGM_TARGET_EXTS:
        # v5.9.12：PS4 的 Wwise 序列（分层 + 循环 + 裁剪）。
        # ⚠ 问的是 **txtp 本身**，不是 tgt —— 见 _VGM_TARGET_EXTS 的注释。
        import vgm_audio
        pr = vgm_audio.probe(path)
        res.update({"ok": pr["ok"], "seconds": pr["secs"],
                    "kind": pr["codec"] or "Wwise",
                    "error": pr["error"]})
        if not pr["ok"]:
            return res
    elif ext in _AV_TARGET_EXTS:
        # v5.9.11：PS Vita 的包是「66 个 .txtp 指向 .at9 / .vag / .wav」。
        # 这些不需要自己解 —— 分别走流式解码 / PyAV，直接问它们要时长。
        pr = _probe_target_audio(tgt)
        res.update({"ok": pr["ok"], "seconds": pr["secs"],
                    "kind": (pr["codec"] or ext.lstrip(".")).upper(),
                    "error": pr["error"]})
        if not pr["ok"]:
            return res
    else:
        res["error"] = ("不支持的目标类型 %s（只认 .xa / .DA / .psf / "
                        ".at9 / .vag / .wav）" % ext)
    return res


def _pick_stream(groups, stream):
    """从 ``{(file, channel): [offsets]}`` 里挑出 ``stream`` 指定的那条。

    ``stream`` 是 **1 起的序号**（``BGM.XA#01`` → 1），按 ``(file, channel)``
    升序排 —— 这和 vgmstream 的 subsong 口径一致：序号是「第几条流」，
    **不是** subheader 里那个原始 channel 字节。
    ``stream == 0`` 表示老口径（整个文件当一条），此时取扇区数最多的那条。
    """
    keys = sorted(groups)
    if not stream:
        return max(groups, key=lambda k: len(groups[k]))
    if not (1 <= stream <= len(keys)):
        raise RuntimeError(
            "子流 #%d 超出范围（这个文件只有 %d 条子流）" % (stream, len(keys)))
    return keys[stream - 1]


def _decode_xa(target, cancel=None, on_progress=None, stream=0):
    """``.xa`` -> 44.1kHz 小端立体声 PCM 字节。

    ``stream`` 见 :func:`_pick_stream`。
    """
    groups = core.xa_sector_groups(target)
    if not groups:
        raise RuntimeError("文件里没有 XA 音频扇区")
    key = _pick_stream(groups, stream)
    offsets = groups[key]
    total = len(offsets)
    left = array.array("h")
    right = array.array("h")
    # 分块解码：既能报进度，也能及时响应取消
    chunk = 512
    for i in range(0, total, chunk):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("已取消")
        l2, r2 = core._xa_decode_group(target, offsets[i:i + chunk])
        left.extend(l2)
        right.extend(r2)
        if on_progress:
            try:
                on_progress(0.15 + 0.65 * min(1.0, (i + chunk) / float(total)))
            except Exception:                        # noqa: BLE001
                pass
    return core._xa_to_pcm(left, right)


def decode_xa_tracks(path, cancel=None, on_progress=None):
    """把一个**独立的** ``.XA`` 解成音轨列表（一条子流一轨）。

    返回 ``[{"name", "pcm", "little_endian"}, …]`` —— 与
    ``convert_job._psf_tracks`` / ``spc_wav.write_tracks`` 同形，可以直接
    交给它们，不用在中间转一道。

    曲名口径：

    * 只有一条子流 → 一轨，曲名就是文件名（``SCUS-94182_BGM01``）
    * 多条子流 → 每条一轨，曲名加 ``#1`` / ``#2``…，按扇区数**从多到少**排
      （主音乐通常是最长那条，先转出来符合直觉）

    ⚠ 解码是纯 Python 逐采样递推，约 1.4M 采样/秒 —— 一首 2 分钟的 XA
    要 4~5 秒。15 个文件串起来转是分钟级的，进度回调不能省。
    """
    groups = core.xa_sector_groups(path)
    if not groups:
        raise RuntimeError("文件里没有 XA 音频扇区")
    keys = sorted(groups, key=lambda k: (-len(groups[k]), k))
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    n_total = float(sum(len(v) for v in groups.values())) or 1.0
    done = 0.0
    tracks = []
    for i, key in enumerate(keys, 1):
        offsets = groups[key]
        left = array.array("h")
        right = array.array("h")
        chunk = 512
        for j in range(0, len(offsets), chunk):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("已取消")
            l2, r2 = core._xa_decode_group(path, offsets[j:j + chunk])
            left.extend(l2)
            right.extend(r2)
            if on_progress:
                try:
                    on_progress(min(1.0, (done + min(chunk, len(offsets) - j))
                                    / n_total))
                except Exception:                # noqa: BLE001 回调不该影响解码
                    pass
        done += len(offsets)
        name = stem if len(keys) == 1 else "%s #%d" % (stem, i)
        tracks.append({"name": name, "pcm": core._xa_to_pcm(left, right),
                       "little_endian": True})
    return tracks


def _decode_da(target, cancel=None, on_progress=None):
    """``.DA``（裸 PCM16LE）-> 直接返回字节（本来就是 44.1k 立体声）。"""
    rate, ch = _da_format(target)
    with open(target, "rb") as fh:
        raw = fh.read()
    if rate != 44100 or ch != 2:
        # 少见：不是 44.1k 立体声就不能原样塞进 WAV，直接报清楚
        raise RuntimeError(
            "裸 PCM 参数是 %dHz/%dch，本工具只处理 44100Hz 立体声" % (rate, ch))
    if cancel is not None and cancel.is_set():
        raise RuntimeError("已取消")
    if on_progress:
        try:
            on_progress(0.8)
        except Exception:                            # noqa: BLE001
            pass
    # 奇数字节说明文件被截断，砍掉最后一个半样本
    if len(raw) % 4:
        raw = raw[:len(raw) - (len(raw) % 4)]
    return raw


def decode_pcm(path, cancel=None, on_progress=None):
    """把一个 ``.txtp`` 解成 16-bit/44.1k/立体声**小端 PCM 字节**。

    与 ``convert()`` 的分工：
      * ``convert()`` 是「批量直接写 WAV」的入口，自带编号/去重/记账；
      * ``decode_pcm()`` 只给 PCM，交给**主转换流水线**（``convert_job``）——
        这样 ``.txtp`` 才能和 SPC / 光盘镜像走同一条路，也才能出 BIN+CUE。

    曲名口径与 ``convert()`` 一致：先看 ``!tags.m3u``，没有就用文件名。

    返回 ``dict``：``{ok, pcm, kind, title, seconds, error}``。
    """
    res = {"ok": False, "pcm": b"", "kind": "", "title": "",
           "seconds": 0.0, "error": ""}
    stem = os.path.basename(str(path))
    try:
        titles = load_titles(os.path.dirname(os.path.abspath(path)))
    except OSError:
        titles = {}
    res["title"] = titles.get(stem) or os.path.splitext(stem)[0]

    # 直接传 .XA 也认（v5.2.4）：只返回**主那条子流**，多子流的完整拆分
    # 走 decode_xa_tracks()。这里留着是为了让「单文件单轨」的老调用方
    # （探测、试听）不至于因为文件是 .xa 就整个报错。
    if is_xa(path):
        try:
            tracks = decode_xa_tracks(path, cancel=cancel, on_progress=on_progress)
        except Exception as exc:                 # noqa: BLE001 交调用方报
            res["error"] = "解码失败：%s" % exc
            return res
        res["pcm"] = tracks[0]["pcm"]
        res["kind"] = "XA" if len(tracks) == 1 else "XA×%d" % len(tracks)
        res["title"] = tracks[0]["name"]
        res["seconds"] = len(res["pcm"]) / 4.0 / 44100.0
        res["ok"] = True
        return res

    info = parse_txtp(path)
    if not info["ok"]:
        res["error"] = info["error"]
        return res
    tgt = info["target"]
    if not os.path.isfile(core.long_path(tgt)):
        res["error"] = "目标文件不存在：%s" % os.path.basename(tgt)
        return res
    ext = os.path.splitext(tgt)[1].lower()
    stream = int(info.get("stream") or 0)
    try:
        if ext == ".xa":
            res["pcm"] = _decode_xa(tgt, cancel=cancel, on_progress=on_progress,
                                    stream=stream)
            res["kind"] = ("XA#%d" % stream) if stream else "XA"
        elif ext == ".da":
            res["pcm"] = _decode_da(tgt, cancel=cancel, on_progress=on_progress)
            res["kind"] = "CD-DA"
        elif ext == ".psf":
            # v5.9.11：GameCube 的 **Pivotal PSF**（《Conflict: Desert Storm》那类）。
            # ⚠ 它和 PS1/PS2 的 PSF 只是撞了扩展名 —— 没有程序段、不需要 BIOS，
            #   就是 PSX-ADPCM / NGC-DSP 的预录流，纯 Python 能解（:mod:`pivotal_psf`，
            #   与 vgmstream 逐样本最大差 0）。所以放在这里而不是 psf_native。
            import pivotal_psf
            res["pcm"] = pivotal_psf.decode_pcm(
                tgt, subsong=(stream or 1), cancel=cancel,
                on_progress=on_progress)
            res["kind"] = ("Pivotal PSF#%d" % stream) if stream else "Pivotal PSF"
        elif ext in _VGM_TARGET_EXTS:
            # v5.9.12：PS4 的 Wwise 序列。交给 **txtp 本身**（见
            # _VGM_TARGET_EXTS）—— vgmstream 会按 #i/#b/#r/#p/#@layer-v
            # 把多个 wem 拼成一首，并直接出 44100/立体声。
            import vgm_audio
            res["pcm"] = vgm_audio.decode_out(path, cancel=cancel,
                                              on_progress=on_progress)
            res["kind"] = "Wwise"
        elif ext in _AV_TARGET_EXTS:
            # v5.9.11：PS Vita（.at9 / .vag / .wav）。见 _decode_target_audio。
            res["pcm"] = _decode_target_audio(tgt, cancel=cancel)
            res["kind"] = ext.lstrip(".").upper()
        else:
            res["error"] = ("不支持的目标类型 %s（只认 .xa / .DA / .psf / "
                            ".at9 / .vag / .wav / .wem）" % ext)
            return res
    except Exception as e:                           # noqa: BLE001 交调用方报
        res["error"] = "解码失败：%s" % e
        return res
    res["seconds"] = len(res["pcm"]) / 4.0 / 44100.0
    res["ok"] = True
    return res


def convert(paths, out_dir, policy="txtp", on_log=None, on_progress=None,
            on_stage=None, cancel=None, speed=1.0, max_secs=0.0,
            used=None, start_idx=0, total=None, **_kw):
    """把一批 ``.txtp`` 转成 WAV。返回结构与 ``spc_wav.convert`` **同形**。

    为什么要同形：``dl_convert`` 的记账段（体积统计 / 零产出判定 /
    manifest / 连续编号）完全复用，一行都不用改。
    """
    import spc_wav                                # 延迟导入：避免循环依赖

    log = on_log or (lambda _t: None)
    stage = on_stage or (lambda _t: None)
    os.makedirs(out_dir, exist_ok=True)
    stat = {"ok": 0, "fail": 0, "files": [], "seconds": 0.0,
            "errors": [], "last_idx": start_idx}
    used = set() if used is None else used
    idx = start_idx
    items = [p for p in (paths or ()) if is_txtp(p) or is_xa(p)]
    total = total or (start_idx + len(items))

    for n, txtp in enumerate(items, 1):
        if cancel is not None and cancel.is_set():
            break
        # 独立 .XA（v5.2.4）：一个文件可能有多条子流，一次全解出来交给
        # write_tracks 批量写 —— 编号/去重/截断口径与 .txtp 完全一致。
        if is_xa(txtp):
            stage("解 %s %d/%d" % (os.path.basename(txtp), n, len(items)))
            try:
                tracks = decode_xa_tracks(txtp, cancel=cancel,
                                          on_progress=on_progress)
            except Exception as e:                   # noqa: BLE001
                stat["fail"] += 1
                msg = "%s: 解码失败 %s" % (os.path.basename(txtp), e)
                stat["errors"].append(msg)
                log("❌ " + msg)
                continue
            idx_before = idx
            sub = spc_wav.write_tracks(
                tracks, out_dir, max_secs=max_secs, on_log=log, cancel=cancel,
                used=used, start_idx=idx, total=total)
            stat["ok"] += int(sub.get("ok") or 0)
            stat["fail"] += int(sub.get("fail") or 0)
            stat["seconds"] += float(sub.get("seconds") or 0.0)
            stat["files"].extend(sub.get("files") or ())
            stat["errors"].extend(sub.get("errors") or ())
            idx = int(sub.get("last_idx") or idx_before)
            stat["last_idx"] = idx
            continue
        info = parse_txtp(txtp)
        pack = os.path.dirname(os.path.abspath(txtp))
        titles = load_titles(pack)
        stem = os.path.basename(txtp)
        name = titles.get(stem) or os.path.splitext(stem)[0]
        if not info["ok"]:
            stat["fail"] += 1
            stat["errors"].append("%s: %s" % (stem, info["error"]))
            log("❌ %s: %s" % (stem, info["error"]))
            continue
        tgt = info["target"]
        if not os.path.isfile(core.long_path(tgt)):
            stat["fail"] += 1
            msg = "%s: 目标不存在 %s" % (stem, os.path.basename(tgt))
            stat["errors"].append(msg)
            log("❌ " + msg)
            continue
        ext = os.path.splitext(tgt)[1].lower()
        stage("解 %s %d/%d" % (name, n, len(items)))
        try:
            if ext == ".xa":
                raw = _decode_xa(tgt, cancel=cancel, on_progress=on_progress,
                                 stream=int(info.get("stream") or 0))
            elif ext == ".da":
                raw = _decode_da(tgt, cancel=cancel, on_progress=on_progress)
            elif ext == ".psf":
                # v5.9.11：GameCube 的 Pivotal PSF（与 decode_pcm 同一分支）
                import pivotal_psf
                raw = pivotal_psf.decode_pcm(
                    tgt, subsong=(int(info.get("stream") or 0) or 1),
                    cancel=cancel, on_progress=on_progress)
            elif ext in _VGM_TARGET_EXTS:
                # v5.9.12：PS4 的 Wwise 序列（与 decode_pcm 同一分支，
                # 同样把 **txtp 本身** 交给 vgmstream）。
                import vgm_audio
                raw = vgm_audio.decode_out(txtp, cancel=cancel,
                                           on_progress=on_progress)
            elif ext in _AV_TARGET_EXTS:
                # v5.9.11：PS Vita（与 decode_pcm 同一分支）
                raw = _decode_target_audio(tgt, cancel=cancel)
            else:
                raise RuntimeError("不支持的目标类型 %s（只认 .xa / .DA / "
                                   ".psf / .at9 / .vag / .wav / .wem / .fsb）" % ext)
        except Exception as e:                       # noqa: BLE001
            stat["fail"] += 1
            msg = "%s: 解码失败 %s" % (name, e)
            stat["errors"].append(msg)
            log("❌ " + msg)
            continue

        # 交给 spc_wav.write_tracks 写盘 —— 命名/去重/截断/削波提示全复用
        idx_before = idx
        sub = spc_wav.write_tracks(
            [{"name": name, "pcm": raw, "little_endian": True}],
            out_dir, max_secs=max_secs, on_log=log, cancel=cancel,
            used=used, start_idx=idx, total=total)
        stat["ok"] += int(sub.get("ok") or 0)
        stat["fail"] += int(sub.get("fail") or 0)
        stat["seconds"] += float(sub.get("seconds") or 0.0)
        stat["files"].extend(sub.get("files") or ())
        stat["errors"].extend(sub.get("errors") or ())
        # write_tracks 的 last_idx 是**最终序号**（它内部 idx += 1 之后才返回），
        # 所以下一轮直接把 start_idx 设成它，**不能**再 +1 ——
        # 早先写成 `+ 1` 导致 01 之后直接跳到 03。
        idx = int(sub.get("last_idx") or idx_before)
        stat["last_idx"] = idx
        if on_progress:
            try:
                on_progress(1.0)
            except Exception:                        # noqa: BLE001
                pass
    return stat
