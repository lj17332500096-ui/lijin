"""修复 .workbuddy/memory/2026-09-19.md 的编码损坏（一次性运维脚本）。

背景
----
该文件是历史多次混写累积损坏的：utf-8 / gbk / cp1252 三段共存。
本脚本只做**最大程度还原**，并在文件头写明限制，绝不静默编造内容。

损坏形态
--------
1. **二次编码乱码（mojibake，占绝大多数）**
   原始 UTF-8 字节被当作 GBK 解码、再以 UTF-8 写回。典型：``入口``→``鍏ュ彛``、
   ``透传``→``閫忎紶``、``验证``→``楠岃瘉``。
   这类**逐行 utf-8 合法性检测发现不了** —— 它确实是合法 UTF-8。

2. **真损坏字节**：出现 UTF-8 非法起始字节（0x89），整行无法解码。

判定策略（两级）
--------------
A. **严格往返**：``s.encode(enc).decode("utf-8")`` 无损成功 → 高置信度乱码。
   原理：正常中文按 GBK 编码后几乎不可能仍是合法 UTF-8。
B. **常用字打分**（用于 A 失败的"有损乱码"行）：
   从文件自身的**健康段落**构建常用字表，比较还原前后的常见字占比，
   只在显著提升时才采纳。这样避免把本来就正常的行改坏。

已知限制
--------
原始乱码由 ``gbk + errors=replace`` 生成，**部分字节已永久丢失**：
- 丢失处以 ``?`` 标记（可见、不可恢复）
- 个别字可能还原成近形字（如 兼容修复→兼忮、截图反馈→戕图报锡）
这是有损转换的必然结果，不是脚本缺陷。

用法
----
    python delivery/repair_mojibake.py <file>            # 干跑预览
    python delivery/repair_mojibake.py <file> --write    # 写回（自动 .bak）
"""

from __future__ import annotations

import sys
from pathlib import Path

CODECS = ("gbk", "gb18030", "big5")

# 健康参考语料：用它们构建"本项目正常中文"的常用字表。
# 选项目的长期记忆文件 + 目标文件的前 34 行（实测为干净 UTF-8）。
HEALTHY_REF = ".workbuddy/memory/MEMORY.md"
HEALTHY_LINE_CAP = 34

HEADER_NOTE = (
    "> 本文件原为混合编码损坏状态（utf-8 / gbk / cp1252 混写），"
    "已由 `delivery/repair_mojibake.py` 最大程度还原。\n"
    "> 原始转换是有损的，少量字符不可恢复：丢失处以 `?` 标记，个别字可能是近形字。\n"
)


