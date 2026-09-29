/* contract_probe.js — L1 契约测试探针（Node，无 LLM / 无网络）。
 * 加载 web/runtime 下 types.js / mock.js / client.js（stub 掉 RT.http），
 * 断言 apiMock 与 apiClient 对同一接口返回的【契约字段集】一致，且含关键字段。
 * 退出码 0=OK，1=FAIL。由 tests/test_contract.py 调用。
 */
const vm = require("vm");
const fs = require("fs");
const path = require("path");

const DIR = path.join(__dirname, "..", "web", "runtime");

// 每个接口的契约字段子集（前端真正依赖的字段）
const CONTRACT = {
  task:     ["id", "title", "summary", "status", "session_id", "updated_at", "stat", "work_location_id"],
  run:      ["id", "state", "tool_calls"],
  approval: ["id", "task_id", "tool", "status"],
  artifact: ["id", "task_id", "name", "kind", "size_bytes"],
  tool:     ["name", "description", "category", "risk", "side_effect"],
};

function makeCtx() {
  const ctx = vm.createContext({
    console, Promise, setTimeout, clearTimeout, setInterval, clearInterval,
    JSON, Date, Math, Array, Object, String, Number, Error,
  });
  ctx.window = ctx;
  ctx.global = ctx;
  ctx.RT = {};
  return ctx;
}

function run(ctx, code) { return vm.runInContext(code, ctx); }

// 客户端 stub：模拟真实后端返回（这些响应必须携带契约字段）
function stubResp(url) {
  const u = url || "";
  if (u.indexOf("/api/tools") === 0) return { tools: [{ name: "read_excel", description: "读 Excel", category: "office", risk: "low", side_effect: false }], total: 1 };
  if (u.indexOf("/api/approvals") === 0) return { approvals: [{ id: "a1", task_id: "t1", tool: "run_python", status: "pending" }], total: 1 };
  if (u.indexOf("/api/artifacts") === 0) return { artifacts: [{ id: "ar1", task_id: "t1", name: "r.docx", kind: "word", size_bytes: 100 }], total: 1 };
  if (u.indexOf("/api/runs/") === 0) return { id: "r1", state: "completed", tool_calls: [], created_at: "2026-09-07T00:00:00Z" };
  if (u.indexOf("/api/runs") === 0) return { runs: [{ id: "r1", state: "completed", tool_calls: [] }], total: 1 };
  if (u.indexOf("/api/tasks/") === 0) return { id: "t1", title: "任务A", summary: "摘要", status: "completed", session_id: "s1", created_at: "2026-09-07T00:00:00Z", updated_at: "2026-09-07T00:00:00Z", stat: { runs: 1, messages: 1, latest_run: { state: "completed" } }, work_location_id: null };
  return { tasks: [{ id: "t1", title: "任务A", summary: "摘要", status: "completed", session_id: "s1", created_at: "2026-09-07T00:00:00Z", updated_at: "2026-09-07T00:00:00Z", stat: { runs: 1, messages: 1, latest_run: { state: "completed" } }, work_location_id: null }], total: 1 };
}

function hasKeys(obj, keys) { return keys.every(function (k) { return obj && Object.prototype.hasOwnProperty.call(obj, k); }); }

(async function main() {
  const results = [];
  function record(name, ok, detail) { results.push({ name: name, ok: ok, detail: detail || "" }); }

  const ctx = makeCtx();
  run(ctx, fs.readFileSync(path.join(DIR, "types.js"), "utf8"));
  run(ctx, fs.readFileSync(path.join(DIR, "api", "mock.js"), "utf8"));
  run(ctx, "RT.http = { get: function(u){ return Promise.resolve(" + stubResp.toString() + "(u)); }, post: function(u,b){ return Promise.resolve({ ok:true }); } };");
  run(ctx, fs.readFileSync(path.join(DIR, "api", "client.js"), "utf8"));

  const M = ctx.RT.apiMock;
  const C = ctx.RT.apiClient;
  const sortKeys = function (o) { return Object.keys(o || {}).slice().sort(); };

  // ---- tasks (listTasks 首条) ----
  const mT = (await M.listTasks({ limit: 1 })).tasks[0];
  const cT = (await C.listTasks({ limit: 1 })).tasks[0];
  record("listTasks: title", hasKeys(mT, ["title", "stat"]) && hasKeys(cT, ["title", "stat"]), "mock=" + sortKeys(mT).join(",") + " | client=" + sortKeys(cT).join(","));
  record("listTasks: stat.latest_run.state", mT.stat && mT.stat.latest_run && mT.stat.latest_run.state && cT.stat && cT.stat.latest_run && cT.stat.latest_run.state);
  record("listTasks: contract fields in client", hasKeys(cT, CONTRACT.task), "missing=" + CONTRACT.task.filter(function (k) { return !hasKeys(cT, [k]); }).join(","));

  // ---- getTask ----
  const mG = await M.getTask("task_mock01");
  const cG = await C.getTask("t1");
  record("getTask: contract fields", hasKeys(mG, ["id", "title", "stat"]) && hasKeys(cG, ["id", "title", "stat", "work_location_id"]), "mock=" + sortKeys(mG).join(",") + " | client=" + sortKeys(cG).join(","));
  record("getTask: mock has title+stat.latest_run", hasKeys(mG, ["title", "stat"]) && mG.stat && mG.stat.latest_run);

  // ---- approvals ----
  const mA = (await M.listApprovals({ limit: 1 })).approvals[0];
  const cA = (await C.listApprovals({ limit: 1 })).approvals[0];
  record("approval: contract fields", hasKeys(mA, CONTRACT.approval) && hasKeys(cA, CONTRACT.approval), "mock=" + sortKeys(mA).join(",") + " | client=" + sortKeys(cA).join(","));

  // ---- artifacts ----
  const mAr = (await M.listArtifacts({ limit: 1 })).artifacts[0];
  const cAr = (await C.listArtifacts({ limit: 1 })).artifacts[0];
  record("artifact: contract fields", hasKeys(mAr, CONTRACT.artifact) && hasKeys(cAr, CONTRACT.artifact), "mock=" + sortKeys(mAr).join(",") + " | client=" + sortKeys(cAr).join(","));

  // ---- tools ----
  const mTo = (await M.listTools())[0];
  const cTo = (await C.listTools())[0];
  record("tool: contract fields", hasKeys(mTo, CONTRACT.tool) && hasKeys(cTo, CONTRACT.tool), "mock=" + sortKeys(mTo).join(",") + " | client=" + sortKeys(cTo).join(","));

  // ---- run ----
  const mR = await M.getRun("r1");
  const cR = await C.getRun("r1");
  record("run: contract fields", hasKeys(mR, CONTRACT.run) && hasKeys(cR, CONTRACT.run), "mock=" + sortKeys(mR).join(",") + " | client=" + sortKeys(cR).join(","));

  // ---- 汇总 ----
  let failed = 0;
  results.forEach(function (r) {
    if (!r.ok) failed++;
    console.log((r.ok ? "PASS" : "FAIL") + "  " + r.name + (r.detail ? ("  [" + r.detail + "]") : ""));
  });
  console.log("\nCONTRACT_RESULT: " + (failed === 0 ? "PASS" : "FAIL (" + failed + ")"));
  process.exit(failed === 0 ? 0 : 1);
})().catch(function (e) { console.error("PROBE_ERROR", e && e.stack || e); process.exit(2); });
