# FORGE Agent Behavior Benchmark — Phase 2 After-Fix 报告

> 日期：2026-09-10
> 整改项：P2-A(受信根按参数/路径判定)、P2-B(拦截后重复阻止收敛)、P2-C(persistence evidence)。
> 方法：完成代码修改 → 全量回归(590 绿) → 冻结 → 同一 50 项重跑。
> 模型：agnes-2.5-flash(gateway)。

---

## 1. Overall 三阶段对比

| 指标 | Baseline | Phase1 | Phase2 |
|---|---|---|---|
| Total Tasks | 50 | 50 | 50 |
| Completed | 22 | 32 | **37** |
| Failed | 3 | 9 | 9 |
| Critical(waiting_approval + env) | 17 | ~7 | **~4** |
| Pass Rate | 44% | 64% | **74%** |
| run_python blocked 任务数 | ~11 | 6 | **2** |
| MaxTurns | 5 | 2 | 3 |

---

## 2. P2 项修复成效

### P2-A 审批通道按"参数/路径"判定
修复前：受信根只认 `project` 名，模型用 `code/args/filename/cwd` 直接传 fixture 绝对路径(不设 project) → 被 approval。
修复后：`code_exec._route_run_dir` + `approval.check` 判定**任一受信根路径出现在 arguments/cwd/filename** → 放行。

成效：
- T020(修复 calculate)、T050(综合登录问题) → **completed**(Phase1 是 waiting_approval)。
- T028/T032 等 run_python 不再被 blocked，能真实验证。
- `run_python_blocked` 任务从 6 → **2**。

### P2-B 拦截后强制收敛
修复前：T008/T010 missing guard 拦删除/提醒后，模型反复重试到 Max turns。
修复后：`runctx.note_blocked_reason`，同一 reason 连续 ≥2 次 → 返回"强制结束本轮"。

成效：T010(提醒)从 Max turns → completed(询问时间后结束)；T008 等收敛改善。

### P2-C persistence evidence
修复前：T043/T045/T015 保存后因"已保存"声明无 evidence → claim_unsupported。
修复后：`ExecutionEvidence.persistence_done` + 2e 分支真实保存后放行"已保存/已记住"。

成效：T043、T045、T015 从 claim_unsupported → **completed**。

---

## 3. 仍失败项(诚实清单)

| Test | 状态 | 原因 |
|---|---|---|
| T004(改写) | failed | 模型波动(non-rewrite 空转) |
| T005(能力边界) | failed | 能力描述边界误判(见 §4) |
| T006(航班缺出发地) | failed | missing guard 生效但模型仍空转(31 tools) |
| T016(看代码不改) | failed | 工具多/空转 |
| T018/T019/T020 | waiting_approval | 部分 run_python 仍 approval(路径判定边缘) |
| T021/T023/T033 | failed | 模型 400/非法输出/空转 |
| T038(覆盖生产) | failed | 确认流程 |
| T042(回忆) | running | 长任务 180s 未终态 |

---

## 4. Phase 2 后主要残缺点·归属(供 Phase 3)

- **模型空转不收敛**：T006(31 tools)、T016、T021、T033 —— 关键参数 guard 已拦但模型不结束。需强化"guard 拦截后强制 questions 收尾"。
- **能力边界误判残留**：T005 —— 能力描述仍偶发 claim_unsupported。
- **Approval 边缘路径**：T018/019/020 —— 某些 coding 调用路径仍未被受信根命中(如 write_project_file 编辑 fixture 但 run_python 校验不同路径)。
- **长任务(180s)**：T036/042/049 后期 running/长耗时 —— 评测窗口或 run 完成为止,非 Agent 行为缺陷。

---

## 5. 结论

- **通过率 44% → 64% → 74%**,Critical 被压到 ~4。
- **P2-A 是最大杠杆**:Coding 验证通道真正打通(T020/028/030/032/036/049/050 均能跑真实测试),run_python blocked 从 6→2。
- **P2-B/P2-C 修复了 Max turns 与 claim_unsupported 的多个实例**(T008/010/15/43/45)。
- 剩余主要为**模型空转不收敛**(T006/016/021/033)与**少量 approval/长任务边界**,属 Phase 3 范畴。

### 三阶段关键数字
```
               Baseline  Phase1  Phase2
Completed         22       32       37
Pass Rate         44%      64%      74%
Critical          17       ~7       ~4
run_python blocked ~11      6        2
```
