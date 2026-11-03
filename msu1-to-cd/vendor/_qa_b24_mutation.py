# -*- coding: utf-8 -*-
"""QA 独立验收 · B24 变异测试（判重 any-exists）

为什么要自己写: 实现者的 N 组测试只验证「当前实现」的行为, 无法证明
「另外两种错写法会被夹住」。本脚本做**变异测试(mutation testing)**:
把dl_convert.ConvertManifest.already_done 分别替换成三种写法, 各自跑
同一组判重场景, 只有 any-exists 写法能全过。三种写法:

  A. any-exists  (正确, 设计 §4.3) : 遍历全部 wavs, 有一个在就 True
  B. wavs[:1]    (草稿残留, B24)   : 只探第一个 -> 删第 1 首误判需重转
  C. all-exists  (反向错)          : all([])==True -> 删光全部也永不重试

期望(PRD AC-C15 / 设计 §4.3):
  - 删光某源的全部 WAV -> 必须重转该源 (只重转它, 不牵连其他源)
  - 删任意一首(第1首/中间首/最后首) -> 不重转

用法: C:/Python314/python.exe vendor/_qa_b24_mutation.py
"""
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dl_convert as dc  # noqa: E402

MANIFEST_NAME = ".msu1-convert.json"

FAILS = []
CHECKS = [0]


def check(cond, msg):
    CHECKS[0] += 1
    if not cond:
        FAILS.append(msg)
        print("   FAIL  %s" % msg)
    return bool(cond)


# --------------------------------------------------------------------------
# 三种 already_done 变体（签名与真实实现一致：self, src -> bool）
# 前置条件（指纹校验）由 _precheck 复用真实实现的逻辑，保证公平对比。
# --------------------------------------------------------------------------

def _fingerprint_ok(self, src):
    """复用真实实现的校验①（mtime/size）与前置的 wavs 非空判断。"""
    try:
        st = os.stat(src)
    except (OSError, TypeError, ValueError):
        return None
    rec = self._data["entries"].get(os.path.normpath(str(src)).lower())
    if not isinstance(rec, dict):
        return None
    try:
        if int(rec.get("size", -1)) != int(st.st_size):
            return None
        if abs(float(rec.get("mtime", -1.0)) - float(st.st_mtime)) > 0.001:
            return None
    except (TypeError, ValueError):
        return None
    wavs = rec.get("wavs") or []
    if not isinstance(wavs, (list, tuple)) or not wavs:
        return None
    base = os.path.dirname(self.path) or os.curdir
    return [(name, os.path.join(base, str(name))) for name in wavs]


def v_any_exists(self, src):
    """A · 正确写法：全量探, 任一存在即 True。"""
    pairs = _fingerprint_ok(self, src)
    if pairs is None:
        return False
    for _name, p in pairs:
        try:
            if os.path.isfile(p):
                return True
        except (TypeError, ValueError):
            continue
    return False


def v_wavs_first(self, src):
    """B · 草稿残留 wavs[:1]：只探第一个。"""
    pairs = _fingerprint_ok(self, src)
    if pairs is None:
        return False
    try:
        return os.path.isfile(pairs[0][1])
    except (TypeError, ValueError):
        return False


def v_all_exists(self, src):
    """C · 反向错：全部存在才算转过。all([])==True 是经典陷阱。"""
    pairs = _fingerprint_ok(self, src)
    if pairs is None:
        return False
    ok = True
    for _name, p in pairs:
        try:
            if not os.path.isfile(p):
                ok = False
        except (TypeError, ValueError):
            ok = False
    return ok


VARIANTS = [
    ("A any-exists (设计§4.3/正确)", v_any_exists),
    ("B wavs[:1]   (草稿残留/B24)", v_wavs_first),
    ("C all-exists (反向错)", v_all_exists),
]


# --------------------------------------------------------------------------
def build(tmp, n_src=3, wavs_per_src=3):
    """造 n_src 个源文件, 每个记账 n_src*wavs_per_src 个真实存在的 WAV。"""
    wav_dir = os.path.join(tmp, "WAV")
    src_dir = os.path.join(tmp, "src")
    os.makedirs(wav_dir, exist_ok=True)
    os.makedirs(src_dir, exist_ok=True)

    srcs = []
    for i in range(n_src):
        p = os.path.join(src_dir, "src%d.spc" % i)
        with open(p, "wb") as f:
            f.write(b"SPC" + bytes([i]) * 512)
        srcs.append(p)

    mf = dc.ConvertManifest(os.path.join(wav_dir, MANIFEST_NAME))
    mf.load()
    counter = [0]
    for i, p in enumerate(srcs):
        names = []
        for j in range(wavs_per_src):
            counter[0] += 1
            nm = "%02d_T%02d.wav" % (counter[0], j + 1)
            with open(os.path.join(wav_dir, nm), "wb") as f:
                f.write(b"RIFF" + bytes([i + 1]) * 1024)
            names.append(nm)
        mf.record(p, names, "id666")

    return wav_dir, src_dir, srcs, mf


def entry_wavs(mf, src):
    rec = mf.entry(src) or {}
    return list(rec.get("wavs") or [])


