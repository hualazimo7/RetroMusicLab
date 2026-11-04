/* aurora_theme.js —— 极光的外观参数与纯计算（无 DOM、无状态）
 *
 * 全部数值来自 AURORA Player 的像素实测
 * （docs/deck/aurora-动态抓取-2026-11-04.md §7–§9），不是估的颜色。
 *
 * 四组设置：
 *   频谱灯光   retro(复古四色) / gold(鎏金) / silver(冷银) / cyber(赛博朋克)
 *   氛围灯颜色 off / warm / silver / ice / violet / rainbow / cyber
 *   氛围灯动态 static / breathe(3.6s) / beat(音频包络) / orbit(2.0s 行波)
 *   面板材质   glass(亮面玻璃) / metal(金属面板) / blur(封面模糊背景)
 *
 * 可单测：node 直接 require，见 aurora_theme.test.js
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (root) root.AuroraTheme = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  var TAU = Math.PI * 2;

  /* ── 频谱灯光：四套主题的四段色 ────────────────────────
     gold / silver 是**单色**（实测鎏金全 16 行同色，不分段）。 */
  var LIGHT_THEMES = {
    retro: { label: "复古四色", bands: ["#ff344f", "#ffc928", "#38ed6c", "#399bff"] },
    gold: { label: "鎏金", bands: ["#e0b45e"], single: true },
    silver: { label: "冷银", bands: ["#9aa3ad"], single: true },
    cyber: { label: "赛博朋克", bands: ["#ff2d78", "#f2e51b", "#22dcff", "#b24fff"] }
  };

  /* ── 氛围灯颜色 ──────────────────────────────────────
     solid 是实测的单色；rainbow / cyber 是沿周界的**色相路径**。
     「自定义」按用户要求不做。 */
  var AMBIENT = {
    off: { label: "关闭", solid: null },
    warm: { label: "暖金", solid: "#a98849" },
    silver: { label: "冷银", solid: "#969da9" },
    ice: { label: "冰蓝", solid: "#4b85b0" },
    violet: { label: "紫霓", solid: "#7f5ab3" },
    /* 彩虹：色相随周界线性推进，绕一圈正好 360°。
       实测端点：上边 3°→107°、右边 +55°、左边 −55°，合起来排除了
       锥形渐变与单线性渐变，必须是「按周长参数化」。
       ⚠ 参数零点在**顶边左端**（左上圆角之后那一点），不是圆角弧的中点。 */
    rainbow: { label: "彩虹", from: 3, span: 360 },
    /* 赛博朋克：不是彩虹那种单调推进，而是四角 + 边中点的锚点插值。
       实测锚点（panel 2125×1225 上的采样）：
         左上 338° / 顶中 311° / 右上 272° / 右边下端 229° / 底中 191° / 左下 230° */
    cyber: { label: "赛博朋克", anchors: true }
  };

  var CYBER_ANCHORS = [
    { at: "topMid", hue: 311 },
    { at: "topRight", hue: 272 },
    { at: "rightLow", hue: 229 },
    { at: "bottomMid", hue: 191 },
    { at: "bottomLeft", hue: 230 },
    /* 左边从底(230°)往上回到左上(338°)：中间没有实测点，按线性补一个。
       这是全文唯一一个**推断值**，其余都是截图采样。 */
    { at: "leftMid", hue: 284 },
    { at: "topLeft", hue: 338 }
  ];

  var DYNAMICS = {
    static: { label: "静态" },
    breathe: { label: "呼吸", period: 3.6, lo: 0.64, hi: 1 },
    beat: { label: "律动", lo: 0.64, hi: 1 },
    orbit: { label: "环绕", period: 2.0, lo: 0.28, hi: 1, sigma: 0.075 }
  };

  var MATERIALS = {
    glass: { label: "亮面玻璃" },
    metal: { label: "金属面板" },
    blur: { label: "封面模糊背景" }
  };

  var DEFAULTS = {
    lightTheme: "retro",
    ambient: "warm",
    ambientDyn: "breathe",
    material: "glass"
  };

  var UNLIT = "#23252a";

  function clamp01(v) {
    v = Number(v);
    if (!isFinite(v)) return 0;
    return v < 0 ? 0 : (v > 1 ? 1 : v);
  }

  function has(obj, k) { return Object.prototype.hasOwnProperty.call(obj, k); }

  /* 频谱灯光取色：单色主题不看 band */
  function lightColor(theme, band) {
    var t = LIGHT_THEMES[theme] || LIGHT_THEMES[DEFAULTS.lightTheme];
    var bands = t.bands;
    if (bands.length === 1) return bands[0];
    band = Math.max(0, Math.min(bands.length - 1, Math.round(Number(band) || 0)));
    return bands[band];
  }

  /* 圆角矩形的周长分段（顺时针，从顶边左端起算） */
  function perimeter(w, h, rad) {
    w = Math.max(0, Number(w) || 0);
    h = Math.max(0, Number(h) || 0);
    var r = Math.max(0, Math.min(Number(rad) || 0, Math.min(w, h) / 2));
    var straightX = Math.max(0, w - 2 * r);
    var straightY = Math.max(0, h - 2 * r);
    var arc = Math.PI * r / 2;
    var total = 2 * straightX + 2 * straightY + 4 * arc;
    return { w: w, h: h, r: r, straightX: straightX, straightY: straightY, arc: arc, total: total };
  }

  /* 周界上某一点的实际坐标（顺时针，零点在顶边左端）
     画氛围环时逐段取点用。w/h/r 传的是**描边所在的那一圈**的尺寸。 */
  function pointAt(frac, w, h, rad) {
    var p = perimeter(w, h, rad);
    if (!p.total) return { x: 0, y: 0 };
    var f = ((Number(frac) || 0) % 1 + 1) % 1;
    var d = f * p.total;
    var r = p.r;
    if (d <= p.straightX) return { x: r + d, y: 0 };
    d -= p.straightX;
    if (d <= p.arc) {
      var a1 = -Math.PI / 2 + (d / p.arc) * (Math.PI / 2);
      return { x: w - r + Math.cos(a1) * r, y: r + Math.sin(a1) * r };
    }
    d -= p.arc;
    if (d <= p.straightY) return { x: w, y: r + d };
    d -= p.straightY;
    if (d <= p.arc) {
      var a2 = (d / p.arc) * (Math.PI / 2);
      return { x: w - r + Math.cos(a2) * r, y: h - r + Math.sin(a2) * r };
    }
    d -= p.arc;
    if (d <= p.straightX) return { x: w - r - d, y: h };
    d -= p.straightX;
    if (d <= p.arc) {
      var a3 = Math.PI / 2 + (d / p.arc) * (Math.PI / 2);
      return { x: r + Math.cos(a3) * r, y: h - r + Math.sin(a3) * r };
    }
    d -= p.arc;
    if (d <= p.straightY) return { x: 0, y: h - r - d };
    d -= p.straightY;
    var a4 = Math.PI + (d / (p.arc || 1)) * (Math.PI / 2);
    return { x: r + Math.cos(a4) * r, y: r + Math.sin(a4) * r };
  }

  /* 周界参数下，各锚点对应的归一化位置（0..1） */
  function anchorFractions(w, h, rad) {
    var p = perimeter(w, h, rad);
    var t = p.total || 1;
    var topEnd = p.straightX;
    var rightStart = topEnd + p.arc;
    var rightEnd = rightStart + p.straightY;
    var bottomStart = rightEnd + p.arc;
    var bottomEnd = bottomStart + p.straightX;
    var leftStart = bottomEnd + p.arc;
    var leftEnd = leftStart + p.straightY;
    return {
      total: t,
      topMid: (p.straightX * 0.5) / t,
      topRight: topEnd / t,
      rightLow: (rightStart + p.straightY * 0.82) / t,
      bottomMid: (bottomStart + p.straightX * 0.5) / t,
      bottomLeft: bottomEnd / t,
      leftMid: (leftStart + p.straightY * 0.5) / t,
      topLeft: (leftEnd + p.arc * 0.5) / t
    };
  }

  function hueOfHex(hex) {
    var h = String(hex || "").replace("#", "");
    if (h.length !== 6) return null;
    var r = parseInt(h.slice(0, 2), 16) / 255;
    var g = parseInt(h.slice(2, 4), 16) / 255;
    var b = parseInt(h.slice(4, 6), 16) / 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    if (d < 1e-6) return 0;
    var hue;
    if (mx === r) hue = ((g - b) / d) % 6;
    else if (mx === g) hue = (b - r) / d + 2;
    else hue = (r - g) / d + 4;
    hue *= 60;
    return hue < 0 ? hue + 360 : hue;
  }

  /* 色相 → #rrggbb（固定饱和度/亮度，取实测氛围环的量级 S≈55% V≈68%） */
  function hslToHex(hue, sat, light) {
    hue = ((Number(hue) || 0) % 360 + 360) % 360;
    sat = sat == null ? 0.55 : sat;
    light = light == null ? 0.62 : light;
    var c = (1 - Math.abs(2 * light - 1)) * sat;
    var x = c * (1 - Math.abs((hue / 60) % 2 - 1));
    var m = light - c / 2;
    var rgb;
    if (hue < 60) rgb = [c, x, 0];
    else if (hue < 120) rgb = [x, c, 0];
    else if (hue < 180) rgb = [0, c, x];
    else if (hue < 240) rgb = [0, x, c];
    else if (hue < 300) rgb = [x, 0, c];
    else rgb = [c, 0, x];
    return "#" + rgb.map(function (v) {
      var n = Math.round((v + m) * 255);
      return (n < 16 ? "0" : "") + n.toString(16);
    }).join("");
  }

  /* 赛博朋克：按锚点分段线性插值出路色相（首尾按环形处理） */
  function cyberHue(frac, fr) {
    var pts = CYBER_ANCHORS.map(function (a) {
      return { f: fr[a.at], hue: a.hue };
    }).sort(function (a, b) { return a.f - b.f; });
    frac = ((Number(frac) || 0) % 1 + 1) % 1;
    var n = pts.length;
    for (var i = 0; i < n; i++) {
      var a = pts[i], b = pts[(i + 1) % n];
      var f0 = a.f;
      var f1 = (i === n - 1) ? b.f + 1 : b.f;    /* 最后一段跨过 1 */
      var x = frac;
      if (i === n - 1 && x < f0) x += 1;
      if (x >= f0 && x <= f1) {
        var k = (f1 - f0) < 1e-9 ? 0 : (x - f0) / (f1 - f0);
        return a.hue + (b.hue - a.hue) * k;
      }
    }
    return pts[0].hue;
  }

  /* 周界某一点的氛围灯颜色；关闭时返回 null
     ⚠ 归一化要写成 ((x % 1) + 1) % 1：负的 frac 也要落到 0..1 */
  function ringColor(key, frac, fr) {
    var a = AMBIENT[key];
    if (!a || key === "off") return null;
    if (a.solid) return a.solid;
    var f = ((Number(frac) || 0) % 1 + 1) % 1;
    if (a.anchors) return hslToHex(cyberHue(f, fr || anchorFractions(360, 200, 12)));
    return hslToHex(a.from + a.span * f);        /* rainbow */
  }

  /* 动态 → 当前透明度。ctx = {t 秒, rms 0..1, frac 0..1}
     ⚠ 不要写 `if (key === "static" || !d.period && !d.sigma) return 1;` ——
       beat 既没有 period 也没有 sigma，会被这句提前 return 1，律动就哑了。
       逐个 case 显式处理，兜底才是 1。 */
  function ringAlpha(key, ctx) {
    var d = DYNAMICS[key] || DYNAMICS.static;
    ctx = ctx || {};
    if (key === "breathe") {
      var t = (Number(ctx.t) || 0) % d.period;
      return d.lo + (d.hi - d.lo) * (0.5 - 0.5 * Math.cos(TAU * t / d.period));
    }
    if (key === "beat") {
      return d.lo + (d.hi - d.lo) * clamp01((Number(ctx.rms) || 0) * 2.2);
    }
    if (key === "orbit") {
      var spot = ((Number(ctx.t) || 0) / d.period) % 1;
      if (spot < 0) spot += 1;
      var frac = ((Number(ctx.frac) || 0) % 1 + 1) % 1;
      var dist = Math.abs(frac - spot);
      dist = Math.min(dist, 1 - dist);
      return d.lo + (d.hi - d.lo) * Math.exp(-(dist * dist) / (2 * d.sigma * d.sigma));
    }
    return 1;
  }

  function resolve(patch) {
    var out = {};
    var p = patch || {};
    out.lightTheme = has(LIGHT_THEMES, p.lightTheme) ? p.lightTheme : DEFAULTS.lightTheme;
    out.ambient = has(AMBIENT, p.ambient) ? p.ambient : DEFAULTS.ambient;
    out.ambientDyn = has(DYNAMICS, p.ambientDyn) ? p.ambientDyn : DEFAULTS.ambientDyn;
    out.material = has(MATERIALS, p.material) ? p.material : DEFAULTS.material;
    return out;
  }

  return {
    TAU: TAU,
    LIGHT_THEMES: LIGHT_THEMES,
    AMBIENT: AMBIENT,
    CYBER_ANCHORS: CYBER_ANCHORS,
    DYNAMICS: DYNAMICS,
    MATERIALS: MATERIALS,
    DEFAULTS: DEFAULTS,
    UNLIT: UNLIT,
    clamp01: clamp01,
    lightColor: lightColor,
    perimeter: perimeter,
    pointAt: pointAt,
    anchorFractions: anchorFractions,
    hueOfHex: hueOfHex,
    hslToHex: hslToHex,
    cyberHue: cyberHue,
    ringColor: ringColor,
    ringAlpha: ringAlpha,
    resolve: resolve
  };
});
