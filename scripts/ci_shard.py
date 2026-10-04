"""CI 分片器：把 tests/ 下的测试文件按"预估耗时"均衡切成 N 片。

# 为什么不直接用 `pytest --shard`

`--shard` 不是 pytest 内置能力（属 pytest-shard 插件），本仓库未装该插件，
照抄 P1 报告 §5.2 的 `--shard-id/--num-shards` 会直接报 unrecognized arguments。
故自己实现，且**不引新依赖**（引依赖就要在 CI 里多一处可能装不上的东西）。

# 为什么不用"按文件名排序平均切"

测试耗时与文件名无关（`test_zzz_*` 不比 `test_aaa_*` 慢）。按序号平均切会
随机把 `test_concurrency_stress.py`（本仓库唯一的并发压力用例，单跑 14~41s）
和一批 0.5s 的用例放进同一片，造成**木桶效应**：4 片里有一片决定总时长。
故用**文件大小**作代价的廉价代理（大小与用例数/导入成本正相关），
并对已知重用例显式加权。

用法：
    python scripts/ci_shard.py --num-shards 4 --shard-id 0
    # 或输出全部片（调试用）：
    python scripts/ci_shard.py --num-shards 4 --list
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = PROJECT_ROOT / "tests"

#: 已知重用例的额外权重（秒级估计，叠加在文件大小代价之上）。
#: concurrency_stress 是本仓库唯一的并发压力用例，必须独占一片 ——
#: 簇 3（SQLITE_READONLY）只在并发下复现，塞进拥挤的片里等于把这条红灯藏起来。
HEAVY_TESTS: dict[str, float] = {
    "test_concurrency_stress.py": 60.0,
}


def _cost_of(path: Path) -> float:
    """代价 = 文件字节数/1000 + 已知重用例权重（粗略但比平均切好得多）。"""
    try:
        size_kb = path.stat().st_size / 1000.0
    except OSError:
        size_kb = 1.0
    return size_kb + HEAVY_TESTS.get(path.name, 0.0)


def collect_test_files() -> list[Path]:
    """收集 tests/ 下真正被 pytest 收集的测试文件。

    只认 `test_*.py`（pytest 默认 python_files），并排除下划线开头的文件
    —— `_FakeAgent` 这类辅助模块不是测试用例，收进来会让 pytest 报
    "no tests ran"。
    """
    if not TESTS_DIR.is_dir():
        return []
    return sorted(
        p for p in TESTS_DIR.glob("test_*.py")
        if p.is_file() and not p.name.startswith("_")
    )


def shard(files: list[Path], num_shards: int, shard_id: int) -> list[Path]:
    """把文件贪心分到 num_shards 片，返回第 shard_id 片（0-based）。

    贪心（按代价降序，每次放进当前最轻的片）而不是 round-robin：
    目标是最小化**最慢那一片**，这才是 CI 总时长的决定因素。
    """
    if num_shards < 1:
        raise ValueError("num_shards 必须 >= 1")
    if not 0 <= shard_id < num_shards:
        raise ValueError(f"shard_id 必须在 [0,{num_shards}) 内，收到 {shard_id}")

    ordered = sorted(files, key=lambda p: (-_cost_of(p), p.name))
    buckets: list[list[Path]] = [[] for _ in range(num_shards)]
    loads = [0.0] * num_shards
    for path in ordered:
        target = min(range(num_shards), key=lambda i: loads[i])
        buckets[target].append(path)
        loads[target] += _cost_of(path)
    return sorted(buckets[shard_id])


def _fmt(paths: list[Path]) -> str:
    return " ".join(str(p.relative_to(PROJECT_ROOT)).replace("\\", "/") for p in paths)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 tests/ 均衡切成 N 片")
    ap.add_argument("--num-shards", type=int, required=True)
    ap.add_argument("--shard-id", type=int)
    ap.add_argument("--list", action="store_true",
                    help="输出全部片（每行 'i: file1 file2 ...'）")
    args = ap.parse_args(argv)

    files = collect_test_files()
    if not files:
        print("[ci_shard] tests/ 下没有测试文件", file=sys.stderr)
        return 2

    if args.list:
        for i in range(args.num_shards):
            print(f"{i}: {_fmt(shard(files, args.num_shards, i))}")
        return 0

    if args.shard_id is None:
        print("[ci_shard] 需要 --shard-id 或 --list", file=sys.stderr)
        return 2

    picked = shard(files, args.num_shards, args.shard_id)
    if not picked:
        print(f"[ci_shard] 第 {args.shard_id} 片为空（--num-shards 超过测试文件数？）",
              file=sys.stderr)
        return 2
    print(_fmt(picked))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
