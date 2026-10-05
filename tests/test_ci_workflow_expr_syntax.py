"""workflow 的 **GitHub 表达式**必须合法 —— 10-05 首跑实测踩坑固化。

# 这个测试存在的理由（真实事故）

首次 push 触发 GitHub runner，`ci.yml` 报：

    Invalid workflow file: .github/workflows/ci.yml#L1
    (Line: 103, Col: 14): An expression was expected

表现是 **`completed/failure` 但 `jobs=0`、`check-runs=0`**，且 created 与
updated 在同一秒 —— 一个 job 都没起过。排查一度以为是账户未启用 Actions、
仓库被禁用、workflow 没被识别，全排除了。真因是**文件里的表达式语法非法**：
GitHub 解析整个文件失败 ⇒ **拒绝执行**，而不是「某一步失败」。

**最容易踩的一处**：`run:` 块里的**注释**也会被表达式求值。我在注释里写了一个
「双花括号表达式」的字面量示例（想说明展开机制），它同样被求值，解析器遇到
不完整表达式就报错。**注释不是避难所。**

# 为什么需要自己的测试（而不是只靠 actionlint）

`scripts/check_workflow_shells.py` 查的是**各步骤声明的 shell 能否解析脚本**，
与本测试查的**表达式语法**是两件不同的事，互补而非替代（10-05 实测：
shell 校验全绿，表达式却是坏的）。两个都进 CI 才覆盖完整。

# 判据设计（本项目铁律：判据要对缺陷敏感、要有下界防恒真）

不用 `yaml.safe_load` ——它**完全查不出**这类问题（YAML 层面完全合法，
实测已核实）。判据是**从 GitHub 拿到的事实**：

1. 完整文件里**不得出现「双花括号 + 反引号」或「不成对的双花括号」**这类
   必然非法的组合（可离线判定，不依赖网络）
2. **存在至少一处真实表达式**（反空转下界：否则「没有表达式」也会全绿，
   而那意味着 matrix/条件全被写坏了）

真正的语法裁决交给 actionlint（GitHub 官方同款解析器），本测试在没有
actionlint 时也要能跑（降级为上述离线判据+ 明确提示）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[1]
WF_DIR = BASE / ".github" / "workflows"

#: 双花括号。GitHub 表达式语法（actions runner 内置解析器）。
#: **注释里也必须避开** —— 见模块文档的实测事故。
_EXPR_OPEN = "{{"
_EXPR_CLOSE = "}}"

#: 必须能匹配到真实表达式的证据（反空转下界）
_EXPECTED_EXPR = re.compile(r"\$\{\{\s*[\w.$-]+")

#: **表达式里**引用 matrix.* （注释里的裸字样不算 —— 那正是 v3 漏检的成因）
_EXPECTED_MATRIX_EXPR = re.compile(r"\$\{\{[^}]*\bmatrix\.")


def _workflow_files() -> list[Path]:
    if not WF_DIR.is_dir():
        return []
    return sorted(WF_DIR.glob("*.yml")) + sorted(WF_DIR.glob("*.yaml"))


def _actionlint() -> str | None:
    """找 actionlint 可执行文件（PATH 或常见安装位置）。"""
    for name in ("actionlint", "actionlint.exe"):
        p = shutil.which(name)
        if p:
            return p
    for base in (Path("/tmp/al"), Path.home() / "actionlint"):
        for nm in ("actionlint", "actionlint.exe"):
            c = base / nm
            if c.is_file():
                return str(c)
    return None


class WorkflowExpressionSyntaxTests(unittest.TestCase):
    """workflow 的表达式语法必须合法 —— 否则 GitHub 拒绝执行整个文件。"""

    def test_workflows_exist(self) -> None:
        """反空转下界：没有 workflow 文件时下面全是恒绿。"""
        files = _workflow_files()
        self.assertGreaterEqual(
            len(files), 1,
            f"{WF_DIR} 下没有 workflow 文件（结构变了？）—— "
            "本测试的判据全部依赖文件存在，会静默恒绿。",
        )

    def test_expressions_are_wellformed(self) -> None:
        """双花括号必须成对（离线可判的那部分）。

        **判据演进：从「有反引号就报错」到放弃反引号判据**（两次调判据的记录）：
        - v1「有反引号且有表达式 ⇒ 报错」→ 误报 `guard-consistency.yml:101`
          （注释里的 Markdown 行内代码，表达式本身完整合法，actionlint 零 error）
        - v2「表达式闭合后紧跟反引号 ⇒ 报错」→ **同一条仍然误报**。
          该行是注释里的 `origin/` + 双花括号表达式 + `origin/` ——
          闭合后紧跟的确实是反引号，但解析器**读得到闭合符**，完全合法。

          这说明**我无法离线精确模拟词法器**：真正的判别是
          「解析器有没有把闭合格后面的字符读进表达式里」，那是 actionlint
          解析器的行为。**判据与权威工具冲突时先信权威工具** ——
          与其写一条我验证不了的启发式（那正是「不断加豁免名单」的老毛病），
          不如把这项交给 actionlint，离线只保留能可靠判的「成对性」。

        成对性能离线可靠判定，且是 10-05 事故的必要条件之一，故保留。
        """
        problems: list[str] = []
        for f in _workflow_files():
            for lineno, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if line.count(_EXPR_OPEN) != line.count(_EXPR_CLOSE):
                    problems.append(
                        f"{f.name}:{lineno}: 双花括号不成对 —— {line.strip()[:90]}"
                    )
        self.assertEqual(
            problems, [],
            "workflow 里的表达式语法非法 ⇒ GitHub 拒绝执行**整个文件**"
            "（表现为 0 job 秒失败，而非某步失败）：\n  " + "\n  ".join(problems),
        )

    def test_real_expressions_present(self) -> None:
        """**声明了 matrix / 分片的地方必须有对应表达式**（反空转下界）。

        没有表达式意味着 matrix / 分片号全被写坏了 —— 那正是 10-05 之前
        那版`--shard-id` 表达式的目标形态。

        **判据演进两次，都是中和实验逼出来的**：

        1. v1「全仓表达式总数 ≥ 1」→ **漏检**：注入实验把 `ci.yml` 的表达式
           全部替换成占位符，判据仍绿 —— `guard-consistency.yml` 的 3 个
           表达式替它满足了计数。**用一个文件的表达式满足全仓下界 = 自己骗自己。**
        2. v2「每个文件都至少有一个」→ **误报**：`nightly.yml` 是定时任务、
           不分片、artifact 名固定，**它本来就不需要表达式**。
           假设错了：「并非每个 workflow 都需要表达式」。

        v3（当前）：按**真正需要表达式的信号**判定 —— 声明了 `matrix:` 就必须
        在该文件里**用表达式**引用 `matrix.*`。

        ⚠️ v3 的第一版实现仍漏检，中和实验抓到：判据写成 `"matrix." in text`，
        而 `ci.yml:25` 的**注释里**有 `matrix.shard` 字样 ⇒ 注入「把真表达式全
        替换成占位符」后，判据仍被注释里的字样满足。
        **注释又一次成了判据的漏洞来源**（本日第3 次同类问题，见 MEMORY.md
        §2.1）。改为 `_EXPECTED_MATRIX_EXPR` —— 必须匹配**双花括号里**的
        `matrix.`，注释里的裸字样不算。
        """
        missing: list[str] = []
        for f in _workflow_files():
            text = f.read_text(encoding="utf-8", errors="replace")
            data = yaml.safe_load(text) or {}
            jobs = data.get("jobs") or {}
            for job_name, spec in jobs.items():
                if not isinstance(spec, dict):
                    continue
                has_matrix = bool((spec.get("strategy") or {}).get("matrix"))
                # 只认真表达式里的 matrix.*，注释里的裸字样不算
                uses_matrix_expr = _EXPECTED_MATRIX_EXPR.search(text) is not None
                if has_matrix and not uses_matrix_expr:
                    missing.append(
                        f"{f.name}: job {job_name} 声明了 matrix，"
                        f"但没有任何表达式引用 matrix.*"
                    )
        self.assertEqual(
            missing, [],
            "声明了 matrix 却没有任何 matrix.* 表达式 —— matrix 不生效，"
            "分片会退化成默认值（正是 10-05 之前那版 --shard-id 的形态）：\n  "
            + "\n  ".join(missing),
        )

    def test_actionlint_clean(self) -> None:
        """官方同款解析器 actionlint 必须零 error（10-05 事故的原始判据）。"""
        al = _actionlint()
        if al is None:
            self.skipTest(
                "未找到 actionlint（可从 rhysd/actionlint release 下载后放进 PATH）。"
                "离线判据（test_expressions_are_wellformed）仍会拦下本次事故的形态。"
            )
        files = [str(f) for f in _workflow_files()]
        try:
            p = subprocess.run(
                [al, *files], cwd=str(BASE), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(f"actionlint 执行失败（暂不判红）：{exc}")
        err = "\n".join(
            ln for ln in ((p.stdout or "") + (p.stderr or "")).splitlines() if ln.strip()
        )
        self.assertEqual(
            err.strip(), "",
            "actionlint 报出 workflow 错误 ⇒ GitHub 会拒绝执行整个文件"
            "（0 job 秒失败）。注意它查的是**表达式语法**，"
            "与 check_workflow_shells.py 的 shell 语法互补：\n" + err,
        )


class WorkflowYamlLoadsTests(unittest.TestCase):
    """前提校验：YAML 本身合法，且 jobs 段真能解析出 job。

    **这不是**上面那条的判据（`yaml.safe_load` 查不出表达式问题），
    而是排除「文件结构坏掉」这个更基础的可能。
    """

    def test_yaml_parses_with_jobs(self) -> None:
        files = _workflow_files()
        if not files:
            self.skipTest("无 workflow 文件")
        for f in files:
            with self.subTest(workflow=f.name):
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                self.assertIsInstance(data, dict, f"{f.name}:顶层不是映射")
                # `on:` 会被 YAML 1.1 解析成布尔 True，故两个键都认
                on = data.get("on", data.get(True))
                self.assertIsNotNone(on, f"{f.name}: 缺触发条件 on:")
                jobs = data.get("jobs") or {}
                self.assertGreaterEqual(
                    len(jobs), 1,
                    f"{f.name}: jobs 为空 ⇒ GitHub 一个 job 都不会创建"
                    f"（正是 10-05 首跑的表现形态）",
                )


if __name__ == "__main__":
    unittest.main()
