# FORGE Agent Behavior Benchmark — Phase 2 修复方案

> 日期：2026-09-10
> 依据：《FORGE-Agent-Behavior-Benchmark-Phase1-After-Fix》(通过率 44%→64%,Critical 17→~7)。
> 目标：**不追求 100% 分数**,针对 Phase 1 仍稳定复现的剩余根因做精准修复,修复后再用同一 50 项回归。
> 原则：仍优先在既有模块内增强,不新建第二套系统;每项给出「根因 → 最小修复 → 落点 → 验证」。

---

## 0. 剩余问题分组(Phase 2 范围)

| 组 | 代表测试 | 根因归属 |
|---|---|---|
| P2-A 审批通道未全覆盖 | T028/T044/T050/T018/T020 | 受信根判定与模型调用方式不匹配 |
| P2-B missing 拦截后空转不收敛 | T008/T010 | guard 拦了但模型不结束,反复重试到 Max turns |
| P2-C memory 收敛 | T015/T043/T045 | 幂等/意图细分已生效,但"保存收尾"仍不稳 |
| P2-D Coding 边界系统 | T036/T049 | 长任务 180s 未终态 |
| P2-E 模型输出/非法参数 | T021/T033 | 模型 400 / 非法输出(非代码) |

---

## 1. P2-A：审批豁免按"真实运行根"而非仅 project 名(关键)

### 根因
受信根判定 `project == root.name` 才豁免 run_python/code_loop。但模型常**直接用 `code`/`args` 传 fixture 绝对路径**(如 `cwd="...benchmark_fixture"`、`args=".../run_tests.py"`),**不设 `project`** → `_trusted_code_roots` 匹配不到 → 仍走 approval → waiting_approval。T028/T050 即此。

### 最小修复
**把豁免判定从"project 名"升级为"任一受信根出现在参数/工作目录中"。**

- `runtime/approval.py` `check()`:run_python/code_loop 时,若参数 `project` **或** `arguments`(含 code/cwd/args 的 JSON 字符串)里命中任一 `FORGE_TRUSTED_CODE_ROOTS` 绝对路径 → 放行。
- `code_exec.py` 同时让 `run_python_impl` 支持"受信根内路径":当 `filename` 是受信根内绝对路径或 `args` 含受信根时,允许(而不强制 `code_sandbox/<project>/`)。

### 落点
1. `runtime/approval.py`：`check()` 加 `_exec_in_trusted_root(tool_name, arguments)` —— 序列化 arguments 判 `str in json.dumps(arguments)`。
2. `code_exec.py`：`_file_target` 增加"受信根内"分支;`run_python_impl` 对受信根路径放行 cwd。

### 验证
- `T028`、`T044`、`T050`、`T018`、`T020` 重新跑:run_python 不再 approval → 能真实验证。
- 单元测试:构造 username 不设 project、但 `args` 含受信根的调用 → approval 豁免;真实 project 路径不含受信根 → 仍审批(production 不受影响)。

### 注意(安全边界)
豁免只在 `FORGE_TRUSTED_CODE_ROOTS` **显式配置**的目录内生效;生产 Project 未配置该 env → 行为不变,不绕过 FileScope/sandbox。

---

## 2. P2-B：missing/constraint 拦截后的"收敛引导"(防止空转到 Max turns)

### 根因
- T010(提醒缺时间):missing guard 拦 `schedule_add`(blocked×27)后,模型反复换措辞调 schedule 被拦 → 不结束 → Max turns。
- T008(删无用文件):missing guard 要求"给候选清单/确认标准",模型反复 list(×11)未问 → no-progress。

### 最小修复
**拦截同工具超过 N 次后,返回"强制结束本轮"而非继续允许重试。** 复用 Phase 1 的 `DiscoveryTracker` 语义收敛(对 schedule/list 类也生效),或在 missing-guard 路径加"该动作已多次被拒 → 强制结束"。

- `runtime/readiness_gate.py` `missing_required_fields`:对命中项返回的提示本就要求"请先向用户确认…不要自己假设"。但模型仍重试 → 需要一个**跨轮次计数**。
- 落点:在 `runctx` 加 `blocked_action_count: dict`;missing/constraint guard 每次拒绝加一,同工具同一 reason ≥ 2 次 → 返回 `ACTION_REPEATEDLY_BLOCKED`(明确要求结束本轮、用 questions / 回答现状,不再尝试)。
- 让 `agent.py` 防空转入设进一步强化(可选第二层)。

