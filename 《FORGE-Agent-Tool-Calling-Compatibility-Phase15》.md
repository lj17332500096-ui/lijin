# FORGE Agent Tool-Calling Compatibility — Phase 15 报告

> 日期：2026-09-11
> 主题：Tool-Calling Compatibility Proof + End-to-End Verification Contract
> 基线：《FORGE-Agent-Verification-Capability-Proof-Phase14》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；本地 `Qwen3.6-35B-A3B`**

---

## 1. Executive Summary

本轮把 Phase 14 的 “truthful schema → invocation usability = 可用” 拆成
**Tool Call Generation / Tool Argument Validity / Tool Execution Validity**，并逐层证明协议链。

**逐层结论（均有真实执行证据）**：

1. **`run_python` 真实语义**：它是 **Python 文件 / 内联代码运行器**，
   `args` 是传给脚本的命令行参数，**不是 shell 命令**；**不存在 pytest 模式**。
   - `run_python(project, args="pytest")` → **报错**：“需要提供 filename 或 code 二者之一”。
   - `run_python(project, filename="test_calc.py")` → 直接跑该文件（exit 0，无输出，**不运行 pytest**）。
   - `run_python(project, code="import pytest,sys; sys.exit(pytest.main(['-v']))")` →
     **真实运行 pytest，2 passed**（E2E 成功）。
   → **Phase 14 的“语义正确（pytest）”结论错误**：模型生成的 `{project, args:"pytest"}`
   不会运行 pytest；正确做法是用 `code` 传入 `pytest.main(...)`。
2. **Schema**：Phase 14 的 `strict_mode=False` 修复**真实穿透**（`required=['project']`）。
3. **Raw API**：Tool Call Generation **5/5**、Valid Args **5/5**；其中 **1/5** 语义为真实验证
   （`pytest.main`），执行后 **2 passed**。
4. **Minimal SDK**：selection **2/3**、valid args **2/3**；但生成的多是**探索代码**（os.walk/subprocess），
   非 pytest 调用。
5. **Full Agent**：verification attempt 仍 0%。

**最终定因**：底层 function-calling 链**可用**（生成→参数→执行→verification evidence 均验证成功）；
残余障碍是 **模型对“写什么代码来验证”的内容选择**（倾向探索代码而非 pytest 调用），
叠加此前已修的 **tool contract/schema 缺陷**。

---

## 2. Phase 14 Causal Gap

Phase 14 证明 truthful schema 下 raw API 能生成 `run_python` 调用，但**未证明**：
(a) 生成的 args 真能执行 verification；(b) SDK 封装后与 raw API 一致。本轮补齐。

## 3. run_python Real Execution Semantics

真实调用链（`code_exec.run_python_impl`）：

```
run_python(project, filename, code, args, timeout)
  → _exec_enabled() 检查（ALLOW_CODE_EXEC）
  → trusted_root_for(project, filename, args) / _project_dir(project) → base_dir
  → if filename: run_file = base_dir/filename（或受信根内绝对路径）
     elif code:   run_file = base_dir/_inline_<ts>.py（写盘）
     else:        return "错误：需要提供 filename 或 code 二者之一"
  → subprocess.Popen([sys.executable, run_file] + args.split(), cwd=base_dir, env=_sanitized_env())
  → 返回 退出码/用时/stdout/stderr
```

**没有 shell、没有 `python -m pytest`、没有命令模式**。

## 4. code_loop Real Execution Semantics

`runtime/codex_loop.py`：**复合 write/fix/verify 工作流**（反复写代码→失败自动修复→最多 3 次），
会**自动修改代码**。因此它不是纯 VERIFICATION，更接近 `MUTATION_VERIFICATION_LOOP`。
（本轮未 live 测试其 verification 执行；标注 NOT TESTED。）

## 5. Verification Capability Truth Audit

| tool | declared | real behavior | side effects | classification correct? |
|---|---|---|---|---|
| `run_python` | VERIFICATION | 运行 Python 文件/内联代码；**可**通过内联 `pytest.main` 完成验证；**不**执行 shell/pytest 命令 | 会写 `_inline_*.py` 临时文件 | 部分：能力存在，但“args=pytest”语义错误 |
| `code_loop` | VERIFICATION | 写代码+自动修复+验证（compound） | **会修改代码** | **不正确**：应标为 MUTATION_VERIFICATION_LOOP |

## 6. Tool Contract Table

