"""Episode —— 一次 Run 结束后的情节记忆（数据结构 + 语义工具，零 IO）。

设计立意
--------
执行痕迹全都在 task_events 里，但没有一条回路把它们送回模型。本模块只做一件事：
把一次 Run 抽象成可比较、可召回的 Episode，并给出纯函数级别的相似度计算。

安全上是三层约束：
1. 不存 goal 原文 —— 只存自生成的短摘要与主题 token，历史用户语句永不回灌；
2. 所有文本入库前先 strip_paths —— 路径既是相似度杀手也是隐私泄漏面；
   （同一项目的 goal 普遍夹带 F:/... 前缀，不去噪会让所有 episode 彼此假相似）
3. 纯函数：无 IO、无网络、无副作用，规则改动后可离线重跑 reindex。

注意：正因为不存原文，分词/意图规则一旦变更就**无法从摘要反算**，
必须走 EpisodeStore.reindex() 从原始 task_events 重建。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# ---------------------------------------------------------------- 意图分桶

# 粗粒度意图桶：控制召回时的候选池，以及同桶样本的加权。
#
# 规则是**按序 first-match**，所以易混淆的窄桶要排在宽桶前面
# （例如 debug 必须早于 file，否则「修复读取文件报错」会被 file 吃掉）。
# 关键词做小写子串匹配；中英混排是常态，两边都要给够。
INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # test 排在最前：真实请求里"测试"是极强的信号，晚排会被 file/debug 吃掉。
    ("test", ("测试", "pytest", "run_tests", "跑测试", "单元测试", "集成测试",
              "测试失败", "测试用例", "这个测试")),
    ("debug", ("报错", "出错", "异常", "崩溃", "traceback", "exception",
               "修复", "修一下", "error", "bug", "排查")),
    ("code", ("代码", "函数", "class", "重构", "脚本",
              "python", "python3", "code", "function")),
    ("file", ("文件", "文件夹", "读取", "读一下", "打开", "目录",
              "工作区", "文件内容", "read file", "open file",
              "list_workspace_files", "read_workspace_file")),
    ("search", ("搜索", "搜一下", "查一下", "最新", "新闻",
                "web_search", "google")),
    ("doc", ("报告", "文档", "总结", "摘要", "整理成",
             "word", "excel", "ppt", "docx")),
    ("memory", ("记住", "之前说过", "上次", "我的偏好", "remember", "recall")),
    ("schedule", ("定时", "提醒", "每天", "每周", "闹钟", "计划任务", "scheduled")),
    ("analyze", ("分析", "对比", "比较", "统计图", "为什么", "原因", "评估", "看板",
                 "analyze", "analysis")),
)

DEFAULT_INTENT = "chat"

# ---------------------------------------------------------------- 分词

_TOKEN_EN = re.compile(r"[a-z][a-z0-9_]{1,}", re.I)
_TOKEN_DIGITS = re.compile(r"\d+")

# 文件路径不是主题词。历史 goal 里大量夹带 "F:/Byong-hermes/.../app.py"，
# 若不去噪，同一项目的所有 episode 都会因为共享路径 token 而彼此假相似。
_PATH_RE = re.compile(
    r"[a-z]:[\\/][^\s，。；）)\]]*"          # Windows: C:\... 或 C:/...
    r"|(?<![\w])/(?:[\w.\-]+/)+[\w.\-]*"     # Unix: /usr/local/bin
    r"|[\w.\-]+\.(?:py|md|json|ya?ml|txt|csv|ts|tsx|js|ts|jsx|toml|ini|log|db|sqlite)\b",
    re.I,
)

_CN_SEG = re.compile(r"[\u4e00-\u9fff]+")

# 虚字/方位/量词：任何含有这些字的 2-gram 直接丢弃。
# 这是修复 "我读一下当前的X" 被切成 ['我读','读一','一下','下当'] 这类噪声的关键 ——
# 滑窗只在**语义连续的词内部**才有意义，跨虚词边界的都是垃圾。
_CN_FUNC = frozenset(
    "的了是在我有和就不都很也要会能请帮你他她它这那与及为被把么呢吧啊"
    "上下中前后里外对于从到过还没一二三个些点丶又再才更最就只若如但是而"
)

# 通用停用词：出现频率极高但毫无区分度的词，不参与指纹，否则所有 episode 都会彼此相似。
STOPWORDS: frozenset[str] = frozenset({
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "这", "那", "有", "和",
    "就", "都", "而", "及", "与", "也", "很", "要", "会", "能", "可以", "请", "帮", "给",
    "一个", "一下", "什么", "怎么", "如何", "哪些", "多少", "吗", "呢", "吧", "啊",
    "the", "a", "an", "is", "are", "to", "of", "for", "in", "on", "and", "or", "it",
    "this", "that", "do", "does", "can", "you", "me", "my", "i", "we", "with", "from",
    "get", "set", "put", "make", "use", "new", "not", "no", "yes",
})

STOP_EN_VERBS = frozenset({"帮我", "我想", "需要", "希望", "能不能", "是否可以"})

# 中文 2-gram 窗口：没有分词库时对短中文句足够有效，
# 「读取 PDF 文件」→ ['读取','取P'…] 实际会被 _TOKEN_EN 拆出 PDF，中文部分得到 读取/文件 等。
_CN_CHAR = re.compile(r"[\u4e00-\u9fff]")


def strip_paths(text: str) -> str:
    """去掉绝对路径与常见文件名，返回纯语义文本。"""
    if not text:
        return ""
    return _PATH_RE.sub(" ", text)


def _cn_ngrams(seg: str, n: int = 2) -> list[str]:
    """中文二元滑窗，跳过跨虚词边界的 gram。短串整段返回。"""
    chars = "".join(_CN_CHAR.findall(seg))
    if not chars:
        return []
    if len(chars) <= n + 1:
        return [chars] if len(chars) >= 2 else []
    out: list[str] = []
    for i in range(len(chars) - n + 1):
        # 只按**首字**判定：句语起点落在虚字上，说明这个窗口跨了词边界。
        # 不能顺带查尾字 —— "当前""提高"这类有效词的尾字恰好也是方位/趋向字，
        # 一并过滤会把真正有意义的 gram 杀掉。
        if chars[i] in _CN_FUNC:
            continue
        out.append(chars[i:i + n])
    if not out and len(chars) >= 2:
        out = [chars]
    return out


def tokenize(text: str) -> list[str]:
    """把请求文本切成可比较的主题 token 集合（去重、去停用、去路径）。"""
    if not text:
        return []
    low = strip_paths((text or "").lower())
    toks: list[str] = []

    for m in _TOKEN_EN.findall(low):
        if len(m) >= 2 and m not in STOPWORDS:
            toks.append(m)

    for seg in _CN_SEG.findall(low):
        for g in _cn_ngrams(seg):
            if g not in STOPWORDS:
                toks.append(g)

    seen: set[str] = set()
    out: list[str] = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _stop_filtered(toks: Iterable[str]) -> list[str]:
    return [t for t in toks if t not in STOP_EN_VERBS]


def classify_intent(text: str) -> str:
    """返回粗粒度意图桶。命中不了任何规则时归为 chat。

    先剥路径再分类：否则 "跑 benchmark_fixture/test_xxx.py" 会因为路径里的 test
    被误判进 test 桶（实测这让 test 桶虚高到 78 条）。
    """
    low = (text or "").lower()
    low = strip_paths(low)
    for intent, kws in INTENT_RULES:
        for kw in kws:
            if kw in low:
                return intent
    return DEFAULT_INTENT


def fingerprint(text: str) -> str:
    """同一类请求的稳定指纹：intent + 主题 token 的有序哈希。

    token 取前 8 个并排序 → 保证同义改写（语序不同）仍可能命中；
    不用全部 token → 保证长句不会因为多一个词就完全失配。
    """
    intent = classify_intent(text)
    toks = sorted(tokenize(text))[:8]
    raw = intent + "|" + ",".join(toks)
    return intent + ":" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    if inter == 0:
        return 0.0
    return inter / len(sa | sb)


def overlap(a: Iterable[str], b: Iterable[str]) -> float:
    """重叠系数 = 交集 / 较小集合。

    比 Jaccard 更适合这里：查询往往只有 3~5 个 token，而 episode 有 10+ 个，
    Jaccard 会因为并集过大把"完全被包含"的强相关样本也压到阈值以下。
    """
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


INTENT_BONUS = 0.08

_STRONG_RE = re.compile(r"^[a-z][a-z0-9_]{1,}$", re.I)


def split_strength(tokens: Iterable[str]) -> tuple[set[str], set[str]]:
    """把 token 拆成强信号（英文术语/技术名）与弱信号（中文 2-gram）。

    "python" 命中一次，比三个碰巧重叠的中文字组更能说明两件事是同一类。
    把它们混在一个集合里算 Jaccard，强信号会被 gram 噪声稀释掉。
    """
    strong, weak = set(), set()
    for t in tokens:
        (strong if _STRONG_RE.match(t) else weak).add(t)
    return strong, weak


def similarity(query_tokens: list[str], ep_tokens: list[str], *, same_intent: bool = False) -> float:
    """综合相似度：强信号主导 + 全量 jaccard 校正 + 同 intent 加权。

    拆三档而非一刀切，是因为单看任一度量都会错：
      - 纯 overlap：两个八竿子打不着的长句也能靠个别虚词组命中；
      - 纯 jaccard：短查询对长 episode 永远吃亏（"写 python 代码" vs 一整段描述）。
    """
    if not query_tokens or not ep_tokens:
        return 0.0
    q_strong, q_weak = split_strength(query_tokens)
    e_strong, e_weak = split_strength(ep_tokens)

    if q_strong and e_strong:
        base = 0.45 * overlap(q_strong, e_strong) + 0.20 * overlap(q_weak, e_weak)
    else:
        base = 0.65 * overlap(q_weak, e_weak)
    base += 0.35 * jaccard(query_tokens, ep_tokens)

    if same_intent:
        base += INTENT_BONUS
    return min(base, 1.0)


# ---------------------------------------------------------------- 数据模型


@dataclass(slots=True)
class Episode:
    """一次 Run 结束后的结构化复盘记录。

    注意：刻意**不保存 goal 原文**，只保存用于排错的 60 字摘要——
    摘要由我们自己从统计中生成，永远不会把历史用户语句原封不动送回模型。
    """

    run_id: str
    fingerprint: str
    intent: str
    topic_tokens: list[str] = field(default_factory=list)
    outcome: str = "unknown"          # completed / failed / cancelled
    terminal_kind: str = ""           # run.terminal 的 kind（completed/provider_error/...）
    tool_sequence: list[str] = field(default_factory=list)
    tool_count: int = 0
    failed_tools: list[str] = field(default_factory=list)
    rounds: int = 0                   # 模型轮数 = max(model_calls.turn_number)
    produced_artifact: bool = False
    error_excerpt: str = ""
    goal_digest: str = ""             # ≤60 字的归一化摘要（非原文）
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_row(self) -> dict[str, Any]:
        import json

        return {
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "intent": self.intent,
            "topic_tokens": json.dumps(self.topic_tokens, ensure_ascii=False),
            "outcome": self.outcome,
            "terminal_kind": self.terminal_kind,
            "tool_sequence": json.dumps(self.tool_sequence, ensure_ascii=False),
            "tool_count": self.tool_count,
            "failed_tools": json.dumps(self.failed_tools, ensure_ascii=False),
            "rounds": self.rounds,
            "produced_artifact": int(self.produced_artifact),
            "error_excerpt": self.error_excerpt[:300],
            "goal_digest": self.goal_digest[:60],
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Episode":
        import json

        def _j(v: Any, default: Any) -> Any:
            if not v:
                return default
            try:
                return json.loads(v)
            except Exception:
                return default

        return cls(
            run_id=row["run_id"],
            fingerprint=row["fingerprint"],
            intent=row["intent"],
            topic_tokens=_j(row["topic_tokens"], []),
            outcome=row["outcome"],
            terminal_kind=row["terminal_kind"] or "",
            tool_sequence=_j(row["tool_sequence"], []),
            tool_count=int(row["tool_count"] or 0),
            failed_tools=_j(row["failed_tools"], []),
            rounds=int(row["rounds"] or 0),
            produced_artifact=bool(row["produced_artifact"]),
            error_excerpt=row["error_excerpt"] or "",
            goal_digest=row["goal_digest"] or "",
            created_at=row["created_at"] or "",
            schema_version=int(row["schema_version"] or SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 终态归一

OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_NEEDS_USER = "needs_user"
OUTCOME_NEEDS_APPROVAL = "needs_approval"
OUTCOME_UNKNOWN = "unknown"

# run.terminal 的 kind → 归一化 outcome。
# 注意 needs_user_input / needs_approval **不是失败** —— 它们是被门禁拦下的中间态，
# 历史上这 79 条曾被误记为 unknown，导致召回时成败比算错。
_KIND_OUTCOME: dict[str, str] = {
    "completed": OUTCOME_COMPLETED,
    "cancelled": OUTCOME_CANCELLED,
    "no_progress": OUTCOME_FAILED,
    "bounded_failure": OUTCOME_FAILED,
    "provider_error": OUTCOME_FAILED,
    "token_budget": OUTCOME_FAILED,
    "budget_exhausted": OUTCOME_FAILED,
    "needs_user_input": OUTCOME_NEEDS_USER,
    "needs_approval": OUTCOME_NEEDS_APPROVAL,
}


def normalize_outcome(kind: str, state: str = "") -> str:
    """由 terminal kind（优先）与 state（兜底）推出 outcome。"""
    k = (kind or "").strip().lower()
    if k in _KIND_OUTCOME:
        return _KIND_OUTCOME[k]
    s = (state or "").strip().lower()
    if s in (OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_CANCELLED):
        return s
    if "wait" in k or "pending" in k:
        return OUTCOME_NEEDS_APPROVAL if "approval" in k else OUTCOME_NEEDS_USER
    return OUTCOME_UNKNOWN


def is_informative(ep: "Episode") -> bool:
    """这条 episode 值不值得被召回？

    纯寒暄类 Run（"你好"、"你有哪些工具"）的特征是：没调工具、没走几轮、还成功了。
    它们能告诉模型的唯一信息是"上次说了句废话并成功了" —— 零指导价值，
    但会挤占宝贵的注入预算并稀释真正的同类样本。所以排除。
    """
    if ep.outcome in (OUTCOME_FAILED, OUTCOME_NEEDS_APPROVAL):
        return True  # 失败与拦截样本永远是高价值教训
    if len(ep.topic_tokens) < 2 and ep.tool_count == 0:
        return False  # 连主题词都抽不出来的纯数字/符号类请求，无法归类也无法匹配
    return ep.tool_count > 0 or ep.rounds >= 2


# ---------------------------------------------------------------- 摘要


def readable_digest(goal: str, *, limit: int = 60) -> str:
    """给人看的短摘要：按标点/空白切成语义片段，保留可读的词组。

    刻意**不**返回原文：既保护隐私，也杜绝"历史用户语句被当作新指令执行"这条注入面。
    """
    parts = [p.strip() for p in re.split(r"[，。！？；,\.!\?;\s]+", goal or "") if p.strip()]
    if not parts:
        return (goal or "")[:limit]
    # 丢掉纯语气片段后拼接；超长按片段整体截断，避免半截词
    noise = {"请", "帮我", "我想", "我需要", "能不能", "你好", "谢谢"}
    keep = [p for p in parts if p not in noise] or parts
    out = " / ".join(keep[:4])
    return out[:limit]


def digest_goal(goal: str, *, limit: int = 60) -> str:
    """兼容旧名：对外仍是"摘要"，内部改用可读版。"""
    return readable_digest(goal, limit=limit)

# ---------------------------------------------------------------- 教训可用性过滤

# error_excerpt 是 Run 终当时的最后一小段错误消息。实测约 180 条里绝大多数是
# **harness 内部噪音** —— 终态标记（needs_user_input）、收敛提示、审批拦截、
# Completion Gate 的拒绝文案、生成失败兜底语。它们是给"用户看"的运行状态说明，
# 不是给"模型自用"的可行动教训。
#
# 把这些当"教训："注入给模型会同时造成两种伤害：
#   1. 挤占注入预算（一条 20 字左右的噪音就占掉一条高位席）
#   2. 把 harness 的内部话语 & 拒绝文案灌进上下文，可能干扰模型对自身行为的判断
# 50 case A/B 实测失败的归因之一即在此。

_NOISE_EXACT = {
    "needs_user_input",
    "needs_approval",
    "approval required",
    "final answer generation failed",
    "completed",
}

_NOISE_SUBSTR = (
    "已安全结束本轮",
    "连续没有取得新进展",
    "我不会把它当成已完成",
    "最终回答生成失败",
    "这不会产生任何副作用",
    "详情见运行记录",
    "Max turns",
    "请告诉我是否继续",
    "已执行的操作仍然保留",
    "已执行的操作与结果都保留",
    "先回答错误还是会触发工具调用拦截",
    "模型调用出现未分类错误",
    "Tool invocations require",
    "call ID",
)

# 反过来，这些特征说明它是一条**真实技术错误**，值得留下
_ACTIONABLE_HINT = (
    "errno", "error:", "exception", "traceback", "failed to", "invalid",
    "not found", "no such", "permission", "denied", "timeout", "timed out",
    "refused", "unexpected", "assert", "syntaxerror", "importerror",
    "attributeerror", "nameerror", "typeerror", "valueerror", "keyerror",
    "has no attribute", "is not defined", "cannot unpack", "out of range",
    "无法", "找不到", "不存在", "失败：", "解析失败", "超时",
)


def is_actionable_error(text: str | None) -> bool:
    """这条 error_excerpt 值不值得当"教训"注入给模型？

    判据：先排除已知噪音（精确匹配 + 子串），再看是否有真实技术错误的特征。
    宁可漏掉一些，也不要把 harness 内部话语灌进上下文。
    """
    if not text:
        return False
    raw = text.strip()
    if len(raw) < 4:
        return False
    low = raw.lower()

    if low in _NOISE_EXACT:
        return False
    for frag in _NOISE_SUBSTR:
        if frag.lower() in low:
            return False

    for hint in _ACTIONABLE_HINT:
        if hint in low:
            return True
    return False
