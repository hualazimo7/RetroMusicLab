# -*- coding: utf-8 -*-
"""抽样实测：Zophar 的 GG/SMS 包里，libgme 到底能解多少。

要回答一个问题：v5.7 接入的这两个机种，是「个别包解不了」还是「大面积解不了」。
判据 = libgme 渲染第一轨前 ~5 秒的峰值；0 = 静音 = 解不了。

    py -3.14 vendor/_probe_zophar_kss.py z_gg
    py -3.14 vendor/_probe_zophar_kss.py z_sms
"""
import ctypes
import os
import random
import sys
import urllib.request
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import msu1_to_cd as core                      # noqa: E402
import zophar_source as zs                     # noqa: E402

WORK = os.path.join(core.temp_root() if hasattr(core, 'temp_root') else r'D:\MSU1Temp',
                    'zopsample')
os.makedirs(WORK, exist_ok=True)
SR = 48000
BUF = (ctypes.c_short * (4096 * 2))()
GME = core._load_gme()


def peak_of(path, secs=5.0):
    """渲染前 secs 秒，返回 (峰值, 说明)。打不开返回 -1。"""
    err, emu, _hold = core.gme_open(GME, path, SR)
    if err:
        return -1, 'open: %s' % err
    peak = 0
    e = GME.gme_start_track(emu, 0)
    if e:
        return -1, 'start: %s' % e
    n = int(SR * secs / 4096)
    for _ in range(n):
        if GME.gme_play(emu, 4096 * 2, BUF):
            break
        peak = max(peak, max(abs(x) for x in BUF))
    return peak, ''


def head_of(path):
    with open(path, 'rb') as f:
        return f.read(16)


def kind_of(path):
    h = head_of(path)
    if h[:4] == b'KSCC':
        load = h[4] | (h[5] << 8)
        ln = h[6] | (h[7] << 8)
        return 'KSCC load=0x%04X len=0x%04X bank=0x%02X' % (load, ln, h[10])
    if h[:4] == b'SGC\x1a':
        return 'SGC'
    if h[:4] == b'KSSX':
        return 'KSSX'
    return h[:4].hex()


def get_entries(key, want=12):
    got = zs.load_cache(key)
    if got:
        entries = got[0]
        print('缓存命中 %s: %d 条' % (key, len(entries)), flush=True)
    else:
        print('扫站 %s ...' % key, flush=True)
        entries = zs.scan_platform(key)
        print('扫到 %d 条' % len(entries), flush=True)
    if len(entries) <= want:
        return entries
    random.seed(7)
    step = len(entries) / float(want)          # 均匀铺开，别全挤在字母 A
    return [entries[int(i * step)] for i in range(want)]


def run(key):
    entries = get_entries(key)
    print('抽样 %d 个' % len(entries), flush=True)
    res = zs.resolve_many([e['url'] for e in entries])
    rows = []
    for i, e in enumerate(entries, 1):
        name = e['name']
        r = res.get(e['url']) or {}
        if not r.get('ok'):
            rows.append((name, '-', '-', -1, 'resolve 失败: %s' % r.get('error')))
            print('[%2d/%d] %-34s resolve 失败' % (i, len(entries), name[:34]), flush=True)
            continue
        zp = os.path.join(WORK, 'pkg%02d.zip' % i)
        try:
            req = urllib.request.Request(r['url'], headers={'User-Agent': zs.USER_AGENT})
            with urllib.request.urlopen(req, timeout=180) as resp, open(zp, 'wb') as f:
                f.write(resp.read())
            d = os.path.join(WORK, 'pkg%02d' % i)
            os.makedirs(d, exist_ok=True)
            with zipfile.ZipFile(zp) as z:
                z.extractall(d)
        except Exception as exc:                    # noqa: BLE001
            rows.append((name, '-', '-', -1, '下载/解压失败: %s' % exc))
            print('[%2d/%d] %-34s 下载失败 %s' % (i, len(entries), name[:34], exc), flush=True)
            continue
        mus = []
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(('.kss', '.sgc')):
                    mus.append(os.path.join(root, fn))
        if not mus:
            rows.append((name, '-', '-', -1, '包里没有 kss/sgc'))
            continue
        for mp in mus:
            pk, why = peak_of(mp)
            rows.append((name, os.path.basename(mp), kind_of(mp), pk, why))
            print('[%2d/%d] %-28s %-12s %-30s peak=%s %s'
                  % (i, len(entries), name[:28], os.path.basename(mp)[:12],
                     kind_of(mp), pk, why), flush=True)

    ok = sum(1 for r in rows if isinstance(r[3], int) and r[3] > 0)
    zero = sum(1 for r in rows if r[3] == 0)
    bad = sum(1 for r in rows if r[3] == -1)
    print('\n===== %s 汇总 =====' % key, flush=True)
    print('文件总数 %d / 有声 %d / 静音 %d / 打不开 %d' % (len(rows), ok, zero, bad),
          flush=True)
    print('\n%-32s %-14s %-32s %s' % ('游戏', '文件', '头', '峰值'))
    for r in rows:
        print('%-32s %-14s %-32s %5s %s'
              % (r[0][:32], str(r[1])[:14], str(r[2])[:32], r[3], r[4]))


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else 'z_gg')
