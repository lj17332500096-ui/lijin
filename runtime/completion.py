"""Completion Gate：把 run 的终态从「模型最后说了什么」升级为
「模型声明 + Runtime 实际执行证据 + 必要的 Approval / Verification 状态」共同决定。

原则（P0 阶段第一版，刻意保持确定性、无第二个 LLM 裁判）：
- 模型可以「提出完成」，但不能自己「决定 completed」；
- 普通问答 / questions / plan 属于会话型输出，允许 0 工具直接完成，不误伤聊天；
- 当回复存在「执行型完成声明」（已修改/已修复/已保存/已生成/测试通过…）时，
  必须存在对应的真实 Execution Evidence，否则不得按完成收尾；
- 当回复表达「等待批准/需要审批」时，必须存在真实的 pending approval，
  否则视为 Reply 与 Runtime 状态不一致（口头 Approval 绕过）。

Evidence 只来自 Runtime 事实：真实执行的工具调用（本 Run 内）、
产物目录新文件（ArtifactTracker diff）、ApprovalStore pending 记录。
禁止只用自然语言判完成。
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.readiness_gate import clarification_precision


class GateVerdict(str, Enum):
    """Completion Gate 判定结果。"""

    PASS = "pass"
    """会话型输出（无执行声明）或执行声明已被真实证据支撑 → 允许完成。"""

    CLAIM_UNSUPPORTED = "claim_unsupported"
    """存在执行型完成声明，但缺少相应 Execution Evidence → 不得 completed。"""

    APPROVAL_INCONSISTENT = "approval_inconsistent"
    """回复表达等待/需要批准，但 ApprovalStore 没有 pending → 不得作为完成收尾。"""

    VERIFICATION_FAILED = "verification_failed"
    """任务要求验证通过（用户明确意图）但最近验证结果失败 → 不得按成功完成收尾。"""

    NO_PROGRESS = "no_progress"
    """模型空转/无进展（无新执行证据、或重复完全相同调用）→ 触发 bounded recovery。"""

    CLARIFICATION_VAGUE = "clarification_vague"
    """kind=questions 但只写“需要补充信息/信息不足”等空泛措辞，
    未点名缺失字段/无具体问句 → 触发一次精度修复。"""

    FACT_UNSUPPORTED = "fact_unsupported"
    """回复包含具体外部事实（温度/价格/股价/比分/新闻/职位等），
    但本轮没有任何检索/查询工具证据支撑 → 不得作为完成收尾（撤回或说明限制）。"""


#: 能力盘点类问题（“有哪些 MCP/能力/工具/技能”）——答案属于“描述当前 Runtime
#: 事实”（Capability Introspection 结果），不是“本轮已执行某任务”的完成声明。
_CAPABILITY_QUERY_RE = [
    re.compile(r"mcp|技能|能力|功能|插件|工具(?:列表|清单)?|能用|可用|会用|你会|"
               r"能做|能做什么|有哪些|capabil|tools?|扩展|已连接|"
               r"能(?:不能|否)?处理|可以处理|支持|会不会|能不能|能否|是否可以|"
               r"可以帮我做|你能|你可以|can you|还能|会做|擅长|"
               r"是不是(?:所有|什么|都)|所有事情|都(?:可以|能)|替我完成|直接替", re.IGNORECASE),
]


def is_capability_query(request_text: str | None) -> bool:
    """0 工具的能力回答只有在“用户问能力/MCP”语境下才属于 Runtime 事实描述。"""
    if not request_text:
        return False
    text = (request_text or "").lower()
    # 需要出现问句意图/盘点词，避免“帮我用 MCP 下载字幕”这类执行请求被误放行
    if not any(p.search(text) for p in _CAPABILITY_QUERY_RE):
        return False
    return bool(re.search(r"有哪些|有什么|能用|可用|你会|能做|能做什么|列出|"
                          r"盘点|现在|当前|连接|安装了|介绍一下|能不能|能否|"
                          r"可以处理|支持|会做|擅长|是不是|所有事情|都(?:可以|能)|"
                          r"替我完成|你(?:能|可以|会|也是不是)|what|cannot|capab|can you",
                          text, re.IGNORECASE))


#: 第一人称直接完成声明（能力回答语境下仍必须拦截；0 工具不得放行）
_DIRECT_EXEC_CLAIM_RE = [
    re.compile(r"已(?:经|成功|刚刚|正在)?(?:成功|刚刚|正在)?(?:调用|执行|运行|下载|保存|生成|"
               r"修改|写入|更新|创建|发送|上传|删除|预订|购买|提交|修复|抓取|导出)"
               r"(?:了|过|到)?", re.IGNORECASE),
    re.compile(r"\b(?:i\s+(?:have|'ve|already|successfully|just)\s+|"
               r"already\s+successfully\s+|successfully\s+)"
               r"(?:called|ran|downloaded|saved|generated|modified|wrote|written|updated|"
               r"created|sent|uploaded|deleted|booked|submitted|fixed|exported)\b", re.IGNORECASE),
]


def has_direct_exec_claim(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _DIRECT_EXEC_CLAIM_RE)


# ---------------------------------------------------------------------------
# External Fact Claim（外部具体事实：需要检索/查询证据，而非能力/一般性回答）
# ---------------------------------------------------------------------------

#: 具体外部事实模式：需要工具证据支撑，0 证据时不得直接当作完成。
#: 保守：只命中"具体数值/单位/比分"等可被检索核实的事实，避免误伤能力/一般性描述。
_EXTERNAL_FACT_RE = [
    # 温度/气温：数字 + 温度单位
    re.compile(r"[0-9]{1,3}\s*(?:°|度|℃|°C|摄氏度)(?:[Cc]|以上|左右)?"),
    re.compile(r"(?:气温|温度|天气).{0,6}[0-9]{1,3}\s*(?:°|度)"),
    # 价格 / 股价 / 汇率（数字+货币/涨跌）
    re.compile(r"[0-9.,]+\s*(?:元|块|¥|￥|USD|美元|港元)\b"),
    re.compile(r"(?:股价|涨|跌|上涨|下跌|涨幅|跌幅|汇率).{0,8}[0-9.,]+"),
    # 比赛比分（数字:数字）
    re.compile(r"[0-9]{1,2}\s*[-:：]\s*[0-9]{1,2}(?![0-9])"),
    # 具体日期 + 事件（新闻类）
    re.compile(r"(?:[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日|昨天|今天|当地时间).{0,12}(?:举行|发生|发布|宣布|开幕|召开|夺冠|当选|任命)"),
]


def has_external_fact_claim(text: str) -> bool:
    """是否包含可被检索核实的"具体外部事实"（非能力/一般性描述）。"""
    if not text:
        return False
    return any(p.search(text) for p in _EXTERNAL_FACT_RE)


_QUESTION_MARK_RE = re.compile(r"[?？]")


#: 用户消息里出现的“验证必须成功”意图（供 VERIFICATION_FAILED 判定，见 §8）
_VERIFY_INTENT_RE = [
    re.compile(r"确保.{0,6}(?:测试|验证).{0,4}(?:通过|成功)"),
    re.compile(r"修复并验证|修复后.{0,6}(?:验证|测试)|验证修复"),
    re.compile(r"让.{0,4}(?:测试|验证).{0,4}(?:通过|成功)"),
    re.compile(r"改到.{0,4}(?:通过|成功)|确保.{0,6}通过"),
    re.compile(r"(?:fix|make).{0,12}(?:pass|green)|ensure.{0,10}(?:test|check).{0,6}(?:pass|ok)", re.IGNORECASE),
]


def has_verify_intent(request_text: str | None) -> bool:
    return bool(request_text) and any(p.search(request_text) for p in _VERIFY_INTENT_RE)


# ---------------------------------------------------------------------------
# 执行证据定义
# ---------------------------------------------------------------------------

#: 写/改/生成类工具（真实修改了文件系统或产出了文件）
WRITE_TOOLS = {
    "write_project_file",
    "edit_project_file",
    "write_code_file",
    "save_note",
    "save_word_doc",
    "save_excel_workbook",
    "save_ppt_deck",
}

#: 运行/验证类工具（能产生验证结果的真实执行）
VERIFY_TOOLS = {"run_tests", "run_python", "code_loop"}

#: Mutation 类工具（会修改 workspace）
MUTATION_TOOLS = {
    "write_project_file", "edit_project_file", "write_code_file",
    "save_note", "save_word_doc", "save_excel_workbook", "save_ppt_deck",
    "sandbox_rollback", "code_loop",
}

# Mutation outcome 判定标记（与 runner._MUTATION_* 一致，用于 ExecutionEvidence 层解析）
_MUTATION_FAIL_MARKERS = (
    "错误", "没有找到", "未找到", "失败", "not found",
    "error:", "exception", "未执行", "skipped", "noop",
    "nothing to replace", "no matching", "no changes",
    "permission", "denied", "blocked", "timeout",
    "❌ 失败",
)
_MUTATION_PASS_MARKERS = (
    "已写入", "已在", "替换", "已保存", "已创建", "已修改",
    "已更新", "已生成", "成功", "succeeded", "saved",
    "written", "created", "modified", "updated",
    "✅ 通过", "覆盖", "回滚",
)



#: 工具调用记录的状态
TOOL_EXECUTED = "executed"
TOOL_BLOCKED = "blocked"
TOOL_ERROR = "error"

# ---------------------------------------------------------------------------
# Verification 结果语义（P1-1）：从“执行过”升级为“结果通过”
# ---------------------------------------------------------------------------

_EXIT_CODE_RE = re.compile(r"退出码\s*[:：]\s*(\d+)")
_PASS_MARK = "✅ 通过"
_FAIL_MARK = "未通过"


def verification_outcome_of(call: dict[str, Any] | None) -> str:
    """解析一次运行/验证类工具的真实结果。

    返回 "success" | "failed" | "unknown"。
    规则（确定性，不做内容理解）：
    - 文本含「未通过」→ failed；
    - 含「✅ 通过」→ success（自动修复循环通过标记）；
    - 解析「退出码: N」：最后一次出现为 0 → success；任一次非 0 → failed；
    - 其余（工具报错/无结果文本）→ unknown（不算可信成功）。
    禁止把“执行过”当作“验证通过”。
    """
    if not call:
        return "unknown"
    if call.get("status") != TOOL_EXECUTED:
        return "unknown"
    text = str(call.get("output_head") or call.get("output") or "")
    if not text:
        return "unknown"
    if _FAIL_MARK in text:
        return "failed"
    if _PASS_MARK in text:
        return "success"
    codes = [int(c) for c in _EXIT_CODE_RE.findall(text)]
    if not codes:
        return "unknown"
    return "success" if codes[-1] == 0 else "failed"


def codeloop_outcome_of(call: dict[str, Any] | None) -> tuple[str, str, bool | None]:
    """Phase 26：解析 code_loop 的结构化双结果。

    返回 (mutation_effect, verification_result, final_verification_after_last_change)。

    mutation_effect = CHANGED | UNCHANGED | UNKNOWN
    verification_result = PASS | FAIL | UNKNOWN
    final_verification_after_last_change = True | False | None（无结构化标签时为 None）

    优先级：
    1. [Phase24-outcome] 结构化标签 → 精确解析
    2. 无结构化标签 → mutation_effect=UNKNOWN，verification_result 从关键词推断
    """
    if not call:
        return ("UNKNOWN", "UNKNOWN", None)
    if call.get("status") != TOOL_EXECUTED:
        return ("UNKNOWN", "UNKNOWN", None)
    text = str(call.get("output_head") or call.get("output") or "")
    if not text:
        return ("UNKNOWN", "UNKNOWN", None)

    import re
    # 优先解析结构化标签
    outcome_match = re.search(
        r'\[Phase24-outcome\]\s*'
        r'workspace_changed=(true|false)\s+'
        r'verification_passed=(true|false)',
        text)
    if outcome_match:
        workspace_changed = outcome_match.group(1) == "true"
        verification_passed = outcome_match.group(2) == "true"
        mutation_effect = "CHANGED" if workspace_changed else "UNCHANGED"
        verification_result = "PASS" if verification_passed else "FAIL"
        # Phase 26：需要 final_verification_after_last_change
        # 保守：如果 workspace_changed=true 且 verification_passed=true，
        #       假设 verification 发生在最后修改之后（code_loop 内部保证）
        final_after_change = verification_passed if workspace_changed else None
        return (mutation_effect, verification_result, final_after_change)

    # 无结构化标签 → mutation_effect = UNKNOWN
    # 但 verification_result 仍可从关键词推断
    if "✅ 通过" in text or "退出码: 0" in text or "退出码：0" in text:
        return ("UNKNOWN", "PASS", None)
    if "❌ 失败" in text or "未通过" in text:
        return ("UNKNOWN", "FAIL", None)
    return ("UNKNOWN", "UNKNOWN", None)


def mutation_outcome_of(call: dict[str, Any] | None) -> str:
    """Phase 26：解析简单原子 mutation 工具的 workspace 变更语义。

    返回 "COMMITTED" | "FAILED" | "UNKNOWN"。
    code_loop 请使用 codeloop_outcome_of()。

    优先级：
    1. 工具名是 code_loop → 返回 UNKNOWN（code_loop 需用 codeloop_outcome_of）
    2. 失败标记命中 → FAILED
    3. 成功标记命中 → COMMITTED
    4. 其余 → UNKNOWN（不得默认 COMMITTED）
    """
    if not call:
        return "UNKNOWN"
    if call.get("status") != TOOL_EXECUTED:
        return "UNKNOWN"
    name = call.get("name", "")
    if name == "code_loop":
        # code_loop 使用双结果语义，此处返回 UNKNOWN
        return "UNKNOWN"
    text = str(call.get("output_head") or call.get("output") or "")
    if not text:
        return "UNKNOWN"
    if any(m in text for m in _MUTATION_FAIL_MARKERS):
        return "FAILED"
    if any(m in text for m in _MUTATION_PASS_MARKERS):
        return "COMMITTED"
    return "UNKNOWN"


#: 用户消息中明确的“验证必须成功”类任务意图（用于 §8 的 Verification 完成条件）
_VERIFY_INTENT_RE = [
    re.compile(r"确保.{0,6}测试.{0,4}通过"),
    re.compile(r"修复并验证|修复后.{0,6}(?:验证|测试)|验证修复"),
    re.compile(r"让.{0,4}(?:测试|验证).{0,4}通过"),
    re.compile(r"改到.{0,4}通过|改到.{0,4}成功"),
    re.compile(r"(?:fix|make).{0,12}(?:pass|green)|ensure.{0,10}(?:test|check).{0,6}(?:pass|ok)", re.IGNORECASE),
]


def has_verify_intent(request_text: str | None) -> bool:
    if not request_text:
        return False
    return any(p.search(request_text) for p in _VERIFY_INTENT_RE)


#: 用户是否要求了 mutation（修复/修改/新增/删除等）
_MUTATION_INTENT_RE = [
    re.compile(r"修复|修改|编辑|重构|实现|新增|添加|补充|更新|替换|写入|删除|移除|"
               r"改(?:一下|成|为|到|好)|修好|fix|modify|edit|refactor|implement|"
               r"add|update|delete|remove|replace", re.IGNORECASE),
]


#: 显式否定/只读语境：出现时不算 mutation 义务
_NO_MUTATION_RE = re.compile(
    r"不要修改|别修改|先不要改|先别改|别改|不要改动|不要动|只读|仅阅读|只看|"
    r"do not modify|read.?only|don't modify", re.IGNORECASE,
)


def has_mutation_intent(request_text: str | None) -> bool:
    if not request_text:
        return False
    if _NO_MUTATION_RE.search(request_text):
        return False
    return any(p.search(request_text) for p in _MUTATION_INTENT_RE)


#: Phase 12：三态义务模型（required / not_required / unknown），避免单关键词误判。
OBLIGATION_REQUIRED = "required"
OBLIGATION_NOT_REQUIRED = "not_required"
OBLIGATION_UNKNOWN = "unknown"

#: 明确“执行验证”的动词语义（跑/运行/执行 + 测试/验证/lint/build）
_VERIFY_ACTION_RE = re.compile(
    # 允许“运行 <路径> 下的测试”这类夹带路径的写法（不跨句末标点）
    r"(?:跑|运行|执行|做|进行|完成|重跑|再跑)[^，。；;\n]{0,40}?"
    r"(?:测试|验证|校验|lint|build|单元测试|集成测试|pytest|unittest)|"
    r"验证(?:一下|修改|修复|结果|是否)|"
    r"(?:run|execute|rerun)\s+(?:the\s+)?(?:test|tests|lint|build)|"
    r"(?:test|tests|lint|build)\s+(?:pass|green|通过)|"
    r"确保.{0,10}(?:测试|验证|校验).{0,6}(?:通过|成功)|"
    r"让.{0,4}(?:测试|验证).{0,4}(?:通过|成功)|"
    r"(?:fix|make).{0,12}(?:pass|green)|"
    r"(?:改|修).{0,6}(?:到|为).{0,4}(?:通过|成功)",
    re.IGNORECASE,
)
#: 验证名词（用于 UNKNOWN 判定；单独出现不自动 REQUIRED）
_VERIFY_NOUN_RE = re.compile(r"测试|验证|校验|lint|build|单元测试|集成测试|pytest|unittest",
                             re.IGNORECASE)
#: 名词/内容语境：验证名词被这些动词支配时，不是“执行验证”
_VERIFY_NOUN_CONTEXT_RE = re.compile(
    r"(?:修改|编辑|阅读|读|解释|说明|查找|找|看|了解|介绍|编写|撰写|写|重构|更新)"
    r"[^，。,\.]{0,8}(?:测试|验证|校验|lint|build|说明|文件|配置|文档)",
    re.IGNORECASE,
)


#: 执行验证的动词（与验证名词同句出现即可，允许中间夹带路径）
_VERIFY_ACTION_VERB_RE = re.compile(
    r"跑|运行|执行|重跑|再跑|跑一下|运行一下|run|execute|rerun",
    re.IGNORECASE,
)


def detect_verification_obligation(request_text: str | None) -> str:
    """三态判定是否需要执行 verification。"""
    text = str(request_text or "").strip()
    if not text:
        return OBLIGATION_UNKNOWN
    if has_verify_intent(text) or _VERIFY_ACTION_RE.search(text):
        return OBLIGATION_REQUIRED
    if _VERIFY_NOUN_RE.search(text):
        # 出现验证名词：若同句有执行动词且不是“修改/阅读/解释…测试”内容语境 → REQUIRED
        if (_VERIFY_ACTION_VERB_RE.search(text)
                and not _VERIFY_NOUN_CONTEXT_RE.search(text)):
            return OBLIGATION_REQUIRED
        if _VERIFY_NOUN_CONTEXT_RE.search(text):
            return OBLIGATION_NOT_REQUIRED
        return OBLIGATION_UNKNOWN
    return OBLIGATION_NOT_REQUIRED


def extract_obligations(request_text: str | None) -> dict[str, str]:
    """极轻量三态义务提取（不建 Planner）。"""
    return {
        "mutation": (OBLIGATION_REQUIRED if has_mutation_intent(request_text)
                     else OBLIGATION_NOT_REQUIRED),
        "verification": detect_verification_obligation(request_text),
    }


@dataclass(slots=True)
class CompletionEligibility:
    """完成资格（来自 Completion Gate 语义，不是新状态）。"""

    eligible: bool
    reasons: dict[str, Any]


def evaluate_completion_eligibility(
    *,
    evidence: Any,
    request_text: str | None,
    mutation_revision: int,
    verified_revision: int | None,
    pending_approval: bool = False,
    pending_user: bool = False,
    unresolved_failure: bool = False,
) -> CompletionEligibility:
    """判断当前执行是否具备收口条件，并给出可解释 reasons。"""
    obligations = extract_obligations(request_text)
    mutation_required = obligations["mutation"] == OBLIGATION_REQUIRED
    verification_required = obligations["verification"] == OBLIGATION_REQUIRED
    reasons: dict[str, Any] = {}
    ok = True

    if mutation_required:
        satisfied = evidence.file_mutation_evidence()
        reasons["mutation_requirement"] = satisfied
        ok = ok and satisfied
    else:
        reasons["mutation_requirement"] = None  # 不要求

    if verification_required:
        ran = evidence.verification_passed()
        latest_verified = bool(ran and verified_revision is not None
                               and verified_revision == mutation_revision)
        reasons["verification_requirement"] = ran
        reasons["latest_revision_verified"] = latest_verified
        ok = ok and latest_verified
    else:
        reasons["verification_requirement"] = None
        reasons["latest_revision_verified"] = None

    reasons["pending_approval"] = pending_approval
    reasons["pending_user"] = pending_user
    reasons["unresolved_blockers"] = unresolved_failure
    if pending_approval or pending_user or unresolved_failure:
        ok = False

    if evidence.executed_count() == 0 and not evidence.new_files:
        reasons["execution_evidence"] = False
        ok = False
    else:
        reasons["execution_evidence"] = True

    return CompletionEligibility(eligible=bool(ok), reasons=reasons)


@dataclass(slots=True)
class ExecutionEvidence:
    """Completion Gate 依赖的最小结构化事实。"""

    #: 本 Run 内每次工具调用的记录：{name, status, output_head}
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    #: 产物目录（notes/exports）快照后新出现的文件（未登记也可用于判定）
    new_files: list[str] = field(default_factory=list)

    #: 真实存在的 pending approval 数量（ApprovalStore）
    approvals_pending: int = 0

    #: 本 Run 是否出现过已被决定的审批（approved/denied）
    approvals_decided: bool = False

    #: 本 Run 真实执行过的持久化写入（save_note/remember），用于支撑“已保存/已记住”声明
    persistence_done: set[str] = field(default_factory=set)

    def persistence_evidence(self) -> bool:
        """是否真实执行过持久化写入（save_note/remember）。"""
        return bool(self.persistence_done)

    def executed(self, *names: str) -> bool:
        return any(
            call.get("name") in names and call.get("status") == TOOL_EXECUTED
            for call in self.tool_calls
        )

    def executed_count(self) -> int:
        return sum(1 for call in self.tool_calls if call.get("status") == TOOL_EXECUTED)

    def retrieval_evidence(self) -> bool:
        """是否真实执行过外部检索/查询工具（支撑外部事实声明）。"""
        return self.executed("web_search", "search_documents", "deep_research",
                             "search_sources", "fetch_github_repo", "web_search_v2")

    def executed_names(self) -> list[str]:
        seen: list[str] = []
        for call in self.tool_calls:
            name = call.get("name", "?")
            if call.get("status") == TOOL_EXECUTED and name not in seen:
                seen.append(name)
        return seen

    def file_mutation_evidence(self) -> bool:
        """是否真实发生过文件写入/修改/生成（工具执行或产物新文件二选一）。"""
        return self.executed(*WRITE_TOOLS) or bool(self.new_files)

    # ---- P1-1: Verification 结果语义 ----

    def verification_ran(self) -> bool:
        """是否执行过运行/验证类工具。"""
        return self.executed(*VERIFY_TOOLS)

    def verification_outcomes(self) -> list[tuple[str, str]]:
        """[(tool_name, success|failed|unknown), ...] 仅含真实执行过的验证工具。"""
        out: list[tuple[str, str]] = []
        for call in self.tool_calls:
            if call.get("name") in VERIFY_TOOLS and call.get("status") == TOOL_EXECUTED:
                out.append((str(call.get("name")), verification_outcome_of(call)))
        return out

    def verification_passed(self) -> bool:
        """存在真实执行且结果解析为 success。"""
        return any(outcome == "success" for _, outcome in self.verification_outcomes())

    def verification_failed(self) -> bool:
        """存在真实执行且结果解析为 failed（exit≠0 / 未通过标记）。"""
        return any(outcome == "failed" for _, outcome in self.verification_outcomes())

    def mutation_outcomes(self) -> list[tuple[str, str]]:
        """Phase 24：[(tool_name, COMMITTED|FAILED|UNKNOWN), ...] 仅含真实执行过的 mutation 工具。"""
        from runtime.completion import MUTATION_TOOLS
        out: list[tuple[str, str]] = []
        for call in self.tool_calls:
            if call.get("name") in MUTATION_TOOLS and call.get("status") == TOOL_EXECUTED:
                out.append((str(call.get("name")), mutation_outcome_of(call)))
        return out

    def mutation_committed_count(self) -> int:
        """Phase 24：workspace 确定发生变更的 mutation 次数。"""
        return sum(1 for _, o in self.mutation_outcomes() if o == "COMMITTED")

    def mutation_any_failed(self) -> bool:
        """Phase 24：是否存在至少一次确定的失败 mutation。"""
        return any(o == "FAILED" for _, o in self.mutation_outcomes())

    def codeloop_outcomes(self) -> list[tuple[str, tuple[str, str, bool | None]]]:
        """Phase 26：code_loop 双结果 [(tool_name, (mutation_effect, verification_result, final_after_change)), ...]。"""
        out: list[tuple[str, tuple[str, str, bool | None]]] = []
        for call in self.tool_calls:
            if call.get("name") == "code_loop" and call.get("status") == TOOL_EXECUTED:
                out.append((str(call.get("name")), codeloop_outcome_of(call)))
        return out

    def codeloop_changed_count(self) -> int:
        """Phase 26：code_loop 实际修改了 workspace 的次数。"""
        return sum(1 for _, (me, _, _) in self.codeloop_outcomes() if me == "CHANGED")

    def codeloop_pass_count(self) -> int:
        """Phase 26：code_loop 验证通过次数。"""
        return sum(1 for _, (_, vr, _) in self.codeloop_outcomes() if vr == "PASS")

    def latest_verification_failed(self) -> bool:
        """按时间顺序最后一次验证结果是否为 failed（修复后已通过则不再视为失败）。"""
        outcomes = self.verification_outcomes()
        if not outcomes:
            return False
        return outcomes[-1][1] == "failed"

    def repeated_identical_calls(self, threshold: int = 3) -> bool:
        """是否存在连续 N 次完全相同（name+args）的调用（无进展信号）。"""
        if len(self.tool_calls) < threshold:
            return False
        for i in range(len(self.tool_calls) - threshold + 1):
            window = self.tool_calls[i:i + threshold]
            first = (window[0].get("name"), window[0].get("args"))
            if all((c.get("name"), c.get("args")) == first for c in window):
                return True
        return False

    def verification_evidence(self) -> bool:
        """兼容别名：是否执行过验证工具（旧语义）。"""
        return self.verification_ran()


# ---------------------------------------------------------------------------
# 声明识别（确定性正则；只扫 summary + content，不扫结构字段）
# ---------------------------------------------------------------------------

#: 文件/代码/文档「写入、修改、修复、生成、保存」类完成声明
_WRITE_CLAIM_RE = [
    re.compile(r"已(?:经)?(?:修改|修复|修正|写完|写入|更新|创建|覆盖|生成|保存|产出|落盘)"),
    # 名词+动词扫描必须排除“修复建议/修改方案/更新说明”这类名词后缀：
    # 描述能力时的“输出：健康报告 + 修复建议”不是“本轮已修复报告”的完成声明。
    re.compile(
        r"(?:文件|代码|报告|文档|笔记|脚本|程序).{0,8}(?:已|已经)?"
        r"(?:修改|修复|更新|创建|生成|保存|写好|写完|改好)"
        r"(?!建议|方案|清单|列表|步骤|记录|说明|计划|流程|指南|指引)"
    ),
    re.compile(r"fixed|modified|patched|updated|created|written|saved|generated", re.IGNORECASE),
]

#: 「运行/验证通过」类声明（排除未来/假设语气：如果/若/建议/将…）
_VERIFY_CLAIM_RE = [
    re.compile(r"(?:已|已经)(?:测试|验证|校验|运行|执行|检查).{0,6}(?:通过|成功)"),
    re.compile(r"测试(?:已|已经)?(?:全部|所有|一切)?通过"),
    re.compile(r"(?:验证|校验|检查)(?:已|已经)?(?:全部|所有|一切)?通过"),
    re.compile(r"(?:test|tests|check|verification|build)\s+(?:passed|ok|succeeded)", re.IGNORECASE),
]

#: 验证声明前 10 个字符内出现这些词 → 视为未来/假设语气，不算完成声明
_VERIFY_NEGATION_TOKENS = ("如果", "若", "假如", "假设", "一旦", "建议", "计划",
                           "打算", "将", "会", "可能", "需要", "想", "可以", "希望")


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - 10):start]
    return any(tok in window for tok in _VERIFY_NEGATION_TOKENS)

#: 「等待/需要批准」类声明（现在时 / 祈使语气，用于识别口头 Approval）
_APPROVAL_CLAIM_RE = [
    re.compile(r"(?:等待|需要|正在等待|(?<!申)请).{0,10}(?:批准|审批|授权)"),
    re.compile(r"需要你(?:的)?(?:批准|审批|授权|允许)"),
    re.compile(r"待你(?:的)?(?:批准|审批|授权)"),
    re.compile(r"(?:waiting for|needs?|please)\s+(?:your\s+)?(?:approval|authorization|confirmation)", re.IGNORECASE),
]

#: 假设/条件语气（如需/若需要/无需…）不算“正在等待审批”
_APPROVAL_NEGATION_TOKENS = ("如果", "若", "如需", "除非", "无需", "不需要",
                             "不用", "建议", "请勿", "不用你", "假设")


def has_approval_claim(text: str) -> bool:
    if not text:
        return False
    for pattern in _APPROVAL_CLAIM_RE:
        for m in pattern.finditer(text):
            window = text[max(0, m.start() - 10):m.start()]
            if not any(tok in window for tok in _APPROVAL_NEGATION_TOKENS):
                return True
    return False

#: 明显是「已保存/生成」的产物类声明（由文件证据支撑，单独的提示性归类）
_ARTIFACT_CLAIM_RE = [
    # “产出”去掉：能力介绍里“笔记与产出 / 内容产出”是名词小标题，
    # 不是“已产出”声明；真实“已产出”仍由 _WRITE_CLAIM_RE(已…产出) 拦截。
    re.compile(r"(?:报告|文件|文档|表格|工作簿|幻灯片|PPT|Word|Excel|笔记|方案).{0,6}(?:已经|已)?(?:生成|保存|写好)"),
    re.compile(r"已(?:经)?(?:生成|保存).{0,10}(?:报告|文件|文档|表格)"),
    re.compile(r"(?:report|file|document|sheet|deck|note)\s+(?:generated|saved)", re.IGNORECASE),
]

#: 中文产物声明判定用：名词前缀表 + 名词与动词之间的“动作夹层”词。
#: 能力介绍里的“文档读写生成 / 表格制作导出”是能力描述，不是本轮已产出声明。
_ARTIFACT_NOUNS = (
    "工作簿", "幻灯片", "报告", "文件", "文档", "表格",
    "PPT", "Word", "Excel", "笔记", "方案",
)
_ARTIFACT_FILLER_ACTIONS = (
    "读写", "读取", "编辑", "修改", "制作", "输出", "使用",
    "支持", "查看", "打开", "导出", "整理", "处理",
    "生成", "保存", "写好",
)
_ARTIFACT_VERB_SUFFIX = re.compile(r"(?:生成|保存|写好)$")

_WRITE_NOUNS = ("文件", "代码", "报告", "文档", "笔记", "脚本", "程序")
_WRITE_VERB_SUFFIX = re.compile(
    r"(?:修改|修复|更新|创建|生成|保存|写好|写完|改好)$"
)

_REPAIR_BUDGET_DEFAULT = 1


def _matches(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _seg_filler(seg: str, nouns: tuple[str, ...], verb_re: re.Pattern[str]):
    """把“名词…动词”匹配段拆出夹层：返回 (夹层文本, 是否可解析)。"""
    noun = next((n for n in nouns if seg.startswith(n)), "")
    tail = seg[len(noun):] if noun else seg
    vm = verb_re.search(tail)
    if not vm:
        return tail, False
    return tail[: vm.start()], True


def has_write_claim(text: str) -> bool:
    if not text:
        return False
    # “已…产出/生成/修改” 断言式（regex 1）无需夹层判断
    if _matches([_WRITE_CLAIM_RE[0]], text):
        return True
    for m in _WRITE_CLAIM_RE[1].finditer(text):
        filler, parsed = _seg_filler(m.group(0), _WRITE_NOUNS, _WRITE_VERB_SUFFIX)
        if not parsed:
            return True  # 保守兜底
        if filler and any(act in filler for act in _ARTIFACT_FILLER_ACTIONS):
            continue  # “文档读写生成/表格导出保存”是能力描述，非本轮修改声明
        return True
    return _matches([_WRITE_CLAIM_RE[2]], text)


def has_verify_claim(text: str) -> bool:
    if not text:
        return False
    for p in _VERIFY_CLAIM_RE:
        for m in p.finditer(text):
            if not _negated(text, m.start()):
                return True
    return False


def has_artifact_claim(text: str) -> bool:
    if not text:
        return False
    # 英文完成式（file generated / report saved）仍直接视为声明
    if _matches([_ARTIFACT_CLAIM_RE[-1]], text):
        return True
    for m in _ARTIFACT_CLAIM_RE[0].finditer(text):
        filler, parsed = _seg_filler(m.group(0), _ARTIFACT_NOUNS,
                                     _ARTIFACT_VERB_SUFFIX)
        if not parsed:
            return True  # 保守兜底：无法拆解仍按声明处理
        if filler and any(act in filler for act in _ARTIFACT_FILLER_ACTIONS):
            continue  # “文档读写生成”这类能力描述，不是“已生成文档”
        return True
    return False


#: 「等待/需要批准」现在时声明（在 §Approval 定义区实现，此处不再重复定义）
#: has_approval_claim 已在上方基于 finditer + 假设语气窗口实现。


#: 运行观察反馈（运行时事实，不是 prompt 纪律）
OBSERVATION_CLAIM_UNSUPPORTED = (
    "【运行时校验】你声明已经完成了某项工作，但当前 Run 没有检测到支持该声明的执行记录"
    "（真实工具调用 / 文件变更 / 验证记录 / 产物）。"
    "请继续执行必要操作以获得真实执行结果；或者撤回完成声明，明确说明还缺什么信息。"
)
OBSERVATION_APPROVAL_INCONSISTENT = (
    "【运行时校验】你表示需要批准 / 正在等待审批，但当前 Run 没有登记任何待审批操作"
    "（没有真实工具调用被审批门拦截）。"
    "如需执行需要审批的操作（如 run_python），请直接调用该工具发起真实审批流程；"
    "否则请撤回‘等待审批’的说法，直接给出回答或提问。"
)
OBSERVATION_VERIFICATION_FAILED = (
    "【运行时校验】你的任务要求验证通过，但最近一次验证命令实际返回失败"
    "（退出码非 0 / 未通过）。你声明的内容与真实验证结果不一致。"
    "请修复问题并重新运行验证，直到真实通过；或者准确说明当前状态（不要声称验证成功）。"
)
OBSERVATION_NO_PROGRESS = (
    "【运行时校验】本轮没有检测到可验证的进展：没有新的真实工具执行/验证结果，"
    "或你在重复完全相同的调用。请基于当前任务目标继续执行必要操作（写文件/运行验证），"
    "或明确说明你无法继续的原因；不要再输出空回复或重复相同调用。"
)
OBSERVATION_CLARIFICATION_VAGUE = (
    "【运行时校验】你的 questions 回答只写了“需要补充信息/信息不足”之类的空泛措辞，"
    "没有点名缺失字段、也没有给出用户可直接回答的具体问题（如“从哪里出发？”）。"
    "请重写这一轮：readiness 标注 NEEDS_USER 并在 missing 里列出缺失字段名，"
    "questions 里一次问清所有 USER_REQUIRED 缺失项，每一条都要让用户能直接回答。"
)
OBSERVATION_FACT_UNSUPPORTED = (
    "【运行时校验】你的回答里包含具体外部事实（温度/价格/股价/比分/新闻/职位等），"
    "但本轮没有任何检索/查询工具的返回作为依据。请撤回这些没有依据的外部具体数据，"
    "保留能确定的部分；对于无法查证的事实，如实说明当前没有获得可靠数据，而不是给出具体数字。"
)


def observation_for(verdict: GateVerdict) -> str:
    if verdict == GateVerdict.APPROVAL_INCONSISTENT:
        return OBSERVATION_APPROVAL_INCONSISTENT
    if verdict == GateVerdict.VERIFICATION_FAILED:
        return OBSERVATION_VERIFICATION_FAILED
    if verdict == GateVerdict.NO_PROGRESS:
        return OBSERVATION_NO_PROGRESS
    if verdict == GateVerdict.FACT_UNSUPPORTED:
        return OBSERVATION_FACT_UNSUPPORTED
    if verdict == GateVerdict.CLARIFICATION_VAGUE:
        return OBSERVATION_CLARIFICATION_VAGUE
    return OBSERVATION_CLAIM_UNSUPPORTED


#: 面向用户的短事实（不含任何 系统/校验/证据/运行 术语）
_USER_TRUTH: dict[GateVerdict, str] = {
    GateVerdict.CLAIM_UNSUPPORTED: "该任务实际上还没有真正执行或没有产生可见结果。",
    GateVerdict.APPROVAL_INCONSISTENT: "这个操作需要你的确认才能继续。",
    GateVerdict.VERIFICATION_FAILED: "这次的验证没有通过。",
    GateVerdict.NO_PROGRESS: "这一轮没有取得实际进展。",
    GateVerdict.FACT_UNSUPPORTED: "你问到的一些具体事实当前没有可靠的查询依据，我不能给出确切数字。",
    GateVerdict.CLARIFICATION_VAGUE: "你刚才的追问太笼统，我需要点明具体缺失的信息。",
}
_USER_TRUTH.setdefault(GateVerdict.CLAIM_UNSUPPORTED, "该任务尚未完成。")


def short_user_reason(verdict: GateVerdict) -> str:
    return _USER_TRUTH.get(verdict, "你的请求还需要补充一些信息。")


#: 失败收尾时给用户的文案（人话；内部细节保留在 events/audit，不进 run.error_message）
_USER_FAIL_TEXT: dict[GateVerdict, str] = {
    GateVerdict.APPROVAL_INCONSISTENT:
        "这个操作需要你的确认才能继续；在你确认前，我先把这一步停下。请告诉我是否继续。",
    GateVerdict.VERIFICATION_FAILED:
        "这次的验证没有通过。已经做的修改都保留了；请修复问题后再验证一次，或告诉我具体失败原因。",
    GateVerdict.NO_PROGRESS:
        "这一步暂时没有取得进展，我先安全停了下来。你可以换个思路，或补充更多信息后继续。",
    GateVerdict.CLARIFICATION_VAGUE:
        "你刚才的回复还需要更具体一些。请直接告诉我：你最关心的几个点分别是什么？",
}
_USER_FAIL_DEFAULT = (
    "这个任务还没有真正执行完成，我不会把它当成已完成。请告诉我还需要继续做什么，或补充必要的信息。"
)


def user_feedback_text(verdict: GateVerdict) -> str:
    return _USER_FAIL_TEXT.get(verdict, _USER_FAIL_DEFAULT)


def repair_prompt(message: str, verdict: GateVerdict) -> str:
    """把「上一轮被完成校验拒绝」转成面向用户的重新生成指令（输出通道收口）。

    关键：绝不把内部 observation（“请撤回完成声明 / 无执行证据 / 系统提示…”）拼进
    面向模型的用户消息，避免模型复述成“系统提示让我……”。只给用户能懂的事实 +
    明确的“重新回答、勿提内部机制”要求。
    """
    m = str(message or "").strip()
    truth = short_user_reason(verdict)
    return (
        f"{m}\n\n"
        f"请基于实际情况重新回答。{truth}"
        f"如果任务尚未真正完成，请直接说明还需要什么；如果需要用户补充信息，请直接点明具体缺少哪几项。"
        f"请用普通、友好、直接的口吻回答，只回答用户关心的事实。"
        f"不要提及、复述或解释任何后台的校验机制、内部规则或这一次的更正说明。"
    )


class CompletionGate:
    """完成判定闸：evaluate(reply, evidence) → GateVerdict。纯函数式，可离线测试。"""

    def evaluate(self, reply: dict[str, Any] | None, evidence: ExecutionEvidence,
                 request_text: str | None = None) -> GateVerdict:
        if not isinstance(reply, dict):
            return GateVerdict.PASS  # 无可用回复本身由 FinalResponse 路径处理，不在此判定
        kind = str(reply.get("kind") or "answer")
        text = "\n".join(
            [str(reply.get("summary") or ""), str(reply.get("content") or "")]
        )

        # ---- 1) 会话型输出：questions / plan 不需要执行证据 ----
        if kind in ("questions", "plan"):
            if kind == "questions":
                qtext = "\n".join(
                    [str(q) for q in (reply.get("questions") or []) if str(q).strip()]
                )
                content = str(reply.get("content") or "").strip()
                combined = (qtext + "\n" + content).strip()
                if clarification_precision(reply.get("questions")) == "low" or (
                    not qtext and not _QUESTION_MARK_RE.search(combined)
                ):
                    return GateVerdict.CLARIFICATION_VAGUE
            return GateVerdict.PASS

        # ---- 2) Approval 一致性：口头“等待/需要批准”必须有真实 pending ----
        if kind in ("answer", "done") and has_approval_claim(text):
            if evidence.approvals_pending <= 0:
                return GateVerdict.APPROVAL_INCONSISTENT

        # ---- 2b) 能力事实回答（描述当前 Runtime 能力/MCP，0 执行证据）----
        # 只有“能力盘点问题 + 0 执行 + 无直接完成声明”才按 Runtime 事实放行；
        # “我已经成功调用 YouTube MCP 下载了字幕”这类直接完成声明仍会被拦截。
        if (
            kind in ("answer", "done")
            and evidence.executed_count() == 0
            and not evidence.new_files
            and evidence.approvals_pending <= 0
            and is_capability_query(request_text)
            and not has_direct_exec_claim(text)
            and not has_verify_claim(text)
        ):
            return GateVerdict.PASS

        # ---- 2c) 第一人称直接完成声明（“我已经成功调用/下载/修改…”）----
        # 无论是否能力盘点语境，0 执行证据时都不得放行（§14 回归语义）。
        # 排除持久化写入声明（已保存/已记住）——交 2e 依据 persistence evidence 判定。
        if (
            kind in ("answer", "done")
            and has_direct_exec_claim(text)
            and evidence.executed_count() == 0
            and not evidence.new_files
            and not re.search(r"已(?:经)?(?:保存|记住|记下|存好)|(?:保存|记住)完成", text)
        ):
            return GateVerdict.CLAIM_UNSUPPORTED

        # ---- 2d) 具体外部事实声明必须有检索证据（P0，防无证据编造外部数据）----
        # 能力/一般性回答无需证据；但“北京现在25度 / 股价涨到X / 比分X:Y”这类
        # 可被检索核实的事务，若 0 检索证据则不得当作完成收尾（撤回或说明限制）。
        if (
            kind in ("answer", "done")
            and has_external_fact_claim(text)
            and not evidence.retrieval_evidence()
            and not evidence.new_files
            and not is_capability_query(request_text)
        ):
            return GateVerdict.FACT_UNSUPPORTED

        # ---- 2e) 持久化声明（已保存/已记住）——若真实执行过保存则视为完成 ----
        if (
            kind in ("answer", "done")
            and re.search(r"已(?:经)?(?:保存|记住|记下|存好)|(?:保存|记住)完成", text)
            and evidence.persistence_evidence()
            and not evidence.file_mutation_evidence()
            and not evidence.approvals_pending
        ):
            return GateVerdict.PASS

        # ---- 3) 执行声明必须有执行证据（文件写入 / 验证运行 / 产物）----
        # 能力盘点语境（用户问"你能做什么"）里的"能生成/能保存/能修改"是能力描述，
        # 仅真正的"已执行完成"声明（has_direct_exec_claim）仍需证据；纯能力描述豁免。
        if has_write_claim(text) or has_artifact_claim(text):
            capability_desc = is_capability_query(request_text) and not has_direct_exec_claim(text)
            if not capability_desc and not evidence.file_mutation_evidence():
                return GateVerdict.CLAIM_UNSUPPORTED

        # ---- 4) Verification 结果语义（P1-1）----
        # 4a) 声称“验证/测试通过”→ 必须有真实执行且结果=success（不是“执行过”）
        if has_verify_claim(text):
            if not evidence.verification_passed():
                return GateVerdict.CLAIM_UNSUPPORTED
        # 4b) 用户明确“验证必须通过”且【最近一次】验证结果实际失败 → 不得 success 收尾
        if has_verify_intent(request_text) and evidence.latest_verification_failed():
            return GateVerdict.VERIFICATION_FAILED

        # ---- 5) No-progress（模型空转，产品化收口）----
        # 5a) 明确执行意图（验证类）下 0 真实执行且直接以 answer/done 收尾 → 无进展
        if (
            kind in ("answer", "done")
            and has_verify_intent(request_text)
            and evidence.executed_count() == 0
            and not evidence.approvals_pending
        ):
            return GateVerdict.NO_PROGRESS
        # 5b) 连续完全相同调用（name+args）且无任何成功验证 → 无进展
        if (
            kind in ("answer", "done")
            and evidence.repeated_identical_calls()
            and not evidence.verification_passed()
        ):
            return GateVerdict.NO_PROGRESS

        return GateVerdict.PASS

    def describe(self, reply: dict[str, Any] | None, evidence: ExecutionEvidence, verdict: GateVerdict) -> dict[str, Any]:
        """给审计/事件用的结构化解释。"""
        text = ""
        if isinstance(reply, dict):
            text = "\n".join(
                [str(reply.get("summary") or ""), str(reply.get("content") or "")]
            )
        return {
            "verdict": verdict.value,
            "kind": (reply or {}).get("kind", "?"),
            "write_claim": has_write_claim(text) or has_artifact_claim(text),
            "verify_claim": has_verify_claim(text),
            "approval_claim": has_approval_claim(text),
            "evidence": {
                "tool_calls": len(evidence.tool_calls),
                "executed_tools": evidence.executed_names(),
                "new_files": evidence.new_files[:20],
                "approvals_pending": evidence.approvals_pending,
                "file_mutation": evidence.file_mutation_evidence(),
                "verification": evidence.verification_ran(),
                "verification_passed": evidence.verification_passed(),
                "verification_failed": evidence.verification_failed(),
                "verification_outcomes": evidence.verification_outcomes(),
            },
        }


# ---------------------------------------------------------------------------
# 保守的系统结果摘要（Final Response 失败时的降级文本，只复述结构化事实）
# ---------------------------------------------------------------------------

def build_degraded_reply(
    reason: str,
    evidence: ExecutionEvidence,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """最终回复失败时，由 Runtime 生成只包含真实事实的降级回复。

    禁止编造业务结论：只罗列「已执行哪些工具 / 产物新文件 / 审批状态」这类
    Evidence 中存在的事实；reason 只用于向用户解释说明生成失败本身。
    """
    facts: list[str] = []
    for name in evidence.executed_names():
        if name in WRITE_TOOLS:
            facts.append(f"已执行文件修改/生成操作：{name}")
        elif name in VERIFY_TOOLS:
            facts.append(f"已执行运行/验证操作：{name}")
        elif name not in WRITE_TOOLS and name not in VERIFY_TOOLS:
            facts.append(f"已执行操作：{name}")
    for art in artifacts or []:
        facts.append(f"已生成产物：{art.get('name') or '?'}")
    for path in evidence.new_files[:20]:
        facts.append(f"已生成产物文件：{path}")

    if not facts:
        facts_text = "本轮没有任何可复述的工具执行记录。"
    else:
        seen: list[str] = []
        for f in facts:
            if f not in seen:
                seen.append(f)
        facts_text = "\n".join(seen)

    content = (
        f"FORGE 已执行并记录了本次操作，但最终说明生成失败（原因：{reason}）。\n"
        "以下是运行时记录到的真实执行结果：\n" + facts_text +
        "\n详细执行记录可在运行详情（事件/工具调用/产物）中查看。"
    )
    return {
        "kind": "answer",
        "summary": "操作已执行，但最终说明生成失败",
        "content": content,
        "questions": [],
        "saved_file": None,
        "next_step": None,
        "ui": [],
    }
