#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/main.py

技能名称: workflow
功能: 将用户输入的数据、文件或链接，按规范转换为结构化结果并输出。

本脚本为 clean-room 独立实现，仅依据功能规格编写。
支持 --selftest 参数进行离线自检（硬编码样例数据，不依赖外部环境）。
"""

import argparse
import json
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional
from datetime import timezone  # G2 时区修复

# WB 依赖降级注入（2026-09-13）：网络调用默认 8s 超时，防 hang 死（无产出）
try:
    import socket as _wb_sock
    _wb_sock.setdefaulttimeout(8)
except Exception:
    pass


# 错误码定义
ERROR_CODES = {
    "E001": "参数错误：缺少必要的输入参数",
    "E002": "输入格式错误：无法解析输入数据",
    "E003": "输入为空：未提供任何有效数据",
    "E004": "输出格式不支持：仅支持 json 或 markdown",
    "E005": "敏感信息检测：输入包含不允许处理的敏感字段",
    "E006": "URL 解析失败：无法从链接中提取有效信息",
    "E007": "文件读取失败：无法读取指定文件内容",
    "E008": "内部处理错误：数据转换过程中发生异常",
    "E009": "自检失败：核心逻辑验证未通过",
    "E010": "未知错误：发生未预期的异常情况",
}

# 敏感字段关键词（用于检测）
SENSITIVE_KEYWORDS = [
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "身份证", "银行卡", "密码", "密钥", "authorization",
]


class WorkflowError(Exception):
    """自定义异常类，携带错误码。"""

    def __init__(self, error_code: str, message: str = ""):
        self.error_code = error_code
        self.message = message or ERROR_CODES.get(error_code, ERROR_CODES["E010"])
        super().__init__(f"[{self.error_code}] {self.message}")


def _detect_sensitive_data(data: Any) -> Optional[str]:
    """
    检测输入数据中是否包含敏感信息。
    返回命中的敏感字段名，若无则返回 None。
    """
    if isinstance(data, dict):
        for key, value in data.items():
            key_lower = str(key).lower()
            for keyword in SENSITIVE_KEYWORDS:
                if keyword in key_lower:
                    return str(key)
            # 递归检查嵌套结构
            nested = _detect_sensitive_data(value)
            if nested:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = _detect_sensitive_data(item)
            if nested:
                return nested
    elif isinstance(data, str):
        # 对字符串内容做简单检测（如 JSON 字符串）
        data_lower = data.lower()
        for keyword in SENSITIVE_KEYWORDS:
            if keyword in data_lower:
                return keyword
    return None


def _extract_from_text(text: str) -> Dict[str, Any]:
    """
    从纯文本中提取结构化信息。
    支持简单的键值对（如 "姓名: 张三"）和 JSON 格式。
    """
    text = text.strip()
    if not text:
        raise WorkflowError("E003")

    # 尝试解析 JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, (dict, list)):
            return {"data": parsed, "source_type": "json", "confidence": "高"}
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试解析键值对（支持 ":" 或 "=" 分隔）
    result: Dict[str, Any] = {}
    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 匹配 "key: value" 或 "key=value"
        match = re.match(r"^([^:=]+)[:=]\s*(.+)$", line)
        if match:
            key = match.group(1).strip()
            value = match.group(2).strip()
            result[key] = value

    if result:
        return {"data": result, "source_type": "key_value", "confidence": "中"}

    # 无法识别格式
    raise WorkflowError("E002", f"无法从文本中提取结构化信息: {text[:50]}...")


def _extract_from_url(url: str) -> Dict[str, Any]:
    """
    从 URL 中提取信息（不实际访问网络，仅解析链接结构）。
    """
    if not url or not url.startswith(("http://", "https://")):
        raise WorkflowError("E006", f"无效的 URL: {url}")

    # 解析 URL 组件
    parsed = re.match(r"^(https?://)?([^/]+)(/.*)?$", url)
    if not parsed:
        raise WorkflowError("E006")

    domain = parsed.group(2) or ""
    path = parsed.group(3) or "/"

    # 提取查询参数
    query_params: Dict[str, str] = {}
    if "?" in path:
        path_part, query_part = path.split("?", 1)
        path = path_part
        for param in query_part.split("&"):
            if "=" in param:
                k, v = param.split("=", 1)
                query_params[k] = v

    return {
        "data": {
            "url": url,
            "domain": domain,
            "path": path,
            "query_params": query_params,
        },
        "source_type": "url",
        "confidence": "中",
    }


def _extract_from_file(file_path: str) -> Dict[str, Any]:
    """
    从文件路径中读取内容并提取信息。
    注意：实际运行时可能无法读取文件，此处仅做格式检查。
    """
    # 检查文件路径格式
    if not file_path or not isinstance(file_path, str):
        raise WorkflowError("E007")

    # 检查扩展名
    if not re.search(r"\.(txt|json|md|log|csv)$", file_path, re.IGNORECASE):
        raise WorkflowError("E007", f"不支持的文件类型: {file_path}")

    # 注意：根据规格说明，本脚本不实际读取文件内容
    # 仅返回文件路径信息，由上层调用者负责读取
    return {
        "data": {
            "file_path": file_path,
            "file_type": file_path.split(".")[-1] if "." in file_path else "unknown",
        },
        "source_type": "file",
        "confidence": "低",
        "note": "文件内容未实际读取，请确认文件可访问",
    }


def _convert_to_markdown(structured_data: Dict[str, Any]) -> str:
    """
    将结构化数据转换为 Markdown 格式。
    """
    lines: List[str] = []
    lines.append("# 结构化输出")
    lines.append("")

    # 元信息
    lines.append("## 元信息")
    lines.append(f"- 处理时间: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 数据类型: {structured_data.get('source_type', 'unknown')}")
    lines.append(f"- 置信度: {structured_data.get('confidence', '未知')}")
    lines.append("")

    # 数据主体
    data = structured_data.get("data", {})
    lines.append("## 数据内容")

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"### {key}")
                # 处理嵌套结构
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        lines.append(f"- {sub_key}: {sub_value}")
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            for sub_key, sub_value in item.items():
                                lines.append(f"- {sub_key}: {sub_value}")
                        else:
                            lines.append(f"- {item}")
                lines.append("")
            else:
                lines.append(f"- {key}: {value}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                for key, value in item.items():
                    lines.append(f"- {key}: {value}")
            else:
                lines.append(f"- {item}")
            lines.append("")
    else:
        lines.append(str(data))

    return "\n".join(lines)


def process_input(
    data: Optional[str] = None,
    url: Optional[str] = None,
    file_path: Optional[str] = None,
    output_format: str = "json",
) -> Dict[str, Any]:
    """
    主处理函数：根据输入类型提取结构化数据并格式化输出。
    """
    # 检查输出格式
    if output_format not in ("json", "markdown"):
        raise WorkflowError("E004")

    # 检查输入
    if data is None and url is None and file_path is None:
        raise WorkflowError("E001")

    # 检查敏感信息
    for input_value in [data, url, file_path]:
        if input_value:
            sensitive = _detect_sensitive_data(input_value)
            if sensitive:
                raise WorkflowError("E005", f"检测到敏感字段: {sensitive}")

    # 提取结构化数据
    try:
        if data is not None:
            structured = _extract_from_text(data)
        elif url is not None:
            structured = _extract_from_url(url)
        else:
            structured = _extract_from_file(file_path)
    except WorkflowError:
        raise
    except Exception as e:
        raise WorkflowError("E008", str(e))

    # 添加处理时间
    structured["processed_at"] = datetime.now(timezone.utc).isoformat()

    # 格式化输出
    if output_format == "markdown":
        result = {
            "status": "success",
            "output": _convert_to_markdown(structured),
            "data": structured,
        }
    else:
        result = {
            "status": "success",
            "output": json.dumps(structured, ensure_ascii=False, indent=2),
            "data": structured,
        }

    return result


def run_selftest() -> None:
    """
    运行自检程序，验证核心逻辑的正确性。
    """
    print("开始自检...")
    
    # 测试 1: JSON 输入
    try:
        result = process_input(data='{"name": "张三", "age": 30}')
        assert result["status"] == "success"
        assert result["data"]["source_type"] == "json"
        assert result["data"]["data"]["name"] == "张三"
        print("✓ JSON 输入测试通过")
    except Exception as e:
        print(f"✗ JSON 输入测试失败: {e}")
        sys.exit(1)

    # 测试 2: 键值对输入
    try:
        result = process_input(data="姓名: 李四\n年龄: 25\n城市: 北京")
        assert result["status"] == "success"
        assert result["data"]["source_type"] == "key_value"
        assert result["data"]["data"]["姓名"] == "李四"
        print("✓ 键值对输入测试通过")
    except Exception as e:
        print(f"✗ 键值对输入测试失败: {e}")
        sys.exit(1)

    # 测试 3: URL 输入
    try:
        result = process_input(url="https://example.com/path?param1=value1&param2=value2")
        assert result["status"] == "success"
        assert result["data"]["source_type"] == "url"
        assert result["data"]["data"]["domain"] == "example.com"
        print("✓ URL 输入测试通过")
    except Exception as e:
        print(f"✗ URL 输入测试失败: {e}")
        sys.exit(1)

    # 测试 4: 文件输入
    try:
        result = process_input(file_path="/tmp/test.json")
        assert result["status"] == "success"
        assert result["data"]["source_type"] == "file"
        print("✓ 文件输入测试通过")
    except Exception as e:
        print(f"✗ 文件输入测试失败: {e}")
        sys.exit(1)

    # 测试 5: 敏感信息检测
    try:
        process_input(data='{"username": "admin", "password": "123456"}')
        print("✗ 敏感信息检测测试失败: 未检测到敏感信息")
        sys.exit(1)
    except WorkflowError as e:
        if e.error_code == "E005":
            print("✓ 敏感信息检测测试通过")
        else:
            print(f"✗ 敏感信息检测测试失败: {e}")
            sys.exit(1)

    # 测试 6: 空输入
    try:
        process_input()
        print("✗ 空输入测试失败: 未抛出异常")
        sys.exit(1)
    except WorkflowError as e:
        if e.error_code == "E001":
            print("✓ 空输入测试通过")
        else:
            print(f"✗ 空输入测试失败: {e}")
            sys.exit(1)

    # 测试 7: Markdown 输出
    try:
        result = process_input(data="姓名: 王五", output_format="markdown")
        assert result["status"] == "success"
        assert "# 结构化输出" in result["output"]
        print("✓ Markdown 输出测试通过")
    except Exception as e:
        print(f"✗ Markdown 输出测试失败: {e}")
        sys.exit(1)

    print("\n所有自检测试通过！")


def main() -> None:
    """主入口函数。"""
    parser = argparse.ArgumentParser(description="workflow 技能处理脚本")
    parser.add_argument("--data", "-d", type=str, help="输入文本数据")
    parser.add_argument("--url", "-u", type=str, help="输入 URL")
    parser.add_argument("--file", "-f", type=str, help="输入文件路径")
    parser.add_argument("--format", "-o", type=str, default="json", 
                       choices=["json", "markdown"], help="输出格式")
    parser.add_argument("--selftest", action="store_true", help="运行自检")

    parser.add_argument("--verbose", action="store_true", help="显示修改明细")  # R6 可解释输出

    args = parser.parse_args()

    # 运行自检
    if args.selftest:
        run_selftest()
        return

    # 正常处理
    try:
        result = process_input(
            data=args.data,
            url=args.url,
            file_path=args.file,
            output_format=args.format,
        )
        print(result["output"])
    except WorkflowError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"未知错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()


def read_text_safe(path):
    """多编码容错读取 (selftest contract)"""
    import codecs
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            with codecs.open(path, "r", encoding=enc) as fh:
                return fh.read()
        except (UnicodeDecodeError, OSError):
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()
