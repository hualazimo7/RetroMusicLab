# -*- coding: utf-8 -*-
"""审计脚本（v6.0.12 保留）：直接调**真** ``cover_art.pick`` 验「数字序列」判据的效果。

``更新说明-v6.0.12.md`` 第 4 节那张表（错配 30 → 0、误伤 0）就是它跑出来的。
和 ``_probe_minratio`` / ``_probe_numrule`` 的区别：那两个是把判据抄一份出来跑
（为了多阈值复用），这个跑的是线上真正会执行的那段代码。

⚠ 一次性分析用，不进门禁。依赖本机 ``D:\\MSU1Downloads\\Artworks\\_idx``
的索引缓存，换机器要改 ``IDX``。

和探针一/二的区别：那两个是把判据抄一份出来跑（为了多阈值复用），
这个跑的是线上真正会执行的那段代码 —— 加了 ``num_seq`` 之后
「错配是不是真没了、对的有没有被误伤」。
"""
import io
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import cover_art as ca                                      # noqa: E402

KEY = (sys.argv[1] if len(sys.argv) > 1 else "nsf")
IDX = os.path.join(r"D:\MSU1Downloads\Artworks\_idx", KEY + ".json")

with io.open(IDX, encoding="utf-8") as fh:
    blob = json.load(fh)
items = [{"dir": p[0], "name": p[1]} for p in blob["items"]]
print("机种 %s：库 %d 条" % (KEY, len(items)), flush=True)

normed = [(it["name"], ca.norm_title(it["name"])) for it in items]
names = sorted({n for _f, n in normed if n})
base = [n for n in names if not ca.num_seq(n)]
print("正传名（不带数字）%d" % len(base), flush=True)

random.seed(42)
sample = random.sample(base, 150)

t0 = time.time()
hit, miss, wrong = 0, 0, []
for i, x in enumerate(sample):
    # 模拟「库里没有这个游戏」：把 norm == x 的项拿掉
    pool = [{"dir": d, "name": f} for (d, _n2), (f, n) in zip(blob["items"], normed) if n != x]
    got = ca.pick(pool, x)
    if not got:
        miss += 1
        continue
    hit += 1
    gn = ca.norm_title(got)
    if ca.num_seq(gn) != ca.num_seq(x):
        wrong.append((x, gn))
    if i % 30 == 29:
        print("  ... %d/%d  %.0fs" % (i + 1, len(sample), time.time() - t0), flush=True)

print("\n耗时 %.1fs" % (time.time() - t0))
print("命中 %d / 无封面 %d / 其中数字序列不一致 %d" % (hit, miss, len(wrong)))
for x, g in wrong[:10]:
    print("   !! %s -> %s" % (x, g))
