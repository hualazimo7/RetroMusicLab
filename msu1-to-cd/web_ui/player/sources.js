/* sources.js —— 两个页面的数据适配器
 *
 * 皮肤永远不知道背后是 id 还是相对路径（见方案 §6.1）。
 * 两页的差异全部收在这里：
 *   转换页 = 会话内输出登记表，id 有效域 = 本次运行
 *   下载页 = 磁盘持久库，rel 有效域 = 永久，且带封面
 */
(function (global) {
  "use strict";

  function stripWav(s) {
    return String(s || "").replace(/\.wav$/i, "");
  }
  function tail(s) {
    var a = String(s || "").split(/[\\/]/);
    return a[a.length - 1] || "";
  }

  var Sources = {};

  /* 转换页：ctx = { outputs: () => [{id,name,size,...}] } */
  Sources.converter = function (ctx) {
    return {
      kind: "converter",                 /* 上次曲目键按页面隔离，见 core._lastKey */
      list: function () { return (ctx.outputs && ctx.outputs()) || []; },
      keyOf: function (o) { return "id:" + o.id; },
      urlOf: function (o) { return "/api/audio?id=" + o.id; },
      titleOf: function (o) { return stripWav(o.name); },
      subOf: function (o) { return o.size ? fmtSize(o.size) : ""; },
      resume: "session",
      caps: { prevNext: true, covers: false }
    };
  };

  /* 下载页：ctx = { rows: () => [试听列表项] }（= download.js 的 S.wavs） */
  Sources.download = function (ctx) {
    return {
      kind: "download",
      list: function () { return (ctx.rows && ctx.rows()) || []; },
      keyOf: function (o) { return "rel:" + o.rel; },
      urlOf: function (o) { return "/api/dl/audio?rel=" + encodeURIComponent(o.rel); },
      titleOf: function (o) { return stripWav(o.name || tail(o.rel)); },
      /* 条目字段：{ rel, name, game, size_text, fmt } —— 没有 platform，
         副标题用「游戏名 · 大小」，正是用户在列表里认曲子的两个依据。 */
      subOf: function (o) {
        return [o.game, o.size_text].filter(Boolean).join(" · ");
      },
      coverKeyOf: function (o) { return o.game || o.gameName || ""; },
      resume: "persistent",
      caps: { prevNext: true, covers: true },
      coverApi: {
        count: function (g) { return "/api/dl/covers?game=" + encodeURIComponent(g); },
        url: function (g, i) { return "/api/dl/cover?game=" + encodeURIComponent(g) + "&i=" + i; }
      }
    };
  };

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
    return n + " B";
  }

  global.PlayerSources = Sources;
})(window);
