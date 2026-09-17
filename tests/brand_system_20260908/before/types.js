/* types.js — Agent Runtime 数据模型（纯类型与守卫，无 UI / 无网络）。
 * 所有层（api / mock / reducer / 组件）只依赖这里的形状。 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};

  /* ---------- 状态 ---------- */
  var TaskState = Object.freeze({
    SUBMITTED: "submitted",
    RUNNING: "running",
    WAITING_TOOL: "waiting_tool",
    WAITING_USER: "waiting_user",
    WAITING_APPROVAL: "waiting_approval",
    WAITING_AUTH: "waiting_auth",
    PAUSED: "paused",
    COMPLETED: "completed",
    FAILED: "failed",
    CANCELLED: "cancelled",
  });

  var TERMINAL = new Set([TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED]);
  var WAITING = new Set([TaskState.WAITING_TOOL, TaskState.WAITING_USER, TaskState.WAITING_APPROVAL, TaskState.WAITING_AUTH]);

  function isKnownState(s) {
    return Object.values(TaskState).indexOf(s) !== -1;
  }
  function isTerminal(s) { return TERMINAL.has(s); }
  function isWaiting(s) { return WAITING.has(s); }
  function normalizeState(s, fallback) {
    return isKnownState(s) ? s : (fallback || TaskState.SUBMITTED);
  }

  /* ---------- 数值模型工厂（后端 JSON → 固定形状） ---------- */
  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }

  function Usage(src) {
    src = src || {};
    return {
      turns: num(src.turns),
      tool_calls: num(src.tool_calls),
      failures: num(src.failures),
      input_tokens: num(src.input_tokens),
      output_tokens: num(src.output_tokens),
      cost_usd: num(src.cost_usd),
    };
  }

  function Budget(src) {
    src = src || {};
    return {
      max_turns: src.max_turns != null ? num(src.max_turns) : null,
      max_tool_calls: src.max_tool_calls != null ? num(src.max_tool_calls) : null,
      max_wall_seconds: src.max_wall_seconds != null ? num(src.max_wall_seconds) : null,
    };
  }

  function Task(src) {
    src = src || {};
    return {
      id: String(src.id || ""),
      session_id: String(src.session_id || "personal"),
      goal: String(src.goal || ""),
      state: normalizeState(src.state),
      agent_name: String(src.agent_name || "assistant"),
      metadata: src.metadata && typeof src.metadata === "object" ? src.metadata : {},
      error: src.error || null,
      created_at: src.created_at || null,
      updated_at: src.updated_at || null,
      started_at: src.started_at || null,
      completed_at: src.completed_at || null,
      usage: Usage(src.usage),
      artifacts: Array.isArray(src.artifacts) ? src.artifacts.map(Artifact) : [],
      approvals_pending: num(src.approvals_pending),
      model_calls: num(src.model_calls),
      tool_calls_total: num(src.tool_calls_total),
      checkpoint: src.checkpoint ? Checkpoint(src.checkpoint) : null,
    };
  }

  function Checkpoint(src) {
    src = src || {};
    return {
      schema_version: num(src.schema_version),
      created_at: src.created_at || null,
      summary: String(src.summary || ""),
    };
  }

  function Artifact(src) {
    src = src || {};
    return {
      id: String(src.id || ""),
      task_id: src.task_id || null,
      session_id: src.session_id || null,
      name: String(src.name || ""),
      kind: String(src.kind || "file"),
      storage_path: String(src.storage_path || ""),
      sha256: String(src.sha256 || ""),
      size_bytes: num(src.size_bytes),
      created_at: src.created_at || null,
      exists: !!src.id,
    };
  }

  function Approval(src) {
    src = src || {};
    return {
      id: String(src.id || ""),
      task_id: String(src.task_id || ""),
      tool: String(src.tool || src.tool_name || ""),
      arguments: src.arguments && typeof src.arguments === "object" ? src.arguments : {},
      status: String(src.status || "pending"),
      actor: src.actor || null,
      reason: src.reason || null,
      created_at: src.created_at || null,
      decided_at: src.decided_at || null,
    };
  }

  function RuntimeEvent(src) {
    src = src || {};
    return {
      type: String(src.type || src.event || ""),
      payload: src.payload && typeof src.payload === "object" ? src.payload : {},
      created_at: src.created_at || null,
      task_id: String(src.task_id || (src.payload && src.payload.task_id) || ""),
    };
  }

  function ToolSpec(src) {
    src = src || {};
    return {
      name: String(src.name || ""),
      description: String(src.description || ""),
      category: String(src.category || "utility"),
      risk: String(src.risk || "low"),
      side_effect: !!src.side_effect,
      destructive: !!src.destructive,
      idempotent: !!src.idempotent,
      source: String(src.source || "native"),
    };
  }

  function ToolCall(src) {
    src = src || {};
    return {
      id: String(src.id || ""),
      task_id: String(src.task_id || ""),
      tool: String(src.tool || src.tool_name || ""),
      arguments: src.arguments || {},
      status: String(src.status || "succeeded"),
      result_excerpt: src.result_excerpt || null,
      created_at: src.created_at || null,
    };
  }

  RT.types = {
    TaskState: TaskState,
    isKnownState: isKnownState,
    isTerminal: isTerminal,
    isWaiting: isWaiting,
    normalizeState: normalizeState,
    Usage: Usage,
    Budget: Budget,
    Task: Task,
    Checkpoint: Checkpoint,
    Artifact: Artifact,
    Approval: Approval,
    RuntimeEvent: RuntimeEvent,
    ToolSpec: ToolSpec,
    ToolCall: ToolCall,
  };
})(window);
