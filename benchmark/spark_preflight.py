"""Phase 30: Ornith local provider smoke test + function calling qualification.

Tests:
  P1 /v1/models
  P2 simple completion
  P3 reasoning response
  P4 streaming
  FC-A calculate (N=5)
  FC-B run_tests (N=5)
  FC-C 14-tool coding profile (N=5)
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = "http://localhost:8080/v1"
MODEL = "ornith"


def post(path, payload, timeout=300):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer local-no-key"})
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode("utf-8", "replace"))
        return resp.status, data, round(time.monotonic() - t0, 2), None
    except urllib.error.HTTPError as e:
        return e.code, None, round(time.monotonic() - t0, 2), f"HTTP {e.code}: {e.read(200).decode('utf-8','replace')}"
    except Exception as e:
        return None, None, round(time.monotonic() - t0, 2), f"{type(e).__name__}: {str(e)[:200]}"


def get(path, timeout=10):
    req = urllib.request.Request(BASE + path)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, json.loads(resp.read().decode("utf-8", "replace")), None
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:200]}"


def main():
    results = {}

    # P1 /v1/models
    status, data, err = get("/models")
    ids = [m.get("id") or m.get("name") for m in (data or {}).get("data", [])] if data else []
    results["P1_models"] = {"status": status, "ids": ids[:3], "error": err}

    # P2 simple completion
    status, data, el, err = post("/chat/completions", {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 200, "stream": False,
    })
    msg = (data or {}).get("choices", [{}])[0].get("message", {}) if data else {}
    results["P2_completion"] = {
        "status": status, "elapsed": el,
        "content": (msg.get("content") or "")[:80],
        "reasoning_present": bool(msg.get("reasoning_content")),
        "error": err,
    }

    # P4 streaming
    status, data, el, err = post("/chat/completions", {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say hi"}],
        "max_tokens": 50, "stream": True,
    })
    results["P4_streaming"] = {"status": status, "error": err,
                                "note": "non-stream path only (urllib)"}

    # FC-A: calculate
    calc_tool = {"type": "function", "function": {
        "name": "calculate", "description": "Calculate a math expression",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}},
                       "required": ["expression"]}}}
    a_ok = 0
    a_valid = 0
    a_name = 0
    a_lat = []
    for i in range(5):
        status, data, el, err = post("/chat/completions", {
            "model": MODEL,
            "messages": [{"role": "user", "content": f"计算 {i+2}+{i+3}，请调用 calculate 工具。"}],
            "tools": [calc_tool], "max_tokens": 400, "stream": False,
        })
        a_lat.append(el)
        tc = ((data or {}).get("choices", [{}])[0].get("message", {}) or {}).get("tool_calls")
        if tc:
            a_ok += 1
            name = tc[0].get("function", {}).get("name")
            if name == "calculate":
                a_name += 1
            try:
                json.loads(tc[0].get("function", {}).get("arguments") or "{}")
                a_valid += 1
            except Exception:
                pass
        print(f"  FC-A[{i+1}] tc={bool(tc)} el={el}s", flush=True)
    results["FC_A_calculate"] = {
        "n": 5, "tool_call_generated": a_ok, "tool_name_correct": a_name,
        "valid_json_args": a_valid, "latency_avg": round(sum(a_lat)/len(a_lat), 1),
    }

    # FC-B: run_tests
    rt_tool = {"type": "function", "function": {
        "name": "run_tests", "description": "Run project pytest tests",
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"}, "target": {"type": "string"}},
            "required": ["project"]}}}
    b_ok = 0
    b_valid = 0
    b_sem = 0
    b_lat = []
    for i in range(5):
        status, data, el, err = post("/chat/completions", {
            "model": MODEL,
            "messages": [{"role": "user", "content": "修复了 micro_fixture 项目里的 calc.py，请运行项目测试验证修改。"}],
            "tools": [rt_tool], "max_tokens": 400, "stream": False,
        })
        b_lat.append(el)
        tc = ((data or {}).get("choices", [{}])[0].get("message", {}) or {}).get("tool_calls")
        if tc:
            b_ok += 1
            try:
                args = json.loads(tc[0].get("function", {}).get("arguments") or "{}")
                b_valid += 1
                if args.get("project"):
                    b_sem += 1
            except Exception:
                pass
        print(f"  FC-B[{i+1}] tc={bool(tc)} el={el}s", flush=True)
    results["FC_B_run_tests"] = {
        "n": 5, "tool_call_generated": b_ok, "valid_json_args": b_valid,
        "semantic_verification": b_sem, "latency_avg": round(sum(b_lat)/len(b_lat), 1),
    }

    # FC-C: 14-tool profile
    tools = [
        {"type": "function", "function": {"name": "get_current_datetime", "description": "Get current datetime", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "read_workspace_file", "description": "Read a workspace file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
        {"type": "function", "function": {"name": "list_workspace_files", "description": "List workspace files", "parameters": {"type": "object", "properties": {"directory": {"type": "string"}}}}},
        calc_tool,
        {"type": "function", "function": {"name": "run_python", "description": "Run python code in sandbox", "parameters": {"type": "object", "properties": {"project": {"type": "string"}, "code": {"type": "string"}}, "required": ["project"]}}},
        rt_tool,
        {"type": "function", "function": {"name": "edit_project_file", "description": "Edit a project file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}}},
        {"type": "function", "function": {"name": "index_workspace", "description": "Index workspace docs", "parameters": {"type": "object", "properties": {"directory": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "search_documents", "description": "Search docs", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
        {"type": "function", "function": {"name": "write_code_file", "description": "Write a code file", "parameters": {"type": "object", "properties": {"project": {"type": "string"}, "filename": {"type": "string"}, "content": {"type": "string"}}, "required": ["project", "filename", "content"]}}},
        {"type": "function", "function": {"name": "read_code_file", "description": "Read a code file", "parameters": {"type": "object", "properties": {"project": {"type": "string"}, "filename": {"type": "string"}}, "required": ["project", "filename"]}}},
        {"type": "function", "function": {"name": "list_code_files", "description": "List code files", "parameters": {"type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"]}}},
        {"type": "function", "function": {"name": "code_loop", "description": "Code verify loop", "parameters": {"type": "object", "properties": {"project": {"type": "string"}, "filename": {"type": "string"}}, "required": ["project", "filename"]}}},
        {"type": "function", "function": {"name": "write_project_file", "description": "Write a project file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    ]
    c_ok = 0
    c_valid = 0
    c_fake = 0
    c_lat = []
    for i in range(5):
        status, data, el, err = post("/chat/completions", {
            "model": MODEL,
            "messages": [{"role": "user", "content": "读取工作区的 agent.py 文件内容。"}],
            "tools": tools, "max_tokens": 400, "stream": False,
        })
        c_lat.append(el)
        msg = (data or {}).get("choices", [{}])[0].get("message", {}) or {}
        tc = msg.get("tool_calls")
        content = msg.get("content") or ""
        if tc:
            c_ok += 1
            try:
                json.loads(tc[0].get("function", {}).get("arguments") or "{}")
                c_valid += 1
            except Exception:
                pass
        # fake tool call = content contains tool-like text but no real tool_calls
        if not tc and ("read_workspace_file" in content or "tool_call" in content.lower()):
            c_fake += 1
        print(f"  FC-C[{i+1}] tc={bool(tc)} el={el}s", flush=True)
    results["FC_C_14tools"] = {
        "n": 5, "tool_call_generated": c_ok, "valid_json_args": c_valid,
        "plain_text_fake_tool_call": c_fake, "latency_avg": round(sum(c_lat)/len(c_lat), 1),
    }

    print(json.dumps(results, ensure_ascii=False, indent=2))
    with open("phase30/provider_smoke.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    os.makedirs("phase30", exist_ok=True)
    main()
