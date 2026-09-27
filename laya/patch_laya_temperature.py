"""
FORGE Laya 路线A · checkpoint 改造（task #29）

目标：
- 把 Laya english checkpoint 里烤死的 temperature（默认 1.64/1.25/1.98，
  二分类 2-option 命中 choice:2=1.9 最高档，把 confidence 洗散到 0.002~0.92）
  压到 0.3（落在 0.2~0.5 区间，让 confidence 有区分度）。
- 不碰 HF 缓存原始文件（升级会被覆盖），而是把 checkpoint 复制到项目内
  data/laya_forge/english/ ，改那里的 rl_agent_config.json，训练也基于它。

产出：
- data/laya_forge/english/            完整 checkpoint 副本（权重 + 改后的 config）
- data/laya_forge/english/rl_agent_config.json  temperature=0.3
- data/laya_forge/temperature_patched.json       记录改了什么，供追溯

用法：
  python patch_laya_temperature.py            # 默认 HF 缓存源
  python patch_laya_temperature.py --temp 0.4 # 指定温度
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def _default_hf_snapshot() -> str:
    hub_cache = (os.getenv("HF_HUB_CACHE") or "").strip()
    if hub_cache:
        cache_root = Path(hub_cache).expanduser()
    else:
        hf_home = (os.getenv("HF_HOME") or "").strip()
        home_root = Path(hf_home).expanduser() if hf_home else Path.home() / ".cache" / "huggingface"
        cache_root = home_root / "hub"
    return str(cache_root / "models--convaiinnovations--laya" / "snapshots" /
               "1c5edc17a7acd8701df6fc341c0d179f1c62c982")


def main() -> int:
    ap = argparse.ArgumentParser(description="Laya checkpoint temperature 改造")
    ap.add_argument("--temp", type=float, default=0.3, help="目标温度（默认 0.3）")
    ap.add_argument(
        "--src",
        default=_default_hf_snapshot(),
        help="Laya english checkpoint 源目录（HF 缓存 snapshot）",
    )
    ap.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "laya_forge", "english"),
        help="改造后 checkpoint 落地目录",
    )
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"✗ 源 checkpoint 不存在: {src}")
        print(f"  请先跑 HF 下载（HF_HUB_DISABLE_XET=1 python _laya_eval.py 会触发）")
        return 1

    out = Path(args.out)
    print(f"[1/3] 复制 checkpoint 到 {out} ...")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out)

    cfg_path = out / "rl_agent_config.json"
    if not cfg_path.exists():
        print(f"✗ {cfg_path} 不存在（checkpoint 结构变了？）")
        return 1

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    before = {
        "temperature": cfg.get("temperature"),
        "temperature_by_options": cfg.get("temperature_by_options"),
    }

    # 压温度：主 temperature 全档 + 按 option 数细分的覆盖全压到目标值
    T = args.temp
    cfg["temperature"] = [T, T, T]
    tbo = cfg.get("temperature_by_options") or {}
    cfg["temperature_by_options"] = {k: T for k in tbo}
    # 标记 FORGE 改造版
    cfg["forge_patched"] = True
    cfg["forge_patch_note"] = (
        f"temperature patched to {T} (was {before.get('temperature')}); "
        f"by_options was {before.get('temperature_by_options')}"
    )
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 审计记录
    audit = out.parent / "temperature_patched.json"
    audit.write_text(
        json.dumps(
            {
                "source": str(src),
                "output": str(out),
                "temperature_before": before["temperature"],
                "temperature_after": [T, T, T],
                "by_options_before": before["temperature_by_options"],
                "by_options_after": {k: T for k in (before.get("temperature_by_options") or {})},
                "model_name": cfg.get("model_name"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"[2/3] temperature: {before['temperature']} -> {[T,T,T]}")
    print(f"      by_options: {before['temperature_by_options']}")
    print(f"[3/3] 已写 {audit}")
    print(f"\n✅ 改造完成: {out}")
    print("下一步：train_laya_forge.py 基于此 checkpoint 微调（task #30）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
