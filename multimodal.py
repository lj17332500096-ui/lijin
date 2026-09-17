"""多模态问答工具：让 Agent 能"看"图片/截图并回答。

实现：把工作区内的图片以 data URL 形式喂给当前网关的多模态模型
（chat/completions + image_url，配 VISION_MODEL 指定，默认同 AGENT_MODEL），
模型看到图后把文字回答返回给 Agent 循环。全程不出本机、不落盘。
"""

import base64
import os
from pathlib import Path

from agents import function_tool
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}

_SYSTEM_PROMPT = (
    "你是「全能助手」的图片理解子能力。用户或主 Agent 会发来一张图片和相关问题。"
    "请仔细观察图片内容后如实回答：描述画面、识别文字（截图/文档/表格里的字要逐字读出）、"
    "回答关于图片的任何问题。看不到或不确定的地方要直说，不要编造。默认用中文回答，"
    "回答保持简洁（通常 300 字以内）。只输出回答正文，不要加前缀。"
)


def _resolve_image_path(path_str: str) -> Path:
    """把用户给的图片路径解析到工作区内；越界/不存在会抛 ValueError。"""
    from tools import _is_protected, _resolve_under_root

    target = _resolve_under_root(path_str)
    if target is None:
        raise ValueError(f"只能读取工作区内的文件：{path_str}")
    if _is_protected(target):
        raise ValueError("出于安全考虑，这个文件不允许读取")
    if not target.exists() or not target.is_file():
        raise ValueError(f"找不到文件：{target}")
    return target


def _vision_model() -> str:
    return os.getenv("VISION_MODEL") or os.getenv("AGENT_MODEL") or ""


def _ask_model_with_image(image_path: Path, question: str, timeout: int = 90) -> str:
    from openai import OpenAI
    from runtime.provider_errors import call_with_provider_retry

    suffix = image_path.suffix.lower()
    data = image_path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB，暂不支持：{image_path.name}")
    data_url = f"data:{_IMAGE_MIME.get(suffix, 'image/png')};base64," + base64.b64encode(data).decode()

    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        timeout=timeout,
    )
    response = call_with_provider_retry(
        lambda: client.chat.completions.create(
            model=_vision_model(),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"图片文件：{image_path}\n问题：{question}"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            max_tokens=1500,
            temperature=0.2,
        )
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise ValueError(
            "图片模型没有返回内容（可能是推理型模型把额度用尽），"
            "可稍后重试或在 .env 里用 VISION_MODEL 换一个多模态模型"
        )
    return content


@function_tool
def ask_image(image_path: str, question: str) -> str:
    """查看一张图片/截图并回答问题（多模态问答）。
    image_path 是工作区内的图片路径（png/jpg/webp 等，可用绝对路径）；
    question 是针对图片的具体问题，如“图里写了什么字”“表格第 2 列是什么”。
    用于看截图、图表、照片、扫描件（配合 PDF 页面截图）等。"""
    question = (question or "").strip()
    if not question:
        return "错误：问题不能为空。"
    if not _vision_model():
        return "错误：.env 里没有配置模型（VISION_MODEL 或 AGENT_MODEL）。"
    try:
        path = _resolve_image_path(image_path)
    except ValueError as exc:
        return f"错误：{exc}"
    try:
        answer = _ask_model_with_image(path, question)
    except Exception as exc:
        from runtime.provider_errors import provider_public_text

        public = provider_public_text(exc)
        if public:
            return f"看图失败：{public}"
        return f"看图失败：{type(exc).__name__}: {str(exc)[:300]}"
    from runtime.trust import tag

    return tag(
        "图片视觉识别",
        str(path),
        f"问题：{question}\n模型回答：{answer}",
        max_len=6000,
    )
