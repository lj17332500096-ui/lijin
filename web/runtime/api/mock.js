/* api/mock.js — MockAdapter：与 client.js 同接口。
 * 用于无后端/演示；绝不把 mock 数据散落在组件里。
 * 切换：/runtime?backend=mock 或 localStorage['rt.backend']='mock'。 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};

  var db = {
    sessions: [{ id: "personal" }],
    messages: [],
    tasks: [
      {
        id: "task_mock01",
        session_id: "personal",
        title: "分析销售数据并生成报告",
        goal: "（Mock）分析 sales.xlsx 并生成 Word 报告",
        summary: "分析销售数据并生成报告",
        state: "completed",
        status: "completed",
        agent_name: "assistant",
        metadata: { channel: "mock" },
        usage: { turns: 6, tool_calls: 9, input_tokens: 18400, output_tokens: 2600, cost_usd: 0.18 },
        stat: {
          runs: 2, messages: 3,
          latest_run: { state: "completed", goal: "分析销售数据并生成报告", created_at: "2026-09-05T01:47:00+00:00" }
        },
        memory_scope: "project_only",
        instructions: "",
        sourcesCount: 1,
        work_location_id: null,
        artifacts: [
          { id: "art_mock01", task_id: "task_mock01", name: "Q3_sales_report.docx", kind: "word",
            storage_path: "mock://artifacts/Q3_sales_report.docx", sha256: "0".repeat(64),
            size_bytes: 49152, created_at: "2026-09-05T01:47:00+00:00" },
        ],
        created_at: "2026-09-05T01:40:00+00:00",
        updated_at: "2026-09-05T01:47:00+00:00",
      },
    ],
    approvals: [
      {
        id: "approv_mock01",
        task_id: "task_mock01",
        tool: "create_docx",
        arguments: { path: "/workspace/Q3_sales_report.docx" },
        status: "pending",
        created_at: "2026-09-05T01:45:00+00:00",
      },
    ],
    artifacts: [
      {
        id: "art_mock01",
        task_id: "task_mock01",
        session_id: "personal",
        name: "Q3_sales_report.docx",
        kind: "word",
        storage_path: "mock://artifacts/Q3_sales_report.docx",
        sha256: "0".repeat(64),
        size_bytes: 49152,
        created_at: "2026-09-05T01:47:00+00:00",
      },
    ],
    schedules: [],
    memories: [],
    tools: [
      { name: "read_excel", description: "读取 Excel 工作表", category: "office", risk: "low" },
      { name: "create_docx", description: "生成 Word 文档", category: "office", risk: "low", side_effect: true },
    ],
  };

  function delay(value, ms) {
    return new Promise(function (resolve) { setTimeout(function () { resolve(value); }, ms || 60); });
  }
  function clone(x) { return JSON.parse(JSON.stringify(x)); }
  function findTask(id) { return db.tasks.find(function (t) { return t.id === id; }); }

  RT.apiMock = {
    _db: db,
    listSessions: function () { return delay(clone(db.sessions)); },
    history: function () { return delay(clone(db.messages)); },

    listTasks: function (opts) {
      opts = opts || {};
      var rows = db.tasks.filter(function (t) {
        if (opts.state && t.state !== opts.state) return false;
        if (opts.session && t.session_id !== opts.session) return false;
        if (opts.archived !== undefined && opts.archived) return false;
        return true;
      }).slice(0, opts.limit || 50);
      return delay({ tasks: clone(rows), total: rows.length });
    },
    getTask: function (id) {
      var t = findTask(id);
      if (!t) return Promise.reject(Object.assign(new Error("task not found"), { status: 404 }));
      return delay({
        id: t.id, title: t.title || t.goal, summary: t.summary || "", status: t.status || t.state,
        session_id: t.session_id, created_at: t.created_at, updated_at: t.updated_at,
        memory_scope: t.memory_scope || "project_only", instructions: t.instructions || "",
        sourcesCount: (t.sources || []).length,
        work_location_id: (t.work_location_id != null ? t.work_location_id : null),
        work_location: null,
        stat: t.stat || { runs: 0, messages: 0 },
        goal: t.goal, artifacts: (t.artifacts || []).slice(),
        messages: clone(t.messages || []), runs: clone(t.runs || []),
      });
    },
    listRuns: function (opts) {
      opts = opts || {};
      var t = opts.task_id ? findTask(opts.task_id) : null;
      var rows = (t && t.runs) ? t.runs.slice(0, opts.limit || 50) : [];
      return delay({ runs: clone(rows), total: rows.length });
    },
    getRun: function (id) {
      return delay({
        id: id, state: "completed", goal: "", error: null, status: "succeeded",
        tool_calls: [], events: [], model_calls: [], artifacts: [],
        checkpoint: null,
      });
    },
    createProject: function (body) {
      body = body || {};
      var t = {
        id: "task_mock" + (1000 + Date.now() % 9000),
        session_id: body.session_id || "personal",
        title: body.name || "新任务", goal: body.name || "新任务", summary: "",
        state: "submitted", status: "submitted",
        stat: { runs: 0, messages: 0 },
        memory_scope: body.memory_scope || "project_only", instructions: body.instructions || "",
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        messages: [], runs: [], artifacts: [],
      };
      db.tasks.unshift(t);
      return delay({ id: t.id, title: t.title, session_id: t.session_id, task: t });
    },
    listMaterials: function () { return delay({ artifacts: [] }); },
    uploadMaterial: function () { return delay({ ok: true, artifact: { id: "mat_mock01", name: "mock.pdf" } }); },
    pinTask: function () { return delay({ ok: true }); },
    archiveTask: function () { return delay({ ok: true }); },
    deleteTask: function () { return delay({ ok: true }); },
    restoreTask: function () { return delay({ ok: true }); },
    addMessage: function () { return delay({ ok: true }); },
    taskEvents: function (id) { return delay([]); },
    listWorkLocations: function () { return delay([]); },
    createWorkLocation: function (name, localPath) {
      return delay({ id: "wl_mock01", name: name, local_path: localPath });
    },
    pickWorkLocation: function () { return delay({ ok: true, path: "C:/mock/workplace" }); },
    pauseTask: function () { return delay({ ok: true }); },
    resumeTask: function () { return delay({ ok: true }); },
    cancelTask: function () { return delay({ ok: true }); },

    listApprovals: function (opts) {
      opts = opts || {};
      var rows = db.approvals.filter(function (a) {
        if (opts.state && a.status !== opts.state) return false;
        if (opts.task_id && a.task_id !== opts.task_id) return false;
        return true;
      });
      return delay({ approvals: rows.map(RT.types.Approval), total: rows.length });
    },
    decideApproval: function (id, decision) {
      var a = db.approvals.find(function (x) { return x.id === id; });
      if (a) { a.status = decision; a.decided_at = new Date().toISOString(); }
      return delay({ ok: true, id: id, status: decision });
    },

    listArtifacts: function (opts) {
      opts = opts || {};
      var rows = db.artifacts.filter(function (a) {
        if (opts.task_id && a.task_id !== opts.task_id) return false;
        return true;
      });
      return delay({ artifacts: rows.map(RT.types.Artifact), total: rows.length });
    },
    getArtifact: function (id) {
      var a = db.artifacts.find(function (x) { return x.id === id; });
      if (!a) return Promise.reject(Object.assign(new Error("artifact not found"), { status: 404 }));
      return delay(RT.types.Artifact(a));
    },
    artifactDownloadUrl: function () { return "#mock-download"; },

    runtimeStatus: function () {
      return delay({ ok: true, db: { wal: true }, mcp_servers: ["mock-mcp"], tools_count: 2, service: "mock runtime" });
    },
    listTools: function () { return delay(db.tools.map(RT.types.ToolSpec)); },
    listSchedules: function () { return delay({ schedules: clone(db.schedules) }); },
    listMemories: function () { return delay({ memories: clone(db.memories) }); },
  };
})(window);
