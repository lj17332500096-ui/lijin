/* eventClient.js — SSE 统一解析（run.* 新事件 + legacy 兼容）+ 可选自动重连。
 *
 * start(options):
 *   { url?: string                      // 显式 SSE 地址（推荐：由 workspace 拼好）
 *     handlers: { onEvent, onStatus }   // onEvent(归一 RuntimeEvent)；onStatus(status,detail)
 *     autoReconnect?: boolean }         // 默认 true
 *
 * 2026-09 语义：SSE 流只做「订阅」（GET 无任何副作用）。断连 ≠ 取消：
 * 服务端 Run 继续执行；重新订阅同一 run_id 即可续看（历史回放 + 实时）。
 * 取消必须走 POST /api/runs/{id}/cancel。
 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};

  var LEGACY_MAP = {
    tool: "tool.started",
    reply: "assistant.reply",
    approval: "approval.required",
    error: "runtime.error",
    done: "runtime.done",
  };

  var NAMED_EVENTS = [
    "activity", "assistant_delta", "control",
    "task.started", "task.completed", "task.failed", "task.cancelled",
    "run.started", "run.completed", "run.failed", "run.waiting_approval",
    "run.cancelled", "tool", "reply", "reply_delta", "stream_reset", "approval",
    "source.not_ready", "error", "done",
  ];

  function normalizeEvent(name, data) {
    var payload = data && typeof data === "object" ? data : {};
    if (["activity", "assistant_delta", "control"].indexOf(name) >= 0) {
      if (payload.visibility !== "public" || payload.channel !== name) return null;
      return { type: payload.type, channel: name, event_id: payload.event_id,
        task_id: payload.run_id, created_at: payload.timestamp, envelope: payload,
        payload: Object.assign({}, payload.metadata || {}, {run_id: payload.run_id,
          status: payload.status, label: payload.label}) };
    }
    var type = LEGACY_MAP[name] || String(name || "");
    return RT.types.RuntimeEvent({
      type: type,
      payload: payload,
      task_id: payload.task_id || payload.run_id || "",
      created_at: payload.created_at || null,
    });
  }

  RT.eventClient = {
    normalizeEvent: normalizeEvent,

    parseSse(text) {
      var name = "message";
      var dataLines = [];
      String(text || "").split("\n").forEach(function (line) {
        if (line.indexOf("event:") === 0) name = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) dataLines.push(line.slice(5).trim());
      });
      if (!dataLines.length) return null;
      var data;
      try { data = JSON.parse(dataLines.join("\n")); } catch (e) { return null; }
      return normalizeEvent(name, data);
    },

    start(options) {
      var handlers = options.handlers || {};
      var stopped = false;
      var es = null;
      var attempts = 0;
      var maxAttempts = handlers.maxAttempts || 4;
      var autoReconnect = options.autoReconnect !== false;
      var url = options.url || "/api/stream";

      function dispatch(event) {
        if (!event) return;
        if (handlers.onEvent) handlers.onEvent(event);
        if (event && event.type === "runtime.done") {
          stopped = true;
          if (es) { es.close(); es = null; }
        }
      }

      function open() {
        if (stopped) return;
        if (!autoReconnect) {
          attempts += 1;
          if (attempts > 1) {
            if (handlers.onStatus) handlers.onStatus("closed", "连接已断开（本流不自动重连）");
            return;
          }
        } else {
          attempts += 1;
          if (attempts > maxAttempts) {
            if (handlers.onStatus) handlers.onStatus("error", "超过最大重连次数");
            return;
          }
          if (attempts > 1 && handlers.onStatus) handlers.onStatus("reconnecting", attempts);
        }
        es = new EventSource(url);
        es.onopen = function () {
          attempts = 0;
          if (handlers.onStatus) handlers.onStatus("open");
        };
        es.onmessage = function (ev) {
          var event = RT.eventClient.parseSse(ev.data);
          dispatch(event);
        };
        NAMED_EVENTS.forEach(function (name) {
          es.addEventListener(name, function (ev) {
            var event = RT.eventClient.normalizeEvent(name, (function () {
              try { return JSON.parse(ev.data); } catch (e) { return null; }
            })());
            dispatch(event);
          });
        });
        es.onerror = function () {
          if (stopped) return;
          if (es) { es.close(); es = null; }
          if (handlers.onStatus) handlers.onStatus("closed");
          if (!autoReconnect) return; // message/run 流：服务端已取消执行，交给 UI 处理
          var delay = Math.min(12000, 800 * Math.pow(2, attempts - 1));
          setTimeout(open, delay);
        };
      }
      open();

      return {
        stop: function () {
          stopped = true;
          if (es) { es.close(); es = null; }
        },
      };
    },
  };
})(window);
