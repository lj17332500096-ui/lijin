/* components.js — Runtime UI 组件（纯渲染，读 snapshot 写 DOM）。
 * 安全约束：所有不信任内容一律 textContent；组件只依赖 types/taskStore 形状。 */
(function (global) {
  "use strict";
  var RT = global.RT = global.RT || {};

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function stateClass(state) {
    if (state === "running") return "running";
    if (state === "waiting_approval" || state === "waiting_user" || state === "waiting_tool" || state === "waiting_auth") return "warn";
    if (state === "failed" || state === "cancelled") return "bad";
    return "";
  }

  function stateLabel(state) {
    return String(state || "—").replace(/_/g, " ").toUpperCase();
  }

  RT.components = {
    stateClass: stateClass,
    stateLabel: stateLabel,
    renderTaskHeader(snapshot, targets) {
      var task = snapshot.task;
      targets.title.textContent = task ? task.goal : "—";
      targets.meta.innerHTML = "";
      if (!task) {
        targets.progressTxt.textContent = "暂无运行任务 · 在下方输入开始";
        targets.progressBar.style.width = "0%";
        targets.pause.disabled = true;
        targets.cancel.disabled = true;
        return;
      }
      var state = snapshot.state || task.state;
      targets.meta.appendChild(el("span", "pill " + (stateClass(state) || "blue"), stateLabel(state)));
      targets.meta.appendChild(el("span", "pill blue", task.id));
      targets.meta.appendChild(el("span", "pill", "agnes-2.5-flash"));
      var usage = task.usage || {};
      targets.progressTxt.textContent =
        "turns " + (usage.turns || 0) + " · tool calls " + (usage.tool_calls || 0) +
        " · tokens " + ((usage.input_tokens || 0) + (usage.output_tokens || 0));
      targets.progressBar.style.width = (state === "completed" || state === "failed") ? "100%" : "62%";
      targets.progressBar.style.background = state === "failed" ? "var(--danger)" : "";
      targets.pause.disabled = !(state === "running" || state === "paused" || state === "submitted");
      targets.cancel.disabled = !(state === "running" || state === "paused" || state === "submitted");
      var paused = state === "paused";
      targets.pause.innerHTML = RT.icon(paused ? "play" : "pause", 14) + "<span>　" + (paused ? "恢复" : "暂停") + "</span>";
    },

    runtimeStack(snapshot) {
      var calls = snapshot.toolCalls || [];
      if (!calls.length) return null;
      var wrap = el("div", "rt-stack");
      wrap.appendChild(el("div", "rt-head", "Runtime 正在执行 · 实时事件"));
      calls.forEach(function (c) {
        var row = el("div", "rt-item");
        row.appendChild(el("span", "rt-ico", String(c.tool || "ƒ").slice(0, 2).toUpperCase()));
        var name = el("div", "rt-name", c.tool || "?");
        if (c.detail) name.appendChild(el("small", "", String(c.detail).slice(0, 200)));
        row.appendChild(name);
        var st = el("span", "rt-status " + c.status);
        if (c.status === "running") {
          st.appendChild(el("span", "rt-run running"));
          st.appendChild(document.createTextNode("执行中"));
        } else if (c.status === "succeeded") {
          st.appendChild(document.createTextNode("✓ 完成"));
        } else if (c.status === "failed") {
          st.appendChild(document.createTextNode("✕ 失败"));
        } else {
          st.textContent = String(c.status || "");
        }
        row.appendChild(st);
        wrap.appendChild(row);
      });
      return wrap;
    },

    message(role, text) {
      var wrap = el("div", "msg-block");
      var head = el("div", "msg-role");
      head.appendChild(el("b", "", role === "user" ? "你" : "全能助手"));
      wrap.appendChild(head);
      wrap.appendChild(el("div", "msg-body" + (role === "user" ? " user" : ""), text || ""));
      return wrap;
    },

    approvalCard(snapshot, handlers) {
      var approvals = snapshot.approvals || [];
      if (!approvals.length) return null;
      var wrap = el("div", "approval-card");
      approvals.forEach(function (a) {
        var top = el("div", "approval-top");
        top.appendChild(el("span", "risk-tag", "需要审批"));
        top.appendChild(el("span", "approval-title", "高风险操作 · " + a.tool));
        wrap.appendChild(top);
        var argsText = "";
        try { argsText = JSON.stringify(a.arguments || {}).slice(0, 300); } catch (e) { argsText = ""; }
        wrap.appendChild(el("div", "approval-desc", "参数：" + (argsText || "—")));
        var scope = el("div", "scope-box");
        [["approval", a.id], ["tool", a.tool], ["risk", "medium · side_effect"]].forEach(function (pair) {
          var line = el("div");
          var label = document.createElement("i");
          label.textContent = pair[0];
          line.appendChild(label);
          line.appendChild(el("span", "", String(pair[1] || "")));
          scope.appendChild(line);
        });
        wrap.appendChild(scope);
        var actions = el("div", "approval-actions");
        var deny = el("button", "ghost-btn", "拒绝");
        deny.onclick = function () { if (handlers && handlers.decide) handlers.decide(a.id, "denied"); };
        var allow = el("button", "primary-btn", "批准一次");
        allow.onclick = function () { if (handlers && handlers.decide) handlers.decide(a.id, "approved"); };
        actions.appendChild(deny);
        actions.appendChild(allow);
        wrap.appendChild(actions);
      });
      return wrap;
    },

    artifacts(snapshot) {
      var artifacts = (snapshot.task && snapshot.task.artifacts) ? snapshot.task.artifacts : [];
      if (!artifacts.length) return null;
      var wrap = el("div", "artifact-list");
      artifacts.forEach(function (art) {
        var card = el("div", "artifact-card");
        card.appendChild(el("span", "art-ico", String(art.kind || "F").slice(0, 3).toUpperCase()));
        var main = el("div", "art-main");
        var nameLine = el("b");
        nameLine.appendChild(el("span", "", art.name));
        nameLine.appendChild(el("span", "verified", "✓ 已验证"));
        main.appendChild(nameLine);
        main.appendChild(el("small", "", art.id + " · " + (art.size_bytes || 0) + " B · sha256 已记录"));
        card.appendChild(main);
        var actions = el("div", "art-actions");
        var dl = el("button", "primary-btn", "下载");
        dl.onclick = function () { window.open(RT.api.artifactDownloadUrl(art.id), "_blank"); };
        var ref = el("button", "ghost-btn", "复制引用");
        ref.onclick = function () {
          var text = art.id;
          if (navigator.clipboard) navigator.clipboard.writeText(text).catch(function () {});
          else prompt("复制 artifact 引用", text);
        };
        actions.appendChild(ref);
        actions.appendChild(dl);
        card.appendChild(actions);
        wrap.appendChild(card);
      });
      return wrap;
    },

    error(text) {
      return el("div", "error-card", text || "任务失败");
    },

    renderInspector(snapshot, targets) {
      var task = snapshot.task;
      var usage = task ? task.usage : null;
      targets.kvState.textContent = snapshot.state || (task ? task.state : "—");
      targets.kvTask.textContent = task ? task.id : "—";
      targets.budgetTurns.textContent = usage ? usage.turns + " / —" : "0 / —";
      targets.budgetTools.textContent = usage ? usage.tool_calls + " / —" : "0 / —";
      targets.budgetTokens.textContent = usage ? String((usage.input_tokens || 0) + (usage.output_tokens || 0)) : "0";
      targets.budgetCost.textContent = usage ? ("$" + (usage.cost_usd || 0).toFixed(3)) : "—";
      targets.checkpointBox.innerHTML = "";
      if (!task) {
        targets.checkpointBox.appendChild(el("b", "", "—"));
        targets.checkpointBox.appendChild(el("p", "", "任务结束后由 Runtime 落盘快照。"));
        return;
      }
      var cp = task.checkpoint;
      if (cp) {
        targets.checkpointBox.appendChild(el("b", "", "checkpoint · " + (cp.created_at || "")));
        targets.checkpointBox.appendChild(el("p", "", String(cp.summary || "—").slice(0, 300)));
      } else {
        targets.checkpointBox.appendChild(el("b", "", "—"));
        targets.checkpointBox.appendChild(el("p", "", "尚无 checkpoint（执行中每轮结束时写入）。"));
      }
    },
  };

  /* =====================================================================
     Icon System — single SVG set (Lucide-style), stroke-width 2, 24 grid.
     Usage: RT.icon(name, size) -> svg string ; RT.iconEl(name, cls) -> <span class="icon">.
     System icons must NOT mix Emoji / unicode glyphs.
     ===================================================================== */
  var ICONS = {
    clock:       '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    bell:        '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/>',
    plus:        '<path d="M5 12h14"/><path d="M12 5v14"/>',
    folder:      '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    sliders:     '<line x1="21" y1="4" x2="14" y2="4"/><line x1="10" y1="4" x2="3" y2="4"/><line x1="21" y1="12" x2="12" y2="12"/><line x1="8" y1="12" x2="3" y2="12"/><line x1="21" y1="20" x2="16" y2="20"/><line x1="12" y1="20" x2="3" y2="20"/><line x1="14" y1="2" x2="14" y2="6"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="16" y1="18" x2="16" y2="22"/>',
    chevdown:    '<path d="m6 9 6 6 6-6"/>',
    arrowright:  '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    star:        '<path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>',
    x:           '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    pause:       '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    play:        '<polygon points="5 3 19 12 5 21 5 3"/>',
    square:      '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    search:      '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    chart:       '<line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/>',
    check:       '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/>',
    list:        '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/>',
    file:        '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
    trash:       '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    archive:     '<rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
  };

  function icon(name, size) {
    size = size || 18;
    var p = ICONS[name] || ICONS.list;
    return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true" focusable="false">' + p + '</svg>';
  }

  function iconEl(name, cls) {
    var s = document.createElement("span");
    s.className = "icon" + (cls ? " " + cls : "");
    s.innerHTML = icon(name);
    return s;
  }

  RT.icons = ICONS;
  RT.icon = icon;
  RT.iconEl = iconEl;
})(window);
