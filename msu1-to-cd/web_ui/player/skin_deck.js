/* skin_deck.js —— 磁带机皮肤
 *
 * 走带物理来自 Re:Play 的实现（见 deck_physics.js 与
 * docs/deck/replay-逆向分析-2026-11-04.md §5.2–§5.3）：
 *   · 盘径比 = sqrt(1-0.72p) / sqrt(0.28+0.72p)，p 是当前曲目的进度
 *   · 角速度 ω = 线速度 / 半径 —— 空盘转得快
 *   · 一阶平滑，τ≈111ms —— 起停不是硬切
 *
 * 形态由 CSS 容器查询决定（player.css：只看**宽度**）：
 *   宽 ≥ 460px → 走带条（转换页 1260×200 的 dock）
 *   宽 < 460px → 竖置卡座（下载页 356×300 的右栏）
 *
 * 为什么用 rAF 而不是 CSS 动画：两个盘的角速度不同（同一个线速度、不同的
 * 半径），而且每帧都要跟着进度改盘径 —— CSS 只能给固定周期，做不到。
 */
(function () {
  "use strict";

  var DP = window.DeckPhysics;
  var REDUCED = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  var skin = {
    id: "deck",
    label: "磁带机",

    mount: function (host, core) {
      this._host = host;
      this._core = core;
      this._om = [0, 0];      /* 平滑后的角速度（rad/s） */
      this._ang = [0, 0];     /* 累计角度（rad） */
      this._last = 0;
      this._wrote = "";

      var chassis = PVUI.el("div", "dk-chassis");
      /* 结构：卷盘井 > 旋转层 > (带盘 + 轮毂)。
         旋转层单独一层，是为了让「井」的高光不跟着转 —— 光不会转。 */
      function reel(side) {
        return '<span class="dk-reel dk-reel--' + side + '">' +
          '<span class="dk-spin">' +
          '<span class="dk-pack"></span><span class="dk-hub"></span>' +
          '</span></span>';
      }
      chassis.innerHTML =
        '<div class="dk-window">' + reel("l") +
        '<span class="dk-tape"></span>' + reel("r") + '</div>' +
        '<div class="dk-plate"><span class="dk-brand">TAPE</span>' +
        '<span class="dk-strip"></span></div>';

      this._reels = [
        chassis.querySelector(".dk-reel--l"),
        chassis.querySelector(".dk-reel--r")
      ];

      /* 封面不进播放器（下载页有独立封面区，转换页没有封面源），
         这里只用一条「磁带贴纸」表达"有内容"，见方案 §5.3.3。 */
      this._ui = PVUI.mount(host, core, chassis, {
        skinLabel: "极光",
        onSwitchSkin: function () { core.setSkin("aurora"); }
      });

      var self = this;
      function tick(ts) {
        if (!self._host) return;          /* 卸载后自然停，不用额外标志位 */
        self._frame(ts);
        self._raf = requestAnimationFrame(tick);
      }
      this._raf = requestAnimationFrame(tick);
    },

    unmount: function () {
      if (this._raf) cancelAnimationFrame(this._raf);
      this._raf = 0;
      this._ui = null;
      this._host = null;
      this._core = null;
    },

    /* 每帧：进度 → 盘径 → 角速度 → 角度 */
    _frame: function (ts) {
      var s = this._core.state;
      var dt = this._last ? (ts - this._last) / 1000 : 0;
      this._last = ts;

      /* 时长未知（还没 loadedmetadata / 失效态）时用 0.5 这个中性值：
         两盘一样大，画面上不撒谎；绝不除以 0。 */
      var p = (s.dur > 0) ? Math.min(1, Math.max(0, s.pos / s.dur)) : 0.5;
      var rad = DP.reelRadius(p);

      var moving = !(REDUCED && REDUCED.matches) && s.playing && s.status !== "missing";
      var speed = moving ? DP.SPEED.playing : DP.SPEED.paused;

      var nx = DP.advance({ omega: this._om, angle: this._ang }, rad, speed, dt);
      this._om = nx.omega;
      this._ang = nx.angle;

      /* 只在数值真的变了才写样式：暂停且已停稳时，rAF 仍然跑但一次 DOM 都不碰。 */
      var r0 = rad[0].toFixed(3), r1 = rad[1].toFixed(3);
      var a0 = (nx.angle[0] * 180 / Math.PI).toFixed(1);
      var a1 = (nx.angle[1] * 180 / Math.PI).toFixed(1);
      var sig = r0 + "|" + r1 + "|" + a0 + "|" + a1;
      if (sig === this._wrote) return;
      this._wrote = sig;

      var e0 = this._reels[0], e1 = this._reels[1];
      e0.style.setProperty("--r", r0);
      e0.style.setProperty("--a", a0 + "deg");
      e1.style.setProperty("--r", r1);
      e1.style.setProperty("--a", a1 + "deg");
    },

    onState: function (s) {
      if (this._ui) this._ui.sync(s);
      /* 只标自己的宿主 —— document 级查询在一页两个播放器时会标错对象。 */
      if (this._host) this._host.classList.toggle("is-playing", !!s.playing);
    },

    resize: function () { /* 形态切换交给容器查询 */ }
  };

  window.PlayerCore.registerSkin("deck", skin);
})();
