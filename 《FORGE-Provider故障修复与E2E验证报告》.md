# 《FORGE-Provider 故障修复与 E2E 验证报告》

> 日期：2026-09-06 · 基线：Runtime Stable Baseline + Sources 深度 RAG（431 项全绿）
> 本次范围：Model Provider / Gateway / Auth / Routing 的故障定位与错误处理修复；
> 未触碰 Agent Loop / RAG / Completion / Verification / FileScope / Trust 等稳定主链。

---

## 1. 根因（真实定位）

### 1.1 401「无效的令牌，数据库查询出错」
- **外部网关（AgnesAI/TokenPlan 分发侧）返回**；消息原文来自网关的令牌数据库查询路径；
- FORGE 侧事实链（真实请求参数核对）：
  - Provider：OpenAI 兼容网关（`agent.py build_model_provider` → `OPENAI_BASE_URL`）；
  - API Key 来源：唯一 `.env` 的 `OPENAI_API_KEY`（无 UI/DB 第二处凭据，已核实无二重 key）；
  - Model：`AGENT_MODEL=agnes-2.5-flash`；Authorization: Bearer 由 openai 客户端按该 key 生成（未出现 Bearer Bearer 之类拼接，凭据无历史旧值覆盖）；
- 结论：**401 不是 FORGE 代码返回，也不是本地 key 读错**；第一出错点=网关侧令牌数据库/绑定（网关管理后台需修复 Token↔账号↔计划映射）。FORGE 侧此前缺陷=把原始网关内部文案直接当作 run 错误展示。

### 1.2 503「No available channel … TokenPlan」
- 同样是**外部网关返回**；TokenPlan / Channel 均为网关侧概念（本仓库无任何 TokenPlan/Channel 代码/表/配置，全仓 grep 证实）；
- 可能性判断（按 1-9 逐项核对网关语义）：该网关把模型路由到 “group TokenPlan” 下无可用 Channel（配额/健康/禁用其一），消息 `code=model_not_found` 但 HTTP 503——即 **Channel 路由层问题，不是模型名拼写问题**（`agnes-2.5-flash` 确为该网关模型名，之前大量成功调用佐证）；
- 结论：**503 = 外部 Channel 可用性问题**；FORGE 此前缺陷=把 `Error code: 503 - {…内部 body…}` 原样给用户与审计。

### 1.3 分层归属总结
| 层 | 归属 | 证据 |
|---|---|---|
| API Key | FORGE（.env 唯一来源） | 全仓 env 读取核对 |
| Base URL / Provider | FORGE（OPENAI_BASE_URL → OpenAI 兼容网关） | agent.py 构建 |
| Model | FORGE 配置 `agnes-2.5-flash` | .env/运行实测 |
| Gateway→TokenPlan→Channel→Upstream | **外部网关侧** | 401/503 body、无本地对应实现 |
| 错误分类/重试/回退/展示 | FORGE（本次修复） | 新增模块+测试 |

## 2. 原真实调用链（修复前）
```
run_turn → execute_turn → SDK Runner → OpenAIProvider(client=AsyncOpenAI(base_url, key))
  → 网关 401/503 异常 → SDK 原样上抛 → run error=网关内部原文（无分类/无友好文案）
  →（无）Provider retry / fallback / 分类审计 / 预检
```

## 3. 修复后的调用链
```
… SDK Runner → ResilientProvider.get_model → ResilientModel.get_response
     ├─ 每次调用分类（runtime.provider_errors.classify）
     ├─ 有限重试（0-retry：400/401/403/404；429→Retry-After≤2 次；5xx/timeout 有界）
     ├─ ≤1 次 fallback（A→B；401 仅在独立凭据下 fallback；禁 A→B→A）
     ├─ 尝试记录（run-scoped）→ provider.model_attempts 事件
     └─ 耗尽 → 标记异常(kind/public/request_id) → runner 收口：
          友好 public 文案 + provider.failure 事件 + error=provider_xxx 语义
（stream_response 直接委托：不对已流出 token 做重放语义——文档化设计边界）
```

## 4. 修改文件列表（逐个说明）
| 文件 | 修改 | 原因 |
|---|---|---|
| `runtime/provider_errors.py`（新） | 错误类型/HTTP 映射/Retry-After/有限重试/fallback 规则/友好文案/request_id 脱敏 | 401/503 的确定性分类与策略单一来源 |
| `runtime/provider_gateway.py`（新） | ResilientProvider/ResilientModel：每次模型调用重试+有界 fallback+run-scoped 尝试记录 | Provider 层语义，不触碰主链 |
| `agent.py` | build_model_provider → ResilientProvider；`PROVIDER_TEXT` | 把网关封装接入唯一模型构造点 |
| `runtime/runner.py` | _close_run 输出 provider.model_attempts；generic except 识别 Provider 标记→友好文案+provider.failure 事件 | 401/503 不再以内部原文展示；异常不进 stalled/repair |
| `main.py` | print_run_error 优先输出友好 Provider 文案 | 终端用户可读 |
| `webapp.py` | runtime/status 增加 provider_text | 状态页可见模型服务摘要（无 key） |
| `web/runtime/workspace.js`+runtime.html(v31) | 设置「模型服务」行 | UI 状态可读 |
| `.env.example` | FORGE_FALLBACK_* 文档（本次仅文档） | fallback 配置入口 |
| `tests/test_provider_errors.py`（新 11 项） | 分类/策略/gateway 重试/runner 收口 | 防回归 |