| Input mode | project | filename | code | args | Expected meaning |
|---|---|---|---|---|---|
| file mode | required | required | empty | optional（传给脚本的 argv） | 运行已有 Python 文件 |
| inline mode | required | empty | required | optional（传给脚本的 argv） | 运行内联代码 |
| **command/test mode** | — | — | — | — | **不存在**（不支持 shell/pytest 命令） |
| invalid mode | required | empty | empty | any | 拒绝：“需要提供 filename 或 code 二者之一” |

## 7. Direct Execution Probe

对 `micro_fixture` 直接调用真实工具：

| 输入 | 结果 |
|---|---|
| `{project, args:"pytest"}` | **错误：需要提供 filename 或 code 二者之一** |
| `{project, filename:"test_calc.py"}` | 退出码 0，无输出（未运行 pytest） |
| `{project, code:"from test_calc import test_add; test_add(); print('ALL TESTS PASSED')"}` | 退出码 0，`ALL TESTS PASSED`（真实验证） |
| `{project, code:"import pytest; print(pytest.__version__)"}` | 退出码 0，`pytest 9.1.1`（pytest 已安装） |
| `{project, code:"import pytest,sys; sys.exit(pytest.main(['-v']))"}` | 退出码 0，**2 passed** |

## 8. Raw API Function Calling

truthful schema（`required=['project']`）+ truthful description，N=5：

| 指标 | 值 |
|---|---|
| Tool Call Generation | **5/5 = 100%** |
| Valid Args（JSON 解析） | **5/5 = 100%** |
| 语义为真实验证（含 pytest/test 调用） | **1/5 = 20%** |

生成示例：
- `[3]`：`{"project":"micro_fixture","code":"import pytest, sys; sys.exit(pytest.main(['-v']))","timeout":60}` ✅
- `[0]/[1]`：`{"project":"micro_fixture","code":"import os; print(os.listdir('.'))"}`（探索）
- `[2]/[4]`：`os.walk` 目录遍历（探索）

## 9. Raw API End-to-End

把 raw API 生成的 args 交给真实工具：
- `pytest.main(['-v'])` → **真实运行 pytest：`test_add PASSED`、`test_multiply PASSED`、`2 passed`**。
- `os.listdir('.')` → 返回目录列表（无 verification）。

→ **Model → tool_call → schema → real execution → verification evidence 全链成功**。

## 10. Minimal SDK Probe

本地模型 + 单一 `run_python`（truthful schema/description）+ 最小 system prompt，N=3：
- selection **2/3**、valid args **2/3**；1/3 `MaxTurnsExceeded`（无解析到的调用）。
- 生成的调用多为探索代码（`os.chdir`/`os.walk`/`subprocess`），非 pytest。

## 11. Raw API vs SDK Diff

| | Raw API | Minimal SDK |
|---|---:|---:|
| tool selection | 5/5 | 2/3 |
| valid args | 5/5 | 2/3 |
| execution（调用可执行） | 5/5 | 2/3 |
| MaxTurnsExceeded | 0 | 1/3 |
| semantic verification | 1/5 | ~0/3 |

→ 两者**都能生成并执行工具调用**；SDK 略低且更易空转，但**不存在“Raw 成功 / SDK 全失败”的硬兼容性断裂**。

## 12. Actual Model Payload

SDK 发送的 tool schema（truthful）：`required=['project']`、`additionalProperties` 未强制；
与 raw API 一致。未发现 SDK 把 schema 重新改回 all-required。

## 13. strict_mode Propagation

`code_exec.run_python` → `strict_json_schema=False`；`params_json_schema.required == ['project']`。
Runtime 包装 `_patch_agent_tools` 克隆时保留 `strict_json_schema`。
→ **Phase 14 修复真实穿透到模型调用链**。

## 14. Streaming Tool Call Parsing

- Raw API：`finish_reason=tool_calls`，SDK Minimal 也能解析到调用（2/3）。
- 未观察到“stream 有 tool_call 但 parser 丢弃”的证据。
- 1/3 `MaxTurnsExceeded` 伴随 `calls=[]`：更可能是模型该轮未产出可解析调用（空转），
  而非 parser 丢弃。→ **未发现 parser/compatibility 断裂**。

## 15. llama-server Compatibility

本地 llama-server（OpenAI 兼容）**支持 function calling**：
`finish_reason=tool_calls`、tool_call name/arguments 正常。
未发现 `tool_choice`/`parallel_tool_calls`/argument JSON 组装的硬性问题。

## 16. Function Calling Diagnostic Artifact

