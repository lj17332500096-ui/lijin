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
- 不 grep 记忆文本里的措辞来判「有没有问题」（那只会锁住字面量）
- 而是对每个待查条目跑**真实的取证命令**（git 追溯 / 跑它的回归测试），
  把「已修」/「没修」这个事实变成可复现的判定
- 条目在 MEMORY.md 里被划掉（`~~...~~` 或撤销措辞）后，对应检查自动放行
- 唯一还用措辞的地方是「**它是否声称自己无源**」—— 那是判「撤销理由」，
  不是判「状态」；状态永远来自外部取证

**两条镜像判据**（缺一不可，方向不可反）：
- `stale`：条目说「待修」，取证说「已修」⇒ 该划掉却没划 ⇒ 报红
- `contradictions`：条目说「无源 ⇒ 已撤销」，取证说「还没修」⇒ 被掩埋 ⇒ 报红

反向组合（「无源撤销」+「确实已修」）**不是矛盾**，那是正确的纠错记录，不得报错。
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
    (
        # 这条曾被我判为「无源 flaky」而撤销，**当天下午就在全量下红了**
        # （负载下 6 次 3 红）。已修（`test_tui_artifacts.py:99` 改轮询）。
        # 留在此表是为了：**若有人再把它标成"无源/已撤销"，这里会报红**。
        #
        # ⚠️ `retire_marks` 这里**故意不含「无源」**：它是全文裸子串匹配，
        # 而「无源」在铁律正文里出现 3 次（L77/L95 等）⇒ 加上它会让
        # `_entry_is_retired` 对本条**恒为 True**，判据与条目内容脱钩（恒真=永不鉴别）。
        # 中和实验实测：仅用 mark='无源' 即得 retired=True（全文含该 mark）。
        # 「无源」不是撤销**标记**，它是撤销**理由** —— 两者不可混用。
        # 撤销标记只取完成态措辞（已撤销/已修/不成立/过期），理由交给
        # `_claims_unsourced` 的句式判据去管。
        "test_tui_artifacts",
        "test",
        "tests/test_tui_artifacts.py",
        "tests/test_tui_artifacts.py",
        ("已撤销", "已修", "不成立"),
    ),
]


def _memory_text() -> str:
    if not MEMORY.is_file():
        return ""
    return MEMORY.read_text(encoding="utf-8", errors="replace")


_RETIRE_WORDS = ("已撤销", "已修", "过期", "实测不成立", "不成立")


