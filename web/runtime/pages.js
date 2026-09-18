/* pages.js — 导航各页：全部由 RT.api 驱动（typed），无散落 mock。 */
(function (global) {
  "use strict";
  var RT = global.RT = global.RT || {};

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  var api = null;
  var containers = {};

  function findSection(page) {
    return document.getElementById("page-" + page);
  }

  function clearPage(page) {
    var sec = findSection(page);
    while (sec.firstChild) sec.removeChild(sec.firstChild);
    return sec;
  }

  function statePill(state) {
    var cls = RT.components.stateClass(state);
    return el("span", "pill small " + (cls || "blue"), RT.components.stateLabel(state));
  }

  function showError(sec, e) {
    sec.appendChild(el("div", "empty-note", "加载失败：" + ((e && e.message) || e) + "（可刷新重试）"));
  }

  /* ---- Tasks ---- */
  async function renderTasks() {
    var sec = clearPage("tasks");
    sec.appendChild(el("div", "page-scroll"));
    var scroll = sec.firstChild;
    var top = el("div", "page-top");
    top.appendChild(el("h2", "", "任务"));
    var span = el("span");
    span.id = "pageTasksCount";
    top.appendChild(span);
    var refresh = el("button", "ghost-btn refresh-btn", "刷新");
    refresh.onclick = renderTasks;
    top.appendChild(refresh);
    scroll.appendChild(top);
    var hint = el("div", "empty-note", "点击任务可切到工作台查看执行流、审批与产物。");
    scroll.appendChild(hint);
    var table = document.createElement("table");
    table.className = "ptable";
    var head = "<tr><th>状态</th><th>Task</th><th>目标</th><th>用量</th><th>更新时间</th></tr>";
    table.innerHTML = head;
    try {
      var data = await api.listTasks({ limit: 100 });
      var tbody = document.createElement("tbody");
      data.tasks.forEach(function (t) {
        var tr = document.createElement("tr");
        tr.className = "clickable";
        var tdState = el("td"); tdState.appendChild(statePill(t.state));
        var tdId = el("td", "mono", t.id);
        var tdGoal = el("td"); var goalB = el("b"); goalB.textContent = String(t.goal).slice(0, 120);
        tdGoal.appendChild(goalB);
        if (t.error) tdGoal.appendChild(el("div", "empty-note", "error: " + String(t.error).slice(0, 120)));
        var tdUsage = el("td", "", "turns " + t.usage.turns + " · tools " + t.usage.tool_calls + " · " + t.usage.input_tokens + "/" + t.usage.output_tokens + " tok");
        var tdTime = el("td", "mono", String(t.updated_at || "").replace("T", " ").slice(5, 19));
        [tdState, tdId, tdGoal, tdUsage, tdTime].forEach(function (c) { tr.appendChild(c); });
        tr.onclick = function () {
          if (RT.workspace) RT.workspace.openExisting(t.id);
          switchPage("workspace");
        };
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      scroll.appendChild(table);
      span.textContent = data.total + " 条";
    } catch (e) { showError(scroll, e); }
  }

  /* ---- Artifacts ---- */
  async function renderArtifacts() {
    var sec = clearPage("artifacts");
    sec.appendChild(el("div", "page-scroll"));
    var scroll = sec.firstChild;
    var top = el("div", "page-top");
    top.appendChild(el("h2", "", "产物"));
    var refresh = el("button", "ghost-btn refresh-btn", "刷新");
    refresh.onclick = renderArtifacts;
    top.appendChild(refresh);
    scroll.appendChild(top);
    var table = document.createElement("table");
    table.className = "ptable";
    table.innerHTML = "<tr><th>文件</th><th>kind</th><th>size</th><th>task</th><th>created</th><th></th></tr>";
    try {
      var data = await api.listArtifacts({ limit: 100 });
      var tbody = document.createElement("tbody");
      data.artifacts.forEach(function (a) {
        var tr = document.createElement("tr");
        tr.appendChild(el("td", "", a.name + ' <span class="verified">✓</span>'));
        tr.appendChild(el("td", "", a.kind));
        tr.appendChild(el("td", "", String(a.size_bytes || 0) + " B"));
        tr.appendChild(el("td", "mono", a.task_id || "—"));
        tr.appendChild(el("td", "mono", String(a.created_at || "").replace("T", " ").slice(5, 19)));
        var act = el("td");
        var dl = el("button", "ghost-btn", "下载");
        dl.onclick = function () { window.open(api.artifactDownloadUrl(a.id), "_blank"); };
        act.appendChild(dl);
        tr.appendChild(act);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      scroll.appendChild(table);
      if (!data.total) scroll.appendChild(el("div", "empty-note", "还没有产物：完成任务且真实落盘后会自动登记（服务器端 Artifact Registry）。"));
    } catch (e) { showError(scroll, e); }
  }

  /* ---- Approvals ---- */
  async function renderApprovals() {
    var sec = clearPage("approvals");
    sec.appendChild(el("div", "page-scroll"));
    var scroll = sec.firstChild;
    var top = el("div", "page-top");
    top.appendChild(el("h2", "", "权限与审批"));
    var refresh = el("button", "ghost-btn refresh-btn", "刷新");
    refresh.onclick = renderApprovals;
    top.appendChild(refresh);
    scroll.appendChild(top);
    var table = document.createElement("table");
    table.className = "ptable";
    table.innerHTML = "<tr><th>状态</th><th>approval</th><th>tool</th><th>task</th><th>时间</th><th></th></tr>";
    try {
      var data = await api.listApprovals({ limit: 100 });
      var tbody = document.createElement("tbody");
      data.approvals.forEach(function (a) {
        var tr = document.createElement("tr");
        var st = el("td");
        var pill = el("span", "pill small " + (a.status === "pending" ? "warn" : a.status === "approved" ? "" : "bad"), a.status);
        st.appendChild(pill);
        tr.appendChild(st);
        tr.appendChild(el("td", "mono", a.id));
        tr.appendChild(el("td", "", a.tool));
        var taskCell = el("td", "mono", a.task_id);
        tr.appendChild(taskCell);
        tr.appendChild(el("td", "mono", String(a.created_at || "").replace("T", " ").slice(5, 19)));
        var act = el("td");
        if (a.status === "pending") {
          var deny = el("button", "ghost-btn", "拒绝");
          deny.onclick = async function () { try { await api.decideApproval(a.id, "denied"); } catch (e) { alert(e.message); } renderApprovals(); };
          var allow = el("button", "primary-btn", "批准");
          allow.onclick = async function () { try { await api.decideApproval(a.id, "approved"); } catch (e) { alert(e.message); } renderApprovals(); };
          act.appendChild(deny);
          act.appendChild(allow);
        }
        tr.appendChild(act);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      scroll.appendChild(table);
      if (!data.total) scroll.appendChild(el("div", "empty-note", "没有审批记录。高风险操作（代码执行/删除记忆/删除任务等）会在这里出现。"));
    } catch (e) { showError(scroll, e); }
  }

  /* ---- Tools ---- */
  async function renderTools() {
    var sec = clearPage("tools");
    sec.appendChild(el("div", "page-scroll"));
    var scroll = sec.firstChild;
    scroll.appendChild(el("div", "page-top")).appendChild(el("h2", "", "工具与 MCP"));
    var groups = {};
    try {
      var data = await api.listTools();
      data.forEach(function (t) {
        if (!groups[t.category]) groups[t.category] = [];
        groups[t.category].push(t);
      });
      Object.keys(groups).sort().forEach(function (cat) {
        var head = el("div", "page-top");
        var h = el("h2", "", cat);
        h.style.fontSize = "13px";
        head.appendChild(h);
        scroll.appendChild(head);
        var row = el("div", "chip-row");
        groups[cat].forEach(function (t) {
          var chip = el("div", "tool-chip");
          var line = el("b");
          line.appendChild(el("span", "", t.name));
          var riskTag = el("span", "risk-tag-mini risk-" + t.risk, t.risk);
          line.appendChild(riskTag);
          chip.appendChild(line);
          chip.appendChild(el("small", "", String(t.description).slice(0, 120) || "（无描述）"));
          chip.appendChild(el("small", "", (t.source || "native") + (t.side_effect ? " · 副作用" : "") + (t.destructive ? " · 破坏性" : "")));
          row.appendChild(chip);
        });
        scroll.appendChild(row);
      });
    } catch (e) { showError(scroll, e); }
  }

  /* ---- Memory ---- */
  async function renderMemory() {
    var sec = clearPage("memory");
    sec.appendChild(el("div", "page-scroll"));
    var scroll = sec.firstChild;
    var top = el("div", "page-top");
    top.appendChild(el("h2", "", "长期记忆"));
    var refresh = el("button", "ghost-btn refresh-btn", "刷新");
    refresh.onclick = renderMemory;
    top.appendChild(refresh);
    scroll.appendChild(top);
    var table = document.createElement("table");
    table.className = "ptable";
    table.innerHTML = "<tr><th>内容</th><th>类型</th><th>tags</th><th>source</th><th>confidence</th></tr>";
    try {
      var data = await api.listMemories();
      var memories = (data && data.memories) || [];
      var tbody = document.createElement("tbody");
      memories.forEach(function (m) {
        var tr = document.createElement("tr");
        tr.appendChild(el("td", "", String(m.text || "").slice(0, 200)));
        tr.appendChild(el("td", "", m.memory_type || "semantic"));
        tr.appendChild(el("td", "", Array.isArray(m.tags) ? m.tags.join("、") : ""));
        tr.appendChild(el("td", "", m.source || "agent-tool"));
        tr.appendChild(el("td", "", String(m.confidence != null ? m.confidence : 1.0)));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      scroll.appendChild(table);
      if (!memories.length) scroll.appendChild(el("div", "empty-note", "还没有长期记忆。告诉 Agent 你的偏好/目标，它会在需要时用 remember 存到这里。"));
    } catch (e) { showError(scroll, e); }
  }

  /* ---- Schedules ---- */
  async function renderSchedules() {
    var sec = clearPage("schedules");
    sec.appendChild(el("div", "page-scroll"));
    var scroll = sec.firstChild;
    var top = el("div", "page-top");
    top.appendChild(el("h2", "", "计划任务"));
    var refresh = el("button", "ghost-btn refresh-btn", "刷新");
    refresh.onclick = renderSchedules;
    top.appendChild(refresh);
    scroll.appendChild(top);
    var table = document.createElement("table");
    table.className = "ptable";
    table.innerHTML = "<tr><th>id</th><th>名称</th><th>计划</th><th>状态</th><th>下次运行</th></tr>";
    try {
      var data = await api.listSchedules();
      var schedules = (data && data.schedules) || [];
      var tbody = document.createElement("tbody");
      schedules.forEach(function (s) {
        var tr = document.createElement("tr");
        tr.appendChild(el("td", "mono", s.id));
        tr.appendChild(el("td", "", s.name));
        tr.appendChild(el("td", "mono", s.schedule || ""));
        tr.appendChild(el("td", "", s.enabled ? "启用" : "停用"));
        tr.appendChild(el("td", "mono", String(s.next_run || "—").replace("T", " ").slice(0, 19)));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      scroll.appendChild(table);
      if (!schedules.length) scroll.appendChild(el("div", "empty-note", "暂无计划任务（聊天里说“每天早上 9 点提醒我”即可创建；daemon 常驻执行）。"));
    } catch (e) { showError(scroll, e); }
  }

  /* ---- Trace（工作台当前任务的简要轨迹） ---- */
  async function renderTrace() {
    var sec = clearPage("trace");
    sec.appendChild(el("div", "page-scroll"));
    var scroll = sec.firstChild;
    var top = el("div", "page-top");
    top.appendChild(el("h2", "", "Trace"));
    scroll.appendChild(top);
    var activeId = RT.workspace ? RT.workspace.activeId() : null;
    if (!activeId) {
      scroll.appendChild(el("div", "empty-note", "工作台没有活动任务；打开一个任务后这里会显示它的运行轨迹。"));
      return;
    }
    try {
      var events = await api.taskEvents(activeId);
      scroll.appendChild(el("div", "kv-grid"));
      var grid = scroll.lastChild;
      grid.appendChild(el("span", "", "task"));
      grid.appendChild(el("span", "", activeId));
      if (!events.length) {
        scroll.appendChild(el("div", "empty-note", "该任务还没有事件记录。"));
        return;
      }
      var stack = el("div", "rt-stack");
      var head = el("div", "rt-head", "task.run");
      stack.appendChild(head);
      events.forEach(function (ev) {
        var row = el("div", "rt-item");
        row.appendChild(el("span", "rt-ico", "·"));
        var name = el("div", "rt-name", ev.type);
        var detail = (function () { try { return JSON.stringify(ev.payload).slice(0, 160); } catch (e) { return ""; } })();
        if (detail) name.appendChild(el("small", "", detail));
        row.appendChild(name);
        row.appendChild(el("span", "rt-status", String(ev.created_at || "").replace("T", " ").slice(5, 19)));
        stack.appendChild(row);
      });
      scroll.appendChild(stack);
    } catch (e) { showError(scroll, e); }
  }

  /* ---- Settings（静态信息 + 后端切换；section 已移除时为 no-op） ---- */
  function renderSettings() {
    var sec = findSection("settings");
    if (!sec) return;
    while (sec.firstChild) sec.removeChild(sec.firstChild);
    var scroll = el("div", "page-scroll");
    sec.appendChild(scroll);
    scroll.appendChild(el("div", "page-top")).appendChild(el("h2", "", "设置"));
    var panel = el("div", "settings-panel");
    panel.appendChild(el("div", "settings-row", "")).appendChild(el("b", "", "UI"));
    var row2 = el("div", "settings-row");
    row2.appendChild(el("b", "", "版本"));
    row2.appendChild(el("span", "", "此刻 · NOW · 本地个人助手"));
    panel.appendChild(row2);
    var row3 = el("div", "settings-row");
    row3.appendChild(el("b", "", "API 后端"));
    row3.appendChild(el("span", "", RT.apiBackend === "mock" ? "演示数据" : "连接本地服务（/api/*）"));
    panel.appendChild(row3);
    var rowDb = el("div", "settings-row");
    rowDb.appendChild(el("b", "", "SQLite"));
    rowDb.appendChild(el("span", "", "agent.db (WAL) · tasks/events/approvals/artifacts/audit/memories"));
    panel.appendChild(rowDb);
    var rowMode = el("div", "settings-row");
    rowMode.appendChild(el("b", "", "后端切换"));
    var btns = el("div", "");
    var realBtn = el("button", "ghost-btn", "真实后端");
    realBtn.onclick = function () { try { localStorage.setItem("rt.backend", "real"); } catch (e) {} location.reload(); };
    var mockBtn = el("button", "ghost-btn", "Mock 后端");
    mockBtn.onclick = function () { try { localStorage.setItem("rt.backend", "mock"); } catch (e) {} location.reload(); };
    btns.appendChild(realBtn);
    btns.appendChild(mockBtn);
    rowMode.appendChild(btns);
    panel.appendChild(rowMode);
    var rowAbout = el("div", "settings-row");
    rowAbout.appendChild(el("b", "", "分层"));
    rowAbout.appendChild(el("span", "", "UI → api(client|mock) → SSE eventClient → reducer → taskStore；UI 不直读任意 JSON"));
    panel.appendChild(rowAbout);
    scroll.appendChild(panel);
  }

  var RENDERERS = {
    tasks: renderTasks,
    artifacts: renderArtifacts,
    approvals: renderApprovals,
    tools: renderTools,
    memory: renderMemory,
    schedules: renderSchedules,
    trace: renderTrace,
  };

  function initPages(apiRef) {
    api = apiRef || RT.api;
    renderSettings();
    return { onShow: function (page) { if (RENDERERS[page]) RENDERERS[page](); } };
  }

  RT.initPages = initPages;
})(window);
