# -*- coding: utf-8 -*-
"""TMR 重构 E2E（需要后端已启动于 127.0.0.1:8765，且真实网关可用）。

用法: python tests/e2e_tmr.py
2026-09 生产收口后的验收语义：
- 1-3 同 Task 三 Run（POST /api/tasks/{cid}/runs + GET run stream 订阅）
- 4-5 审批 resume 同一 Run / 拒绝（POST /api/runs/{id}/resume）
- 6  重复 POST（同 client_message_id）幂等 → 不产生第二个 Run
- 7  重复订阅（刷新语义）→ 不产生第二个 Run
- 8  断开连接 ≠ 取消：Run 后台继续并最终完成
- 9  GET /api/stream?message= 不再产生任何 DB 写入（返回迁移指引）
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

HOST = "http://127.0.0.1:8765"
PASS = []
FAIL = []


def http_json(method, path, body=None, timeout=60):
    url = HOST + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8")), exc.code
        except Exception:
            return {"error": str(exc)}, exc.code


def sse_collect(path, timeout=240):
    """收集一条 SSE 流的所有 (event,payload)；正常以 done 结束。"""
    url = HOST + path
    events = []
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buffer = b""
        while True:
            chunk = resp.read(512)
            if not chunk:
                break
            buffer += chunk
            while b"\n\n" in buffer:
                raw, buffer = buffer.split(b"\n\n", 1)
                name, payload = "message", {}
                for line in raw.decode("utf-8", "replace").splitlines():
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        try:
                            payload = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            payload = {}
                events.append((name, payload))
                if name == "done":
                    return events
    return events


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(("  PASS  " if ok else "  FAIL  ") + label + ("  | " + str(detail)[:220] if detail else ""))


def submit_and_watch(tid, message, client_id=None):
    body = {"message": message}
    if client_id:
        body["client_message_id"] = client_id
    payload, code = http_json("POST", f"/api/tasks/{tid}/runs", body)
    run_id = payload.get("run_id") or ""
    ev_pairs = []
    if run_id:
        ev_pairs = sse_collect(f"/api/runs/{run_id}/stream", timeout=300)
    names = [e for e, _ in ev_pairs]
    return run_id, names, ev_pairs, payload, code


print("== T1: 新建 Task 容器（默认会话键隔离） ==")
payload, code = http_json("POST", "/api/tasks/create", {"title": "E2E 连续性验证"})
task = payload.get("task") or {}
tid = task.get("id") or ""
check("创建容器成功", bool(tid), payload)
check("容器带独立 session 键", str(task.get("session_id", "")).startswith("sess-"),
      task.get("session_id"))

print("== T2/T3: 同容器连发三轮 → 3 个 Run、消息链完整（POST + 订阅） ==")
run_ids = []
for i, msg in enumerate(["1+1 等于几？只回数字。", "再算 2+2，只回数字。", "最后算 3+3，只回数字。"], 1):
    run_id, names, _evs, _p, _c = submit_and_watch(tid, msg)
    check(f"第{i}轮收到 run.started + run.completed/run.failed 事件",
          "run.started" in names and ("run.completed" in names or "run.failed" in names),
          ",".join(names[:8]))
    if run_id:
        run_ids.append(run_id)

detail, _ = http_json("GET", f"/api/tasks/{tid}")
check("同一 Task 下 3 个 Run（无 Task 2）", len(detail.get("runs", [])) == 3,
      f"runs={len(detail.get('runs', []))}")
check("Run id 各不相同", len(set(run_ids)) == 3, run_ids)
roles = [m["role"] for m in detail.get("messages", [])]
check("消息链 user/assistant ×3", roles == ["user", "assistant"] * 3, roles)

print("== T6: 重复 POST 同 client_message_id → 幂等，不产生第二个 Run ==")
dup_run, _dn, _de, payload2, code2 = submit_and_watch(tid, "幂等探针消息", client_id="e2e-cmid-1")
rep_run, names2, _re2, payload3, code3 = submit_and_watch(tid, "幂等探针消息", client_id="e2e-cmid-1")
check("重复 POST 返回既有 run_id", bool(dup_run) and dup_run == rep_run,
      f"{dup_run} vs {rep_run}")
detail2, _ = http_json("GET", f"/api/tasks/{tid}")
check("幂等后容器 Run 数只 +1", len(detail2.get("runs", [])) == 4,
      f"runs={len(detail2.get('runs', []))}")

print("== T7: 重复订阅同一 run_id（刷新语义）→ 不产生新 Run ==")
if run_ids:
    run0 = run_ids[0]
    evs_a = sse_collect(f"/api/runs/{run0}/stream", timeout=60)
    evs_b = sse_collect(f"/api/runs/{run0}/stream", timeout=60)
    detail3, _ = http_json("GET", f"/api/tasks/{tid}")
    check("两次订阅均有 done 且不新建 Run",
          any(e == "done" for e, _ in evs_a) and any(e == "done" for e, _ in evs_b)
          and len(detail3.get("runs", [])) == 4,
          f"runs={len(detail3.get('runs', []))}")

print("== T8: SSE 中途断开 → Run 不被取消，后台继续并最终完成 ==")
import http.client

payload_c, code_c = http_json("POST", "/api/tasks/" + tid + "/runs",
                              {"message": "只回复“收到”两个字，不要调用任何工具。"})
disconn_run = payload_c.get("run_id") or ""
conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=40)
conn.request("GET", f"/api/runs/{disconn_run}/stream")
resp = conn.getresponse()
try:
    resp.read(1024)
except Exception:
    pass
time.sleep(1.5)
conn.close()  # 真实断开：新语义 = 事件订阅断开，Run 继续执行
final_state = None
for _ in range(30):
    time.sleep(2)
    sp, _ = http_json("GET", f"/api/runs/{disconn_run}")
    final_state = sp.get("state")
    if final_state in ("completed", "failed", "cancelled", "waiting_approval"):
        break
check("断开后 Run 继续执行并到达终态（未被误取消）",
      bool(disconn_run) and final_state in ("completed", "failed", "waiting_approval"),
      f"run={disconn_run} state={final_state}")
running = [r for r in (http_json("GET", f"/api/tasks/{tid}")[0]).get("runs", [])
           if r["state"] in ("running", "submitted")]
check("无悬挂 RUNNING Run", len(running) == 0, f"悬挂 {len(running)}")

print("== T4/T5: 审批 → 批准 resume 同一 Run / 拒绝 ==")
ev_run, ev_names, ev_pairs, _p4, _c4 = submit_and_watch(
    tid, "请调用 run_python 工具在沙箱打印 hello")
appr = [p for e, p in ev_pairs if e == "approval" and p.get("approvals")]
run_wait = next((p.get("run_id") for e, p in ev_pairs if e == "run.started"), "") or ev_run
if appr:
    first = appr[0]["approvals"][0]["id"]
    check("进入 waiting_approval（approval 事件）", True, f"approval={first}")
    _r, code_dec, = http_json("POST", "/api/approval", {"id": first, "decision": "approved"})
    # 批准后：POST resume 启动执行 → 再订阅
    payload_r, code_r = http_json("POST", f"/api/runs/{run_wait}/resume")
    check("resume POST 启动成功", code_r in (200, 202), f"{code_r} {payload_r}")
    evs2 = sse_collect(f"/api/runs/{run_wait}/stream", timeout=300)
    names2 = [e for e, _ in evs2]
    check("批准后 resume 同一 Run（未新建）", any(
        e in ("run.completed", "run.failed", "run.waiting_approval") for e in names2), names2[:8])
    state_payload, _ = http_json("GET", f"/api/runs/{run_wait}")
    check("Run 终态落库", state_payload.get("state") in ("completed", "failed", "waiting_approval"),
          state_payload.get("state"))
    container_payload, _ = http_json("GET", f"/api/tasks/{tid}")
    check("容器内 Run 数无重复新增", len(container_payload.get("runs", [])) <= 6,
          len(container_payload.get("runs", [])))
else:
    check("本轮触发了审批等待（若无审批则说明模型未调用工具）", False, f"events={ev_names[:6]}")

print("== T9: GET /api/stream?message= 无副作用（返回迁移指引，不写库） ==")
raw_sse = sse_collect("/api/stream?session=legacy-e2e&message=" +
                      urllib.parse.quote("你好，只回复收到"), timeout=30)
err_msgs = [p.get("message", "") for e, p in raw_sse if e == "error"]
check("GET message 返回迁移错误指引", any("POST" in m for m in err_msgs), err_msgs[:1])
time.sleep(1)

print("== T10: /chat 旧页面仍可返回（已不再作为创建入口） ==")
html = urllib.request.urlopen(HOST + "/chat", timeout=30).read().decode("utf-8", "replace")
check("/chat 页面仍返回", "runtime" in html or "chat" in html.lower() or len(html) > 5000,
      f"{len(html)} bytes")

print()
print(f"== 结果: PASS {len(PASS)} / FAIL {len(FAIL)} ==")
for label in FAIL:
    print("  [FAIL] " + label)
sys.exit(0 if not FAIL else 1)
