# FORGE Agent Final Tool Exposure — Phase 21 Addendum
## Model Substitution + Checkpoint Validity Correction

> 日期：2026-09-12
> 关系：**本附录修正并部分推翻**《FORGE-Agent-Final-Tool-Exposure-Phase21》关于 M1 的结论。
> 约束：本地模型；禁止 gateway/agnes/外部模型。

---

## 0. 本轮触发

1. 尝试切换到 `G:\models\Spark-X2.5-4B.gguf` → **失败**：
   `general.architecture = spark2_5`，本机所有 llama.cpp 构建（sycl build 10622 / cpu 9245 / sycl 5388）
   均报 `unknown model architecture: 'spark2_5'`；无网络无法获取新构建。
2. 按指示改用**已支持的小模型**：`G:\models\Ornith-1.5-9B-Q5_K_M.gguf`（6.5GB），
   在 `localhost:8080` 提供服务（`--alias Ornith-1.5-9B`），`.env` 已指向该模型。
3. 用 Ornith 重跑 Phase 21 E0–E4。**重跑过程中发现 Phase 20/21 的 M1 LIVE snapshot 是无效 checkpoint**，
   这才是 Phase 21 主报告里 “M1 恒 0%” 的真正原因。

---

## 1. Executive Summary（修正版）

**根因（checkpoint validity defect）**：Phase 20 抓取的 M1 LIVE snapshot 的最后一次 mutation **失败**：

```text
M1 live snapshot 最后一个 tool output =
错误：在 calc.py 里没有找到要替换的内容（注意区分全半角/换行）。

【执行义务 / Execution obligation】
verification = REQUIRED / status = UNSATISFIED / revision = 1
```

即：`edit_project_file` 实际失败，但 runtime 仍 `mutation_seen=true`、`evidence_epoch=1`、
`verification_due=true`，harness 又只以 `verification_due()+mutation_seen` 为抓取条件 →
**捕获了一个“修改失败却要求验证”的自相矛盾 checkpoint**。模型在此 checkpoint 下自然地继续修复/探索，
所以 M1 在 E0–E4、Qwen 与 Ornith 上**全部 0%** —— 这不是模型选择问题，而是 **benchmark 抓取条件缺陷**。

**修复（harness-only）**：`capture_live_snapshot` 新增 `_mutation_result_ok()` 成功门槛，
只在**真实成功 mutation** 后抓取；重抓得到有效 checkpoint（M1/M4 均为 `edit_project_file` 成功 + obligation）。

**修正后的因果结果**（有效 checkpoint）：

| 变体 | 暴露 | Ornith M1 | Ornith M4 | Qwen M1 | Qwen M4 |
|---|---|---:|---:|---:|---:|
| E0 | 120 | 6/10 | 10/10 | 3/5 | 4/5 |
| E1 | 14 | 8/10 | 8/10 | 3/5 | 4/5 |
| E2 | 11 | 2/10 | 8/10 | 3/5 | 4/5* |
| E3 | 4 | **5/5** | **5/5** | 4/5 | **5/5** |
| E4 | 2 | **5/5** | **5/5** | — | — |

\* Qwen E2 M4 有 1 次 TIMEOUT（240s，非 provider error）。

- **M1 0% 消失**：有效 checkpoint 下 M1 E0 60%、E1 80%、E3/E4 100%。
- **Exposure pollution 仍真实存在但为次要因素**：E0(120) → E3/E4(2–4) 把 combined 从 70–80% 提升到 90–100%。
- **E1（Router-authoritative 14）在有效 checkpoint 上不再优于 E0**（两者约 70–80%）→
  “Router 即权威”仍有边界价值，但不是 M1 的决定因素。
- **E2（verification-focused）跨模型不稳定**（Ornith M1 2/10 vs Qwen 3/5）→ 拒绝。
- **小模型可用**：Ornith-1.5-9B 在相同 checkpoint 上不劣于 Qwen3.6-35B（M4 E0 10/10 vs 4/5），
  延迟约低 3–5 倍。

---

## 2. 模型替换记录

