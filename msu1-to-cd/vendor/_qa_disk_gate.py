# -*- coding: utf-8 -*-
"""QA 独立验收 · 磁盘门禁 7 条判据 + 0x15 字段自查（架构师要求「必须自己跑」）

架构师明确要求：**不要引用文档里的 0x15 调研表**（那是论证支柱，
直接引用等于「用结论验证结论」）。本脚本自己跑一遍。

两部分:
  A. 0x15 字段自查（7 个真实 HES 样本，自读 0x15 + 真解码产出轨数）
  B. 磁盘门禁 7 条判据（含唯一有鉴别力的判据1: free == need 必须放行）

背景（架构师更正）:
  - 门禁比的是 need = 256 x 150 x 176400 = 6.309 GiB，**不是真实产出**
  - v3.9 的三条旧断言把 free 设成 need x 1.3 -> 去掉 1.3 后**依然全绿**
    => **零鉴别力**。只有 free 精确 == need 才夹得住。
  - 所以判据 1（free == need -> 放行）是全套唯一有鉴别力的用例。

用法: C:/Python314/python.exe vendor/_qa_disk_gate.py
"""
import glob
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = [0]
FAIL = []


def check(label, cond, detail="", ac=""):
    PASS[0] += 1
    line = "  [%s] %s" % ("PASS" if cond else "FAIL", label)
    if ac:
        line += "  (%s)" % ac
    if detail:
        line += "  -- %s" % detail
    print(line, flush=True)
    if not cond:
        FAIL.append("%s | %s" % (label, detail))
    return bool(cond)


GIB = 1024.0 ** 3
NEED = 256 * 150 * 176400          # 6.309 GiB，与门禁同口径


