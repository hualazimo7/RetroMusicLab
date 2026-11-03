#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/zip_range_fetch.py — 用 HTTP Range 从远程大 ZIP 里只取需要的几个成员。

为什么需要它（实测）：
  · GYM 素材在archive.org 上的整包是 **146 MB** 的 ZIP
    (`gym-archive-genesis-music-zophar.net-01-24-06`)。为了 4个验收样本把
    146 MB 全下下来既慢又占盘，不划算。
  · 但 archive.org 支持 Range，且 ZIP 的**中央目录（central directory）在文件尾部**，
    所以可以：先 Range 读最后若干字节 -> 解析目录 -> 只对目标成员做 Range 下载。

不依赖第三方库（PRD 非目标①），只用 stdlib zipfile + urllib 的 Range 头。
一个前提：远程必须**如实返回 Range**（archive.org 会，且返回 206）。
若服务器忽略 Range（返回 200全量），本脚本会明确报错而不是静默吃全量。

用法:
    # 列出远程 zip 的成员名（只下目录区）
    python vendor/zip_range_fetch.py --list "<zip-url>"
    # 只取匹配 --match 的前 --limit 个成员
    python vendor/zip_range_fetch.py "<zip-url>" --dest vendor/samples/gym \
        --match ".gym" --limit 4
"""

import argparse
import io
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request

UA = "MSU1Converter-sample-fetcher/1.0 (+local verification only)"
DELAY = 0.5
EOCD_SIG = b"PK\x05\x06"
EOCD64_LOC_SIG = b"PK\x06\x07"
EOCD64_SIG = b"PK\x06\x06"
CD_SIG = b"PK\x01\x02"


def _log(m):
    print(m, flush=True)


def _req(url, rng=None, timeout=90):
    """发一个 GET（可带 Range）。返回 (status, body)。**HTTPError 一律抛出**，
    因为 206/200 的区别是本脚本的正确性前提，不能静默吞掉。

    带重试：本机走代理访问 archive.org 时会偶发
    `Tunnel connection failed: 502 Bad Gateway`（实测两次），
    属于瞬时故障，重试即可，不必让整个抓取脚本失败。
    """
    headers = {"User-Agent": UA}
    if rng:
        headers["Range"] = f"bytes={rng[0]}-{rng[1]}"
    last = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError:
            raise                      # 4xx/5xx 是真问题，不重试
        except Exception as e:         # noqa: BLE001  网络层瞬时故障才重试
            last = e
            _log(f"    重试 {attempt}/3 ({type(e).__name__}: {str(e)[:60]})")
            time.sleep(DELAY * 2 * attempt)
    raise RuntimeError(f"重试 3 次仍失败: {last}")


def head_size(url):
    """探测总长。返回 total_size。

    硬性要求服务器返回 206（真的支持 Range）—— 若返回 200 全量，
    说明按需下载的前提不成立，必须报错而不是静默吃下整个文件。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status != 206:
            raise RuntimeError(f"服务器忽略 Range（返回 {r.status}），无法按需下载")
        cr = r.headers.get("Content-Range", "")
    m = re.match(r"bytes 0-0/(\d+)", cr)
    if not m:
        raise RuntimeError(f"Content-Range 无法解析: {cr!r}")
    return int(m.group(1))


def read_tail(url, total, want):
    """读最后 want 字节。"""
    start = max(0, total - want)
    st, body = _req(url, (start, total - 1))
    if st != 206:
        raise RuntimeError(f"尾部 Range 请求返回 {st}")
    return body


def find_cd_offset(tail, total):
    """在尾部字节里定位中央目录偏移。

    优先 EOCD64（ZIP64，>4GB 或条目多时用），回退 EOCD32。
    返回 (cd_offset, cd_size)。
    """
    i = tail.rfind(EOCD64_SIG)
    if i >= 0:
        # EOCD64: 偏移 40 处为 8 字节 cd_offset,48 为 8 字节 cd_size
        cd_size, cd_off = struct.unpack_from("<QQ", tail, i + 40)
        if cd_off and cd_size:
            return int(cd_off), int(cd_size)
    j = tail.rfind(EOCD_SIG)
    if j >= 0:
        # EOCD32: 12=cd_size, 16=cd_offset
        cd_size, cd_off = struct.unpack_from("<II", tail, j + 12)
        if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
            raise RuntimeError("ZIP64 EOCD64 未找到，无法定位中央目录")
        return int(cd_off), int(cd_size)
    raise RuntimeError("尾部未找到 EOCD 记录")


def parse_cd(blob):
    """解析中央目录字节流 -> [{name, csize, usize, local_off, method}]。"""
    out, p = [], 0
    n = len(blob)
    while p + 46 <= n and blob[p:p + 4] == CD_SIG:
        method = struct.unpack_from("<H", blob, p + 10)[0]
        csize, usize = struct.unpack_from("<II", blob, p + 20)
        nlen, elen, clen = struct.unpack_from("<HHH", blob, p + 28)
        off = struct.unpack_from("<I", blob, p + 42)[0]
        name = blob[p + 46:p + 46 + nlen].decode("utf-8", "replace")
        extra = blob[p + 46 + nlen:p + 46 + nlen + elen]
        # ZIP64 extra field(0x0001) 里可能藏着真实的 csize/usize/off
        if 0xFFFFFFFF in (csize, usize, off):
            vals = _zip64_extra(extra)
            it = iter(vals)
            if usize == 0xFFFFFFFF:
                usize = next(it)
            if csize == 0xFFFFFFFF:
                csize = next(it)
            if off == 0xFFFFFFFF:
                off = next(it)
        out.append({"name": name, "csize": csize, "usize": usize,
                    "local_off": off, "method": method})
        p += 46 + nlen + elen + clen
    return out


