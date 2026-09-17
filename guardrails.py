"""输入/输出安全校验（Guardrails）。

输入侧：拦截越狱、指令覆盖、套取系统提示词或密钥等请求。
输出侧：校验 AgentReply 结构与内容，防止编造 saved_file、输出空壳或泄露密钥。
全部为本地规则，零额外模型调用。
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    TResponseInputItem,
    input_guardrail,
    output_guardrail,
)

from schemas import AgentReply
from pydantic import ValidationError

from runtime.key_patterns import KEY_PATTERNS, mask_keys, has_key

BASE_DIR = Path(__file__).resolve().parent
NOTES_DIR = BASE_DIR / "notes"
EXPORTS_DIR = BASE_DIR / "exports"
OFFICE_SUFFIXES = {".docx", ".xlsx", ".pptx"}

# 生成式 UI 卡片的安全上限（防模型输出海量/畸形数据）
UI_MAX_BLOCKS = 8          # 单条回复最多几块卡片
UI_MAX_DATA_ROWS = 500     # 图表 labels / 表格行 数据总量上限
UI_MAX_FIELDS = 6          # 表单字段数上限
UI_MAX_FILES = 100         # 文件清单条目上限

_LOG_DIR = BASE_DIR / "logs"


def _mask_secrets(text: str) -> str:
    return mask_keys(text)


def _log_guardrail_failure(output: object, reason: str) -> None:
    """拦截失败时把原始输出样本落盘（logs/guardrail_failures.jsonl），便于排查格式问题。"""
    try:
        raw = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    except Exception:
        raw = str(output)
    snippet = _mask_secrets(raw)[:2000]
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "reason_head": reason[:200],
        "output_len": len(raw),
        "output_sample": snippet,
    }
    with (_LOG_DIR / "guardrail_failures.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _text_of_input(input_value: str | list[TResponseInputItem]) -> str:
    """把输入转成纯文本，便于规则匹配。"""
    if isinstance(input_value, str):
        return input_value
    parts: list[str] = []
    for item in input_value or []:
        if isinstance(item, dict):
            content = item.get("content")
        else:
            content = getattr(item, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for chunk in content:
                text = chunk.get("text") if isinstance(chunk, dict) else getattr(chunk, "text", None)
                if text:
                    parts.append(str(text))
    return "\n".join(parts)


def _last_user_text(input_value: str | list[TResponseInputItem]) -> str:
    """输入安全闸只应扫描「用户最新一条输入」。

    SDK 会把整个会话历史（含 assistant 自己的历史回答）交给 input guardrail，
    若全量扫描，FORGE 历史回答里正常提到的“读取… .env 说明”会把之后所有轮次永久误拦。
    因此：str 直接返回；list 只取最后一条 role=user 的文本。
    """
    if isinstance(input_value, str):
        return input_value
    last_user = ""
    for item in input_value or []:
        if isinstance(item, dict):
            role = item.get("role")
        else:
            role = getattr(item, "role", None)
        if role == "user":
            last_user = _text_of_input([item])
    return last_user


def scan_input(text: str) -> str | None:
    """扫描用户输入，命中风险返回原因，否则返回 None。"""
    low = text.lower()

    # 1) 越狱 / 指令覆盖 / 角色替换
    if re.search(
        r"(忽略|无视|忘记|覆盖|跳过|不要遵守|假装你是).{0,15}(指令|规则|人设|系统提示|限制)"
        r"|jailbreak|do anything now|ignore (all|previous|prior) (instructions|rules|prompts)",
        low,
    ):
        return "输入疑似越狱或试图覆盖 Agent 指令"

    # 2) 套取系统提示词 / 人设原文
    if re.search(
        r"(复述|完整|原文|原样|全部).{0,4}(系统提示词?|人设|指令|prompt|instructions)"
        r"|(你的|系统).{0,6}(系统提示词?|人设|指令|prompt|instructions)"
        r".{0,8}(是什么|内容|完整|原文|发给我|告诉我|复述)",
        low,
    ):
        return "输入疑似试图套取系统提示词/人设原文"

    # 3) 读取/输出密钥或敏感配置文件（.env、apikey 等）；窗口收紧防误伤正常说明
    if re.search(
        r"(读取|输出|给我|显示|打印).{0,6}"
        r"(\.env(?!\.example)|apikey|api ?key|密钥|密码|凭据).{0,6}(内容|值|文件)?",
        low,
        re.I,
    ) or re.search(r"\.env(?!\.example).{0,6}(内容|读取|给我|输出)", low):
        return "输入疑似索取密钥或敏感配置文件内容"

    # 4) 明显要求泄露其他密钥格式
    if re.search(r"(帮我|请).{0,8}(记住|保存|读取|查看).{0,12}(api ?key|密钥|密码)", low):
        return "输入涉及 API Key/密码等敏感凭据，请改用 .env 配置"

    # 5) 输入里直接夹带疑似密钥文本（防止被要求"记住"后存进 memory.json）
    if has_key(text):
        return "输入疑似包含 API Key/密钥文本，请勿粘贴或让 Agent 保存密钥"

    # 6) 破坏性操作拦截（越权类：删文件/清空数据库/改系统配置/执行危险命令）
    # 多轮维修回归风险：Router 词表匹配可能把"执行 rm -rf /"误进 coding 族，
    # 但安全层必须在 guardrail 层拦截，不依赖 Router 层。
    # 正则写法：扁平化交替分组；内层全部用 (?:...) 非捕获分组；SQL 关键字显式 (?i) 大小写不敏感。
    _DESTRUCTIVE_RE = (
        r"rm\s+-rf"                                     # rm -rf
        r"|del\s+/s"                                     # del /s
        r"|delete\s+all"                                 # delete all
        r"|清空.{0,6}(?:数据库|生产库|生产表|数据表|生产环境|业务数据)"
        r"|把.{0,4}(?:数据库|生产库|生产表|数据表|生产环境).{0,4}清空"
        r"|删(?:掉|除)?(?:所有|全部|整个|生产)(?:文件|数据|表|库|记录|日志|环境)"
        r"|修改(?:系统|生产|全局|内核)(?:配置|参数|设置)?"
        r"|格式化.{0,4}(?:磁盘|硬盘|分区|盘符|系统盘|盘)"
        r"|diskpart|fdisk\s+/"
        r"|curl\s+\S+\s*\|\s*(?:ba)?sh"
        r"|wget\s+\S+\s*\|\s*(?:ba)?sh"
        r"|(?i:DROP\s+(?:DATABASE|TABLE))"               # SQL 关键字（大小写不敏感）
        r"|(?i:TRUNCATE\s+TABLE)"
        r"|(?i:SHUTDOWN\s+NOW)"
    )
    if re.search(_DESTRUCTIVE_RE, low):
        return "输入包含破坏性操作指令，请改用受控流程（如审批+沙箱）"

    # 7) 套取敏感文件路径（/etc/passwd、.env、密钥文件）
    if re.search(
        r"(/etc/(?:passwd|shadow|sudoers)|~/(?:\.ssh|\.aws)|"
        r"WINDOWS\S{0,6}(?:System32|win\.ini)|boot\.ini|ntds\.dit)",
        low,
    ):
        return "输入试图访问敏感系统文件路径，请改用受控接口"

    return None


def _extract_json_object(text: str) -> str:
    """去掉可能的 markdown 代码块，取出第一个 {...} JSON 片段（忽略前后杂文）。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    return cleaned[start : end + 1] if 0 <= start < end else cleaned


