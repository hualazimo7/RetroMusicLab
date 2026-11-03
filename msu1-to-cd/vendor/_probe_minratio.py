# -*- coding: utf-8 -*-
"""审计脚本（v6.0.12 保留）：MIN_RATIO 调到多少合适 —— 用真实封面库跑，不靠猜。

⚠ 一次性分析用，不进门禁。依赖本机 ``D:\\MSU1Downloads\\Artworks\\_idx``
的索引缓存，换机器要改 ``IDX``。

做法：拿 nsf 的 13835 条真实封面名，抽 150 个「正传」名（本身不带尾部编号）。
对每个名字 X，**把库里 norm == X 的项全部移除**（模拟「库里没有这个游戏」），
再按 ``cover_art.pick`` 的三层判据去挑，看挑到什么。

分类（只看「模糊档」挑出来的）：
  bad    挑到的是 ``X + 尾部编号``（续作）—— 明显错图
  suspect 挑到的名字包含 X 或被 X 包含 —— 可疑，需要人看
  ok     其它（拼写差异、真副标题）

对多个阈值一次算完（SequenceMatcher 只跑一遍，各阈值复用）。
"""
import json
import io
import os
import random
import re
import sys
import time
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import cover_art as ca                                      # noqa: E402

KEY = (sys.argv[1] if len(sys.argv) > 1 else "nsf")
IDX = os.path.join(r"D:\MSU1Downloads\Artworks\_idx", KEY + ".json")
THRS = [0.86, 0.88, 0.90, 0.92, 0.94, 0.96]
NUM = re.compile(r"^(.*\S)\s+(2|3|4|5|6|7|8|9|10|ii|iii|iv|v|vi|vii|viii|ix|xi|xii)$")

with io.open(IDX, encoding="utf-8") as fh:
    blob = json.load(fh)
keyed = [(pair[1], ca.norm_title(pair[1])) for pair in blob["items"]]
keyed = [(f, n) for f, n in keyed if n]
print("库条目 %d" % len(keyed), flush=True)

names = sorted({n for _f, n in keyed})
base = [n for n in names if not NUM.match(n)]
print("正传名 %d" % len(base), flush=True)

random.seed(42)
sample = random.sample(base, 150)
print("抽样 %d" % len(sample), flush=True)


def sub_prefixed(want_raw, files_raw):
    """副标题前缀层（和 pick 里一致）。"""
    if len(want_raw) < 4:
        return ""
    hit = []
    for f, raw_f in files_raw:
        if not raw_f:
            continue
        if raw_f.startswith(want_raw + " - ") or want_raw.startswith(raw_f + " - "):
            hit.append(f)
    return hit[0] if hit else ""


def classify(x, got_norm):
    if got_norm == x:
        return "ok"
    m = NUM.match(got_norm)
    if m and m.group(1) == x:
        return "bad"
    if got_norm.startswith(x) or x.startswith(got_norm):
        return "suspect"
    return "ok"


t0 = time.time()
stat = {thr: {"bad": 0, "suspect": 0, "ok": 0, "none": 0, "exact": 0, "sub": 0}
        for thr in THRS}
samples_bad = []
samples_ok = []

for i, x in enumerate(sample):
    pool = [(f, n) for f, n in keyed if n != x]      # 模拟库里没这个游戏
    if not pool:
        continue
    raw_want = ca._raw_title(x).lower()
    files_raw = [(f, ca._raw_title(f).lower()) for f, _n in pool]
    sub = sub_prefixed(raw_want, files_raw)
    if sub:
        for thr in THRS:
            stat[thr]["sub"] += 1
        continue

    cands = []
    for f, n in pool:
        if abs(len(n) - len(x)) > max(6, len(x) // 2):
            continue
        s = SequenceMatcher(None, x, n).ratio()
        if s < min(THRS):
            continue
        cands.append((f, n, s))

    for thr in THRS:
        best = None
        best_score = 0.0
        for f, n, s in cands:
            if s < thr:
                continue
            sc = s - ca._region_rank(f) * 0.001
            if sc > best_score:
                best, best_score = n, sc
        if best is None:
            stat[thr]["none"] += 1
        else:
            k = classify(x, best)
            stat[thr][k] += 1
            if k == "bad" and thr == THRS[0] and len(samples_bad) < 12:
                samples_bad.append((x, best))
            if k == "ok" and thr == THRS[0] and len(samples_ok) < 18:
                samples_ok.append((x, best))
    if i % 30 == 29:
        print("  ... %d/%d  %.0fs" % (i + 1, len(sample), time.time() - t0), flush=True)

print("\n耗时 %.1fs\n" % (time.time() - t0), flush=True)
hdr = "阈值   续作错配(bad)  可疑(suspect)  合理(ok)  无匹配(none)"
print(hdr)
print("-" * len(hdr))
n = len(sample)
for thr in THRS:
    d = stat[thr]
    print("%.2f   %4d (%4.1f%%)  %4d (%4.1f%%)  %4d  %4d"
          % (thr, d["bad"], 100.0 * d["bad"] / n,
             d["suspect"], 100.0 * d["suspect"] / n, d["ok"], d["none"]))

print("\n0.86 档挑错的例子（前 12 条）：")
for x, got in samples_bad:
    print("  要 %-46s 挑到 %s" % (x, got))

print("\n0.86 档被判「合理」的例子（前 18 条，人工核对用）：")
for x, got in samples_ok:
    print("  要 %-46s 挑到 %s" % (x, got))
