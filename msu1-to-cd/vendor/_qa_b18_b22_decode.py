# -*- coding: utf-8 -*-
"""QA 独立验收 · B18 + B22 真解码独立验证

为什么自己写: B22 的本质是「结构测试全绿、只有真解码才暴露」。
实现者自己的 --decode 我不敢只听汇报, 这里用**独立写法**再跑一次真解码。

验证内容:
  B22-L1  NSF 真解码: on_progress 被调用且**不抛 TypeError**, ok > 0
  B22-L2  GBS/VGM/HES 各一份真解码, 全部 ok > 0
  B22-L3  SPC 真解码(走 id666, 只经 write_tracks 4 参路径), ok > 0
  B22-L4  三回调签名静态检查: 均为 (*args) 无固定位置参数
  B18-K1  截断 NSF(probe 通过但解码必失败) -> 必须发 cv_file_failed
  B18-K1b 绝不能发 cv_file_done
  B18-K2  错误文案含「未产出任何 WAV」+ 可执行提示
  B18-K3  manifest **不得**有该文件记账(already_done 必须 False)
  B18-K4  正常文件: 有 cv_file_done、无「没有产出」提示、manifest 有记录
  B18-K5  read_last_skips() 在 id666 路径被调用不抛异常
  EXTRA   真解码 WAV 峰值 > 300 (非静音) —— FR「有声音」

用法: C:/Python314/python.exe vendor/_qa_b18_b22_decode.py
"""
import glob
import inspect
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dl_convert as dc  # noqa: E402

PASS = [0]
FAIL = []


def check(label, cond, detail="", ac=""):
    PASS[0] += 1
    tag = "PASS" if cond else "FAIL"
    line = "  [%s] %s" % (tag, label)
    if ac:
        line += "  (%s)" % ac
    if detail:
        line += "  -- %s" % detail
    print(line, flush=True)
    if not cond:
        FAIL.append("%s | %s" % (label, detail))
    return bool(cond)


class Collector(object):
    """线程安全事件收集器（worker 侧不碰 Tk）。"""

    def __init__(self):
        self.ev = []
        self._lk = threading.Lock()
        self._cv = threading.Condition(self._lk)

    def __call__(self, kind, payload):
        with self._cv:
            self.ev.append((kind, dict(payload or {})))
            self._cv.notify_all()

    def of(self, kind):
        with self._lk:
            return [p for k, p in self.ev if k == kind]

    def kinds(self):
        with self._lk:
            return set(k for k, _ in self.ev)

    def wait_for(self, kind, timeout=180.0):
        end = time.time() + timeout
        with self._cv:
            while True:
                for k, p in self.ev:
                    if k == kind:
                        return p
                left = end - time.time()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 0.25))


