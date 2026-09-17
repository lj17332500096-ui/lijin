// tests/markdown_runner.js — Node 侧运行器：加载 markdown.js，读取 stdin JSON 批量解析输出 AST
const fs = require("fs");
const path = require("path");
global.window = global; // markdown.js IIFE 挂到 window
const src = fs.readFileSync(path.join(__dirname, "..", "web", "runtime", "markdown.js"), "utf-8");
eval(src);
const RT = global.RT;
let input = "";
process.stdin.setEncoding("utf-8");
process.stdin.on("data", (d) => { input += d; });
process.stdin.on("end", () => {
  try {
    const cases = JSON.parse(input || "[]");
    const out = cases.map((c) => ({
      ok: true,
      ast: RT.markdown.parse(c.text),
      href: RT.markdown.cleanHref(c.href),
    }));
    console.log(JSON.stringify(out));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: String(e) }));
    process.exit(1);
  }
});
