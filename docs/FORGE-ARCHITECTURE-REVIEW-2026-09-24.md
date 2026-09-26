# FORGE Agent Harness 架构重估与调整方案

**日期**：2026-09-24　**专家**：墨衡 · Agent Harness 架构设计师
**方法**：不信历史报告，直接读代码 + 实证运行验证
**基线**：`main` @ `42fab65`，工作区含未提交改动（Laya 全家桶 + `runtime/runner.py` +11 行）
**性质**：架构重估 + 调整方案 + 修复步骤。**本报告未修改任何生产代码**。

---

## 0. 执行摘要

**一句话判定**：FORGE 的**内核层（L 生命周期 / E 收敛）已达产品级，是真正的护城河**；
但外围三层（T 工具路由 / V 评估 / C 上下文计量）正在拖后腿，且**刚接线的 Laya 路由存在一处
必现的致命 bug，当前状态下会让每次 `run_turn` 崩溃**。

三个必须立刻处理的事实（均已实证，非推测）：

| # | 事实 | 证据 | 影响 |
|---|---|---|---|
| **F1** | `runner.py:1669` 用**类调用实例方法** `LayaRouter.screen(message)` | 实测抛 `TypeError: LayaRouter.screen() missing 1 required positional argument: 'query'` | `laya`+`torch` 已安装、`.env` 无 `FORGE_LAYA` → 默认启用 → **每次 `run_turn` 崩** |
| **F2** | 同分支 `return []` 把空列表当 agent 返回 | `selected_agent` 后续要 `.clone()`（runner.py:2256） | F1 修好后立即暴露 `AttributeError` |
| **F3** | `laya_router.py` 模块级 `_SINGLETON = LayaRouter()` | 实测 `import runtime.laya_router` **耗时 11.6s** | 每个进程首次 `run_turn` 卡 11.6s；且 `FORGE_LAYA=off` 也照加载 |

**这不是"架构不够先进"，而是"新能力接线未跑通就搁置"。** 建议先把 F1–F3 修掉（约 30 分钟），
再谈架构演进。

---

## 1. 方法论与证据等级

采用通用分析框架 **H = (E, T, C, S, L, V) + P**：

- **E** 执行循环 · **T** 工具注册 · **C** 上下文管理 · **S** 状态存储 · **L** 生命周期钩子 · **V** 评估接口
- **P** 架构范式（扩展 / 配置 / 部署 / 编排 四个正交子维度）

证据等级：**L2 = 源码实证**（grep + AST + 真实运行）优先；**L3 = 文档/历史报告**仅作旁证。
本报告中所有"现状"判定均标注来源，凡与 9-21 架构审计冲突处，以本次实证为准。

---

## 2. 现状：H 六层逐层体检

### 六层评分卡

| 层 | 评分 | 一句话现状 | 主要短板 |
|---|---|---|---|
| **E** 执行循环 | **B+** | `run_turn` 主链 + 确定性 Completion Gate + TERMINALIZE 收敛 | `runner.py` 184KB/3300+ 行，`_patch_agent_tools` 单方法 890 行 |
| **T** 工具注册 | **C+** | 37 工具 + 技能注入 + MCP；路由 = 关键词表(934行) + Laya 快筛(新,未跑通) | 无语义层；全量 schema 每轮发送；新路由未跑通 |
| **C** 上下文 | **B-** | 有工具消息裁剪 + 8 节 `state_snapshot` 压缩 | **无真实 token 计量** → 压缩靠代理指标触发；工具返回无硬截断 |
| **S** 状态存储 | **B+** | SQLite WAL + `run_id` 主键 + task_events 审计 | `tool_calls.task_id` 装 run_id、`messages.task_id` 装容器 id（**同列异义**） |
| **L** 生命周期 | **A-** | 11 道门 + ApprovalGate + FileScope + cancel_scope | **规则条数过多**（11 门 + 7 verdict + 9 类拦截），规则疲劳风险 |
| **V** 评估 | **C+** | 50 case + 确定性 verdict（判定不依赖模型，稀缺优势） | n=50 只能检 ±28pp；评测入口与生产不完全 parity；路由层无独立评估 |