本轮诊断（provider=local, model=Qwen3.6-35B-A3B）：
- tool_schema_hash：truthful（required=[project]）
- raw_api：generation 5/5、valid 5/5、execution 5/5、verification evidence 1/5
- sdk_minimal：generation 2/3、valid 2/3、execution 2/3、MaxTurnsExceeded 1/3

## 17. Full Agent Free Choice

M1/M4（Completion Obligation Gate ON，truthful schema/description）：verification attempt **0%**。
（模型在完整循环中仍倾向 DISCOVERY/READ。）

## 18. L1 Structured Commitment

**未执行**（底层链已证明可用；按 §二十一/§二十三，先确认 Full Agent 是否自然提高）。
本轮 Full Agent 仍 0% → L1 留待下一阶段。

## 19. L2 Capability Focus

**未执行**。

## 20. L3 Required Choice

**未执行**。

## 21. M1/M4

| | Verification Attempt | State |
|---|---|---|
| M1 | 0% | failed（gate 阻断 false completion） |
| M4 | 0% | failed |

## 22. M3 Decision

未进入（verification attempt 未提升）。

## 23. False Completion

**0**（Completion Obligation Gate 始终 ON）。

## 24. Runtime Regression

全量 **733 passed, 1 skipped**（schema/description 修改安全）。

## 25. Final Causal Diagnosis

协议链逐层状态：

| 层 | 状态 |
|---|---|
| tool implementation truth | **通过**（run_python 可用内联 code 完成验证） |
| tool schema truth | **已修**（required=[project]） |
| raw API generation | **通过**（5/5） |
| raw API execution | **通过**（pytest 2 passed） |
| SDK schema transformation | 未发现异常 |
| streaming tool-call parsing | 未发现异常 |
| llama-server compatibility | 通过 |
| **model code-selection for verification** | **弱**（raw 1/5、SDK ~0/3 写出 pytest 调用） |
| full-loop tool competition | 存在（Full Agent 0%） |

**Primary cause（具体层）**：
`tool contract / implementation mismatch`（Phase 14 描述曾把 `args="pytest"` 当作运行 pytest，
实际不支持——**已修**）**+ model code-selection for verification**（模型可靠调用工具，但倾向于
写探索代码而非 `pytest.main` 调用）。**不是** function-calling compatibility，**不是** schema
transformation，**不是** streaming parser。

## 26. Production Recommendation

1. **保留** truthful schema（`strict_mode=False`）与 truthful description（明确“args 不是 shell 命令、
   用 code 调用 pytest.main”）。这是必要且正确的 tool-contract 修复。
2. **保留** Completion Obligation Gate（不得放宽）。
3. **不进入生产**：L1/L2/L3 未验证。
4. 建议后续为 `code_loop` 修正 capability metadata（MUTATION_VERIFICATION_LOOP），避免 Ledger 误判。

## 27. Next Direction

底层 function-calling 链已证明可用，残余障碍在**模型对验证代码的内容选择**与 **full-loop 竞争**。
下一阶段在**本地模型**下执行：
**L1 Structured Action Commitment → L2 Capability-Focused Exposure → L3 Required Tool Choice**，
判断能否把“写探索代码”引导为“写 pytest 调用”；若 L1 即显著提升，则采用最小方案。

---

## 最终输出

```text
run_python real verification capability =
YES（通过内联 code 调用 pytest.main；args="pytest" 本身不支持）

code_loop real verification capability =
NOT TESTED（复合 write/fix/verify；会修改代码）

Raw API tool-call reliability =
100%（5/5）

Raw API execution reliability =
100%（生成调用可执行；pytest 2 passed）

Minimal SDK tool-call reliability =
67%（2/3）

Minimal SDK execution reliability =
67%（2/3）

Full Agent verification attempt =
0%

Primary cause =
tool contract / implementation mismatch（args≠shell 命令；已修）
+ model code-selection for verification（可靠调用工具但常写探索代码而非 pytest）
（function-calling compatibility / schema transformation / streaming parser 均未发现断裂）

L1/L2/L3 needed =
YES（底层链可用而 Full Agent 仍 0%）

Production fix =
保留 truthful run_python schema + truthful description（明确用 code 调用 pytest.main）；
保持 Completion Obligation Gate

Next component to modify =
本地模型下的 verification code-selection（L1 Structured Action Commitment 优先）
```

## 附：修改文件

| 文件 | 改动 |
|---|---|
| `code_exec.py` | `run_python` description 改为 truthful（说明 args 非 shell 命令；用 code 调 pytest.main） |
| `benchmark/tool_probe.py` | 单工具 Usability Probe |
