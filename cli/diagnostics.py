"""把一轮运行抛出的异常翻译成「诊断码 + 人话 + 下一步怎么做」。

为什么需要它
------------
`main.print_run_error` 已经能把错误说成人话，但排查时还需要一个**可检索的稳定标识**：
看日志、贴报错、写回归用例都需要一句话认出「这是哪类问题」。所以这里在第一句话前面
加一个诊断码（如 `E-PROVIDER`、`E-MAXTURNS`），把细节与建议结构化。

诊断码是**稳定契约**，测试会断言它们；`detail` 与 `hints` 文案可以随时改。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Diagnosis:
    """一条诊断结论。"""

    code: str
    title: str
    detail: str = ""
    hints: tuple[str, ...] = ()
    recovered: bool = False  # True = 已自动恢复/可继续，不必当成故障

    def head(self) -> str:
        return f"[{self.code}] {self.title}"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "detail": self.detail,
            "hints": list(self.hints),
            "recovered": self.recovered,
        }


_BUSY_PATTERN = re.compile(r"已有正在处理的 Run|只允许一个活跃任务")
_CONFIG_PATTERN = re.compile(r"(?i)api[_ -]?key|OPENAI_BASE_URL|未配置|missing.*key")
_TIMEOUT_PATTERN = re.compile(r"(?i)timeout|timed?\s?out|超时")


def _provider_detail(exc: BaseException) -> str:
    """复用运行时的对外文案（已做脱敏），失败就退回空串。"""
    try:
        from runtime.provider_errors import mask_request_id
        from runtime.provider_gateway import provider_public_message

        public = provider_public_message(exc)
        if not public:
            return ""
        kind = getattr(exc, "provider_kind", None)
        request_id = getattr(exc, "provider_request_id", None)
        parts = [str(public)]
        if kind:
            parts.append(f"类型={kind}")
        masked = mask_request_id(request_id) if request_id else ""
        if masked:
            parts.append(f"request={masked}")
        return "；".join(parts)
    except Exception:
        return ""


def diagnose(exc: BaseException) -> Diagnosis:
    """给异常定性。未知异常也一定返回一个带码的结论（绝不返回 None）。"""
    message = str(exc) or type(exc).__name__

    # 1) 安全闸（最需要一眼认出，避免被误当成代码 bug）
    try:
        from agents import InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered
        from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError

        if isinstance(exc, InputGuardrailTripwireTriggered):
            return Diagnosis(
                "E-INPUT-GUARD", "输入被安全闸拦截（越狱/套取系统提示词/索取密钥）",
                detail=message[:400],
                hints=(
                    "换个正常说法重发；这是拦截，不是故障。",
                    "若确属误判，可用 python main.py --no-input-guardrail 临时停用输入闸（仅本次运行）。",
                ),
            )
        if isinstance(exc, OutputGuardrailTripwireTriggered):
            reason = getattr(exc, "reason", "") or message
            return Diagnosis(
                "E-OUTPUT-GUARD", "输出被安全闸拦截（结构不合规/疑似泄露密钥）",
                detail=str(reason)[:400],
                hints=(
                    "常见原因是模型把密钥写进了正文，或没按 AgentReply JSON 输出。",
                    "重发一次通常即可；反复出现请带 --debug 并查看 logs/guardrail_failures.jsonl。",
                ),
            )
        if isinstance(exc, MaxTurnsExceeded):
            return Diagnosis(
                "E-MAXTURNS", "单轮循环上限用尽（LLM 与工具来回次数过多）",
                detail=message[:400],
                hints=(
                    "用更大的上限重跑：python main.py --max-turns 30",
                    "若反复触发，通常是模型在重复调用同一个工具，把任务拆小或要求它「拿到结果就作答」。",
                ),
            )
        if isinstance(exc, ModelBehaviorError) and "not found in agent" in message:
            match = re.search(r"Tool (.+?) not found in agent", message)
            tool_name = match.group(1) if match else "未知工具"
            return Diagnosis(
                "E-TOOL-HALLUCINATION", f"模型幻觉调用了不存在的工具「{tool_name}」",
                detail=message[:400],
                recovered=True,
                hints=("现网已改为把该错误回传给模型自行纠正，通常不会中断整轮；重发即可。",),
            )
    except ImportError:  # pragma: no cover - 无 SDK 环境
        pass

    # 2) Runtime 自有的错误层级
    detail = _provider_detail(exc)
    if detail:
        return Diagnosis(
            "E-PROVIDER", "模型服务调用失败（网关/鉴权/限流/网络）",
            detail=detail,
            hints=(
                "检查 .env 里的 OPENAI_API_KEY / OPENAI_BASE_URL 与网关是否可达。",
                "用 /diag 看本轮 provider_attempts，确认是第几次尝试、什么错误。",
            ),
        )

    try:
        from runtime.errors import (
            ApprovalRequired,
            BudgetExceeded,
            FinalResponseFailed,
            PolicyDenied,
            TaskCancelled,
            ToolNotFound,
            ToolTimeout,
        )

        if isinstance(exc, FinalResponseFailed):
            return Diagnosis(
                "E-FINAL-EMPTY", "最终回复生成失败（执行可能已成功，只是正文出不来）",
                detail=str(getattr(exc, "reason", "") or message)[:400],
                hints=(
                    "先确认任务是否已真的完成（文件/命令结果为准），不要重复跑一遍副作用。",
                    "重发一次通常即可；持续失败按模型服务问题处理（/diag 看 provider 尝试）。",
                ),
            )
        if isinstance(exc, BudgetExceeded):
            return Diagnosis(
                "E-BUDGET", "本轮预算超限（tokens/时长为上限所限）",
                detail=message[:400],
                hints=("把任务拆小，或放宽 FORGE_* 预算相关环境变量后重试。",),
            )
        if isinstance(exc, (ApprovalRequired, PolicyDenied)):
            return Diagnosis(
                "E-APPROVAL", "需要人工审批 / 策略拒绝",
                detail=message[:400],
                hints=("按提示确认审批；用 /diag 可看待审批明细与所在 Run。",),
            )
        if isinstance(exc, TaskCancelled):
            return Diagnosis(
                "E-CANCELLED", "任务已被取消", detail=message[:400],
                hints=("重新发起同样的请求即可（会新建一个 Run）。",),
            )
        if isinstance(exc, ToolTimeout):
            return Diagnosis(
                "E-TOOL-TIMEOUT", "工具执行超时", detail=message[:400],
                hints=("确认外部依赖（网络/子进程）是否可用，再重试。",),
            )
        if isinstance(exc, ToolNotFound):
            return Diagnosis(
                "E-TOOL-NOTFOUND", "调用了未注册的工具", detail=message[:400],
                hints=("检查工具注册（tools.py / skills）是否被改动。",),
            )
    except ImportError:  # pragma: no cover
        pass

    # 3) 靠文本特征兜底（顺序敏感：先识别人为语义，再识别环境问题）
    if _BUSY_PATTERN.search(message):
        return Diagnosis(
            "E-BUSY", "同一会话已有正在处理的任务（一容器一活跃 Run）",
            detail=message[:400],
            hints=(
                "用 /diag 看活跃 Run 的 id，等它结束、取消它，或 /new 开一个新会话再发。",
            ),
        )
    if _TIMEOUT_PATTERN.search(message):
        return Diagnosis("E-TIMEOUT", "操作超时", detail=message[:400],
                         hints=("确认网络与模型网关状态；必要时加大超时环境变量后重试。",))
    if _CONFIG_PATTERN.search(message):
        return Diagnosis("E-CONFIG", "配置缺失（API Key / 网关地址）", detail=message[:400],
                         hints=("复制 .env.example 为 .env，填好 OPENAI_API_KEY 与 OPENAI_BASE_URL。",))
    return Diagnosis(
        "E-UNKNOWN", "未归类的运行错误",
        detail=message[:600],
        hints=("用 /diag 看本轮事件与 provider 尝试；必要时带 --debug 重跑复现。",),
    )
