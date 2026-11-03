# -*- coding: utf-8 -*-
"""探针：下载页表头的列宽把手，到底哪几个能抓到。

用户报「只有英文名称列可以调整大小，其他几列无法调整」。先量清楚：
1. 把手一共几个、各自的矩形在哪；
2. 在把手中心点 `elementFromPoint` 命中谁 —— 如果命中的不是把手，
   就是被别的元素盖住了（这条最能解释「点了没反应」）；
3. 真拖一下 `c-zh` 的把手，看 `--dl-cols` 变没变。
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

for k in [k for k in os.environ if "proxy" in k.lower()]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

import websocket                                                  # noqa: E402

import shot                                                       # noqa: E402
from web_ui import server                                         # noqa: E402

KEY = sys.argv[1] if len(sys.argv) > 1 else "nes"

edge = shot.find_edge()
print("Edge:", edge)
httpd, port, state = server.serve(port=0)
base = "http://127.0.0.1:%d" % port
from web_ui.server import dl_state                                # noqa: E402
print("load:", dl_state().load(KEY))

url = base + "/download.html?theme=dark"
args = [edge, "--app=%s" % url, "--window-size=1280,840",
        "--window-position=40,30",
        "--user-data-dir=%s" % shot.PROFILE,
        "--remote-debugging-port=%d" % shot.CDP_PORT,
        "--no-first-run", "--no-default-browser-check"]
proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)

target = None
for _ in range(25):
    time.sleep(1.0)
    try:
        for t in shot.cdp_targets():
            if t.get("type") == "page" and "127.0.0.1" in (t.get("url") or ""):
                target = t
                break
    except Exception:                                             # noqa: BLE001
        continue
    if target:
        break
if not target:
    print("没拿到 target")
    proc.terminate()
    httpd.shutdown()
    sys.exit(1)

time.sleep(1.2)
ws = None
for a in range(4):
    try:
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=20, suppress_origin=True,
            http_proxy_host=None, http_no_proxy=["127.0.0.1"])
        shot.call(ws, 1, "Runtime.evaluate",
                  {"expression": "1", "returnByValue": True})
        break
    except Exception as exc:                                      # noqa: BLE001
        print("WS 失败:", exc)
        time.sleep(2)
        ws = None
if ws is None:
    proc.terminate()
    httpd.shutdown()
    sys.exit(1)

time.sleep(1.0)

if "--fresh" in sys.argv:
    # 清掉存档再刷新，测「全新安装」时的默认列宽（旧存档会把上一次拖的结果带进来）
    shot.evaluate(ws, 20, "localStorage.removeItem('msu1.dl.cols'); 1")
    shot.evaluate(ws, 21, "location.reload(); 1")
    time.sleep(3.0)

INSPECT = r"""
(function () {
  var head = document.querySelector('.dl-thead');
  var wrap = document.querySelector('.dl-table-wrap');
  var out = {errs: window.__errs || [], colsVar: '', grips: []};
  if (wrap) {
    out.colsVar = wrap.style.getPropertyValue('--dl-cols');
    out.wrapW = wrap.clientWidth;
    out.gridCols = getComputedStyle(head).gridTemplateColumns;
  }
  if (!head) return JSON.stringify(out);
  var gs = head.querySelectorAll('.c-grip');
  out.nGrip = gs.length;
  gs.forEach(function (g) {
    var r = g.getBoundingClientRect();
    var cx = Math.round(r.left + r.width / 2);
    var cy = Math.round(r.top + r.height / 2);
    var hit = document.elementFromPoint(cx, cy);
    out.grips.push({
      col: g.dataset.col,
      x: Math.round(r.left), w: Math.round(r.width),
      h: Math.round(r.height),
      cx: cx, cy: cy,
      hit: hit ? (hit.tagName + '.' + (hit.className || '')) : null,
      hitIsGrip: !!(hit && hit.classList && hit.classList.contains('c-grip'))
    });
  });
  return JSON.stringify(out);
})()
"""

info = json.loads(shot.evaluate(ws, 2, INSPECT) or "{}")
print("errs:", info.get("errs"))
print("wrap.clientWidth =", info.get("wrapW"))
print("--dl-cols =", info.get("colsVar"))
print("computed  =", info.get("gridCols"))
print("把手数 =", info.get("nGrip"))
for g in info.get("grips") or []:
    print("  %-6s x=%-4s w=%-3s h=%-3s  命中=%s  抓到把手=%s"
          % (g["col"], g["x"], g["w"], g["h"], g["hit"], g["hitIsGrip"]))


SYNTH = r"""
(function (col, dx) {
  var g = document.querySelector('.c-grip[data-col="' + col + '"]');
  if (!g) return JSON.stringify({error: 'no grip'});
  var wrap = document.querySelector('.dl-table-wrap');
  var before = wrap.style.getPropertyValue('--dl-cols');
  var r = g.getBoundingClientRect();
  var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  var opt = {bubbles: true, cancelable: true, clientX: cx, clientY: cy,
             button: 0, buttons: 1};
  g.dispatchEvent(new MouseEvent('mousedown', opt));
  window.dispatchEvent(new MouseEvent('mousemove',
    {bubbles: true, clientX: cx + dx, clientY: cy, button: 0, buttons: 1}));
  window.dispatchEvent(new MouseEvent('mouseup',
    {bubbles: true, clientX: cx + dx, clientY: cy, button: 0, buttons: 0}));
  var after = wrap.style.getPropertyValue('--dl-cols');
  return JSON.stringify({before: before, after: after,
                         changed: before !== after});
})('%s', %d)
"""


def drag(col, dx):
    """用**合成 MouseEvent** 直接调页面里的拖拽逻辑。

    为什么不用 CDP 的 Input.dispatchMouseEvent：那条路受窗口焦点/命中测试
    影响，测出来「什么都没发生」时分不清是逻辑坏了还是事件没送到。
    合成事件只验逻辑本身。
    """
    raw = shot.evaluate(ws, 3, SYNTH % (col, dx))
    d = json.loads(raw or "{}")
    print("  拖 %-6s %+d px：" % (col, dx))
    print("    前: %s" % d.get("before"))
    print("    后: %s" % d.get("after"))
    print("    变了: %s" % d.get("changed"))
    return d.get("changed")


print("\n=== 合成事件真拖 c-zh（+40）===")
drag("zh", 40)
print("\n=== 合成事件真拖 c-date（+20）===")
drag("date", 20)
print("\n=== 合成事件真拖 c-size（+16）===")
drag("size", 16)
print("\n=== 合成事件真拖 c-state（+20）===")
drag("state", 20)
print("\n=== 合成事件真拖 c-name（+40）===")
drag("name", 40)
print("\n=== 合成事件真拖 c-act（+40）===")
drag("act", 40)

# v6.0.10：每列独立之后，列宽合计会超过容器 —— 横向滚动条必须出来，
# 而且表头得跟着滚（表头不在滚动容器里，靠 translateX）。
SCROLL = r"""
(function () {
  var box = document.getElementById('dlRows');
  var head = document.querySelector('.dl-thead');
  var wrap = document.querySelector('.dl-table-wrap');
  var out = {
    cols: wrap.style.getPropertyValue('--dl-cols'),
    dlw: wrap.style.getPropertyValue('--dl-w'),
    boxClientW: box.clientWidth,
    boxScrollW: box.scrollWidth,
    hasHScroll: box.scrollWidth > box.clientWidth + 1,
    headTransform: head.style.transform || '(none)'
  };
  box.scrollLeft = 60;
  return JSON.stringify(out);
})()
"""
sc = json.loads(shot.evaluate(ws, 4, SCROLL) or "{}")
print("\n=== 横向滚动 ===")
print("  --dl-cols   =", sc.get("cols"))
print("  --dl-w      =", sc.get("dlw"))
print("  容器宽/内容宽 = %s / %s" % (sc.get("boxClientW"), sc.get("boxScrollW")))
print("  出横向滚动条 =", sc.get("hasHScroll"))

time.sleep(0.4)
head_tf = shot.evaluate(ws, 5,
                        "(document.querySelector('.dl-thead').style.transform||'(none)')")
print("  scrollLeft=60 后表头 transform =", head_tf)

# 表头与行的**列左边界**必须逐个对齐。横向滚动之后最容易在这里错位，
# 而错位在截图里只是「看着有点怪」，不量就发现不了。
ALIGN = r"""
(function () {
  var head = document.querySelector('.dl-thead');
  var row = document.querySelector('.dl-row');
  if (!head || !row) return JSON.stringify({error: 'no row'});
  var out = [];
  ['c-sel','c-name','c-zh','c-date','c-size','c-state','c-act'].forEach(function (c) {
    var h = head.querySelector('.' + c), r = row.querySelector('.' + c);
    if (!h || !r) return;
    out.push({col: c,
              head: Math.round(h.getBoundingClientRect().left),
              row: Math.round(r.getBoundingClientRect().left)});
  });
  return JSON.stringify(out);
})()
"""
al = json.loads(shot.evaluate(ws, 7, ALIGN) or "[]")
print("\n=== 表头 vs 行 左边界（scrollLeft=60）===")
for a in al:
    d = a["row"] - a["head"]
    print("  %-7s 表头 x=%-5s 行 x=%-5s 差=%+d %s"
          % (a["col"], a["head"], a["row"], d, "OK" if abs(d) <= 1 else "✗ 错位"))

# 拍一张「列宽超出容器 + 已横向滚动」的样子，肉眼看表头有没有跟内容对齐
if "--shot" in sys.argv:
    time.sleep(0.3)
    shot.shot_now(ws, 6, r"D:\MSU1Temp\v610_scrolled.png")

proc.terminate()
try:
    proc.wait(timeout=10)
except Exception:                                                 # noqa: BLE001
    proc.kill()
httpd.shutdown()
