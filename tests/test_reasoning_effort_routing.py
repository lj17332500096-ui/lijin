"""P1-B(3)：reasoning_effort 场景路由单测。

验证 runtime/router.py 的 reasoning_effort_for_scene / reasoning_effort_enabled
纯函数，以及该网关（apihub.agnes-ai.cn）错位语义下 coding→low 的标定。
开关默认 off（不影响生产），显式 FORGE_REASONING_EFFORT=on 才注入。
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FORGE_REASONING_EFFORT", raising=False)
    yield


def test_scene_coding_returns_low():
    """coding 场景关键词 → low（该网关错位语义下=最深推理）。"""
    from runtime.router import reasoning_effort_for_scene

    coding_prompts = [
        "帮我修复这个 Python bug：def foo(): ...",
        "实现一个单元测试 test_auth.py",
        "写一个快速排序的函数",
        "调试这个 TypeError 异常",
        "run_tests 失败了，看看 pytest 输出",
    ]
    for p in coding_prompts:
        assert reasoning_effort_for_scene(p) == "low", p


def test_scene_non_coding_returns_medium():
    """非 coding 场景 → medium（默认档）。"""
    from runtime.router import reasoning_effort_for_scene

    plain_prompts = [
        "北京天气怎么样？",
        "总结一下昨天的会议纪要",
        "这个产品叫什么名字？",
        "",
        None,
    ]
    for p in plain_prompts:
        assert reasoning_effort_for_scene(p) == "medium", p


def test_scene_empty_and_none_safe():
    """空 / None 输入不抛异常，返回默认档。"""
    from runtime.router import reasoning_effort_for_scene

    assert reasoning_effort_for_scene("") == "medium"
    assert reasoning_effort_for_scene(None) == "medium"


def test_enabled_default_off():
    """FORGE_REASONING_EFFORT 未设 → 默认 off（不影响生产）。"""
    from runtime.router import reasoning_effort_enabled

    assert reasoning_effort_enabled() is False


def test_enabled_on_when_set(monkeypatch):
    """显式 on/1/true → 启用。"""
    from runtime.router import reasoning_effort_enabled

    for v in ("on", "1", "true", "ON", "True"):
        monkeypatch.setenv("FORGE_REASONING_EFFORT", v)
        assert reasoning_effort_enabled() is True, v
    monkeypatch.delenv("FORGE_REASONING_EFFORT", raising=False)
    for v in ("off", "0", "false", ""):
        monkeypatch.setenv("FORGE_REASONING_EFFORT", v)
        assert reasoning_effort_enabled() is False, v


def test_model_settings_reasoning_constructs():
    """ModelSettings(reasoning={'effort':'low'}) 可构造（SDK 转 Reasoning）。"""
    from agents import ModelSettings

    s = ModelSettings(max_tokens=4096, reasoning={"effort": "low"})
    assert s.reasoning is not None
    assert s.reasoning.effort == "low"
    # 非 coding 场景走 medium
    s2 = ModelSettings(max_tokens=4096, reasoning={"effort": "medium"})
    assert s2.reasoning.effort == "medium"
