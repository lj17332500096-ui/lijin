/* api/index.js — Provider 选择器：真实 client 或 MockAdapter（同接口）。
 * 选择：URL ?backend=mock > localStorage['rt.backend'] > 默认真实后端。
 * 组件/Store 只 import RT.api，不关心背后实现。 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};

  RT.api = null;
  RT.apiBackend = "real";

  function backendFromUrl() {
    try {
      var p = new URLSearchParams(window.location.search);
      var v = p.get("backend");
      if (v === "mock" || v === "real") return v;
    } catch (e) { /* ignore */ }
    try {
      var s = localStorage.getItem("rt.backend");
      if (s === "mock" || s === "real") return s;
    } catch (e) { /* ignore */ }
    return "real";
  }

  function init() {
    RT.apiBackend = backendFromUrl();
    RT.api = RT.apiBackend === "mock" ? RT.apiMock : RT.apiClient;
    return RT.api;
  }

  if (!RT.api) init();
  RT.initApi = init;
})(window);