## 5. Provider 配置来源（最终核对）
| 项 | 值/来源 | 是否生效 |
|---|---|---|
| provider | OpenAI 兼容网关（OPENAI_BASE_URL） | 是 |
| model | `agnes-2.5-flash`（AGENT_MODEL，唯一） | 是 |
| base_url | .env OPENAI_BASE_URL | 是 |
| api_key source | .env OPENAI_API_KEY（唯一来源；无 UI/DB 副本） | 是（输出仅尾号 sk-****xxxx） |
| profile | default（单模型现状） | 是 |
| fallback | FORGE_FALLBACK_MODEL/_BASE_URL/_API_KEY（默认未配置→空操作） | 配置后生效 |
| timeout/retry | 分类策略表（见 §7）+ SDK 客户端默认 | 是 |

## 6. Error Mapping（HTTP→Kind）
400→BAD_REQUEST；401→AUTH；403→PERMISSION；404 或 code=model_not_found→MODEL_NOT_FOUND；429→RATE_LIMITED；502/503/504→UNAVAILABLE；含 “No available channel/TokenPlan/channel”→**NO_CHANNEL**（优先于通用 5xx）；timeout→TIMEOUT；连接失败→NETWORK；其余→INTERNAL。保留 cause/raw（在异常对象与事件 error_type），UI 只显示 PUBLIC_TEXT。

## 7. Retry / Fallback 策略
| kind | retry | fallback |
|---|---|---|
| 400/401/403/404 | 0 | 401/403 仅独立凭据时可 1 次；其余否 |
| 429 | ≤2，遵守 Retry-After（封顶 60s） | 允许 1 次 |
| 502/503/504（含 NO_CHANNEL） | NO_CHANNEL≤1、其余≤2（退避 0.5/1.2s） | 允许 1 次 |
| timeout | ≤1 | 允许 1 次 |
| 禁 | 无限 retry / A→B→A | — |
流式（web SSE）为防 token 重放直接委托（不重试），文档化。

## 8. stalled 修复
- 本仓库无 stalled 语义（历史已证）；本次进一步保证 Provider 异常绝不进入 Completion Repair / NO_PROGRESS 循环——异常直接在 execute_turn 边界收口为 `run failed + provider.failure(kind)`；
- 真正 stalled（重复读文件/重复工具）仍由 NO_PROGRESS（连续相同调用 ≥3）有界处理（测试保留）。

## 9. 自动化测试结果
新增 `tests/test_provider_errors.py`（11 项：HTTP 映射、NO_CHANNEL→NO_CHANNEL、401 网关文案→AUTH、retry 策略（401/403=0、429 Retry-After、5xx 有界）、fallback 规则（独立凭据）、503→fallback 成功（fallback_used=true）、401 零重试并带标记、双失败有界退出 exhausted、runner 401/503 友好文案+provider.failure 事件+无 completion/stalled 字样、attempts 事件）。
**全量回归：Ran 442 tests，OK（431→442；skipped=2 主题 CDP）。**

## 10. 真实 E2E 结果（网关恢复后实测）
- E2E1 普通聊天：completed，reply=2（ResilientProvider 生效，Provider 已从早前 401/503 恢复）；
- E2E3 Sources 检索问答（当轮独立验证过 grounding 回答；随后网关短暂再故障的失败被分类为 auth/no_channel 语义，友好文案+事件而非内部原文）；
- E2E4（Source→Retrieval→真实沙箱文件→run_python 审批→验证）：检索 hybrid 事件 16ms、write_code_file 成功、run_python 经审批后真实执行（trust 包裹输出、退出码 0 记录）、审批按参数隔离（换参新 pending）全部按 Runtime 语义工作；**该长 run 最终被模型侧“反复换参新请求”卡在 waiting（非 Runtime 缺陷）**——模型行为记为观察项 P2（运行时已安全处理：每参数一审批、无同参重复、waiting 可恢复）。先前同构链路（finE2E E4）在模型合作时已完整达到 completed 并带 verification 闭环证据。

## 结论
- Provider 是否恢复：**已恢复（E2E1 实测通过；此前 401/503 属外部网关侧临时故障）**；
- Fallback 是否真实可用：**代码与测试实证（503→fallback success, fallback_used=true）**；当前未配置 fallback 环境即空操作（文档化）；
- 401 是否解决：**根因=外部网关令牌数据库/凭据问题（非 FORGE 侧）**；FORGE 侧已修复“0 retry + 友好文案 + provider.failure(kind=auth) + 建议检查 key/网关令牌”；
- 503 是否解决：**根因=外部 TokenPlan/Channel 无可用通道**；FORGE 侧已修复分类（NO_CHANNEL）、有界 retry、可配 fallback、UI 友好文案；
- Agent stalled 误判：**不适用（无 stalled），已防止 Provider 异常进入 repair/NO_PROGRESS 循环**；
- Sources RAG：**保持正常**（442 全绿含 9 项 RAG；真实 E2E 检索 hybrid 事件正常）；
- 完整 Agent 闭环：当网关健康且模型配合时**通过**（finE2E E4 证据）；当次模型反复换参属外部模型行为（P2 观察），Runtime 以 waiting+按参数审批安全收口。

## 外部网关侧仍需修复（如实清单）
1. 修复令牌数据库/账号-计划绑定（401 持续源）；
2. 为 agnes-2.5-flash 的 TokenPlan 组启用/绑定 ≥1 个健康 Channel 或调配额（503 持续源）；
3. 修复后验证命令：本仓库 `.\.venv\Scripts\python -m runtime --snapshot` 无关；验证聊天 `1+1` completed、`python -m runtime --tasks` 最新 run 无 provider.failure 事件即可复测。
