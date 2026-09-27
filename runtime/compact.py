"""长会话自动摘要（第三方 OpenAI 兼容网关友好版）。

SDK 自带的 OpenAIResponsesCompactionSession 依赖官方 responses.compact 接口，
兼容网关（chat/completions）用不了，所以这里自己实现：

每轮对话结束后检查该会话积累的消息量，超过阈值时把较早的对话交给同一个模型
压缩成一段中文摘要，写成一条 system 消息放到会话历史开头，并保留最近若干轮
原文。上下文因此始终有界，重点信息（需求目标、关键决定、已保存的文件、待办）又不丢。
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from runtime_paths import PROJECT_ROOT, RUNTIME_ROOT

BASE_DIR = PROJECT_ROOT
load_dotenv(BASE_DIR / ".env")

SUMMARY_DIR = RUNTIME_ROOT / "summaries"

# 长会话自动摘要阈值（激进档默认：贴近 256K 上下文才触发；可用 .env 覆盖：
# AUTO_SUMMARY_MIN_TURNS / AUTO_SUMMARY_TRIGGER_TURNS / AUTO_SUMMARY_TRIGGER_CHARS /
# AUTO_SUMMARY_KEEP_TURNS / AUTO_SUMMARY_TRANSCRIPT_CAP）
MIN_USER_TURNS = 6          # 至少积累这么多轮用户输入才考虑摘要
TRIGGER_USER_TURNS = 60     # 用户输入轮数达到这个值即触发（或正文字数达标）
TRIGGER_CHARS = 180000      # 或对话（含工具输出，粗略估算）总字符数超过这个值也触发
KEEP_USER_TURNS = 20        # 摘要后保留最近几轮原文
PER_MSG_CAP = 1600          # 单条消息进入摘要材料的长度上限
TRANSCRIPT_CAP = 150000     # 一次发给模型做摘要的材料总长度上限
SUMMARY_MAX_CHARS = 1600

#: 最近一次摘要模型失败的分类信息（供 compaction 事件如实记录 provider 原因）
_LAST_SUMMARY_ERROR: dict[str, str] = {}


def last_summary_error() -> dict[str, str]:
    """返回最近一次摘要失败的 {kind, public}（无失败或非 provider 失败时为空 dict）。"""
    return dict(_LAST_SUMMARY_ERROR)


def _refresh_thresholds() -> None:
    """每次检查前从 .env 刷新阈值（不写死，便于按网关上下文调档）。"""
    global MIN_USER_TURNS, TRIGGER_USER_TURNS, TRIGGER_CHARS, KEEP_USER_TURNS, TRANSCRIPT_CAP
    MIN_USER_TURNS = max(1, int(os.getenv("AUTO_SUMMARY_MIN_TURNS", str(MIN_USER_TURNS))))
    TRIGGER_USER_TURNS = max(1, int(os.getenv("AUTO_SUMMARY_TRIGGER_TURNS", str(TRIGGER_USER_TURNS))))
    TRIGGER_CHARS = max(1000, int(os.getenv("AUTO_SUMMARY_TRIGGER_CHARS", str(TRIGGER_CHARS))))
    KEEP_USER_TURNS = max(1, int(os.getenv("AUTO_SUMMARY_KEEP_TURNS", str(KEEP_USER_TURNS))))
    TRANSCRIPT_CAP = max(20000, int(os.getenv("AUTO_SUMMARY_TRANSCRIPT_CAP", str(TRANSCRIPT_CAP))))

_SUMMARY_MARK = "[此前对话的自动摘要"

#: 压缩开篇语（参考库《Practical Guide to Context Engineering》：ClaudeCode 压缩后
#: 必须加一句"上下文已压缩、可无缝继续"的开篇语，让模型续接时知道这是压缩态、
#: 关键信息已结构化保留，避免把摘要当成"完整历史"而重复执行已完成步骤）。
_SUMMARY_OPENING = (
    "上下文已使用结构化 8 节算法压缩。所有必要信息已保留，可无缝继续对话。"
    "不要重复执行已完成步骤；如需某条被压缩掉的细节，请重新调用对应工具。"
)

# P0-B-2（上下文工程三层之层 2）：把 _SUMMARY_SYSTEM 从「自由散文摘要」升级为
# 参考库 ClaudeCode 的 8 节 state_snapshot 结构（对齐《Practical Guide to Context
# Engineering》上下文压缩指令篇）。用 XML 而非 JSON（Claude 模型对 XML 标签友好），
# 8 节对应：任务意图 / 技术上下文 / 文件与代码变更 / 错误与修复 / 当前状态 /
# 待处理任务 / 用户偏好 / 关键决策。信息密集、省略客套与重复、保留实体名。
_SUMMARY_SYSTEM = """你是会话压缩器。请把下面这段"用户与助手"的旧对话，压缩成一份结构化的中文 8 节 state_snapshot，供之后无缝续接时使用。

