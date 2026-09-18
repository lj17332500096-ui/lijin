/* api/client.js — 真实后端 API client（每个方法返回 typed 数据）。
 * 接口与 mock.js 完全一致：UI 只依赖 api/index.js 选出的 provider。 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};

  function enc(v) { return encodeURIComponent(v); }

  RT.apiClient = {
    /* ---- sessions / history（旧聊天兼容，保留供对话组件） ---- */
    listSessions: function () {
      return RT.http.get("/api/sessions").then(function (d) { return (d.sessions || []).slice(); });
    },
    history: function (session) {
      return RT.http.get("/api/history?session=" + enc(session || "personal")).then(function (d) {
        return (d.messages || []).slice();
      });
    },
    /* ---- tasks ---- */
    listTasks: function (opts) {
      opts = opts || {};
      var q = [];
      if (opts.session) q.push("session=" + enc(opts.session));
      if (opts.state) q.push("state=" + enc(opts.state));
      q.push("limit=" + (opts.limit || 50));
      return RT.http.get("/api/tasks?" + q.join("&")).then(function (d) {
        return { tasks: (d.tasks || []).map(RT.types.Task), total: d.total || 0 };
      });
    },
    getTask: function (id) {
      return RT.http.get("/api/tasks/" + enc(id)).then(RT.types.Task);
    },
    taskEvents: function (id) {
      return RT.http.get("/api/tasks/" + enc(id) + "/events").then(function (d) {
        return (d.events || []).map(RT.types.RuntimeEvent);
      });
    },
    pauseTask: function (id) { return RT.http.post("/api/tasks/" + enc(id) + "/pause"); },
    resumeTask: function (id) { return RT.http.post("/api/tasks/" + enc(id) + "/resume"); },
    cancelTask: function (id) { return RT.http.post("/api/tasks/" + enc(id) + "/cancel"); },
    /* ---- approvals ---- */
    listApprovals: function (opts) {
      opts = opts || {};
      var q = [];
      if (opts.state) q.push("state=" + enc(opts.state));
      if (opts.task_id) q.push("task_id=" + enc(opts.task_id));
      return RT.http.get("/api/approvals" + (q.length ? "?" + q.join("&") : "")).then(function (d) {
        return { approvals: (d.approvals || []).map(RT.types.Approval), total: d.total || 0 };
      });
    },
    decideApproval: function (id, decision) {
      return RT.http.post("/api/approval", { id: id, decision: decision });
    },
    /* ---- artifacts ---- */
    listArtifacts: function (opts) {
      opts = opts || {};
      var q = [];
      if (opts.task_id) q.push("task_id=" + enc(opts.task_id));
      if (opts.session_id) q.push("session_id=" + enc(opts.session_id));
      return RT.http.get("/api/artifacts" + (q.length ? "?" + q.join("&") : "")).then(function (d) {
        return { artifacts: (d.artifacts || []).map(RT.types.Artifact), total: d.total || 0 };
      });
    },
    getArtifact: function (id) {
      return RT.http.get("/api/artifacts/" + enc(id)).then(RT.types.Artifact);
    },
    artifactDownloadUrl: function (id) { return "/api/artifacts/" + enc(id) + "/download"; },
    uploadMaterial: function (file, name) {
      // 资料上传（原始文件体 + 文件名参数；无需 JSON）
      return fetch("/api/artifacts/upload?name=" + enc(name || file.name || "upload"), {
        method: "POST",
        body: file,
      }).then(function (r) { return r.json(); });
    },
    /* ---- runtime / meta ---- */
    runtimeStatus: function () { return RT.http.get("/api/runtime/status"); },
    listTools: function () {
      return RT.http.get("/api/tools").then(function (d) {
        return (d.tools || []).map(RT.types.ToolSpec);
      });
    },
    listSchedules: function () { return RT.http.get("/api/schedules"); },
    listMemories: function () { return RT.http.get("/api/memories"); },
        postSettingsMemory: function (enabled) {
      return RT.http.post("/api/settings/memory", { enabled: !!enabled });
    },
    postJson: function (path, body) {
      return RT.http.post(path, body || {});
    },


    /* ---- 新模型：Project / Task 容器 / Message / Run ---- */
    listProjects: function () {
      return RT.http.get("/api/projects").then(function (d) { return (d.projects || []).slice(); });
    },
    createProject: function (body) {
      return RT.http.post("/api/projects/create", body || {}).then(function (d) { return d.project; });
    },
    getProject: function (id) {
      return RT.http.get("/api/projects/" + enc(id));
    },
    updateProject: function (id, body) {
      return RT.http.post("/api/projects/" + enc(id) + "/update", body || {}).then(function (d) { return d.project; });
    },
    deleteProject: function (id) { return RT.http.post("/api/projects/" + enc(id) + "/delete"); },
    resetProject: function (id) { return RT.http.post("/api/projects/" + enc(id) + "/reset"); },
    projectStreamUrl: function (id) { return "/api/projects/" + enc(id) + "/stream"; },
    projectArtifacts: function (id) { return RT.http.get("/api/projects/" + enc(id) + "/artifacts"); },
    uploadProjectSource: function (id, file, name) {
      return fetch("/api/projects/" + enc(id) + "/sources/upload?name=" + enc(name || file.name || "upload"), {
        method: "POST", body: file,
      }).then(function (r) { return r.json(); });
    },
    uploadProjectAttachment: function (id, file, name) {
      return fetch("/api/projects/" + enc(id) + "/attachments/upload?name=" + enc(name || file.name || "upload"), {
        method: "POST", body: file,
      }).then(function (r) { return r.json(); });
    },
    promoteAttachment: function (pid, attId) {
      return RT.http.post("/api/projects/" + enc(pid) + "/attachments/" + enc(attId) + "/promote");
    },
    deleteAttachment: function (pid, attId) {
      return RT.http.post("/api/projects/" + enc(pid) + "/attachments/" + enc(attId) + "/delete");
    },
    createAttachmentRefs: function (pid, sourceIds) {
      return RT.http.post("/api/projects/" + enc(pid) + "/attachments/refs", { source_ids: sourceIds });
    },
    createProjectMessage: function (pid, content, attachments) {
      return RT.http.post("/api/projects/" + enc(pid) + "/messages/create", {
        content: content, attachments: attachments || [],
      });
    },
    /* v2026-09 主入口：POST 建 Run + 后台执行；GET stream 只订阅 */
    createRun: function (cid, message, attachments, clientMessageId) {
      return RT.http.post("/api/tasks/" + enc(cid) + "/runs", {
        message: message, attachments: attachments || [],
        client_message_id: clientMessageId || "",
      });
    },
    resumeRun: function (runId) {
      return RT.http.post("/api/runs/" + enc(runId) + "/resume");
    },
    deleteProjectSource: function (pid, sourceId) {
      return RT.http.post("/api/projects/" + enc(pid) + "/sources/" + enc(sourceId) + "/delete");
    },
    listWorkLocations: function () {
      return RT.http.get("/api/worklocations").then(function (d) { return d.work_locations || []; });
    },
    createWorkLocation: function (name, localPath) {
      return RT.http.post("/api/worklocations/create", { name: name, local_path: localPath })
        .then(function (d) { return d.work_location; });
    },
    pickWorkLocation: function () {
      return RT.http.post("/api/worklocations/pick", {}).then(function (d) {
        return d || {};
      });
    },
    createTask: function (body) {
      return RT.http.post("/api/tasks/create", body || {}).then(function (d) { return d.task; });
    },
    /* GET /api/tasks 现为 Task 容器列表 */
    listTasks: function (opts) {
      opts = opts || {};
      var q = [];
      if (opts.session) q.push("session=" + enc(opts.session));
      if (opts.project_id) q.push("project_id=" + enc(opts.project_id));
      if (opts.archived !== undefined) q.push("archived=" + (opts.archived ? "1" : "0"));
      q.push("limit=" + (opts.limit || 50));
      return RT.http.get("/api/tasks?" + q.join("&")).then(function (d) {
        return { tasks: (d.tasks || []).slice(), total: d.total || 0 };
      });
    },
    getTask: function (id) {
      return RT.http.get("/api/tasks/" + enc(id)).then(function (d) {
        return { id: d.id, title: d.title, summary: d.summary, status: d.status,
                 session_id: d.session_id, created_at: d.created_at, updated_at: d.updated_at,
                 memory_scope: d.memory_scope || "project_only", instructions: d.instructions || "",
                 sourcesCount: (d.sources || []).length,
                 work_location: d.work_location || null,
                 work_location_id: (d.work_location_id != null ? d.work_location_id : null),
                 stat: d.stat || { runs: 0, messages: 0 },
                 messages: (d.messages || []).slice(), runs: (d.runs || []).slice() };
      });
    },
    addMessage: function (id, content) {
      return RT.http.post("/api/tasks/" + enc(id) + "/messages/create", { content: content });
    },
    /* runs */
    listRuns: function (opts) {
      opts = opts || {};
      var q = [];
      if (opts.task_id) q.push("task_id=" + enc(opts.task_id));
      if (opts.state) q.push("state=" + enc(opts.state));
      q.push("limit=" + (opts.limit || 50));
      return RT.http.get("/api/runs?" + q.join("&")).then(function (d) {
        return { runs: (d.runs || []).slice(), total: d.total || 0 };
      });
    },
    getRun: function (id) {
      return RT.http.get("/api/runs/" + enc(id));
    },
    getRunDebug: function (id) {
      return RT.http.get("/api/runs/" + enc(id) + "?debug=1");
    },
    runAction: function (id, action) {
      return RT.http.post("/api/runs/" + enc(id) + "/" + action);
    },
    runStreamUrl: function (id) { return "/api/runs/" + enc(id) + "/stream"; },
    taskStreamUrl: function (id) {
      return "/api/tasks/" + enc(id) + "/stream";
    },
    search: function (q) {
      return RT.http.get("/api/search?q=" + enc(q)).then(function (d) {
        return (d.groups || []).slice();
      });
    },
    notifications: function () {
      return RT.http.get("/api/notifications").then(function (d) {
        return { items: d.items || [], unread: d.unread || 0 };
      });
    },
    deleteTask: function (id) { return RT.http.post("/api/tasks/" + enc(id) + "/delete"); },
    restoreTask: function (id) { return RT.http.post("/api/tasks/" + enc(id) + "/restore"); },
    archiveTask: function (id) { return RT.http.post("/api/tasks/" + enc(id) + "/archive"); },
    pinTask: function (id, pinned) { return RT.http.post("/api/tasks/" + enc(id) + "/pin", { pinned: !!pinned }); },
    legacyStreamUrl: function (session, message, opts) {
      var q = ["session=" + enc(session || "personal")];
      if (message) q.push("message=" + enc(message));
      opts = opts || {};
      if (opts.max_turns) q.push("max_turns=" + opts.max_turns);
      return "/api/stream?" + q.join("&");
    },
  };
})(window);
