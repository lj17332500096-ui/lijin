"""语音对话（Windows 本机能力，不依赖任何模型网关）。

语音输入：System.Speech 桌面识别（zh-CN dictation）→ 文字；
语音输出：System.Speech 语音合成（优先中文语音）。
通过 PowerShell 调用，全部在本机完成，不消耗 API。
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from pathlib import Path

POWERSHELL = os.environ.get(
    "POWERSHELL_PATH",
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
)

_POWERSHELL_PREFIX = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Add-Type -AssemblyName System.Speech; "
)


def _run_powershell(script: str, timeout: float = 45.0) -> tuple[str, str, int]:
    """把 PowerShell 脚本以 UTF-16 base64 传给 powershell.exe，避免引号转义问题。"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        timeout=timeout,
        creationflags=creationflags,
    )
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    return out, err, proc.returncode


def _recognition_script(language: str = "zh-CN", timeout_seconds: float = 10.0) -> str:
    cultures = []
    for candidate in (language, "zh-CN", "en-US"):
        if candidate not in cultures:
            cultures.append(candidate)
    culture_list = ", ".join(f"'{c}'" for c in cultures)
    return f"""
{_POWERSHELL_PREFIX}
$chosen = $null
foreach ($name in @({culture_list})) {{
  try {{
    $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine(
        (New-Object System.Globalization.CultureInfo($name)))
    $chosen = $engine
    break
  }} catch {{ }}
}}
if ($chosen -eq $null) {{
  Write-Output '__VOICE_ERR__: 没有可用的语音识别引擎'
  exit 1
}}
try {{
  $chosen.SetInputToDefaultAudioDevice()
}} catch {{
  Write-Output ('__VOICE_ERR__: 无法访问麦克风：' + $_.Exception.Message)
  exit 1
}}
$chosen.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
try {{
  $timeout = [TimeSpan]::FromSeconds({float(timeout_seconds)})
  $result = $chosen.Recognize($timeout)
  if ($result -ne $null) {{
    Write-Output $result.Text
  }}
}} catch {{
  Write-Output ('__VOICE_ERR__: ' + $_.Exception.Message)
}} finally {{
  $chosen.Dispose()
}}
"""


def _ps_single_quote(text: str) -> str:
    """把文本转成 PowerShell 单引号字符串字面量。"""
    return "'" + text.replace("'", "''") + "'"


def _tts_script(text: str) -> str:
    return f"""
{_POWERSHELL_PREFIX}
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{
  $synth.Rate = 0
  foreach ($v in $synth.GetInstalledVoices()) {{
    if ($v.Enabled -and $v.VoiceInfo.Culture.Name.StartsWith('zh')) {{
      $synth.SelectVoice($v.VoiceInfo.Name)
      break
    }}
  }}
  $synth.Speak({_ps_single_quote(text)})
  Write-Output 'OK'
}} catch {{
  Write-Output ('__TTS_ERR__: ' + $_.Exception.Message)
}} finally {{
  $synth.Dispose()
}}
"""


def listen_once(timeout_seconds: float = 10.0, language: str = "zh-CN") -> str:
    """从麦克风听一句话，返回识别文本；没听到/出错返回空字符串。"""
    try:
        out, err, _code = _run_powershell(
            _recognition_script(language, timeout_seconds),
            timeout=timeout_seconds + 20,
        )
    except subprocess.TimeoutExpired:
        return ""
    except OSError as exc:
        print(f"（语音识别不可用：{exc}）")
        return ""
    if "__VOICE_ERR__" in out or "__VOICE_ERR__" in err:
        message = (out or err).split("__VOICE_ERR__:", 1)[-1].strip()
        print(f"（语音识别不可用：{message}）")
        return ""
    text = (out.splitlines()[-1] if out else "").strip()
    return text


def check_microphone(language: str = "zh-CN") -> bool:
    """探测麦克风是否可用（只做引擎创建 + 默认输入设备绑定，不等待语音）。"""
    script = f"""
{_POWERSHELL_PREFIX}
$chosen = $null
foreach ($name in @('{language}', 'zh-CN')) {{
  try {{
    $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine(
        (New-Object System.Globalization.CultureInfo($name)))
    $chosen = $engine
    break
  }} catch {{ }}
}}
if ($chosen -eq $null) {{
  Write-Output '__VOICE_ERR__: no recognizer'
  exit 1
}}
try {{
  $chosen.SetInputToDefaultAudioDevice()
  Write-Output 'MIC_OK'
}} catch {{
  Write-Output ('__VOICE_ERR__: ' + $_.Exception.Message)
  exit 1
}} finally {{
  $chosen.Dispose()
}}
"""
    try:
        out, _err, _code = _run_powershell(script, timeout=20.0)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return "MIC_OK" in out


def _strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", "，", text, flags=re.S)
    text = re.sub(r"[*_>`#~\[\]()]", "", text)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\s*[-•·]\s+", "，", text)
    text = re.sub(r"https?://\S+", "链接", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ，。、；：")


def _split_for_speech(text: str, max_len: int = 160) -> list[str]:
    """按标点把长文本切成适合朗读的短句。"""
    text = _strip_markdown(text)
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;，,])", text)
    chunks: list[str] = []
    buffer = ""
    for part in parts:
        if not part:
            continue
        if len(buffer) + len(part) <= max_len:
            buffer += part
        else:
            if buffer:
                chunks.append(buffer)
            while len(part) > max_len:
                chunks.append(part[:max_len])
                part = part[max_len:]
            buffer = part
    if buffer:
        chunks.append(buffer)
    return [c for c in chunks if c.strip()]


def speak(text: str) -> bool:
    """把文本朗读出来（自动切成短句，优先中文语音）；成功返回 True。"""
    for chunk in _split_for_speech(text):
        if not chunk:
            continue
        try:
            out, err, _code = _run_powershell(_tts_script(chunk), timeout=60.0)
        except (subprocess.TimeoutExpired, OSError):
            return False
        if "__TTS_ERR__" in out or "__TTS_ERR__" in err:
            return False
    return True


def describe_availability() -> str:
    """探测本机语音能力（用于 --voice 启动提示）。"""
    lines = [
        "语音对话将使用 Windows 本机能力（不消耗模型额度）",
        f"识别引擎：zh-CN（System.Speech）",
        "朗读：优先中文语音（Microsoft Huihui）",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if mode == "probe":
        print(describe_availability())
    elif mode == "listen":
        print(f"听写结果：{listen_once(timeout_seconds=float(sys.argv[2]) if len(sys.argv) > 2 else 8)}")
    elif mode == "speak":
        text = " ".join(sys.argv[2:]) or "语音功能测试成功"
        print("朗读结果：", speak(text))
