from pathlib import Path
p=Path('web/runtime/workspace.js')
s=p.read_text(encoding='utf-8')
s=s.replace('var nav = el("div", "settings-nav");','var nav = el("nav", "settings-nav");\n    nav.setAttribute("aria-label", "设置分类");')
s=s.replace('var b = el("button", "chip" + (settingsSec === sec[0] ? " active" : ""), sec[1]);','var b = el("button", "settings-tab" + (settingsSec === sec[0] ? " active" : ""), sec[1]);\n      b.setAttribute("aria-current", settingsSec === sec[0] ? "page" : "false");')
s=s.replace('wrap.appendChild(content);\n    stream.appendChild(wrap);','var layout = el("div", "settings-layout");\n    layout.appendChild(nav); layout.appendChild(content);\n    wrap.appendChild(layout);\n    stream.appendChild(wrap);',1)
s=s.replace('function secTitle(t) { return el("div", "tl-head settings-head", t); }','''function secTitle(t) { return el("h2", "tl-head settings-head", t); }
  function settingsIntro(box, title, description) {
    box.appendChild(secTitle(title));
    box.appendChild(el("p", "settings-description", description));
  }
  function settingsChoice(box, name, description, selected, onSelect) {
    var row = el("button", "mode-row settings-choice" + (selected ? " active" : ""));
    row.type = "button"; row.setAttribute("aria-pressed", String(selected));
    var copy = el("span", "settings-choice-copy");
    copy.appendChild(el("span", "mode-name", name));
    copy.appendChild(el("span", "mode-desc", description));
    row.appendChild(copy);
    var mark = el("span", "settings-radio"); mark.setAttribute("aria-hidden", "true"); row.appendChild(mark);
    row.addEventListener("click", function () {
      onSelect();
      box.querySelectorAll(".settings-choice").forEach(function (r) {
        var active = r === row; r.classList.toggle("active", active); r.setAttribute("aria-pressed", String(active));
      });
    });
    box.appendChild(row); return row;
  }
  function settingsSwitch(box, name, description, initial, save) {
    var row = el("div", "settings-toggle-row"), copy = el("div", "settings-choice-copy");
    copy.appendChild(el("div", "mode-name", name)); copy.appendChild(el("p", "mode-desc", description));
    row.appendChild(copy);
    var toggle = el("button", "settings-switch"); toggle.type = "button";
    toggle.setAttribute("role", "switch"); toggle.setAttribute("aria-label", name);
    toggle.setAttribute("aria-checked", String(initial));
    toggle.addEventListener("click", async function () {
      var next = toggle.getAttribute("aria-checked") !== "true";
      toggle.disabled = true;
      try { await save(next); toggle.setAttribute("aria-checked", String(next)); showToast(name + (next ? "已开启" : "已关闭")); }
      catch (e) { showToast("未能保存，请重试"); }
      finally { toggle.disabled = false; }
    });
    row.appendChild(toggle); box.appendChild(row);
  }''')
