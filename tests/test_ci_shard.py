"""CI 分片器（scripts/ci_shard.py）单测。

为什么 CI 工具本身要有测试：分片器算错会让 CI **静默漏跑**测试文件 ——
而"门禁没测到"和"门禁测过并通过"在报告上无法区分。这类失效必须可见。
"""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCRIPTS = BASE / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ci_shard


class CollectTests(unittest.TestCase):
    def test_collects_only_test_files(self) -> None:
        files = ci_shard.collect_test_files()
        self.assertGreater(len(files), 100, "测试文件数量异常，分片基准可能失效")
        for p in files:
            self.assertTrue(p.name.startswith("test_"), f"非测试文件被收进分片：{p.name}")
            self.assertFalse(p.name.startswith("_"), f"辅助模块被收进分片：{p.name}")

    def test_every_file_is_a_real_test(self) -> None:
        """收进来的文件必须真的存在（防止 glob 语义变化导致空跑）。"""
        for p in ci_shard.collect_test_files():
            self.assertTrue(p.is_file(), f"{p} 不存在")


class ShardPartitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = ci_shard.collect_test_files()

    def test_partition_is_complete_and_disjoint(self) -> None:
        """**不漏不重**是分片器的硬要求。

        漏一个 -> 那个文件本轮 CI 从未被执行；重一个 -> 浪费时间且掩盖问题。
        """
        for n in (2, 3, 4, 5):
            with self.subTest(num_shards=n):
                seen: list[Path] = []
                for i in range(n):
                    seen.extend(ci_shard.shard(self.files, n, i))
                self.assertEqual(
                    sorted(seen), sorted(self.files),
                    f"{n} 片时文件集合与源不一致（漏或重）",
                )
                self.assertEqual(
                    len(seen), len(set(seen)),
                    f"{n} 片时出现重复文件",
                )

    def test_output_paths_are_relative_and_forward_slashed(self) -> None:
        """输出必须是相对路径 + 正斜杠：workflow 里直接拼进命令行。"""
        n = 4
        for i in range(n):
            got = ci_shard._fmt(ci_shard.shard(self.files, n, i))
            self.assertNotIn("\\", got, "输出含反斜杠，POSIX shell 下会被当转义")
            for token in got.split():
                self.assertFalse(Path(token).is_absolute(), f"输出了绝对路径：{token}")
                self.assertTrue((BASE / token).is_file(), f"输出路径不存在：{token}")

    def test_empty_shard_is_reported_not_silently_accepted(self) -> None:
        """片数 > 文件数时，多出来的片应显式报错（exit 2），不能静默产出空命令。"""
        rc = ci_shard.main(["--num-shards", "500", "--shard-id", "499"])
        self.assertEqual(rc, 2, "空片必须显式报错，否则 CI 会跑一个空 pytest")

    def test_invalid_shard_id_exits_nonzero(self) -> None:
        for bad in (4, 99, -1):
            with self.subTest(shard_id=bad):
                with self.assertRaises(ValueError):
                    ci_shard.shard(self.files, 4, bad)

    def test_heavy_test_does_not_share_shard_with_crowd(self) -> None:
        """并发压力用例必须被分出去，不能和 27 个文件挤在一片。

        依据：簇 3（SQLITE_READONLY）只在并发下复现；把它埋进拥挤的片里，
        等于把这条红灯永久藏起来 —— 那比 CI 慢更糟。
        """
        n = 4
        stress = [p for p in self.files if p.name == "test_concurrency_stress.py"]
        self.assertEqual(len(stress), 1, "找不到并发压力用例文件")
        host = [i for i in range(n) if stress[0] in ci_shard.shard(self.files, n, i)]
        self.assertEqual(len(host), 1, "压力用例被重复分片")
        self.assertLessEqual(
            len(ci_shard.shard(self.files, n, host[0])), 30,
            "压力用例所在片文件数过多，失去独占意义",
        )

    def test_list_mode_prints_every_shard(self) -> None:
        rc = ci_shard.main(["--num-shards", "4", "--list"])
        self.assertEqual(rc, 0)

    def test_missing_shard_id_is_usage_error(self) -> None:
        self.assertEqual(ci_shard.main(["--num-shards", "4"]), 2)


if __name__ == "__main__":
    unittest.main()
