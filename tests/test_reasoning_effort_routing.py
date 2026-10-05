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


# ---------------------------------------------------------------------------
# 2026-05 追加：把「默认关闭」这个安全前提锁成断言
#
# `reasoning_effort_for_scene` 是**关键词表做语义判断**（出现 "bug"/"python"
# 就判为编码场景），违反 AGENTS.md 分工宪法。实测误命中很高：
#     "介绍一下 Python 的历史" / "这个 bug 是什么意思" / "Debian bug 是什么"
# 三条概念性提问都被错判成编码场景。
#
# 因此它**必须保持关闭**，直到场景判断改由 LLM 承担。本文件锁住这个前提：
# 一旦有人顺手把默认打开（或把它接进主干决策），立刻报红。
# ---------------------------------------------------------------------------


def test_default_is_off_and_shipped_config_does_not_enable_it():
    """默认关闭，且仓库里不得有把它打开的配置。

    这是上面那条「不要顺手打开」警告的可执行版本。
    """
    from runtime.router import reasoning_effort_enabled

    # 1) 未设置时必须关闭
    assert reasoning_effort_enabled() is False, "未设置 FORGE_REASONING_EFFORT 时必须关闭"

    # 2) 仓库内不得有启用它的配置（.env 不入库，故只查 .env.example）
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    if example.is_file():
        for line in example.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("FORGE_REASONING_EFFORT"):
                assert stripped.split("=", 1)[1].strip().lower() in (
                    "", "off", "0", "false",
                ), f".env.example 启用了关键词路由：{stripped!r}"


def test_known_false_positives_are_documented_not_silently_accepted():
    """把实测的误命中钉成用例，避免后人以为它工作正常。

    这些**当前确实会误判**（默认关闭所以无害）。本测试锁住现状，
    将来若改成 LLM 判定，应改写本测试而不是让它悄悄通过。
    """
    from runtime.router import reasoning_effort_for_scene

    # 概念性提问里含关键词，会被判成编码场景 —— 这是已知缺陷，不是期望行为
    for text in ("介绍一下 Python 的历史", "这个 bug 是什么意思", "Debian bug 是什么"):
        got = reasoning_effort_for_scene(text)
        assert got == "low", (
            f"{text!r} 现在判为 {got}。若这里变成了 medium，说明判定方式已改"
            f"（比如换成 LLM 意图标签）—— 请同步改写本测试的说明与"
            f" runtime/router.py 里那条『不要顺手打开』的警告。"
        )