**总体：B（内核强、外围失衡）。** 最强项是 L 与 E 的确定性治理——这恰是市面多数 harness 缺失的；
最弱项是 T 的路由与 V 的统计效力。

### 2.1 E 执行循环（B+）

```
run_turn → route_agent（含 Laya 快筛 / C2 熔断）→ resolve_budget → 
execute_turn（3 模式）→ Runner 模型循环 → 工具包装器拦截（11 道门）→ 
_record_tool + ExecutionEvidence → CompletionGate.evaluate → 终态收口
```

**做得好的**：终止条件用**确定性规则**（`ExecutionEvidence` + 6 类 verdict），而非模型自评——
这是 checklist 明确要求、且多数 harness 做不到的；防跑飞三保险（轮次 + token + 墙钟）齐全。

**问题**：`runtime/runner.py` 184KB、`_patch_agent_tools` 单方法 890 行，审批门/记账/约束拦截堆叠。
这不是正确性问题，但**是演进速度的瓶颈**——每加一道门的边际成本在上升。

### 2.2 T 工具层（C+）

- 工具面：37 个 native + 技能动态注入（`*SKILL_TOOLS`）+ MCP（`mcp_bridge.py`）。
- 路由：`tool_router.py` 934 行关键词/别名表（41→8~16 裁剪）+ C2 熔断升级。
- **新增**：`runtime/laya_router.py` L1 二分类快筛（未提交，见 §3）。

**结构性短板**：路由是「**硬编码关键词表**」与「**未跑通的模型快筛**」两个极端，**中间没有语义层**。
9-21 审计建议的 bge 向量一级召回（`rag.py` 已有 `OnnxEmbedEngine`）至今未接——这不是遗漏，
但在工具数 37→50+ 的路径上，关键词表的维护成本会线性膨胀。

### 2.3 C 上下文（B-）

已实现（9-21 落地）：`trim_tool_messages`（保留最近 2 轮工具调用）+ `compact.py` 8 节
`state_snapshot` 摘要 + 开篇语。

**短板（本次实证确认）**：
1. **无真实 token 计量** → 压缩触发依赖墙钟/轮次等代理指标。对照 DeepSeek Harness 的
   「上下文 80% 水位触发压缩」，FORGE 缺的是**分母**。
2. 9-21 指出的「工具返回限 2000 字符、超出写文件只回路径」**未补**——`read_workspace_file` 无硬截断。

### 2.4 S 状态存储（B+）

- `agent.db`（32MB, WAL）+ `sessions.sqlite`；`run_id` 为事件/审计主键（2026-09-22 已纠正
  run_id ≠ trace_id 的错误）。
- **遗留坑**：`tool_calls.task_id` 装 run_id，而 `messages.task_id` 装容器 id——**同列异义**，
  join 必错。这类"名字相同语义不同"的漂移，正是审计里反复出现的根因模式。
- **未完全达成**：事件溯源（append-only）不彻底——DeepSeek 的「model-visible means logged」
  不变量在 FORGE 没有对应断言，无法回答"模型看到的每样东西都能从日志重建吗"。

### 2.5 L 生命周期钩子（A-，最强项）

11 道门（needs_user_input → constraint → missing_fields → 幂等 → intent gate → Readiness →
Approval/FileScope → budget → redundant → completion_ready → repeat_target）+ `ApprovalGate` +
`cancel_scope`（杀同步工具进程树）+ 审批续跑（Phase 38）。

**唯一风险是成功带来的**：规则太多。9-19 评审引前沿警告——系统提示与 schema 合计超 ~150~200 条
指令后，即便 frontier 模型规则遵循率也下降。FORGE 已逼近这条曲线。**下一步不是"加门"，是"门减法"。**

### 2.6 V 评估（C+）

