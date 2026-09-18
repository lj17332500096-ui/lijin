/* api/http.js — 底层 HTTP 封装（JSON + 错误规范化）。不包含任何业务路径。 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};
  RT.http = {
    baseUrl: function () {
      return ""; // 同源；可在此加前缀/环境变量
    },
    async request(method, path, body) {
      var opts = { method: method, headers: { "Content-Type": "application/json" } };
      if (body !== undefined && body !== null) opts.body = JSON.stringify(body);
      var resp = await fetch(this.baseUrl() + path, opts);
      var text = await resp.text();
      var data = null;
      if (text) {
        try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
      }
      if (!resp.ok) {
        var err = new Error((data && data.error) || ("HTTP " + resp.status));
        err.status = resp.status;
        err.data = data;
        throw err;
      }
      return data;
    },
    get: function (path) { return this.request("GET", path); },
    post: function (path, body) { return this.request("POST", path, body || {}); },
  };
})(window);
