#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spc_wav.py — WAV 输出核心 (Pico-Plus / pico-infonesPlus)。

输出规格由设备固定要求, 不做可选项:
    16-bit / 44100Hz / 立体声 / 未压缩 PCM (.wav)

两条入口:
    write_tracks()  已解码的归一化音轨 -> WAV (带峰值检查、时长上限)
    convert()       SPC 文件列表 -> WAV (分批解码, 内存/磁盘占用有界)

解码复用 msu1_to_cd 的 libgme 子进程管线(避免解码卡死拖垮界面)。
"""

import os
import re
import wave

import msu1_to_cd as core

SR = 44100
BYTES_PER_SEC = SR * 2 * 2      # 16bit × 2ch
BATCH = 8                       # 每批解码数量, 限制临时 PCM 占用

#: 走 ``flac_out`` 编码的容器（v6.0.0：flac / mp3 / ogg）。
#:
#: ⚠ 这是 :data:`flac_out.WRITE_FMTS` 的**副本**，不是重复定义 ——
#: ``flac_out`` 一导入就拉 soundfile（几十 MB 的 DLL），放在模块级会
#: 让「只想转 WAV」的人也跟着付这笔启动开销，所以这里只留一份字符串表。
#: 两处必须同步；门禁 ``t_v600`` 有断言盯着（``set(ENC_FMTS) ==
#: set(flac_out.WRITE_FMTS)``），改一处漏一处会立刻红。
ENC_FMTS = ("flac", "mp3", "ogg")

#: 从 ``encode_pcm`` 的 detail 里抠自检 SNR 用的正则。
#:
#: detail 形如 ``"44100 帧 / 44100 Hz / 159232 B  自检 SNR 39.7 dB / 相关 1.0000"``。
#: 之所以从字符串里抠而不是改返回值：``encode_pcm`` 的 ``(ok, dst, detail)``
#: 三返回值被十来个调用点用着，为一个日志字段改签名不划算。
_SNR_RE = re.compile(r"自检 SNR\s*(-?[0-9.]+)\s*dB")

#: 封面在**音频目录里**的文件名（v5.9.16）。
#:
#: 下载页会把图复制到 ``FLAC/<游戏名>/cover.png`` —— 这样「把整个 FLAC
#: 目录拷给别人」时封面跟着走，也不再依赖 ``%LOCALAPPDATA%`` 里那份缓存。
#: 用 ``cover.png`` 而不是 ``folder.png``：后者是 Windows 资源管理器
#: 显示文件夹缩略图的约定名，会被系统当成目录图标，容易被误删/误改。
COVER_NAME = "cover.png"

# 播放长度策略: 界面显示文本 -> core 的 policy 名
POLICIES = {
    "单遍 (按 ID666 标注时长, 推荐)": "id666",
    "两遍 (想多听一轮循环)": "id666_x2",
    "自然结束 (解码到静音为止, 最慢最占空间)": "natural",
}
DEFAULT_POLICY_LABEL = "单遍 (按 ID666 标注时长, 推荐)"


def _natural_key(name):
    """自然排序: 让 "2 xxx" 排在 "10 xxx" 前面。"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", name)]


def collect_supported(paths, exts=None):
    """文件/文件夹混合列表 -> 去重后的文件列表(按名称自然排序)。

    exts=None 时用 chip_formats.ALL_EXTS(芯片 11 种 + .cue/.bin/.mp3);
    指定 exts 则只收该集合。**目录只扫一层**(与原 collect_spc 一致)。
    """
    if exts is None:
        import chip_formats
        exts = chip_formats.ALL_EXTS
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


def collect_spc(paths):
    """文件/文件夹混合列表 -> 去重后的 .spc 文件列表(按名称自然排序)。

    保留此函数是为了**零回归**: 外部调用点(msu1_gui / vendor 测试)全部不改。
    """
    return collect_supported(paths, (".spc",))


def probe(path):
    """读取 SPC 的 ID666 信息 -> (曲名, 游戏名, 秒数, 淡出毫秒)。"""
    return core._spc_id666_title(path)


def _peak_of(raw):
    """int16 小端字节流的峰值 (C 层 max/min, 比逐样本遍历快得多)。"""
    from array import array
    a = array("h")
    a.frombytes(raw)
    return max(max(a), -min(a)) if a else 0