def _extract_reply(output: object) -> AgentReply | None:
    """把输出转成 AgentReply；失败返回 None。

    兼容三种形态：AgentReply 实例、dict、字符串（允许 JSON 前后混有杂文，
    只取第一个 {...} 对象，其余文字一律丢弃）。
    """
    if isinstance(output, AgentReply):
        return output
    if isinstance(output, dict):
        try:
            return AgentReply.model_validate(output)
        except Exception:
            return None
    if isinstance(output, str) and output.strip():
        try:
            return AgentReply.model_validate_json(_extract_json_object(output))
        except Exception:
            return None
    return None


def _has_secret(text: str) -> bool:
    return has_key(text)


def _check_ui(reply: AgentReply) -> str | None:
    """校验交互卡片数组；超限/畸形返回原因，否则 None。"""
    blocks = reply.ui
    if not blocks:
        return None
    if len(blocks) > UI_MAX_BLOCKS:
        return f"ui 卡片数量超过上限（{UI_MAX_BLOCKS} 块）"
    rows_total = 0
    for block in blocks:
        btype = getattr(block, "type", "")
        if btype in ("bar", "line", "pie"):
            rows_total += len(getattr(block, "labels", []) or [])
            series = getattr(block, "series", []) or []
            if len(series) > 8 or any(len(s.values) != len(block.labels) for s in series):
                return "ui 图表数据长度不合法（series 数量或 values 长度与 labels 不一致）"
        elif btype == "table":
            rows = getattr(block, "rows", []) or []
            cols = len(getattr(block, "columns", []) or [])
            rows_total += len(rows)
            if any(len(row) != cols for row in rows):
                return "ui 表格某行长度与列数不一致"
            if cols > 20:
                return "ui 表格列数超过上限（20）"
        elif btype == "form":
            fields = getattr(block, "fields", []) or []
            if len(fields) > UI_MAX_FIELDS:
                return f"ui 表单字段超过上限（{UI_MAX_FIELDS} 个）"
            if len({f.name for f in fields}) != len(fields):
                return "ui 表单字段 name 重复"
        elif btype == "file_list":
            files = getattr(block, "files", []) or []
            if len(files) > UI_MAX_FILES:
                return f"ui 文件清单条目超过上限（{UI_MAX_FILES} 条）"
            rows_total += len(files)
    if rows_total > UI_MAX_DATA_ROWS:
        return f"ui 数据行数超过上限（{UI_MAX_DATA_ROWS} 行），请精简后再输出"
    return None


