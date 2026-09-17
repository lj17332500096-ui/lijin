"""最终回答边界 / 泄露检测（防御性，非字符串删除）。

验证 Completion Gate 修复：repair_prompt（输出通道）必须把内部 observation 转成
【面向用户的重新生成指令】，绝不把“系统提示：…请撤回完成声明 / 无执行证据 / 运行时校验”等
内部文案拼进面向模型的用户消息，避免模型复述成“系统提示让我……”。
"""
from runtime.completion import (
    GateVerdict,
    repair_prompt,
    short_user_reason,
    user_feedback_text,
)

# 禁止出现在用户可见 / 面向模型重生成指令 中的内部术语
FORBIDDEN = [
    "系统提示", "运行时校验", "撤回完成声明", "撤回‘等待审批’", "执行证据",
    "Run 没有", "没有检测到", "Completion Gate", "CLAIM_UNSUPPORTED", "ExecutionEvidence",
    "验证未通过", "退出码非 0", "readiness", "policy", "gate", "runtime",
]

def _no_forbidden(text: str) -> list[str]:
    return [w for w in FORBIDDEN if w in text]


def test_repair_prompt_never_injects_gate_wording():
    for verdict in [
        GateVerdict.CLAIM_UNSUPPORTED,
        GateVerdict.APPROVAL_INCONSISTENT,
        GateVerdict.VERIFICATION_FAILED,
        GateVerdict.NO_PROGRESS,
        GateVerdict.CLARIFICATION_VAGUE,
    ]:
        p = repair_prompt("用户原问题", verdict)
        leaks = _no_forbidden(p)
        assert leaks == [], f"repair_prompt({verdict}) 仍含内部术语: {leaks}\n{p}"


def test_repair_prompt_is_user_facing_and_directive():
    p = repair_prompt("帮我写一个文件", GateVerdict.CLAIM_UNSUPPORTED)
    assert p.startswith("帮我写一个文件")
    # 用户能懂的短事实 + 明确的“直接说还需要什么 + 勿提内部机制”
    assert ("还需要什么" in p) or ("未真正完成" in p) or ("尚未" in p)
    assert "普通、友好" in p or "普通用户" in p
    assert "不要提及" in p and ("后台" in p or "校验" in p)


def test_short_user_reason_is_jargon_free():
    for verdict in [
        GateVerdict.CLAIM_UNSUPPORTED,
        GateVerdict.APPROVAL_INCONSISTENT,
        GateVerdict.VERIFICATION_FAILED,
        GateVerdict.NO_PROGRESS,
        GateVerdict.CLARIFICATION_VAGUE,
    ]:
        r = short_user_reason(verdict)
        assert _no_forbidden(r) == [], f"short_user_reason({verdict}) 含术语: {r}"
        assert len(r) < 60


def test_user_feedback_text_is_jargon_free():
    # 失败卡 / run.error_message 用的人话文案，不得含 Gate/Runtime 术语
    for verdict in [
        GateVerdict.CLAIM_UNSUPPORTED,
        GateVerdict.APPROVAL_INCONSISTENT,
        GateVerdict.VERIFICATION_FAILED,
        GateVerdict.NO_PROGRESS,
        GateVerdict.CLARIFICATION_VAGUE,
    ]:
        t = user_feedback_text(verdict)
        assert _no_forbidden(t) == [], f"user_feedback_text({verdict}) 含术语: {t}"
        assert any(k in t for k in ["需要", "请", "告诉我", "继续"]), t


def test_hypothetical_clean_final_answer():
    # 最终回答应该是什么样（模拟）——不含任何内部术语
    clean = ("要继续搭建评测集，我还需要：\n• 主要评测对象\n• 最关心的核心场景\n• 主要边界案例")
    assert _no_forbidden(clean) == []
