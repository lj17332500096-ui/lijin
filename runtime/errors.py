"""统一异常层级（Agent Runtime 基础）。

设计目标：不同模块不再各用各的错误风格——先立骨架，后续各层逐步迁移到这套异常。
"""


try:  # 让“终止 Agent Loop”的异常能被 agents SDK 识别并原样上抛
    from agents.exceptions import AgentsException as _SDKAgentsException
except Exception:  # pragma: no cover - 无 SDK 环境下的回退
    _SDKAgentsException = RuntimeError


class AgentError(Exception):
    """Agent 域基础异常。"""


class RuntimeError(AgentError):
    """运行时错误。"""


class BudgetExceeded(RuntimeError):
    pass


class FinalResponseFailed(RuntimeError):
    """最终回复失败（空输出 / 输出闸拦截后重试仍失败）。

    与「执行失败」是两回事：执行（工具调用/文件修改/验证）可能已经真实成功，
    只是最后一轮 Assistant 文本无法生成或未通过输出安全闸。
    消息文本 = 面向用户的友好原因（不含 SDK 内部报错）。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason or "最终回复生成失败")
        self.reason = reason or "最终回复生成失败"


class TaskCancelled(RuntimeError):
    pass


class ToolError(AgentError):
    pass


class ToolNotFound(ToolError):
    pass


class ToolTimeout(ToolError):
    pass


class PolicyDenied(AgentError):
    pass


class ApprovalRequired(AgentError):
    pass


class ModelError(AgentError):
    pass


class ConvergenceTerminated(_SDKAgentsException):
    """收敛终止：Run 已进入 TERMINALIZE，正常 Agent Loop 必须立即结束。

    在 tool wrapper 中抛出，用于**真正 break** SDK Runner 的工具循环（而不是
    仅返回一段“请结束”的文本让模型继续调用工具）。继承 agents 的 AgentsException，
    SDK 工具执行层会原样上抛而不是包成 tool error 喂回模型。
    由 run_turn 捕获后进入有限的 bounded terminalization。
    """

    def __init__(self, level: str = "TERMINALIZE") -> None:
        super().__init__(f"convergence terminated: {level}")
        self.level = level


class CompletionReadyTerminated(_SDKAgentsException):
    """任务已完成（verification 通过且无未决 blocker），模型仍继续纯探索。

    与 ConvergenceTerminated 不同：终态应为 **completed**（任务确实完成），
    而不是 failed/no_progress。由 run_turn 捕获后用现有收口路径收尾。
    """

    def __init__(self, reason: str = "completion_ready") -> None:
        super().__init__(reason)
        self.reason = reason


class NeedsUserInputTerminated(_SDKAgentsException):
    """缺必需信息终止：needs_user_input 已确定，模型仍继续调用工具。

    与 ConvergenceTerminated 同理，用于**真正 break** SDK 工具循环，避免模型
    在被拦后空转到 max_turns（或产出非法 tool call）。由 run_turn 捕获后直接把
    pending_questions 收口为 questions → WAITING_USER。
    """

    def __init__(self, questions: list[str] | None = None) -> None:
        super().__init__("needs_user_input terminated")
        self.questions = list(questions or [])
