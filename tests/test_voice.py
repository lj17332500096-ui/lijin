import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import voice


class VoiceTextUtilTests(unittest.TestCase):
    def test_strip_markdown(self) -> None:
        cleaned = voice._strip_markdown("**重点**：详情见 https://example.com/a\n- 第一点")
        self.assertNotIn("**", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("-", cleaned)
        self.assertIn("重点", cleaned)

    def test_split_long_text(self) -> None:
        long_text = "这是第一句。" + "内容" * 80 + "。这是第二句！"
        chunks = voice._split_for_speech(long_text, max_len=120)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 130)

    def test_script_escapes_single_quote(self) -> None:
        script = voice._tts_script("It's 中文'test")
        self.assertIn("'It''s 中文''test'", script)


if __name__ == "__main__":
    unittest.main()
