# 方案 B（A770 SYCL）切换 XPU 训练的操作手册

CPU 验证收敛通过后，按此切换到 Intel Arc A770 XPU 训练（提速 5-10x）。

## 前置（一次性，~20GB 下载）

### 1. 装 Intel oneAPI Base Toolkit（DPC++ 编译器 + Level Zero SDK + SYCL 运行时）
- 下载：https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit.html
- 安装时选 "DPC++/C++" + "Level Zero" + "oneMath" 组件
- 装完把 `IntelOneAPIvars.bat` 加入系统 PATH（或每次 `call` 一次）
- 验证：`sycl-ls`（能列出 A770 设备 = Level Zero 运行时 OK）

### 2. 重装 XPU 版 torch（替换 venv 里的 CPU 版）
```bat
.venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/xpu
```
- 验证：`torch.xpu.is_available() == True` 且 `torch.xpu.device_count() >= 1`
- 显存 16G，跑 421M 全参 453 条 1-2 epoch 估 20-40min

## 切换训练

```bat
:: 先激活 oneAPI 环境
call "C:\Program Files (x86)\Intel\oneAPI\setvars.bat"
:: 跑 XPU 训练（device 切 xpu）
.venv\Scripts\python.exe train_laya_forge.py --train --device xpu --epochs 2 --batch-size 32
```

## 与 CPU 的差异
- 421M 全参 + 453 条：CPU ~30-40min/epoch → XPU 估 5-10min/epoch
- GRPO 采样/forward 在 XPU 上算子覆盖度比 NVIDIA 低（少数算子回退 CPU），但 421M 小模型 453 条数据量小，影响有限
- 如某算子 XPU 不支持，`torch.xpu` 会自动回退 CPU（有 warning），不影响收敛

## 风险点
1. **oneAPI 路径**：`setvars.bat` 要激活正确，否则 `torch.xpu` 找不到 Level Zero 运行时
2. **显存上限**：Windows 下 XPU 默认显存上限 ~75%（16G → ~12G 可用），421M 模型全参 + 453 条 batch 32 绰绰有余
3. **算子回退**：若某些算子 XPU 不支持回退 CPU，速度会打折但能跑
4. **CPU 验证通过是前提**：先确认 CPU 这套 GRPO/proper_reward 对 453 条能收敛，再切 XPU 提速；否则 XPU 跑得快但结果无效
