/* markdown.js — 无依赖安全 Markdown 渲染器（AST → 白名单 DOM）。
 *
 * 安全模型：
 *   1. 解析层产出纯数据 AST（可序列化、可测试）；
 *   2. 渲染层只用 document.createElement + textContent 建节点，
 *      绝不 innerHTML 注入模型输出 → 天然免疫 XSS（script/iframe/svg/事件属性
 *      都只会变成普通文本或不会进入属性）；
 *   3. 链接 href 白名单（http/https/mailto/#/相对路径），javascript: 一律置 '#'；
 *   4. 图片语法 ![]() 按链接文本降级（不渲染 <img>，防止跟踪/onerror）。
 *
 * 支持：heading / paragraph / **bold** / *italic* / 无序·有序列表 / blockquote /
 * inline code / fenced code block（带语言）/ link / table / hr。
 */
(function (global) {
  "use strict";

  var RT = global.RT = global.RT || {};

  var SAFE_SCHEMES = /^(https?:\/\/|mailto:|#|\/|\.\/|\.\.\/)/i;

  function cleanHref(raw) {
    var u = String(raw == null ? "" : raw).trim();
    if (!u) return "#";
    if (SAFE_SCHEMES.test(u)) return u;
    return "#";
  }

  /* ---------- inline 解析：产出 [{t:'text'|'strong'|'em'|'code'|'link', v, href?}] ---------- */
  function parseInline(text) {
    var out = [];
    var rest = String(text == null ? "" : text);
    var re = /(`[^`\n]+`|\[[^\]]*\]\([^)\n]*\)|\*\*[^*\n]+\*\*|\*[^*\n]+\*|_([^_\n]+)_)/;
    while (rest.length) {
      var m = re.exec(rest);
      if (!m) { out.push({ t: "text", v: rest }); break; }
      if (m.index > 0) out.push({ t: "text", v: rest.slice(0, m.index) });
      var tok = m[0];
      if (tok[0] === "`") {
        out.push({ t: "code", v: tok.slice(1, -1) });
      } else if (tok[0] === "[") {
        var close = tok.indexOf("](");
        var label = tok.slice(1, close);
        var href = tok.slice(close + 2, -1).trim();
        var space = href.indexOf(" ");
        if (space > 0) href = href.slice(0, space); // [t](url "title")
        out.push({ t: "link", v: label, href: cleanHref(href) });
      } else if (tok[0] === "*" && tok[1] === "*") {
        out.push({ t: "strong", v: tok.slice(2, -2) });
      } else if (tok[0] === "*") {
        out.push({ t: "em", v: tok.slice(1, -1) });
      } else if (tok[0] === "_") {
        out.push({ t: "em", v: tok.slice(1, -1) });
      }
      rest = rest.slice(m.index + tok.length);
    }
    return out;
  }

  /* ---------- block 解析 ---------- */
  function isTableRow(line) {
    return /^\s*\|.*\|\s*$/.test(line) && line.indexOf("|") !== line.lastIndexOf("|");
  }
  function splitTableRow(line) {
    return line.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); });
  }
  function isDelimRow(line) {
    return /^\s*\|?[\s:|-]+\|[\s:|-]+\|?\s*$/.test(line) && line.indexOf("-") >= 0;
  }

  function parseMarkdown(src) {
    var lines = String(src == null ? "" : src).replace(/\r\n/g, "\n").split("\n");
    var ast = [];
    var i = 0;
    var para = [];
    function flushPara() {
      if (para.length) {
        ast.push({ t: "p", inlines: parseInline(para.join("\n")) });
        para = [];
      }
    }
    while (i < lines.length) {
      var line = lines[i];
      var trimmed = line.trim();
      // fenced code
      var fence = /^```(\w*)/.exec(trimmed);
      if (fence) {
        flushPara();
        var lang = fence[1] || "";
        var codeLines = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i].trim())) {
          codeLines.push(lines[i]);
          i++;
        }
        i++; // skip closing ```
        ast.push({ t: "code", lang: lang, code: codeLines.join("\n") });
        continue;
      }
      // table（至少两行且第二行是分隔行）
      if (isTableRow(trimmed) && i + 1 < lines.length && isDelimRow(lines[i + 1])) {
        flushPara();
        var headers = splitTableRow(trimmed);
        var rows = [];
        i += 2;
        while (i < lines.length && isTableRow(lines[i].trim())) {
          rows.push(splitTableRow(lines[i]).map(function (c) { return parseInline(c); }));
          i++;
        }
        ast.push({ t: "table", headers: headers.map(parseInline), rows: rows });
        continue;
      }
      // heading
      var h = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (h) {
        flushPara();
        ast.push({ t: "h" + h[1].length, inlines: parseInline(h[2]) });
        i++;
        continue;
      }
      // hr
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
        flushPara();
        ast.push({ t: "hr" });
        i++;
        continue;
      }
      // blockquote
      if (/^>\s?/.test(trimmed)) {
        flushPara();
        var qlines = [];
        while (i < lines.length && /^>\s?/.test(lines[i].trim())) {
          qlines.push(lines[i].trim().replace(/^>\s?/, ""));
          i++;
        }
        ast.push({ t: "quote", inlines: parseInline(qlines.join("\n")) });
        continue;
      }
      // list（- * 或 数字.）
      var ul = /^[-*]\s+(.*)$/.exec(trimmed);
      var ol = /^(\d+)[.)]\s+(.*)$/.exec(trimmed);
      if (ul || ol) {
        flushPara();
        var items = [];
        var isOl = !!ol;
        while (i < lines.length) {
          var li = isOl
            ? /^(\d+)[.)]\s+(.*)$/.exec(lines[i].trim())
            : /^[-*]\s+(.*)$/.exec(lines[i].trim());
          if (!li) break;
          // 无序列表正则只有 1 个捕获组（内容），有序列表有 2 个（序号+内容）。
          // 取错下标会让 parseInline(undefined) → []，条目只剩空 <li>（项目符号）。
          items.push(parseInline(isOl ? li[2] : li[1]));
          i++;
        }
        ast.push({ t: isOl ? "ol" : "ul", items: items });
        continue;
      }
      // 空行 → 段落分隔
      if (!trimmed) { flushPara(); i++; continue; }
      para.push(trimmed);
      i++;
    }
    flushPara();
    return ast;
  }

  /* ---------- 渲染：AST → DOM（白名单节点，纯 createElement/textContent） ---------- */
  function renderAst(ast, doc) {
    var frag = (doc || document).createDocumentFragment();
    ast.forEach(function (node) {
      var el = null;
      if (node.t === "p") {
        el = doc.createElement("p");
        appendInlines(el, node.inlines, doc);
      } else if (/^h[1-6]$/.test(node.t)) {
        el = doc.createElement(node.t);
        appendInlines(el, node.inlines, doc);
      } else if (node.t === "ul" || node.t === "ol") {
        el = doc.createElement(node.t);
        node.items.forEach(function (item) {
          var li = doc.createElement("li");
          appendInlines(li, item, doc);
          // 模型常把执行过程写成 “* 正在… / 接下来… / 我现在…”：
          // 这些行自动弱化为过程辅助色（真实状态以 Runtime Activity 为准）
          var first = item.length ? item[0] : null;
          if (first && first.t === "text" && /^(正在|接下来|我现在|首先|然后|接着|下一步)/.test(first.v)) {
            li.className = "agent-thought-line";
          }
          el.appendChild(li);
        });
      } else if (node.t === "quote") {
        el = doc.createElement("blockquote");
        appendInlines(el, node.inlines, doc);
      } else if (node.t === "code") {
        el = buildCodeBlock(node, doc);
      } else if (node.t === "table") {
        el = buildTable(node, doc);
      } else if (node.t === "hr") {
        el = doc.createElement("hr");
      }
      if (el) frag.appendChild(el);
    });
    return frag;
  }

  function appendInlines(parent, inlines, doc) {
    (inlines || []).forEach(function (n) {
      var el;
      if (n.t === "text") {
        parent.appendChild(doc.createTextNode(n.v));
        return;
      } else if (n.t === "strong") {
        el = doc.createElement("strong");
        el.textContent = n.v;
      } else if (n.t === "em") {
        el = doc.createElement("em");
        el.textContent = n.v;
      } else if (n.t === "code") {
        el = doc.createElement("code");
        el.className = "inline-code";
        el.textContent = n.v;
      } else if (n.t === "link") {
        el = doc.createElement("a");
        el.textContent = n.v;
        el.setAttribute("href", n.href || "#");
        if (n.href && /^https?:/i.test(n.href)) {
          el.setAttribute("target", "_blank");
          el.setAttribute("rel", "noopener noreferrer");
        }
      } else {
        parent.appendChild(doc.createTextNode(String(n.v || "")));
        return;
      }
      parent.appendChild(el);
    });
  }

  function buildCodeBlock(node, doc) {
    var wrap = doc.createElement("div");
    wrap.className = "code-block";
    var head = doc.createElement("div");
    head.className = "code-head";
    var lang = doc.createElement("span");
    lang.className = "code-lang";
    lang.textContent = node.lang || "code";
    head.appendChild(lang);
    var copy = doc.createElement("button");
    copy.className = "code-copy";
    copy.textContent = "复制";
    copy.addEventListener("click", function () {
      var done = function () { copy.textContent = "已复制"; setTimeout(function () { copy.textContent = "复制"; }, 1500); };
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(node.code).then(done, function () { fallbackCopy(); });
        } else {
          fallbackCopy();
        }
      } catch (e) { fallbackCopy(); }
      function fallbackCopy() {
        var ta = doc.createElement("textarea");
        ta.value = node.code;
        doc.body.appendChild(ta);
        ta.select();
        try { doc.execCommand("copy"); done(); } catch (e) {}
        ta.remove();
      }
    });
    head.appendChild(copy);
    wrap.appendChild(head);
    var pre = doc.createElement("pre");
    var code = doc.createElement("code");
    code.textContent = node.code;
    pre.appendChild(code);
    wrap.appendChild(pre);
    return wrap;
  }

  function buildTable(node, doc) {
    var table = doc.createElement("table");
    table.className = "md-table";
    var thead = doc.createElement("thead");
    var tr = doc.createElement("tr");
    node.headers.forEach(function (h) {
      var th = doc.createElement("th");
      appendInlines(th, h, doc);
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = doc.createElement("tbody");
    node.rows.forEach(function (row) {
      var r = doc.createElement("tr");
      row.forEach(function (cell) {
        var td = doc.createElement("td");
        appendInlines(td, cell, doc);
        r.appendChild(td);
      });
      tbody.appendChild(r);
    });
    table.appendChild(tbody);
    var wrap = doc.createElement("div");
    wrap.className = "table-wrap";
    wrap.appendChild(table);
    return wrap;
  }

  /* 渲染到容器（清空后重建） */
  function renderTo(container, raw) {
    var doc = container.ownerDocument || document;
    while (container.firstChild) container.removeChild(container.firstChild);
    var ast = parseMarkdown(raw);
    container.appendChild(renderAst(ast, doc));
    return container;
  }

  RT.markdown = {
    parse: parseMarkdown,
    renderTo: renderTo,
    renderAst: renderAst,
    cleanHref: cleanHref,
  };
})(window);