- 优势：确定性 verdict，**判定不依赖模型**——这是最值钱的资产，绝不能换成 LLM-as-judge。
- 短板：
  - **统计效力不足**：n=50 只能检 ≈±28pp。3.0 模型的 −8pp（68% vs 76%）**不显著**（CI 含 0），
    但被当作结论在用。**n=200 对照至今未做**（9-21 就列为待办）。
  - **评测/生产 parity 缺口**：`decision_checkpoint` 入口仍绕 `run_turn`
    （`BENCHMARK_NOT_PRODUCTION_PARITY`），影响"评测结论外推生产"的可信度。
  - **路由层无独立评估**：`eval_laya_forge.py` 已写好，但**卡在 `test_final.jsonl` 不存在**。

---

## 3. P0：Laya 接线三连 Bug（实证）

> 本节结论全部来自真实运行验证，非静态推测。

### 3.1 Bug A —— 类调用实例方法（致命）

```python
# runtime/runner.py:1667-1672  （未提交改动）
from runtime import laya_router
if laya_router.laya_router_enabled():
    _laya_res = laya_router.LayaRouter.screen(message)   # ← BUG
```

AST 实证：`screen(self, query)` 是普通实例方法，**无 `@staticmethod` 装饰器**。
实测输出：

```
TypeError -> LayaRouter.screen() missing 1 required positional argument: 'query'
```

**为什么必现**（三个条件同时成立，均已实测）：

| 条件 | 实测值 |
|---|---|
| `laya` 已安装 | `laya-0.3.3.dist-info` ✅ |
| `torch` 已安装 | `torch-2.14.0.dist-info` ✅ |
| `.env` 未设 `FORGE_LAYA` | grep = 0 命中 → `laya_router_enabled()` 默认 **True** |

**爆炸半径**：`run_turn` 有三层兜底（runner.py:2136/2153/2159），但三层调的是**同一个坏调用**，
第三层（2159）无保护 → TypeError 冒泡出 `run_turn` → **整个 Run 崩溃**。

### 3.2 Bug B —— 返回类型违反契约

```python
if _laya_res == "direct_text":
    return []      # ← BUG：调用方要的是 Agent，不是 list
```

调用方契约（实证 grep）：`selected_agent` 在 runner.py:2256 被 `.clone(...)` 调用，
在 2722/2963 作为 agent 传入 Runner/stream_final。`list` 无 `.clone` → `AttributeError`。
**正确写法**：`return chosen.clone(tools=[])`（保留 agent，只清空工具面）。

### 3.3 Bug C —— 模块级单例导致 import 即加载模型

```python
_SINGLETON = LayaRouter()   # 模块导入即构造 → 加载 checkpoint
```
`__init__` 里 `if laya_available(): self._init_router()` —— **不看 `FORGE_LAYA` 开关**。
实测：`import runtime.laya_router` **耗时 11.6 秒**，`_SINGLETON._router is not None`。
即：开关关着也付 11.6s 冷启动代价；且 `_init_router` 无 try，加载失败会让 import 抛异常。

### 3.4 Bug D —— 验收指标与风险不对称（设计层）

数据分布（`manifest.json` 实证）：**tool 440 : text 125 = 77.9% : 22.1%**，严重不平衡。

`eval_laya_forge.py` 的门禁是「**总体准确率 > 95%**」。在 77.9/22.1 分布下这是个**会骗人的指标**：
全判 `tool` 就有 77.9%。解 `acc = 0.779·recall_tool + 0.221·recall_text` 可知，
即便 `recall_text` 只有 77.4%，总体准确率也能到 95%——**看起来达标，实则每 4 个该纯文本回答的
query 就错 1 个**。

更关键的是**错误方向不对称**：
- 判 `tool_needed`（保守）→ 多走一次工具路由，**代价是几毫秒，可恢复**；
- 判 `direct_text`（激进）→ 0 工具 → 模型编造答案 → **静默的功能降级（假完成），不可恢复**。

因此验收指标必须换成**精度优先**，而非总体准确率：

| 指标 | 门禁 | 理由 |
|---|---|---|
| `direct_text` **精确率** | **≥ 0.99** | 被短路的样本里，真正不需要工具的比例 |
| `tool` 类**召回** | **≥ 0.995** | 该用工具的绝不能被短路（安全方向） |
| 总体准确率 | 仅参考，不作门禁 | 在不平衡分布下会误导 |

