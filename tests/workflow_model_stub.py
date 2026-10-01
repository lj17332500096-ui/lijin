"""Helpers for Runtime integration tests after API workflow orchestration."""
from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any


def workflow_model_stub(execute_fake: Callable[..., Any]) -> Callable[..., Any]:
    """Adapt an existing fake Runtime response to the two tool-free API stages.

    The review stub returns ``complete`` only because AgentRuntime reaches the
    review node after its deterministic completion and evidence gates pass.
    Tests of blocked/failed execution therefore keep exercising Runtime's
    actual safety decisions.
    """

    async def _wrapped(mode: str, message: str, *args: Any, **kwargs: Any) -> Any:
        agent = kwargs.get("agent")
        name = str(getattr(agent, "name", ""))
        if name == "FORGE 请求分析":
            try:
                request = str(json.loads(message).get("request") or "")
            except (TypeError, ValueError):
                request = ""
            direct_answer = not any(token in request.casefold() for token in (
                "查询", "搜索", "读取", "查看", "打开", "修改", "修复", "创建",
                "保存", "运行", "测试", "验证", "天气", "下载", "安装", "分析文件", "删除", "读",
            ))
            return json.dumps({
                "disposition": "answer" if direct_answer else "execute",
                "objective": request[:1200] or "完成用户请求",
                "completion_criteria": [request[:300]] if request else [],
                "context_requirements": [],
                "questions": [],
            }, ensure_ascii=False)
        if name == "FORGE 工具候选筛选":
            # Runtime workflow tests exercise execution policy and evidence,
            # not selector quality. Return the registered catalog as the
            # candidate set so a new LangGraph stage never consumes a test's
            # scripted execution response or accidentally hides its tool.
            try:
                catalog = json.loads(message).get("registered_tools") or []
            except (TypeError, ValueError):
                catalog = []
            names = [str(row.get("name") or "")[:120]
                     for row in catalog if isinstance(row, dict) and row.get("name")]
            return json.dumps({
                "tool_names": names,
                "rationale": "测试桩保留注册工具候选，Runtime 仍负责授权与执行门。",
            }, ensure_ascii=False)
        if name == "FORGE 结果复核":
            return json.dumps({
                "verdict": "complete", "gaps": [], "supplement_prompt": "",
                "supplement_requires_tools": False, "questions": [],
                "reason": "测试桩：Runtime 确定性完成门已通过。",
            }, ensure_ascii=False)

        # Keep older fakes usable even if they predate newer optional adapter
        # parameters such as provider.
        try:
            signature = inspect.signature(execute_fake)
            parameters = signature.parameters
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD
                                 for p in parameters.values())
            if not accepts_kwargs:
                kwargs = {key: value for key, value in kwargs.items()
                          if key in parameters}
        except (TypeError, ValueError):
            pass
        return await execute_fake(mode, message, *args, **kwargs)

    return _wrapped
