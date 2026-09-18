/* eventReducer.js — 纯函数：RuntimeEvent → 任务快照更新。无 IO、无副作用。 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};

  var EVENT_STATE = {
    "task.created": "submitted",
    "task.submitted": "submitted",
    "task.started": "running",
    "task.running": "running",
    "task.completed": "completed",
    "task.failed": "failed",
    "task.cancelled": "cancelled",
    "task.paused": "paused",
    "task.resumed": "running",
    "task.waiting_user": "waiting_user",
    "task.waiting_approval": "waiting_approval",
    "task.waited": "running",
    "task.approval.pending": "waiting_approval",
    "task.approval.approved": "running",
    "task.approval.denied": "failed",
    "task.recovered": "failed",
  };

  function emptySnapshot(taskId) {
    return {
      task_id: taskId,
      state: null,
      task: null,
      events: [],
      toolCalls: [],
      approvals: [],
      artifacts: [],
      lastReply: null,
      error: null,
      updated_at: null,
    };
  }

  /* 事件去重 key（stream 与轮询都可能拿到同一事件） */
  function eventKey(ev) {
    if (ev.event_id) return ev.event_id;
    return ev.type + "|" + (ev.created_at || "") + "|" +
      (ev.payload && (ev.payload.approval_id || ev.payload.id || "")) + "|" +
      (ev.payload && String(ev.payload.turn || ev.payload.tool || ev.payload.name || ""));
  }

  function nextState(snapshot, ev) {
    snapshot = snapshot || emptySnapshot("");
    if (ev.envelope && ev.envelope.visibility !== "public") return snapshot;
    if (ev.channel) {
      var publicSeen = snapshot.publicSeen || [];
      if (ev.event_id && publicSeen.indexOf(ev.event_id) >= 0) return snapshot;
      var next = Object.assign({}, snapshot, {publicSeen: publicSeen.concat(ev.event_id).slice(-5000)});
      if (ev.payload && ev.payload.activities) next.activities = ev.payload.activities;
      if (ev.channel === "assistant_delta") next.assistantText = (snapshot.assistantText || "") + (ev.payload.delta || "");
      if (ev.type === "assistant.reset") next.assistantText = "";
      if (ev.channel === "control" && /^run\./.test(ev.type)) next.state = ev.type === "run.waiting_for_user" ? "waiting_for_user" : ev.type.split(".")[1];
      return next;
    }
    // 幂等：同 key 事件（stream 重连/重复投递）整体忽略，防止覆盖终态或叠行
    var seenKeys = new Set(snapshot.events.map(eventKey));
    if (ev.type && seenKeys.has(eventKey(ev))) return snapshot;

    var s = {
      task_id: snapshot.task_id || ev.task_id || "",
      state: snapshot.state,
      task: snapshot.task,
      events: snapshot.events.slice(-499),
      toolCalls: snapshot.toolCalls ? snapshot.toolCalls.slice() : [],
      approvals: snapshot.approvals ? snapshot.approvals.slice() : [],
      artifacts: snapshot.artifacts ? snapshot.artifacts.slice() : [],
      lastReply: snapshot.lastReply,
      error: snapshot.error,
      updated_at: snapshot.updated_at,
    };
    s.events.push(ev);
    s.updated_at = ev.created_at || s.updated_at;
    if (EVENT_STATE[ev.type]) s.state = EVENT_STATE[ev.type];

    switch (ev.type) {
      case "task.created":
      case "task.started": {
        var p = ev.payload || {};
        s.state = EVENT_STATE[ev.type];
        if (s.task && s.task.id === s.task_id && p.goal && !s.task.goal) s.task.goal = p.goal;
        break;
      }
      case "tool.started": {
        var p2 = ev.payload || {};
        var call = {
          id: p2.call_id || ("call_" + s.toolCalls.length),
          tool: p2.name || p2.tool || "?",
          status: "running",
          args: p2.args || p2.arguments || "",
          detail: p2.summary || "",
        };
        var last = s.toolCalls[s.toolCalls.length - 1];
        if (last && last.tool === call.tool && last.status === "running") {
          // 同类工具连续重复调用：合并到同一条“执行中”记录，避免无限叠行
          last.detail = call.detail || last.detail;
        } else {
          s.toolCalls.push(call);
        }
        break;
      }
      case "tool.completed":
      case "tool.failed":
      case "tool.succeeded": {
        var p3 = ev.payload || {};
        var last = s.toolCalls[s.toolCalls.length - 1];
        if (last && (!p3.name || last.tool === (p3.name || p3.tool || last.tool))) {
          last.status = ev.type === "tool.failed" ? "failed" : "succeeded";
          last.detail = p3.summary || p3.output || p3.error || last.detail;
        }
        break;
      }
      case "assistant.reply": {
        s.lastReply = ev.payload;
        break;
      }
      case "approval.required": {
        var p4 = ev.payload || {};
        var list = Array.isArray(p4.approvals) ? p4.approvals : [];
        if (list.length) s.approvals = list.map(RT.types.Approval);
        else if (p4.approval) s.approvals = [RT.types.Approval(p4.approval)];
        s.state = "waiting_approval";
        break;
      }
      case "approval.resolved":
      case "task.approval.approved":
      case "task.approval.denied": {
        var p5 = ev.payload || {};
        if (p5.approval_id || p5.id) {
          s.approvals = s.approvals.filter(function (a) {
            return a.id !== (p5.approval_id || p5.id);
          });
        }
        break;
      }
      case "artifact.created": {
        var p6 = ev.payload || {};
        var art = RT.types.Artifact(p6.artifact || p6);
        if (art.id && !s.artifacts.some(function (x) { return x.id === art.id; })) {
          s.artifacts.push(art);
        }
        break;
      }
      case "task.completed": {
        s.toolCalls.forEach(function (t) { if (t.status === "running") t.status = "succeeded"; });
        break;
      }
      case "task.failed": {
        var pf = ev.payload || {};
        s.toolCalls.forEach(function (t) { if (t.status === "running") t.status = "failed"; });
        if (pf.message && !s.error) s.error = String(pf.message);
        break;
      }
      case "runtime.error": {
        var p7 = ev.payload || {};
        s.error = p7.message || p7.error || String(p7);
        if (p7.kind) s.state = "failed";
        break;
      }
      default:
        break;
    }
    return s;
  }

  function applyMany(snapshot, events) {
    var s = snapshot;
    (events || []).forEach(function (ev) { s = nextState(s, ev); });
    return s;
  }

  RT.eventReducer = {
    EVENT_STATE: EVENT_STATE,
    emptySnapshot: emptySnapshot,
    eventKey: eventKey,
    nextState: nextState,
    applyMany: applyMany,
  };
})(window);