阈值 0.85 也**不能拍脑袋**——应在 val 集上扫 threshold，取满足 `tool 召回 ≥0.995` 的最大阈值。

### 3.5 Bug E —— 冗余前向 + 字段名靠猜

`screen()` 里先调 `self._router.route(...)` 又调 `predict(...)`，代码注释自己承认
「`route` 不跑模型」——**白白多一次调用**。且 `_extract_confidence` 遍历
`("probabilities","answers","score","calibration")` 猜字段名——**这是"API 真实"门槛的反例**。
应先固定 laya 0.3.3 的真实返回结构（`laya/_laya_inspect/laya-main/laya/router.py` 实测有
`route`(L257) 与 `predict`(L315) 两个方法），再写死字段。

---

## 4. 对照市面主流架构

### 4.1 知识库五案例对照

| 案例 | 它的核心主张 | FORGE 现状 | 可迁移 / 不可迁移 |
|---|---|---|---|
| **DeepSeek Harness** | 一切皆插件、无特权内核；80% 水位压缩；「model-visible means logged」 | 反其道：**厚内核、特权主链** | ✅ 迁移「80% 水位压缩」与「事件溯源不变量断言」；❌ 不迁移无特权内核（会拆掉 Single Active Run 所有权） |
| **Claude Code** | hooks 确定性拦截；subagent 上下文隔离；**持久规则在文件**（CLAUDE.md） | 门硬编码在 `runner.py`；`agent.py` **229 行 instructions 内联** | ✅ 迁移「规则外置为文件」+「hooks 配置化」；❌ 不迁移 subagent（9-21 已论证收益为零） |
| **Codex Harness** | app-server + Item/Turn/Thread 抽象 | HTTP 层已整体归档（2026-09-22） | ✅ 迁移 Thread/Item 的**状态建模**；❌ 不重启 HTTP（冻结决策，勿推翻） |
| **OpenClaw** | 权限分离、网关化、3200+ skills | 单机、单用户 | ❌ 不适用（部署拓扑不同） |
| **Hermes Agent** | 文件即记忆、自进化、自修复验证 | `episode_store/recall` 已实现但**决定不接主链** | ✅ 折中：不做上下文注入，做「离线复盘 + 失败 case 定向注入」 |

### 4.2 2026 趋势对照（置信度标注）

> 防污染声明：以下内容中，**仅有明确一手出处者标注 `verified`**；来自聚合站/个人博客/厂商营销页的
> 归入 `unverified`，**只作趋势旁证，不作设计依据**，也未写入知识库。

| 趋势 | 来源等级 | 与 FORGE 的关系 |
|---|---|---|
| **Harnessed Agentic RL**：harness 参与训练循环（Agent Lightning v1.0，2026-08；Qwen-3.5-9B SWE-bench 41.8%→56.4%） | 一手出处待核 `unverified` | **你正在做的 Laya 微调正是这条路**。方向前沿、判断正确——这是本次重估中最正面的发现 |
| **Gavel**（arXiv 2026）：路由信号已存在于 frozen LLM 中间层激活，用两个线性映射打分全库 | 一手出处待核 `unverified` | 对 FORGE 的 T 层是**替代路线**：与其外挂小模型，可能不如读主模型自身的激活。值得跟踪，但**优先级低于把现有 Laya 跑通** |
| Harness 从循环演进为语义任务运行时（Scope/Context/Capability/State/Execution/Policy 六抽象） | `unverified`（聚合站） | FORGE 六层已实质覆盖，只是命名不同。**无动作** |
| 「好 harness 是**拿走** LLM 不该管的东西」 | `unverified`（个人博客） | 与 FORGE 厚治理**同向**，是既有路线的外部印证 |
| **过度工程会拖累性能**：Anthropic 发现 Context Reset 在模型升级后变多余 | `unverified`（聚合站，二手引述） | ⚠️ **警示**：直接支持「门减法」的必要性，与 §2.5 独立得出的结论一致 |

