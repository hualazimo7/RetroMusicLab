/* level_scale.js —— 点阵的分级与配色（纯函数，无 DOM、无状态）
 *
 * 参数来源：AURORA Player 的实测（docs/deck/aurora-动态抓取-2026-11-04.md）
 *
 *   · 点阵 26 列 × 16 行，四段色
 *   · 换色落在自底向上的第 3 / 6 / 11 行 —— 等价于 0.20 / 0.40 / 0.70 三条分界，
 *     也就是「先算 10 级电平，再在 2/4/7 级换色」（2/10=0.2、4/10=0.4、7/10=0.7）
 *   · 复古四色 #ff344f / #ffc928 / #38ed6c / #399bff
 *   · 赛博朋克  #ff2d78 / #f2e51b / #22dcff / #b24fff
 *
 * ⚠ 分界**不是四等分**。写成 0.25/0.50/0.75 会让第 4 行、第 8 行就该换色，
 *   而参考产品是第 3、6 行 —— 一眼能看出「颜色分层不对」。
 *
 * 可单测：node 直接 require，见 level_scale.test.js
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (root) root.LevelScale = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  /* 网格尺寸：唯一来源。CSS 里的 calc(26 * var(--dot-pitch)) 必须与之一致，
     level_scale.test.js 会读 CSS 校验，防止两边各改一个数。 */
  var GRID = { cols: 26, rows: 16 };

  /* 自底向上的三条分界（归一化高度，0 = 底、1 = 顶） */
  var EDGES = [0.20, 0.40, 0.70];

  var PALETTES = {
    retro: ["#ff344f", "#ffc928", "#38ed6c", "#399bff"],
    cyber: ["#ff2d78", "#f2e51b", "#22dcff", "#b24fff"]
  };

  var UNLIT = "#23252a";     /* 实测的熄灭亡点色，不是纯黑 */

  function clamp01(v) {
    v = Number(v);
    if (!isFinite(v)) return 0;
    return v < 0 ? 0 : (v > 1 ? 1 : v);
  }

  /* 第 row 行属于哪一段（row 从 0 开始，0 = 最底下那行）
     取行中心判断，避免整行贴边时的边界抖动。 */
  function bandOf(row, rows) {
    rows = Math.max(1, Math.round(Number(rows) || 1));
    row = Math.max(0, Math.min(rows - 1, Math.round(Number(row) || 0)));
    var p = (row + 0.5) / rows;
    if (p < EDGES[0]) return 0;
    if (p < EDGES[1]) return 1;
    if (p < EDGES[2]) return 2;
    return 3;
  }

  /* 电平 → 点亮几行（0..rows）。四舍五入而不是向下取整：
     向下取整会让 0.99 只亮 15/16，顶上永远缺一行。 */
  function litRows(level, rows) {
    rows = Math.max(0, Math.round(Number(rows) || 0));
    return Math.round(clamp01(level) * rows);
  }

  /* 柱状模式用的连续高度（单位：行） */
  function barRows(level, rows) {
    return clamp01(level) * Math.max(0, Number(rows) || 0);
  }

  /* 每一段覆盖的行区间，给测试和调试用 */
  function bandsOf(rows) {
    var out = [];
    for (var r = 0; r < rows; r++) {
      var b = bandOf(r, rows);
      if (!out[b]) out[b] = { band: b, from: r, to: r, count: 0 };
      out[b].to = r;
      out[b].count++;
    }
    return out.filter(Boolean);
  }

  function palette(name) {
    return PALETTES[name] || PALETTES.retro;
  }

  return {
    GRID: GRID,
    EDGES: EDGES,
    PALETTES: PALETTES,
    UNLIT: UNLIT,
    clamp01: clamp01,
    bandOf: bandOf,
    litRows: litRows,
    barRows: barRows,
    bandsOf: bandsOf,
    palette: palette
  };
});