def _limit_len(raw, max_secs):
    """按单曲最长时长裁剪, 并在末端 1.5s 升余弦淡出。返回 (raw, 是否截断)。

    44.1kHz/16bit/立体声下每秒固定 176.4KB, 体积唯一的可调项就是时长,
    所以裁剪是目前唯一有效的瘦身手段。硬切会有爆音, 必须带淡出。
    """
    if max_secs <= 0:
        return raw, False
    max_bytes = int(max_secs * BYTES_PER_SEC) & ~3     # 对齐到整帧
    if len(raw) <= max_bytes:
        return raw, False
    return core._apply_fade(raw[:max_bytes], 1500, SR), True


def _empty_stat():
    return {"ok": 0, "fail": 0, "files": [], "seconds": 0.0, "errors": [],
            "last_idx": 0}


def _merge(stat, sub):
    stat["ok"] += sub["ok"]
    stat["fail"] += sub["fail"]
    stat["seconds"] += sub["seconds"]
    stat["files"].extend(sub["files"])
    stat["errors"].extend(sub["errors"])


def _save_cover_beside(img, out_dir):
    """把封面复制成 ``<out_dir>/cover.png``，返回实际该用的路径。

    v5.9.16：用户要「封面放在下载目录里 FLAC 文件夹内的对应游戏文件夹」。
    这里做的是**落副本**：不管图是从全局缓存查到的还是调用方传进来的，
    只要这次转的是 FLAC / MP3，就在音频旁边留一份。

    为什么要留副本，而不是只认缓存：

    * 用户能在资源管理器里直接看见封面，不用去翻 ``%LOCALAPPDATA%``；
    * 把整个 ``FLAC/<游戏名>/`` 拷给别人的时候封面跟着走；
    * 清了缓存之后重新转码，``write_tracks`` 会优先用这份，不用再联网。

    复制失败就退回原路径 —— 嵌图是正事，落副本只是附带，不能因为它
    让整首转换失败。
    """
    if not img:
        return ""
    dst = os.path.join(str(out_dir), COVER_NAME)
    tmp = dst + ".part"
    try:
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return dst                        # 已经有了，别重复写
        with open(img, "rb") as fh:
            data = fh.read()
        if not data:
            return str(img)
        with open(tmp, "wb") as out:
            out.write(data)
        os.replace(tmp, dst)
        return dst
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return str(img)


