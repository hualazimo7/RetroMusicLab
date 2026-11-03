# -*- coding: utf-8 -*-
"""v6.0.6 临时审计：全库扫一遍「衍生 WAV 被当成独立音源」的历史遗留。

判据：某个输出目录的记账里，存在 ``xxx.fsb.wav`` / ``xxx.txtp.wav`` 这类
**由 WAV 转换产生**、却又被当独立源转了一道的记录。这类记录的产物就是
「同一首歌转了两遍」的那一份。

只读，不动任何文件。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dl_convert as dc                                        # noqa: E402

JOSHW = r"D:\MSU1Downloads\joshw"


def main():
    hits = []
    total_man = 0
    for dirpath, _dirnames, filenames in os.walk(JOSHW):
        if ".msu1-convert.json" not in filenames:
            continue
        total_man += 1
        # ConvertManifest 收的是 **json 文件路径**，不是目录（load() 直接 open）
        man = dc.ConvertManifest(os.path.join(dirpath, ".msu1-convert.json"))
        try:
            man.load()                      # ← 构造不会自动读盘，必须显式 load
            keys = man.derived_src_keys()
        except Exception as e:                                 # noqa: BLE001
            print("!! 读不动 %s: %s" % (dirpath, e))
            continue
        if not keys:
            continue
        # 这些衍生源的产物文件还在不在
        alive = 0
        names = []
        recs = dict(man.all_records())
        for k in keys:
            for w in (recs.get(k) or {}).get("wavs") or ():
                p = os.path.join(dirpath, w)
                if os.path.isfile(p):
                    alive += 1
                    names.append(w)
        hits.append((dirpath, len(keys), alive, names[:2]))

    print("扫描 %s" % JOSHW)
    print("有记账的目录：%d 个" % total_man)
    print("受影响的目录：%d 个" % len(hits))
    print()
    for d, nk, alive, sample in hits:
        rel = d[len(JOSHW) + 1:]
        print("%s" % rel)
        print("   衍生源 %d 条，产物还在 %d 个" % (nk, alive))
        for s in sample:
            print("     %s" % s)
        print()
    if not hits:
        print("干净：没有发现重复产物。")


if __name__ == "__main__":
    main()