def _entry_is_retired(text: str, keyword: str, retire_marks: tuple[str, ...]) -> bool:
    """条目是否已在记忆里被明确划掉 / 移出待办。

    判定三种形式：
    1. 含 `~~keyword~~`（Markdown 删除线）
    2. **keyword 所在行或其紧邻行**出现「已撤销 / 已修 / 过期 / 实测不成立 / 不成立」
    3. 出现 `retire_marks` 里的**合并撤销标记**（如「已全部撤销」）
       —— 供「多条并成一条撤销」的场景使用，见 PROBES 的说明

    **第2 条为什么必须限定「同一行或紧邻行」**（中和实验实测的恒真缺陷）：
    初版是「只要全文**任意一行**含 keyword，且**任意一行**含撤销措辞 ⇒ retired」。
    这两件事完全解耦 —— 实测 `test_tui_artifacts` 在 MEMORY.md 里出现 4 次，
    其中 L91/L96 行自带「已修」「撤销」等词，于是无论该条目内容如何变，
    retired 恒为 True，判据与被测状态脱钩。
    **恒真的判据不是「宽松」，是「无鉴别力」** —— 它永远绿，也永远抓不到东西。
    与 MEMORY.md §2.1「白名单断言在新增项时天然失效」同源：判据一旦与
    被测对象解耦，就退化成恒真。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if keyword not in line:
            continue
        # 1. 删除线
        if "~~" in line:
            return True
        # 2. 撤销措辞：同行，或上/下一行（撤销理由常写在续行）
        lo, hi = max(0, i - 1), min(len(lines), i + 2)
        window = "\n".join(lines[lo:hi])
        if any(w in window for w in _RETIRE_WORDS):
            return True
    # 3. 合并撤销标记可在别的行（本条的标题行写「已全部撤销：三条分别是…」）
    return any(mark in text for mark in retire_marks)


#: 声称「这条结论没有来源」的**句式**（不是单个词）。
#:
#: 为什么必须用句式而非关键词「无源」二字：记忆里「曾把 X 当无源撤销」这类
#: 叙述出现在**铁律正文**里（§2.1 那条铁律本身就在讲这个案例），
#: 只看「有无源二字」会把铁律正文当成条目断言（中和实测：4 条误报）。
#:
#: 精确判据 = **该条目附近既说「无源」又说「已撤销/已划掉」**，
#: 即「这条被判定为无源而撤销」这个**完整断言**，而非铁律里的泛泛表述。
_UNSOURCED_PATTERNS = (
    re.compile(r"无源.{0,20}(已撤销|撤销了|已划掉)"),
    re.compile(r"(已撤销|撤销了).{0,20}无源"),
    re.compile(r"无源\s*flaky"),
    re.compile(r"flaky.{0,10}无源"),
)


def _context_blocks(text: str, keyword: str, before: int = 2, after: int = 2) -> list[str]:
    """keyword 出现的**每一个**位置附近的上下文块（按块，不按单行）。

    **为什么必须遍历全部出现处而不是只看第一处**（中和实验实测的漏检）：
    keyword `test_tui_artifacts` 在 MEMORY.md 里出现 4 次，前3 次都在**铁律正文
    的举例**里（L78 讲「从未有源 ⇒ 撤销」、L91 讲「撤销前先问取样负载」、
    L100 讲「pytest 报出的用例名 ≠ 根因」），真正的条目状态在 L96。
    初版只取首处⇒ 判据读的是铁律举例、**永远读不到条目本身**：
    把 L96 改写成矛盾的「无源 flaky 记录已撤销」后，判据仍返回 `False`。

    两个同源的教训：
    - 与本文件 `test_pending_items_cite_evidence` 的「按块判定」同源
    - 与 MEMORY.md §2.1「可达性要按**调用点**判、不能按函数名判」同源
      （同一函数被调多次时，只看第一次会漏掉其余调用点上的真实状态）
    """
    lines = text.splitlines()
    blocks: list[str] = []
    for i, line in enumerate(lines):
        if keyword not in line:
            continue
        lo, hi = max(0, i - before), min(len(lines), i + after + 1)
        blocks.append("\n".join(lines[lo:hi]))
    return blocks


def _claims_unsourced(text: str, keyword: str) -> bool:
    """条目是否**断言**「这个问题没有来源」（按句式判定，不受铁律正文干扰）。"""
    return any(
        p.search(block)
        for block in _context_blocks(text, keyword)
        for p in _UNSOURCED_PATTERNS
    )


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
        contradictions: list[str] = []
        for keyword, kind, arg, scope, marks in PROBES:
            if keyword not in text:
                # 条目已从记忆里移除 ⇒ 本次不检查（不视为失败）
                continue

            # 先跑取证 —— **不论条目标注了什么状态**。
            # 理由：判据不能靠「记忆里写了已修」就放行，那等于让记忆自己给自己
            # 背书；取证是外部事实（git 历史 / 实际跑测试），记忆说了不算。
            if kind == "commit":
                ok, detail = _probe_commit(arg, scope)
            elif kind == "test":
                ok, detail = _probe_test(arg)
            else:
                ok, detail = False, "人工核实项，未自动化"

            retired = _entry_is_retired(text, keyword, marks)
            label = f"{keyword}（{arg}@{scope}）"

            if retired:
                # 条目已划掉。只在一种情况下该报红：
                #
                # **条目声称「无源所以撤销了」，而取证说它【还没修】。**
                #
                # ⚠️ 判据方向曾写反过一次，这里记录原因（这是「测通过 ≠ 有鉴别力」
                # 的又一实例，且比「注入没生效」更隐蔽 —— 它**确实有鉴别力**，
                # 只是鉴别的是**错的方向**）：
                # 初版判据是 `ok and _claims_unsourced`，即
                # 「取证说已修 + 记忆说无源 ⇒ 矛盾」。但这个组合**不是矛盾**：
                # 「我曾把它当无源撤销、后来证明是真问题并修好了」是**正确且有价值**
                # 的记忆（MEMORY.md §2.1 L90-97 记的正是这件事），按初版反而会被
                # 当成自相矛盾报错 —— **判据会把正确的记忆判成错的**。
                #
                # 真正该抓的反面是：说「无源、已撤销、不用管了」，实际却还坏着
                # ⇒ 下个会话读到「已撤销」就跳过，一个真问题被永久掩埋。
                # 这与 `stale` 分支（已修却没划掉）是**一对镜像**：
                # 一个抓「该修的被记成不用修」，一个抓「该记的没记」。
                if (not ok) and "暂不判红" not in detail and _claims_unsourced(text, keyword):
                    contradictions.append(
                        f"{label}: 条目声称「无源 ⇒ 已撤销」，但取证显示**尚未修复** —— "
                        f"{detail}。真问题被「无源」掩埋，下个会话会跳过它。"
                    )
                continue
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
        self.assertEqual(
            contradictions, [],
            "MEMORY.md 把条目标为「无源 ⇒ 已撤销」，但取证显示它**还没修** —— "
            "真问题被「无源」掩埋，下个会话读到「已撤销」就会跳过它：\n  "
            + "\n  ".join(contradictions),
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