**结论**：FORGE 在"厚治理 + 确定性判定"上**不是落伍，而是超前**；真正落后的是
**上下文计量（C）与评估统计效力（V）**这两块"测量基础设施"。而**自训路由模型（Laya 路线）**
恰好踩在 2026 最前沿的 Harnessed-RL 方向上——**方向对，但工程没跑通**。

---

## 5. 调整方案

### 5.1 范式 P 决策（标注状态）

| 子维度 | 现状 | 建议 | 状态 |
|---|---|---|---|
| 扩展方式 | 插件化（MCP/skills）+ **特权主链** | **保留特权主链**，但把 11 道门改为**可插拔门链**（注册表 + 配置驱动） | 默认假设 |
| 配置方式 | env 驱动（18 个 `FORGE_*`）+ 代码硬编码 | **规则外置**：instructions 与门定义走文件/声明式，env 只管开关 | 默认假设 |
| 部署拓扑 | 单机（CLI/daemon/benchmark），HTTP 已归档 | **不动**。不重启 HTTP 层 | 已确认（你的决策） |
| 编排模式 | 单 Agent 单主链 | **不动**。multi-agent 会让 FileScope/Approval 所有权失效 | 已确认（你的决策） |

> **核心建议一句话**：内核继续厚，但**把"厚"从硬编码改成可配置**。这样既保住护城河，
> 又拿到插件化的演进速度——这不是向 DeepSeek 的「无特权内核」投降，而是在**门这一层**做插件化，
> 主链所有权仍然集中。

### 5.2 六层调整方向

| 层 | 调整 | 预期收益 |
|---|---|---|
| **E** | `runner.py` 拆分（`_patch_agent_tools` 890 行 → 若干 helper） | 加一道门的边际成本下降 |
| **T** | ① 修 Laya 三连 bug；② 门链注册表化；③ 快筛短路仅对低风险 intent 生效 | 路由可用 + 演进快 |
| **C** | 接真实 token 计量 → 80% 水位触发压缩；工具返回 2000 字符硬截断 | 长会话可控、成本可预测 |
| **S** | 修同列异义（`task_id` 语义分裂）；加「model-visible ⇒ logged」断言 | 审计可重建 |
| **L** | **门减法**：把 11 道门配置化，先关掉实测无净收益的项 | 治规则疲劳 |
| **V** | n=200 对照；修评测/生产 parity；**路由层独立评估（精度优先）** | 结论可信 |

---

## 6. 完善修复步骤

> 每个 Phase 都可独立提交、独立回滚。判据全部可验证。

### Phase 0 · 止血（约 30 分钟，建议立刻做）

**Step 0.1 — 修 Bug A/B/C**

`runtime/laya_router.py` 删除模块级 `_SINGLETON = LayaRouter()`，改为懒加载 + 模块级函数 API：

```python
_SINGLETON: Optional["LayaRouter"] = None


def laya_router() -> "LayaRouter":
    """懒加载单例：构造会加载 checkpoint，只在真正需要时才做。"""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = LayaRouter()
    return _SINGLETON


def laya_screen(query: str) -> Optional[str]:
    """模块级快筛入口。未启用/未安装/置信度不足/异常 → None（一律回落）。"""
    if not laya_router_enabled():
        return None
    try:
        return laya_router().screen(query)
    except Exception:
        return None          # 快筛绝不能阻塞主链
```

`runtime/runner.py:1667-1672` 改为：

```python
from runtime import laya_router
if laya_router.laya_router_enabled():
    if laya_router.laya_screen(message) == "direct_text":
        slog.info("Laya 快筛短路 direct_text")
        return chosen.clone(tools=[])      # 返回 Agent（非 list），只清空工具面
```

**Step 0.2 — 清 Bug E 的冗余前向**：`screen()` 内删除 `self._router.route(...)` 那次调用，
只保留 `predict(...)`；按 laya 0.3.3 实测结构写死字段，去掉 `_extract_confidence` 的猜字段逻辑。