### 验证
- T010、T008 重跑:最多 3 次尝试后强制收敛,不再 Max turns。
- 单元测试:同一 blocked reason 连续 ≥2 次 → 返回结束引导。

---

## 3. P2-C：Memory 保存收尾(幂等扩展)

### 根因
- T015(保存笔记):幂等(同一 tool 一次)后模型仍 remember×6(不同内容/意图),未真正"存一次就答"。
- T043/T045:保存后未给出明确完成 → claim_unsupported / no-progress。

### 最小修复
- `runctx.persistence_already_done` 幂等已在。扩展到:**保存动作成功后,若模型后续再发起任何 save_note/remember/forget(同 Run)→ 返回 `ALREADY_SAVED_THIS_RUN`/`PER_PERSISTENCE_ONE`**,并把这作为"已完成的证明"传给 completion(允许 PASS)。
- `completion.py`：当 reply 含"已保存/已记住"且 `rctx.persistence_done` 为真 → 视为完成,不再 claim_unsupported。
- 或者更简:把"保存一次成功"作为 evidence 注入 ExecutionEvidence(`persistence_evidence`),completion 对 save 类声明放行。

### 落点
`runtime/completion.py` 的 evidence 扩展 + `runner.py` 记录 persistence_done 到 evidence。

### 验证
- T043/T045/T015 重跑:保存一次成功即 completed;不再 claim_unsupported。
- 单元:persistence_done 为真 + 保存声明 → PASS。

---

## 4. P2-D：Coding 长任务(180s 上限)

### 根因
T036(删除迁移文件)/T049(登录修复)在 180s 内未到终态,被 runner 判 running。真实 run 可能已跑(后台),但 benchmark 窗口不足。

### 最小修复(评测侧,非 Agent)
benchmark runner 把单任务轮询窗口 `180s → 300s`;对 `running` 终态允许额外等待或轮询 agent.db 的 run 最终状态。**不改 Agent 行为**,只延长评测窗口。
> 属 Benchmark 环境参数,非 Agent 缺陷;记录即可。

---

## 5. P2-E：模型输出/非法参数(记录,非代码)

- T021:模型服务 `400 参数错误`;T033:非法输出导致 claim_unsupported。属模型行为/gateway 稳定性,非本收敛系统可修。记录到"模型档位/网关"观察项,不强改。

---

## 6. 落地顺序与回归策略

| 步骤 | 内容 | 验证 |
|---|---|---|
| 1 | P2-A(approval 受信根按参数判定) | 单元测试 + T028/044/050/018/020 定向重跑 |
| 2 | P2-B(blocked 收敛) | T008/T010 定向重跑,确认不 Max turns |
| 3 | P2-C(persistence evidence) | T043/045/015 定向重跑 |
| 4 | 全量回归 | `pytest tests`(期望 590+ 全绿,不回归) |
| 5 | 完整 50 项重跑 | 出 Phase 2 After 报告 |

**禁止边测边改**:步骤 1-3 完成后冻结版本,再跑定向(step1-3 各自定向失败可回改),确认无回归后一次性完整 50 项。

---

## 7. 风险与边界

- P2-A 豁免仅限显式配置的受信根目录,生产无配置 → 不变。
- P2-B 的"强制收敛"可能让个别合法多步任务过早停止 → 阈值设 ≥2 次且仅针对"同一 blocked reason",不误伤正常探索。
- P2-C 的 persistence evidence 仅对"已真实保存"放行,不豁免"声称已保存但未执行"(仍拦)。
- P2-D 只调评测窗口,不影响 Agent。

---

## 8. 完成标准(Phase 2)

- run_python approval 覆盖全部 fixture 路径 → T018/020/028/044/050 Coding 能真实验证。
- missing/constraint 拦截后不再 Max turns → T008/T010 收敛(completed 或 明确 questions/limitation)。
- Memory 保存收尾 → T015/043/045 不再 claim_unsupported。
- 完整 50 项通过率进一步上升,且**无新增回归**。
- 说明:A 类(纯问答)与 C 类(工具选择)不应因本次修复被误伤(不新增过度澄清/过度限制)。
