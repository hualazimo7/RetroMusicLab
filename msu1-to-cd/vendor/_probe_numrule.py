# -*- coding: utf-8 -*-
"""审计脚本（v6.0.12 保留）：验「数字序列必须一致」这条判据，能拦掉多少错、误伤多少对。

⚠ 一次性分析用，不进门禁。依赖本机 ``D:\\MSU1Downloads\\Artworks\\_idx``
的索引缓存，换机器要改 ``IDX``。

背景：探针一发现「合理匹配」里约一半其实也是错的（6合1→9合1、SMB3→SMB2），
而且这些错的和真对的（空格 / & / 罗马字写法差异）**混在同一个相似度区间**，
所以单纯调 MIN_RATIO 分不开。这里验「提取数字序列再比较」这条规则。
"""
import io
import json
import os
import random
import re
import sys
import time
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import cover_art as ca                                      # noqa: E402

KEY = (sys.argv[1] if len(sys.argv) > 1 else "nsf")
IDX = os.path.join(r"D:\MSU1Downloads\Artworks\_idx", KEY + ".json")

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
         "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
TOK = re.compile(r"\b(\d+|xii|xi|ix|viii|vii|vi|iv|iii|ii|v|x)\b")


def num_seq(s):
    """名字里的数字序列（阿拉伯数字 + 独立成词的罗马数字，统一成 int）。"""
    out = []
    for m in TOK.finditer(s):
        t = m.group(1)
        out.append(int(t) if t.isdigit() else ROMAN.get(t, 0))
    return out


with io.open(IDX, encoding="utf-8") as fh:
    blob = json.load(fh)
keyed = [(p[1], ca.norm_title(p[1])) for p in blob["items"]]
keyed = [(f, n) for f, n in keyed if n]
names = sorted({n for _f, n in keyed})
base = [n for n in names if not TOK.search(n)]
print("机种 %s：库 %d 条 / 正传名 %d" % (KEY, len(keyed), len(base)), flush=True)

random.seed(42)
sample = random.sample(base, 150)

t0 = time.time()
rows = []
for x in sample:
    pool = [(f, n) for f, n in keyed if n != x]
    if not pool:
        continue
    xs = num_seq(x)
    cands = []
    for f, n in pool:
        if abs(len(n) - len(x)) > max(6, len(x) // 2):
            continue
        s = SequenceMatcher(None, x, n).ratio()
        if s < ca.MIN_RATIO:
            continue
        cands.append((f, n, s, num_seq(n) == xs))
    if not cands:
        rows.append(("none", "", "", None, None))
        continue
    best = max(cands, key=lambda t: t[2] - ca._region_rank(t[0]) * 0.001)
    rows.append(("hit", x, best[1], best[2], best[3]))

hits = [r for r in rows if r[0] == "hit"]
print("模糊档命中 %d / %d 个样本" % (len(hits), len(rows)), flush=True)
same = [r for r in hits if r[4]]
diff = [r for r in hits if not r[4]]
print("  数字序列一致（结构判据放行）: %d" % len(same))
print("  数字序列不同（结构判据拒绝）: %d" % len(diff))
print("耗时 %.1fs" % (time.time() - t0), flush=True)

print("\n被结构判据拒绝的（应该都是不同游戏）：")
for _k, x, got, s, _f in diff[:14]:
    print("  %.3f  %-44s -> %s" % (s, x, got))

print("\n被结构判据放行的（应该都是同一游戏的不同写法）：")
for _k, x, got, s, _f in same[:14]:
    print("  %.3f  %-44s -> %s" % (s, x, got))
