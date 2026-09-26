# FORGE 新一代分层架构设计方案

**日期**：2026-09-24　**专家**：墨衡 · Agent Harness 架构设计师
**性质**：可编码架构设计文档（从"用户六层想象"重设计为七阶段分层架构）
**基线事实**：Laya 是**非自回归判别器**（`system_one(state, questions)`，choice/score/noul 三类问题，
输出概率分布，无工具调用、无文本生成能力）；现有 37 工具 + 11 道门 + 审批 + Completion Gate 已就绪。

---

## 1. 需求规格

| 维度 | 内容 | 状态 |
|---|---|---|
| 目标与用途 | 把 Laya 小模型作为第一层快筛，LLM 做补全/比对，工具层执行，形成有界限的循环，配六类记忆 | 已确认 |
| 成功标准 | ① Laya 快筛精度达门禁（direct_text 精确率 ≥0.99，tool 召回 ≥0.995）② 循环不失控（硬上限）③ 结果经确定性 Gate 落地 MD | 已确认 |
| 任务结构 | 单用户请求 → 七阶段流水线 → 终态收口 | 已确认 |
| 数据/资源边界 | 复用 agent.db + sessions.sqlite；新增 run_plan 表记录过程记忆 | 已确认 |
| 硬约束 | 不破坏单 Run 所有权 / FileScope / ApprovalGate；Laya 快筛不得绕过任何门 | 已确认 |
| 用户场景 | CLI / daemon / benchmark 三入口 | 已确认 |
| 边界（不做什么） | 不做 multi-agent swarm；不重启已归档的 HTTP 层 | 已确认（你的决策） |
| 技术栈约束 | Python + openai agents SDK + laya(0.3.3) + torch + SQLite WAL | 已确认 |

---

## 2. 架构范式 P

| 子维度 | 决策 | 理由 |
|---|---|---|
| 扩展方式 | 门链注册表化 + 能力插件（Laya/LLM/工具都是可替换插件） | 保主链所有权，同时拿到演进速度 |
| 配置方式 | 声明式 `forge.yaml` + env 开关覆盖 | 规则外置，env 只管开关 |
| 部署拓扑 | 单机（CLI/daemon/benchmark） | 已确认，不动 |
| 编排模式 | 单 Agent 单主链 + 确定性编排器 | 已确认，不动 |

---

## 3. 技术栈选型

| 项 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.13（managed venv） | 现有 |
| 运行时 | openai agents SDK（Runner） | 现有主链 |
| 判别器 | laya 0.3.3 `Agent.system_one` | 33ms 非自回归，做意图/路由/完备性 |
| LLM | agnes-3.0-flash（gateway） | 现有 |
| 存储 | SQLite WAL（agent.db + sessions.sqlite） | 现有 |
| 结构化日志 | runtime.structured_log `slog` | 现有 D3 |

---

## 4. 七阶段逐层设计

### 阶段① 用户对话入口
- 决策：三入口（CLI/daemon/benchmark）统一进 `run_turn`。
- 落地：不改。新增 `plan` 参数透传编排意图。

### 阶段② L1 Laya 判别层（33ms）
**职责**：对每个 query 跑**三个并行判别**（一次 `system_one` 前向）：

| 判别 | 类型 | 选项 | 用途 |
|---|---|---|---|
| intent | choice | `{greeting, arith, tool_needed, ambiguous}` | 直接回答 vs 需工具 |
| route | choice | 候选工具名子集（≤8）+ `none` | 工具路由 |
| completeness | choice | `{self_sufficient, need_llm}` | 是否需要 LLM 补全 |

**落地规格**：
```python
# runtime/laya_router.py（重写）
class LayaRouter:
    def __init__(self, checkpoint, device="cpu"):
        from laya.agent import Agent
        self._agent = Agent(str(checkpoint), device=device)   # 惰性构造

    def classify(self, query: str) -> LayaDecision:           # 一次前向三判别
        q = {
            "intent": {"type": "choice", "instructions": query,
                       "criteria": {"greeting": "…", "arith": "…",
                                    "tool_needed": "…", "ambiguous": "…"}},
            "route":  {"type": "choice", "instructions": query, "criteria": tool_set},
            "completeness": {"type": "choice", "instructions": query,
                             "criteria": {"self_sufficient": "…", "need_llm": "…"}},
        }
        res = self._agent.system_one(query, q)
        return LayaDecision.from_answers(res["answers"])
```
- 失败处理：任何异常 → `LayaDecision.fallback()`（全 None）→ 编排器回落 LLM 兜底。
- **三判别的 checkpoint 需要**：意图/完备性用现有微调头；route 需训练多路头。见 §9 遗留。

### 阶段③ 编排器 Orchestrator（确定性，核心）
**职责**：读 LayaDecision，产出**唯一**的下一步指令。