要求：
1. 严格按下列 8 节输出，每节用 XML 标签包裹（Claude 模型对 XML 更友好，不要改用 JSON）；
   某节无内容时保留标签并写"（无）"，不要省略标签：
   <task_intent>用户的明确请求与总体目标（一句话高层 + 具体意图）</task_intent>
   <tech_context>技术栈 / 框架 / 包管理器 / 环境约定（用于重建开发环境的关键事实）</tech_context>
   <code_changes>检查、修改或创建过的具体文件与代码片段（文件名 + 改动摘要，保留关键签名/路径）</code_changes>
   <errors_fixes>遇到的错误及修复方式；用户的纠正反馈（用户说"以不同方式做"的尤其要留）</errors_fixes>
   <current_status>当前任务进度：已完成哪些步骤、现在停在哪一步（避免压缩后重复执行）</current_status>
   <pending_tasks>尚未完成的待办任务，按优先级列出</pending_tasks>
   <user_preferences>用户的工作偏好 / 沟通方式 / 工具习惯 / 测试覆盖率要求等（本项目工作记忆）</user_preferences>
   <key_decisions>已拍板的关键决策（防止项目方向丢失）</key_decisions>
2. 省略：客套话、重复解释、纯过程性对话填充；但保留所有实体名（文件 / 函数 / 工具 / 路径 / 数值）；
3. 信息要极其密集，全文控制在 1200 字以内；如果之前已经有过摘要（"先前摘要"），先概括旧摘要要点再补充新内容；
4. 只输出 <state_snapshot>...</state_snapshot> 一个 XML 块，不要额外解释、不要加其它标签。
"""


def _model_name() -> str:
    model = os.getenv("AGENT_MODEL")
    if not model:
        raise ValueError("自动摘要需要 .env 里配置 AGENT_MODEL")
    return model


def _openai_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        timeout=90,
    )


def _item_text(item: Any) -> str:
    """提取消息类 item 的纯文本内容（兼容 dict 与对象、字符串与分段列表）。"""
    content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("content"), str):
                    parts.append(part["content"])
            else:
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def _item_role(item: Any) -> str:
    return item.get("role", "") if isinstance(item, dict) else getattr(item, "role", "")


def _item_rough_chars(item: Any) -> int:
    """粗略估算一条历史记录的字符开销（含工具调用参数/输出，防超上下文）。

    正文直接计数；function_call 等无 content 的记录，把参数/输出文本按上限计入。
    """
    text = _item_text(item)
    if text:
        return len(text)
    if isinstance(item, dict):
        for key in ("arguments", "output", "input"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return min(len(value), 2000)
    else:
        for key in ("arguments", "output", "input"):
            try:
                value = getattr(item, key, None)
            except Exception:
                continue
            if isinstance(value, str) and value:
                return min(len(value), 2000)
    return 0


def _is_user_message(item: Any) -> bool:
    return _item_role(item) == "user" and bool(_item_text(item).strip())


def _is_previous_summary(item: Any) -> bool:
    return _item_role(item) == "system" and _SUMMARY_MARK in _item_text(item)


def _build_transcript(items: list[Any], max_chars: int = TRANSCRIPT_CAP) -> str:
    """把需要摘要的旧消息整理成给模型的纯文本对话记录。"""
    lines: list[str] = []
    total = 0
    truncated = False
    for item in items:
        role = _item_role(item)
        text = _item_text(item).strip()
        if not text:
            continue
        if _is_previous_summary(item):
            label = "先前摘要"
        elif role == "user":
            label = "用户"
        elif role == "assistant":
            label = "助手"
        else:
            continue
        if len(text) > PER_MSG_CAP:
            text = text[:PER_MSG_CAP] + "……"
        segment = f"{label}: {text}"
        if total + len(segment) > max_chars:
            truncated = True
            break
        lines.append(segment)
        total += len(segment)
    if truncated:
        lines.append("（历史较长，以上只截取了较早的一部分；摘要请尽量保留已出现的关键信息）")
    return "\n\n".join(lines)


def summarize_transcript(transcript: str) -> str:
    """调用当前网关模型把对话记录压成中文摘要。"""
    from runtime.provider_errors import call_with_provider_retry

    client = _openai_client()
    response = call_with_provider_retry(
        lambda: client.chat.completions.create(
            model=_model_name(),
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": transcript},
            ],
            temperature=0.2,
            max_tokens=SUMMARY_MAX_CHARS * 2,
        )
    )
    summary = (response.choices[0].message.content or "").strip()
    if not summary:
        raise RuntimeError("摘要模型返回了空内容")
    return summary[:SUMMARY_MAX_CHARS]


def _summary_item(summary: str) -> dict[str, str]:
    """把压缩产物写成一条 system 历史项。

    P0-B-2：按参考库《Practical Guide to Context Engineering》，压缩产物前必须加
    一句「开篇语」（_SUMMARY_OPENING），让模型续接时知道这是结构化压缩态、
    关键信息已保留、不要重复执行已完成步骤。开篇语 + 摘要 一起构成「新一轮
    对话的上下文」，减少断片率。
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return {
        "role": "system",
        "content": f"{_SUMMARY_MARK} · {stamp}]\n{_SUMMARY_OPENING}\n{summary}",
    }