def write_tracks(tracks, out_dir, max_secs=0.0, on_log=None, on_progress=None,
                 cancel=None, used=None, start_idx=0, total=None, fmt="wav",
                 cover=None, cover_beside=None):
    """归一化音轨 -> 每轨一个 WAV / FLAC。**不负责释放** tracks 的临时 PCM。

    tracks       core.load_source / load_spc_paths 的返回值
    max_secs     单曲最长时长(秒), 0 = 不限制; 超出则截断 + 1.5s 淡出
    used         跨批次文件名去重集合, 传同一个 set 可避免同名覆盖
    start_idx    起始序号(多批次时保持 01/02/03 连续)
    total        总曲目数, 仅用于日志分母
    fmt          ``"wav"``（默认）/ ``"flac"`` / ``"mp3"``（v5.9.15）/
                 ``"ogg"``（v6.0.0）。都是 16bit / 44100Hz / 立体声；
                 FLAC 逐样本无损（解回来与 WAV 完全一致，实测 md5 相同，
                 体积约 35%）；MP3 有损（libsndfile 自带 LAME，320 kbps
                 CBR，体积约 23%）；OGG 有损（libsndfile 自带 libvorbis，
                 q10 约 395 kbps VBR，体积约 28%）——
                 后两者是「换容器会动音频」的例外，所以**不参与**
                 逐字节一致性断言。
    cover        封面图片路径（可选，**只对 FLAC / MP3 生效**，OGG 不嵌）。
                 ``None`` 时按 ``out_dir`` 的目录名去查 —— 下载页的产物结构是
                 ``<根>/FLAC/<包名>/``，目录名就是游戏名。查不到就不嵌，
                 不影响转换成功。
    cover_beside 要不要把封面**复制一份到 ``out_dir``**（``cover.png``）。
                 ``None``（默认）= 自动：**只有封面是按目录名推断来的才落**
                 —— 那种情况下 ``out_dir`` 就是「一个游戏一个目录」（下载页
                 ``FLAC/<包名>/``），图跟着音频走是对的。
                 首页转换所有音源**共用一个输出目录**，那边显式传 ``cover``，
                 自动判定为「不落副本」—— 否则第一个音源的封面会糊到后面
                 每一个游戏上（v5.9.14 显式传 cover 就是为了防这个）。
                 传 ``True`` 可强制落（目前没人用，留给以后）。

    ⚠ ``fmt`` 必须留在**倒数第二个**参数：``convert()`` 内部是位置传参
      （``write_tracks(tracks, out_dir, max_secs, log, prog, cancel, used,
      start_idx, total)``），插在中间会把 log 当 cancel 传进去。
      v5.9.14 的 ``cover`` / v5.9.16 的 ``cover_beside`` 只能接在它后面。
    """
    log = on_log or (lambda _t: None)
    prog = on_progress or (lambda *_a: None)
    os.makedirs(out_dir, exist_ok=True)
    try:
        max_secs = max(0.0, float(max_secs or 0))
    except (TypeError, ValueError):
        max_secs = 0.0

    # 格式归一：认不出来的值一律退回 wav（宁可出 WAV，也别让整批失败）
    fmt = str(fmt or "").lower()
    if fmt not in ENC_FMTS:
        fmt = "wav"
    # v6.0.0：扩展名直接从格式名推，不再手写映射表 ——
    # 三种容器的后缀本来就等于 "." + fmt，多一张表就多一处会漏的地方。
    ext = ("." + fmt) if fmt in ENC_FMTS else ".wav"
    tag = fmt.upper()

    # 封面：显式传了就用；没传就**先看同目录**，再退回全局缓存。
    # 全程包在 try 里 —— 封面模块出任何问题都不该让音频写不出来。
    #
    # v5.9.16：顺序改成「同目录 cover.png 优先」。下载页会把图复制到
    # ``FLAC/<游戏名>/cover.png``，这样把整个 FLAC 目录拷到别的机器、
    # 或者清掉 ``%LOCALAPPDATA%\MSU1Converter\covers`` 之后，
    # 重新转码照样有封面 —— 图跟着音频走，不再依赖那台机器的缓存。
    #
    # v5.9.17：MP3 也走这条路（ID3v2 APIC）。目录里那份同样叫 ``cover.png``，
    # 两种格式共用一张图 —— 同一首曲子先转 FLAC 再转 MP3 不会各抓一次。
    _auto_cover = cover is None        # 封面是「按目录名推断」来的吗
    if fmt in ENC_FMTS and cover is None:
        _here = os.path.join(str(out_dir), COVER_NAME)
        if os.path.isfile(_here):
            cover = _here
        else:
            try:
                import cover_art
                cover = cover_art.cover_for_title(
                    os.path.basename(os.path.normpath(str(out_dir))))
            except Exception:                      # noqa: BLE001
                cover = ""
    if fmt not in ENC_FMTS:
        cover = ""                                # WAV 没有容器放图
    if cover:
        # 落副本：默认只在「一个目录 = 一个游戏」时做 —— 那种情况
        # cover 一定是推断来的（见 cover_beside 的说明）。
        _beside = _auto_cover if cover_beside is None else bool(cover_beside)
        if _beside:
            cover = _save_cover_beside(cover, out_dir)
    if cover:
        log("   🖼 封面 %s" % os.path.basename(str(cover)))

    stat = _empty_stat()
    # 编码器不可用时**整批立刻返回**，不要每首各失败一次 —— 33 首会刷 33 行
    # 同样的红字，用户以为音源坏了。这里只报一次，且失败数按真实轨数记。
    if fmt in ENC_FMTS:
        import flac_out
        _av, _why = flac_out.available(fmt)
        if not _av:
            msg = "%s 编码器不可用：%s" % (tag, _why)
            stat["fail"] = len(tracks)
            stat["errors"].append(msg)
            log("❌ " + msg)
            stat["last_idx"] = start_idx
            return stat

    used = set() if used is None else used
    total = total or (start_idx + len(tracks))
    idx = start_idx
    #: 本轮每首的自检 SNR（只有 flac/mp3/ogg 会有；静音/太短的轨不计入）
    _snrs = []

    for tr in tracks:
        if cancel is not None and cancel.is_set():
            break
        idx += 1
        name = core.safe_filename(tr["name"], f"track{idx:02d}")
        base = name
        n = 2
        while name.lower() in used:
            name = f"{base} ({n})"
            n += 1
        used.add(name.lower())
        outp = os.path.join(out_dir, f"{idx:02d}_{name}{ext}")
        try:
            raw = core.pcm_le(tr)
            raw, cut = _limit_len(raw, max_secs)
            peak = _peak_of(raw)
            if fmt in ENC_FMTS:
                # 直接从内存 PCM 写：不先落一个 WAV 再读回来，
                # 省掉一整次磁盘往返（150 秒立体声 = 26MB，33 首就是 860MB）。
                ok2, _p, detail = flac_out.encode_pcm(raw, outp, SR, 2,
                                                      cover=cover, fmt=fmt)
                if not ok2:
                    raise OSError(detail)
                # v6.0.0：把落盘自检的 SNR 收起来，最后汇总一行。
                # 以前这个数只活在返回值里、写完就扔 —— 用户看不到
                # 「自检在跑」，也就没法判断这道保险到底有没有生效。
                _m = _SNR_RE.search(detail or "")
                if _m:
                    try:
                        _snrs.append(float(_m.group(1)))
                    except ValueError:
                        pass
            else:
                with wave.open(outp, "wb") as w:
                    w.setnchannels(2)
                    w.setsampwidth(2)
                    w.setframerate(SR)
                    w.writeframes(raw)
        except Exception as e:                       # noqa: BLE001
            stat["fail"] += 1
            msg = f"{name}: 写盘失败 {e}"
            stat["errors"].append(msg)
            log("❌ " + msg)
            continue
        secs = len(raw) / BYTES_PER_SEC
        stat["ok"] += 1
        stat["seconds"] += secs
        stat["files"].append(outp)
        note = ""
        if cut:
            note = f"  ✂ 已截到 {max_secs:.0f}s"
        if peak >= 32700:
            note += "  ⚠ 有削波(源本身过载)"
        elif peak < 300:
            note += "  ⚠ 近乎静音"
        # 体积按**落盘后**的真实大小报：FLAC 的字节数与 PCM 不成正比
        # （静音/方波压得狠，噪声压不动），拿 len(raw) 估会差一倍。
        try:
            disk_mb = os.path.getsize(outp) / 1048576.0
        except OSError:
            disk_mb = len(raw) / 1048576.0
        log(f"  {tag} {idx:02d}/{total:02d}: {name}{ext}"
            f"  {int(secs // 60)}:{secs % 60:04.1f}"
            f"  {disk_mb:.1f}MB{note}")
        prog(idx, total, name, secs)      # secs = 本轨音频秒数, 供进度条加权

    # v6.0.0：自检汇总一行。只报「最低的那一首」—— 逐首报会刷屏，
    # 报平均又会把一首特别差的掩盖掉；最低值才是「最接近失守」的那次。
    if _snrs:
        log("   🛡 %s 落盘自检 %d 首通过，SNR 最低 %.1f dB（门槛 %.0f dB）"
            % (tag, len(_snrs), min(_snrs),
               __import__("flac_out").VERIFY_MIN_SNR_BY_FMT.get(fmt, 8.0)))
    stat["last_idx"] = idx
    return stat


