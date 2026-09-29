/* workspace.js — FORGE 下一版 UIUX 主控（P0 ×5 + 第二阶段 P0）。
 *
 * 心智（《下一版 UIUX 的正式修改方案》）：
 *   "告诉 FORGE 你想完成什么"。用户不需要理解 Agent/Run/Event/Project……
 *   - 默认进入「项目」列表（开始工作入口 = 左栏「＋ 新项目」+ 各项目对话）
 *   - 左栏：FORGE + 新项目 + 最近项目（项目即对话），资料/设置走二级入口
 *   - 任务页：进行中 / 等你确认 / 已完成；执行过程三层信息（过程→详情→技术详情）
 *   - 状态语言：running=正在处理 / waiting_approval=等你确认 / paused=已暂停
 *     failed=遇到问题 / cancelled=已停止 / completed=已完成（绝不出现 Run/Approval 等词）
 *   - 交付卡（完成态）+ 失败恢复卡（遇到问题）
 *   - 项目=顶部「工作位置」；Inspector=按需 Drawer（文件/修改/运行记录/高级信息）
 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};
  var api = null;
  var $ = function (id) { return document.getElementById(id); };

  var state = {
    containers: [],
    active: null,          // 当前打开的容器 detail
    runCache: {},          // runId -> getRun payload
    live: null,            // { stream, runId, containerId }
    page: "tasklist",      // tasklist | materials | settings | task
    materials: { artifacts: [], memories: [] },
    config: {},           // /api/runtime/status.config（只读高级设置）
    configFetched: false, // 高级设置只拉取一次的闸
    drawerRunId: null,     // Drawer 当前选中的 Run
    drawerTab: "activity", // files | diff | tests | activity
    showAllRuns: false,    // 任务页是否展开全部历史轮次
  };

  /* ---------- 小工具 ---------- */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function esc(t) { return String(t == null ? "" : t); }

  function st(state) { return RT.activity.userState(state); }
  function pill(rawState) {
    var s = st(rawState);
    return el("span", "pill small " + s.cls, s.label);
  }
  function relTime(iso) {
    if (!iso) return "";
    var t = new Date(iso).getTime();
    if (isNaN(t)) return "";
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  }

  /* ---------- 主题（外观）实现位于设置页段落；初始应用一次 ---------- */
  function applyThemeInit() {
    var pref = localStorage.getItem("forge.theme.main") || "dark";
    var dark = pref === "auto"
      ? !(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches)
      : pref === "dark";
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    // 跟随系统：系统主题变化实时切换（仅 auto 模式）
    try {
      var mq = window.matchMedia("(prefers-color-scheme: light)");
      var onSys = function () {
        if (themeSetting() === "auto") applyTheme();
      };
      if (mq.addEventListener) mq.addEventListener("change", onSys);
      else if (mq.addListener) mq.addListener(onSys);
    } catch (e) {}
  }

  /* ---------- 对话气泡（assistant 走 Markdown 渲染器；user 纯文本 + 附件 chips） ---------- */
  function messageRow(m) {
    var row = el("div", "msg-row " + (m.role === "user" ? "user" : "assistant"));
    var bubble = el("div", "bubble");
    var full = String(m.content == null ? "" : m.content).trim();
    if (m.role === "user") {
      if (!full) full = "（历史轮次）";
      bubble.appendChild(el("div", "msg-text", full));
      var atts = (m.attachments || []);
      if (atts.length) {
        var attList = el("div", "msg-att-list");
        atts.forEach(function (a) {
          var chip = el("span", "att-chip");
          chip.appendChild(el("span", "att-name", (a.display_name || "").slice(0, 40)));
          var scopeNote = a.attachment_scope === "project_source" ? "项目来源" : "仅本次使用";
          chip.appendChild(el("span", "att-state" + (a.attachment_scope === "project_source" ? " src" : ""), scopeNote));
          attList.appendChild(chip);
        });
        bubble.appendChild(attList);
      }
    } else {
      var md = el("div", "agent-markdown");
      RT.markdown.renderTo(md, full);
      bubble.appendChild(md);
    }
    row.appendChild(bubble);
    return row;
  }

  /* 历史轮次（无消息记录）用 run.goal 补一帧「你说」 */
  function legacyGoalRow(run) {
    var row = el("div", "msg-row user");
    var bubble = el("div", "bubble");
    var body = el("div", "msg-text", run.goal || "（无记录）");
    bubble.appendChild(body);
    row.appendChild(bubble);
    return row;
  }

  /* ================= 左栏 ================= */
  function refreshRail() {
    var rail0 = $("railTasks");
    if (rail0 && !containersFilled) {
      clear(rail0);
      rail0.appendChild(skeletonRows(4, true));
    }
    var opts = { limit: 100 };
    if (currentProjectId) opts.project_id = currentProjectId;
    return api.listTasks(opts).then(function (d) {
      containersFilled = true;
      state.containers = d.tasks || [];
      [["allTaskCount", null], ["runningTaskCount", ["running", "submitted", "waiting_tool"]], ["waitingTaskCount", ["waiting_approval", "paused", "waiting_user", "waiting_auth"]]].forEach(function (spec) {
        var count = state.containers.filter(function (c) { return !spec[1] || spec[1].indexOf(c.stat && c.stat.latest_run ? c.stat.latest_run.state : "") >= 0; }).length;
        if ($(spec[0])) $(spec[0]).textContent = count;
      });
      var rail = $("railTasks");
      if (!rail) return;
      clear(rail);
      var shown = 0;
      state.containers.forEach(function (c) {
        if (shown >= 12) return;
        var item = el("div", "rail-item" + (state.page === "task" && state.active && state.active.id === c.id ? " active" : ""));
        item.title = c.title || "未命名任务";
        item.dataset.taskId = String(c.id);
        item.setAttribute("role", "button"); item.tabIndex = 0;
        item.addEventListener("keydown", function (e) { if (e.target === item && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openTask(c.id); } });
        item.addEventListener("click", function () { openTask(c.id); });
        var title = el("div", "rail-title", c.title || "未命名项目");
        var meta = el("div", "rail-meta");
        var latest = c.stat && c.stat.latest_run ? c.stat.latest_run.state : "";
        if (latest) {
          var s = st(latest);
          meta.appendChild(el("span", "rail-dot " + s.cls));
        }
        var showLabel = { running: "正在处理", submitted: "正在处理", waiting_approval: "等你确认",
                          paused: "已暂停", failed: "遇到问题" }[latest || ""];
        meta.appendChild(document.createTextNode(
          " " + relTime(c.updated_at) + (showLabel ? (" · " + showLabel) : "")));
        item.appendChild(title);
        item.appendChild(meta);
        // 收藏 / 删除（hover 操作；不冒泡避免打开任务）
        var ops = el("div", "rail-ops");
        var star = el("button", "rail-star" + (c.pinned ? " on" : ""));
        star.title = c.pinned ? "取消收藏" : "收藏置顶";
        star.innerHTML = RT.icon("star", 15);
        star.addEventListener("click", function (ev) {
          ev.stopPropagation();
          api.pinTask(c.id, !c.pinned).then(function () {
            showToast(c.pinned ? "已取消收藏" : "已收藏置顶");
            refreshRail();
          }).catch(function () { showToast("操作失败"); });
        });
        var del = el("button", "rail-del");
        del.title = "归档到回收站";
        del.innerHTML = RT.icon("x", 15);
        del.addEventListener("click", function (ev) {
          ev.stopPropagation();
          api.archiveTask(c.id).then(function () {
            showToast("已归档到回收站");
            if (state.active && state.active.id === c.id) {
              state.active = null;
              renderTaskList();
            }
            refreshRail();
          }).catch(function () { showToast("归档失败"); });
        });
        ops.appendChild(star);
        ops.appendChild(del);
        item.appendChild(ops);
        rail.appendChild(item);
        shown += 1;
      });
      if (!shown) rail.appendChild(el("div", "rail-empty", "还没有任务，点「新任务」开始。"));
      // 进行中统计（顶部兼容旧徽标）
      var activeCount = state.containers.filter(function (c) {
        var s = c.stat && c.stat.latest_run ? c.stat.latest_run.state : "";
        return s === "running" || s === "waiting_approval" || s === "paused" || s === "submitted";
      }).length;
      var badge = document.querySelector('[data-page="workspace"] .nav-badge, #railBadge');
      if (badge) badge.textContent = String(activeCount);
    }).catch(function () {});
  }

  /* ================= 新建任务（无活动任务时直接发消息用） ================= */
  function createAndRun(text) {
    api.createProject({ name: autoTitle(text), memory_scope: "project_only", model_pref: composerModelPref() }).then(function (project) {
      state.active = {
        id: project.id, title: project.title || autoTitle(text),
        session_id: project.session_id, messages: [], runs: [], stat: { runs: 0, messages: 0 },
        memory_scope: project.memory_scope || "project_only", instructions: project.instructions || "",
      };
      refreshRail();
      renderTask();
      startLive(text);
    }).catch(function (e) { showToast("新建任务失败：" + (e && e.message ? e.message : "")); });
  }

  function autoTitle(text) {
    // §三：任务标题自动生成（截断并去掉问句词）
    var t = String(text || "").trim().replace(/[？?\n]/g, " ").slice(0, 24);
    return t || "新任务";
  }

  /* ================= 任务列表页 ================= */
  var taskFilter = "home";
  var navigationToken = 0;
  var taskQuery = "";
  var draftCache = {};
  function saveDraft() {
    draftCache[state.active ? state.active.id : "home"] = { text: $("composerInput").value, attachments: attachItems.slice() };
  }
  function restoreDraft() {
    var d = draftCache[state.active ? state.active.id : "home"] || { text: "", attachments: [] };
    $("composerInput").value = d.text;
    composerDraftMemo = d.text;
    attachItems = d.attachments.slice();
    renderChips();
    updateComposerInput();
  }
  function dockComposer() {
    var composer = document.querySelector(".composer-wrap");
    if (composer) $("mainRegion").appendChild(composer);
    document.querySelector(".app").classList.remove("home-mode");
  }
  function updateComposerInput() {
    var input = $("composerInput");
    if (!input) return;
    input.style.height = "auto";
    input.style.height = Math.min(180, Math.max(62, input.scrollHeight)) + "px";
    $("sendBtn").disabled = !(input.value.trim() || attachItems.some(function (a) { return a.state === "ready" && a.id; }));
  }
  function closeSidebar() {
    document.querySelector(".app").classList.remove("sidebar-open");
    syncSidebar();
  }
  function syncSidebar() {
    var app = document.querySelector(".app");
    var visible = matchMedia("(max-width: 760px)").matches ? app.classList.contains("sidebar-open") : !app.classList.contains("sidebar-collapsed");
    $("sidebarToggle").setAttribute("aria-expanded", String(visible));
    $("taskSidebar").inert = !visible;
  }
  function goHome(filter) {
    navigationToken += 1;
    saveDraft();
    stopLive();
    state.active = null;
    taskFilter = filter || "home";
    taskQuery = "";
    restoreDraft();
    renderTaskList();
    closeSidebar();
    return refreshRail().then(function () { if (state.page === "tasklist") renderTaskList(); });
  }
  function matchesTask(c) {
    var status = c.stat && c.stat.latest_run ? c.stat.latest_run.state : "";
    if (taskFilter === "running" && ["running", "submitted", "waiting_tool"].indexOf(status) < 0) return false;
    if (taskFilter === "waiting" && ["waiting_approval", "paused", "waiting_user", "waiting_auth"].indexOf(status) < 0) return false;
    return !taskQuery || (c.title || "").toLowerCase().includes(taskQuery.toLowerCase());
  }
  function renderHomeRows(host) {
    clear(host);
    var items = state.containers.filter(matchesTask);
    var visible = taskFilter === "home" ? items.slice(0, 3) : items;
    if (!visible.length) host.appendChild(el("div", "home-empty", taskQuery ? "没有找到匹配的任务，试试其他关键词。" : taskFilter === "running" ? "暂时没有进行中的任务。" : taskFilter === "waiting" ? "没有需要你确认的任务。" : "从上方开始一个新任务，让想法有个着落。"));
    visible.forEach(function (c) {
      var row = el("button", "home-task-row");
      row.type = "button";
      var ico = el("span", "home-task-icon"); ico.innerHTML = RT.icon("file", 18);
      row.appendChild(ico);
      row.appendChild(el("span", "home-task-title", c.title || "未命名任务"));
      var raw = c.stat && c.stat.latest_run ? c.stat.latest_run.state : "";
      if (raw) row.appendChild(pill(raw));
      row.appendChild(el("time", "home-task-time", relTime(c.updated_at)));
      row.appendChild(el("span", "home-task-arrow", "↗"));
      row.addEventListener("click", function () { openTask(c.id); });
      host.appendChild(row);
    });
  }
  function renderTaskList() {
    dockComposer();
    surfaceCtx = null;
    state.page = "tasklist";
    setCrumb(taskFilter === "home" ? "首页" : "任务");
    setTaskHeadVisible(false);
    var composer = document.querySelector(".composer-wrap");
    composer.classList.remove("hidden");
    document.querySelector(".app").classList.add("home-mode");
    var stream = $("stream");
    clear(stream);
    var wrap = el("div", "home-dashboard");
    var hello = el("div", "home-intro");
    var signature = el("div", "home-signature");
    signature.appendChild(el("span", "logo"));
    signature.appendChild(el("span", null, "此刻  NOW"));
    hello.appendChild(signature);
    hello.appendChild(el("h1", null, "现在，想做什么？"));
    hello.appendChild(el("p", null, "把任务交给我，让复杂的事情变简单。"));
    wrap.appendChild(hello);
    wrap.appendChild(composer);
    var cards = el("div", "home-shortcuts");
    [["chart", "分析报告", "从数据中发现洞察", "帮我分析一份行业报告，提取关键洞察并生成摘要。"],
     ["list", "制定计划", "把目标拆成行动", "帮我制定一个学习计划，根据目标拆解步骤并安排时间。"],
     ["chart", "数据图表", "让数据清晰可见", "把这份数据做成图表，分析数据并生成可视化图表。"],
     ["file", "撰写文案", "为想法找到表达", "帮我写一篇产品文案，基于我的需求生成多版本文案。"]].forEach(function (ex, i) {
      var card = el("button", "home-shortcut shortcut-" + i);
      var ico = el("span", "shortcut-icon"); ico.innerHTML = RT.icon(ex[0], 20);
      var copy = el("span", "shortcut-copy");
      copy.appendChild(el("b", null, ex[1])); copy.appendChild(el("small", null, ex[2]));
      card.appendChild(ico); card.appendChild(copy);
      card.addEventListener("click", function () { $("composerInput").value = ex[3]; composerDraftMemo = ex[3]; updateComposerInput(); $("composerInput").focus(); });
      cards.appendChild(card);
    });
    wrap.appendChild(cards);
    var section = el("section", "home-recent");
    var head = el("div", "home-section-head");
    head.appendChild(el("h2", null, {home:"最近任务", all:"全部任务", running:"进行中", waiting:"等待确认"}[taskFilter]));
    if (taskFilter === "home") {
      var more = el("button", "home-more", "查看全部 " + state.containers.length + " 个任务 →");
      more.addEventListener("click", function () { taskFilter = "all"; renderTaskList(); }); head.appendChild(more);
    } else {
      var search = el("input", "task-filter-input"); search.type = "search"; search.placeholder = "筛选任务名称…"; search.setAttribute("aria-label", "筛选任务名称"); search.value = taskQuery;
      search.addEventListener("input", function () { taskQuery = search.value; renderHomeRows(rows); }); head.appendChild(search);
    }
    section.appendChild(head);
    var rows = el("div", "home-task-list"); renderHomeRows(rows); section.appendChild(rows);
    wrap.appendChild(section); stream.appendChild(wrap); stream.scrollTop = 0;
    $("composerInput").placeholder = "告诉此刻你想做什么……";
    renderWorkspaceButton(); updateComposerInput();
    document.querySelectorAll("[data-filter]").forEach(function (b) { b.classList.toggle("active", b.dataset.filter === taskFilter); });
    document.querySelectorAll(".rail-item").forEach(function (b) { b.classList.remove("active"); });
  }

  /* ================= 资料页 ================= */
  /* ================= 资料页（§十一：你的文件 / 连接的资料 / 最近使用 + 添加资料） ================= */
  function mats() { return state.materials; }

  function renderMaterials() {
    navigationToken += 1;
    state.page = "materials";
    setCrumb("资料");
    setComposerState(false);
    var stream = $("stream");
    clear(stream);
    var wrap = el("div", "materials");

    // 顶部：标题 + 添加资料
    var top = el("div", "materials-top");
    top.appendChild(el("div", "materials-title", "资料"));
    var addBtn = el("button", "primary-btn small", "＋ 添加资料");
    addBtn.addEventListener("click", function () {
      var input = document.createElement("input");
      input.type = "file";
      input.accept = ".pdf,.docx,.xlsx,.pptx,.txt,.md,.csv,.json,.png,.jpg,.py,.zip";
      input.addEventListener("change", function () {
        var f = input.files && input.files[0];
        if (!f) return;
        showToast("正在上传「" + f.name + "」…");
        api.uploadMaterial(f, f.name).then(function (r) {
          if (r && r.ok) {
            showToast("资料已就绪：" + r.artifact.name);
            loadMaterialsThen(renderMaterials)();
          } else {
            showToast("上传失败：" + (r && r.error ? r.error : "未知"));
          }
        }).catch(function (e) { showToast("上传失败：" + (e && e.message ? e.message : "网络错误")); });
      });
      input.click();
    });
    top.appendChild(addBtn);
    wrap.appendChild(top);

    var all = mats().artifacts || [];
    var connected = all.filter(function (a) { return a.kind === "material"; });
    var files = all.filter(function (a) { return a.kind !== "material"; });

    // 连接的资料（上传的资料）
    var connSec = el("div", "tl-sec");
    connSec.appendChild(el("div", "tl-head", "连接的资料"));
    if (connected.length) {
      connected.forEach(function (a) {
        var row = el("div", "tl-row");
        row.appendChild(el("span", "tl-title", (a.name || "").slice(0, 42)));
        row.appendChild(el("span", "tl-note", "更新时间：" + relTime(a.created_at)));
        var open = el("a", "ghost-btn small", "打开");
        open.href = api.artifactDownloadUrl(a.id);
        row.appendChild(open);
        var use = el("button", "ghost-btn small", "让此刻使用");
        use.addEventListener("click", function () { useMaterial(a.name); });
        row.appendChild(use);
        connSec.appendChild(row);
      });
    } else {
      var hint = el("div", "tl-empty", "点上方「＋ 添加资料」上传 PDF/Excel/文档，选完即可让此刻使用。");
      connSec.appendChild(hint);
    }
    wrap.appendChild(connSec);

    // 你的文件（任务生成的产物）
    var filesSec = el("div", "tl-sec");
    filesSec.appendChild(el("div", "tl-head", "你的文件"));
    if (files.length) {
      files.slice(0, 40).forEach(function (a) {
        var row = el("div", "tl-row");
        row.appendChild(el("span", "tl-title", (a.name || "").slice(0, 42)));
        row.appendChild(el("span", "pill small blue", (a.kind || "文件").slice(0, 10)));
        var open = el("a", "ghost-btn small", "打开");
        open.href = api.artifactDownloadUrl(a.id);
        row.appendChild(open);
        filesSec.appendChild(row);
      });
    } else {
      filesSec.appendChild(el("div", "tl-empty", "任务完成后生成的报告/表格/文档会出现在这里。"));
    }
    wrap.appendChild(filesSec);

    // 最近使用
    if (all.length) {
      var recent = el("div", "tl-sec");
      recent.appendChild(el("div", "tl-head", "最近使用"));
      all.slice().sort(function (a, b) { return a.created_at < b.created_at ? 1 : -1; })
        .slice(0, 6).forEach(function (a) {
          var row = el("div", "tl-row");
          row.appendChild(el("span", "tl-title", (a.name || "").slice(0, 42)));
          row.appendChild(el("span", "tl-note", relTime(a.created_at)));
          var open = el("a", "ghost-btn small", "打开");
          open.href = api.artifactDownloadUrl(a.id);
          row.appendChild(open);
          recent.appendChild(row);
        });
      wrap.appendChild(recent);
    }
    stream.appendChild(wrap);
  }

  function useMaterial(name) {
    // 「让此刻使用」→ 新建一个针对该资料的任务并预填首条消息
    var title = ("分析·" + name).slice(0, 24) || "分析资料";
    api.createProject({ name: title, memory_scope: "project_only", model_pref: composerModelPref() }).then(function (project) {
      state.active = {
        id: project.id, title: project.title || title, session_id: project.session_id,
        messages: [], runs: [], stat: { runs: 0, messages: 0 },
        memory_scope: project.memory_scope || "project_only", instructions: project.instructions || "",
      };
      refreshRail();
      renderTask();
      setTimeout(function () {
        var ci = $("composerInput");
        if (ci) { ci.value = "帮我分析这份资料《" + name + "》"; ci.focus(); }
      }, 80);
    }).catch(function (e) { showToast("创建任务失败：" + (e && e.message ? e.message : "")); });
  }

  /* ================= 设置页（§十二 基础/高级分层） ================= */
  var THEMES = [
    { key: "auto", label: "跟随系统", desc: "按操作系统自动切换深浅色" },
    { key: "dark", label: "深色", desc: "适合暗光环境" },
    { key: "light", label: "浅色", desc: "明亮清爽" },
  ];
  function themeSetting() { return localStorage.getItem("forge.theme.main") || "dark"; }
  function applyTheme() {
    var pref = themeSetting();
    var dark = pref === "auto"
      ? !(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches)
      : pref === "dark";
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  }

  /* ============ v28：Surface 导航（Settings=辅助面，返回原 Project） ============ */
  var surfaceCtx = null;
  function captureOrigin() {
    if (surfaceCtx) return surfaceCtx;
    var kind = "list";
    if (state.page === "tasklist") kind = "list";
    else if (state.page === "task" || (state.active && state.active.id)) kind = "project";
    surfaceCtx = {
      kind: kind,
      id: state.active && state.active.id ? state.active.id : null,
      title: state.active ? (state.active.title || "项目") : null,
      scroll: ($("stream") || { scrollTop: 0 }).scrollTop || 0,
      draft: $("composerInput") ? $("composerInput").value : "",
    };
    return surfaceCtx;
  }
  function originLabel() {
    var sc = surfaceCtx;
    if (sc && sc.kind === "project") return sc.title || "项目";
    if (sc && sc.kind === "list") return "项目列表";
    return "项目列表";
  }
  function backRestore(sc) {
    var apply = function () {
      var st = $("stream");
      if (st && sc.scroll) st.scrollTop = Math.min(sc.scroll, st.scrollHeight);
      var ci = $("composerInput");
      var want = sc.draft || composerDraftMemo;
      if (ci && want) ci.value = want;
    };
    [120, 380, 800, 1600].forEach(function (t) { setTimeout(apply, t); });
  }
  function surfaceBack() {
    var sc = surfaceCtx;
    surfaceCtx = null;
    if (sc && sc.kind === "project" && sc.id) {
      openTask(sc.id, { keepLive: true });
      backRestore(sc);
    } else {
      refreshRail().then(renderTaskList);
    }
  }
  function surfaceBackButton(label) {
    var row = el("div", "surface-back");
    var b = el("button", "ghost-btn small", "← 返回 " + (label || originLabel()));
    b.addEventListener("click", surfaceBack);
    row.appendChild(b);
    return row;
  }

  var settingsSec = "general";
  function renderSettings() {
    navigationToken += 1;
    captureOrigin();
    state.page = "settings";
    setCrumb("设置");
    setComposerState(false);
    renderSettingsShell();
  }
  function renderSettingsShell() {
    var stream = $("stream");
    clear(stream);
    var wrap = el("div", "settings");
    // 头部：固定返回入口 + 标题
    var head = el("div", "settings-topbar");
    head.appendChild(surfaceBackButton(""));
    head.appendChild(el("div", "settings-head-title", "设置"));
    wrap.appendChild(head);
    // 一级导航
    var nav = el("div", "settings-nav");
    [["general", "常规"], ["appearance", "外观"], ["notify", "通知"], ["memory", "记忆"],
     ["data", "数据"], ["advanced", "高级"]].forEach(function (sec) {
      var b = el("button", "chip" + (settingsSec === sec[0] ? " active" : ""), sec[1]);
      b.addEventListener("click", function () { settingsSec = sec[0]; renderSettingsShell(); });
      nav.appendChild(b);
    });
    wrap.appendChild(nav);
    var content = el("div", "settings-content");
    renderSettingsPane(content);
    wrap.appendChild(content);
    stream.appendChild(wrap);
    if (!state.configFetched) {
      api.runtimeStatus().then(function (s) {
        state.configFetched = true;
        state.config = (s && s.config) || {};
        state.config.toolsCount = (s && s.tools_count) || 0;
        renderComposerModel();
        if (state.page === "settings" && !settingsAdvPage) renderSettingsShell();
      }).catch(function () { state.configFetched = true; });
    }
  }
  var settingsAdvPage = null;
  function renderSettingsPane(host) {
    if (settingsSec === "general") renderGeneralPane(host);
    else if (settingsSec === "appearance") renderAppearancePane(host);
    else if (settingsSec === "notify") renderNotifyPane(host);
    else if (settingsSec === "memory") renderMemoryPane(host);
    else if (settingsSec === "data") renderDataPane(host);
    else renderAdvancedPane(host);
  }
  function secTitle(t) { return el("div", "tl-head settings-head", t); }
  function renderGeneralPane(host) {
    var box = el("div", "tl-sec");
    box.appendChild(secTitle("常规"));
    var account = el("div", "tl-row");
    account.appendChild(el("span", "tl-title", "账户"));
    account.appendChild(el("span", "tl-note", "本地使用 · 无账号"));
    box.appendChild(account);
    var selMode = localStorage.getItem("forge.mode") || "推荐";
    [["推荐", "此刻可以自行读取和分析内容。重要修改前会询问你。"],
     ["安全", "只查看，不修改。适合第一次检查文件和项目。"],
     ["自动", "普通操作自动执行，重要操作仍会询问你。"]].forEach(function (g) {
      var row = el("div", "mode-row" + (selMode === g[0] ? " active" : ""));
      row.appendChild(el("div", "mode-name", g[0]));
      row.appendChild(el("div", "mode-desc", g[1]));
      row.addEventListener("click", function () {
        localStorage.setItem("forge.mode", g[0]);
        box.querySelectorAll(".mode-row").forEach(function (r) { r.classList.remove("active"); });
        row.classList.add("active");
        showToast("使用方式已设为「" + g[0] + "」");
      });
      box.appendChild(row);
    });
    host.appendChild(box);
  }
  function renderAppearancePane(host) {
    var box = el("div", "tl-sec");
    box.appendChild(secTitle("外观"));
    THEMES.forEach(function (t) {
      var row = el("div", "mode-row small" + (themeSetting() === t.key ? " active" : ""));
      row.appendChild(el("div", "mode-name", t.label));
      row.appendChild(el("div", "mode-desc", t.desc));
      row.addEventListener("click", function () {
        localStorage.setItem("forge.theme.main", t.key);
        applyTheme();
        box.querySelectorAll(".mode-row").forEach(function (r) { r.classList.remove("active"); });
        row.classList.add("active");
        showToast("外观已切换：" + t.label);
      });
      box.appendChild(row);
    });
    host.appendChild(box);
  }
  function renderNotifyPane(host) {
    var box = el("div", "tl-sec");
    box.appendChild(secTitle("通知"));
    var notifRow = el("div", "tl-row clickable");
    notifRow.appendChild(el("span", "tl-title", "通知"));
    var notifVal = localStorage.getItem("forge.notify") !== "off";
    var notifNote = el("span", "tl-note", notifVal ? "已开启（重要事项）" : "已关闭");
    notifRow.appendChild(notifNote);
    notifRow.addEventListener("click", function () {
      var next = !(localStorage.getItem("forge.notify") !== "off");
      localStorage.setItem("forge.notify", next ? "on" : "off");
      notifNote.textContent = next ? "已开启（重要事项）" : "已关闭";
      showToast("通知" + (next ? "已开启" : "已关闭"));
    });
    box.appendChild(notifRow);
    box.appendChild(el("div", "tl-note", "通知仅用于重要事项（等你确认等）；以本机界面呈现。"));
    host.appendChild(box);
  }
  function renderMemoryPane(host) {
    var box = el("div", "tl-sec");
    box.appendChild(secTitle("记忆"));
    var enabled = localStorage.getItem("forge.memoryEnabled") !== "off";
    var row = el("div", "tl-row clickable");
    row.appendChild(el("span", "tl-title", "长期记忆"));
    var note = el("span", "tl-note", enabled ? "已开启" : "已关闭");
    row.appendChild(note);
    row.addEventListener("click", function () {
      var next = !enabled;
      localStorage.setItem("forge.memoryEnabled", next ? "on" : "off");
      note.textContent = next ? "已开启" : "已关闭";
      api.postSettingsMemory(next);
      showToast(next ? "长期记忆已开启" : "长期记忆已关闭");
      renderSettingsShell();
    });
    box.appendChild(row);
    box.appendChild(el("div", "tl-note", "开启后此刻可以记住你的长期偏好；记忆范围可在具体任务的设置里选择。"));
    host.appendChild(box);

    var list = el("div", "tl-sec");
    list.appendChild(secTitle("查看长期记忆"));
    var body = el("div", "");
    body.appendChild(skeletonRows(3, true));
    list.appendChild(body);
    host.appendChild(list);
    api.listMemories().then(function (d) {
      var rows = d.memories || [];
      clear(body);
      if (!rows.length) body.appendChild(el("div", "tl-empty", "还没有长期记忆。"));
      rows.forEach(function (m) {
        var r = el("div", "tl-row");
        r.appendChild(el("span", "tl-title", (m.text || "").slice(0, 46)));
        r.appendChild(el("span", "pill small blue", (m.tags || []).slice(0, 2).join("、") || "记忆"));
        var del = el("button", "ghost-btn small", "删除");
        del.addEventListener("click", function () {
          api.postJson("/api/memories/delete", { id: m.id }).then(function () {
            showToast("已删除");
            renderSettingsShell();
          });
        });
        r.appendChild(del);
        body.appendChild(r);
      });
    }).catch(function () {
      clear(body);
      body.appendChild(el("div", "tl-empty", "加载失败"));
    });
  }
  function renderDataPane(host) {
    var cfg = state.config || {};
    var box = el("div", "tl-sec");
    box.appendChild(secTitle("数据"));
    [["数据库", cfg.db_path || "agent.db"],
     ["大小", cfg.db_size_mb ? cfg.db_size_mb + " MB" : "—"],
     ["备份", cfg.backups_count != null ? cfg.backups_count + " 份（改动前自动备份）" : "—"]].forEach(function (it) {
      var r = el("div", "tl-row");
      r.appendChild(el("span", "tl-title", it[0]));
      r.appendChild(el("span", "tl-note", String(it[1]).slice(0, 120)));
      box.appendChild(r);
    });
    host.appendChild(box);
  }
  function renderAdvancedPane(host) {
    var cfg = state.config || {};
    var box = el("div", "tl-sec");
    box.appendChild(secTitle("高级"));
    [["模型", cfg.model || "此刻自动选择"],
     ["模型服务", cfg.provider_text || "未测试"],
     ["命令权限", cfg.code_exec_enabled ? "已开启（沙箱执行代码）" : "未开启"],
     ["路径权限", cfg.project_edit_enabled ? "已开启（可修改项目文件）" : "只读"],
     ["审批规则", cfg.approval_enabled ? "重要操作需要你确认" : "已关闭"],
     ["沙箱", cfg.sandbox_root || "—"]].forEach(function (it) {
      var r = el("div", "tl-row");
      r.appendChild(el("span", "tl-title", it[0]));
      r.appendChild(el("span", "tl-note", String(it[1]).slice(0, 160)));
      box.appendChild(r);
    });
    host.appendChild(box);
    var group = el("div", "tl-sec");
    group.appendChild(secTitle("扩展能力与记录"));
    [["扩展能力", "网页搜索、文件处理、代码执行等已接入能力", "tools"],
     ["定时任务", "让此刻定期自动做某事", "schedules"],
     ["安全与记录", "重要操作记录（审批、允许/拒绝历史）", "approvals"],
     ["诊断与审计", "运行过程、日志与开发者信息", "trace"]].forEach(function (it) {
      var r = el("div", "tl-row clickable");
      r.appendChild(el("span", "tl-title", it[0]));
      r.appendChild(el("span", "tl-note", it[1]));
      r.addEventListener("click", function () { openAdvancedSurface(it[2], it[0]); });
      group.appendChild(r);
    });
    host.appendChild(group);
  }
  function openAdvancedSurface(page, label) {
    captureOrigin();
    settingsAdvPage = page;
    setComposerState(false);
    if (window.switchPage) window.switchPage(page);
    else showToast("无法打开页面");
    // 全局悬浮返回条（独立于页面内容生命周期，避免被 onShow 重渲染清掉）
    var old = document.getElementById("advBackBar");
    if (old) old.remove();
    var bar = el("div", "surface-back adv-global");
    bar.id = "advBackBar";
    var b = el("button", "ghost-btn small", "← 返回 设置");
    b.addEventListener("click", function () {
      var bb = document.getElementById("advBackBar");
      if (bb) bb.remove();
      settingsAdvPage = null;
      if (window.switchPage) switchPage("workspace");
      renderSettingsShell();
    });
    bar.appendChild(b);
    bar.appendChild(el("span", "adv-title", label));
    document.querySelector(".main") ? document.querySelector(".main").appendChild(bar) : document.body.appendChild(bar);
  }


  /* ================= Project 对话页（统一 Conversation Flow） ================= */
  function isRunEnriched(run) {
    var cached = state.runCache[run.id];
    return !!(cached && cached.tool_calls);
  }
  function enrichedOr(run) {
    return state.runCache[run.id] || run;
  }
  var currentFlow = null;
  function flowHost() { return currentFlow || $("stream"); }

  function hasMeaningfulResult(run) {
    if (!run || !run.tool_calls || !run.tool_calls.length) return false;
    var sum = RT.activity.deliverySummary(run);
    return (sum.changedFiles.length > 0) || (sum.artifacts.length > 0) || (sum.verifiedCount > 0);
  }

  function activityInline(run) {
    // 过程只做轻量辅助块，不占用正文样式
    var wrap = el("div", "act-inline");
    var groups = RT.activity.project(entriesOf(run));
    groups.forEach(function (g) {
      var line = el("div", "act-line", (g.meta.icon || "") + " " + g.summary);
      wrap.appendChild(line);
    });
    return wrap.children.length ? wrap : null;
  }

  /* ---- Run 过程摘要：聚合为语义步骤（绝不渲染 原始 tool/args/JSON/推理） ---- */
  function processSteps(run) {
    if (!run || !run.tool_calls || !run.tool_calls.length) return [];
    try {
      // project() 按阶段连续合并：同类工具聚合为一条人类语言步骤
      return RT.activity.project(entriesOf(run)).map(function (g) {
        return { label: g.summary, icon: g.meta.icon || "•" };
      });
    } catch (e) { return []; }
  }

  function renderProcessSummary(flow, steps, state) {
    if (!steps || !steps.length) return;
    var m = { completed: ["✓", "已完成", "ok"],
              failed: ["✕", "遇到问题", "err"],
              cancelled: ["○", "已停止", "muted"],
              paused: ["○", "已暂停", "muted"],
              waiting_approval: ["●", "等你确认", "warn"],
              running: ["◉", "正在处理", "running"] }[state] || ["✓", "已完成", "ok"];
    var proc = el("div", "run-proc " + m[2]);
    var head = el("button", "rp-head");
    head.innerHTML = '<span class="rp-ico">' + m[0] + '</span><span class="rp-title">' + m[1] +
      ' · ' + steps.length + ' 个步骤</span><span class="rp-caret">▸</span>';
    var body = el("div", "rp-body");
    steps.forEach(function (s) { body.appendChild(el("div", "rp-step", s.icon + "  " + s.label)); });
    proc.appendChild(head);
    proc.appendChild(body);
    flow.appendChild(proc);
    head.addEventListener("click", function () { proc.classList.toggle("open"); });
  }

  function renderTask(init) {
    state.page = "task";
    setCrumb("任务");
    document.querySelectorAll(".rail-item").forEach(function (item) { item.classList.toggle("active", !!state.active && item.dataset.taskId === String(state.active.id)); });
    setComposerState(true);
    setRunControls(false);
    renderComposerModel();
    renderWorkspaceButton();
    var stream = $("stream");
    clear(stream);
    currentFlow = null;
    if (!state.active) { renderTaskList(); return; }
    var detail = state.active;
    var latest = null;
    (detail.runs || []).forEach(function (r) {
      if (["running", "submitted", "waiting_approval", "paused"].indexOf(r.state) >= 0) latest = r;
    });

    setHeader(detail.title || "未命名项目", "");
    var bs = $("btnProjectSources");
    var n = detail.sourcesCount || 0;
    if (bs) { bs.disabled = false; bs.textContent = n > 0 ? ("资料 " + n) : "资料"; }
    var bset = $("btnProjectSettings");
    if (bset) bset.disabled = false;
    $("composerInput").placeholder = "告诉此刻接下来要做什么……";
    updateContextStats();
    updateTopbarProject(detail.title || "");
    var _w = (state.active && (state.active.work_location || {})) || {};
    var wlName = (state.active && (state.active.work_location_name || _w.name)) || "";
    var wlPath2 = (state.active && (state.active.work_location_path || _w.local_path)) || "";
    if (wlName) {
      var mEl = $("taskMeta");
      if (mEl) {
        var wlPill = el("span", "pill blue", "工作区 · " + wlName);
        wlPill.title = wlPath2 || "";
        mEl.appendChild(wlPill);
      }
    }

    var runs = (detail.runs || []).slice().sort(function (a, b) { return a.created_at < b.created_at ? -1 : 1; });
    if (!runs.length) {
      var empty = el("div", "page-placeholder");
      empty.appendChild(el("b", null, "开始和此刻工作"));
      empty.appendChild(el("span", null, "在下方告诉此刻你想完成什么。"));
      stream.appendChild(empty);
      return;
    }
    var hiddenCount = 0;
    var visible = runs;
    if (!state.showAllRuns && runs.length > 15) {
      visible = runs.slice(runs.length - 15);
      hiddenCount = runs.length - 15;
    }
    var flow = el("div", "flow");
    currentFlow = flow;
    stream.appendChild(flow);

    visible.forEach(function (runRaw) {
      var run = enrichedOr(runRaw);
      var msgs = (detail.messages || []).filter(function (m) { return m.run_id === run.id; })
        .sort(function (a, b) { return a.id - b.id; });
      var userMsgs = msgs.filter(function (m) { return m.role === "user"; });
      var assistantMsgs = msgs.filter(function (m) { return m.role === "assistant"; });

      // 1) 你说
      if (userMsgs.length) userMsgs.forEach(function (m) { flow.appendChild(messageRow(m)); });
      else if (runRaw.goal) flow.appendChild(el("div", "legacy-goal", "（早期记录）" + String(runRaw.goal).slice(0, 200)));
      // 2) 运行/终态 → 保留的过程摘要 + 正文（+ 终态卡片）
      var enriched = isRunEnriched(run);
      var steps = enriched ? processSteps(run) : [];
      var st2 = run.state;
      if (st2 === "failed" && !state.live) {
        renderProcessSummary(flow, steps, "failed");
        flow.appendChild(isRunEnriched(run) ? failureCard(run) : compactStateCard(run, "fail"));
        assistantMsgs.forEach(function (m) { flow.appendChild(messageRow(m)); });
        return;
      }
      if (st2 === "waiting_approval") {
        renderProcessSummary(flow, steps, "waiting_approval");
        flow.appendChild(approvalWaitCard(run));
        assistantMsgs.forEach(function (m) { flow.appendChild(messageRow(m)); });
        return;
      }
      if (st2 === "paused" || st2 === "cancelled") {
        renderProcessSummary(flow, steps, "cancelled");
        flow.appendChild(el("div", "flow-note", st(run.state).label + "。"));
        assistantMsgs.forEach(function (m) { flow.appendChild(messageRow(m)); });
        return;
      }
      if (["running", "submitted"].indexOf(st2) >= 0 && !state.live) {
        if (steps.length) renderProcessSummary(flow, steps, "running");
        else flow.appendChild(el("div", "flow-note", "正在处理…"));
        return;
      }
      // 3) 完成：过程摘要(折叠) 先于正文，Final Answer 不覆盖 Activity
      if (steps.length) renderProcessSummary(flow, steps, "completed");
      assistantMsgs.forEach(function (m) { flow.appendChild(messageRow(m)); });
      // 4) 交付卡（仅确实产出成果时）
      if (st2 === "completed" && enriched && hasMeaningfulResult(run)) {
        flow.appendChild(resultCard(run));
      } else if (st2 === "completed" && !enriched) {
        var load = el("button", "ghost-btn small", "查看过程");
        load.addEventListener("click", function () {
          fetchRun(run.id).then(renderTask).catch(function () { showToast("加载失败"); });
        });
        var ln = el("div", "flow-note");
        ln.appendChild(load);
        flow.appendChild(ln);
      }
    });
    if (hiddenCount) {
      var more = el("button", "ghost-btn small tl-more", "显示更早对话");
      more.addEventListener("click", function () {
        state.showAllRuns = true;
        renderTask();
      });
      flow.appendChild(more);
    }
    scrollToLatest();
  }

  /* 历史“需要你确认”卡片：读取真实 pending 审批 */
  function actionDesc(a) {
    var tool = (a && (a.tool || a.tool_name)) || "";
    var args = a && a.arguments;
    if (typeof args === "string") {
      try { args = JSON.parse(args); } catch (e) { args = {}; }
    }
    if (!args && a && a.args_text) {
      try { args = JSON.parse(a.args_text); } catch (e) { args = {}; }
    }
    return RT.activity.describe(tool, args || {});
  }

  function approvalWaitCard(run) {
    var card = el("div", "approval-inline");
    card.appendChild(el("div", "approval-title", "需要你确认"));
    card.appendChild(el("div", "approval-desc", "此刻执行时遇到了需要你决定的操作："));
    var box = el("div", "approval-lines");
    box.appendChild(el("div", "approval-line", "加载中…"));
    card.appendChild(box);
    var btns = el("div", "approval-actions");
    var look = el("button", "ghost-btn", "看看会改什么");
    look.addEventListener("click", function () { openDrawer(run.id, "diff"); });
    var allow = el("button", "primary-btn", "继续");
    allow.addEventListener("click", function () {
      api.listApprovals({ task_id: run.id, state: "pending" }).then(function (d) {
        var ids = (d.approvals || []).map(function (a) { return a.id; });
        if (!ids.length) { showToast("没有待确认的操作"); return; }
        decideAll(d.approvals || [], "approved", run.id);
      });
    });
    btns.appendChild(look);
    btns.appendChild(allow);
    card.appendChild(btns);
    api.listApprovals({ task_id: run.id, state: "pending" }).then(function (d) {
      if (!(d.approvals || []).length) {
        box.textContent = "（已处理完毕，可继续）";
        return;
      }
      clear(box);
      (d.approvals || []).forEach(function (a) {
        box.appendChild(el("div", "approval-line", "· " + actionDesc(a)));
      });
    }).catch(function () {});
    return card;
  }

  /* 轻量完成卡（仅真正产出时出现；无重复“已完成/FORGE 完成了”） */
  function resultCard(run) {
    var sum = RT.activity.deliverySummary(run);
    var card = el("div", "result-card");
    card.appendChild(el("div", "result-head", "✓ 已完成"));
    var body = el("div", "result-body");
    if (sum.changedFiles.length) body.appendChild(el("div", "result-item", "• 修改 " + sum.changedFiles.length + " 个文件"));
    sum.artifacts.forEach(function (a) { body.appendChild(el("div", "result-item", "• 生成 " + a)); });
    if (sum.verifiedCount) body.appendChild(el("div", "result-item", "• 验证通过"));
    if (body.children.length) card.appendChild(body);
    var btns = el("div", "result-actions");
    if (sum.changedFiles.length) {
      var v = el("button", "ghost-btn small", "查看修改");
      v.addEventListener("click", function () { openDrawerFiles(run.id); });
      btns.appendChild(v);
    }
    if (sum.artifacts.length) {
      var a0 = run.artifacts && run.artifacts[0];
      if (a0 && a0.id) {
        var open = el("a", "ghost-btn small", "打开产物");
        open.href = api.artifactDownloadUrl(a0.id);
        btns.appendChild(open);
      }
    }
    var all = el("button", "ghost-btn small", "查看完整过程");
    all.addEventListener("click", function () { openDrawer(run.id, "activity"); });
    btns.appendChild(all);
    if (btns.children.length) card.appendChild(btns);
    return card;
  }

  /* 明细未加载时的轻量状态卡（点击加载完整过程） */
  function compactStateCard(run, kind) {
    var card = el("div", kind === "done" ? "deliver-card" : "fail-card");
    card.appendChild(el("div", kind === "done" ? "result-head" : "fail-head",
                        kind === "done" ? "✓ 已完成" : "遇到一个问题"));
    var b = el("button", "ghost-btn", "查看过程");
    b.addEventListener("click", function () {
      fetchRun(run.id).then(renderTask).catch(function () { showToast("加载失败"); });
    });
    card.appendChild(b);
    return card;
  }

  function setRunControls(live) {
    var p = $("pauseBtn"), c = $("cancelBtn");
    if (p) p.style.display = live ? "" : "none";
    if (c) c.style.display = live ? "" : "none";
  }
  function updateTopbarProject(name) {
    var t = $("crumbProject");
    if (t) t.textContent = name ? name : "项目";
  }

  /* ---------- 过程卡（第一层：过程） ---------- */
  function activityCard(runId, withLoad, cached) {
    var run = cached || state.runCache[runId];
    var card = el("div", "act-card");
    if (run && run.tool_calls && run.tool_calls.length) {
      var groups = RT.activity.project(entriesOf(run));
      groups.forEach(function (g) {
        var gEl = el("div", "act-group");
        gEl.appendChild(el("div", "act-head", (g.meta.icon || "") + " " + g.summary));
        var list = el("div", "act-lines");
        g.lines.forEach(function (l) { list.appendChild(el("div", "act-line", l)); });
        gEl.appendChild(list);
        card.appendChild(gEl);
      });
      return card;
    }
    if (run && run.tool_calls && run.tool_calls.length === 0 && (run.state === "completed" || run.state === "failed")) {
      card.appendChild(el("div", "act-empty", run.state === "completed" ? "本轮直接完成。" : "本轮遇到的问题见下方。"));
      return card;
    }
    if (withLoad) {
      var load = el("button", "ghost-btn small", "查看过程");
      load.addEventListener("click", function () {
        fetchRun(runId).then(renderTask).catch(function () { showToast("加载失败"); });
      });
      card.appendChild(load);
    } else {
      card.appendChild(el("div", "act-empty", "加载过程中…"));
    }
    return card;
  }

  function entriesOf(run) {
    return (run.tool_calls || []).map(function (t) {
      var args = {};
      try { args = (typeof t.arguments === "string") ? JSON.parse(t.arguments || "{}") : (t.arguments || {}); }
      catch (e) { args = {}; }
      return { tool: t.tool_name, args: args, status: t.status };
    });
  }

  /* ---------- 完成交付卡（§十六） ---------- */
  function completionCard(run) {
    var card = el("div", "deliver-card");
    card.appendChild(el("div", "deliver-head", "✓ 已完成"));

    var summary = RT.activity.deliverySummary(run);
    // 做了什么（从 assistant 消息 meta 的 next_step（如有）与过程生成）
    var reasons = [];
    (run.tool_calls || []).forEach(function (t) {
      if (RT.activity.phaseFor(t.tool_name) === "exploring") reasons = [];
    });
    card.appendChild(el("div", "deliver-title", "已完成"));
    var list = el("div", "deliver-list");
    // 从过程归纳（借用 Activity 总结行）
    var groups = RT.activity.project(entriesOf(run));
    groups.forEach(function (g) {
      if (g.lines.length && g.phase !== "exploring") {
        list.appendChild(el("div", "deliver-item", "✓ " + g.summary));
      }
    });
    if (summary.changedFiles.length) {
      list.appendChild(el("div", "deliver-item", "✓ 修改 " + summary.changedFiles.length + " 个文件"));
    }
    if (summary.artifacts.length) {
      list.appendChild(el("div", "deliver-item", "✓ 生成 " + summary.artifacts.length + " 个文件"));
    }
    if (summary.verifiedCount) {
      list.appendChild(el("div", "deliver-item", "✓ 验证通过"));
    }
    if (!list.children.length) list.appendChild(el("div", "deliver-item", "✓ 已完成"));
    card.appendChild(list);

    // 修改 / 生成 / 验证 区
    var grid = el("div", "deliver-grid");
    if (summary.changedFiles.length) {
      var m = el("div", "deliver-cell");
      m.appendChild(el("div", "deliver-cell-title", "修改"));
      m.appendChild(el("div", "deliver-cell-big", summary.changedFiles.length + " 个文件"));
      var btn = el("button", "ghost-btn small", "查看");
      btn.addEventListener("click", function () { openDrawerFiles(run.id); });
      m.appendChild(btn);
      grid.appendChild(m);
    }
    if (summary.artifacts.length) {
      var g = el("div", "deliver-cell");
      g.appendChild(el("div", "deliver-cell-title", "生成"));
      g.appendChild(el("div", "deliver-cell-big", summary.artifacts[0].slice(0, 18)));
      var open = el("a", "ghost-btn small", "打开");
      open.href = api.artifactDownloadUrl((run.artifacts && run.artifacts[0] && run.artifacts[0].id) || "");
      g.appendChild(open);
      grid.appendChild(g);
    }
    if (summary.verifiedCount) {
      var v = el("div", "deliver-cell");
      v.appendChild(el("div", "deliver-cell-title", "验证"));
      v.appendChild(el("div", "deliver-cell-big", summary.verifiedCount + " 次自检"));
      grid.appendChild(v);
    }
    if (grid.children.length) card.appendChild(grid);

    // 接下来
    var next = el("div", "deliver-next");
    next.appendChild(el("div", "deliver-cell-title", "接下来"));
    var c1 = el("button", "ghost-btn", "继续追加要求");
    c1.addEventListener("click", function () { $("composerInput").focus(); });
    next.appendChild(c1);
    var c2 = el("button", "ghost-btn", "查看完整过程");
    c2.addEventListener("click", function () { openDrawer(run.id); });
    next.appendChild(c2);
    card.appendChild(next);
    return card;
  }

  /* ---------- 失败恢复卡（§七） ---------- */
  function failureCard(run) {
    var card = el("div", "fail-card");
    card.appendChild(el("div", "fail-head", "遇到一个问题"));
    card.appendChild(el("div", "fail-desc",
      "此刻没有完成全部内容。可以先“查看问题”了解原因，或直接再试一次。"));
    var actions = el("div", "fail-actions");
    var retry = el("button", "primary-btn", "继续尝试");
    retry.addEventListener("click", function () {
      // 失败 Run 是终态不可恢复：同 Task 内以同样目标发起新的 Run
      stopLive();
      startLive(retryText(run));
    });
    var view = el("button", "ghost-btn", "查看问题");
    view.addEventListener("click", function () { openDrawerError(run); });
    actions.appendChild(retry);
    actions.appendChild(view);
    card.appendChild(actions);
    return card;
  }
  function retryText(run) {
    var goal = run.goal || "";
    return goal + "（请再试一次：上一轮遇到问题，继续解决。）";
  }

  /* ================= Drawer（按需，4 个业务 Tab + 高级信息折叠） =================
   * §九：Drawer 只有 文件/修改/测试/运行记录 四个一级 Tab；
   * Audit/Context/Model Calls/Events/Observability 全部收进「高级信息」折叠。
   */
  var DRAWER_TABS = [
    { key: "files", label: "文件" },
    { key: "diff", label: "修改" },
    { key: "tests", label: "测试" },
    { key: "activity", label: "运行记录" },
  ];

  function openDrawerShell() {
    var app = document.querySelector(".app");
    app.classList.add("drawer-open");
    return app;
  }

  function latestRunId() {
    var runs = state.active && state.active.runs ? state.active.runs : [];
    if (!runs.length) return null;
    var sorted = runs.slice().sort(function (a, b) { return a.created_at < b.created_at ? -1 : 1; });
    return sorted[sorted.length - 1].id;
  }

  function openDrawer(runId, tab) {
    openDrawerShell();
    if (!state.active) return;
    state.drawerRunId = runId || latestRunId();
    state.drawerTab = tab || "activity";
    renderDrawer();
  }

  function openDrawerFiles(runId) { openDrawer(runId, "diff"); }
  function openDrawerError(run) { openDrawer(run ? run.id : state.drawerRunId, "activity"); }

  function runNumberFor(runId, runs) {
    var sorted = (runs || []).slice().sort(function (a, b) { return a.created_at < b.created_at ? -1 : 1; });
    var i = sorted.findIndex(function (r) { return r.id === runId; });
    return i >= 0 ? i + 1 : null;
  }

  function renderDrawer() {
    var host = $("detailDrawerBody");
    clear(host);
    if (!state.active) return;
    var detail = state.active;
    var head = el("div", "dr-hero");
    head.appendChild(el("b", null, (detail.title || "未命名项目").slice(0, 50)));
    var lrs = detail.stat && detail.stat.latest_run ? detail.stat.latest_run.state : "";
    if (lrs) {
      var statusLine = el("div", "dr-sub");
      statusLine.appendChild(pill(lrs));
      statusLine.appendChild(document.createTextNode("  " + (detail.stat.latest_run.goal || "当前任务")));
      head.appendChild(statusLine);
    }
    head.appendChild(el("div", "dr-sub",
      (detail.stat ? detail.stat.messages + " 条消息 · " + detail.stat.runs + " 次执行" : "")));
    host.appendChild(head);

    // 轮次选择（chip 行）
    var runs = (detail.runs || []).slice().sort(function (a, b) { return a.created_at < b.created_at ? -1 : 1; });
    if (runs.length) {
      var rounds = el("div", "dr-rounds");
      runs.forEach(function (r) {
        var num = runNumberFor(r.id, runs);
        var chip = el("button", "dr-round-chip" + (state.drawerRunId === r.id ? " active" : ""),
          "第 " + num + " 次");
        chip.addEventListener("click", function () {
          state.drawerRunId = r.id;
          renderDrawer();
        });
        rounds.appendChild(chip);
      });
      host.appendChild(rounds);
    }

    // 四个业务 Tab
    var tabs = el("div", "dr-tabs");
    DRAWER_TABS.forEach(function (t) {
      var b = el("button", "dr-tab" + (state.drawerTab === t.key ? " active" : ""), t.label);
      b.addEventListener("click", function () { state.drawerTab = t.key; renderDrawer(); });
      tabs.appendChild(b);
    });
    host.appendChild(tabs);

    if (!state.drawerRunId) {
      host.appendChild(el("div", "dr-empty", "还没有执行记录"));
      return;
    }
    api.getRun(state.drawerRunId).then(function (run) {
      var pane = el("div", "dr-pane");
      if (state.drawerTab === "files") pane.appendChild(drawerFilesPane(run));
      else if (state.drawerTab === "diff") pane.appendChild(drawerDiffPane(run));
      else if (state.drawerTab === "tests") pane.appendChild(drawerTestsPane(run));
      else pane.appendChild(drawerActivityPane(run));
      pane.appendChild(drawerTechFold(run)); // 高级信息（最底层折叠）
      host.appendChild(pane);
      host.scrollTop = host.scrollHeight;
    }).catch(function () {
      host.appendChild(el("div", "dr-empty", "加载失败"));
    });
  }

  /* Tab 内容：文件（读取/修改/生成的文件一览） */
  function drawerFilesPane(run) {
    var sec = el("div", "dr-sec");
    sec.appendChild(el("h4", null, "本轮涉及的文件"));
    var rows = [];
    (run.tool_calls || []).forEach(function (t) {
      var phase = RT.activity.phaseFor(t.tool_name);
      if (phase !== "reading" && phase !== "editing" && t.tool_name !== "list_workspace_files" && t.tool_name !== "list_code_files" && phase !== "generating") return;
      var args = {};
      try { args = (typeof t.arguments === "string") ? JSON.parse(t.arguments || "{}") : (t.arguments || {}); }
      catch (e) { args = {}; }
      var ref = args.path || args.filename || args.file || args.directory;
      if (!ref) return;
      var kind = phase === "editing" ? "修改" : phase === "generating" ? "生成" : "读取";
      rows.push({ ref: ref, kind: kind });
    });
    var seen = {};
    rows.forEach(function (r) {
      if (seen[r.ref + "|" + r.kind]) return;
      seen[r.ref + "|" + r.kind] = true;
      var line = el("div", "dr-file-row");
      line.appendChild(el("span", "dr-file-kind", r.kind));
      line.appendChild(el("span", "dr-file-path", r.ref.slice(0, 80)));
      sec.appendChild(line);
    });
    if (!rows.length) sec.appendChild(el("div", "dr-empty", "本轮没有读取或修改文件。"));
    // 生成产物（可下载）
    var arts = (run.artifacts || []).slice(0, 20);
    arts.forEach(function (a) {
      var link = el("a", "dr-art-link", "↓ " + a.name + " · " + (a.kind || "文件"));
      link.href = api.artifactDownloadUrl(a.id);
      sec.appendChild(link);
    });
    return sec;
  }

  /* Tab 内容：修改（Diff 列表；完整 diff 存证待 P2，如实提示） */
  function drawerDiffPane(run) {
    var sec = el("div", "dr-sec");
    sec.appendChild(el("h4", null, "改动过的位置"));
    var rows = [];
    (run.tool_calls || []).forEach(function (t) {
      if (RT.activity.phaseFor(t.tool_name) !== "editing") return;
      var args = {};
      try { args = (typeof t.arguments === "string") ? JSON.parse(t.arguments || "{}") : (t.arguments || {}); }
      catch (e) { args = {}; }
      var ref = args.path || args.filename || args.file || t.tool_name;
      var note = "";
      if (args.old_string && args.new_string) note = "（有替换块 " + String(args.old_string).slice(0, 24) + " → " + String(args.new_string).slice(0, 24) + "）";
      rows.push({ ref: ref, note: note, tool: t.tool_name });
    });
    var seen = {};
    rows.forEach(function (r) {
      if (seen[r.ref]) return;
      seen[r.ref] = true;
      var line = el("div", "dr-file-row");
      line.appendChild(el("span", "dr-file-kind", "改"));
      line.appendChild(el("span", "dr-file-path", r.ref.slice(0, 70) + r.note.slice(0, 50)));
      sec.appendChild(line);
    });
    if (!rows.length) sec.appendChild(el("div", "dr-empty", "本轮没有修改任何文件。"));
    sec.appendChild(el("div", "dr-tip", "统一 diff（旧行/新行对照）目前保留在执行记录里；可视化 Diff 面板将随第三阶段接入。"));
    return sec;
  }

  /* Tab 内容：测试（验证类工具 + 结果摘录） */
  function drawerTestsPane(run) {
    var sec = el("div", "dr-sec");
    sec.appendChild(el("h4", null, "验证与测试"));
    var found = false;
    (run.tool_calls || []).forEach(function (t) {
      if (RT.activity.phaseFor(t.tool_name) !== "verifying") return;
      found = true;
      var line = el("div", "dr-test-row");
      line.appendChild(el("span", "dr-test-name", describeToolShort(t.tool_name)));
      line.appendChild(el("span", "dr-test-status", statusWord(t.status)));
      if (t.result_excerpt) line.appendChild(el("span", "dr-test-excerpt", String(t.result_excerpt).slice(0, 160)));
      sec.appendChild(line);
    });
    if (!found) sec.appendChild(el("div", "dr-empty", "本轮没有运行验证/测试步骤。"));
    return sec;
  }
  function describeToolShort(name) {
    return RT.activity.labelFor(name) || "验证";
  }
  function statusWord(s) {
    return s === "succeeded" ? "通过" : s === "failed" ? "未通过" : (s || "运行中");
  }

  /* Tab 内容：运行记录（Activity 分组投影） */
  function drawerActivityPane(run) {
    var sec = el("div", "dr-sec");
    sec.appendChild(el("h4", null, "执行过程"));
    var groups = RT.activity.project(entriesOf(run));
    if (!groups.length) sec.appendChild(el("div", "dr-empty", "没有过程记录。"));
    groups.forEach(function (g) {
      var gEl = el("div", "act-group compact");
      gEl.appendChild(el("div", "act-head", g.summary));
      var list = el("div", "act-lines");
      g.lines.forEach(function (l) { list.appendChild(el("div", "act-line", l)); });
      gEl.appendChild(list);
      sec.appendChild(gEl);
    });
    if (run.error) sec.appendChild(el("div", "run-err", String(run.error).slice(0, 300)));
    return sec;
  }

  /* 高级信息（Observability 折叠层） */
  function drawerTechFold(run) {
    var adv = el("details", "dr-adv");
    adv.appendChild(el("summary", null, "高级信息（技术详情）"));
    var body = el("div", "dr-events");

    var ev = el("div", "dr-adv-group", "Event");
    (run.events || []).slice(-20).forEach(function (e) {
      ev.appendChild(el("div", "dr-event", e.type + " · " + (e.payload && e.payload.reason ? String(e.payload.reason).slice(0, 60) : "")));
    });
    body.appendChild(ev);

    (run.model_calls || []).slice(-10).forEach(function (m) {
      body.appendChild(el("div", "dr-event",
        "模型调用 · " + (m.model || "?") + " · " + m.status + (m.input_tokens != null ? (" · " + m.input_tokens + "/" + m.output_tokens + " tok") : "")));
    });

    if (run.checkpoint && run.checkpoint.summary) {
      body.appendChild(el("div", "dr-event", "检查点 · " + String(run.checkpoint.summary).slice(0, 120)));
    }
    if (run.state) body.appendChild(el("div", "dr-event", "审计 · Run 明细可在 /api/runs/" + run.id + " 查（事件/工具/模型调用落库）"));
    adv.appendChild(body);
    return adv;
  }

  /* ================= 执行（SSE） ================= */
  function stopLive() {
    if (state.live && state.live.stream) state.live.stream.stop();
    state.live = null;
    liveEls.streamBubble = null;
    liveEls.streamText = null;
    liveEls.streamRaw = "";
    if (liveEls.streamTimer) { clearTimeout(liveEls.streamTimer); liveEls.streamTimer = null; }
    $("pauseBtn").disabled = true;
    $("cancelBtn").disabled = true;
    setRunControls(false);
  }
  function clearComposerAttachments() {
    attachItems = [];
    renderChips();
  }

  function runLiveFrame() {
    setRunControls(true);
    var stream = flowHost();
    var block = el("div", "run-block live-block");
    var head = el("div", "run-head");
    head.appendChild(el("span", "run-label", "正在处理…"));
    head.appendChild(pill("running"));
    block.appendChild(head);
    var act = el("div", "act-card live");
    act.appendChild(el("div", "act-empty", "此刻正在处理，很快告诉你在做什么…"));
    block.appendChild(act);
    liveEls.block = block; liveEls.act = act;
    stream.appendChild(block);
    scrollToLatest();
    $("pauseBtn").disabled = false;
    $("cancelBtn").disabled = false;
  }
  var liveEls = { block: null, act: null, streamBubble: null, streamText: null, streamRaw: "", streamTimer: null };

  function startLive(message) {
    if (!state.active) { createAndRun(message); return; }
    sendMessageRun(state.active.id, message);
  }

  /* v2026-09：Run 创建 = POST（消息不再进 URL；幂等 client_message_id）；SSE 只订阅 */
  function newClientMessageId() {
    try {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    } catch (e) { /* fallthrough */ }
    return "cm_" + Date.now() + "_" + Math.random().toString(36).slice(2, 10);
  }

  function sendMessageRun(cid, message) {
    if (state.live && state.live.stream) { showToast("有任务正在处理，先等它完成或停止"); return; }
    var readyAtt = attachItems.filter(function (it) { return it.state === "ready" && it.id; });
    var ids = readyAtt.map(function (it) { return it.id; });
    if (ids.length) { attachItems = []; renderChips(); }
    state.live = { runId: null, stream: null, containerId: cid, posting: true };
    runLiveFrame();
    api.createRun(cid, message, ids, newClientMessageId()).then(function (r) {
      if (!state.live || !state.live.posting) return;
      if (r && r.ok && r.run_id) {
        state.live.posting = false;
        subscribeRun(r.run_id, false);
      } else {
        stopLive();
        showToast((r && r.error) || "消息提交失败（可能有其他任务正在处理）");
      }
    }).catch(function (e) {
      stopLive();
      showToast("发送失败：" + (e && e.message ? e.message : ""));
    });
  }

  function subscribeRun(runId, placeholder) {
    state.live = { runId: runId, stream: null, containerId: (state.active && state.active.id) || null };
    if (placeholder) addRunPlaceholder(runId);
    startStream(api.runStreamUrl(runId));
  }

  function resumeRun(runId) {
    // 审批通过/暂停后继续：先 POST resume 启动后台执行，再订阅事件
    state.live = { runId: runId, stream: null, containerId: state.active && state.active.id ? state.active.id : null };
    addRunPlaceholder(runId);
    api.resumeRun(runId).then(function () {
      startStream(api.runStreamUrl(runId));
    }).catch(function (e) {
      stopLive();
      showToast("继续失败：" + (e && e.message ? e.message : ""));
    });
  }

  function addRunPlaceholder(runId) {
    setRunControls(true);
    var stream = flowHost();
    liveEls.block = el("div", "run-block live-block");
    var head = el("div", "run-head");
    head.appendChild(el("span", "run-label", "继续上次"));
    head.appendChild(pill("running"));
    liveEls.block.appendChild(head);
    liveEls.act = el("div", "act-card live");
    liveEls.act.appendChild(el("div", "act-empty", "此刻继续处理…"));
    liveEls.block.appendChild(liveEls.act);
    stream.appendChild(liveEls.block);
    scrollToLatest();
  }

  function startStream(url) {
    var handlers = { onEvent: onEvent, onStatus: onStatus };
    state.live.stream = RT.eventClient.start({ url: url, autoReconnect: false, handlers: handlers });
  }

  function onStatus(status, detail) {
    // 运行状态不再单列（§4：去掉进度条/连接条）；用任务头状态词表达
    if (status === "closed" && state.live) {
      $("cancelBtn").disabled = true;
    }
  }

  function onEvent(ev) {
    if (!state.live) return;
    var type = ev.type;
    if (type === "run.started" || type === "task.started") {
      var rid = (ev.payload && (ev.payload.run_id || ev.payload.task_id)) || "";
      state.live.runId = rid || state.live.runId;
      return;
    }
    if (type === "run.cancelled" || type === "task.cancelled") {
      // 用户明确取消后的真实终态（服务端确认已停止执行）
      finalize("stopped");
      return;
    }
    if (type === "source.not_ready") {
      var note = (ev.payload && ev.payload.note) ||
        "部分参考资料仍在处理或处理失败，本次未使用。";
      showToast(note);
      return;
    }
    if (type === "tool.started") {
      if (liveEls.act) {
        var pName = (ev.payload && ev.payload.name) || "工具";
        var pVerb = RT.activity.beVerb(pName);
        var lastLine = liveEls.act.lastChild;
        if (!lastLine || lastLine.textContent.indexOf(pVerb) === -1) {
          var nl = el("div", "act-line", pVerb + "…");
          liveEls.act.appendChild(nl);
          while (liveEls.act.children.length > 4) liveEls.act.removeChild(liveEls.act.firstChild);
        } else {
          lastLine.textContent = pVerb + "…";
        }
      }
      return;
    }
    if (type === "reply_delta") {
      // 回答逐字流：rawContent 累积 + 60ms debounce 全量重渲染（跨 chunk Markdown 正确）
      var dtext = String((ev.payload && ev.payload.text) || "");
      if (!dtext) return;
      var stream2 = flowHost();
      if (!liveEls.streamBubble) {
        liveEls.streamBubble = el("div", "msg-row assistant");
        var bub = el("div", "bubble");
        liveEls.streamText = el("div", "agent-markdown live-markdown", "");
        bub.appendChild(liveEls.streamText);
        liveEls.streamBubble.appendChild(bub);
        stream2.appendChild(liveEls.streamBubble);
        liveEls.streamRaw = "";
      }
      liveEls.streamRaw += dtext;
      if (liveEls.streamTimer) clearTimeout(liveEls.streamTimer);
      liveEls.streamTimer = setTimeout(function () {
        if (liveEls.streamText && liveEls.streamRaw !== undefined) {
          RT.markdown.renderTo(liveEls.streamText, liveEls.streamRaw);
          scrollToLatest();
        }
      }, 60);
      return;
    }
    if (type === "stream_reset") {
      // 上一轮输出未通过校验，重试前清空失败增量（避免“回答重复三次”）
      if (liveEls.streamBubble) {
        liveEls.streamBubble.remove();
        liveEls.streamBubble = null;
        liveEls.streamText = null;
        liveEls.streamRaw = "";
        if (liveEls.streamTimer) { clearTimeout(liveEls.streamTimer); liveEls.streamTimer = null; }
      }
      if (liveEls.act) {
        clear(liveEls.act);
        liveEls.act.appendChild(el("div", "act-empty", "上轮输出未通过校验，自动重试…"));
      }
      return;
    }
    if (type === "assistant.reply") {
      var text = String((ev.payload && (ev.payload.content || ev.payload.summary)) || "").slice(0, 30000);
      if (text) {
        var stream = flowHost();
        if (liveEls.streamBubble) {
          // 已有流式气泡：用最终完整内容重渲染（rawContent 语义）
          if (liveEls.streamText) RT.markdown.renderTo(liveEls.streamText, text);
          liveEls.streamRaw = text;
        } else {
          if (liveEls.act) { liveEls.act.remove(); liveEls.act = null; }
          stream.appendChild(messageRow({ role: "assistant", content: text }));
        }
        scrollToLatest();
      }
      return;
    }
    if (type === "approval.required") { inlineApproval(ev); return; }
    if (type === "task.completed" || type === "run.completed") { finalize("completed"); return; }
    if (type === "task.failed" || type === "run.failed") { finalize("failed"); return; }
    if (type === "runtime.done") {
      if (state.live && state.live.stream) state.live.stream.stop();
      state.live = null;
      refreshRail();
    }
  }

  function inlineApproval(ev) {
    if (!state.live) return;
    var payload = ev.payload || {};
    var runId = (payload && payload.task_id) || (state.live && state.live.runId) || "";
    var stream = flowHost();
    var card = el("div", "approval-inline");
    card.appendChild(el("div", "approval-title", "需要你确认"));
    var desc = el("div", "approval-desc", "此刻准备执行一个需要你确认的操作：");
    card.appendChild(desc);
    (payload.approvals || []).forEach(function (a) {
      card.appendChild(el("div", "approval-line", "· " + actionDesc(a)));
    });
    var btns = el("div", "approval-actions");
    var look = el("button", "ghost-btn", "看看会改什么");
    look.addEventListener("click", function () { openDrawer(runId, "diff"); });
    var deny = el("button", "danger-btn", "暂不");
    var allow = el("button", "primary-btn", "继续");
    deny.addEventListener("click", function () { decideAll(payload.approvals || [], "denied", runId); });
    allow.addEventListener("click", function () { decideAll(payload.approvals || [], "approved", runId); });
    btns.appendChild(look);
    btns.appendChild(deny);
    btns.appendChild(allow);
    card.appendChild(btns);
    stream.appendChild(card);
    scrollToLatest();
  }

  function decideAll(approvals, decision, runId) {
    var chain = Promise.resolve();
    approvals.forEach(function (a) {
      if (!a || !a.id) return;
      chain = chain.then(function () { return api.decideApproval(a.id, decision); });
    });
    chain.then(function () {
      if (decision === "approved" && runId) {
        stopLive();
        resumeRun(runId);
      } else {
        openTask(state.active.id);
      }
    }).catch(function (e) { showToast("处理失败：" + (e && e.message ? e.message : "")); });
  }

  function finalize(status) {
    stopLive();
    if (status === "completed") showToast("已完成");
    else if (status === "stopped") showToast("已停止");
    else showToast("本轮遇到问题");
    if (state.active) {
      var cid = state.active.id;
      api.getTask(cid).then(function (detail) {
        state.active = detail;
        state.showAllRuns = false;
        // 只富化最新一轮（供交付/失败卡），其余轮次点开再取 → 提速
        return enrichLatest().then(renderTask).catch(renderTask);
      }).catch(function () { refreshRail(); });
    }
    refreshRail();
  }

  function enrichLatest() {
    var detail = state.active;
    if (!detail || !detail.runs || !detail.runs.length) return Promise.resolve();
    var runs = detail.runs.slice().sort(function (a, b) { return a.created_at < b.created_at ? -1 : 1; });
    var latest = runs[runs.length - 1];
    if (!latest || isRunEnriched(latest)) return Promise.resolve();
    return fetchRun(latest.id);
  }

  function fetchRun(runId) {
    return api.getRun(runId).then(function (p) { state.runCache[runId] = p; });
  }

  /* ================= 打开任务（可保留同项目 live 流） ================= */
  function resumeLiveVisual() {
    if (!state.live) return;
    setRunControls(true);
    var stream = flowHost();
    if (!liveEls.streamBubble) {
      if (liveEls.streamRaw) {
        liveEls.streamBubble = el("div", "msg-row assistant");
        var bub = el("div", "bubble");
        liveEls.streamText = el("div", "agent-markdown live-markdown");
        RT.markdown.renderTo(liveEls.streamText, liveEls.streamRaw);
        bub.appendChild(liveEls.streamText);
        liveEls.streamBubble.appendChild(bub);
        stream.appendChild(liveEls.streamBubble);
      } else {
        liveEls.streamText = null;
      }
    }
    if (!liveEls.block || !liveEls.block.isConnected) {
      if (!liveEls.streamBubble) runLiveFrame();
    }
    scrollToLatest();
  }

  function openTask(id, opts) {
    var token = ++navigationToken;
    saveDraft();
    closeSidebar();
    surfaceCtx = null;
    opts = opts || {};
    var keepLive = opts.keepLive &&
      !!(state.live && state.live.stream && state.live.containerId === id);
    if (!keepLive) stopLive();
    state.showAllRuns = false;
    return api.getTask(id).then(function (detail) {
      if (token !== navigationToken) return;
      state.active = detail;
      state.runCache = {};
      restoreDraft();
      refreshRail();
      setComposerState(true);
      $("composerInput").placeholder = "告诉此刻接下来要做什么……";
      var renderIfCurrent = function () { if (token === navigationToken) renderTask(); };
      var chain = enrichLatest().then(renderIfCurrent).catch(renderIfCurrent);
      if (keepLive) chain.then(function () { if (token === navigationToken) resumeLiveVisual(); });
      // 刷新/重开页面时：容器内仍有在飞 Run（running）→ 直接重新订阅该 run_id（绝不新建第二个 Run）
      var runs = (detail.runs || []).slice().sort(function (a, b) {
        return a.created_at < b.created_at ? -1 : 1;
      });
      var latestRun = runs[runs.length - 1];
      if (!state.live && latestRun && latestRun.state === "running" && !opts.keepLive) {
        subscribeRun(latestRun.id, true);
      }
      return chain;
    }).catch(function () {
      if (token !== navigationToken) return;
      showToast("打不开这个任务");
      renderTaskList();
    });
  }

  function setCrumb(text) {
    dockComposer();
    closeSidebar();
    document.querySelectorAll("#sidebar .nav-btn").forEach(function (b) {
      var selected = state.page === "settings" ? b.id === "navSettings" : state.page === "materials" ? b.id === "navMaterials" : state.page === "tasklist" && b.dataset.filter === taskFilter;
      b.classList.toggle("active", !!selected);
      if (selected) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    document.querySelectorAll("#mobileBar button").forEach(function (b) { b.classList.toggle("m-active", b.dataset.page === (state.page === "settings" ? "settings" : state.page === "materials" ? "materials" : "workspace")); });
    var c = $("crumbPage");
    if (c) c.textContent = text;
  }
  function setTaskHeadVisible(show) {
    var ws = $("page-workspace");
    if (!ws) return;
    if (show) ws.classList.add("in-task");
    else ws.classList.remove("in-task");
  }
  function setHeader(title, meta) {
    var t = $("taskTitle");
    var m = $("taskMeta");
    if (t) t.textContent = title || "—";
    if (m) m.textContent = meta || "";
    $("btnDetail").disabled = !state.active;
    $("btnArchive").disabled = !state.active;
    var _ps=$("btnProjectSources"); if(_ps) _ps.disabled = !state.active;
    var _ps2=$("btnProjectSettings"); if(_ps2) _ps2.disabled = !state.active;
    $("btnDetail").textContent = "详情";
  }
  function setComposerState(show) {
    dockComposer();
    var wrap = $("page-workspace");
    var composer = document.querySelector(".composer-wrap");
    if (!composer) return;
    if (show) composer.classList.remove("hidden");
    else composer.classList.add("hidden");
    setTaskHeadVisible(show);
  }

  /* ================= 发送（Composer 已接入） ================= */
  /* ================= v24：Composer 附件（当前消息临时使用） ================= */
  var attachItems = [];
  var composerDraftMemo = ""; // {id,name,state:'uploading'|'ready'|'error',scope:'message_only'|'project_source',file}

  function renderChips() {
    var bar = $("composerChips");
    if (!bar) return;
    updateComposerInput();
    clear(bar);
    attachItems.forEach(function (it, idx) {
      var chip = el("span", "att-chip");
      chip.appendChild(el("span", "att-name", (it.name || "").slice(0, 60)));
      if (it.state === "uploading") {
        chip.appendChild(el("span", "att-state", "正在上传…"));
      } else if (it.state === "error") {
        chip.appendChild(el("span", "att-state err", "上传失败"));
        var retry = el("button", null, "重试");
        retry.addEventListener("click", function () { startAttUpload(it, idx); });
        chip.appendChild(retry);
      } else {
        var label;
        if (it.scope === "project_source") label = it.promoted ? "已加入项目来源" : "项目来源";
        else label = "仅本次使用";
        chip.appendChild(el("span", "att-state" + (it.scope === "project_source" ? " src" : ""), label));
        if (it.scope === "message_only" && it.id) {
          var promote = el("button", null, "加入项目来源");
          promote.addEventListener("click", function () {
            api.promoteAttachment(state.active.id, it.id).then(function (r) {
              if (r && r.ok) {
                it.scope = "project_source"; it.promoted = true;
                showToast("已加入项目来源");
                renderChips();
              }
            });
          });
          chip.appendChild(promote);
        }
        var del = el("button", null, "×");
        del.addEventListener("click", function () { removeAttItem(idx); });
        chip.appendChild(del);
      }
      bar.appendChild(chip);
    });
    if (!attachItems.length) clear(bar);
  }

  function removeAttItem(idx) {
    var it = attachItems[idx];
    if (it && it.id && state.active) {
      api.deleteAttachment(state.active.id, it.id).catch(function () {});
    }
    attachItems.splice(idx, 1);
    renderChips();
  }

  function startAttUpload(item, idx) {
    item.state = "uploading";
    renderChips();
    api.uploadProjectAttachment(state.active.id, item.file, item.name).then(function (r) {
      if (r && r.ok) {
        item.id = r.attachment.id;
        item.state = "ready";
        item.scope = r.attachment.attachment_scope || "message_only";
      } else {
        item.state = "error";
      }
      renderChips();
    }).catch(function () {
      item.state = "error";
      renderChips();
    });
  }

  function openAttachmentPicker() {
          var input = document.createElement("input");
          input.type = "file";
          input.multiple = true;
          input.addEventListener("change", function () {
            var files = Array.prototype.slice.call(input.files || []);
            if (!files.length) return;
            var owner = state.active ? Promise.resolve() : api.createProject({ name: autoTitle($("composerInput").value || files[0].name), memory_scope: "project_only", model_pref: composerModelPref() }).then(function (project) { finishNewTask(project, project.title || files[0].name, ""); });
            owner.then(function () {
              files.forEach(function (f) {
                var it = { id: null, name: f.name, state: "uploading", scope: "message_only", file: f };
                attachItems.push(it); startAttUpload(it, attachItems.length - 1);
              });
            }).catch(function () { showToast("暂时无法添加文件，请重试。"); });
            renderChips();
          });
          input.click();
  }

  function bindComposerFile() {
    var btn = $("btnComposerFile");
    if (!btn) return;
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      $("composerFileMenu").classList.toggle("show");
    });
    document.addEventListener("click", function (ev) {
      var menu = $("composerFileMenu");
      if (menu && ev.composedPath().indexOf(menu) < 0 && !btn.contains(ev.target)) menu.classList.remove("show");
    });
    document.querySelectorAll("#composerFileMenu .cfm-item").forEach(function (item) {
      item.addEventListener("click", function () {
        $("composerFileMenu").classList.remove("show");
        var kind = item.getAttribute("data-kind");
        if (kind === "upload") {
          openAttachmentPicker();
        } else {
          pickFromSources();
        }
      });
    });
  }

  function pickFromSources() {
    if (!state.active) { showToast("先添加文件或开始一个任务，再选择项目来源。"); return; }
    var pid = state.active.id;
    api.getProject(pid).then(function (detail) {
      var srcs = detail.sources || [];
      if (!srcs.length) { showToast("这个任务还没有资料，先添加资料再继续"); return; }
      var picked = [];
      var menu = $("composerFileMenu");
      clear(menu);
      srcs.forEach(function (s) {
        var row = el("button", "cfm-item", "[ ] " + s.display_name);
        row.addEventListener("click", function () {
          var idx = picked.indexOf(s.id);
          if (idx >= 0) { picked.splice(idx, 1); row.textContent = "[ ] " + s.display_name; }
          else { picked.push(s.id); row.textContent = "[x] " + s.display_name; }
        });
        menu.appendChild(row);
      });
      var done = el("button", "cfm-item", "✓ 添加所选来源（" + picked.length + "）");
      done.addEventListener("click", function () {
        if (!picked.length) { showToast("未选择任何来源"); return; }
        api.createAttachmentRefs(pid, picked).then(function (r) {
          (r.attachments || []).forEach(function (a) {
            attachItems.push({ id: a.id, name: a.display_name, state: "ready", scope: "project_source", file: null });
          });
          renderChips();
          showToast("已从项目来源添加 " + (r.attachments || []).length + " 个文件");
          resetFileMenu();
        });
      });
      menu.appendChild(done);
      var cancel = el("button", "cfm-item", "返回");
      cancel.addEventListener("click", resetFileMenu);
      menu.appendChild(cancel);
      menu.classList.add("show");
    }).catch(function () { showToast("读取来源失败"); });
  }

  function resetFileMenu() {
    var menu = $("composerFileMenu");
    if (!menu) return;
    clear(menu);
    var u = el("button", "cfm-item", "上传文件（仅本次使用）");
    u.setAttribute("data-kind", "upload");
    u.addEventListener("click", function () {
      menu.classList.remove("show");
      openAttachmentPicker();
    });
    var s = el("button", "cfm-item", "从项目来源选择");
    s.setAttribute("data-kind", "source");
    s.addEventListener("click", function () { menu.classList.remove("show"); pickFromSources(); });
    menu.appendChild(u);
    menu.appendChild(s);
  }

  function composerModelPref() {
    var sel = $("composerModel");
    return sel ? sel.value : "";
  }
  function renderComposerModel() {
    var sel = $("composerModel");
    if (!sel) return;
    var cfg = state.config || {};
    sel.innerHTML = "";
    var opt0 = document.createElement("option");
    opt0.value = ""; opt0.textContent = "默认模型";
    sel.appendChild(opt0);
    if (cfg.local_model) {
      var opt1 = document.createElement("option");
      opt1.value = "local"; opt1.textContent = "本地模型";
      sel.appendChild(opt1);
    }
    sel.value = (state.active && state.active.model_pref) ? state.active.model_pref
              : (localStorage.getItem("forge.composer.model") || "");
  }

  /* ---------- 对话流智能滚动：默认跟随，上翻暂停，↓ 回到最新 ---------- */
  var streamAutoScroll = true;
  var STREAM_NEAR_BOTTOM_PX = 380;   // ≈15 行文字（15px×1.72≈25.8px/行）

  function scrollToLatest(force) {
    var st = $("stream");
    if (!st) return;
    if (!force && !streamAutoScroll) return;   // 用户已上翻 → 暂停自动滚
    st.scrollTop = st.scrollHeight;
  }

  function bindStreamScroll() {
    var st = $("stream");
    if (!st) return;
    var btn = $("scrollLatest");
    st.addEventListener("scroll", function () {
      var near = (st.scrollHeight - st.scrollTop - st.clientHeight) <= STREAM_NEAR_BOTTOM_PX;
      streamAutoScroll = near;
      if (btn) btn.classList.toggle("show", !near);
    });
    if (btn) {
      btn.addEventListener("click", function () {
        streamAutoScroll = true;
        st.scrollTop = st.scrollHeight;
        btn.classList.remove("show");
      });
    }
  }

  function sendNow() {
    var input = $("composerInput");
    var text = (input.value || "").trim();
    if (state.live && state.live.stream) { showToast("有任务正在处理，先等它完成或停止"); return; }
    var ready = attachItems.filter(function (it) { return it.state === "ready" && it.id; });
    if (ready.some(function (it) { return it.state === "error"; })) ready = [];
    if (!text && !ready.length) return;
    input.value = "";
    composerDraftMemo = "";
    delete draftCache[state.active ? state.active.id : "home"];
    updateComposerInput();
    if (ready.length && state.active) {
      var ids = ready.map(function (it) { return it.id; });
      attachItems = [];
      renderChips();
      var pid = state.active.id;
      api.createProjectMessage(pid, text || "请分析我提供的附件。", ids).then(function (r) {
        if (r && r.ok && r.run_id) {
          subscribeRun(r.run_id, false);
        } else {
          showToast((r && r.error) || "消息创建失败（可能有其他任务正在处理）");
        }
      }).catch(function (e) { showToast("发送失败：" + (e && e.message ? e.message : "")); });
      return;
    }
    if (!state.active) { createAndRun(text); return; }
    startLive(text);
  }

  /* ================= 初始化 ================= */
  function bind() {
    var send = $("sendBtn");
    if (send) send.addEventListener("click", sendNow);
    bindTopActions();
    bindStreamScroll();
    var cmodel = $("composerModel");
    if (cmodel) {
      cmodel.addEventListener("change", function () {
        var val = cmodel.value;
        try { localStorage.setItem("forge.composer.model", val); } catch (e) {}
        if (state.active) {
          api.updateProject(state.active.id, { model_pref: val })
            .then(function () { showToast("已切换模型：" + (val === "local" ? "本地模型" : "默认模型")); })
            .catch(function () { showToast("切换模型失败"); });
        }
      });
      renderComposerModel();
    }
    var ci0 = $("composerInput");
    if (ci0) ci0.addEventListener("input", function () { composerDraftMemo = ci0.value; updateComposerInput(); });
    bindComposerFile();
    var input = $("composerInput");
    if (input) input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229 && !matchMedia("(max-width: 760px)").matches) { ev.preventDefault(); sendNow(); }
    });
    var btnNew = $("btnNewTask");
    if (btnNew) btnNew.addEventListener("click", createNewTask);
    var btnDetail = $("btnDetail");
    if (btnDetail) btnDetail.addEventListener("click", function () {
      if (btnDetail.disabled) return;
      openDrawer();
    });
    var btnPS = $("btnProjectSources");
    if (btnPS) btnPS.addEventListener("click", openProjectSourcesPanel);
    var btnPSt = $("btnProjectSettings");
    if (btnPSt) btnPSt.addEventListener("click", openProjectSettingsPanel);
    var btnArchive = $("btnArchive");
    if (btnArchive) btnArchive.addEventListener("click", function () {
      if (!state.active) return;
      api.archiveTask(state.active.id).then(function () {
        showToast("已归档到回收站");
        state.active = null;
        refreshRail().then(renderTaskList);
      }).catch(function () { showToast("归档失败"); });
    });
    var closeB = $("drawerClose");
    if (closeB) closeB.addEventListener("click", function () {
      document.querySelector(".app").classList.remove("drawer-open");
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") {
        document.querySelector(".app").classList.remove("drawer-open");
      }
    });
    var pause = $("pauseBtn");
    if (pause) {
      // P2：当前 PAUSED 只是 DB 状态、不能真正暂停在飞执行 → 不再向普通用户暴露假能力
      pause.style.display = "none";
    }
    var cancel = $("cancelBtn");
    if (cancel) cancel.addEventListener("click", function () {
      if (cancel.disabled || !state.live || !state.live.runId) return;
      api.runAction(state.live.runId, "cancel").then(function () {
        showToast("已停止");
        if (state.live && state.live.stream) state.live.stream.stop();
        finalize("stopped");
      }).catch(function () { showToast("停止失败"); });
    });

    // 左栏导航：项目列表 / 资料 + 查看全部项目
    bindNav("navTasks", function () { goHome("all"); });
    bindNav("navRunning", function () { goHome("running"); });
    bindNav("navWaiting", function () { goHome("waiting"); });
    $("newDraftBtn").addEventListener("click", function () { goHome().then(function () { $("composerInput").focus(); }); });
    $("sidebarHome").addEventListener("click", function () { goHome(); });
    $("sidebarToggle").addEventListener("click", function () {
      var app = document.querySelector(".app");
      if (matchMedia("(max-width: 760px)").matches) app.classList.toggle("sidebar-open");
      else app.classList.toggle("sidebar-collapsed");
      syncSidebar();
      if (app.classList.contains("sidebar-open")) $("newDraftBtn").focus();
    });
    $("sidebarCollapse").addEventListener("click", function () {
      if (matchMedia("(max-width: 760px)").matches) closeSidebar();
      else document.querySelector(".app").classList.add("sidebar-collapsed");
      syncSidebar(); $("sidebarToggle").focus();
    });
    $("sidebarBackdrop").addEventListener("click", function () { closeSidebar(); $("sidebarToggle").focus(); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { closeSidebar(); }
      if (e.key === "Tab" && document.querySelector(".app").classList.contains("sidebar-open")) {
        var nodes = $("taskSidebar").querySelectorAll("button, [tabindex=\"0\"]");
        var first = nodes[0], last = nodes[nodes.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
    window.addEventListener("resize", syncSidebar); syncSidebar();
    bindNav("navMaterials", loadMaterialsThen(renderMaterials));
    bindNav("navSettings", renderSettings);
    var more = $("railMoreTasks");
    if (more) more.addEventListener("click", function () { goHome("all"); });
    renderWorkspaceButton();
    // 顶栏品牌（此刻 NOW）点击 → 返回主页
    var brand = document.querySelector(".topbar .logo, .topbar .brand-name");
    if (brand) brand.addEventListener("click", function () { goHome(); });
    bindContextStats();
  }

  function bindNav(id, fn) {
    var b = $(id);
    if (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll("#sidebar .nav-btn").forEach(function (x) { x.classList.remove("active"); });
        b.classList.add("active");
        fn();
      });
    }
  }

  function loadMaterialsThen(fn) {
    return function () {
      api.listArtifacts({}).then(function (d) {
        state.materials.artifacts = d.artifacts || [];
      }).finally(function () { fn(); });
    };
  }

  function createNewTask() {
    showToast("正在弹出文件夹选择框…");
    api.pickWorkLocation().then(function (res) {
      if (res && res.canceled) { showToast("已取消选择"); return; }
      if (res && res.error) { showToast("选择失败：" + res.error); return; }
      var p = res && res.path;
      if (!p) { showToast("未选择文件夹"); return; }
      var name = p.split(/[\\/]/).pop() || "新任务";
      var targetId = state.active && state.active.id;
      resolveWorkLocation(p, name).then(function (wl) {
        if (targetId) return api.updateProject(targetId, {work_location_id: wl.id}).then(function () {
          if (!state.active || state.active.id !== targetId) return;
          state.active.work_location = wl; state.active.work_location_name = name; state.active.work_location_path = p;
          renderWorkspaceButton(); showToast("工作文件夹已更新");
        });
        return api.createProject({ name: name, memory_scope: "project_only",
          model_pref: composerModelPref(), work_location_id: wl.id }).then(function (project) {
          finishNewTask(project, name, p);
        });
      }).catch(function (e) { showToast("创建失败：" + (e && e.message ? e.message : "")); });
    }).catch(function () { showToast("无法打开文件夹选择器"); });
  }

  /* 新建任务 = 选择文件夹（系统原生选择器）→ 绑定工作区 */
  function resolveWorkLocation(path, name) {
    return api.listWorkLocations().then(function (list) {
      var ex = (list || []).find(function (w) { return w.local_path === path; });
      return ex ? Promise.resolve(ex) : api.createWorkLocation(name, path);
    });
  }

  function finishNewTask(project, name, wlPath) {
    state.active = {
      id: project.id, title: project.title || name, session_id: project.session_id,
      messages: [], runs: [], stat: { runs: 0, messages: 0 },
      memory_scope: project.memory_scope || "project_only", instructions: project.instructions || "",
      work_location_name: wlPath ? (wlPath.split(/[\\/]/).pop() || name) : "",
      work_location_path: wlPath
    };
    refreshRail();
    renderWorkspaceButton();
    renderTask();
    showToast(wlPath ? ("已用文件夹「" + name + "」新建任务") : "任务已创建：开始和此刻工作");
  }

  function init() {
    api = RT.api;
    [["navTasks", "list"], ["navRunning", "clock"], ["navWaiting", "bell"], ["navMaterials", "folder"], ["navSettings", "sliders"]].forEach(function (nav) {
      var icon = $(nav[0]).querySelector(".nav-symbol");
      icon.innerHTML = RT.icon(nav[1], 17);
    });
    $("btnComposerFile").innerHTML = RT.icon("plus", 15) + '<span class="attach-label">添加文件</span>';
    $("btnComposerFile").setAttribute("aria-label", "添加文件");
    $("btnComposerFile").title = "添加文件";
    var folder = $("btnNewTask");
    var foot = document.querySelector(".composer-foot");
    foot.insertBefore(folder, $("composerModel"));
    document.querySelector(".composer-top").remove();
    bind();
    applyThemeInit();
    bindSearch();         // Ctrl+K / ⌕（如顶栏按钮存在）
    refreshBadges();
    loadMaterials();
    api.runtimeStatus().then(function (s) {
      state.config = (s && s.config) || {};
      renderComposerModel();
    }).catch(function () {});
    refreshRail().then(function () { renderTaskList(); });
    var btnDetail = $("btnDetail");
    if (btnDetail) btnDetail.disabled = true;
    var btnPause = $("pauseBtn");
    if (btnPause) btnPause.disabled = true;
    var btnCancel = $("cancelBtn");
    if (btnCancel) btnCancel.disabled = true;
  }

  function loadMaterials() {
    api.listArtifacts({}).then(function (d) {
      state.materials.artifacts = d.artifacts || [];
    }).catch(function () {});
  }

  function initWorkspaceSelector() {
    api.listProjects().then(function (projects) {
      initWorkspaceSelectorLabel(projects);
      fillWsMenu(projects);
      if (!projects.length) {
        // 确保默认项目存在
        RT.http.get("/api/projects").then(function () {});
      }
    }).catch(function () {
      initWorkspaceSelectorLabel([]);
    });
  }

  function refreshBadges() {
    api.listApprovals({ state: "pending" }).then(function (d) {
      var b = document.getElementById("approvalBadge");
      if (b) b.textContent = String(d.total || 0);
      var top = document.getElementById("approvalTop");
      if (top) top.title = "待确认 " + (d.total || 0);
    }).catch(function () {});
  }

  function showToast(msg) {
    var t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.add("show");
    setTimeout(function () { t.classList.remove("show"); }, 2200);
  }

  /* ================= 导出（兼容旧调用） ================= */
  /* ================= P2：全局搜索 / 通知中心 / 回收站 / 工作位置 / 拖拽上传 ================= */

  /* ---- 全局搜索（Ctrl+K / 顶栏搜索键） ---- */
  function bindSearch() {
    var btn = $("btnSearch");
    if (btn) btn.addEventListener("click", function () { openSearch(); });
    document.addEventListener("keydown", function (ev) {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") {
        ev.preventDefault();
        openSearch();
      }
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closeSearch();
    });
  }

  function openSearch() {
    var ov = $("searchOverlay");
    if (!ov) {
      buildSearchOverlay();
      ov = $("searchOverlay");
    }
    ov.classList.add("show");
    var input = $("searchInput");
    if (input) { input.value = ""; setTimeout(function () { input.focus(); }, 30); }
    var results = $("searchResults");
    if (results) {
      clear(results);
      results.appendChild(el("div", "search-hint", "查找任务 / 消息 / 资料…"));
    }
  }
  function closeSearch() {
    var ov = $("searchOverlay");
    if (ov) ov.classList.remove("show");
  }

  function buildSearchOverlay() {
    var ov = el("div", "search-overlay");
    ov.id = "searchOverlay";
    var box = el("div", "search-box");
    var input = document.createElement("input");
    input.id = "searchInput";
    input.placeholder = "搜索任务、消息、资料…";
    input.type = "text";
    input.addEventListener("input", function () { scheduleSearch(input.value); });
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") runSearch(input.value);
    });
    box.appendChild(input);
    var results = el("div", "search-results");
    results.id = "searchResults";
    box.appendChild(results);
    ov.appendChild(box);
    document.querySelector(".app").appendChild(ov);
  }

  var _searchTimer = null;
  function scheduleSearch(q) {
    if (_searchTimer) clearTimeout(_searchTimer);
    _searchTimer = setTimeout(function () {
      if (q && q.trim()) runSearch(q);
    }, 260);
  }
  function runSearch(q) {
    q = (q || "").trim();
    var results = $("searchResults");
    if (!results) return;
    if (!q) { clear(results); results.appendChild(el("div", "search-hint", "输入关键词")); return; }
    results.appendChild(el("div", "search-wait", "搜索中…"));
    api.search(q).then(function (groups) {
      clear(results);
      var any = false;
      groups.forEach(function (g) {
        if (!g.items.length) return;
        any = true;
        var sec = el("div", "search-group");
        sec.appendChild(el("div", "search-group-title", g.label));
        sec.appendChild(el("div", "search-list"));
        g.items.forEach(function (it) {
          var row = el("div", "search-item");
          if (g.kind === "task") {
            row.addEventListener("click", function () { closeSearch(); openTask(it.id); });
            row.appendChild(el("span", "search-item-title", (it.title || "").slice(0, 46)));
            row.appendChild(el("span", "search-item-sub", "任务"));
          } else if (g.kind === "run") {
            row.addEventListener("click", function () { closeSearch(); openTask(it.container_id); });
            row.appendChild(el("span", "search-item-title", (it.goal || "").slice(0, 46)));
            row.appendChild(el("span", "search-item-sub", st(it.state).label));
          } else if (g.kind === "message") {
            row.addEventListener("click", function () { closeSearch(); openTask(it.container_id); });
            row.appendChild(el("span", "search-item-title", (it.content || "").slice(0, 46)));
            row.appendChild(el("span", "search-item-sub", it.role === "user" ? "你说" : "此刻"));
          } else {
            var a = el("a", "search-item", null);
            a.href = api.artifactDownloadUrl(it.id);
            a.appendChild(el("span", "search-item-title", (it.name || "").slice(0, 46)));
            a.appendChild(el("span", "search-item-sub", it.kind || "文件"));
            row = a;
          }
          sec.appendChild(row);
        });
        results.appendChild(sec);
      });
      if (!any) results.appendChild(el("div", "search-hint", "没有找到「" + q + "」相关内容"));
    }).catch(function () {
      clear(results);
      results.appendChild(el("div", "search-hint", "搜索失败，请重试"));
    });
  }

  /* ---- 通知中心 ---- */
  function bindNotifications() {
    var btn = $("btnNotify");
    if (btn) btn.addEventListener("click", function () { toggleNotifyPanel(); });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closeNotifyPanel();
    });
  }
  function toggleNotifyPanel() {
    var p = $("notifyPanel");
    if (!p) { buildNotifyPanel(); p = $("notifyPanel"); }
    p.classList.toggle("show");
    if (p.classList.contains("show")) loadNotifications();
  }
  function closeNotifyPanel() {
    var p = $("notifyPanel");
    if (p) p.classList.remove("show");
  }
  function buildNotifyPanel() {
    var p = el("div", "notify-panel");
    p.id = "notifyPanel";
    p.appendChild(el("div", "notify-head", "通知"));
    var body = el("div", "notify-body");
    body.id = "notifyBody";
    p.appendChild(body);
    document.querySelector(".app").appendChild(p);
  }
  function loadNotifications() {
    var body = $("notifyBody");
    if (!body) return;
    clear(body);
    body.appendChild(el("div", "search-wait", "加载中…"));
    api.notifications().then(function (d) {
      clear(body);
      var items = d.items || [];
      if (!items.length) {
        body.appendChild(el("div", "notify-empty", "还没有需要你处理或留意的事项。"));
      }
      items.forEach(function (it) {
        var row = el("div", "notify-item " + it.type);
        row.appendChild(el("div", "notify-type", it.type === "approval" ? "等你确认" : "状态提醒"));
        row.appendChild(el("div", "notify-title", it.title + " · " + (it.subtitle || "")));
        var created = it.created_at ? relTime(it.created_at) : "";
        row.appendChild(el("div", "notify-meta", created + (it.container_id ? " · 在任务中" : "")));
        if (it.container_id) {
          row.addEventListener("click", function () { closeNotifyPanel(); openTask(it.container_id); });
        }
        body.appendChild(row);
      });
      var badge = $("notifyBadge");
      if (badge) {
        badge.textContent = d.unread > 0 ? String(d.unread) : "";
        badge.classList.toggle("show", d.unread > 0);
      }
    }).catch(function () {
      clear(body);
      body.appendChild(el("div", "notify-empty", "加载失败"));
    });
  }
  function refreshNotifyBadge() {
    api.notifications().then(function (d) {
      var badge = $("notifyBadge");
      if (badge) {
        badge.textContent = d.unread > 0 ? String(d.unread) : "";
        badge.classList.toggle("show", d.unread > 0);
      }
    }).catch(function () {});
  }

  /* ---- 回收站（任务页入口 / 设置·数据） ---- */
  function renderTrash() {
    navigationToken += 1;
    captureOrigin();
    state.page = "trash";
    setCrumb("回收站");
    setComposerState(false);
    var stream = $("stream");
    clear(stream);
    var wrap = el("div", "tasklist");
    wrap.insertBefore(surfaceBackButton(""), wrap.firstChild);
    var sec = el("div", "tl-sec");
    sec.appendChild(el("div", "tl-head", "回收站 · 已归档的项目"));
    api.listTasks({ archived: true, limit: 100 }).then(function (d) {
      (d.tasks || []).forEach(function (c) {
        var row = el("div", "tl-row");
        row.appendChild(el("span", "tl-title", c.title || "未命名项目"));
        row.appendChild(el("span", "tl-note", "归档于 " + relTime(c.archived_at)));
        var restore = el("button", "ghost-btn small", "恢复");
        restore.addEventListener("click", function () {
          api.restoreTask(c.id).then(function () { showToast("已恢复"); renderTrash(); refreshRail(); });
        });
        row.appendChild(restore);
        var del = el("button", "danger-btn small", "彻底清除");
        del.addEventListener("click", function () {
          if (!confirm("确定彻底清除该任务与全部记录？此操作不可恢复。")) return;
          api.deleteTask(c.id).then(function () { showToast("已清除"); renderTrash(); refreshRail(); });
        });
        row.appendChild(del);
        sec.appendChild(row);
      });
      if (!(d.tasks || []).length) sec.appendChild(el("div", "tl-empty", "回收站是空的。"));
      wrap.appendChild(sec);
      stream.appendChild(wrap);
    }).catch(function () {
      sec.appendChild(el("div", "tl-empty", "加载失败"));
      wrap.appendChild(sec);
      stream.appendChild(wrap);
    });
  }

  /* ---- 工作位置切换（§十） ---- */
  var currentProjectId = null;
  function bindWorkspaceSwitch() {
    var ws = $("workspaceSelector");
    if (!ws) return;
    ws.addEventListener("click", function (ev) {
      ev.stopPropagation();
      toggleWsMenu();
    });
    document.addEventListener("click", function (ev) {
      var menu = $("wsMenuPop");
      if (menu && !menu.contains(ev.target)) menu.classList.remove("show");
    });
    initWorkspaceSelector();
  }
  function toggleWsMenu() {
    var menu = $("wsMenuPop");
    if (!menu) { buildWsMenu(); menu = $("wsMenuPop"); }
    menu.classList.toggle("show");
  }
  function buildWsMenu() {
    var menu = el("div", "ws-menu");
    menu.id = "wsMenuPop";
    var title = el("div", "ws-menu-title", "工作位置");
    menu.appendChild(title);
    var list = el("div", "ws-menu-list");
    list.id = "wsMenuList";
    menu.appendChild(list);
    var create = el("button", "ws-menu-new", "＋ 新建工作位置");
    create.addEventListener("click", function () {
      var name = prompt("给这个工作位置起个名字（如：学习平台）", "学习平台");
      if (!name) return;
      RT.http.post("/api/projects/create", { name: name }).then(function () {
        menu.classList.remove("show");
        initWorkspaceSelector();
        showToast("已创建「" + name + "」");
      }).catch(function () { showToast("创建失败"); });
    });
    menu.appendChild(create);
    document.querySelector(".app").appendChild(menu);
  }
  function fillWsMenu(projects) {
    var list = $("wsMenuList");
    if (!list) return;
    clear(list);
    projects.forEach(function (p) {
      var row = el("div", "ws-menu-item" + (currentProjectId === p.id ? " active" : ""), p.name);
      row.addEventListener("click", function () {
        currentProjectId = p.id;
        list.querySelectorAll(".ws-menu-item").forEach(function (x) { x.classList.remove("active"); });
        row.classList.add("active");
        document.querySelector(".ws-switch").textContent = "工作位置：" + p.name + " ▾";
        $("wsMenuPop").classList.remove("show");
        refreshRail();
      });
      list.appendChild(row);
    });
    if (!projects.length) list.appendChild(el("div", "ws-menu-empty", "暂无工作位置"));
  }
  function initWorkspaceSelectorLabel(projects) {
    var ws = $("workspaceSelector");
    if (!ws) return;
    var cur = projects.find(function (p) { return p.id === currentProjectId; });
    var name = cur ? cur.name : (projects.length ? projects[0].name : "默认项目");
    ws.textContent = "工作位置：" + name + " ▾";
  }

  function skeletonRows(n, tall) {
    var wrap = el("div", "");
    for (var k = 0; k < n; k++) {
      wrap.appendChild(el("div", "skel " + (tall ? "skel-row" : "skel-line")));
    }
    return wrap;
  }
  var containersFilled = false;

  /* ================= v17：左下账户 + 右上上下文统计 ================= */
  var THEME_ORDER = ["auto", "dark", "light"];
  var THEME_NAME = { auto: "跟随系统", dark: "深色", light: "浅色" };

  function bindAccount() {
    var btn = $("accountBtn");
    if (!btn) return;
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      $("accountMenu").classList.toggle("show");
      syncAccountMenu();
    });
    document.addEventListener("click", function (ev) {
      var menu = $("accountMenu");
      if (menu && !menu.contains(ev.target)) menu.classList.remove("show");
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") {
        var menu = $("accountMenu");
        if (menu) menu.classList.remove("show");
      }
    });
    document.querySelectorAll("#accountMenu .am-item[data-act]").forEach(function (item) {
      item.addEventListener("click", function () { onAccountAction(item); });
    });
  }

  function syncAccountMenu() {
    var t = $("amTheme");
    if (t) t.textContent = THEME_NAME[themeSetting()] || "跟随系统";
    var n = $("amNotify");
    if (n) n.textContent = localStorage.getItem("forge.notify") !== "off" ? "已开启" : "已关闭";
    var b = $("approvalBadge");
    if (b) b.textContent = "";
  }

  function onAccountAction(item) {
    $("accountMenu").classList.remove("show");
    var act = item.getAttribute("data-act");
    if (act === "settings") { renderSettings(); }
    else if (act === "trash") { renderTrash(); }
  }

  /* 顶栏右侧：资料下拉 + 添加资料 */
  function bindTopActions() {
    var mbBtn = $("moreBtn");
    var mbMenu = $("moreMenu");
    if (mbBtn && mbMenu) {
      mbBtn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        mbMenu.classList.toggle("show");
      });
      mbMenu.querySelectorAll(".dd-item[data-act]").forEach(function (item) {
        item.addEventListener("click", function () {
          mbMenu.classList.remove("show");
          var act = item.getAttribute("data-act");
          if (act === "materials") { loadMaterialsThen(renderMaterials)(); }
          else if (act === "settings") { renderSettings(); }
          else if (act === "trash") { renderTrash(); }
          else if (act === "about") { showToast("此刻 · NOW · 本地个人助手"); }
        });
      });
      document.addEventListener("click", function (ev) {
        if (mbMenu.classList.contains("show") && !mbMenu.contains(ev.target) && !mbBtn.contains(ev.target)) {
          mbMenu.classList.remove("show");
        }
      });
    }
  }

  /* 输入区左上按钮：显示当前工作区（文件夹名 + 路径），未选则提示选择文件夹 */
  function renderWorkspaceButton() {
    var b = $("btnNewTask");
    if (!b) return;
    var a = state.active || {};
    var wl = a.work_location || {};
    var name = a.work_location_name || wl.name || "";
    var p = a.work_location_path || wl.local_path || "";
    if (!name && a.work_location_id != null && !state._wlHydrating) {
      state._wlHydrating = true;
      api.listWorkLocations().then(function (list) {
        var w = (list || []).find(function (x) { return String(x.id) === String(a.work_location_id); });
        if (w && state.active && state.active.id === a.id) { state.active.work_location = w; renderWorkspaceButton(); }
      }).catch(function () {}).then(function () { state._wlHydrating = false; });
    }
    var label = name || "工作文件夹 · 可选";
    b.innerHTML = RT.icon("folder", 15);
    b.appendChild(el("span", "workspace-label", label));
    b.title = p ? (name + "\n" + p) : "选择一个文件夹作为这个任务的工作区";
  }

  function addMaterialViaTop() {
    var input = document.createElement("input");
    input.type = "file";
    input.accept = ".pdf,.docx,.xlsx,.pptx,.txt,.md,.csv,.json,.png,.jpg,.py,.zip";
    input.addEventListener("change", function () {
      var f = input.files && input.files[0];
      if (!f) return;
      showToast("正在添加资料…");
      api.uploadMaterial(f, f.name).then(function (r) {
        if (r && r.ok) { showToast("资料已就绪：" + r.artifact.name); loadMaterialsThen(renderMaterials)(); }
        else showToast("上传失败：" + (r && r.error ? r.error : "未知"));
      }).catch(function () { showToast("上传失败：网络错误"); });
    });
    input.click();
  }

  function cycleTheme() {
    var cur = THEME_ORDER.indexOf(themeSetting());
    var next = THEME_ORDER[(cur + 1) % THEME_ORDER.length];
    localStorage.setItem("forge.theme.main", next);
    applyTheme();
    showToast("外观：" + THEME_NAME[next]);
  }

  function toggleNotifyPref() {
    var next = localStorage.getItem("forge.notify") === "off" ? "on" : "off";
    localStorage.setItem("forge.notify", next);
    showToast("通知" + (next === "on" ? "已开启" : "已关闭"));
  }

  function bindContextStats() {
    var chip = $("contextStats");
    if (!chip) return;
    chip.addEventListener("click", function () {
      var pop = $("ctxPop");
      if (!pop) {
        pop = el("div", "ctx-pop");
        pop.id = "ctxPop";
        document.querySelector(".app").appendChild(pop);
      }
      pop.classList.toggle("show");
      if (pop.classList.contains("show")) fillContextPop(pop);
    });
    document.addEventListener("click", function (ev) {
      var pop = $("ctxPop");
      if (pop && !pop.contains(ev.target) && ev.target.id !== "contextStats") pop.classList.remove("show");
    });
    updateContextStats();
  }

  function currentContextText() {
    var detail = state.active;
    if (!detail || !detail.messages) return null;
    var msgs = detail.messages;
    var chars = 0;
    msgs.forEach(function (m) { chars += String(m.content || "").length; });
    var tokens = Math.max(1, Math.round(chars / 1.8));
    return { messages: msgs.length, chars: chars, tokens: tokens, runs: detail.stat ? detail.stat.runs : 0 };
  }

  function updateContextStats() {
    var chip = $("contextStats");
    if (!chip) return;
    var ctx = currentContextText();
    if (!ctx) { chip.textContent = "上下文 —"; return; }
    var k = ctx.tokens >= 1000 ? (ctx.tokens / 1000).toFixed(1) + "k" : String(ctx.tokens);
    chip.textContent = "上下文 · " + ctx.messages + " 条 · ≈" + k;
  }

  function fillContextPop(pop) {
    clear(pop);
    var ctx = currentContextText();
    pop.appendChild(el("div", "ctx-title", "上下文统计"));
    if (!ctx) { pop.appendChild(el("div", "ctx-row", "未打开任务")); return; }
    pop.appendChild(el("div", "ctx-row", "消息：" + ctx.messages + " 条"));
    pop.appendChild(el("div", "ctx-row", "字符：" + ctx.chars.toLocaleString()));
    pop.appendChild(el("div", "ctx-row", "估算 tokens：≈" + ctx.tokens.toLocaleString()));
    pop.appendChild(el("div", "ctx-row", "执行轮次：" + ctx.runs + " 次"));
    pop.appendChild(el("div", "ctx-note", "上下文窗口由系统自动管理；统计仅用于感知规模。"));
  }

  /* ================= v23：Project 面板（来源 / 项目设置） ================= */
  function scopeLabel(s) { return s === "global" ? "使用全局记忆" : "仅此项目"; }

  function openProjectSourcesPanel() {
    if (!state.active) return;
    openDrawerShell();
    var host = $("detailDrawerBody");
    clear(host);
    var pid = state.active.id;
    var head = el("div", "dr-hero");
    head.appendChild(el("b", null, "资料 · " + (state.active.title || "")));
    head.appendChild(el("div", "dr-sub", "此刻在这个任务里可以参考的资料；与此刻生成的成果分开管理。"));
    host.appendChild(head);
    var addBtn = el("button", "primary-btn small", "＋ 添加文件");
    addBtn.addEventListener("click", function () {
      var input = document.createElement("input");
      input.type = "file";
      input.addEventListener("change", function () {
        var f = input.files && input.files[0];
        if (!f) return;
        showToast("正在添加来源…");
        api.uploadProjectSource(pid, f, f.name).then(function (r) {
          if (r && r.ok) { showToast("已添加来源：" + r.source.display_name); openProjectSourcesPanel(); }
          else showToast("添加失败：" + (r && r.error ? r.error : ""));
        });
      });
      input.click();
    });
    host.appendChild(addBtn);
    var list = el("div", "dr-sec");
    list.appendChild(el("h4", null, "资料"));
    var skelHost = el("div", "");
    skelHost.appendChild(skeletonRows(3, true));
    list.appendChild(skelHost);
    api.getProject(pid).then(function (detail) {
      if (skelHost && skelHost.parentNode) skelHost.parentNode.removeChild(skelHost);
      var srcs = detail.sources || [];
      if (!srcs.length) list.appendChild(el("div", "dr-empty", "还没有资料。点上方“＋ 添加文件”上传 PDF/Word/Excel/图片/文本等。"));
      srcs.forEach(function (s) {
        var row = el("div", "tl-row");
        row.appendChild(el("span", "tl-title", (s.display_name || "").slice(0, 50)));
        row.appendChild(el("span", "pill small blue", s.parse_status === "ok" ? "可用" : "暂时无法使用"));
        var del = el("button", "ghost-btn small", "移除");
        del.addEventListener("click", function () {
          api.deleteProjectSource(pid, s.id).then(function () {
            showToast("已移除来源");
            openProjectSourcesPanel();
          });
        });
        row.appendChild(del);
        list.appendChild(row);
      });
      var sec2 = el("div", "dr-sec");
      sec2.appendChild(el("h4", null, "成果（此刻生成）"));
      api.projectArtifacts(pid).then(function (d) {
        var arts = d.artifacts || [];
        if (!arts.length) sec2.appendChild(el("div", "dr-empty", "此刻完成工作后生成的成果会出现在这里（不会混入资料）。"));
        arts.forEach(function (a) {
          var link = el("a", "dr-art-link", "↓ " + a.name + " · " + (a.kind || "文件"));
          link.href = api.artifactDownloadUrl(a.id);
          sec2.appendChild(link);
        });
      }).catch(function () {});
      list.appendChild(sec2);
    }).catch(function () {});
    host.appendChild(list);
  }

  function openProjectSettingsPanel() {
    if (!state.active) return;
    openDrawerShell();
    var host = $("detailDrawerBody");
    clear(host);
    var pid = state.active.id;
    api.getProject(pid).then(function (detail) {
      var hero = el("div", "dr-hero");
      hero.appendChild(el("b", null, "项目设置 · " + (detail.title || "")));
      host.appendChild(hero);
      var sec = el("div", "dr-sec");
      sec.appendChild(el("h4", null, "项目名称"));
      var nameIn = document.createElement("input");
      nameIn.type = "text"; nameIn.value = detail.title || "";
      nameIn.style.width = "100%"; nameIn.style.padding = "6px";
      sec.appendChild(nameIn);
      var saveName = el("button", "primary-btn small", "保存名称");
      saveName.addEventListener("click", function () {
        api.updateProject(pid, { name: nameIn.value }).then(function () {
          showToast("名称已保存");
          openTask(pid);
        });
      });
      sec.appendChild(saveName);
      host.appendChild(sec);

      var sec2 = el("div", "dr-sec");
      sec2.appendChild(el("h4", null, "项目说明"));
      var instr = document.createElement("textarea");
      instr.style.width = "100%"; instr.style.minHeight = "80px";
      instr.value = detail.instructions || "";
      sec2.appendChild(instr);
      var saveInstr = el("button", "primary-btn small", "保存说明");
      saveInstr.addEventListener("click", function () {
        api.updateProject(pid, { instructions: instr.value }).then(function () { showToast("项目说明已保存"); });
      });
      sec2.appendChild(saveInstr);
      host.appendChild(sec2);

      var sec3 = el("div", "dr-sec");
      sec3.appendChild(el("h4", null, "记忆范围"));
      [["project_only", "仅此任务", "此刻只使用本任务的对话/资料/记忆，不读取其他任务"],
       ["global", "使用全局记忆", "在此基础上还读取全局长期记忆"]].forEach(function (opt) {
        var val = opt[0], label = opt[1], desc = opt[2];
        var row = el("div", "mode-row" + ((detail.memory_scope || "project_only") === val ? " active" : ""));
        row.appendChild(el("div", "mode-name", label));
        row.appendChild(el("div", "mode-desc", desc));
        row.addEventListener("click", function () {
          sec3.querySelectorAll(".mode-row").forEach(function (x) { x.classList.remove("active"); });
          row.classList.add("active");
          api.updateProject(pid, { memory_scope: val }).then(function () { showToast("记忆范围已更新"); });
        });
        sec3.appendChild(row);
      });
      host.appendChild(sec3);

      var secModel = el("div", "dr-sec");
      secModel.appendChild(el("h4", null, "模型"));
      var cfg = state.config || {};
      var modelOptions = [["", "跟随默认", "使用默认模型服务（远程网关：" + (cfg.model || "默认") + "）"]];
      if (cfg.local_model) {
        modelOptions.push(["local", "本地模型", cfg.local_model + "（" + (cfg.local_model_url || "本地服务") + "）"]);
      }
      modelOptions.forEach(function (opt) {
        var val = opt[0], label = opt[1], desc = opt[2];
        var active = (detail.model_pref || "") === val;
        var row = el("div", "mode-row" + (active ? " active" : ""));
        row.appendChild(el("div", "mode-name", label));
        row.appendChild(el("div", "mode-desc", desc));
        row.addEventListener("click", function () {
          secModel.querySelectorAll(".mode-row").forEach(function (x) { x.classList.remove("active"); });
          row.classList.add("active");
          api.updateProject(pid, { model_pref: val }).then(function () { showToast("模型选择已更新"); });
        });
        secModel.appendChild(row);
      });
      if (!cfg.local_model) {
        secModel.appendChild(el("div", "dr-empty", "未配置本地模型（.env 设置 FORGE_LOCAL_MODEL_NAME / FORGE_LOCAL_MODEL_BASE_URL 后重启启用）。"));
      }
      host.appendChild(secModel);

      var sec4 = el("div", "dr-sec");
      sec4.appendChild(el("h4", null, "工作位置（可选）"));
      if (detail.work_location && detail.work_location.local_path) {
        sec4.appendChild(el("div", "dr-sub", "此刻可实际操作文件的本地目录：" + detail.work_location.local_path));
        var detach = el("button", "ghost-btn small", "解绑工作位置");
        detach.addEventListener("click", function () {
          api.updateProject(pid, { detach_work_location: true }).then(function () { showToast("已解绑"); openProjectSettingsPanel(); });
        });
        sec4.appendChild(detach);
      } else {
        sec4.appendChild(el("div", "dr-empty", "项目可以没有工作位置（纯资料/对话型项目）。"));
        var pathIn = document.createElement("input");
        pathIn.type = "text"; pathIn.placeholder = "例如 D:/Work/my_creative_agent";
        pathIn.style.width = "100%"; pathIn.style.padding = "6px";
        sec4.appendChild(pathIn);
        var bind = el("button", "primary-btn small", "绑定工作位置");
        bind.addEventListener("click", function () {
          var p = (pathIn.value || "").trim();
          if (!p) { showToast("请输入本地路径"); return; }
          api.listWorkLocations().then(function (list) {
            var existing = list.find(function (w) { return w.local_path === p; });
            var pr = existing ? Promise.resolve(existing)
              : api.createWorkLocation(p.split(/[\\/]/).pop() || "工作位置", p);
            return pr;
          }).then(function (wl) {
            return api.updateProject(pid, { work_location_id: wl.id });
          }).then(function () { showToast("工作位置已绑定"); openProjectSettingsPanel(); })
            .catch(function (e) { showToast("绑定失败：" + (e && e.message ? e.message : "")); });
        });
        sec4.appendChild(bind);
      }
      host.appendChild(sec4);

      var sec5 = el("div", "dr-sec");
      var resetP = el("button", "ghost-btn small", "清空对话");
      resetP.addEventListener("click", function () {
        if (!confirm("清除当前任务的对话记录？不会删除资料与历史执行记录。确定继续吗？")) return;
        api.resetProject(pid).then(function (r) {
          showToast("对话记录已清空");
          state.active = null;
          refreshRail();
          renderTaskList();
        }).catch(function (e) { showToast("清空失败：" + (e && e.message ? e.message : "")); });
      });
      sec5.appendChild(resetP);
      var delP = el("button", "danger-btn", "删除项目");
      delP.addEventListener("click", function () {
        if (!confirm("删除该项目及其全部对话、来源与产物？此操作不可恢复。")) return;
        api.deleteProject(pid).then(function () {
          showToast("项目已删除");
          state.active = null;
          refreshRail().then(renderTaskList);
        });
      });
      sec5.appendChild(delP);
      host.appendChild(sec5);
    });
  }
  var projectScopeFlag = scopeLabel;

  RT.workspace = {
    init: init,
    openExisting: openTask,
    activeId: function () { return state.active ? state.active.id : null; },
    resumeRun: resumeRun,
    drawerOpen: openDrawer,
    send: sendNow,
    showToast: showToast,
    showProjects: function () { goHome("all"); },
    showMaterials: function () { loadMaterialsThen(renderMaterials)(); },
    showSettings: function () { renderSettings(); },
    goHome: goHome,
  };
  RT.initWorkspace = init;
})(window);