**Step 0.3 — 补回归测试**（`tests/test_laya_router.py` 增补 3 条）：
1. `laya_screen()` 在启用状态下**不得抛 TypeError**（F1 回归）；
2. 短路返回值必须**有 `.clone` 属性**（F2 回归）；
3. `FORGE_LAYA=off` 时 `import runtime.laya_router` **不构造 Router**（F3 回归，断言
   `_SINGLETON is None`）。

**验收判据**：
```bash
PYTHONPATH= .venv/Scripts/python.exe -m pytest tests/test_laya_router.py -q
PYTHONPATH= .venv/Scripts/python.exe -m pytest tests/ -q     # 基线 1408+ 不回归
```

### Phase 1 · 测量基础设施（1–2 天）

> 理由：C 层的"无 token 计量"和 V 层的"n=50"是同一类病——**没有可靠的尺子**。
> 在尺子修好前，任何"优化"都无法证伪。

1. **接 token 计量**：在 `provider_gateway` 记录每轮 prompt/completion tokens，
   落 `task_events`；压缩触发改为 `当前 tokens ≥ max × 0.8`（对齐 DeepSeek 的水位策略）。
2. **工具返回硬截断**：`read_workspace_file` 等超 2000 字符写文件只回路径（9-21 已识别、未做）。
3. **n=200 对照**：4×n=50 合并，检验 agnes-3.0 vs 2.5 的 −8pp 是否真实
   （n=200 → ≈±14pp）。判据：CI 是否含 0。
4. **修同列异义**：`tool_calls.task_id`(run_id) 与 `messages.task_id`(容器 id) 至少改名其一，
   并加一条 join 回归测试。

### Phase 2 · 结构化减负（3–5 天）

5. **门链注册表化**：新建 `runtime/gates/registry.py`，11 道门改为 `(name, predicate, on_block)`
   三元组注册，`.env` 或 `gates.yaml` 控制启停。**先做"能关"，再做"减"。**
6. **门减法**：跑一次「逐门消融」（关掉某门 → n=100 评测 → 看是否劣于基线），
   把实测无净收益的门默认关闭。判据用**统计检验（不劣于基线）**，不用绝对值。
7. **`runner.py` 拆分**：`_patch_agent_tools` 890 行 → `needs_user` / `constraint` / `ledger` 三个 helper。
8. **instructions 外置**：`agent.py` 229 行内联 prompt 抽到 `prompts/agent.md`，
   支持 per-project 覆盖（对齐 Claude Code 的 CLAUDE.md 思路）。

### Phase 3 · 路由与进化（1–2 周）

9. **Laya 路线 A 收口**（按顺序，前一步不过不进下一步）：
   - 9.1 生成 `data/laya_tool_intent/test_final.jsonl`（154 条标注 → review 后落盘）；
   - 9.2 处理类别不平衡：class weight 或重采样，**不要**只看到处可见的 77.9% baseline；
   - 9.3 在 val 集扫阈值，取满足 `tool 召回 ≥0.995` 的最大阈值（替换拍脑袋的 0.85）；
   - 9.4 验收改为**精度优先门禁**（`direct_text` 精确率 ≥0.99 + `tool` 召回 ≥0.995），
       总体准确率降级为参考；
   - 9.5 短路上线前**先在影子模式跑**（只记录判断、不短路），对比影子判断与现有路由的实际结果。
10. **评测→策略回流闭环**：失败 case 的 `terminal.kind` 分布 → 自动映射到策略调整建议。
11. **Prompt caching 探测**：探测 `AGNES_BASE_URL` 是否支持；不支持则做**前缀顺序固化**
    （人设 → 工具 schema → 技能片段，每轮一致）+ 路由子集固定次序，减少前缀 diff。
12. **跟踪 Gavel 路线**（仅调研）：读主模型中间层激活做路由，是否与自训小模型互补。

---

## 7. 层间交叉检查