| 项 | 值 |
|---|---|
| 目标模型 | `G:\models\Spark-X2.5-4B.gguf` |
| Spark 架构 | `spark2_5`，4.1B，36 层，ctx 1,048,576，sliding window 512，GQA 16/4，vocab 131072 |
| 结果 | **FAILED**：本机 llama.cpp 不支持该架构；无网络获取新构建 |
| 替代模型 | `G:\models\Ornith-1.5-9B-Q5_K_M.gguf` |
| 服务 | `llama-server.exe -m Ornith... -ngl 99 -c 32768 --port 8080`，`/v1/models -> Ornith-1.5-9B` |
| `.env` | `FORGE_LOCAL_MODEL_NAME=G:\models\Ornith-1.5-9B-Q5_K_M.gguf` |
| Qwen 对照 | 临时切回 `Qwen3.6-35B-A3B` 跑同 checkpoint 对照 |

---

## 3. Checkpoint Validity Correction

Phase 20 的抓取条件（缺陷）：

```text
capture if  rc.verification_due() and rc._t().mutation_seen
```

修正后：

```text
capture if  rc.verification_due() and rc._t().mutation_seen and last_mutation_ok
last_mutation_ok = _mutation_result_ok(last_mutation_tool_result)
```

`_mutation_result_ok` 规则：结果含 `错误/没有找到/未找到/失败/not found/error` → False；
含 `已写入/已在/替换/已保存/已创建/已修改/已更新/已生成` → True。

重抓结果（`phase21/snapshots/M1.json`、`M4.json`）：

| case | calls | 最后 mutation 输出 | valid |
|---|---|---|---|
| M1 | read_workspace_file → edit_project_file | `已在 micro_fixture/calc.py 里替换 1 处匹配…` | ✅ |
| M4 | read_workspace_file → edit_project_file | `已在 micro_fixture/calc.py 里替换 1 处匹配…` | ✅ |

对比原 `phase20/snapshots/M1.json`：`edit_project_file` → `错误：没有找到要替换的内容` → ❌ 无效。

---

## 4. 修正后的 Exposure 因果结果（Ornith）

`phase21/exposure_valid_ornith`（E0–E2 N=10，E3 N=5，E4 N=5）：

| 变体 | tools | M1 semantic | M4 semantic | Combined | M1 first tools |
|---|---:|---:|---:|---:|---|
| E0 | 120 | 6/10 | 10/10 | 16/20 (80%) | run_tests 6 / list 4 |
| E1 | 14 | 8/10 | 8/10 | 16/20 (80%) | run_tests 7 / run_python 1 / list 2 |
| E2 | 11 | 2/10 | 8/10 | 10/20 (50%) | list 8 / run_tests 2 |
| E3 | 4 | 5/5 | 5/5 | 10/10 (100%) | run_tests 5 |
| E4 | 2 | 5/5 | 5/5 | 10/10 (100%) | run_tests 5 |

Lift：M1 `E1-E0=+0.2, E2-E1=-0.6, E3-E2=+0.8`；M4 `E1-E0=-0.2, E2-E1=0.0, E3-E2=+0.2`。

## 5. 跨模型对照（Qwen，同有效 checkpoint）

`phase21/exposure_valid_qwen`（N=5）：

| 变体 | Qwen M1 | Qwen M4 | 说明 |
|---|---:|---:|---|
| E0 | 3/5 | 4/5 | 全量 120 |
| E1 | 3/5 | 4/5 | router 14 |
| E2 | 3/5 | 4/5* | *1 TIMEOUT |
| E3 | 4/5 | 5/5 | verification + prep |

→ 两模型在有效 checkpoint 上**都不再出现 M1 0%**；E3 均接近/达到 100%。

---

## 6. 对 Phase 21 主报告的修正

