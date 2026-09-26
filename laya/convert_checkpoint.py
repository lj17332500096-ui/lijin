"""把 train_laya_forge.py 产出的 forge_checkpoint.pt 转成 Agent 可加载的完整 checkpoint。

Agent 加载需要目录含：rl_agent_config.json + model.safetensors + tokenizer。
但训练脚本只存了 forge_checkpoint.pt（state_dict），没转 safetensors、没拷 config。
本脚本补齐。

用法：
  .venv/Scripts/python.py convert_checkpoint.py \
      --pt data/laya_forge/forge_finetuned/forge_checkpoint.pt \
      --src-config data/laya_forge/english/rl_agent_config.json \
      --src-tokenizer data/laya_forge/english/tokenizer \
      --out data/laya_forge/forge_finetuned
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pt", required=True, help="训练产出的 forge_checkpoint.pt 路径")
    ap.add_argument("--src-config", required=True, help="源 rl_agent_config.json 路径")
    ap.add_argument("--src-tokenizer", required=True, help="源 tokenizer 目录路径")
    ap.add_argument("--out", required=True, help="输出目录（Agent 可加载）")
    args = ap.parse_args()

    import torch
    from safetensors.torch import save_file

    pt = Path(args.pt)
    if not pt.exists():
        print(f"[convert] ✗ 找不到 {pt}")
        return 1

    # 1. 加载 state_dict
    print(f"[convert] 加载 {pt}")
    ckpt = torch.load(pt, map_location="cpu")
    state_dict = ckpt["state_dict"]
    print(f"[convert]  state_dict 张量数: {len(state_dict)}  训练 epoch: {ckpt.get('epoch')}")

    # 2. 转 safetensors
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # state_dict 的 key 可能是 "model.xxx" 前缀（Laya Agent 内部），保持原样
    safetensors_path = out / "model.safetensors"
    save_file(state_dict, str(safetensors_path))
    print(f"[convert]  已写 {safetensors_path} ({safetensors_path.stat().st_size/1e6:.1f}MB)")

    # 3. 拷 config
    cfg_src = Path(args.src_config)
    cfg_dst = out / "rl_agent_config.json"
    shutil.copy2(cfg_src, cfg_dst)
    print(f"[convert]  已拷 {cfg_src.name}")

    # 4. 拷 tokenizer（若 out 里没有）
    tok_src = Path(args.src_tokenizer)
    if tok_src.is_dir():
        tok_dst = out / "tokenizer"
        if tok_dst.exists():
            shutil.rmtree(tok_dst)
        shutil.copytree(tok_src, tok_dst)
        print(f"[convert]  已拷 tokenizer/ ({len(list(tok_dst.iterdir()))} 文件)")

    print(f"[convert] 完成：{out} 已是 Agent 可加载目录")
    print(f"下一步：eval_route_head.py --checkpoint {out} ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