def load_common_chars(target: Path) -> set[str]:
    """从健康语料构建常用字表。

    语料越大、判别越准 —— 只用 MEMORY.md 时字表仅 241 字，误判率高。
    这里额外并入项目里的 Markdown 文档与目标文件的健康前缀。
    """
    # target = <project>/.workbuddy/memory/<date>.md
    # parent=memory, parent.parent=.workbuddy, parent.parent.parent=<project>
    root = target.parent.parent.parent
    corpus: list[str] = []
    for rel in (".workbuddy/memory/MEMORY.md", "README.md",
                "ARCHITECTURE_REVIEW_2026-09-19.md"):
        p = root / rel
        if p.exists():
            try:
                corpus.append(p.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    for raw in target.read_bytes().split(b"\n")[:HEALTHY_LINE_CAP]:
        try:
            corpus.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            pass
    return {c for c in "".join(corpus) if "\u4e00" <= c <= "\u9fff"}


def cjk_count(s: str) -> int:
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff")


def score(s: str, common: set[str]) -> float:
    """常见字占比。

    注意：无汉字的串**不**给满分 —— 有损还原会把汉字整片丢掉，
    若给满分，正常中文行会被"还原"成一堆 ASCII 残骸（实测踩过）。
    """
    if not s:
        return 0.0
    cjk = [c for c in s if "\u4e00" <= c <= "\u9fff"]
    if not cjk:
        return -1.0
    good = sum(1 for c in cjk if c in common)
    return good / len(cjk) - s.count("\ufffd") * 0.5


def acceptable(orig: str, cand: str, common: set[str], *, margin: int = 2) -> bool:
    """候选是否值得采纳。三道闸，宁可不还原也不改坏：

    1. 汉字不能大面积丢失 —— 有损还原若把汉字吃掉了，宁可保留原样
       （实测：正常中文行走有损路径会被整片吃成 ASCII 残骸）。
    2. **绝对**常见字数不得减少。用绝对数而非占比，是因为有损还原
       常把候选变短，占比会虚高（汉字丢光时占比反而"漂亮"）。
    3. 不得引入替换符。

    ``margin``：精确枚举（已通过 UTF-8 严格校验）用 0 —— 它的收益常常是
    箭头/标点这类非汉字，强制 +2 会把它误杀；有损路径用 2 以防改坏。
    """
    if not cand or cand == orig:
        return False
    if cand.count("\ufffd") > orig.count("\ufffd"):
        return False
    oc, cc = cjk_count(orig), cjk_count(cand)
    if oc > 0 and cc < oc * 0.55:
        return False
    good = lambda s: sum(1 for c in s if c in common)  # noqa: E731
    return good(cand) >= good(orig) + margin


def has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def try_strict(s: str) -> str | None:
    """无损往返成功 → 高置信度乱码，返回还原结果。"""
    if not has_cjk(s):
        return None
    for enc in CODECS:
        try:
            raw = s.encode(enc)
        except UnicodeEncodeError:
            continue
        try:
            fixed = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if has_cjk(fixed):
            return fixed
    return None


def try_lossy(s: str) -> str | None:
    """有损还原：丢弃无法映射的字节，尽量保住可读部分。"""
    if not has_cjk(s):
        return None
    for enc in CODECS:
        try:
            raw = s.encode(enc, errors="ignore")
        except Exception:
            continue
        try:
            return raw.decode("utf-8", errors="ignore")
        except Exception:
            continue
    return None


def _valid_utf8_prefix(b: bytes) -> bool:
    """是否为合法 UTF-8 前缀（允许结尾是不完整的序列）。用于 DFS 剪枝。"""
    i, n = 0, len(b)
    while i < n:
        c = b[i]
        if c < 0x80:
            i += 1
            continue
        if 0xC0 <= c < 0xE0:
            need = 2
        elif 0xE0 <= c < 0xF0:
            need = 3
        elif 0xF0 <= c < 0xF8:
            need = 4
        else:
            return False
        tail = b[i + 1:i + need]
        if any(not (0x80 <= x < 0xC0) for x in tail):
            # 尾部不完整时，已出现的续字节必须合法；残缺本身允许
            if len(tail) < need - 1:
                return False
            return False
        if len(tail) < need - 1:
            return True  # 不完整但是合法前缀
        i += need
    return True


def try_bruteforce(s: str, common: set[str], *, max_gaps: int = 6) -> str | None:
    """暴力枚举丢失字节。

    原理：乱码里的每个 ``?`` 通常对应**恰好一个**孤立字节 ——
    3 字节 UTF-8 序列（如 ``→`` = E2 86 92）被 GBK 吃掉前两个（E2 86 = ``鈫``），
    剩下的 92 无处可去，被替换成 ``?``。

    所以只在每个 ``?`` 处枚举 256 种取值，并用"UTF-8 前缀合法"剪枝 ——
    绝大多数取值会立刻产生非法序列而被剪掉，搜索空间实际很小。
    """
    if "?" not in s:
        return None
    gaps = s.count("?")
    if gaps > max_gaps:
        return None

    chunks = [c.encode("gb18030", errors="ignore") for c in s.split("?")]
    # 末尾 chunk 后面没有 gap
    results: list[str] = []
    budget = [200_000]

    def good(t: str) -> int:
        return sum(1 for c in t if c in common)

    # 组装顺序：chunk[0] + ?字节 + chunk[1] + ?字节 + ... + chunk[n]
    def dfs(gap_i: int, acc: bytearray) -> None:
        """gap_i = 已填充的 gap 数量；此时 acc 末尾是 chunk[gap_i] 的内容。"""
        if budget[0] <= 0:
            return
        if gap_i == len(chunks) - 1:
            try:
                results.append(bytes(acc).decode("utf-8"))
            except UnicodeDecodeError:
                pass
            return
        for b in range(0x80, 0x100):
            budget[0] -= 1
            cand = bytearray(acc)
            cand.append(b)
            cand += chunks[gap_i + 1]
            if not _valid_utf8_prefix(bytes(cand)):
                continue
            dfs(gap_i + 1, cand)

    dfs(0, bytearray(chunks[0]))
    if not results:
        return None
    return max(results, key=good)


def bytes_to_str(raw: bytes) -> tuple[str, bool]:
    """字节 -> str，返回 (文本, 是否发生过兜底)。"""
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        pass
    out: list[str] = []
    i = 0
    damaged = False
    while i < len(raw):
        for n in (4, 3, 2, 1):
            try:
                out.append(raw[i:i + n].decode("utf-8"))
                i += n
                break
            except UnicodeDecodeError:
                continue
        else:  # pragma: no cover
            out.append(raw[i:i + 1].decode("cp1252", errors="replace"))
            damaged = True
            i += 1
    return "".join(out), damaged


def repair_line(raw: bytes, common: set[str]) -> tuple[str, str]:
    """返回 (文本, 状态)：ok / mojibake / lossy / damaged。"""
    s, damaged = bytes_to_str(raw)

    fixed = try_strict(s)
    if fixed is not None and fixed != s:
        return fixed, "damaged" if damaged else "mojibake"

    # 先试"枚举丢失字节"的精确还原，再退到有损还原
    cand = try_bruteforce(s, common)
    if cand is not None and acceptable(s, cand, common, margin=0):
        return cand, "brute"

    cand = try_lossy(s)
    if cand is not None and acceptable(s, cand, common):
        return cand, "lossy"

    return s, "damaged" if damaged else "ok"


SUSPECT_MARK = "〔此行编码损坏，未能还原〕"


def looks_damaged(s: str, common: set[str]) -> bool:
    """还原后仍然是乱码吗？用"罕见字占比"判断。

    正常中文行绝大多数字都落在项目语料的常用字表里；乱码行的字几乎全在表外。
    阈值取得保守（≥4 个汉字 且 罕见率 >0.7），宁可漏标也不误标。
    """
    cjk = [c for c in s if "\u4e00" <= c <= "\u9fff"]
    if len(cjk) < 4:
        return False
    rare = sum(1 for c in cjk if c not in common)
    return rare / len(cjk) > 0.7


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: repair_mojibake.py <file> [--write]")
        return 2
    path = Path(argv[1])
    write = "--write" in argv
    data = path.read_bytes()
    lines = data.split(b"\n")
    common = load_common_chars(path)

    results = [repair_line(l, common) for l in lines]
    fixed_lines = [r[0] for r in results]

    # 还原后仍是乱码的行：显式标记，而不是让它们看起来像正常内容
    n_marked = 0
    for i, txt in enumerate(fixed_lines):
        if looks_damaged(txt, common):
            fixed_lines[i] = txt.rstrip("\r") + "  " + SUSPECT_MARK
            n_marked += 1

    from collections import Counter
    stats = Counter(r[1] for r in results)
    print(f"file     : {path}")
    print(f"lines    : {len(lines)}  | common chars: {len(common)}")
    print(f"repaired : mojibake={stats['mojibake']} brute={stats['brute']} "
          f"lossy={stats['lossy']} damaged={stats['damaged']}")
    print(f"marked   : {n_marked} 行仍为乱码（已显式标注）")
    print("-" * 68)
    for i, (orig_b, (new, st)) in enumerate(zip(lines, results), 1):
        if st == "ok":
            continue
        print(f"[{i}:{st}] {orig_b.decode('utf-8', errors='replace')[:54]}")
        print(f"        -> {new[:54]}")

    if write:
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_bytes(data)
        body = "\n".join(fixed_lines)
        path.write_text(HEADER_NOTE + body, encoding="utf-8")
        print("-" * 68)
        print(f"written  : {path}   (backup: {bak.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
