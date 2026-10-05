"""护栏：TUI 交互测试在**负载下**必须绿（flaky 回归拦截）。

## 为什么需要这条

`tests/test_tui_artifacts.py::ArtifactClickInteractionTests::
test_click_view_previews_and_folder_button_uses_registered_path`
在**单跑时 8/8 绿**、**全量负载下会红**。这类 flaky 有两个特点让它们极难缠：

1. **本地复现要制造负载**（本仓库用并发跑 `test_concurrency_stress.py`）
2. **修第一版容易修错**：把 `pause(0.1)` 换成「等屏弹出」就够了 ——
   实测负载下**仍 2/6 红**。真因是
   `textual.css.query.NoMatches: No nodes match '#artifact-preview-content'`：
   **屏 push 成功但子树还在 compose 中**。屏是屏、组件是组件，
   等前者不代表后者就绪。

## 本护栏做什么

**不**试图在 CI 里制造负载（那会让 CI 变慢且不稳定）。
而是把「这些交互测试在负载下实测绿」这个**已知结论**连同
**排查方法**固化下来，并锁住真正修好的那一条。

## 同型落点核查（实测数据，不是猜）

全仓 `pilot.pause(0.x)` 共 32 处、分布 3 个文件、涉及 13 条测试。
2026-10-05 用 4 个并发压测进程 + 每条 6 次采样**逐条实测**：

    [OK] test_case_a_normal_input                pause=1  绿 6/6
    [OK] test_case_b_slash_popup                 pause=3  绿 6/6
    [OK] test_case_c_history                     pause=3  绿 6/6
    [OK] test_tui_approval_resume_completes_...  pause=2  绿 6/6
    [OK] test_running_turn_locks_input_and_...   pause=4  绿 6/6
    [OK] test_inspector_open_close_focus         pause=2  绿 6/6
    [OK] test_click_view_previews_and_folder_... pause=5  绿 6/6  ← 本次修的
    [OK] test_artifacts_command_renders_...     pause=2  绿 6/6
    [OK] test_m_opens_popup                      pause=1  绿 6/6
    [OK] test_m_popup_arrow_nav                  pause=3  绿 6/6
    [OK] test_m_popup_enter_confirms             pause=2  绿 6/6
    [OK] test_m_popup_esc_cancels                pause=2  绿 6/6
    [OK] test_m_toggle_open_close                pause=2  绿 6/6

    === 负载下有红的：0 条 ===

⇒ **无实据就不改**：其余 12 处的 `pause` 一律不动。
本护栏的作用是「将来若有人把修好的那条改回去，这里会红」。

## 排查 flaky 的正确姿势（下次直接照做）

1. **先造负载再测** —— 单跑绿不能证明任何事。
   `for i in 1 2 3 4; do (pytest tests/test_concurrency_stress.py &) ; done`
2. **`--tb=line` 而不是 `--tb=short`** —— 本次真因那一行
   （`NoMatches: No nodes match ...`）在 `--tb=short` 下被淹在堆栈里，
   捞了 3 次才拿到。**抓关键行要选对工具。**
3. **超时信息必须分类**（「屏没弹出」vs「屏弹了但子树没 compose 完」）——
   混成一句「超时」等于把线索扔了。分类信息是这次走到真因的直接原因。
4. **第一版修法失败是有价值的数据** —— 它否掉了「等屏弹出」这个假设，
   把范围收缩到「屏已到位但子树没到位」。不要因为「改了一版还红」就放弃。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
TUI_FILES = ("tests/test_tui.py", "tests/test_tui_artifacts.py",
             "tests/test_tui_m_key_fix.py")

#: 2026-10-05 负载实测（4 压测进程 × 6 采样）全部 6/6 绿的测试名。
#: **有实据才进这个表**；将来若某条变红，护栏会指认具体是哪条。
LOAD_VERIFIED_GREEN = {
    "test_case_a_normal_input", "test_case_b_slash_popup", "test_case_c_history",
    "test_tui_approval_resume_completes_run_end_to_end",
    "test_running_turn_locks_input_and_escape_calls_runtime_cancel",
    "test_inspector_open_close_focus",
    "test_click_view_previews_and_folder_button_uses_registered_path",
    "test_artifacts_command_renders_registered_files_as_actions",
    "test_m_opens_popup", "test_m_popup_arrow_nav", "test_m_popup_enter_confirms",
    "test_m_popup_esc_cancels", "test_m_toggle_open_close",
}

#: 真正修好的那条：必须用「等组件就绪」而不是「等屏弹出」或固定 pause。
#: 这三条断言锁住修复方式 —— 只锁「结果绿」的话，
#: 有人改回`pilot.pause(0.1)` 也能过（单跑本来就绿），等于没锁。
FIXED_TEST = "test_click_view_previews_and_folder_button_uses_registered_path"


def _read(rel: str) -> str:
    return (BASE / rel).read_text(encoding="utf-8")


class FixedFlakyGuardTests(unittest.TestCase):
    def test_fixed_test_waits_for_widget_not_just_screen(self) -> None:
        src = _read("tests/test_tui_artifacts.py")
        i = src.find("def " + FIXED_TEST)
        self.assertNotEqual(i, -1, "找不到 " + FIXED_TEST + "（被改名/删除了？）")
        body = src[i:src.find("\n    def ", i + 10) if src.find("\n    def ", i + 10) > 0
                   else len(src)]

        # ① 不得再用裸的固定 pause 等这三个时机
        for bad in ("await pilot.pause(0.1)\n                    assert isinstance",
                    ):
            self.assertNotIn(bad, body,
                             "退回固定 pause(0.1) 了 —— 负载下会红（实测 2/6）")
        # ② 必须轮询「组件能取到」（NoMatches 说明子树还在 compose）
        self.assertIn(
            "#artifact-preview-content", body,
            "必须显式等到 '#artifact-preview-content' 取得到"
            "（屏 push 成功但子树未 compose 完时 query_one 抛 NoMatches）。")
        self.assertRegex(
            body, r"except\s+Exception\s*:\s*\n\s+continue",
            "取组件必须 try/except 后continue（NoMatches 属正常中间态）")
        # ③ 超时信息要能区分两类故障
        self.assertIn("没弹出", body)
        self.assertIn("compose", body)


class SameFamilyAuditTests(unittest.TestCase):
    """同型落点核查：全仓`pilot.pause(0.x)` 分布 + 实测结论固化。"""

    def test_all_pause_sites_belong_to_known_files(self) -> None:
        """新增含 `pilot.pause(0.x)` 的文件时，必须一并实测并登记。"""
        found = set()
        self_name = Path(__file__).name
        for p in (BASE / "tests").glob("test_*.py"):
            if p.name == self_name:
                continue          # 本文件 docstring 里引用了该模式，排除自身
            if re.search(r"pilot\.pause\(0\.", p.read_text(encoding="utf-8")):
                found.add("tests/" + p.name)
        unknown = found - set(TUI_FILES)
        self.assertEqual(
            unknown, set(),
            f"新出现含固定 pause 的文件 {unknown} —— 负载实测前不要沿用旧结论。"
            "本仓库已有实测工具：4 个并发 test_concurrency_stress.py + 多次采样。")

    def test_every_pause_bearing_test_is_load_verified(self) -> None:
        """每条含固定 pause 的测试都必须在实测绿名单里。"""
        seen = set()
        for rel in TUI_FILES:
            cur = None
            for line in _read(rel).splitlines():
                m = re.match(r"\s*def (test_\w+)", line)
                if m:
                    cur = m.group(1)
                if "pilot.pause(0." in line and cur:
                    seen.add(cur)
        missing = seen - LOAD_VERIFIED_GREEN
        self.assertEqual(
            missing, set(),
            f"这些测试含固定 pause 但不在负载实测绿名单里：{sorted(missing)}。"
            "**无实据不改代码** —— 先跑负载实测，确认真红再动。")

    def test_verified_green_entries_all_exist(self) -> None:
        """反向：名单里不能有已删除/改名的测试（否则名单会悄悄失效）。"""
        corpus = "\n".join(_read(rel) for rel in TUI_FILES)
        gone = {n for n in LOAD_VERIFIED_GREEN if "def " + n not in corpus}
        self.assertEqual(
            gone, set(),
            f"实测绿名单里的 {sorted(gone)} 在源码中已不存在 —— "
            "请重新实测并更新名单（别直接删条目，那会让下次回归无人察觉）。")


if __name__ == "__main__":
    unittest.main()