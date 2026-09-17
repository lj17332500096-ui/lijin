import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import github_fetch as gf


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class RepoParseTests(unittest.TestCase):
    def test_url_and_shorthand(self) -> None:
        self.assertEqual(gf.parse_repo("https://github.com/octocat/Hello-World"), ("octocat", "Hello-World"))
        self.assertEqual(gf.parse_repo("octocat/Hello-World"), ("octocat", "Hello-World"))
        self.assertEqual(gf.parse_repo("https://github.com/a/b.git/"), ("a", "b"))
        self.assertEqual(gf.parse_repo("https://github.com/a/b/tree/main/src"), ("a", "b"))

    def test_invalid_rejected(self) -> None:
        self.assertIsNone(gf.parse_repo("https://example.com/a/b"))
        self.assertIsNone(gf.parse_repo("https://github.com/octocat"))
        self.assertIsNone(gf.parse_repo("不是链接"))


class ExtractSafetyTests(unittest.TestCase):
    def test_path_traversal_rejected(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="ghfetch_"))
        with self.assertRaises(ValueError):
            gf._safe_extract_zip(make_zip({"../evil.txt": b"x"}), target)

    def test_absolute_path_rejected(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="ghfetch_"))
        with self.assertRaises(ValueError):
            gf._safe_extract_zip(make_zip({"C:/Windows/evil.txt": b"x"}), target)

    def test_strip_archive_root(self) -> None:
        self.assertEqual(gf._strip_archive_root("Hello-World-main/README.md"), "README.md")

    def test_resolve_entry_blocks_dotdot(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="ghfetch_"))
        self.assertIsNone(gf._resolve_entry(target, "a/../../b.txt"))
        self.assertIsNotNone(gf._resolve_entry(target, "src/x.py"))


class FetchPipelineTests(unittest.TestCase):
    """替换下载器后跑通 抓取→解压→返回路径 全流程（不联网）。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="ghfetch_pipe_"))
        self._orig_dir = gf.REPO_ROOT_DIR
        self._orig_download = gf._download_archive
        gf.REPO_ROOT_DIR = self._tmp
        zip_bytes = make_zip(
            {
                "Hello-World-main/README.md": b"# Hello World\nA sample repo.",
                "Hello-World-main/main.py": b"print('hi')\n",
            }
        )
        gf._download_archive = lambda owner, repo, branch, token="": (zip_bytes, "zip")

    def tearDown(self) -> None:
        gf.REPO_ROOT_DIR = self._orig_dir
        gf._download_archive = self._orig_download

    def test_fetch_extracts_and_reports_path(self) -> None:
        out = gf.fetch_github_repo_impl("https://github.com/octocat/Hello-World", branch="main")
        self.assertIn("已抓取", out)
        self.assertIn("Hello-World", out)
        self.assertIn("index_workspace", out)
        target = self._tmp / "octocat__Hello-World"
        self.assertTrue((target / "README.md").exists())
        self.assertTrue((target / "main.py").exists())
        self.assertFalse((target / "Hello-World-main").exists())

    def test_existing_dir_not_refetched(self) -> None:
        target = self._tmp / "octocat__Hello-World"
        target.mkdir()
        (target / "a.md").write_text("x", encoding="utf-8")
        out = gf.fetch_github_repo_impl("octocat/Hello-World", branch="main")
        self.assertIn("目录已存在", out)

    def test_bad_url_reported(self) -> None:
        out = gf.fetch_github_repo_impl("https://example.com/a/b")
        self.assertIn("无法识别", out)


if __name__ == "__main__":
    unittest.main()
