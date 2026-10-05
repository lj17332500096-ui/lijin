"""工具错误文案：只放行**刻意抛给用户看的边界错误**，其余交给 SDK 兜底。

## 为什么需要它（10-05 CI run 37350998914实据）

`tests/test_rag.py::KeywordSearchTests::test_outside_workspace_rejected` 在 CI 上红：

    AssertionError: '只能索引工作区' not found in
                     'An error occurred while running the tool. Please try again.'

**真因是 SDK 版本漂移，不是代码缺陷**：
- 本地 venv：`openai-agents==0.22.0`，其
  `default_tool_error_function` 返回
  `f"An error occurred while running the tool. Please try again. Error: {str(error)}"`
  ⇒ 带 `Error: <原因>` 后缀。
- CI：`requirements.txt` 写的是 **`openai-agents>=0.22.0`（开放上界）**
  ⇒ 每次 `pip install` 装到**当时最新版**。新版出于安全加固
  （不把原始异常泄露给模型）**已删除该后缀**，官方文档原文：

      'Return a fixed error response without exposing the exception to the model.'
      return "An error occurred while running the tool. Please try again."

  ⇒ 断言依赖的 detail **在新版里根本不存在**。

**这不是「测试写错了」，也不是「把断言迁就新版」**。业务上，
「只能索引工作区 X 内的目录」是**必须让模型看到**的边界说明 ——
模型据此知道该换哪个目录再试。SDK 默认把它藏起来是**信息损失**。
官方为此提供 `failure_error_function` 让应用自行决定暴露什么。

## 放行边界（有意收窄，不是「全部放行」）

实测 `grep "raise "` 于 `tools.py` / `rag.py` / `code_exec.py` /
`project_edit.py`（共 26 个 `@function_tool`）只有两类：

| 异常 | 处数 | 处置 | 理由 |
|---|---|---|---|
| `ValueError` | 6 | **放行** | 刻意抛给用户的边界说明（如「只能索引工作区 X 内的目录」），模型据此纠正行为 |
| `EmbeddingEngineError` | 7 | **不放行** | 内部实现细节（模型目录缺失、onnxruntime 未装等），对模型无行动价值，且可能泄露本机路径 |
| 其他一切 | — | **不放行** | 走SDK 默认兜底 |

⚠️ **不放行不等于吞掉**：`failure_error_function` 返回的是**给模型的字符串**，
原始异常照常按 SDK 语义处理（`default_tool_error_function` 本身就是这个用法）。
护栏 `tests/test_tool_error_visibility.py` 用**双向验收**钉住这条边界：
既断言业务错误**可见**，也断言内部错误**不可见**（防「为了修一条红把什么都放行」）。
"""

from __future__ import annotations

from typing import Any

#: 唯一放行的异常类型前缀 —— 刻意收窄，见模块 docstring 的处置表。
_ALLOWED_PREFIXES = ("ValueError", "ToolError")

#: 兜底前缀（与 SDK `default_tool_error_function` 的文案对齐）。
#: 改这里前先想清楚：模型看到这句就知道「重试没用，得换策略」。
_FALLBACK = "An error occurred while running the tool. Please try again."


def is_user_facing_error(error: BaseException) -> bool:
    """判断该异常是否是**刻意抛给用户/模型看的边界错误**。

    单独抽成函数是为了让护栏能直接测它（测判定逻辑比测 SDK 集成稳定得多，
    且不依赖 SDK 版本的具体行为）。
    """
    if isinstance(error, KeyboardInterrupt):
        # 中断不是业务边界错误，绝不能被当成可展示信息吞掉
        return False
    for cls in type(error).__mro__:
        name = cls.__name__
        if name in ("ValueError", "ToolError"):
            return True
        # 子类化检查：只认我们自己定义的 AgentError 家族
        if name in _ALLOWED_PREFIXES:
            return True
    return False


def tool_failure_error_function(ctx: Any, error: Exception) -> str:
    """`@function_tool(failure_error_function=...)` 用的文案函数。

    - 业务边界错误 ⇒ 带上原因，让模型能纠正行为。
    - 其他 ⇒ **完全照抄 SDK 默认文案**，不新增任何暴露面。
      （不是自己写近似文案：SDK 若将来再改文案，这里要能自动跟随。）
    """
    if is_user_facing_error(error):
        detail = str(error).strip()
        if detail:
            return f"{_FALLBACK} Error: {detail}"
    return _FALLBACK