def _zip64_extra(extra):
    """从 extra field 里抽出 ZIP64 记录的数值列表。"""
    vals, p = [], 0
    while p + 4 <= len(extra):
        hid, hsz = struct.unpack_from("<HH", extra, p)
        body = extra[p + 4:p + 4 + hsz]
        if hid == 0x0001:
            for i in range(0, len(body) - 7, 8):
                vals.append(struct.unpack_from("<Q", body, i)[0])
            break
        p += 4 + hsz
    return vals


def fetch_member(url, total, ent):
    """只下载一个成员的原始（可能是压缩的）字节。"""
    # 本地文件头 30 字节：文件名/extra 长度可变，必须先读头再算数据起点
    st, lh = _req(url, (ent["local_off"], min(total - 1, ent["local_off"] + 29)))
    if st != 206 or lh[:4] != b"PK\x03\x04":
        raise RuntimeError(f"{ent['name']}: 本地头异常 {lh[:4]!r}")
    nlen, elen = struct.unpack_from("<HH", lh, 26)
    start = ent["local_off"] + 30 + nlen + elen
    end = start + ent["csize"] - 1
    st, raw = _req(url, (start, end), timeout=180)
    if st != 206:
        raise RuntimeError(f"{ent['name']}: 数据段 Range 返回 {st}")
    if len(raw) != ent["csize"]:
        raise RuntimeError(f"{ent['name']}: 取回 {len(raw)} != csize {ent['csize']}")
    return raw


def materialize(raw, method, name):
    """把原始成员字节解成最终文件字节（deflate 才解）。"""
    if method == 0:
        return raw
    if method == 8:
        import zlib
        return zlib.decompress(raw, -15)
    raise RuntimeError(f"{name}: 不支持的压缩方法 {method}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--dest", default="")
    ap.add_argument("--match", default="", help="成员名子串过滤（区分大小写）")
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--list", action="store_true", help="只打印成员名")
    ap.add_argument("--sort", choices=("name", "size-asc", "size-desc"),
                    default="name", help="挑选顺序（默认按名字）")
    ap.add_argument("--cd-bytes", type=int, default=1 << 20,
                    help="从尾部读多少字节用于定位中央目录")
    args = ap.parse_args()

    total = head_size(args.url)
    _log(f"远程 ZIP 总大小 {total:,} B（{total / 1048576:.1f} MB）")

    tail = read_tail(args.url, total, min(args.cd_bytes, total))
    cd_off, cd_size = find_cd_offset(tail, total)
    _log(f"中央目录 offset={cd_off:,} size={cd_size:,}")

    # 中央目录可能不在读到的尾部范围内（罕见），不够就扩读
    cd_start = cd_off
    if cd_start < total - len(tail):
        need = cd_off + cd_size - (total - len(tail))
        extra = read_tail(args.url, total, len(tail) + need + 16)
        cd_blob = extra[len(extra) - (need + cd_size):]
    else:
        cd_blob = tail[len(tail) - (total - cd_off - cd_size + cd_size):] \
            if cd_off + cd_size <= total else tail
        cd_blob = tail[cd_off - (total - len(tail)):]
    entries = parse_cd(cd_blob)
    _log(f"解析到 {len(entries)} 个成员")

    if args.list:
        for e in entries:
            _log(f"  {e['usize']:>10,}  {e['name']}")
        return 0

    if not args.dest:
        _log("--dest 必填（除 --list 外）")
        return 2
    os.makedirs(args.dest, exist_ok=True)

    picked = [e for e in entries
              if args.match.lower() in e["name"].lower()
              and not e["name"].endswith("/")]
    # GYM 站整包里每个游戏是一个独立 .rar，按体积升序取最小的几个 ——
    # 验收只需要"能解出 .gym 且 libgme 能出声"，挑小包最省时间。
    if args.sort == "size-asc":
        picked.sort(key=lambda e: e["usize"])
    elif args.sort == "size-desc":
        picked.sort(key=lambda e: -e["usize"])
    _log(f"匹配 '{args.match}' 的成员 {len(picked)} 个，取前 {args.limit}"
         f"（排序 {args.sort}）")
    n = 0
    for e in picked[:args.limit]:
        try:
            raw = fetch_member(args.url, total, e)
            data = materialize(raw, e["method"], e["name"])
            base = os.path.basename(e["name"].replace("\\", "/")) or "unnamed"
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
            dst = os.path.join(args.dest, safe)
            with open(dst, "wb") as f:
                f.write(data)
            _log(f"  OK {safe}  {len(data):,} B  (magic {data[:4].hex(' ')})")
            n += 1
        except Exception as e:                           # noqa: BLE001
            _log(f"  FAIL {e}")
        time.sleep(DELAY)
    _log(f"\n共取得 {n} 个文件 -> {args.dest}")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())