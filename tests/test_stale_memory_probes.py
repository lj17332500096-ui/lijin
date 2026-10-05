"""记忆里的「已知遗留问题」是否已经过期？—— 用 git + 测试双重取证。

**为什么需要这个测试**（本项目 10-05 踩到的真实问题）：

MEMORY.md §6 曾记着「跨会话遗留三条」待修，但逐条实测发现**三条都已修完**
（`95888af` / `bf0616e` 已提交、有专门回归测试）。记忆条目本身也会**因对应
修复已完成而失效** —— 这是「无源结论要撤销」那条铁律的**第二种形态**：

- 第一种：结论**从未有源**（如`test_tui_artifacts` 偶发红，4 份 junit 里从未失败）
- 第二种：结论**曾经有源，但已过期**（修复完成后没人回来划掉）

第二种更隐蔽：条目读起来言之凿凿（「不记真实异常类型/消息 ⇒ 空话」），
下一个会话会照着去"修"一个已经修好的东西 —— 轻则白干，重则把好代码改坏。

**本测试的作用**：把「记忆里的待修条目」变成**可执行的检查**。
任何人往 MEMORY.md 写「待修/遗留」时，本测试会强制他去核实；
若条目已被修复却没划掉 ⇒ 这里报红。

**判据设计**（按项目铁律「判据要对缺陷敏感」）：
- 不 grep 记忆文本里的措辞（那只会锁住字面量，不证明代码状态）
- 而是对每个待查条目跑**真实的取证命令**（git 追溯 / 跑它的回归测试），
  把「已修」这个事实变成可复现的判定
- 条目在 MEMORY.md 里被划掉（`~~...~~` 或移出待办区）后，对应检查自动放行
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
MEMORY = BASE / ".workbuddy" / "memory" / "MEMORY.md"

# 每条 = (条目关键词, 取证方式, 证据锚点, 限定路径, 撤销标记)
# 取证方式：
#   "commit"  -> git log -S <锚点> -- <限定路径>，看是否已有修复提交
#   "test"    -> 跑该回归测试文件，看是否已存在且全绿
# 限定路径很重要：`git log -S` 不限定路径时要扫全仓历史（>60s 会超时），
# 限定到相关文件后是秒级。**判据要选「够用且快」的那个** —— 与
# 「优先选全序量而非慢的连续量」同源。
#
# `retire_marks` 是**撤销标记**：记忆里出现任一标记即视为「已明确撤销」。
# 为什么需要它：条目被合并撤销时（如三条并成一条「已全部撤销」），
# 逐条关键词都不在划掉的文本里 —— 探针会去取证、发现已修、然后报红。
# 也就是说：**没有标记机制时，「正确地撤销」反而会让探针误报**。
# 这与 MEMORY.md 里「判据字符串必须与生产端写出的值对齐」同源：
# 撤销动作与撤销标记必须成对，否则自动化会把它读成「未撤销」。
PROBES: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    (
        "provider_internal_error",
        "commit",
        "error_type",
        "runtime/provider_gateway.py",
        ("已全部撤销", "全部撤销"),
    ),
    (
        "MCP 退出",
        "test",
        "tests/test_mcp_teardown_noise.py",
        "tests/test_mcp_teardown_noise.py",
        ("已全部撤销", "全部撤销"),
    ),
    (
        "审批死分支",
        "commit",
        "approval_required",
        "llama_bridge.py",
        ("已全部撤销", "全部撤销"),
    ),
]


def _memory_text() -> str:
    if not MEMORY.is_file():
        return ""
    return MEMORY.read_text(encoding="utf-8", errors="replace")


def _entry_is_retired(text: str, keyword: str, retire_marks: tuple[str, ...]) -> bool:
    """条目是否已在记忆里被明确划掉/ 移出待办。

    判定三种形式：
    1. 含 `~~keyword~~`（Markdown 删除线）
    2. 所在行附近出现「已撤销 / 已修 / 实测不成立 / 过期」等撤销措辞
    3. 出现 `retire_marks` 里的**合并撤销标记**（如「已全部撤销」）
       —— 供「多条并成一条撤销」的场景使用，见PROBES 的说明
    """
    for line in text.splitlines():
        if keyword not in line:
            continue
        if f"~~" in line and keyword in line.split("~~")[0] + "".join(
            line.split("~~")[1::2]
        ):
            return True
        if any(w in line for w in ("已撤销", "已修", "过期", "实测不成立", "不成立")):
            return True
    # 合并撤销标记可在别的行（本条的标题行写「已全部撤销：三条分别是…」）
    return any(mark in text for mark in retire_marks)


def _probe_commit(anchor: str, path: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            ["git", "log", "--oneline", "-S", anchor, "--", path],
            cwd=str(BASE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git 追溯失败（无法判定，暂不判红）：{exc}"
    if p.returncode != 0:
        return False, f"git 追溯失败（无法判定，暂不判红）：{(p.stderr or '')[:200]}"
    commits = [ln for ln in p.stdout.splitlines() if ln.strip()]
    if not commits:
        return False, f"{path} 的历史里未追溯到 {anchor!r}（若修复已合入，应能追溯到）"
    return True, f"{commits[0]}（{path} 中已含 {anchor!r}）"


def _probe_test(rel: str) -> tuple[bool, str]:
    path = BASE / rel
    if not path.is_file():
        return False, f"{rel} 不存在（若已修，测试文件应存在；否则需重查）"
    py = BASE / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        return False, f"未找到 {py}（无法判定，暂不判红）"
    try:
        import os

        p = subprocess.run(
            [str(py), "-m", "pytest", rel, "-q", "-p", "no:cacheprovider"],
            cwd=str(BASE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
            env={**os.environ, "PYTHONPATH": ""},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"测试执行失败（无法判定，暂不判红）：{exc}"
    if p.returncode == 0:
        return True, f"{rel} 全绿 ⇒ 该条已被修复"
    last = [ln for ln in (p.stdout or "").splitlines() if ln.strip()][-1:]
    return False, f"{rel} 未通过：{last[0] if last else '（无输出）'}"


class RetiredMemoryProbeTests(unittest.TestCase):
    """探测 MEMORY.md 里的「待修/遗留」条目是否已过期。"""

    def test_memory_file_exists(self) -> None:
        """反空转下界：本测试必须真的读到记忆文件，否则下面全是恒绿。"""
        self.assertTrue(MEMORY.is_file(), f"找不到 {MEMORY}")
        text = _memory_text()
        self.assertGreater(
            len(text), 500,
            "MEMORY.md 内容异常短—— 读不到实质内容，下面的检查会失去意义。",
        )

    def test_no_stale_known_issues(self) -> None:
        """记忆里的每条「已知遗留」都必须**尚未修复**，否则应从记忆里划掉。

        反面（这是本测试存在的理由）：10-05 发现「跨会话遗留三条」三条全已修完，
        但记忆里仍标着待修。下个会话会照着去"修"一个已修好的东西。
        """
        text = _memory_text()
        if not text:
            self.skipTest("记忆文件不可读")

        stale: list[str] = []
        unknown: list[str] = []
        for keyword, kind, arg, scope, marks in PROBES:
            if keyword not in text:
                # 条目已从记忆里移除 ⇒ 本次不检查（不视为失败）
                continue
            if _entry_is_retired(text, keyword, marks):
                continue  # 已在记忆里明确划掉 ⇒ 符合预期

            if kind == "commit":
                ok, detail = _probe_commit(arg, scope)
            elif kind == "test":
                ok, detail = _probe_test(arg)
            else:
                ok, detail = False, "人工核实项，未自动化"

            label = f"{keyword}（{arg}@{scope}）"
            if "暂不判红" in detail:
                unknown.append(f"{label}: {detail}")
            elif ok:
                stale.append(f"{label}: {detail}")
            # ok is False and not unknown ⇒ 真的还没修，本轮无需动作

        self.assertEqual(
            stale, [],
            "MEMORY.md 里的「已知遗留」已被修复但未划掉 —— 下一个会话会去"
            "\"修\"一个已修好的东西：\n  " + "\n  ".join(stale),
        )
        if unknown:
            print(f"[提示] {len(unknown)} 条无法自动判定，需人工核实：")
            for u in unknown:
                print(f"  {u}")


class MemoryHygieneTests(unittest.TestCase):
    """记忆文件本身的可读性检查。

    **这里曾有一条判据被删掉**，记录过程因为它是「判据不可靠就删」的实例：

    初版叫 `test_no_unretired_stale_markers`，用「含『遗留』/『待修』字样且
    无撤销措辞」来判定「待办漏划」。它报出 4 条，逐条看：
    - 2 条是**铁律正文**（「曾记成待修，10-05 追溯时发现…」「…待观察而非待修」）
    - 1 条是**真实待办**（评测遗留 n=200 对照，确实还没做）
    - 1 条是铁律的续行

    即它同时抓三类语义，且**无法可靠区分** —— 真实待办不该被「漏划」判据管，
    铁律正文不是待办。只能不断加豁免前缀名单，而那正是本项目
    「硬编码阈值 / 豁免白名单会随新增项失效」的老毛病。

    **结论：判据不可靠就删掉。** 真正有价值的检查是上面那条
    `test_no_stale_known_issues` —— 它用**真实的 git 追溯 + 跑测试**来判定
    「已修却没划掉」，不依赖措辞匹配，因而不受上述三类语义混淆影响。

    保留一条可判定的：**条目必须自带取证来源**，否则下个会话无法验证它。
    """

    def test_pending_items_cite_evidence(self) -> None:
        """待办区每条必须能指出「怎么验证它」—— 至少含commit、测试或报告路径之一。

        这是**可判定**的：不看措辞，只看是否给出了可执行的验证入口。
        """
        text = _memory_text()
        if not text:
            self.skipTest("记忆文件不可读")
        # 取 §6 待办区
        m = re.search(r"^## 6\. 待办.*?$(.*?)^## 7\.", text, re.M | re.S)
        if not m:
            self.skipTest("未找到 §6 待办区（记忆结构变了？）")
        block = m.group(1)
        # 逐条待办：以 `- ` 开头，后续缩进行属同一条（**按块判定，不按行** ——
        # 首版按行判，验证入口写在续行时全部误报，那是判据错而非条目错）
        items: list[str] = []
        for ln in block.splitlines():
            if not ln.strip():
                continue
            if ln.startswith("- ") or ln.startswith("-~~"):
                items.append(ln)
            elif items and (ln.startswith("  ") or ln.startswith("\t")):
                items[-1] += "\n" + ln
            else:
                items.append(ln)
        items = [i for i in items if i.lstrip().startswith(("- ", "-~~"))]
        self.assertGreater(
            len(items), 0, "§6 待办区解析不到条目（结构变了？）"
        )
        # 每条要么已划掉/已撤销，要么含可验证入口
        no_evidence: list[str] = []
        for it in items:
            if any(w in it for w in ("已撤销", "已修", "已全部撤销", "过期")):
                continue
            has_ref = (
                ".py" in it or ".md" in it or ".yml" in it
                or "commit" in it or "T0" in it
                or re.search(r"`[^`]+`", it) is not None
            )
            if not has_ref:
                no_evidence.append(it.strip()[:140])
        self.assertEqual(
            no_evidence, [],
            "§6 待办区有条目未给出任何可验证入口（文件/commit/报告），"
            "下个会话无法核实它是否已过期：\n  " + "\n  ".join(no_evidence),
        )


if __name__ == "__main__":
    unittest.main()
