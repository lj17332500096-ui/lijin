/* frontend_smoke.js — L2 前端冒烟（Playwright，?backend=mock）。
 * 自包含：启动静态服务（/rt -> web/runtime）加载 runtime.html?backend=mock，
 * 依次断言：加载/RT 初始化 → 欢迎页（标题+4卡）→ 设置/资料 → composer/模型选择器 →
 * 主题切换（dark↔light 背景变化）→ 无 JS 错误。
 * 退出码：0=PASS，1=FAIL；playwright/chromium 缺失时输出 SKIP_* 并退出 0（供 pytest skip）。
 */
const http = require("http");
const fs = require("fs");
const path = require("path");

let pw;
try { pw = require("playwright"); }
catch (e) { console.log("SKIP_PLAYWRIGHT_MISSING"); process.exit(0); }

const ROOT = path.join(__dirname, "..", "web");
const MIME = { ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
  ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png" };

function startServer() {
  return new Promise((resolve, reject) => {
    const srv = http.createServer((req, res) => {
      let p = decodeURIComponent(req.url.split("?")[0]);
      if (p === "/") p = "/runtime.html";
      if (p.indexOf("/rt/") === 0) p = "/runtime/" + p.slice(4);
      fs.readFile(path.join(ROOT, p), (err, data) => {
        if (err) { res.writeHead(404); res.end("404"); return; }
        res.writeHead(200, { "Content-Type": MIME[path.extname(p)] || "application/octet-stream" });
        res.end(data);
      });
    });
    srv.listen(0, () => resolve(srv));
  });
}

(async () => {
  const srv = await startServer();
  const port = srv.address().port;
  const checks = [];
  const problems = [];
  const ok = (n, c) => checks.push((c ? "PASS" : "FAIL") + "  " + n);

  let browser;
  try { browser = await pw.chromium.launch({ channel: "msedge" }); }
  catch (e) {
    // 未安装浏览器（CI 未 prefetch）时降级到默认 chromium，仍失败则 skip
    try { browser = await pw.chromium.launch(); }
    catch (e2) { console.log("SKIP_CHROMIUM_MISSING"); process.exit(0); }
  }

  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.on("pageerror", (e) => problems.push("PAGEERROR: " + e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/favicon/.test(m.text())) problems.push("CONSOLE: " + m.text()); });

  await page.goto(`http://127.0.0.1:${port}/runtime.html?backend=mock`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  ok("RT workspace initialized", await page.evaluate(() => !!(window.RT && RT.workspace)));
  ok("no pageerror on load", problems.filter((p) => p.startsWith("PAGEERROR")).length === 0);
  ok("theme default dark set", await page.evaluate(() => document.documentElement.getAttribute("data-theme") === "dark"));

  // 欢迎页
  ok("welcome title (现在，想做什么？)", await page.evaluate(() => {
    const t = document.querySelector(".home-intro h1");
    return !!t && t.textContent.indexOf("现在，想做什么？") >= 0;
  }));
  ok("welcome has 4 example cards", await page.evaluate(() => document.querySelectorAll(".home-shortcut").length === 4));

  // 设置
  await page.evaluate(() => { if (window.RT && RT.workspace) RT.workspace.showSettings(); });
  await page.waitForTimeout(500);
  ok("settings renders (real panel + tabs)", await page.evaluate(() =>
    !!document.querySelector(".settings, .settings-topbar") && document.querySelectorAll(".settings-nav .settings-tab").length === 6));

  // 资料
  await page.evaluate(() => { if (window.RT && RT.workspace) RT.workspace.showMaterials(); });
  await page.waitForTimeout(500);
  ok("materials renders", await page.evaluate(() => !!document.querySelector(".materials")));

  // 回到主页（欢迎态）+ composer
  await page.evaluate(() => { if (window.RT && RT.workspace) RT.workspace.goHome(); });
  await page.waitForTimeout(600);
  ok("composer present", await page.evaluate(() => !!document.getElementById("composerInput") && !document.querySelector(".composer-wrap").classList.contains("hidden")));
  ok("model selection managed by backend", await page.locator("#composerModel").count() === 0);

  // 主题切换 dark -> light
  const bgDark = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
  await page.waitForTimeout(250);
  const bgLight = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  ok("theme switch changes background (dark→light)", bgDark !== bgLight);

  // 顶栏 ⋯ 菜单
  await page.evaluate(() => document.getElementById("moreBtn").click());
  await page.waitForTimeout(200);
  ok("⋯ more menu opens", await page.evaluate(() => document.getElementById("moreMenu").classList.contains("show") && document.querySelectorAll("#moreMenu .dd-item").length >= 4));

  console.log("\n=== FRONTEND SMOKE (mock) ===");
  checks.forEach((c) => console.log(c));
  console.log("\n=== RUNTIME PROBLEMS (" + problems.length + ") ===");
  problems.forEach((p) => console.log(" - " + p));
  const fails = checks.filter((c) => c.startsWith("FAIL")).length;
  const pass = fails === 0 && problems.length === 0;
  console.log("\nSMOKE_RESULT: " + (pass ? "PASS" : "FAIL"));

  await browser.close();
  srv.close();
  process.exit(pass ? 0 : 1);
})().catch((e) => { console.error("FATAL", e); process.exit(2); });