a=s.index('  function renderGeneralPane(host)'); b=s.index('  function renderMemoryPane(host)',a)
s=s[:a]+'''  function renderGeneralPane(host) {
    var box = el("div", "tl-sec");
    settingsIntro(box, "执行与确认", "选择你习惯的协作方式，重要操作保持清晰可控。");
    var selMode = localStorage.getItem("forge.mode") || "推荐";
    [["推荐", "修改前确认", "可以读取和分析内容，重要修改前会询问你。"],
     ["安全", "只读检查", "只查看，不修改，适合初次检查文件和项目。"],
     ["自动", "自动执行", "普通操作自动执行，重要操作仍会询问你。"]].forEach(function (g) {
      settingsChoice(box, g[1] + (g[0] === "推荐" ? " · 推荐" : ""), g[2], selMode === g[0], function () {
        localStorage.setItem("forge.mode", g[0]); showToast("使用方式已设为「" + g[0] + "」");
      });
    });
    host.appendChild(box);
    var account = el("div", "tl-sec");
    settingsIntro(account, "使用环境", "当前设备上的账户与使用信息。");
    var row = el("div", "tl-row"); row.appendChild(el("span", "tl-title", "账户"));
    row.appendChild(el("span", "tl-note", "本地使用 · 无需登录")); account.appendChild(row); host.appendChild(account);
  }
  function renderAppearancePane(host) {
    var box = el("div", "tl-sec");
    settingsIntro(box, "主题外观", "选择舒适的阅读背景，所有页面使用同一套品牌风格。");
    THEMES.forEach(function (t) {
      settingsChoice(box, t.label, t.desc, themeSetting() === t.key, function () {
        localStorage.setItem("forge.theme.main", t.key); applyTheme(); showToast("外观已切换：" + t.label);
      });
    });
    host.appendChild(box);
  }
  function renderNotifyPane(host) {
    var box = el("div", "tl-sec");
    settingsIntro(box, "消息提醒", "仅在需要你关注时提醒，减少不必要的打扰。");
    settingsSwitch(box, "重要事项通知", "等待确认等重要事项将在本机界面提醒你。", localStorage.getItem("forge.notify") !== "off", function (next) {
      localStorage.setItem("forge.notify", next ? "on" : "off");
    });
    host.appendChild(box);
  }
''' + s[b:]
a=s.index('    box.appendChild(secTitle("记忆"));',s.index('function renderMemoryPane')); b=s.index('    host.appendChild(box);',a)
s=s[:a]+'''    settingsIntro(box, "记忆偏好", "让此刻记住你的习惯，减少重复说明。");
    settingsSwitch(box, "长期记忆", "开启后保留长期偏好；具体任务的记忆范围可在项目设置中选择。", localStorage.getItem("forge.memoryEnabled") !== "off", async function (next) {
      await api.postSettingsMemory(next);
      localStorage.setItem("forge.memoryEnabled", next ? "on" : "off");
    });
'''+s[b:]
s=s.replace('box.appendChild(secTitle("数据"));','settingsIntro(box, "本地数据", "查看当前设备的数据存储与备份情况。");')
s=s.replace('box.appendChild(secTitle("高级"));','settingsIntro(box, "运行配置", "以下为当前生效的配置，仅供查看。");')
s=s.replace('var r = el("div", "tl-row clickable");\n      r.appendChild(el("span", "tl-title", it[0]));','var r = el("button", "tl-row clickable settings-link");\n      r.type = "button";\n      r.appendChild(el("span", "tl-title", it[0]));',1)
s=s.replace('String(it[1]).slice(0, 120)','String(it[1])').replace('String(it[1]).slice(0, 160)','String(it[1])')
p.write_text(s,encoding='utf-8')
p=Path('web/runtime/design.css'); s=p.read_text(encoding='utf-8')
s=s.replace('max-width:840px; margin:0 auto; padding:38px 22px;\n}\n.settings-topbar','width:100%; max-width:1020px; margin:0 auto; padding:32px 28px;\n}\n.settings-topbar',1)
s=s.replace('display:flex; align-items:center; gap:12px; padding:0 0 8px;','display:flex; align-items:center; gap:12px; padding:0 0 24px;',1)
s=s.replace('display:flex; gap:8px; flex-wrap:wrap; border-bottom:1px solid var(--border-subtle); padding-bottom:10px; margin-bottom:14px;','display:flex; flex-direction:column; gap:6px; padding:6px 18px 0 0; border-right:1px solid var(--border-subtle); align-self:start;',1)
pos=s.index('.surface-back{')
s=s[:pos]+'''.settings-layout{display:grid; grid-template-columns:148px minmax(0,1fr); gap:28px}
.settings-content{min-width:0}
.settings-tab{text-align:left; padding:11px 14px; min-height:44px; border-radius:var(--radius-md); color:var(--text-secondary); font-size:14px; border:1px solid transparent}
.settings-tab:hover{background:var(--surface-hover)}
.settings-tab.active{background:var(--accent-soft); color:var(--accent-hover); border-color:var(--border-strong); font-weight:var(--fw-semibold)}
.settings-tab:focus-visible,.settings-choice:focus-visible,.settings-switch:focus-visible,.settings-link:focus-visible{outline:2px solid var(--accent); outline-offset:3px}
.settings-content .tl-sec{padding:22px; margin-bottom:18px}
.settings-content .settings-head{font-size:17px; color:var(--text-primary); border:0; padding:0; margin:0 0 6px}
.settings-description{font-size:13px; line-height:1.7; color:var(--text-muted); margin:0 0 20px}
.settings-content .settings-choice{display:flex; width:100%; align-items:center; gap:16px; text-align:left; padding:16px; min-height:80px; background:var(--surface-2); margin-top:10px}
.settings-content .settings-choice.active{background:var(--accent-soft); border-color:var(--accent)}
.settings-choice-copy{display:block; flex:1; min-width:0}
.settings-choice-copy .mode-name{display:block; font-size:14px; line-height:1.5}
.settings-choice-copy .mode-desc{display:block; font-size:13px; line-height:1.7; margin-top:5px}
.settings-radio{width:18px; height:18px; border:1px solid var(--border-strong); border-radius:var(--radius-full); flex:none}
.active .settings-radio{border:5px solid var(--accent); background:var(--color-on-accent)}
.settings-toggle-row{display:flex; gap:24px; align-items:center; padding:4px 0 8px}
.settings-switch{position:relative; flex:none; width:44px; height:26px; border:1px solid var(--border-strong); border-radius:var(--radius-full); background:var(--surface-3)}
.settings-switch::after{content:""; position:absolute; width:18px; height:18px; top:3px; left:3px; border-radius:var(--radius-full); background:var(--text-muted); transition:transform var(--motion-normal) var(--ease-standard),background-color var(--motion-normal) var(--ease-standard)}
.settings-switch[aria-checked="true"]{background:var(--accent); border-color:var(--accent)}
.settings-switch[aria-checked="true"]::after{transform:translateX(18px); background:var(--color-on-accent)}
.settings-content .tl-row{padding:14px 0; border-radius:0; border-bottom:1px solid var(--border-subtle); cursor:default; gap:20px; align-items:flex-start}
.settings-content .tl-row:last-child{border-bottom:0}
.settings-content .tl-row:not(.clickable):hover{background:transparent}
.settings-content .tl-title{flex:0 0 96px; white-space:normal; font-size:13px}
.settings-content .tl-note{font-size:13px; overflow-wrap:anywhere; line-height:1.7; min-width:0}
.settings-content .settings-link{width:100%; text-align:left; cursor:pointer}
.settings-link::after{content:"›"; margin-left:auto; color:var(--text-muted)}
@media(max-width:760px){
  .settings-layout{grid-template-columns:minmax(0,1fr); gap:18px}
  .settings-nav{flex-direction:row; padding:0 0 8px; border-right:0; border-bottom:1px solid var(--border-subtle); overflow-x:auto; gap:4px}
  .settings-tab{white-space:nowrap; padding:10px 12px; flex:none}
  .settings-topbar{padding-bottom:18px}
  .settings-topbar .brand-lockup{font-size:12px}
  .settings-content .tl-sec{padding:18px 16px}
  .settings-content .settings-choice{padding:14px 12px}
  .settings-content .tl-row{gap:12px; flex-wrap:wrap}
  .settings-content .tl-title{flex-basis:80px}
  .settings-content .tl-note{flex:1}
}
''' + s[pos:]
s=s.replace('.settings-content .settings-head{\n  margin-top:0;\n}\n','')
p.write_text(s,encoding='utf-8')
p=Path('web/runtime.html');p.write_text(p.read_text(encoding='utf-8').replace('v=224','v=225'),encoding='utf-8')