```python
# runtime/orchestrator.py（新建）
@dataclass
class Step:
    kind: Literal["direct_tool", "need_llm", "direct_text", "llm_fallback"]
    tool: str | None = None
    args: dict | None = None

def decide(d: LayaDecision, conf_thr: float, tool_recall_thr: float) -> Step:
    if d.intent == "direct_text" and d.intent_conf >= conf_thr:
        return Step("direct_text")                    # 纯文本，0 工具
    if d.intent in ("tool_needed", "ambiguous"):
        if d.completeness == "self_sufficient" and d.route_conf >= tool_recall_thr:
            return Step("direct_tool", tool=d.route, args=None)   # 直接派发
        if d.completeness == "need_llm":
            return Step("need_llm", tool=d.route)      # LLM 补全后派发
    return Step("llm_fallback")                        # 置信度不足 → 全量 LLM 兜底
```
- **关键安全原则**：`direct_tool` 只对**低风险工具**（读/发现类）生效；凡命中 `_P9_MUTATION_TOOLS`
  的派发，**强制走 need_llm 或 ApprovalGate**，绝不由 Laya 直接触发写操作。

### 阶段④ L2 LLM 补全层（需要时）
- 决策：`need_llm` 时，把「Laya 判定的工具 + 缺参数」作为约束喂给 LLM，让 LLM 补全参数/上下文。
- 落地：复用现有 Runner 单轮调用，`instructions` 注入 `Laya 判定: 意图=tool_needed, 工具=X, 缺参数=Y`。
- 失败处理：补全失败 → 回落澄清追问（复用 Clarification Gate）。

### 阶段⑤ 工具 / 技能层
- 决策：**完全复用现有** 37 工具 + skills + 11 道门 + ApprovalGate + cancel_scope。**一行不改**。
- 落地：无论 `direct_tool` 还是 `need_llm` 派发，都走既有 `_patch_agent_tools` 拦截链。
  这就是「Laya 快筛不得绕过任何门」的结构保证。

### 阶段⑥ L5 LLM 比对层（三向裁决）
- 决策：工具结果返回后，LLM 比对「结果 vs 用户问题」，产出三向裁决 + 确定性 Gate 兜底。

| 裁决 | 条件 | 动作 |
|---|---|---|
| **完成** | 结果覆盖问题 + Completion Gate PASS | 生成 MD 文档 → 终态收口 |
| **再派发** | 结果未覆盖，且 `loop_count < max_loops` | 把「缺口」作为新 query 发回 Laya（有界限） |
| **追认** | 结果部分覆盖 | 记 `run_plan` 为部分完成，补一条说明后收口 |

- **循环界限（硬约束，你点名的关键）**：
  ```python
  max_loops = FORGE_LOOP_MAX  # 默认 3
  loop_budget = FORGE_LOOP_BUDGET  # 默认 token/墙钟双闸
  # 两个条件任一超限 → 强制 Completion Gate 收口，禁止再派发
  ```
- 落地：`completion.py` 已有 6 类 verdict，新增 `LOOP_EXHAUSTED` 一类，触发即收口。

### 阶段⑦ 记忆层（六类记忆，横切）

| 记忆类型 | 现有 | 存储 | 写入时机 | 读取时机 |
|---|---|---|---|---|
| 过程记忆 | ✗ 新建 | `run_plan` 表（run_id + step + decision + result） | 编排器每步写 | 回环时供 LLM 看"已做过什么" |
| 会话记忆 | ✓ | sessions.sqlite | 工具/消息记账 | 上下文组装 |
| 长久记忆 | ✓ | memories 表 + remember/recall | 显式 remember | recall_memory |
| 文件夹记忆 | ✗ 新建 | `folder_memory` 表（路径 → 索引） | index_workspace | search_documents |
| 工作记忆 | ✓ | RunContext（scratchpad） | think 工具 | 当轮 |
| 情节记忆 | ✓ | episode_store（离线） | ingest_pending | 定向注入（你的决策：不接主链） |

- **核心纪律**：「model-visible ⇒ logged」不变量——凡进模型的内容必落账，凡落账必可重建。
- **过程记忆是新增关键**：它让"循环有界限"有了依据——回环时 LLM 能看到上一步的决策与结果，
  避免重复劳动，这正是你构想的"循环"能收敛而非空转的前提。

---

## 5. 模块 / 目录结构（新增部分）

```
runtime/
├── laya_router.py        # 重写：Laya 判别层（三判别 + 惰性加载 + 降级）
├── orchestrator.py       # 新建：编排器（Step 决策 + 循环界限）
├── loop_gate.py          # 新建：循环界限闸（max_loops + budget）
├── folder_memory.py      # 新建：文件夹记忆（路径索引 + 检索）
├── runner.py             # 改：route_agent 改走 orchestrator；修 F1-F3
├── completion.py         # 改：新增 LOOP_EXHAUSTED verdict
└── ...                   # 其余不动
```

---

## 6. 关键接口签名