def run_scenario(label, fn, tmp):
    """跑 6 个判重场景。返回 (通过数, 失败描述列表)。"""
    global FAILS, CHECKS
    FAILS = []
    CHECKS[0] = 0
    print("\n--- 变体 %s ---" % label)
    wav_dir, _src_dir, srcs, mf = build(tmp)

    real = dc.ConvertManifest.already_done
    dc.ConvertManifest.already_done = fn
    try:
        # 场景 1: 全都在 -> 必须跳过
        check(fn(mf, srcs[0]) is True, "场景1 全在 -> 应判已转过")

        # 场景 2(N1-a): 删第 1 首 -> 不该重转
        w0 = entry_wavs(mf, srcs[0])
        os.remove(os.path.join(wav_dir, w0[0]))
        check(fn(mf, srcs[0]) is True,
              "场景2 N1-a 删第1首(%s) -> 不应重转" % w0[0])

        # 场景 3(N1-b): 删中间首 -> 不该重转
        wav_dir, _src_dir, srcs, mf = build(tmp)
        w1 = entry_wavs(mf, srcs[1])
        os.remove(os.path.join(wav_dir, w1[1]))
        check(fn(mf, srcs[1]) is True,
              "场景3 N1-b 删中间首(%s) -> 不应重转" % w1[1])

        # 场景 4(N1-c): 删最后首 -> 不该重转
        wav_dir, _src_dir, srcs, mf = build(tmp)
        w2 = entry_wavs(mf, srcs[2])
        os.remove(os.path.join(wav_dir, w2[-1]))
        check(fn(mf, srcs[2]) is True,
              "场景4 N1-c 删最后首(%s) -> 不应重转" % w2[-1])

        # 场景 5(N2): 删光某源全部 WAV -> 必须重转（且只重转它）
        wav_dir, _src_dir, srcs, mf = build(tmp)
        w3 = entry_wavs(mf, srcs[0])
        for nm in w3:
            os.remove(os.path.join(wav_dir, nm))
        got = fn(mf, srcs[0])
        check(got is False, "场景5 N2 删光该源全部 %d 首 -> 必须重转" % len(w3))
        check(fn(mf, srcs[1]) is True,
              "场景5b 删源0不得牵连源1 -> 源1仍应判已转过")
        check(fn(mf, srcs[2]) is True,
              "场景5c 删源0不得牵连源2 -> 源2仍应判已转过")

        # 场景 6: 无记账条目 -> 必须重转
        wav_dir, _src_dir, srcs, mf = build(tmp)
        fresh = os.path.join(_src_dir, "fresh.spc")
        with open(fresh, "wb") as f:
            f.write(b"SPCfresh" + b"\0" * 256)
        check(fn(mf, fresh) is False, "场景6 无记账 -> 必须重转")

        # 场景 7: 源被改写(mtime/size 变)-> 必须重转
        wav_dir, _src_dir, srcs, mf = build(tmp)
        time.sleep(1.05)
        with open(srcs[0], "ab") as f:
            f.write(b"\0" * 64)
        check(fn(mf, srcs[0]) is False,
              "场景7 源被改写(size 变) -> 必须重转")
    finally:
        dc.ConvertManifest.already_done = real

    return CHECKS[0] - len(FAILS), CHECKS[0], list(FAILS)


def main():
    print("=" * 74)
    print("QA 独立验收 · B24 变异测试  pid=%d" % os.getpid())
    print("=" * 74)

    # 0) 先确认线上真实实现就是 any-exists
    tmp0 = tempfile.mkdtemp(prefix="qa_b24_real_%d " % os.getpid())
    try:
        wav_dir, _sd, srcs, mf = build(tmp0)
        real_fn = dc.ConvertManifest.already_done
        check_kind = real_fn.__name__ if hasattr(real_fn, "__name__") else "?"
        print("\n[线上真实实现] %s" % check_kind)
        w = entry_wavs(mf, srcs[0])
        os.remove(os.path.join(wav_dir, w[0]))
        r1 = real_fn(mf, srcs[0])
        print("[线上真实实现] 删第1首 -> %s (期望 True=不重转)" % r1)
        for nm in w[1:]:
            os.remove(os.path.join(wav_dir, nm))
        r2 = real_fn(mf, srcs[0])
        print("[线上真实实现] 再删光剩余 -> %s (期望 False=重转)" % r2)
        OK_REAL = (r1 is True and r2 is False)
    finally:
        shutil.rmtree(tmp0, ignore_errors=True)

    # 1) 三变体对比
    rows = []
    for label, fn in VARIANTS:
        tmp = tempfile.mkdtemp(prefix="qa_b24_%d " % os.getpid())
        try:
            p, n, fails = run_scenario(label, fn, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        rows.append((label, p, n, fails))

    print("\n" + "=" * 74)
    print("变体对比矩阵")
    print("=" * 74)
    print("  %-30s %-12s %s" % ("变体", "结果", "失败场景"))
    for label, p, n, fails in rows:
        tag = "PASS %d/%d" % (p, n) if p == n else "FAIL %d/%d" % (p, n)
        print("  %-30s %-12s %s" % (label, tag, "; ".join(fails) or "-"))

    # 2) 判定：正确的变体必须唯一
    winners = [lb for lb, p, n, _ in rows if p == n]
    print("")
    print("  通过变体数: %d -> %s" % (len(winners), winners))

    verdict_ok = (len(winners) == 1 and "any-exists" in winners[0] and OK_REAL)

    print("")
    print("=" * 74)
    if verdict_ok:
        print("B24 结论: PASS —— 线上实现是 any-exists, 且三种写法中只有它")
        print("         能同时夹住 N1(删任意一首不重转)与 N2(删光全部必重转)。")
    else:
        print("B24 结论: FAIL —— 线上实现=%s, 唯一通过变体=%s"
              % ("any-exists 行为正确" if OK_REAL else "行为不符 any-exists", winners))
    print("=" * 74)
    return 0 if verdict_ok else 1


if __name__ == "__main__":
    sys.exit(main())