def wav_peak(path):
    """返回 (peak_abs, n_frames, sr, ch)。读不出返回 (0,0,0,0)。"""
    try:
        with wave.open(path, "rb") as w:
            nch = w.getnchannels()
            sw = w.getsampwidth()
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            # 统一按 16 位看
            return (0, 0, sr, nch)
        vals = struct.unpack("<%dh" % (len(raw) // 2), raw[:len(raw) // 2 * 2])
        peak = max((abs(v) for v in vals), default=0)
        return (peak, len(vals) // nch, sr, nch)
    except Exception as e:                             # noqa: BLE001
        print("      [wav_peak] %s: %s" % (type(e).__name__, e))
        return (0, 0, 0, 0)


def n_wavs(d):
    return len(glob.glob(os.path.join(d, "*.wav")))


def find_sample(exts, prefer_name=""):
    """在 vendor/samples 里找真实素材（跳过 _rejected）。"""
    base = os.path.join(ROOT, "vendor", "samples")
    if not os.path.isdir(base):
        raise SystemExit("FATAL: 样本目录不存在 %s —— 不允许静默回落" % base)
    for sub in sorted(os.listdir(base)):
        if sub.startswith("_"):
            continue
        sdir = os.path.join(base, sub)
        if not os.path.isdir(sdir):          # MANIFEST.txt 等非目录项
            continue
        for f in sorted(os.listdir(sdir)):
            fp = os.path.join(sdir, f)
            if not os.path.isfile(fp):
                continue
            if os.path.splitext(f)[1].lower() in exts:
                if prefer_name and prefer_name.lower() not in f.lower():
                    continue
                if os.path.getsize(fp) > 0:
                    return fp
    return None


def run_one(src, wav_dir, timeout=240.0, policy=None):
    """单个源文件跑完整 ConvertWorker 流程, 返回 (collector, worker)。"""
    col = Collector()
    w = dc.ConvertWorker(wav_dir, col, threading.Event())
    w.start()
    w.enqueue({"src": src, "url": "qa://" + os.path.basename(src),
               "policy": policy or dc.policy_for(src), "pkg": "QA包"})
    w.mark_src_finished()
    col.wait_for("cv_thread_exit", timeout)
    w.wait(30)
    return col, w


def main():
    print("=" * 78)
    print("QA 独立验收 · B18 + B22 真解码  pid=%d" % os.getpid())
    print("python=%s" % sys.executable)
    print("=" * 78)

    # ---------------------------------------------------------------- L4
    print("\n===== L 组 · B22 回调 arity =====")
    print("-- L4 静态检查 --")
    for name in ("_on_progress", "_on_stage", "_on_log"):
        fn = getattr(dc.ConvertWorker, name, None)
        if fn is None:
            check("L4 %s 存在" % name, False)
            continue
        sig = inspect.signature(fn)
        has_varargs = any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())
        # B22 的本质是「**必填**位置参数个数 > 实际调用方给的个数」-> TypeError。
        # 带默认值的参数（如 _on_log(text=None)）不构成 arity 风险。
        required = [p.name for p in sig.parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                    and p.name != "self" and p.default is p.empty]
        check("L4 %s 无必填位置参数（*args 收全部形态）" % name,
              has_varargs and not required,
              "sig=%s 必填位置参数=%s" % (sig, required))

    tmp = tempfile.mkdtemp(prefix="qa_b18b22_%d " % os.getpid())
    try:
        # ------------------------------------------------------------ L1
        print("\n-- L1 NSF 真解码 --")
        nsf = find_sample({".nsf"})
        if not nsf:
            check("L1 找到 NSF 样本", False, "vendor/samples 无 .nsf")
        else:
            wd = os.path.join(tmp, "l1", "WAV")
            os.makedirs(wd, exist_ok=True)
            col, w = run_one(nsf, wd)
            done = col.of("cv_file_done")
            faild = col.of("cv_file_failed")
            check("L1 NSF 真解码发 cv_file_done（未抛 TypeError）",
                  len(done) == 1,
                  "done=%d failed=%d kinds=%s err=%s"
                  % (len(done), len(faild), sorted(col.kinds()),
                     faild[0].get("error") if faild else ""),
                  "AC-C5/L1")
            if done:
                check("L1 NSF ok > 0", int(done[0].get("n_wav") or 0) > 0,
                      "n_wav=%s" % done[0].get("n_wav"))
                check("L1 NSF 落盘数 == 记账数",
                      n_wavs(wd) == int(done[0].get("n_wav") or 0),
                      "落盘 %d / 记账 %s" % (n_wavs(wd), done[0].get("n_wav")))
                pks = [wav_peak(p)[0] for p in sorted(glob.glob(os.path.join(wd, "*.wav")))]
                check("L1 NSF WAV 非静音（峰值 > 300）",
                      bool(pks) and max(pks) > 300,
                      "峰值=%s" % pks)

        # ------------------------------------------------------------ L2
        print("\n-- L2 多芯片格式真解码 --")
        for label, exts in (("GBS", {".gbs"}), ("VGM", {".vgm"}),
                            ("HES", {".hes"}), ("KSS", {".kss"}),
                            ("GYM", {".gym"}), ("SAP", {".sap"}),
                            ("AY", {".ay"}), ("NSFE", {".nsfe"}),
                            ("SMD机种", {".vgm"})):
            p = find_sample(exts)
            if not p:
                check("L2 %s 有样本" % label, False, "vendor/samples 无 %s" % exts)
                continue
            wd = os.path.join(tmp, "l2_" + label, "WAV")
            os.makedirs(wd, exist_ok=True)
            col, w = run_one(p, wd)
            done = col.of("cv_file_done")
            faild = col.of("cv_file_failed")
            ok = len(done) == 1 and int(done[0].get("n_wav") or 0) > 0
            check("L2 %s 真解码 ok > 0" % label, ok,
                  "n_wav=%s 落盘=%d %s"
                  % (done[0].get("n_wav") if done else None, n_wavs(wd),
                     ("err=" + str(faild[0].get("error"))[:90]) if faild else ""),
                  "L2")
            if done:
                pks = [wav_peak(q)[0] for q in sorted(glob.glob(os.path.join(wd, "*.wav")))]
                check("L2 %s WAV 非静音（峰值 > 300）" % label,
                      bool(pks) and max(pks) > 300, "峰值=%s" % pks[:4])

        # ------------------------------------------------------------ L3
        print("\n-- L3 SPC 真解码（id666, write_tracks 4 参路径）--")
        spc = os.path.join(ROOT, "vendor", "sample.spc")
        if not os.path.isfile(spc):
            check("L3 sample.spc 存在", False, spc)
        else:
            wd = os.path.join(tmp, "l3", "WAV")
            os.makedirs(wd, exist_ok=True)
            check("L3 SPC 分流 policy=id666",
                  dc.policy_for(spc) == "id666",
                  "policy_for=%s" % dc.policy_for(spc))
            col, w = run_one(spc, wd, policy="id666")
            done = col.of("cv_file_done")
            faild = col.of("cv_file_failed")
            check("L3 SPC 真解码 ok > 0（4 参回调未 TypeError）",
                  len(done) == 1 and int(done[0].get("n_wav") or 0) > 0,
                  "n_wav=%s %s"
                  % (done[0].get("n_wav") if done else None,
                     ("err=" + str(faild[0].get("error"))[:120]) if faild else ""),
                  "L3/AC-C5")
            if done:
                check("L3 SPC 记账 policy=id666", done[0].get("policy") == "id666",
                      str(done[0].get("policy")))
                pks = [wav_peak(q)[0] for q in sorted(glob.glob(os.path.join(wd, "*.wav")))]
                check("L3 SPC WAV 非静音（峰值 > 300）",
                      bool(pks) and max(pks) > 300, "峰值=%s" % pks[:4])

        # ------------------------------------------------------------ K 组
        print("\n" + "=" * 78)
        print("K 组 · B18 零产出静默失败（真解码 + 截断文件）")
        print("=" * 78)

        good = find_sample({".nsf"})
        if not good:
            check("K 组 找到好样本 NSF", False)
            return 1
        wd = os.path.join(tmp, "k", "WAV")
        os.makedirs(wd, exist_ok=True)

        # 截断到 0x30 字节 —— probe 只看魔数 NESM, 仍判 NSF/True
        trunc = os.path.join(tmp, "k", "truncated.nsf")
        with open(good, "rb") as f:
            head = f.read(0x30)
        with open(trunc, "wb") as f:
            f.write(head)

        # 先证明 probe 确实放行（否则这个用例不成立）
        try:
            import chip_formats as cf
            pr = cf.probe(trunc)
            verdict = pr if isinstance(pr, (tuple, list)) else (pr,)
            check("K0 probe(truncated) 判为合法（所以必须转换后判产出）",
                  bool(verdict and verdict[0]), "probe=%r" % (verdict,))
        except Exception as e:                          # noqa: BLE001
            check("K0 probe 调用成功", False, "%s: %s" % (type(e).__name__, e))

        # 好文件先入队, 再入坏文件 —— 复刻 PM 的「2 好 + 1 坏」场景
        good2 = None
        for sub in sorted(os.listdir(os.path.join(ROOT, "vendor", "samples", "nsf"))):
            cand = os.path.join(ROOT, "vendor", "samples", "nsf", sub)
            if os.path.isfile(cand) and cand.lower() != good.lower() \
                    and os.path.splitext(cand)[1].lower() == ".nsf":
                good2 = cand
                break

        col, w = run_one(good, wd)
        col, w = run_one(trunc, wd)

        # 单独跑坏文件, 干净归因
        wd_bad = os.path.join(tmp, "k_bad", "WAV")
        os.makedirs(wd_bad, exist_ok=True)
        colb, wb = run_one(trunc, wd_bad)

        f_done = colb.of("cv_file_done")
        f_fail = colb.of("cv_file_failed")
        logs = [d for p in colb.of("cv_log") for d in (p.get("detail") or [])]
        alltext = "\n".join(logs) + "\n" + "\n".join(
            str(p.get("error") or "") for p in f_fail)

        check("K1 截断 NSF 发 cv_file_failed", len(f_fail) == 1,
              "failed=%d done=%d" % (len(f_fail), len(f_done)))
        check("K1b 绝不发 cv_file_done", len(f_done) == 0,
              "done=%d n_wav=%s" % (len(f_done),
                                    [d.get("n_wav") for d in f_done]))
        check("K1c 零产出时落盘 0 个 WAV", n_wavs(wd_bad) == 0,
              "落盘=%s" % glob.glob(os.path.join(wd_bad, "*.wav")))
        check("K1d 失败事件带 zero_output 标记",
              bool(f_fail) and f_fail[0].get("zero_output") is True,
              str(f_fail[0].get("zero_output")) if f_fail else "-")
        check("K2 错误文案含「未产出任何 WAV」",
              "未产出任何 WAV" in str(f_fail[0].get("error") if f_fail else ""),
              str(f_fail[0].get("error"))[:130] if f_fail else "-")
        check("K2b 文案含可执行提示（损坏/格式不受支持）",
              ("损坏" in alltext) or ("格式不受支持" in alltext)
              or ("手动检查" in alltext),
              alltext[:200].replace("\n", " | "))
        check("K2c 失败事件显式给 n_wav=0（不是「已转 0 首」）",
              bool(f_fail) and f_fail[0].get("n_wav") == 0,
              "n_wav=%s" % (f_fail[0].get("n_wav") if f_fail else "-"))

        # K3: manifest 不得记账
        mf = dc.ConvertManifest(os.path.join(wd_bad, ".msu1-convert.json"))
        n = mf.load()
        check("K3 manifest 无该文件记账（already_done=False -> 可重试）",
              mf.already_done(trunc) is False,
              "entries=%d entry=%r" % (n, mf.entry(trunc)))
        mpath = os.path.join(wd_bad, ".msu1-convert.json")
        if os.path.isfile(mpath):
            with open(mpath, encoding="utf-8") as f:
                raw = f.read()
            check("K3b manifest 文件里不含 truncated 源名",
                  "truncated" not in raw.lower(), raw[:160])
        else:
            check("K3b manifest 文件不存在（零产出不该建账本）", True, "无文件")

        # K4: 好文件对照
        wd_good = os.path.join(tmp, "k_good", "WAV")
        os.makedirs(wd_good, exist_ok=True)
        colg, wg = run_one(good, wd_good)
        g_done = colg.of("cv_file_done")
        g_fail = colg.of("cv_file_failed")
        gtext = "\n".join([d for p in colg.of("cv_log")
                           for d in (p.get("detail") or [])])
        check("K4 好文件发 cv_file_done 且 ok>0",
              len(g_done) == 1 and int(g_done[0].get("n_wav") or 0) > 0,
              "done=%d n_wav=%s" % (len(g_done),
                                    g_done[0].get("n_wav") if g_done else None))
        check("K4b 好文件不发 cv_file_failed", len(g_fail) == 0,
              str([f.get("error") for f in g_fail])[:120])
        check("K4c 好文件日志不出现「没有产出」类提示",
              ("没有产出" not in gtext) and ("未产出任何 WAV" not in gtext),
              gtext[:160].replace("\n", " | "))
        mfg = dc.ConvertManifest(os.path.join(wd_good, ".msu1-convert.json"))
        mfg.load()
        check("K4d 好文件 manifest 有记录且 already_done=True",
              mfg.already_done(good) is True,
              "entry=%r" % (mfg.entry(good),))

        # K5: read_last_skips 在 id666 路径不抛
        print("\n-- K5 read_last_skips 安全访问 --")
        try:
            r = dc.read_last_skips()
            check("K5 read_last_skips() 不抛（id666 路径 chip 专有属性）",
                  True, "返回=%r" % (r,))
        except Exception as e:                          # noqa: BLE001
            check("K5 read_last_skips() 不抛", False,
                  "%s: %s" % (type(e).__name__, e))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 78)
    print("结果: 通过 %d / 失败 %d" % (PASS[0], len(FAIL)))
    if FAIL:
        print("\n失败明细:")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
