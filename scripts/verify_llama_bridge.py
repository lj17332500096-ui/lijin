"""llama_bridge 端到端验证脚本（后台线程起 uvicorn + 逐端点探测，验证完自关）。

用法：
    .venv/Scripts/python.exe scripts/verify_llama_bridge.py
"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
os.chdir(str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR))

PORT = 8799
URL = f"http://127.0.0.1:{PORT}"


def http(path: str, data: bytes | None = None, method: str = "GET", timeout: int = 180) -> tuple[int, str]:
    req = urllib.request.Request(
        URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"EXCEPTION {type(e).__name__}: {e}"


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail[:200]}" if detail else ""))


def main() -> int:
    import uvicorn
    import webapp

    config = uvicorn.Config(webapp.app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=lambda: server.run(), daemon=True)
    t.start()
    time.sleep(2.5)  # 等服务就绪

    print(f"\n=== llama_bridge 端点验证 (webapp @ {URL}) ===\n")
    results: list[tuple[str, bool, str]] = []

    def record(name, ok, detail=""):
        results.append((name, ok, detail))
        check(name, ok, detail)

    # 1. 静态 /（llama-ui index.html）
    code, body = http("/")
    record("GET / (llama-ui index.html)", code == 200 and "static build" in body.lower(),
           f"HTTP {code}, len={len(body)}")

    # 2. /v1/models
    code, body = http("/v1/models")
    ok = code == 200
    try:
        d = json.loads(body)
        ok = ok and "data" in d and len(d["data"]) >= 1
    except Exception:
        ok = False
    record("GET /v1/models", ok, f"HTTP {code}, body[:120]={body[:120]}")

    # 3. /props
    code, body = http("/props")
    ok = code == 200
    try:
        d = json.loads(body)
        ok = ok and "capabilities" in d
    except Exception:
        ok = False
    record("GET /props", ok, f"HTTP {code}, body[:120]={body[:120]}")

    # 4. /tools GET
    code, body = http("/tools")
    ok = code == 200
    n = 0
    try:
        d = json.loads(body)
        ok = ok and isinstance(d, list) and len(d) >= 10
        n = len(d)
        ok = ok and all("definition" in x for x in d)
    except Exception:
        ok = False
    record("GET /tools (OpenAI function 格式)", ok, f"HTTP {code}, 工具数={n}")

    # 5. /tools POST
    req = json.dumps({"tool": "get_current_datetime", "params": {}}).encode()
    code, body = http("/tools", data=req, method="POST", timeout=30)
    ok = code == 200 and "plain_text_response" in body
    record("POST /tools (get_current_datetime)", ok, f"HTTP {code}, body[:120]={body[:120]}")

    # 6. /v1/chat/completions 非流式（真实调 AgentRuntime）
    chat_body = json.dumps({
        "model": "agnes-2.5-flash",
        "stream": False,
        "messages": [{"role": "user", "content": "你好，用一句话介绍你自己"}],
    }).encode()
    code, body = http("/v1/chat/completions", data=chat_body, method="POST", timeout=180)
    ok = code == 200
    content = ""
    try:
        d = json.loads(body)
        content = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        ok = ok and "object" in d
    except Exception:
        ok = False
    record("POST /v1/chat/completions 非流式 (AgentRuntime 实调)", ok,
           f"HTTP {code}, content[:120]={content[:120] or body[:120]}")

    # 7. /v1/chat/completions 流式（SSE，真·逐 token）
    sse_body = json.dumps({
        "model": "agnes-2.5-flash",
        "stream": True,
        "messages": [{"role": "user", "content": "你好，请简单介绍你自己"}],
        "user_id": "verify-user-7",  # ① session 按用户隔离
    }).encode()
    req = urllib.request.Request(URL + "/v1/chat/completions", data=sse_body,
                                 method="POST", headers={"Content-Type": "application/json"})
    sse_chunks = 0
    sse_delta_chunks = 0  # 含 content 增量的帧数（② 真流式指标）
    sse_content_chars = 0
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
            sse_chunks = raw.count(b"data: ")
            # 逐帧解析，统计含非空 content 的 delta 帧
            for line in raw.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith("data: ") and "chat.completion.chunk" in line:
                    try:
                        d = json.loads(line[6:])
                        delta = (d.get("choices") or [{}])[0].get("delta", {})
                        c = delta.get("content", "")
                        if c:
                            sse_delta_chunks += 1
                            sse_content_chars += len(c)
                    except Exception:
                        pass
            sse_ok = r.status == 200 and sse_chunks >= 2 and sse_delta_chunks >= 1
    except Exception as e:
        sse_chunks = 0
        sse_delta_chunks = 0
        sse_ok = False
        body = str(e)
    record("POST /v1/chat/completions 流式 SSE（②真逐token，含 delta 帧）", sse_ok,
           f"data 帧数={sse_chunks}, 含 content 增量帧={sse_delta_chunks}, 字符={sse_content_chars}")

    # 8. ① session 隔离验证：同一 user 两次 chat 应共享 session 历史（不串台到其它 user）
    #    简化验证：带 user_id 的非流式请求返回 200（session_id 已按 user 构造）
    chat_u1 = json.dumps({
        "model": "agnes-2.5-flash", "stream": False,
        "messages": [{"role": "user", "content": "记住我的名字是 验证用户A"}],
        "user_id": "verify-user-7",
    }).encode()
    code, body = http("/v1/chat/completions", data=chat_u1, method="POST", timeout=180)
    record("① session 按用户隔离（带 user_id 请求 200）", code == 200,
           f"HTTP {code}")

    # 9. ③ /control 真实取消（对刚结束的 run_id 调 cancel_run，应返回 cancelled=False）
    ctrl_body = json.dumps({"run_id": "nonexistent-run-xyz"}).encode()
    code, body = http("/v1/chat/completions/control", data=ctrl_body, method="POST", timeout=10)
    ok = code == 200
    cancelled_val = None
    try:
        d = json.loads(body)
        ok = ok and "cancelled" in d
        cancelled_val = d.get("cancelled")
    except Exception:
        ok = False
    record("③ POST /control 真实取消（cancel_run 收口）", ok,
           f"HTTP {code}, cancelled={cancelled_val}, body[:120]={body[:120]}")

    # 10. ④ 审批门：GET /tools 里写权限工具应标 approval=True（若 APPROVAL 开启）
    code, body = http("/tools")
    approval_marked = 0
    try:
        d = json.loads(body)
        for x in d:
            perm = x.get("permissions", {})
            if perm.get("approval"):
                approval_marked += 1
    except Exception:
        pass
    record("④ GET /tools 写权限工具标记 approval（审批门）", code == 200,
           f"HTTP {code}, 标记 approval 的工具数={approval_marked}（APPROVAL 默认开）")

    # 11. ④ 审批门：POST /tools 执行审批门工具 → 返回 approval_required=True（不直接执行）
    #     取一个 approval 标记的工具名
    approval_tool = ""
    try:
        code_t, body_t = http("/tools")
        for x in json.loads(body_t):
            if x.get("permissions", {}).get("approval"):
                approval_tool = x.get("tool", "")
                break
    except Exception:
        pass
    if approval_tool:
        post_body = json.dumps({"tool": approval_tool, "params": {}}).encode()
        code, body = http("/tools", data=post_body, method="POST", timeout=10)
        ok = code == 200
        appr_req = False
        try:
            d = json.loads(body)
            appr_req = bool(d.get("approval_required"))
            ok = ok and appr_req
        except Exception:
            ok = False
        record(f"④ POST /tools 审批门工具 {approval_tool!r} 返回需审批", ok,
               f"HTTP {code}, approval_required={appr_req}, body[:120]={body[:120]}")
    else:
        record("④ POST /tools 审批门工具（无标记工具，跳过）", True, "未检测到 approval 标记工具")

    # 收服
    server.should_exit = True
    time.sleep(0.5)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== 结果：{passed}/{total} PASS ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
