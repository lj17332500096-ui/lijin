"""ReplyParser：AgentReply 容错解析管线（Model 输出 → Canonical AgentReply）。

阶段顺序（对应《AgentReply 格式校验修复》方案）：
  raw 输出
  → extract（Markdown fence / 前后说明文字 / 多余换行 / 字段别名）
  → normalize（type→kind、message→content、question→questions、file→files、
              为缺失可选字段填安全默认值）
  → minimal-validate（核心必填只保留 kind + content；questions/note 例外规则）
  → repair（本地确定性修复；可选模型修复 FORGE_REPLY_REPAIR=model）
  → fallback（仍不合法但存在可读文本 → {"kind":"answer","content":"<原文>"}）
  → canonical dict（记录 reply.validation_warning 的宽恕事件）

原则：格式问题永远不构成任务失败；只有安全闸（密钥等）仍可拦截。
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from schemas import AgentReply

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

KINDS = {"answer", "plan", "note", "questions", "done"}
KIND_ALIASES = {"type": "kind", "message": "content", "answer": "content",
                "text": "content", "reply": "content", "result": "content",
                "question": "questions", "file": "files", "files": "files",
                "path": "saved_file"}
MAX_QUESTIONS = 20
MAX_CONTENT = 200000

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.M)
_JSON_START = re.compile(r"\{")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_SMART_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})


@dataclass(slots=True)
class ParseResult:
    ok: bool
    canonical: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stage: str = "direct"  # direct | normalized | repaired | fallback

    @property
    def canonical_json(self) -> str:
        return json.dumps(self.canonical, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. 提取：容忍 fence / 前后说明 / 换行
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


def _extract_json_candidates(text: str) -> list[str]:
    """返回候选 JSON 字符串（可能多个），供逐个尝试解析。"""
    candidates: list[str] = []
    cleaned = _strip_fences(text)
    candidates.append(cleaned)
    # 每次从 '{' 开始尝试 raw_decode，成功即截取该对象
    starts = [m.start() for m in re.finditer(r"\{", cleaned)]
    for pos in starts[:12]:
        candidate = cleaned[pos:]
        candidates.append(candidate)
    return candidates


def _try_decode(text: str) -> tuple[dict | None, str]:
    decoder = json.JSONDecoder()
    for candidate in _extract_json_candidates(text):
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            obj, _end = decoder.raw_decode(candidate)
            if isinstance(obj, dict):
                return obj, "direct"
        except (json.JSONDecodeError, ValueError):
            continue
    return None, ""


# ---------------------------------------------------------------------------
# 2. 本地修复（确定性，不调模型）
# ---------------------------------------------------------------------------

def _repair_locally(text: str) -> tuple[dict | None, str]:
    repaired = text.translate(_SMART_QUOTES)
    repaired = _TRAILING_COMMA.sub(r"\1", repaired)
    obj, stage = _try_decode(repaired)
    if obj is not None:
        return obj, "repaired"
    # 常见：模型把 JSON 包在单行散文里且含转义换行 → 放宽为 “第一个 { 到最后一个 }” 后修尾逗号
    start, end = repaired.find("{"), repaired.rfind("}")
    if 0 <= start < end:
        core = repaired[start:end + 1]
        core = _TRAILING_COMMA.sub(r"\1", core)
        try:
            obj = json.loads(core)
            if isinstance(obj, dict):
                return obj, "repaired"
        except json.JSONDecodeError:
            pass
    return None, ""


def _repair_with_model(text: str) -> dict | None:
    """可选：网关模型修复（仅格式、不重做任务）。默认关闭，FORGE_REPLY_REPAIR=model 开启。"""
    if os.getenv("FORGE_REPLY_REPAIR", "").strip().lower() != "model":
        return None
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("AGENT_MODEL")
    if not api_key or not model:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None, timeout=60)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content":
                    "把下面的文本整理成单个 JSON 对象：kind(answer/plan/note/questions/done) 与 content 必填；"
                    "只输出 JSON，不要解释。"},
                {"role": "user", "content": text[:6000]},
            ],
            temperature=0,
            max_tokens=2000,
        )
        raw = (resp.choices[0].message.content or "").strip()
        obj, _ = _try_decode(raw)
        if obj is None:
            obj, _ = _repair_locally(raw)
        return obj
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 3. 归一化 + 别名映射
# ---------------------------------------------------------------------------

def _as_str_list(value: object) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [str(v) for v in value[:MAX_QUESTIONS]]
    else:
        items = [str(value)] if value is not None else []
    return [s.strip() for s in items if str(s).strip()][:MAX_QUESTIONS]


def normalize(obj: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    out: dict = {}

    # kind：type 别名 + 白名单；未知降级 answer
    kind = obj.get("kind", obj.get("type"))
    if not isinstance(kind, str) or kind.strip() not in KINDS:
        if kind not in (None, ""):
            warnings.append(f"kind 值不合法（{kind!r}），已按 answer 处理")
        kind = "answer"
    out["kind"] = kind

    # content：message/answer/text/reply/result 别名
    content = obj.get("content")
    for alias in ("message", "answer", "text", "reply", "result", "body"):
        if content in (None, ""):
            content = obj.get(alias)
    summary = obj.get("summary")
    if content in (None, "") and summary not in (None, ""):
        content = summary
        warnings.append("content 缺失，已用 summary 兜底")
    content = str(content or "").strip()[:MAX_CONTENT]
    out["content"] = content

    out["summary"] = str(obj.get("summary") or "").strip()[:2000]
    from runtime.public_activity import public_summary
    out["public_summary"] = public_summary(obj.get("public_summary"))

    questions = obj.get("questions", obj.get("question"))
    out["questions"] = _as_str_list(questions)
    if kind == "questions" and not out["questions"] and not out["content"]:
        out["content"] = "请补充信息后告诉我。"
        warnings.append("questions 类回复既无问题也无正文，已填默认追问")

    # saved_file：别名 path/files 兼容；伪造路径宽恕处理（丢弃+警告，交给 guardrail 安全层兜底）
    saved = obj.get("saved_file", obj.get("path"))
    if saved is None and isinstance(obj.get("files"), list) and len(obj["files"]) == 1:
        saved = obj["files"][0]
    out["saved_file"] = str(saved).strip() if saved not in (None, "") else None

    nxt = obj.get("next_step")
    out["next_step"] = str(nxt).strip() if nxt not in (None, "") else None

    # readiness（可选，任务准备度观测字段）：只透传受控枚举与计数，不做大型 Schema
    _rd = obj.get("readiness")
    if isinstance(_rd, dict):
        _status = str(_rd.get("status") or "").strip().upper()
        if _status not in ("READY", "DISCOVERABLE", "NEEDS_USER", "UNKNOWN"):
            _status = ""
        _missing = _rd.get("missing_count")
        # v2 收口：透传 missing[]（{name, reason}），供 Clarification Precision 与
        # 多轮上下文校验使用；仅透传受控长度，不落问题原文之外的大字段。
        _missing_list: list[dict] = []
        try:
            from runtime.readiness_gate import normalize_missing

            _missing_list = normalize_missing(_rd.get("missing"))
        except Exception:
            _missing_list = []
        try:
            _missing = max(0, min(int(_missing), 50)) if _missing is not None \
                else len(_missing_list)
        except (TypeError, ValueError):
            _missing = len(_missing_list)
        if _status:
            _clean: dict = {"status": _status, "missing_count": _missing}
            if _missing_list:
                _clean["missing"] = _missing_list
            _reason = str(_rd.get("reason") or "").strip()[:200]
            if _reason:
                _clean["reason"] = _reason
            out["readiness"] = _clean

    ui = obj.get("ui")
    out["ui"] = list(ui) if isinstance(ui, list) else []

    # 数字/杂项默认
    out.setdefault("plan", obj.get("plan"))
    return out, warnings


# ---------------------------------------------------------------------------
# 4. 最小校验 + 宽恕（drop ui / saved_file 后重试）
# ---------------------------------------------------------------------------

def _ui_within_limits(ui: list) -> str | None:
    """ui 业务上限（宽恕语义：超限返回原因，由调用方丢弃 ui 并记警告）。"""
    import guardrails  # 延迟导入避免循环；复用同一套上限常量

    if not ui:
        return None
    if len(ui) > guardrails.UI_MAX_BLOCKS:
        return f"ui 卡片数量超过上限（{guardrails.UI_MAX_BLOCKS} 块）"
    rows_total = 0
    for block in ui:
        if not isinstance(block, dict):
            return "ui 卡片结构不合法"
        btype = block.get("type", "")
        if btype in ("bar", "line", "pie"):
            labels = block.get("labels") or []
            rows_total += len(labels)
            series = block.get("series") or []
            if len(series) > 8 or any(len(s.get("values") or []) != len(labels) for s in series):
                return "ui 图表数据长度不合法"
        elif btype == "table":
            rows = block.get("rows") or []
            cols = len(block.get("columns") or [])
            rows_total += len(rows)
            if any(len(row) != cols for row in rows):
                return "ui 表格某行长度与列数不一致"
            if cols > 20:
                return "ui 表格列数超过上限（20）"
        elif btype == "form":
            fields = block.get("fields") or []
            if len(fields) > guardrails.UI_MAX_FIELDS:
                return f"ui 表单字段超过上限（{guardrails.UI_MAX_FIELDS} 个）"
            names = [f.get("name") for f in fields]
            if len({n for n in names if n is not None}) != len(names):
                return "ui 表单字段 name 重复"
        elif btype == "file_list":
            files = block.get("files") or []
            if len(files) > guardrails.UI_MAX_FILES:
                return f"ui 文件清单条目超过上限（{guardrails.UI_MAX_FILES} 条）"
            rows_total += len(files)
    if rows_total > guardrails.UI_MAX_DATA_ROWS:
        return f"ui 数据行数超过上限（{guardrails.UI_MAX_DATA_ROWS} 行）"
    return None


def _saved_file_trusted(saved: str | None) -> tuple[str | None, str | None]:
    """校验 saved_file 真实性；不可信返回 (None, warning)，可信原样返回。"""
    if not saved:
        return None, None
    path = Path(str(saved))
    suffix = path.suffix.lower()
    try:
        if suffix == ".md":
            path.resolve().relative_to((BASE_DIR / "notes").resolve())
        elif suffix in (".docx", ".xlsx", ".pptx"):
            path.resolve().relative_to((BASE_DIR / "exports").resolve())
        else:
            raise ValueError
        if path.exists() and path.is_file():
            return str(saved), None
        raise ValueError
    except (ValueError, OSError):
        return None, f"saved_file 不可信（{str(saved)[:80]}），已忽略"


def _build_agent_reply(canonical: dict) -> AgentReply | None:
    try:
        return AgentReply.model_validate(canonical)
    except Exception:
        return None


def _forgive_validate(canonical: dict) -> tuple[dict, list[str]]:
    """逐级宽恕：ui 业务/结构不合法丢弃 → saved_file 不可信丢弃 → 仍失败退回最小体。"""
    warnings: list[str] = []
    candidate = dict(canonical)
    reply = _build_agent_reply(candidate)
    ui_reason = _ui_within_limits(candidate.get("ui") or []) if candidate.get("ui") else None
    if ui_reason or reply is None:
        if candidate.get("ui"):
            candidate["ui"] = []
            warnings.append(f"ui 卡片不合法（{ui_reason or '结构校验失败'}），已忽略（保留正文）")
        reply = _build_agent_reply(candidate)
    if candidate.get("saved_file"):
        trusted, warn = _saved_file_trusted(candidate.get("saved_file"))
        if warn:
            candidate["saved_file"] = None
            warnings.append(warn)
            reply = _build_agent_reply(candidate)
    if reply is None and candidate.get("next_step"):
        candidate["next_step"] = None
        reply = _build_agent_reply(candidate)
    if reply is None:
        candidate = {"kind": "answer", "content": candidate.get("content") or "", "summary": "",
                     "questions": [], "saved_file": None, "next_step": None, "ui": []}
        warnings.append("schema 校验仍未通过，已退回最小 answer 体")
        reply = _build_agent_reply(candidate)
    if reply is not None:
        out = {k: v for k, v in candidate.items() if k in AgentReply.model_fields}
        return out, warnings
    return candidate, warnings


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _usable(reply: dict) -> bool:
    """最小可用性：content 非空；questions 类允许只带问题列表。"""
    if reply.get("kind") not in KINDS:
        return False
    if str(reply.get("content") or "").strip():
        return True
    return reply.get("kind") == "questions" and bool(reply.get("questions"))


def parse(raw: object) -> ParseResult:
    """任何形态的模型输出 → ParseResult（canonical dict + warnings + stage）。"""
    if isinstance(raw, AgentReply):
        try:
            return ParseResult(ok=True, canonical=raw.model_dump(), stage="direct")
        except Exception:
            raw = str(raw)
    if isinstance(raw, dict):
        canonical, w = normalize(raw)
        canonical, w2 = _forgive_validate(canonical)
        if _usable(canonical):
            return ParseResult(ok=True, canonical=canonical, warnings=w + w2,
                               stage="normalized" if (w or w2) else "direct")
        return _fallback(str(raw.get("content") or raw), w + w2)

    text = str(raw or "").strip()
    if not text:
        return ParseResult(ok=False, canonical=_minimal(""), warnings=["模型没有输出任何内容"], stage="fallback")

    obj, stage = _try_decode(text)
    warnings: list[str] = []
    if obj is None:
        obj, stage = _repair_locally(text)
        if obj is not None:
            warnings.append("原始输出不是合法 JSON，已本地修复")
    if obj is None:
        obj = _repair_with_model(text)
        if obj is not None:
            warnings.append("原始输出经模型修复为合法 JSON")
            stage = "repaired"
    if obj is None:
        return _fallback(text, ["原始输出不是 JSON 且无法修复，已按纯文本降级"])

    canonical, w = normalize(obj)
    canonical, w2 = _forgive_validate(canonical)
    if _usable(canonical):
        stage = "normalized" if (w or w2) else stage
        return ParseResult(ok=True, canonical=canonical, warnings=warnings + w + w2, stage=stage)
    return _fallback(text, warnings + w + w2 + ["规范化后仍无有效正文，已按原文降级"])


def _minimal(content: str) -> dict:
    return {"kind": "answer", "content": str(content)[:MAX_CONTENT], "summary": "",
            "questions": [], "saved_file": None, "next_step": None, "ui": []}


def _fallback(text: str, warnings: list[str]) -> ParseResult:
    """最终兜底：可读文本 → answer 降级（绝不失败）。"""
    readable = _strip_fences(text).strip()[:MAX_CONTENT]
    if not readable:
        return ParseResult(ok=False, canonical=_minimal(""), warnings=warnings + ["模型没有可读输出"], stage="fallback")
    warnings.append("reply_validation_warning：已降级为 answer（内容=原始可读文本）")
    return ParseResult(ok=True, canonical=_minimal(readable), warnings=warnings, stage="fallback")


def coerce_reply(raw: object) -> AgentReply:
    """对外：任何输出 → 合法 AgentReply（永远不抛）。"""
    result = parse(raw)
    return AgentReply.model_validate(result.canonical)


def structured_output_supported() -> tuple[bool, str]:
    """Provider 原生结构化输出探测：当前网关(chat/completions 中转)不支持；
    直连 OpenAI responses 且 SDK 支持时返回 True。"""
    base_url = os.getenv("OPENAI_BASE_URL")
    use_responses = os.getenv("OPENAI_USE_RESPONSES", "true").strip().lower() == "true"
    if base_url is None and use_responses:
        return True, "直连 responses API 可用（建议在 Agent 层设置 output 格式）"
    return False, "第三方网关 chat/completions 不支持 response_format/json_schema；由 ReplyParser 兜底"
