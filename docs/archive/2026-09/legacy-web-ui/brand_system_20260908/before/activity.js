/* activity.js — Presentation Projector（v13：用户语言包 + 阶段台词）。
 * 把 Run 内部的工具/过程事实，投影成普通用户能读的 Activity（阶段+一句话）。
 * 默认不出现：Run / Tool Call / Model Call / Event / Observer……这些只在「高级信息」。
 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};

  /* 工具名 → 阶段 + 用户台词 */
  var TOOL_ACT = {
    web_search: ["searching", "搜索", "正在搜索相关信息"],
    deep_research: ["searching", "深度调研", "正在做多角度调研"],
    search_documents: ["searching", "搜索", "正在查找相关内容"],
    index_workspace: ["reading", "建立索引", "正在整理文件索引"],
    read_workspace_file: ["reading", "阅读", "正在阅读项目文件"],
    read_code_file: ["reading", "阅读", "正在阅读代码"],
    list_workspace_files: ["exploring", "检查", "正在查看项目结构"],
    list_code_files: ["exploring", "检查", "正在查看沙箱文件"],
    read_note: ["reading", "阅读", "正在翻看备忘"],
    read_office_file: ["reading", "阅读", "正在阅读文档"],
    read_spreadsheet: ["reading", "阅读", "正在阅读表格"],
    ask_image: ["reading", "查看", "正在查看图片"],
    fetch_github_repo: ["exploring", "检查", "正在抓取仓库"],
    calculate: ["reading", "计算", "正在计算"],
    write_code_file: ["editing", "修改", "正在写入代码"],
    edit_project_file: ["editing", "修改", "正在修改文件"],
    write_project_file: ["editing", "修改", "正在写入文件"],
    run_python: ["verifying", "验证", "正在运行验证"],
    code_loop: ["verifying", "验证", "正在验证并修复"],
    sandbox_snapshot: ["verifying", "验证", "正在保存检查点"],
    sandbox_rollback: ["verifying", "验证", "正在恢复现场"],
    list_sandbox_snapshots: ["exploring", "检查", "正在查看检查点"],
    save_note: ["generating", "生成", "正在生成文档"],
    save_word_doc: ["generating", "生成", "正在生成 Word"],
    save_excel_workbook: ["generating", "生成", "正在生成 Excel"],
    save_ppt_deck: ["generating", "生成", "正在生成 PPT"],
    remember: ["exploring", "记录", "正在记住你的偏好"],
    recall_memory: ["exploring", "回忆", "正在回忆相关背景"],
    forget_memory: ["editing", "修改", "正在删除记忆"],
    schedule_add: ["generating", "安排", "正在设置定时任务"],
    schedule_list: ["exploring", "检查", "正在查看定时任务"],
    schedule_remove: ["editing", "修改", "正在移除定时任务"],
    schedule_set_enabled: ["editing", "修改", "正在启停定时任务"],
    get_current_datetime: ["exploring", "检查", "正在确认当前时间"],
  };
  var DEFAULT_ACT = ["exploring", "处理", "正在处理"];
  var PHASE_META = {
    searching: { icon: "◉", title: "检查" },
    exploring: { icon: "◫", title: "检查" },
    reading: { icon: "◇", title: "阅读" },
    editing: { icon: "✎", title: "修改" },
    running: { icon: "▶", title: "运行" },
    generating: { icon: "＋", title: "生成" },
    verifying: { icon: "✓", title: "验证" },
    completed: { icon: "✓", title: "已完成" },
    failed: { icon: "✕", title: "遇到问题" },
  };

  /* 内部状态 → 普通用户语言 §六 */
  var USER_STATE = {
    running: { label: "正在处理", cls: "running" },
    submitted: { label: "正在处理", cls: "running" },
    waiting_approval: { label: "等你确认", cls: "waiting" },
    paused: { label: "已暂停", cls: "paused" },
    failed: { label: "遇到问题", cls: "err" },
    cancelled: { label: "已停止", cls: "muted" },
    completed: { label: "已完成", cls: "ok" },
    waiting_user: { label: "等你确认", cls: "waiting" },
  };
  function userState(state) {
    var hit = USER_STATE[state || ""];
    return hit ? { label: hit.label, cls: hit.cls } : { label: "进行中", cls: "blue" };
  }
  /* 一句话工作台词（第一阶段底部/头部提示） */
  function phraseFor(state) {
    var label = userState(state).label;
    return { running: "正在处理…", waiting_approval: "需要你确认", paused: "已暂停", failed: "遇到一个问题",
             cancelled: "已停止", completed: "已完成" }[state] || "进行中";
  }

  function phaseFor(toolName) {
    var hit = TOOL_ACT[toolName || ""];
    return hit ? hit[0] : DEFAULT_ACT[0];
  }
  function labelFor(toolName) {
    var hit = TOOL_ACT[toolName || ""];
    return hit ? hit[1] : DEFAULT_ACT[1];
  }
  function beVerb(toolName) {
    var hit = TOOL_ACT[toolName || ""];
    return hit ? hit[2] : DEFAULT_ACT[2];
  }

  /* 未登记工具（含 MCP 工具）按名称动词归类成普通用户语言 */
  function plainLabel(toolName) {
    var n = String(toolName || "").toLowerCase();
    if (/delete|remove|clear|drop|trash|rollback|forget/i.test(n)) return "删除";
    if (/create|add|insert|append|new|fork|write|save|update|edit|modify|patch|upload|put|post|fill|type|click|press|submit|evaluate|run|execute|apply|merge/i.test(n)) return "修改";
    if (/send|mail|message|notify|comment|reply|publish|issue|release/i.test(n)) return "发送";
    if (/schedule|timer|remind/i.test(n)) return "安排";
    if (/query|read|get|list|search|fetch|find|select|describe|compare|open|navigate|view|inspect|scan/i.test(n)) return "查看";
    if (/approve|deny|allow|permit/i.test(n)) return "确认";
    return DEFAULT_ACT[1];
  }

  function describe(toolName, args) {
    args = args || {};
    var hit = TOOL_ACT[toolName || ""];
    var label = hit ? hit[1] : plainLabel(toolName);
    var parts = [];
    ["filename", "path", "file", "name", "directory", "project", "entry_id", "query", "keyword", "sheet_name"]
      .forEach(function (k) {
        var v = args[k];
        if (v === undefined || v === null || v === "") return;
        parts.push(String(v).slice(0, 60));
      });
    if (toolName === "run_python" && args.filename) {
      return "运行 " + args.filename + (args.project ? "（" + args.project + "）" : "");
    }
    if (toolName === "code_loop" && args.filename) {
      return "自动验证修复 " + args.filename;
    }
    if (toolName === "edit_project_file") {
      return ("修改 " + (args.path || "文件"));
    }
    if (args.sheets_json) parts.push("（表格）");
    return parts.length ? label + " " + parts.join(" · ") : label;
  }

  /* 工具条目数组 → 阶段化 Activity 组（连续同类合并） */
  function project(items) {
    var groups = [];
    (items || []).forEach(function (item) {
      var phase = phaseFor(item.tool);
      var last = groups.length ? groups[groups.length - 1] : null;
      if (last && last.phase === phase) {
        last.items.push(item);
      } else {
        groups.push({ phase: phase, meta: PHASE_META[phase] || PHASE_META.exploring, items: [item] });
      }
    });
    var COUNTER = {
      reading: function (n) { return "阅读了 " + n + " 个文件"; },
      exploring: function (n) { return "检查了 " + n + " 个项目位置"; },
      searching: function (n) { return "搜索了 " + n + " 轮"; },
      editing: function (n) { return "修改了 " + n + " 处文件";
      },
      generating: function (n) { return "生成了 " + n + " 个文件"; },
      verifying: function (n) { return "验证了 " + n + " 次运行"; },
    };
    groups.forEach(function (g) {
      var n = g.items.length;
      g.summary = (COUNTER[g.phase] || function () { return g.meta.title + " · " + n + " 项"; })(n);
      g.lines = g.items.map(function (it) { return describe(it.tool, it.args); }).slice(-5);
    });
    return groups;
  }

  /* 从 Run 的工具记录统计「交付卡」素材：
   * changedFiles（editing 阶段 path 集合）、generated（artifacts 名）、verified（verifying 条数） */
  function deliverySummary(run) {
    var changed = [];
    var seen = {};
    (run.tool_calls || []).forEach(function (t) {
      if (phaseFor(t.tool_name) !== "editing") return;
      var args = {};
      try { args = (typeof t.arguments === "string") ? JSON.parse(t.arguments || "{}") : (t.arguments || {}); }
      catch (e) { args = {}; }
      var ref = args.path || args.filename || args.file || t.tool_name;
      if (ref && !seen[ref]) { seen[ref] = true; changed.push(ref); }
    });
    var verified = (run.tool_calls || []).filter(function (t) { return phaseFor(t.tool_name) === "verifying"; }).length;
    var artifacts = (run.artifacts || []).map(function (a) { return a.name; });
    return { changedFiles: changed.slice(0, 12), verifiedCount: verified, artifacts: artifacts.slice(0, 12) };
  }

  RT.activity = {
    phaseFor: phaseFor, labelFor: labelFor, beVerb: beVerb,
    describe: describe, project: project, deliverySummary: deliverySummary,
    userState: userState, phraseFor: phraseFor, PHASE_META: PHASE_META,
  };
})(window);
