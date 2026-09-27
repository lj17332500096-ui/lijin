import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import skills_loader


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("SKILLS")
        os.environ["SKILLS"] = "weekly_report,dep_doctor"

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("SKILLS", None)
        else:
            os.environ["SKILLS"] = self._saved

    def test_definitions_loaded_for_enabled(self) -> None:
        defs = skills_loader.skill_definitions()
        names = [d["name"] for d in defs]
        self.assertIn("weekly_report", names)
        self.assertIn("dep_doctor", names)
        self.assertTrue(all(d["text"].strip() for d in defs))

    def test_tool_collection(self) -> None:
        tools = skills_loader.collect_skill_tools()
        names = {t.name for t in tools}
        self.assertIn("scan_dependencies", names)

    def test_empty_env_yields_nothing(self) -> None:
        os.environ["SKILLS"] = ""
        self.assertEqual(skills_loader.skill_definitions(), [])

    def test_unknown_skill_records_error(self) -> None:
        os.environ["SKILLS"] = "not_exist_skill"
        self.assertEqual(skills_loader.skill_definitions(), [])
        self.assertTrue(skills_loader.status_text())
        self.assertTrue(any("not_exist_skill" in e for e in skills_loader._last_errors))

    def test_tool_symlink_outside_skill_directory_is_not_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "linked_skill"
            skill_dir.mkdir()
            (skill_dir / "skill.md").write_text("enabled", encoding="utf-8")
            marker = root / "executed.txt"
            external_tool = root / "outside_tools.py"
            external_tool.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
            try:
                (skill_dir / "tools.py").symlink_to(external_tool)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"符号链接不可用：{exc}")

            with (
                patch.object(skills_loader, "SKILLS_DIR", root),
                patch.object(skills_loader, "_last_loaded", ["linked_skill"]),
                patch.object(skills_loader, "_last_errors", []),
            ):
                self.assertEqual(skills_loader.collect_skill_tools(), [])
                self.assertFalse(marker.exists())
                self.assertTrue(any("符号链接" in err for err in skills_loader._last_errors))


if __name__ == "__main__":
    unittest.main()