| Phase 21 主报告 | 修正为 |
|---|---|
| “M1 恒 0%，是模型/上下文先验（Case D）” | **M1 0% 是无效 checkpoint（mutation 失败）造成的 benchmark artifact** |
| “Primary bottleneck = M1 模型选择先验” | **Primary bottleneck = checkpoint validity（mutation 成功门槛）** |
| “Verification-focused exposure = NEUTRAL” | 维持：E2 跨模型不稳定，拒绝 |
| “Post-router exposure pollution = CONFIRMED” | 维持：async 分支绕过 router 属实；但为**次要**因素 |
| “Router is final authority = NO” | 维持 |
| “E0 semantic 25%” | 有效 checkpoint 下 E0 combined = **70–80%** |

---

## 7. 结论与下一步

1. **最高优先级**：checkpoint 有效性 —— 任何 “post-mutation decision point” 必须由**成功 mutation**定义。
   harness 已修复；建议生产/benchmark 都显式区分 `mutation_attempt` 与 `mutation_success`。
2. **次要**：async 暴露边界（`main._run_attempt` 用 `active_agent`）+ Router plugin/MCP domain approval。
3. **可选**：`verification_due` 下的 verification-focused exposure（E3/E4 类）能把 semantic 拉到 100%，
   但属 choice constraint，需 Full E2E 证明不破坏其它任务。
4. **run_tests**：在有效 checkpoint 上 verification selection 已达 70–100%；建议进入 **M1/M4 Full E2E N=3**
   （在修复后的 checkpoint 语义与 exposure 边界下）后再决定 EXPERIMENTAL → PRODUCTION。
5. **L1**：维持 REJECTED FOR PRODUCTION。

---

## 最终输出（Addendum）

```text
E0 final tool count =
120

E1 final tool count =
14

E2 final tool count =
11

E0 semantic verification =
Ornith 16/20（80%）；Qwen 7/10（70%）  [有效 checkpoint]

E1 semantic verification =
Ornith 16/20（80%）；Qwen 7/10（70%）

E2 semantic verification =
Ornith 10/20（50%）；Qwen 7/10（70%，含1 TIMEOUT）  [跨模型不稳定]

E3 semantic verification =
Ornith 10/10（100%）；Qwen 9/10（90%）；E4 Ornith 10/10（100%）

Router is final exposure authority =
NO（async 路径 120 vs router 14）

Post-router exposure pollution =
CONFIRMED（main._run_attempt async 忽略 active_agent + mcp_bridge 无条件 append 79 MCP）

Verification-focused exposure =
NOT PROVEN / UNSTABLE（E2 跨模型不稳定；E3/E4 上限 100%）

Structured Action Commitment =
REJECTED FOR PRODUCTION

Full E2E needed =
YES（有效 checkpoint 下 E3/E4 ≥90%；E1 80%；先跑 M1/M4 E2E N=3）

run_tests =
EXPERIMENTAL（接近可进入 E2E 资格评估）

Primary remaining bottleneck =
Checkpoint validity：Phase 20 M1 LIVE snapshot 抓取在 mutation 失败后仍标记 verification_due，
污染了 M1 结论（已由 harness 的 mutation-success 门槛修复）

Production fix =
1) 明确区分 mutation_attempt / mutation_success，post-mutation checkpoint 必须基于成功 mutation；
2) main._run_attempt async 分支改用 router 选择的 active_agent；
3) 补 Router 的 plugin/MCP domain approval

Next component to modify =
runtime/benchmark 的 mutation-success gating（checkpoint 语义）+ main._run_attempt 暴露边界
```

---

## 附：新增 artifacts

| 文件 | 说明 |
|---|---|
| `phase21/snapshots/M1.json`、`M4.json` | **有效** LIVE snapshot（成功 mutation） |
| `phase21/exposure_valid_ornith/` | Ornith 有效 checkpoint E0–E3 |
| `phase21/ceiling_valid_ornith/` | Ornith E4 ceiling |
| `phase21/exposure_valid_qwen/` | Qwen 有效 checkpoint E0–E3（跨模型对照） |
| `benchmark/decision_qualification.py` | 新增 `_mutation_result_ok` 抓取门槛 |
| `benchmark/exposure_phase21.py` | 新增 `--run-e0`、`--snapdir` |

> 当前 `localhost:8080` 运行模型与 `.env` 以最终切换状态为准（Ornith-1.5-9B 为本轮替代模型）。
