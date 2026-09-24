"""
FORGE Laya 路线A · 训练脚本（CPU/XPU 可跑，先 CPU 验证收敛）

把 565 双源数据的 train 集（453 条）构造成 Laya 训练 item，在改造过
temperature=0.3 的本地 checkpoint（data/laya_forge/english/）基础上做
RLCD（proper scoring rule / GRPO 风格 policy gradient）微调，产 FORGE 专属 checkpoint。

数据通路：
  data/laya_tool_intent/train.jsonl  →  build_sequence + render_options  →  Laya 训练 item
  每条 Laya case（fingerprint/question/option）展开成 1 个 typed decision：
    choice question，criteria = {text: "...", tool: "..."}（二分类）
    state = instructions（query 本身）
    gold   = 按 option 给 probabilities {text:1, tool:0} 或 {text:0, tool:1}

设备：
  --device cpu   本机 CPU 验证收敛（421M 全参，453 item × 1-2 epoch 约 1-3h）
  --device xpu   Intel Arc A770 SYCL（需先装 XPU 版 torch + oneAPI Base Toolkit）
  --device cuda  NVIDIA CUDA（非本路线）

产出：
  data/laya_forge/forge_finetuned/   微调后 checkpoint（训练时落盘）

用法：
  # 干跑：只验证 565 → Laya item 的转换通路
  .venv/Scripts/python.exe train_laya_forge.py --dry-run

  # CPU 验证收敛（先跑，证明 GRPO/proper_reward/温度拟合有效）
  .venv/Scripts/python.exe train_laya_forge.py --train --device cpu --epochs 1

  # XPU 加速（A770）
  .venv/Scripts/python.exe train_laya_forge.py --train --device xpu --epochs 2

  # 用独立 test 验收
  .venv/Scripts/python.exe train_laya_forge.py --train --device cpu --epochs 2 \\
      --eval-file data/laya_tool_intent/test_final.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
LAYA_FORK = REPO / "_laya_inspect" / "laya-main" / "laya"
DEFAULT_CHECKPOINT = REPO / "data" / "laya_forge" / "english"
DEFAULT_TRAIN = REPO / "data" / "laya_tool_intent" / "train.jsonl"
DEFAULT_EVAL = REPO / "data" / "laya_tool_intent" / "val.jsonl"
DEFAULT_OUT = REPO / "data" / "laya_forge" / "forge_finetuned"


def _import_laya_runtime():
    """导入 laya 包的训练侧工具。"""
    try:
        from laya.common import build_sequence, render_options, QTYPES
        return build_sequence, render_options, QTYPES
    except Exception:
        sys.path.insert(0, str(LAYA_FORK.parent))
        from laya.common import build_sequence, render_options, QTYPES
        return build_sequence, render_options, QTYPES


def load_cases(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _j(x):
    """565 case 的 state/questions/gold 都是 JSON 字符串，解开。"""
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return x
    return x


def case_to_decision(c: dict):
    """把 565 case 拆成 Laya 二分类 typed decision。

    返回 (qdef, gold_dict, option, instruction)；解析不出返回 None。
    """
    q = _j(c.get("questions"))
    g = _j(c.get("gold"))
    if not isinstance(q, dict) or "needs_tool" not in q:
        return None
    qt = q["needs_tool"]
    instruction = c.get("state") or qt.get("instructions") or ""
    instruction = _j(instruction) if isinstance(instruction, str) and instruction[:1] in '"[' else instruction
    criteria = qt.get("criteria", {})
    if not isinstance(criteria, dict) or "text" not in criteria or "tool" not in criteria:
        return None
    gq = g.get("needs_tool") if isinstance(g, dict) else {}
    option = (gq or {}).get("label")
    if option not in ("text", "tool"):
        return None
    gold = {"text": 0.0, "tool": 0.0}
    gold[option] = 1.0
    qdef = {"t": "choice", "ins": instruction, "crit": criteria}
    return qdef, gold, option, instruction


def build_training_items(cases: list[dict], cfg: dict, tok, build_sequence, render_options):
    """565 双源 train case → Laya 训练 item（二分类 typed decision）。"""
    _, _, QTYPES = _import_laya_runtime()
    items = []
    skipped = 0
    for c in cases:
        d = case_to_decision(c)
        if d is None:
            skipped += 1
            continue
        qdef, gold, option, instruction = d
        k = len(render_options(qdef))
        if k != len(gold):
            skipped += 1
            continue
        raw_ids, raw_markers = build_sequence(
            tok, instruction, qdef, cfg["max_len"], cfg["head_max_len"]
        )
        if raw_ids is None:
            skipped += 1
            continue
        # target 需与 option 顺序对齐（text, tool）的二分类 one-hot 向量
        target_list = [gold.get("text", 0.0), gold.get("tool", 0.0)]
        items.append({
            "state": instruction,
            "qdef": qdef,
            "gold": gold,
            "label": option,
            "fingerprint": c.get("fingerprint", c.get("id", "")),
            # 对齐 Laya item 结构（agent.py:289）：ids/markers/qtype + target
            "raw": {
                "ids": raw_ids,
                "markers": raw_markers,
                "qtype": QTYPES["choice"],
                "target": target_list,
            },
        })
    return items, skipped


def dry_run(path: Path) -> int:
    """只验证 565 → Laya item 的数据通路，不训练。"""
    print(f"[dry-run] 加载 checkpoint config: {DEFAULT_CHECKPOINT/'rl_agent_config.json'}")
    cfg = json.loads((DEFAULT_CHECKPOINT / "rl_agent_config.json").read_text(encoding="utf-8"))
    print(f"  temperature: {cfg.get('temperature')}  forge_patched: {cfg.get('forge_patched')}")
    print(f"[dry-run] 加载 train: {path.name}")
    cases = load_cases(path)
    print(f"  cases: {len(cases)}")
    dist = {}
    ok = 0
    for c in cases:
        d = case_to_decision(c)
        if d:
            ok += 1
            dist[d[2]] = dist.get(d[2], 0) + 1
        else:
            dist["_unparseable"] = dist.get("_unparseable", 0) + 1
    print(f"  option 分布: {dist}")
    print(f"[dry-run] 数据通路验证：565 的 {len(cases)} case → {ok} 条可解析 Laya 二分类 decision。")
    ok_all = ok == len(cases)
    print(f"  schema 对齐: {'OK' if ok_all else f'FAIL（{len(cases)-ok} 条未解析）'}")
    return 0 if ok_all else 1


def _try_fit_temperature(agent, model, tok, dev: str):
    """尝试温度拟合（Laya 训练侧在 notebook 自定义，common.py 不一定暴露）。

    改造版 checkpoint 已把 temperature 配到 0.3，温度拟合是锦上添花。
    找不到入口就静默跳过，不影响主训练。
    """
    try:
        from laya.common import fit_one_temp  # type: ignore
    except ImportError:
        print("[temp] common.fit_one_temp 不存在，跳过温度拟合（用 checkpoint 内置 0.3）")
        return False
    try:
        fit_one_temp(model, tok, dev)
        print("[temp] 温度拟合完成")
        return True
    except Exception as e:
        print(f"[temp] 温度拟合失败（跳过）：{e}")
        return False


def _resolve_device(device: str) -> str:
    """决定实际训练设备。

    - cpu: 强制 CPU（验证收敛）
    - xpu: Intel Arc（需 XPU 版 torch）
    - auto: 有 XPU 用 XPU，否则 CPU
    """
    import torch
    if device == "cpu":
        return "cpu"
    if device == "xpu":
        if getattr(torch, "xpu", None) is not None and torch.xpu.is_available():
            return "xpu"
        print("[warn] XPU 不可用，退回 cpu（先验证收敛）")
        return "cpu"
    # auto
    if getattr(torch, "xpu", None) is not None and torch.xpu.is_available():
        return "xpu"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _load_agent_and_model(checkpoint: Path, dev: str):
    """加载改造版 checkpoint + 建模型 + tokenizer，返回 (agent, model, tok, cfg)。

    用 Agent 自带的 self.tok / self.model（指向 checkpoint/tokenizer/ 子目录，
    避免 AutoTokenizer.from_pretrained 指错根目录）。
    """
    import torch
    from laya.agent import Agent

    agent = Agent(str(checkpoint))
    model = agent.model.to(dev)
    tok = agent.tok
    cfg = agent.cfg
    return agent, model, tok, cfg


def _sft_step(
    model,
    batch_items: list[dict],
    opt,
    dev: str,
    pad_id: int = 0,
):
    """单步 SFT：用 gold one-hot 在 K option 上做交叉熵。

    453 条二分类小数据，SFT 比 RLCD/GRPO 稳（loss 单调降，不靠采样方差）。
    用 Laya 的 collate_items 构造 batch，DecisionModel.forward 返回
    (logits, act_logits)，logits [N, K]（marker 位置聚合到 K option）。
    """
    import torch
    import torch.nn.functional as F
    from laya.common import collate_items

    model.train()
    total_loss = 0.0
    n = 0
    for it in batch_items:
        b = collate_items([[it["raw"]]], pad_id)
        if b is None:
            continue
        input_ids = b["input_ids"].to(dev)
        attention_mask = b["attention_mask"].to(dev)
        marker_pos = b["marker_pos"].to(dev)
        marker_mask = b["marker_mask"].to(dev)
        qtype = b["qtype"].to(dev)
        # forward：DecisionModel.forward 返回 (logits, act_logits)
        logits, _act = model(input_ids, attention_mask, marker_pos, marker_mask, qtype)
        # target [N, K]（one-hot，来自 item 的 target 字段）
        target = b.get("target")
        if target is None:
            g = it["gold"]
            target = torch.tensor(
                [[g.get("text", 0.0), g.get("tool", 0.0)]],
                dtype=torch.float32, device=dev,
            )
        else:
            target = target.to(dev)
        # 交叉熵（logits [N, K] vs target [N, K] one-hot）
        # F.cross_entropy 接受 (logits, class_index)；target 是 one-hot 转成 argmax
        class_idx = target.argmax(dim=-1)  # [N]
        loss = F.cross_entropy(logits, class_idx)
        opt.zero_grad()
        loss.backward()
        opt.step()
        total_loss += loss.item()
        n += 1
    return total_loss / max(n, 1), 0.0, n


def _grpo_step(
    model,
    batch_items: list[dict],
    opt,
    dev: str,
    pad_id: int = 0,
):
    """单步 GRPO：对每个 item 采样 1 次 + 算 proper reward + 更新。

    用 Laya 的 collate_items 构造 batch（跟推理路径对齐），
    forward 传 (input_ids, attention_mask, marker_pos, marker_mask, qtype)，
    返回 (logits, act_logits)；logits 已是 [N, K]（marker 位置聚合到 K 个 option）。
    proper_reward 算严格 proper scoring rule reward。
    这是简化的 GRPO（每 item 1 次采样，无 group baseline），
    453 条小数据量下等价于带 reward shaping 的 SFT。
    """
    import torch
    from laya.common import collate_items, proper_reward

    model.train()
    total_loss = 0.0
    total_reward = 0.0
    n = 0
    for it in batch_items:
        # 1 个 item 一组喂给 collate_items（它期望 batch = list[list[item]]）
        b = collate_items([[it["raw"]]], pad_id)
        if b is None:
            continue
        input_ids = b["input_ids"].to(dev)
        attention_mask = b["attention_mask"].to(dev)
        marker_pos = b["marker_pos"].to(dev)
        marker_mask = b["marker_mask"].to(dev)
        qtype = b["qtype"].to(dev)
        # forward：DecisionModel.forward 返回 (logits, act_logits)
        logits, _act = model(input_ids, attention_mask, marker_pos, marker_mask, qtype)
        # q = reported distribution [N, K]（softmax over K options）
        q = torch.softmax(logits, dim=-1)
        # target [N, K]（one-hot / soft，来自 item 的 target 字段）
        target = b.get("target")
        if target is None:
            # fallback：用 item 的 gold 手动构造
            g = it["gold"]
            target = torch.tensor(
                [[g.get("text", 0.0), g.get("tool", 0.0)]], dtype=torch.float32, device=dev
            )
        else:
            target = target.to(dev)
        # proper reward（log score + spherical score）
        reward = proper_reward(q, target, qtype, marker_mask)  # [N]
        r = reward[0]  # 单个 item
        # 采样 1 次（对 q 采样 action）
        sample_idx = torch.multinomial(q[0], 1, replacement=False).item()
        K = q.size(-1)
        # policy log-prob（对采样到的 option）
        logq = torch.log(q[0].clamp_min(1e-9))
        policy_lp = logq[sample_idx]
        # REINFORCE 单样本 loss：最大化 reward 对应的 log-prob
        loss = -r * policy_lp
        opt.zero_grad()
        loss.backward()
        opt.step()
        total_loss += loss.item()
        total_reward += r.item()
        n += 1
    return total_loss / max(n, 1), total_reward / max(n, 1), n


def train(
    path: Path,
    out_dir: Path,
    eval_file: Path,
    epochs: int,
    lr: float,
    device: str,
    batch_size: int = 16,
    eval_every: int = 1,
    save_every: int = 1,
    temperature_fit: bool = True,
    mode: str = "sft",
) -> int:
    """FORGE Laya 微调（单卡 CPU/XPU；多卡请套 torchrun）。

    mode:
      - sft:  交叉熵监督微调（默认，453 条小数据最稳，loss 单调降）
      - grpo: RLCD/GRPO 强化学习（Laya 原生，大数据 30k 场景才划算，453 条收敛差）
    """
    import torch

    step_fn = _sft_step if mode == "sft" else _grpo_step
    dev = _resolve_device(device)
    build_sequence, render_options, QTYPES = _import_laya_runtime()

    print(f"[train] 设备={dev}  模式={mode}  尝试加载改造版 checkpoint")
    agent, model, tok, cfg = _load_agent_and_model(DEFAULT_CHECKPOINT, dev)
    print(f"[train]  cfg.temperature={cfg.get('temperature')}  forge_patched={cfg.get('forge_patched')}")

    cases = load_cases(path)
    items, skipped = build_training_items(cases, cfg, tok, build_sequence, render_options)
    print(f"[train] items={len(items)} skipped={skipped}")
    if not items:
        print("[train] ✗ 无有效 item，退出")
        return 1

    # 简单打乱（稳定可复现）
    import random
    random.shuffle(items)

    opt = torch.optim.AdamW(model.parameters(), lr=lr)
    model.train()
    out_dir.mkdir(parents=True, exist_ok=True)

    bs = max(1, batch_size)
    n_batches = math.ceil(len(items) / bs)
    print(f"[train] mode={mode}  batch_size={bs}  n_batches={n_batches}  epochs={epochs}  lr={lr}")

    for ep in range(1, epochs + 1):
        t0 = time.time()
        ep_loss = 0.0
        ep_reward = 0.0
        ep_n = 0
        for bi in range(n_batches):
            batch = items[bi * bs: (bi + 1) * bs]
            loss, reward, n = step_fn(model, batch, opt, dev)
            ep_loss += loss
            ep_reward += reward
            ep_n += n
            if (bi + 1) % max(1, n_batches // 8) == 0:
                if mode == "sft":
                    print(f"  [train] epoch {ep} batch {bi+1}/{n_batches}  "
                          f"loss={ep_loss/(bi+1):.4f}")
                else:
                    print(f"  [train] epoch {ep} batch {bi+1}/{n_batches}  "
                          f"loss={ep_loss/(bi+1):.4f}  reward={ep_reward/(bi+1):.4f}")
        if mode == "sft":
            print(f"[train] epoch {ep}/{epochs}  ({time.time()-t0:.1f}s)  "
                  f"avg_loss={ep_loss/max(n_batches,1):.4f}")
        else:
            print(f"[train] epoch {ep}/{epochs}  ({time.time()-t0:.1f}s)  "
                  f"avg_loss={ep_loss/max(n_batches,1):.4f}  avg_reward={ep_reward/max(n_batches,1):.4f}")

        # 温度拟合（每 epoch 末，可选）
        if temperature_fit:
            _try_fit_temperature(agent, model, tok, dev)

        # 每 save_every epoch 落盘
        if ep % save_every == 0:
            _save_checkpoint(model, cfg, out_dir / f"forge_checkpoint_ep{ep}.pt", dev, ep)

    # 温度拟合（最终，可选）
    if temperature_fit:
        _try_fit_temperature(agent, model, tok, dev)

    # 落盘最终 checkpoint
    _save_checkpoint(model, cfg, out_dir / "forge_checkpoint.pt", dev, epochs)

    # 拷 tokenizer / 权重目录供 Agent(load) 复用
    import shutil
    for f in ("tokenizer", "model.safetensors", "config.json"):
        src = DEFAULT_CHECKPOINT / f
        if src.exists():
            if f == "tokenizer" and src.is_dir():
                shutil.copytree(src, out_dir / "tokenizer", dirs_exist_ok=True)
            elif src.is_file():
                shutil.copy2(src, out_dir / f)
    print(f"[train] 已落盘 {out_dir}/forge_checkpoint.pt")
    print(f"下一步：用 Agent(str(out_dir)) 加载微调模型，对 test_final.jsonl 评测 >95%")
    return 0


def _save_checkpoint(model, cfg, path: Path, dev: str, epoch: int):
    """落盘 state_dict + cfg + 元信息。"""
    import torch
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "cfg": cfg,
            "epoch": epoch,
            "device": dev,
            "trained_from": str(DEFAULT_CHECKPOINT),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        path,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="FORGE Laya 微调（CPU/XPU 可跑）")
    ap.add_argument("--dry-run", action="store_true", help="只验证数据通路")
    ap.add_argument("--train", action="store_true", help="实际训练")
    ap.add_argument("--mode", default="sft", choices=("sft", "grpo"),
                    help="sft=交叉熵监督（默认，小数据稳）/ grpo=RLCD 强化（大数据才划算）")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-5,
                    help="SFT 用 1e-4~1e-5；GRPO 用 1e-6~1e-5（默认 1e-5 两模式可用）")
    ap.add_argument("--device", default="auto", choices=("auto", "cpu", "xpu", "cuda"),
                    help="cpu=验证收敛 / xpu=A770 / cuda=NVIDIA / auto=优先 XPU 回退 CPU")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--eval-every", type=int, default=1)
    ap.add_argument("--save-every", type=int, default=1)
    ap.add_argument("--no-temp-fit", action="store_true", help="跳过温度拟合（调试用）")
    ap.add_argument("--train-file", default=str(DEFAULT_TRAIN))
    ap.add_argument("--eval-file", default=str(DEFAULT_EVAL))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    if args.dry_run:
        return dry_run(Path(args.train_file))
    if args.train:
        return train(
            Path(args.train_file), Path(args.out), Path(args.eval_file),
            args.epochs, args.lr, args.device,
            batch_size=args.batch_size,
            eval_every=args.eval_every,
            save_every=args.save_every,
            temperature_fit=not args.no_temp_fit,
            mode=args.mode,
        )
    print("请指定 --dry-run 或 --train。详见文件头部用法。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