def _append_summary_file(session_id: str, summary: str) -> Path:
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", session_id).strip("_") or "session"
    path = SUMMARY_DIR / f"{safe_name}.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## 自动摘要 · {stamp}\n\n{summary}\n")
    return path


async def maybe_compact(session: Any) -> str | None:
    """检查并执行一次会话压缩；返回本次生成的摘要文本，未触发/失败返回 None。

    触发后会把较早历史替换成一条 system 摘要，保留最近 KEEP_USER_TURNS 轮原文，
    并把摘要追加写到 summaries/ 目录方便回溯。任一步失败都回滚，不丢历史。
    """
    summary, _status = await compact_history(session)
    return summary


async def compact_history(session: Any) -> tuple[str | None, str]:
    """Run 前 Session Preparation 用：与 maybe_compact 同一判定与持久化，但支持
    超长历史（1547+ 条）的**分块摘要 + 分层合并**，避免把整段历史一次塞给摘要模型。

    返回 (summary_text, status)：
    - status = "compacted"：成功，旧历史已被 [摘要 + 最近 KEEP 轮原文] 替换；
    - status = "skipped"：历史未达阈值 / 没有可摘要内容；
    - status = "failed"：摘要模型失败或替换失败——原 Session 保持可用（回滚）。
    """
    try:
        items = await session.get_items(limit=10_000_000)
    except Exception:
        return None, "failed"
    if not items:
        return None, "skipped"

    _refresh_thresholds()
    user_indices = [i for i, item in enumerate(items) if _is_user_message(item)]
    turns = len(user_indices)
    total_chars = sum(_item_rough_chars(item) for item in items)
    if turns < MIN_USER_TURNS:
        return None, "skipped"
    if turns < TRIGGER_USER_TURNS and total_chars < TRIGGER_CHARS:
        return None, "skipped"
    if turns <= KEEP_USER_TURNS:
        return None, "skipped"

    boundary = user_indices[-KEEP_USER_TURNS]
    old_items = items[:boundary]
    tail_items = items[boundary:]

    chunks = _split_transcript_chunks(old_items, max_chars=TRANSCRIPT_CAP)
    if not chunks:
        return None, "skipped"

    summaries: list[str] = []
    for chunk in chunks:
        transcript = _build_transcript(chunk, max_chars=TRANSCRIPT_CAP)
        if not transcript.strip():
            continue
        try:
            # Phase 2：摘要模型调用放到线程执行，避免阻塞事件循环（Web 并发不互相卡）
            summaries.append(await asyncio.to_thread(summarize_transcript, transcript))
        except Exception as exc:
            _LAST_SUMMARY_ERROR.clear()
            kind = getattr(exc, "provider_kind", None)
            public = getattr(exc, "provider_public", None)
            if kind:
                _LAST_SUMMARY_ERROR.update(kind=str(kind), public=str(public or ""))
            return None, "failed"
    if not summaries:
        return None, "skipped"

    final = await _merge_summaries(summaries)

    new_items = [_summary_item(final)] + tail_items
    if not await _atomic_replace(session, items, new_items):
        return None, "failed"
    try:
        _append_summary_file(session.session_id, final)
    except Exception:
        pass
    return final, "compacted"


