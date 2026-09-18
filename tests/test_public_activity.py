import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from runtime.public_activity import (RunActivityProjector, FinalContentStream, public_summary,
    public_text, public_wire, public_approval, stored_public_events)
from runtime.task_manager import TaskManager


def projector():
    sent = []
    p = RunActivityProjector("run-test", None, lambda name, event: sent.append(event))
    return p, sent


@pytest.mark.parametrize("name,args", [("search_files", {"query": "login"}), ("read_file", {"path": "auth.py"}), ("write_file", {"path": "auth.py"}), ("run_command", {})])
def test_tool_real_start_completion_failure(name, args):
    p, sent = projector()
    cid = p.tool_started(name, args, "one")
    p.tool_finished(cid, "done")
    cid = p.tool_started(name, args, "two")
    p.tool_finished(cid, "Error: private traceback", error=True)
    p.flush()
    assert any(e["type"] == "tool.started" for e in sent)
    assert any(e["type"] == "tool.completed" for e in sent)
    assert any(e["type"] == "tool.failed" for e in sent)
    assert "private traceback" not in json.dumps(sent)


def test_ten_reads_aggregate_dedupe_and_throttle():
    async def scenario():
        p, sent = projector()
        for n in range(10):
            cid = p.tool_started("read_file", {"path": f"f{n}.py"}, str(n))
            p.tool_started("read_file", {}, str(n))
            p.tool_finished(cid, "file content")
            p.tool_finished(cid, "file content")
        p.flush()
        assert len(p.rows) == 1
        assert p.rows[0]["label"] == "已检查 10 个文件"
        assert len(sent) < 10
    asyncio.run(scenario())


def test_write_counts_success_unique_files_only():
    p, _ = projector()
    for n, result in enumerate(["done", "done", "错误：写入被拒绝"]):
        cid = p.tool_started("write_file", {"path": "a.py" if n < 2 else "b.py"})
        p.tool_finished(cid, result)
    assert len(p.files) == 1


def test_verification_failure_fix_and_reverify():
    p, sent = projector()
    cid = p.tool_started("run_python", {})
    p.tool_finished(cid, "退出码: 1\nprivate traceback")
    p.fixing()
    cid = p.tool_started("write_file", {"path": "auth.py"})
    p.tool_finished(cid, "updated")
    cid = p.tool_started("run_python", {})
    p.tool_finished(cid, "12 passed\n退出码: 0")
    p.generating()
    p.delta("已经修复。")
    p.finish("completed")
    kinds = [e["type"] for e in sent]
    labels = [e["label"] for e in sent]
    assert "verification.failed" in kinds and "verification.completed" in kinds
    assert "正在修正" in labels and "正在重新验证" in labels
    assert "12 项测试通过" in labels
    assert kinds.index("response.generating") < kinds.index("assistant.delta") < kinds.index("run.completed")
    assert "generating" not in [r["activity_id"] for r in sent[kinds.index("assistant.delta")]["metadata"]["activities"]]
    assert "private traceback" not in json.dumps(sent)


def test_unknown_verification_is_not_passed():
    p, _ = projector()
    p.verify_started()
    p.verify_finished("executed without exit code")
    assert p.verification == "failed"


def test_approval_pauses_all_running_rows():
    p, sent = projector()
    cid = p.tool_started("read_file", {})
    p.wait()
    p.tool_finished(cid, "done")
    assert sent[-1]["status"] == "waiting_for_user"
    assert not any(r["status"] == "running" for r in p.rows)
    raw = {"id": "a", "tool_name": "delete_file", "arguments": {"path": "a.py", "token": "VERY_PRIVATE"}}
    assert "VERY_PRIVATE" not in json.dumps(public_approval(raw))
    assert "a.py" in public_approval(raw)["label"]


def test_sensitive_tool_data_never_public():
    p, sent = projector()
    args = {"api_key": "key123", "password": "password123", "token": "token123", "command": 'curl -H "Authorization: Bearer abc-secret"'}
    cid = p.tool_started("exec_command", args)
    p.tool_finished(cid, "Cookie: private-value\nTraceback (most recent call last):")
    p.finish("failed")
    text = json.dumps(sent)
    for bad in ["key123", "password123", "token123", "abc-secret", "private-value", "Traceback", "exec_command", "arguments"]:
        assert bad not in text


def test_public_summary_limits():
    for bad in ["我先分析一下，可能有两个原因。", "已找到文件。" * 50, "password=12345", "可能是登录问题。", "一。二。三。"]:
        assert public_summary(bad) is None
    assert public_summary("发现会话在刷新前被清理，正在核对失败条件。")


def test_final_decoder_split_json_escapes_secret_and_think():
    result = []
    decoder = FinalContentStream(result.append)
    raw = json.dumps({"reasoning": "INTERNAL_REASON", "nested": {"content": "PRIVATE_NESTED"},
                      "content": '<think>PRIVATE_THOUGHT\nprivate continuation</think>\n结果如下。\npassword=hidden123\n测试通过。'}, ensure_ascii=True)
    for char in raw:
        decoder.feed(char)
    assert decoder.complete
    visible = "".join(result)
    assert "结果如下" in visible and "测试通过" in visible
    for bad in ["PRIVATE", "private continuation", "hidden123", "INTERNAL_REASON"]:
        assert bad not in visible
    assert len(result) > 1