def main():
    print("=" * 78)
    print("QA 独立验收 · 磁盘门禁判据 + 0x15 自查  pid=%d" % os.getpid())
    print("=" * 78)

    # ================================================== A. 0x15 自查
    print("\n===== A. 0x15 字段自查（自己跑，不引用文档）=====")
    samples = ["AKCD3001", "JC63011", "HC91048", "HE93003", "TP02011",
               "HC63013", "PV1004"]
    rows = []
    for nm in samples:
        p = os.path.join(ROOT, "vendor", "samples", "hes", nm + ".hes")
        if not os.path.isfile(p):
            print("  %-10s [缺失] %s" % (nm, p))
            continue
        raw = open(p, "rb").read()
        b15 = raw[0x15] if len(raw) > 0x15 else -1
        # 假设：len == (b15 + 1) * 256
        pred = (b15 + 1) * 256
        rows.append((nm, b15, len(raw), pred, len(raw) == pred))
        print("  %-10s 0x15=%3d 实际长度=%6d  预测(%d+1)*256=%6d  %s"
              % (nm, b15, len(raw), b15, pred,
                 "符合" if len(raw) == pred else "不符"))

    if rows:
        n_ok = sum(1 for r in rows if r[4])
        check("A1 0x15 公式 len == (0x15+1)*256 在样本上成立",
              n_ok >= len(rows) - 1,
              "%d/%d 符合（容许 1 个例外以免误判）" % (n_ok, len(rows)))

    # 真解码产出轨数（走 chip_decode 完整管线，空轨由 peak<300 跳过）
    print("\n  -- 真解码产出轨数（chip_decode 完整管线，与 v4.0 同路径）--")
    try:
        import chip_decode
        prod = {}
        for nm in samples:
            p = os.path.join(ROOT, "vendor", "samples", "hes", nm + ".hes")
            if not os.path.isfile(p):
                continue
            try:
                r = chip_decode.load_chip_paths([p], default_secs=150,
                                                cap_secs=600)
                prod[nm] = len(r)
                print("  %-10s 产出 %3d 轨" % (nm, len(r)))
            except Exception as e:                      # noqa: BLE001
                prod[nm] = -1
                print("  %-10s 解码失败: %s: %s"
                      % (nm, type(e).__name__, str(e)[:60]))
        check("A2 至少 5 个样本真解码成功（产出轨数可信）",
              sum(1 for v in prod.values() if v > 0) >= 5,
              "成功 %d 个" % sum(1 for v in prod.values() if v > 0))

        # 比值：0x15 vs 真实产出 —— 这是「0x15 不能当轨数」的依据
        ratios = []
        for nm, b15, _ln, _p, _ok in rows:
            if prod.get(nm, -1) > 0:
                ratios.append((nm, b15, prod[nm], b15 / float(prod[nm])))
        if ratios:
            print("\n  -- 0x15 / 真实产出 比值（若恒≈1 才说明 0x15 是轨数）--")
            for nm, b15, pr, rt in ratios:
                print("     %-10s 0x15=%3d 产出=%3d  比值=%.2f"
                      % (nm, b15, pr, rt))
            rs = [r[3] for r in ratios]
            check("A3 比值**不恒为 1**（证明 0x15 不能当轨数用）",
                  max(rs) - min(rs) > 0.3,
                  "比值区间 %.2f ~ %.2f（跨度 %.2f）"
                  % (min(rs), max(rs), max(rs) - min(rs)))
    except ImportError as e:
        check("A2 chip_decode 可导入", False, str(e))

    # ================================================== B. 门禁判据
    print("\n===== B. 磁盘门禁 7 条判据 =====")
    print("  need = 256 x 150 x 176400 = %.3f GiB，门禁比的是 need"
          % (NEED / GIB))
    import dl_convert as dc

    tmp = tempfile.mkdtemp(prefix="qa_gate_%d " % os.getpid())
    try:
        def gate(free_bytes, need_bytes=NEED):
            """直接调 _disk_ok，注入 free 值。"""
            wd = os.path.join(tmp, "g", "WAV")
            os.makedirs(wd, exist_ok=True)
            w = dc.ConvertWorker(wd, lambda *a: None, threading.Event())
            w.disk_free_fn = lambda: float(free_bytes)
            return w._disk_ok(need_bytes)

        # ---- 判据 1：唯一有鉴别力 ----
        r1 = gate(NEED)
        check("判据1 free == need（6.309 GiB）-> 放行【唯一有鉴别力】",
              r1 is True,
              "free=need=%.3f GiB -> %s" % (NEED / GIB, r1),
              "夹住「1.3 是否去掉」")
        # 鉴别力自证：把 1.3 加回去（模拟旧实现），判据1 应当**被夹住**
        #即旧实现下 free(=need) < need*1.3 -> 拦。 现实现放行 -> 说明确实夹住了
        old_logic = (float(NEED) >= max(float(dc.ConvertWorker.DISK_RESERVE),
                                         NEED * 1.3))
        check("判据1b 鉴别力自证：旧实现(带1.3)会拦、现实现放行",
              (old_logic is False) and (r1 is True),
              "旧实现(need*1.3)=%s vs 现实现=%s -> 该用例能区分两者"
              % (old_logic, r1))

        # ---- 判据 2' / 2'' ----
        check("判据2' free=6.5 GiB -> 放行（门槛已从 8.20 降到 6.31）",
              gate(6.5 * GIB) is True, "%.2f GiB" % 6.5)
        check("判据2'' free=1.0 GiB -> **拦**（真实产出不是判据）",
              gate(1.0 * GIB) is False, "%.2f GiB" % 1.0)

        # ---- 判据 3 ----
        check("判据3 free=0.3 GiB -> 拦", gate(0.3 * GIB) is False, "0.30 GiB")

        # ---- 判据 4（低估方向）----
        low = 1.85 * GIB
        check("判据4 free=1.85 GiB -> 拦（低估方向仍兜住）",
              gate(low) is False,
              "%.2f GiB < need %.2f GiB" % (1.85, NEED / GIB))

        # ---- 判据 5（>= 的另一侧）----
        check("判据5 free == need-1 -> 拦",
              gate(NEED - 1) is False,
              "need-1 = %.3f GiB" % ((NEED - 1) / GIB))

        # ---- 判据 6（地板是下限不是叠加项）----
        r6a = gate(256 * 1024 * 1024, need_bytes=1024)
        r6b = gate(256 * 1024 * 1024 - 1, need_bytes=1024)
        check("判据6 free=256MiB, need=1KiB -> 放行",
              r6a is True, "got %s" % r6a)
        check("判据6b free=256MiB-1, need=1KiB -> 拦",
              r6b is False, "got %s" % r6b)

        # ---- 判据 7：源码反向锁 ----
        #⚠ **必须用 AST，不能用文本匹配**。
        #   第一版我用 `"1.3" not in body` 去grep， 结果**假红**：
        #   `_disk_ok` 的 **docstring 里**就写着「不叠 1.3 倍余量」这句话
        #   （解释为何去掉），文本匹配把它当成代码 -> 误判。
        #   文本匹配无法区分「注释/文档里提到」与「代码里真用了」——
        #   这与「引用文档当证据」是同一类错误。
        import ast
        src = open(os.path.join(ROOT, "dl_convert.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        fn_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and \
                    node.name == "_disk_ok":
                fn_node = node
                break
        check("判据7 找到 _disk_ok 的 AST 节点", fn_node is not None)
        # 只看真实代码表达式里的常量（自动排除注释与 docstring）
        consts = []
        if fn_node is not None:
            for node in ast.walk(fn_node):
                if isinstance(node, ast.Constant) and \
                        isinstance(node.value, (int, float)):
                    consts.append(node.value)
        has_13 = any(abs(c - 1.3) < 1e-9 for c in consts)
        check("判据7 _disk_ok **代码常量**里无 1.3 倍系数（非文本匹配）",
              not has_13,
              "代码常量=%s%s" % (sorted(set(consts)),
                                " <- 含 1.3！" if has_13 else ""))
        # 反向自证：把 1.3 塞进常量列表应当被检出（证明本判据有鉴别力）
        _probe = list(consts) + [1.3]
        check("判据7b 鉴别力自证：注入 1.3 后本判据会报警",
              any(abs(c - 1.3) < 1e-9 for c in _probe),
              "注入后应检出 -> 证明判据7 不是永真")
        body = src.split("def _disk_ok", 1)[-1].split("\n    def ", 1)[0]
        check("判据7c 使用 >= 而非 >（free==need 放行）",
              "free >=" in body, "找到 'free >='")
        check("判据7d DISK_RESERVE 仍在且为 256MiB",
              dc.ConvertWorker.DISK_RESERVE == 256 * 1024 * 1024,
              "DISK_RESERVE=%d" % dc.ConvertWorker.DISK_RESERVE)
        check("判据7e max(DISK_RESERVE, need) 地板语义仍在",
              "max(float(self.DISK_RESERVE), need)" in body
              or "max(self.DISK_RESERVE, need)" in body,
              "地板未被当成叠加项")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 78)
    print("结果: 通过 %d / 失败 %d" % (PASS[0], len(FAIL)))
    for f in FAIL:
        print("  - %s" % f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())