| 关系 | 结论 |
|---|---|
| E 终止 依赖 V | ✅ Completion Gate 用确定性 verdict，不依赖模型自评——**保持，勿改为 LLM-as-judge** |
| C 清理 影响 E 成本 | ⚠️ 压缩靠代理指标触发 → 要么过度压缩（丢信息）要么不压缩（溢出），**先补 token 计量** |
| S 写 被 L 拦截 | ✅ 11 道门覆盖写路径 |
| T 快筛 影响 V | ⚠️ **新问题**：Laya 短路跳过工具 → 若误判，`_record_tool` 无记录 → ExecutionEvidence 空 →
  Completion Gate 会因 `CLAIM_UNSUPPORTED` 兜底拦截。**兜底存在，但浪费整轮**。
  故快筛精度必须 ≥0.99，且建议**影子模式先跑** |
| L 规则数 影响 E 遵循率 | ⚠️ 11 门 + 7 verdict + 9 类拦截 —— **建议做减法而非加法** |

---

## 8. 待你拍板的决策（我不替你决定）

| # | 决策 | 我的建议 | 依据 |
|---|---|---|---|
| D1 | Laya 快筛是否现在上？ | **先修 bug，再影子模式跑，暂不短路上线** | 错误方向不对称，误短路是静默假完成 |
| D2 | 门减法先做还是先做门链配置化？ | **先配置化（能关），再消融（减）** | 没有开关就无法做消融实验 |
| D3 | episode 层是否接主链？ | **维持你的决定：不接**。改为离线复盘 + 定向注入 | 三臂 n=100 已证 A vs B 无效，投喂不划算 |
| D4 | n=200 对照与 Phase 2 谁先？ | **n=200 先** | 没有可信尺子，结构改造的收益无法证伪 |

---

## 9. 遗留问题与未知区（诚实标注）

1. **Laya 真机精度未知**：`forge_finetuned` 已产出（1.68GB checkpoint），但
   `test_final.jsonl` 不存在 → **微调收益至今未经验证**。在它跑完前，
   "Laya 快筛能带来多少收益"是**未知**，不应写进任何收益承诺。
2. **网关能力未探测**：prompt cache / reasoning_effort 是否支持，决定 Phase 3-11 的落地形态。
3. **`decision_checkpoint` 评测入口仍绕 `run_turn`**：影响评测结论外推生产的可信度，本次未改。
4. **§4.2 的一手来源未逐条核实**：Agent Lightning / Gavel / Anthropic Context Reset 均来自
   二手聚合，**已标 `unverified`，未写入知识库**。若要用作决策依据，需先核到一手
   （arXiv DOI / 官方博客）。本次未做该核实（超出架构重估范围）。
5. **`direct_text` 短路与 Completion Gate 的交互**未做端到端实测：理论上 Gate 会兜底
   （`CLAIM_UNSUPPORTED`），但建议补一条端到端用例坐实。

---

## 附：实证证据索引

| 结论 | 证据 |
|---|---|
| F1 TypeError | 实跑 `LayaRouter.screen('今天天气怎么样')` → `TypeError: missing 1 required positional argument: 'query'`；AST 确认 `screen(['self','query'])` decorators=0 |
| laya/torch 已装 | `.venv/Lib/site-packages`: `laya-0.3.3.dist-info`、`torch-2.14.0.dist-info` |
| 默认启用 | `.env` grep `LAYA` = 0 命中；`_env_flag("FORGE_LAYA", default=True)` |
| 三层兜底全失败 | AST：`run_turn`(1934-3330) 内 try 区间 (2136,2159)/(2153,2159)，2159 行无保护；三处调用同为 `route_agent` |
| F3 11.6s | 实跑 `import runtime.laya_router` → `import ok in 11.6s`，`_SINGLETON._router is not None` |
| 类别不平衡 | `data/laya_tool_intent/manifest.json`: tool 440 / text 125，tool_ratio 0.7788 |
| 验收卡住 | `data/laya_tool_intent/test_final.jsonl` 不存在；train 453 / val 56 / test 56 / 标注 154 |
| episode 未接主链 | grep `episode_recall` in runner.py/main.py/cli/app.py = 0 命中 |
| laya 真实 API | `laya/_laya_inspect/laya-main/laya/router.py`: `class Router`(L123)、`route`(L257)、`predict`(L315) |