def convert(spc_paths, out_dir, policy="id666", on_log=None, on_progress=None,
            cancel=None, speed=1.0, max_secs=0.0, used=None, start_idx=0,
            total=None, on_stage=None, chip_secs=0.0, loader=None, fmt="wav"):
    """把音源文件批量转成 WAV / FLAC (分批解码, 每批用完立即删临时 PCM)。

    spc_paths  已展开的文件绝对路径列表
    out_dir    输出目录
    policy     "id666" / "id666_x2" / "natural"；**"chip" = 多格式芯片音源**
    speed      播放设备时钟补偿倍数(1.0=不补偿)。设备把 44.1k 按更低时钟送出
               导致变慢变低时, 用 speed 预加速, 设备慢放后正好还原。
    max_secs   单曲最长时长(秒), 0 = 不限制。44.1kHz 下体积与时长严格成正比,
               这是唯一能减小体积的开关。
    used       跨音源文件名去重集合 (多个文件夹输出到同一目录时传入同一个 set)
    start_idx  起始序号 (多个音源平铺到同一目录时保持 01/02/03 连续)
    total       日志分母 (多音源时传全局总数, 保证 "05/92" 这类显示正确)
    on_stage    阶段回调 (batch_index, batch_total, batch_size): 每批解码开始
                时调用, 让界面在"解码中"这段时间也有明确提示。
    chip_secs  「默认单遍」秒数(仅 policy=="chip" 生效, 0 = 用 150 默认)。
               对应 UI-5 的 Spinbox; NSF/HES 等无时长标注的格式按它截取。
    loader     自定义加载器(仅 policy=="chip" 用)。默认 chip_decode.load_chip_paths;
               传入它是为了**测试可注入**且避免 spc_wav 直接依赖 chip_decode。
    fmt        ``"wav"``（默认）/ ``"flac"`` / ``"mp3"``。只换容器（MP3 除外，
               有损），音频规格不变，见 write_tracks。

    返回 {"ok":N, "fail":M, "files":[路径...], "seconds":S, "errors":[文本...],
          "last_idx": 最后一个序号}
    """
    log = on_log or (lambda _t: None)
    prog = on_progress or (lambda *_a: None)
    stage = on_stage or (lambda *_a: None)
    os.makedirs(out_dir, exist_ok=True)
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(2.0, speed))

    is_chip = (policy == "chip")
    if is_chip:
        # chip 走"按音频秒数切 run"的批次(设计 §5.4), 与 SPC 的"按文件数"
        # 完全不同 —— 256 轨 HES 按文件数切会爆临时盘。批次切分在
        # chip_decode.load_chip_paths 内部完成, 这里一次调用即可。
        import chip_decode
        load = loader or chip_decode.load_chip_paths
        err_prefix = "芯片音源解码失败"
        total = total or len(spc_paths)
        stat = _empty_stat()
        used = set() if used is None else used
        try:
            chip_secs_f = float(chip_secs or 0)
        except (TypeError, ValueError):
            chip_secs_f = 0.0
        stage(1, 1, len(spc_paths))
        try:
            tracks = load(list(spc_paths), default_secs=chip_secs_f,
                          cap_secs=600.0, speed=speed, cancel=cancel,
                          on_progress=prog)
        except chip_decode.ChipCancelled:
            return stat
        except Exception as e:                           # noqa: BLE001
            stat["fail"] += len(spc_paths)
            msg = f"{err_prefix}: {e}"
            stat["errors"].append(msg)
            log("❌ " + msg)
            return stat
        try:
            sub = write_tracks(tracks, out_dir, max_secs, log, prog, cancel,
                               used, start_idx, total, fmt=fmt)
            _merge(stat, sub)
            stat["last_idx"] = start_idx + sub["last_idx"]
        finally:
            import msu1_to_cd as _core
            _core.cleanup_spc_temp(tracks)
        return stat

    total = total or len(spc_paths)
    stat = _empty_stat()
    used = set() if used is None else used
    idx = start_idx
    nbatch = max(1, (len(spc_paths) + BATCH - 1) // BATCH)

    for bi, b0 in enumerate(range(0, len(spc_paths), BATCH), 1):
        if cancel is not None and cancel.is_set():
            break
        batch = spc_paths[b0:b0 + BATCH]
        stage(bi, nbatch, len(batch))
        try:
            tracks = core.load_spc_paths(batch, batch_size=len(batch),
                                         length_policy=policy, speed=speed)
        except Exception as e:                       # noqa: BLE001
            stat["fail"] += len(batch)
            msg = f"解码失败: {e}"
            stat["errors"].append(msg)
            log("❌ " + msg)
            continue
        try:
            sub = write_tracks(tracks, out_dir, max_secs, log, prog, cancel,
                               used, idx, total, fmt=fmt)
            _merge(stat, sub)
            idx += len(tracks)
        finally:
            # 本批解码产物已全部写成 WAV, 立即删掉临时 PCM
            core.cleanup_spc_temp(tracks)

    stat["last_idx"] = idx
    return stat
