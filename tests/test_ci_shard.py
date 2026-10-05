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

    def test_heavy_test_is_the_most_expensive_file(self) -> None:
        """并发压力用例的**代价必须严格高于所有其它测试文件**。

        依据：`shard()` 是贪心（按代价降序，第一个文件必然落进第一个空片）。
        压力用例 `test_concurrency_stress.py` 只有 17KB 源码，却要跑 14~41s；
        若不靠 `HEAVY_TESTS` 抬价，它的代价排名会掉到第 17 名 —— 那时它被排在
        约 1/4 的文件之后分配，所在片已装了三十来个文件，「只在并发下复现」的
        簇 3（SQLITE_READONLY）红灯就会被埋在拥挤的片里长期藏住。

        **判据为什么是「代价排名」而不是「所在片总代价最高」**（此处改过三次判据，
        每次都要做中和实验，前两版都全绿 —— 记在这里防止再犯）：

        - v1「≤30 个文件」：只在校验 126 个文件时成立，2026-10-05 加了 2 个测试
          文件（126→128）即触发 31/32/32/33 全部越界。**把「分片总数」和
          「分片大小」耦合错了** —— 新增测试这个正常动作反而让断言变红。
        - v2「文件数 ≤ 其它片中位数」：文件数几乎均匀（129/4≈32），无论权重
          如何都差不了 1 个，中和实验（权重 60→0）根本咬不住。
        - v3「所在片总代价 == max」：方向对，**但量级选错**。贪心算法本身就是在
          均衡各片，各片总代价必然接近 —— 实测 129 文件下四片为
          `[329.4, 328.5, 328.5, 328.6]`，极差仅 0.9；而 `places=1` 只容差 0.05。
          换句话说这是一个**布尔量**，而决定它的差距只有 0.7，任何人编辑任意一个
          测试文件都可能让它翻转。实测同一份代码两次运行结论相反：一次
          `328.0 == 328.7` 通过，另一次 `[328.0, 327.9, 328.7, 327.7]` 中 host
          落在次重片、报红 `327.979 != 328.706`。**噪声压过信号**。
        - v4（本版）「代价排名 == 1」：直接读机制本身，**无量纲、无阈值**。
          排名是全序的整数，文件字节怎么变都不会翻转（第二名与第一名的差距是
          12.2，不是 0.7）；且对「权重被调小/被删」立刻敏感（w=60→第 1，
          w=5→第 11，w=0→第 17）。这就是「判据指标必须选对缺陷敏感的那个」。

        判据用「严格大于第二名 + 留足裕度」而不是「相等」：若某天新增了一个
        50KB 级别的大测试文件，代价差可能缩到个位数，留 5.0 的裕度仍能报红，
        但不会因为 0.1 的抖动误报。
        """
        ordered = sorted(self.files, key=lambda p: (-ci_shard._cost_of(p), p.name))
        top, second = ordered[0], ordered[1]
        self.assertEqual(
            top.name, "test_concurrency_stress.py",
            f"代价最高的文件是 {top.name}（{ci_shard._cost_of(top):.1f}）而不是"
            f" 并发压力用例 —— 压力用例排第 "
            f"{[p.name for p in ordered].index('test_concurrency_stress.py') + 1}。"
            f" `HEAVY_TESTS` 的权重可能已被调小或删除，它会被埋在拥挤的片里。",
        )
        lead = ci_shard._cost_of(top) - ci_shard._cost_of(second)
        self.assertGreater(
            lead, 5.0,
            f"压力用例只比第二名（{second.name}）贵 {lead:.2f} —— 领先幅度太小，"
            f"任何文件大小变动都可能把排名挤掉，这条断言将变成随机红。",
        )

    def test_heavy_ranking_is_caused_by_the_weight_not_by_luck(self) -> None:
        """**中和实验固化成用例**：上一条断言必须真的由 `HEAVY_TESTS` 造成。

        没有这条，`test_heavy_test_is_the_most_expensive_file` 可能因为
        「压力用例恰好是最大文件」而恒绿 —— 那就是 test vacuity：
        测「通过」不等于断言有鉴别力。这里把权重临时归零，断言排名必须掉下去，
        从反面证明那条绿是权重挣来的。

        `HEAVY_TESTS` 是模块级全局字典，直接改会污染同进程内的其它用例，
        故必须 try/finally 还原。
        """
        key = "test_concurrency_stress.py"
        saved = dict(ci_shard.HEAVY_TESTS)
        try:
            ci_shard.HEAVY_TESTS[key] = 0.0
            ordered = sorted(self.files, key=lambda p: (-ci_shard._cost_of(p), p.name))
            rank = [p.name for p in ordered].index(key) + 1
            self.assertGreater(
                rank, 1,
                f"权重归零后压力用例仍排第 1 —— 说明上一条断言绿的不是权重的功劳，"
                f"而是压力用例本来就最大。这条中和用例自己失效了。",
            )
        finally:
            ci_shard.HEAVY_TESTS.clear()
            ci_shard.HEAVY_TESTS.update(saved)

    def test_heavy_weight_actually_isolates_the_stress_file(self) -> None:
        """构造性验证机制本身：权重够大时压力用例**独占一片**。

        为什么真实目录里测不出来：129 个测试文件分 4 片，怎么都不可能独占。
        上面两条断言锁的是「排名」这个**前提**，缺了「排名 1 ⇒ 独占」这条推论
        的直接证据 —— 若哪天 `shard()` 的贪心改了（比如改成 round-robin），
        排名照样是 1，但「埋在拥挤片里」的原始目的就无声地丢了。

        这里用构造出来的文件集直接检验隔离行为，**不依赖真实目录的规模**，
        因此对文件数变化免疫。文件是真文件（`Path.stat` 要真实大小），
            但放在临时目录，不污染 tests/。
        """
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as td:
            root = _Path(td)
            for i in range(10):
                (root / f"test_plain{i}.py").write_bytes(b"x" * 20000)
            stress = root / "test_concurrency_stress.py"
            stress.write_bytes(b"x" * 1000)
            files = [p for p in sorted(root.glob("test_*.py"))]

            saved = dict(ci_shard.HEAVY_TESTS)
            try:
                # 权重生效：压力用例（代价 61）应当被第一个分配，独占成本最低的空片
                ci_shard.HEAVY_TESTS["test_concurrency_stress.py"] = 60.0
                hosted = [i for i in range(4)
                          if stress in ci_shard.shard(files, 4, i)]
                self.assertEqual(len(hosted), 1, "压力用例被重复分片")
                self.assertEqual(
                    len(ci_shard.shard(files, 4, hosted[0])), 1,
                    f"权重生效时压力用例应独占一片，实际与 "
                    f"{len(ci_shard.shard(files, 4, hosted[0])) - 1} 个文件混在一起"
                    f" —— 「重用例不被埋在人群里」这条目的没有实现。",
                )
            finally:
                ci_shard.HEAVY_TESTS.clear()
                ci_shard.HEAVY_TESTS.update(saved)

    def test_heavy_test_is_not_duplicated_across_shards(self) -> None:
        """压力用例必须恰好落在一片里（原有断言，保留）。"""
        n = 4
        stress = [p for p in self.files if p.name == "test_concurrency_stress.py"]
        self.assertEqual(len(stress), 1, "找不到并发压力用例文件")
        host = [i for i in range(n) if stress[0] in ci_shard.shard(self.files, n, i)]
        self.assertEqual(len(host), 1, "压力用例被重复分片")

    def test_list_mode_prints_every_shard(self) -> None:
        rc = ci_shard.main(["--num-shards", "4", "--list"])
        self.assertEqual(rc, 0)

    def test_missing_shard_id_is_usage_error(self) -> None:
        self.assertEqual(ci_shard.main(["--num-shards", "4"]), 2)


if __name__ == "__main__":
    unittest.main()
