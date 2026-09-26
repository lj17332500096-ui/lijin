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
    """预算超限：token_budget（成本闸门）或 wall_clock（墙钟预算）。

    携带 reason 属性供 classify_exception 区分终态类型：
    - reason="token_budget" → KIND_TOKEN_BUDGET
    - 其它 reason          → KIND_TIMEOUT
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason or "预算超限")
        self.reason = reason or ""


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


class ProcessExitInterrupted(RuntimeError):
    """进程级退出信号（SystemExit / KeyboardInterrupt）被拦截并转成的普通运行时错误。

    为什么需要这层转换（P0-6b / A4，2026-09-22）：asyncio 的 ``Task.__step`` 对
    ``(KeyboardInterrupt, SystemExit)`` 有**特例** —— 它会把异常 ``raise`` 出
    ``run_forever``，于是最外层 ``await`` 永不恢复、协程 ``finally`` 整段跳过，
    Run 永久停在 ``running``，只能等进程重启后的 auto_recover 收尸。

    转换本身只解决「状态收不回」的问题，收口后是否把原异常重新抛出由
    ``must_reraise`` 决定（两种异常的对外语义不同，见该属性的说明）。
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(
            f"执行被进程级退出信号中断（{type(original).__name__}）：{original}"
        )
        self.original = original
        self.original_type = type(original).__name__
        self.exit_code = getattr(original, "code", None)

    @property
    def must_reraise(self) -> bool:
        """清理收口完成后是否应把 ``original`` 重新抛出。

        - ``SystemExit`` → **不重抛**。工具/沙箱里的 ``sys.exit()`` 不是「本进程想退出」
          的合法表达，反而是既有事故源：``runner._close_run`` 的 docstring 记录过
          「清理路径抛出的 SystemExit 让 uvicorn 跑一段时间后突然退出」。
          按 failed 收口即可，进程继续服务。
        - ``KeyboardInterrupt`` → **重抛**。Ctrl-C 是用户意图，必须穿透到
          ``main.py`` / ``cli`` 的顶层处理器（与 ``provider_gateway`` 的
          「KeyboardInterrupt 不吞，保证 Ctrl+C 语义不受影响」一致）。
          只是在它穿透之前，先把 Run 的终态写完 —— 否则一次 Ctrl-C 就会留下一个
          永远停在 running 的僵尸 Run。
        """
        return isinstance(self.original, KeyboardInterrupt)


class ToolError(AgentError):
    pass


class ToolNotFound(ToolError):
    pass


class ToolTimeout(ToolError):
    pass


class PolicyDenied(AgentError):
    pass


class ApprovalRequired(AgentError, _SDKAgentsException):
    """审批门挂起：高风险工具需用户确认。

    继承 AgentError（用于 run_turn 顶层捕获），
    同时继承 _SDKAgentsException（让 agents SDK 工具执行层原样上抛，
    而非包装成 tool error 字符串喂回模型 → 防止模型收到 BLOCK 文本后
    换参数重试再触发新审批，造成频繁弹审批的循环）。
    """


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