def _split_transcript_chunks(items: list[Any], max_chars: int) -> list[list[Any]]:
    """把待摘要的旧消息按可送入模型的字符预算切成若干块（每块再按 单条/块 上限裁剪）。

    与 _build_transcript 同一套长度口径：单条文本先按 PER_MSG_CAP 截断再计入。
    """
    _refresh_thresholds()
    segments: list[tuple[int, Any]] = []
    for item in items:
        role = _item_role(item)
        text = _item_text(item).strip()
        if not text:
            continue
        if _is_previous_summary(item):
            label = "先前摘要"
        elif role == "user":
            label = "用户"
        elif role == "assistant":
            label = "助手"
        else:
            continue
        if len(text) > PER_MSG_CAP:
            text = text[:PER_MSG_CAP] + "……"
        segments.append((len(f"{label}: {text}") + 2, item))
    chunks: list[list[Any]] = []
    current: list[Any] = []
    current_len = 0
    for seg_len, item in segments:
        if current and current_len + seg_len > max_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(item)
        current_len += seg_len
    if current:
        chunks.append(current)
    return chunks


async def _merge_summaries(summaries: list[str]) -> str:
    """多块摘要合并成一个有界摘要（不 append 无限增长；旧摘要随分块被重新压缩）。"""
    if len(summaries) == 1:
        return summaries[0][:SUMMARY_MAX_CHARS]
    text = "\n".join(
        f"分段{i + 1}:\n{s[:SUMMARY_MAX_CHARS]}" for i, s in enumerate(summaries)
    )
    if len(text) <= SUMMARY_MAX_CHARS * 2:
        return text[:SUMMARY_MAX_CHARS]
    try:
        merged = await asyncio.to_thread(
            summarize_transcript,
            "下面是同一段长对话的分段摘要，请合并成一份有界总摘要：\n\n" + text,
        )
        if merged.strip():
            return merged[:SUMMARY_MAX_CHARS]
    except Exception:
        pass
    return text[:SUMMARY_MAX_CHARS]


async def _atomic_replace(session: Any, original_items: list[Any], new_items: list[Any]) -> bool:
    """先写新历史，失败则回滚原历史；成功返回 True。"""
    try:
        await session.clear_session()
        await session.add_items(new_items)
        return True
    except Exception:
        try:
            await session.clear_session()
            await session.add_items(original_items)
        except Exception:
            pass
        return False
