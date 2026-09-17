# -*- coding: utf-8 -*-
"""LIVE TEST 8：真实 Provider 连续 N 次合规输出，验证无空项目符号。

不进离线回归（文件名不以 test_ 开头，run_tests.py 不收集）。需要：
1. webapp 已在 127.0.0.1:8765 运行（.env 网关配置正确）；
2. Node 可用（复用 tests/markdown_runner.js 解析真实 markdown.js）。

用法：python tests/live_markdown_list_check.py [--runs 30]

每轮独立容器 → 提交“聊天内直接输出能力介绍 Markdown”任务（禁止调用工具/保存文件）
→ 等 Run completed → 取 DB 最终 assistant 内容 → 用真实 markdown.js 解析：
- 合规 = 内容含 “##” 且至少 1 个 ul/ol 列表项；
- 失败条件 = 列表项存在空文本（只剩项目符号）。
不合规/失败/超时自动补跑，直到攒满 runs 轮合规内容；最终要求空列表项=0。
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:8765"
BASE_DIR = Path(__file__).resolve().parents[1]
RUNNER = BASE_DIR / "tests" / "markdown_runner.js"

PROMPT = (
    "请直接在聊天回复里用中文输出你的能力介绍。"
    "不要调用任何工具、不要保存文件、不要输出代码块、不要引用资料。要求：\n"
    "1. 先写一句开场白；\n"
    "2. 然后依次用三个 Markdown 二级标题：「信息检索」「文档与知识库」「内容产出」；\n"
    "3. 每个标题下必须有一个无序列表（每行以 \"- \" 开头）写 3 条具体能力；\n"
    "4. 结束后不要再添加任何其它小节或说明。"
)


def _post(path: str, payload: dict):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str):
    with urllib.request.urlopen(BASE_URL + path, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_markdown(text: str) -> list[dict]:
    payload = json.dumps([{"text": text, "href": ""}], ensure_ascii=False)
    proc = subprocess.run(["node", str(RUNNER)], input=payload,
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)[0]["ast"]


def _list_stats(ast: list[dict]) -> tuple[int, int]:
    ul = ol = empty = 0
    for node in ast:
        if node.get("t") not in ("ul", "ol"):
            continue
        for item in node.get("items", []):
            text = "".join(n.get("v", "") for n in item)
            if node["t"] == "ul":
                ul += 1
            else:
                ol += 1
            if not text.strip():
                empty += 1
    return ul, ol, empty


def _wait_run(cid: str, run_id: str, timeout_s: int = 150) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        detail = _get(f"/api/tasks/{cid}")
        for run in detail.get("runs", []):
            if run["id"] == run_id:
                if run["state"] in ("completed", "failed", "cancelled"):
                    return run
        time.sleep(0.8)
    raise TimeoutError(f"run {run_id} 未在 {timeout_s}s 内结束")


def _assistant_content(cid: str, run_id: str) -> str:
    detail = _get(f"/api/tasks/{cid}")
    for msg in reversed(detail.get("messages", [])):
        if msg.get("role") == "assistant" and msg.get("run_id") == run_id:
            return msg.get("content") or ""
    return ""


def _run_failure_reason(cid: str, run_id: str) -> str:
    try:
        detail = _get(f"/api/runs/{run_id}")
        return str(detail.get("error") or detail.get("checkpoint") or "no detail")[:200]
    except Exception:  # noqa: BLE001
        return "fetch detail failed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--max-attempts", type=int, default=60)
    args = parser.parse_args()
    if args.runs <= 0:
        return 2

    texts: list[str] = []
    notes: list[str] = []
    attempts = 0
    while len(texts) < args.runs and attempts < args.max_attempts:
        attempts += 1
        cid = None
        try:
            created = _post("/api/tasks/create", {"title": f"bullet-live-{attempts}"})
            cid = created["task"]["id"]
            started = _post(f"/api/tasks/{cid}/runs", {
                "message": PROMPT,
                "client_message_id": f"bullet-live-{attempts}-{int(time.time() * 1000)}",
            })
            run_id = started["run_id"]
            run = _wait_run(cid, run_id)
            if run["state"] != "completed":
                notes.append(f"attempt {attempts}: run state={run['state']} "
                             f"({_run_failure_reason(cid, run_id)})")
                print(f"[{attempts}] skip state={run['state']}", flush=True)
                continue
            content = _assistant_content(cid, run_id)
            ast = _parse_markdown(content)
            ul, ol, empty = _list_stats(ast)
            compliant = ("##" in content and (ul + ol) > 0)
            if not compliant:
                notes.append(f"attempt {attempts}: 不合规 (ul={ul}, ol={ol}, "
                             f"chars={len(content)}) head={content[:90]!r}")
                print(f"[{attempts}] skip not-compliant ul={ul} ol={ol}", flush=True)
                continue
            if empty:
                notes.append(f"attempt {attempts}: 发现 {empty} 个空列表项")
                print(f"[{attempts}] FAIL empty={empty}", flush=True)
                # 空 bullet 是硬失败，不重试算通过
                continue
            texts.append(content)
            print(f"[{attempts}] ok ul={ul} ol={ol} chars={len(content)} "
                  f"({len(texts)}/{args.runs})", flush=True)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"attempt {attempts}: {type(exc).__name__}: {exc}")
            print(f"[{attempts}] EXC {type(exc).__name__}: {exc}", flush=True)
        finally:
            if cid:
                try:
                    _post(f"/api/tasks/{cid}/delete", {})
                except Exception:  # noqa: BLE001
                    pass

    print(f"\n===== 真实 Provider 列表检查 =====", flush=True)
    print(f"合规轮数={len(texts)}/{args.runs} attempts={attempts}", flush=True)
    if notes:
        print(f"跳过/失败记录 ({len(notes)}):", flush=True)
        for n in notes[-10:]:
            print(" -", n, flush=True)
    if len(texts) < args.runs:
        print(f"FAIL: 未能攒满 {args.runs} 轮合规输出", flush=True)
        return 1

    total_empty = 0
    total_ul = 0
    total_ol = 0
    for content in texts:
        ast = _parse_markdown(content)
        ul, ol, empty = _list_stats(ast)
        total_empty += empty
        total_ul += ul
        total_ol += ol
    print(f"列表项统计: ul_items={total_ul} ol_items={total_ol} "
          f"empty_items={total_empty}", flush=True)
    if total_empty:
        print("FAIL: 存在空列表项", flush=True)
        return 1
    print(f"PASS: {args.runs} 轮真实 Provider 合规输出，0 个空列表项", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