def _invalid_reply_detail(output: object) -> str | None:
    """输出能解析成 JSON/对象但 AgentReply 字段校验不过时，返回第一条精确错误。"""
    try:
        if isinstance(output, str):
            obj = json.loads(_extract_json_object(output))
        elif isinstance(output, dict):
            obj = output
        else:
            return None
    except Exception:
        return None
    try:
        AgentReply.model_validate(obj)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            err = errors[0]
            loc = ".".join(str(x) for x in err.get("loc", []))
            return f"字段 {loc}：{str(err.get('msg', ''))[:120]}"
        return str(exc)[:200]
    except Exception as exc:
        return str(exc)[:200]
    return None


def _raw_check_output(output: object) -> str | None:
    """输出安全审查：格式问题一律交给 ReplyParser 宽恕处理；
    这里只拦截真正的安全风险（密钥泄露 / 完全无可用输出）。"""
    from runtime.reply_parser import parse

    result = parse(output)
    if not result.ok:
        return "模型没有产生任何可用回答"
    canonical = result.canonical
    combined = "\n".join(
        [str(canonical.get("summary") or ""), str(canonical.get("content") or ""),
         str(canonical.get("saved_file") or ""), *[str(q) for q in (canonical.get("questions") or [])]]
    )
    if _has_secret(combined):
        return "输出疑似包含 API Key/密钥，已拦截"
    return None


def check_output(output: object) -> str | None:
    """输出校验入口：失败时把原始输出样本落盘后返回原因（便于排查格式问题）。"""
    reason = _raw_check_output(output)
    if reason is not None:
        try:
            _log_guardrail_failure(output, reason)
        except Exception:
            pass
    return reason


@input_guardrail(run_in_parallel=False)
async def safety_input_guardrail(
    ctx: RunContextWrapper[None],
    agent: Agent,
    input_value: str | list[TResponseInputItem],
) -> GuardrailFunctionOutput:
    """输入安全闸：只扫「用户最新一条输入」（不扫历史，避免 FORGE 自己的历史回答造成永久误拦）。"""
    reason = scan_input(_last_user_text(input_value))
    return GuardrailFunctionOutput(
        tripwire_triggered=reason is not None,
        output_info={"reason": reason} if reason else None,
    )


@output_guardrail
async def reply_integrity_guardrail(
    ctx: RunContextWrapper[None],
    agent: Agent,
    output: object,
) -> GuardrailFunctionOutput:
    """输出安全闸：仅拦截安全风险（密钥/无可用输出）。
    格式问题（缺字段/别名/fence/前后说明等）由 ReplyParser 容错修复，不再触发 tripwire。"""
    reason = check_output(output)
    return GuardrailFunctionOutput(
        tripwire_triggered=reason is not None,
        output_info={"reason": reason} if reason else None,
    )
