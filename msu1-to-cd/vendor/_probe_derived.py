# -*- coding: utf-8 -*-
"""v6.0.6 临时探针：拿真实下载目录验证「衍生 WAV 去重」到底丢掉了哪些。

为什么要这个：FLAC 20 / MP3 10 的根因是源目录里 ``xxx.fsb``（真源）和
``xxx.fsb.wav``（WAV 转换的产物）**都** ``is_convertible`` 为真，转 FLAC 时
被当成两首各转一遍。这里用真实目录核对 ``drop_derived_wavs`` 的判定。
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dl_convert as dc                                        # noqa: E402

ROOTS = [
    r"D:\MSU1Downloads\joshw\PlayStation 4\Assetto Corsa (2016-08-25)(Kunos Simulazioni)(505 Games)[PS4]",
    r"D:\MSU1Downloads\joshw\PlayStation 4\Assassin's Creed IV - Freedom Cry (2014-02-18)(Ubisoft Montreal)(Ubisoft)[PS4]",
]


def main():
    for root in ROOTS:
        if not os.path.isdir(root):
            print("跳过（不存在）：%s" % root)
            continue
        # 注意：**不能用 glob**。joshw 的目录名带 ``[PS4]``，glob 会把方括号
        # 当字符集通配符，整个目录直接匹配不上（实测返回 0 个文件）。
        files = [os.path.join(root, n) for n in os.listdir(root)
                 if dc.is_convertible(os.path.join(root, n))]
        kept = dc.drop_derived_wavs(files)
        dropped = [f for f in files if dc.is_derived_wav(f)]
        print(os.path.basename(root)[:52])
        print("  可转 %d  ->  保留 %d   丢掉衍生 %d"
              % (len(files), len(kept), len(files) - len(kept)))
        for f in dropped[:3]:
            print("    丢: %s" % os.path.basename(f))
        # 保留的里面绝不能再有 .wav（真源就是 .wav 的机种除外）
        bad = [f for f in kept if dc.is_derived_wav(f)]
        print("  保留里仍被判为衍生：%d  %s" % (len(bad), "OK" if not bad else "!!"))
        print()


if __name__ == "__main__":
    main()
