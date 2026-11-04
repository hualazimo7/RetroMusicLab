/* skin_aurora.js —— 极光皮肤
 *
 * 三块内容，全部由内核状态驱动：
 *   ① 点阵 / 柱状   26×16，四段色，数据来自 PlayerCore.spectrum()
 *   ② 氛围环        沿面板周界的一圈描边，8 色 × 4 档动态（aurora_theme.js）
 *   ③ 面板材质      亮面玻璃 / 金属面板 / 封面模糊背景
 *
 * 几何与配色的来源：docs/deck/aurora-动态抓取-2026-11-04.md（像素实测）。
 * 皮肤只做几何与上色：分频在内核、分级在 level_scale、色相在 aurora_theme。
 */
(function () {
  "use strict";

  var L = window.LevelScale;
  var A = window.AuroraTheme;
  var COLS = L.GRID.cols;
  var ROWS = L.GRID.rows;
  var TAU = Math.PI * 2;
  var DOT_R = 0.42;          /* 半径 / 间距，实测 27px 直径、32px 间距 */
  var LEVEL_STEPS = 32;      /* 只用于「要不要重绘」的量化档数 */
  var RING_SEG = 220;        /* 氛围环的描边分段数 */
  var RING_W = 2;            /* 环宽 */
  var RING_INSET = 1.2;      /* 环离面板边缘的距离 */

  var skin = {
    id: "aurora",
    label: "极光",

    mount: function (host, core) {
      this._host = host;
      this._core = core;
      this._w = 0; this._h = 0; this._dpr = 0;
      this._sig = "";
      this._idle = null;
      this._ringKey = "";
      this._ringpts = null;
      this._ringcol = null;
      this._ringImg = null;
      this._pw = 0; this._ph = 0;
      this._t0 = performance.now();

      var panel = PVUI.el("div", "au-panel");
      panel.innerHTML =
        '<div class="au-mat"></div>' +
        '<canvas class="au-ring" aria-hidden="true"></canvas>' +
        '<canvas class="au-matrix" role="img" aria-label="音色点阵"></canvas>' +
        '<div class="au-tools">' +
        '  <button class="au-mode" type="button"></button>' +
        '  <button class="au-gear" type="button" title="外观设置">设置</button>' +
        '</div>';

      this._panel = panel;
      this._cv = panel.querySelector(".au-matrix");
      this._g = this._cv.getContext("2d");
      this._ring = panel.querySelector(".au-ring");
      this._rg = this._ring.getContext("2d");
      this._btn = panel.querySelector(".au-mode");
      this._gear = panel.querySelector(".au-gear");

      var self = this;
      this._btn.addEventListener("click", function () {
        core.setVizMode(core.state.vizMode === "bars" ? "dots" : "bars");
      });
      this._gear.addEventListener("click", function (ev) {
        ev.stopPropagation();
        self._toggleSettings();
      });

      /* 封面不画在播放器里（下载页有独立封面区），但「封面模糊背景」这个
         材质要用它 —— 所以只取 state.cover.url 当背景，不做封面控件。 */
      this._ui = PVUI.mount(host, core, panel, {
        skinLabel: "磁带机",
        onSwitchSkin: function () { core.setSkin("deck"); }
      });

      this._buildSettings();

      function tick() {
        if (!self._host) return;
        self._frame();
        self._raf = requestAnimationFrame(tick);
      }
      this._raf = requestAnimationFrame(tick);
    },

    unmount: function () {
      if (this._raf) cancelAnimationFrame(this._raf);
      this._raf = 0;
      if (this._pop && this._pop.parentNode) this._pop.parentNode.removeChild(this._pop);
      if (this._outside) document.removeEventListener("pointerdown", this._outside, true);
      this._ui = null;
      this._host = null;
      this._core = null;
      this._cv = null; this._g = null;
      this._ring = null; this._rg = null;
      this._idle = null; this._ringImg = null;
      this._pop = null;
    },

    /* ── 每帧 ─────────────────────────────────────────── */
    _frame: function () {
      var cv = this._cv;
      if (!cv) return;
      var w = cv.clientWidth, h = cv.clientHeight;
      if (!w || !h) return;
      var dpr = Math.min(2, window.devicePixelRatio || 1);
      if (w !== this._w || h !== this._h || dpr !== this._dpr) {
        this._resize(w, h, dpr);
        this._sig = "";
      }

      var st = this._core.state;
      var look = A.resolve(st.appearance);
      var mode = st.vizMode === "bars" ? "bars" : "dots";
      var lv = this._core.spectrum ? this._core.spectrum(COLS) : null;

      var q = "";
      for (var i = 0; i < COLS; i++) q += Math.round((lv ? lv[i] : 0) * LEVEL_STEPS) + ",";
      var sig = mode + "|" + look.lightTheme + "|" + w + "x" + h + "|" + q;
      if (sig !== this._sig) {
        this._sig = sig;
        if (this._btn) {
          this._btn.textContent = mode === "bars" ? "柱状" : "点阵";
          this._btn.title = mode === "bars" ? "切换为点阵" : "切换为柱状";
        }
        this._draw(lv, mode, look.lightTheme);
      }

      this._drawRing(look);
      this._syncLook(st, look);
    },

    _resize: function (w, h, dpr) {
      var cv = this._cv, rg = this._ring;
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      this._g.setTransform(dpr, 0, 0, dpr, 0, 0);
      /* 环画在**面板**的整个盒子上，比点阵宽 */
      var pw = this._panel.clientWidth, ph = this._panel.clientHeight;
      rg.width = Math.round(pw * dpr);
      rg.height = Math.round(ph * dpr);
      this._rg.setTransform(dpr, 0, 0, dpr, 0, 0);
      this._w = w; this._h = h; this._dpr = dpr;
      this._pw = pw; this._ph = ph;
      this._buildIdle(w, h, dpr);
      this._ringKey = "";
    },

    /* 熄灭亡点只画一次，之后每帧整张贴上来（416 个圆逐帧画是白烧 GPU）。 */
    _buildIdle: function (w, h, dpr) {
      var off = document.createElement("canvas");
      off.width = Math.round(w * dpr);
      off.height = Math.round(h * dpr);
      var g = off.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      var px = w / COLS, py = h / ROWS;
      var pitch = Math.min(px, py);
      var r = pitch * DOT_R;
      g.fillStyle = A.UNLIT;
      g.beginPath();
      for (var c = 0; c < COLS; c++) {
        for (var rr = 0; rr < ROWS; rr++) {
          var cx = (c + 0.5) * px, cy = h - (rr + 0.5) * py;
          g.moveTo(cx + r, cy);
          g.arc(cx, cy, r, 0, TAU);
        }
      }
      g.fill();
      this._idle = off;
    },

    _draw: function (lv, mode, theme) {
      var g = this._g, w = this._w, h = this._h;
      g.setTransform(1, 0, 0, 1, 0, 0);
      g.clearRect(0, 0, this._cv.width, this._cv.height);
      /* 柱状模式不铺暗点层：柱子上叠暗点像「撒了芝麻」。 */
      if (mode !== "bars" && this._idle) g.drawImage(this._idle, 0, 0);
      g.setTransform(this._dpr, 0, 0, this._dpr, 0, 0);

      if (!lv) return;
      var px = w / COLS, py = h / ROWS;
      var pitch = Math.min(px, py);
      var r = pitch * DOT_R;

      if (mode === "bars") {
        var bw = pitch * 0.72;
        for (var c = 0; c < COLS; c++) {
          var n = L.barRows(lv[c], ROWS);
          if (n <= 0.05) continue;
          var bh = n * py;
          for (var rr = 0; rr < ROWS; rr++) {
            var seg = Math.min(py, bh - rr * py);
            if (seg <= 0) break;
            g.fillStyle = A.lightColor(theme, L.bandOf(rr, ROWS));
            g.fillRect((c + 0.5) * px - bw / 2, h - (rr + 1) * py + (py - seg), bw, seg);
          }
        }
        return;
      }

      for (var col = 0; col < COLS; col++) {
        var lit = L.litRows(lv[col], ROWS);
        for (var row = 0; row < lit; row++) {
          g.fillStyle = A.lightColor(theme, L.bandOf(row, ROWS));
          g.beginPath();
          g.arc((col + 0.5) * px, h - (row + 0.5) * py, r, 0, TAU);
          g.fill();
        }
      }
    },

    /* ── 氛围环 ─────────────────────────────────────────
     * 沿圆角矩形周界逐段描边：颜色按周长参数取值（彩虹是一条 360° 色相带，
     * 赛博是四角锚点插值），透明度按动态模式取值。
     * 静态 / 呼吸 / 律动 三种是**整圈同一透明度**，缓存成一张图后只改 globalAlpha；
     * 只有环绕是逐段透明度，才真的每段重画。 */
    _drawRing: function (look) {
      var rg = this._rg, rgc = this._ring;
      var pw = this._pw, ph = this._ph;
      if (!pw || !ph) return;
      var dpr = this._dpr;

      rg.setTransform(1, 0, 0, 1, 0, 0);
      rg.clearRect(0, 0, rgc.width, rgc.height);
      if (look.ambient === "off") return;
      rg.setTransform(dpr, 0, 0, dpr, 0, 0);

      var iw = pw - RING_INSET * 2, ih = ph - RING_INSET * 2;
      var rad = Math.max(0, 11 - RING_INSET);
      var key = pw + "x" + ph + "|" + look.ambient + "|" + dpr;
      if (key !== this._ringKey) {
        this._ringKey = key;
        var fr = A.anchorFractions(iw, ih, rad);
        var pts = [], cols = [];
        for (var i = 0; i < RING_SEG; i++) {
          var f = i / RING_SEG;
          var p = A.pointAt(f, iw, ih, rad);
          pts.push({ x: p.x + RING_INSET, y: p.y + RING_INSET });
          cols.push(A.ringColor(look.ambient, f, fr));
        }
        this._ringpts = pts;
        this._ringcol = cols;
        this._ringImg = this._bakeRing(pts, cols, pw, ph, dpr);
      }

      var now = (performance.now() - this._t0) / 1000;
      var dyn = look.ambientDyn;

      if (dyn !== "orbit") {
        rg.globalAlpha = A.ringAlpha(dyn, { t: now, rms: this._core.rms() });
        rg.setTransform(1, 0, 0, 1, 0, 0);
        rg.drawImage(this._ringImg, 0, 0);
        rg.globalAlpha = 1;
        rg.setTransform(dpr, 0, 0, dpr, 0, 0);
        return;
      }

      /* 环绕：亮斑沿周界行进（实测周期 2.0s） */
      var rms = this._core.rms();
      var pts2 = this._ringpts, cols2 = this._ringcol, n = pts2.length;
      rg.lineWidth = RING_W;
      rg.lineCap = "round";
      for (var k = 0; k < n; k++) {
        var al = A.ringAlpha("orbit", { t: now, frac: k / n, rms: rms });
        if (al < 0.02) continue;
        var a0 = pts2[k], a1 = pts2[(k + 1) % n];
        rg.globalAlpha = al;
        rg.strokeStyle = cols2[k];
        rg.beginPath();
        rg.moveTo(a0.x, a0.y);
        rg.lineTo(a1.x, a1.y);
        rg.stroke();
      }
      rg.globalAlpha = 1;
    },

    _bakeRing: function (pts, cols, pw, ph, dpr) {
      var off = document.createElement("canvas");
      off.width = Math.round(pw * dpr);
      off.height = Math.round(ph * dpr);
      var g = off.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.lineWidth = RING_W;
      g.lineCap = "round";
      for (var i = 0; i < pts.length; i++) {
        var a = pts[i], b = pts[(i + 1) % pts.length];
        g.strokeStyle = cols[i];
        g.beginPath();
        g.moveTo(a.x, a.y);
        g.lineTo(b.x, b.y);
        g.stroke();
      }
      return off;
    },

    /* ── 材质与面板样式同步 ───────────────────────────── */
    _syncLook: function (st, look) {
      var p = this._panel;
      if (!p) return;
      if (p.dataset.mat !== look.material) p.dataset.mat = look.material;
      if (look.material === "blur") {
        var url = st.cover && st.cover.url;
        /* 没有封面时**移除**变量，让 CSS 的默认渐变生效；
           设成 "none" 会把 background-image 也变成 none，整块底就空了。 */
        if (url) p.style.setProperty("--au-cover", 'url("' + url + '")');
        else p.style.removeProperty("--au-cover");
      }
    },

    /* ── 设置面板 ─────────────────────────────────────────
     * 用 position: fixed 而不是 absolute：模糊材质要裁背景（overflow hidden），
     * absolute 弹出层会被裁掉一半。fixed 不受祖先裁剪影响。 */
    _buildSettings: function () {
      var self = this;
      var pop = PVUI.el("div", "au-pop");
      pop.hidden = true;

      var ROWS_DEF = [
        { key: "lightTheme", label: "频谱灯光", table: A.LIGHT_THEMES },
        { key: "ambient", label: "氛围灯颜色", table: A.AMBIENT },
        { key: "ambientDyn", label: "氛围灯动态", table: A.DYNAMICS },
        { key: "material", label: "面板材质", table: A.MATERIALS }
      ];

      ROWS_DEF.forEach(function (row) {
        var line = PVUI.el("div", "au-row");
        line.appendChild(PVUI.el("span", "au-row-k", row.label));
        var chips = PVUI.el("div", "au-chips");
        Object.keys(row.table).forEach(function (k) {
          var b = PVUI.el("button", "au-chip", row.table[k].label);
          b.type = "button";
          b.dataset.k = k;
          b.dataset.row = row.key;
          b.addEventListener("click", function (ev) {
            ev.stopPropagation();
            var patch = {};
            patch[row.key] = k;
            self._core.setAppearance(patch);
            self._paintChips();
          });
          chips.appendChild(b);
        });
        line.appendChild(chips);
        pop.appendChild(line);
      });

      document.body.appendChild(pop);
      this._pop = pop;

      this._outside = function (ev) {
        if (pop.hidden) return;
        if (pop.contains(ev.target)) return;
        if (self._gear && self._gear.contains(ev.target)) return;
        self._toggleSettings(false);
      };
      document.addEventListener("pointerdown", this._outside, true);
    },

    _toggleSettings: function (force) {
      var pop = this._pop;
      if (!pop) return;
      var open = (force == null) ? pop.hidden : !!force;
      if (!open) { pop.hidden = true; return; }
      this._paintChips();
      pop.hidden = false;

      /* 先按「下方」定位，放不下就翻到上方，再夹进视口 */
      var r = this._panel.getBoundingClientRect();
      pop.style.left = "0px"; pop.style.top = "0px";
      var pw = pop.offsetWidth, phh = pop.offsetHeight;
      var left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - pw - 8));
      var top = r.bottom + 6;
      if (top + phh > window.innerHeight - 8) top = Math.max(8, r.top - phh - 6);
      pop.style.left = Math.round(left) + "px";
      pop.style.top = Math.round(top) + "px";
    },

    _paintChips: function () {
      var pop = this._pop;
      if (!pop || !this._core) return;
      var look = A.resolve(this._core.state.appearance);
      var chips = pop.querySelectorAll(".au-chip");
      for (var i = 0; i < chips.length; i++) {
        var b = chips[i];
        b.classList.toggle("on", look[b.dataset.row] === b.dataset.k);
      }
    },

    onState: function (s) {
      if (this._ui) this._ui.sync(s);
      /* 只标自己的宿主：一页两个播放器时会标错对象。 */
      if (this._host) this._host.classList.toggle("is-playing", !!s.playing);
      this._sig = "";          /* 模式/主题/尺寸可能在 state 里变，强制重画 */
      if (this._pop && !this._pop.hidden) this._paintChips();
    },

    resize: function () { this._sig = ""; this._ringKey = ""; }
  };

  window.PlayerCore.registerSkin("aurora", skin);
})();
