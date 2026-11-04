/* ui.js —— 两套皮肤共用的传输控件
 *
 * 只做「读 core.state → 更新 DOM」和「DOM 事件 → 调 core」。
 * 不碰 audio/viz 节点图，不写 localStorage，不发请求（方案 §4.3 不变量）。
 */
(function (global) {
  "use strict";

  var ICON = {
    play: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8.5 5.6v12.8L18.6 12Z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5h3v13H8zM13 5.5h3v13h-3z"/></svg>',
    prev: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 6v12M17 6.8v10.4L9.2 12Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"/></svg>',
    next: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M17 6v12M7 6.8v10.4L14.8 12Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"/></svg>'
  };

  function fmt(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function el(tag, cls, html) {
    var d = document.createElement(tag);
    if (cls) d.className = cls;
    if (html != null) d.innerHTML = html;
    return d;
  }

  /* 在 host 里按 [signature][main][ctrl] 三段布局，返回 sync(state) */
  function mount(host, core, signature, opts) {
    opts = opts || {};
    var body = el("div", "pv-body");

    var main = el("div", "pv-main");
    var title = el("div", "pv-title");
    var sub = el("div", "pv-sub");
    var track = el("div", "pv-track");
    var fill = el("div", "pv-fill");
    var knob = el("div", "pv-knob");
    track.appendChild(fill);
    track.appendChild(knob);
    var time = el("div", "pv-time");
    main.appendChild(title);
    main.appendChild(sub);
    main.appendChild(track);
    main.appendChild(time);

    var ctrl = el("div", "pv-ctrl");
    /* ⚠ 两个跳曲键必须各有自己的类：只给 pv-btn--sm 的话，皮肤没法分别
       处理（比如只在窄版隐藏上一首），自动化也定位不到。 */
    var bPrev = el("button", "pv-btn pv-btn--sm pv-btn--prev", ICON.prev);
    var bPlay = el("button", "pv-btn pv-btn--play", ICON.play);
    var bNext = el("button", "pv-btn pv-btn--sm pv-btn--next", ICON.next);
    bPrev.title = "上一首";
    bNext.title = "下一首";
    bPlay.title = "播放 / 暂停";
    var vol = document.createElement("input");
    vol.type = "range"; vol.min = "0"; vol.max = "100"; vol.step = "1";
    vol.className = "pv-vol"; vol.title = "音量";
    var bSkin = el("button", "pv-btn pv-btn--skin", opts.skinLabel || "换肤");
    bSkin.title = "切换风格";

    ctrl.appendChild(bPrev);
    ctrl.appendChild(bPlay);
    ctrl.appendChild(bNext);
    ctrl.appendChild(vol);
    ctrl.appendChild(bSkin);

    if (signature) body.appendChild(signature);
    body.appendChild(main);
    body.appendChild(ctrl);
    host.appendChild(body);

    /* ── 事件 → core ── */
    bPlay.addEventListener("click", function () { core.toggle(); });
    bPrev.addEventListener("click", function () { core.prev(); });
    bNext.addEventListener("click", function () { core.next(false); });
    vol.addEventListener("input", function () { core.setVolume(vol.value / 100); });
    bSkin.addEventListener("click", function () {
      if (opts.onSwitchSkin) opts.onSwitchSkin();
    });

    /* 进度拖拽 */
    var dragging = false;
    function ratioFromEvent(e) {
      var r = track.getBoundingClientRect();
      return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    }
    function seekTo(e) {
      var d = core.state.dur;
      if (!d) return;
      core.seek(ratioFromEvent(e) * d);
    }
    track.addEventListener("pointerdown", function (e) {
      if (!core.state.dur) return;
      dragging = true;
      track.setPointerCapture(e.pointerId);
      seekTo(e);
    });
    track.addEventListener("pointermove", function (e) { if (dragging) seekTo(e); });
    track.addEventListener("pointerup", function (e) {
      dragging = false;
      try { track.releasePointerCapture(e.pointerId); } catch (err) { /* noop */ }
    });

    /* ── state → DOM ── */
    var lastSec = -1;
    function sync(s) {
      title.textContent = s.status === "missing" ? "文件已不在了" : (s.name || "未选择曲目");
      title.classList.toggle("is-missing", s.status === "missing");
      sub.textContent = s.status === "missing" ? "它在磁盘上被移动或删除了" : (s.sub || "");

      var d = s.dur || 0;
      var p = d > 0 ? Math.max(0, Math.min(1, s.pos / d)) : 0;
      fill.style.width = (p * 100) + "%";
      knob.style.left = (p * 100) + "%";
      track.classList.toggle("is-unknown", !d);

      var sec = Math.floor(s.pos);
      if (sec !== lastSec || s.dur !== sync._d) {
        lastSec = sec; sync._d = s.dur;
        time.textContent = fmt(s.pos) + " / " + (d ? fmt(d) : "--:--");
      }

      bPlay.innerHTML = s.playing ? ICON.pause : ICON.play;
      bPrev.disabled = !s.caps.prevNext || !s.canPrev;
      bNext.disabled = !s.caps.prevNext || !s.canNext;
      bPlay.disabled = s.status === "missing" || s.empty;

      if (document.activeElement !== vol) vol.value = String(Math.round((s.volume || 0) * 100));

      /* 封面：有就画，没有就交给皮肤的降级样式 */
      if (opts.onCover) opts.onCover(s.cover);
    }

    return { sync: sync, body: body, main: main, ctrl: ctrl, title: title, sub: sub, track: track };
  }

  global.PVUI = { mount: mount, fmt: fmt, icon: ICON, el: el };
})(window);
