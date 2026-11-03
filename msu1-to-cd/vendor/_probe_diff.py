# -*- coding: utf-8 -*-
"""审计脚本（v6.0.12 保留）：新判据到底拦掉了哪几条 —— 逐条看是不是真该拦。

手法：把 ``num_seq`` 临时换成「恒返回空列表」（等于关掉判据）跑一遍拿基线，
再正常跑一遍，两个结果做差集。

⚠ 一次性分析用，不进门禁。依赖本机 ``D:\\MSU1Downloads\\Artworks\\_idx``
的索引缓存，换机器要改 ``IDX``。
"""
import io
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import cover_art as ca                                      # noqa: E402

KEY = (sys.argv[1] if len(sys.argv) > 1 else "nsf")
IDX = os.path.join(r"D:\MSU1Downloads\Artworks\_idx", KEY + ".json")

with io.open(IDX, encoding="utf-8") as fh:
    blob = json.load(fh)
items = [{"dir": p[0], "name": p[1]} for p in blob["items"]]
normed = [ca.norm_title(it["name"]) for it in items]
names = sorted({n for n in normed if n})
base = [n for n in names if not ca.num_seq(n)]

random.seed(42)
sample = random.sample(base, 150)


def run():
    out = {}
    for x in sample:
        pool = [it for it, n in zip(items, normed) if n != x]
        out[x] = ca.pick(pool, x)
    return out


real = ca.num_seq
try:
    ca.num_seq = lambda _s: []                       # 关掉判据 = 改前行为
    before = run()
finally:
    ca.num_seq = real
after = run()

blocked = [(x, before[x]) for x in sample if before[x] and not after[x]]
changed = [(x, before[x], after[x]) for x in sample
           if before[x] and after[x] and before[x] != after[x]]
same = sum(1 for x in sample if before[x] == after[x])

print("机种 %s / 样本 %d" % (KEY, len(sample)))
print("改前有封面 %d，改后有封面 %d" % (sum(1 for v in before.values() if v),
                                        sum(1 for v in after.values() if v)))
print("结果完全没变 %d 条" % same)
print("\n被新判据拦掉的 %d 条（逐条看是否该拦）：" % len(blocked))
for x, got in blocked:
    print("  要 %-44s 原本会贴 %s" % (x, got))
print("\n换了一张的 %d 条：" % len(changed))
for x, b, a in changed:
    print("  要 %-40s\n     改前 %s\n     改后 %s" % (x, b, a))