def test_unstructured_intermediate_output_never_streams():
    sent = []
    d = FinalContentStream(sent.append)
    d.feed("我先分析一下……\nThen I should call a tool")
    assert not sent


def test_multiline_content_emits_incrementally_per_line_not_as_single_final_blob():
    """真打字机：多行 content 必须逐行增量 emit（不是攒到 final 一次性爆发出所有行），
    且过滤后行内的秘密内容不能出现在任何一帧里。"""
    result = []
    decoder = FinalContentStream(result.append)
    raw = json.dumps({"content": "第一行结论。\n第二行说明。\npassword=hidden456\n结果通过。"},
                      ensure_ascii=True)
    # 喂到「第二行说明。」刚闭合（仍在字符串值内部、未到结尾引号）时的中间快照：
    snapshot_at_second_line = None
    for i, char in enumerate(raw):
        decoder.feed(char)
        # 在喂完 raw 前，一旦 decoded 已包含"第二行说明。"但还没到 final，记一次 emit 次数
        if "第二行说明。" in decoder.decoded and i < len(raw) - 1 and not decoder.complete:
            if snapshot_at_second_line is None:
                snapshot_at_second_line = len(result)
    assert decoder.complete
    visible = "".join(result)
    assert "第一行结论" in visible and "第二行说明" in visible and "结果通过" in visible
    assert "hidden456" not in visible
    # 关键断言：在 JSON 字符串值尚未最终闭合（decoder.complete 仍 False）时，
    # 必须已经产生了至少 1 次非 final 增量 emit（否则退化成"整段一帧"的老 bug）。
    assert snapshot_at_second_line is not None and snapshot_at_second_line > 0, (
        "多行 content 应在到达行边界时就产生增量 emit，而不是攒到 final 一帧全部发出")


def test_single_line_content_still_reaches_user_via_final_frame_not_lost():
    """单行 content（无换行）在新方案下非 final 帧产生 0 增量是预期行为（受控降级，
    安全优先：避免半截 JSON 假闭包解码污染 sent 前缀）；但 final 帧必须把全文补完，
    内容不能因此丢失。"""
    result = []
    decoder = FinalContentStream(result.append)
    single = "这是一段没有换行的单行最终答案。"
    raw = json.dumps({"content": single}, ensure_ascii=True)
    for char in raw:
        decoder.feed(char)
    assert decoder.complete
    assert "".join(result) == single, "单行 content 必须完整可见，不允许因打字机改造而丢失"


def test_sse_boundary_default_deny():
    for kind in ["reply_delta", "thinking", "analysis", "tool", "tool.progress", "stream_reset"]:
        assert public_wire("r", kind, {"text": "PRIVATE"}) is None
    assert public_wire("r", "activity", {"visibility": "internal"}) is None
    assert public_wire("r", "activity", {"visibility": "public", "channel": "activity", "run_id": "other"}) is None
    assert "PRIVATE" not in str(public_wire("r", "run.failed", {"message": "Traceback PRIVATE"}))


def test_no_tool_qa_has_no_manufactured_activity():
    p, sent = projector()
    p.generating()
    p.delta("这个模块负责会话管理。")
    p.finish("completed")
    assert not any(e["channel"] == "activity" for e in sent)


def test_existing_event_store_replay(tmp_path):
    mgr = TaskManager(tmp_path / "events.db")
    task = mgr.create_task(session_id="s", goal="public projection")
    p = RunActivityProjector(task.id, mgr)
    cid = p.tool_started("read_file", {"path": "a.py", "token": "PRIVATE"})
    p.tool_finished(cid, "PRIVATE CONTENT")
    p.finish("completed")
    replay = stored_public_events(mgr, task.id)
    assert replay[-1]["type"] == "run.completed"
    assert all(e["visibility"] == "public" for e in replay)
    assert "PRIVATE" not in json.dumps(replay)


def test_sdk_reasoning_tool_args_and_content_isolation():
    import main
    events = []
    for kind, value in [("response.reasoning_text.delta", "PRIVATE_REASON"),
                        ("response.function_call_arguments.delta", "PRIVATE_ARGS"),
                        ("response.output_text.delta", "PRIVATE_INTERMEDIATE")]:
        events.append(NS(type="raw_response_event", data=NS(type=kind, delta=value)))

    class Stream:
        async def stream_events(self):
            for e in events:
                yield e

    async def scenario():
        public = []
        with patch.object(main.Runner, "run_streamed", return_value=Stream()):
            await main._run_attempt("stream", "question", None, None, 5, agent=NS(_public_final=False), stream_events_cb=lambda n, p: public.append(p))
        assert public == []
        with patch.object(main.Runner, "run_streamed", return_value=Stream()):
            await main._run_attempt("stream", "question", None, None, 5, agent=NS(_public_final=True), stream_events_cb=lambda n, p: public.append(p))
        assert public == [{"text": "PRIVATE_INTERMEDIATE"}]
    asyncio.run(scenario())