```python
# runtime/laya_router.py
@dataclass
class LayaDecision:
    intent: str | None = None
    intent_conf: float = 0.0
    route: str | None = None
    route_conf: float = 0.0
    completeness: str | None = None
    completeness_conf: float = 0.0
    @classmethod
    def fallback(cls) -> "LayaDecision": ...       # 全 None

class LayaRouter:
    def classify(self, query: str) -> LayaDecision: ...

# runtime/orchestrator.py
def decide(d: LayaDecision, conf_thr: float, tool_recall_thr: float) -> Step: ...

# runtime/loop_gate.py
class LoopGate:
    def __init__(self, max_loops: int, budget: int): ...
    def can_continue(self, loop_count: int) -> bool: ...   # 返回 False 即收口

# runtime/folder_memory.py
def index_folder(path: str) -> int: ...
def search_folder(query: str, path: str, k: int = 5) -> list[dict]: ...
```

---

## 7. 核心数据结构

```python
# run_plan 表（过程记忆）
CREATE TABLE run_plan (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    decision TEXT,          -- direct_tool / need_llm / direct_text / llm_fallback
    tool TEXT,
    args_json TEXT,
    result_summary TEXT,    -- ≤2000 字符
    loop_count INTEGER,
    created_at TEXT
);

# folder_memory 表（文件夹记忆）
CREATE TABLE folder_memory (
    path TEXT PRIMARY KEY,
    indexed_at TEXT,
    doc_count INTEGER,
    summary TEXT
);
```

---

## 8. 配置格式（forge.yaml）

```yaml
orchestrator:
  laya_enabled: true          # 一键关
  laya_confidence: 0.85       # direct_text 阈值
  laya_tool_recall: 0.995     # direct_tool 召回阈值
  direct_tool_risk: "read_only"   # Laya 直连仅限只读工具

loop:
  max_loops: 3                # 硬上限（你点名的"界限"）
  token_budget: 20000
  wall_budget_sec: 300

memory:
  folder_paths: []            # 指定记忆的文件夹
  episode_inject: false       # 维持你的决策：不接主链
```

---

## 9. 遗留问题与未知区（诚实标注）

1. **route 多路头的 checkpoint 未训练**：现有微调头只做 `needs_tool` 二分类；
   `route`（≤8 工具多路）与 `completeness` 两个判别头**需要重新标注 + 微调**，
   否则 L1 判别层只能先跑 intent 单路，route 回落现有 `tool_router.py` 关键词表。
   **建议上线顺序：intent 单路先行 → route 头补训后接入。**
2. **Laya 真机精度未知**：`test_final.jsonl` 未生成，微调收益未验证。门禁见 §10。
3. **`direct_tool` 与 Completion Gate 交互**未端到端实测（理论上 Gate 兜底，需补用例坐实）。
4. **循环收敛性**：`max_loops=3` 是经验值，需用 n=100 评测验证"3 轮内收敛率"，
   再定是否调。当前无该数据。

---

## 10. 上线门禁（精度优先，纠正此前的总体准确率陷阱）

| 指标 | 门禁 | 理由 |
|---|---|---|
| direct_text 精确率 | ≥ 0.99 | 被短路的样本里真正不需要工具的比例 |
| tool 召回 | ≥ 0.995 | 该用工具的绝不能被短路（安全方向） |
| 循环 3 轮内收敛率 | ≥ 0.95 | 你的"界限"是否够用的实测依据 |
| 全量回归 | 1408+ passed | 不破坏既有护城河 |
| 总体准确率 | 仅参考 | 不平衡数据下会骗人，不作门禁 |

---

## 11. 分阶段实施路线

| Phase | 内容 | 前置 | 验收 |
|---|---|---|---|
| **P0 止血** | 修 F1-F3 三连 bug + 惰性加载 + 3 条回归 | 无 | test_laya_router 全绿 |
| **P1 编排器** | 新建 orchestrator + loop_gate + run_plan 表 | P0 | decide() 单测 + 循环界限单测 |
| **P2 intent 单路** | Laya intent 判别接编排器（route 仍走关键词表） | P1 | 影子模式对比 |
| **P3 比对层** | 三向裁决 + LOOP_EXHAUSTED + MD 落地 | P2 | 端到端用例 |
| **P4 记忆层** | folder_memory + 过程记忆接入 | P1 | 检索/写读分离单测 |
| **P5 route 头补训** | 标注 route/completeness 多路头 → 微调 | P2 | 精度门禁达标后切全量 |

---

## 12. 与你原构想的差异回执（必须让你清楚）

| 你的说法 | 我的落地 | 为什么 |
|---|---|---|
| "Laya 直接调用工具" | 编排器确定性派发 | Laya 无工具调用能力，且直连写操作不安全 |
| "Laya 补充内容" | LLM 补全层 | Laya 无文本生成能力 |
| "两层判断出两方向" | 三向裁决（完成/再派发/追认） | 部分覆盖需单独处理，不能强行二选一 |
| "循环有界限" | LoopGate 硬上限 + LOOP_EXHAUSTED | 保留你的核心创意，做成确定性闸 |
| "四类记忆" | 六类记忆（补过程记忆 + 文件夹记忆） | 过程记忆是循环收敛的前提，文件夹记忆是你的明确需求 |
