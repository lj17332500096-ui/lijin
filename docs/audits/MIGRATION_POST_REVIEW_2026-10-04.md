# 架构迁移后复审报告（LANGGRAPH_TASK_PLAN_BOUNDARY 迁移）

> ⚠️ **勘误（2026-10-04 P2/P3 实测后追加）**：本报告有 **6 处事实性错误**已在下游阶段被实测推翻。
> 原文一律保留以存审计痕迹（不做事后美化），**但结论必须按 [§11 勘误表](#11-勘误表p2p3-实测更正) 采信**。
> 最关键的一处：§3.5 认定簇 1 的真凶是三个模块的 import 期 `load_dotenv`，**那不是充分集** ——
> 真凶是 `runtime_paths.py:16-20`，它被所有模块传递导入。

> 设计台账：`2026-10-04` ｜ 阶段：P1 架构复审 ｜ 复审对象：`aa05c1f`/`b861a73`/`7c5191d`/`4e66d60`/`265a558`/`b5d3b3f`/`c3f4b0f`/`dd634d1`
> 框架：Agent Harness `H = (E, T, C, S, L, V) + P`
> 性质：**重构轨**（评估既有系统），非从零设计。本报告只做复审与修复排序，**不含任何生产代码改动**。

---

## 0. 执行摘要（先读这一节）

本次复审推翻了委托简报中的 **1 条核心推断**，并把「22 条红灯」重新归因。三个必须先记住的结论：

| # | 结论 | 与简报的关系 |
|---|---|---|
| **A** | **簇 1（15 条审批测试全红）不是迁移造成的。** 真因是本机未跟踪的 `.env:86 APPROVAL=off`，经 `runtime/compact.py:25` 等模块的 **import 期 `load_dotenv`** 泄漏成进程级环境变量，使 `runtime/approval.py:150` 的审批门整体关闭。4 个相关文件迁移前后 **md5 完全一致**。 | ❌ 推翻「护栏被迁移打断」 |
| **B** | **簇 1 仍是 P0**，但 P0 的性质是「**护栏开关由一个未跟踪文件单向控制 + 15 条断言静默失去鉴别力**」，属于配置治理缺陷，不是编排缺陷。 | ✅ 严重级成立，✂️ 成因改写 |
| **C** | **簇 3（只读库）确实是迁移引入的。** 已用对照实验证明：把迁移新增的 `routing.selection.pending` 事件写入置空，该测试 **2 passed**；开启则失败。真因在 **S 层连接管理无韧性**，被 E 层新增的每 Run 写放大触发。 | ✅ 成立，且定位到层 |

另有两处**委托简报中的事实偏差**已在 §7 更正（`benchmark/eval_runner.py` 并未被删除；`annotation/` 确实已全删）。

以及一个**必须由 P2 补齐的账目缺口**：本报告逐簇实证归因到 **18 条**，与简报的 22 条差 **4 条**，这 4 条的真实归属我未验证（§7  unknowns #1）。我不会替它们编造归因。

---

## 1. 复审方法与证据纪律

- **不采信委托简报的推断**，每簇结论均以「文件:行号」+ 可复现实验为准。
- 只跑 **4 次聚焦测试**（审批 22 条用例 19s、名册+能力 23 条 14s、并发 2 条 16~41s），**未跑全量**（遵委托约束）。
- 对簇 3 使用了 **对照实验**（monkeypatch 中和单一变量）而非相关性推断，对照组/实验组结果都记录在案。
- 插桩文件已清理（`var/tmp/probe_*.py` 已删）。

复现命令统一前缀（否则本机 `shutil` 行为异常会产生假失败）：

```bash
cd F:/Byong-hermes/Byong-hermes/my_creative_agent
PYTHONPATH= ./.venv/Scripts/python.exe -m pytest <目标> -q
```

---

## 2. 架构范式 P 的第一性原理判定

### 2.1 迁移实际选择的范式（正交子维度拆解，而非四选一）

简报把这次迁移称作「换成 LangGraph」。按 P 的多维分解，实际发生的是**四个正交子维度同时变更**：

| 子维度 | 迁移前 | 迁移后 | 变更性质 |
|---|---|---|---|
| **决策主体** | 关键词规则（`runtime/tool_router.py` + `laya_router.py`） | LLM 决策（LangGraph `select_tools` 节点） | 语义判断归属从规则 → LLM，**方向正确** |
| **工作流拓扑** | 单层线性 + 隐式重试 | 有界图（analyze→select_tools→plan→execute→review，6 次执行上限 + 1 次补充） | 显式化，**可审计性提升** |
| **状态载体** | 内存态为主 | 事件流落库（`routing.decision` / `routing.selection.pending`） | 可观测性提升，**但写放大打到 S 层** |
| **决策粒度** | 关键词分组 → 工具集 | LLM 选「暴露哪些已注册工具」 | 未越权，Runtime 仍握授权 |

### 2.2 第一性原理判据：这次迁移做对了什么

判据一：**「语义判断」是否离开了规则层。**
✅ 工具选择从关键词规则迁到 LLM 节点（`runtime/conversation_workflow.py:71-74` + `runtime/runner.py:3581`）。这是本次迁移最大的正确决策。

判据二：**授权是否仍然只握在 Runtime 一只手。**
✅ 已验证。`runtime/runner.py:3646-3672` 做了四件事：剔除清单外名称（`_valid_names`，3647-3649）、登记未知名称（`_unknown_names`，3650-3651）、补入计划要求工具（`_required_names`，3652-3653）、把计划裁剪进已选范围（3690-3700）。**LangGraph 的输出是"建议"，Runtime 做交集与兜底**，与 `LANGGRAPH_TASK_PLAN_BOUNDARY.md:5` 的声明一致。

判据三：**失效时是否 fail-safe。**
✅ 三层兜底齐全：筛选异常 → 全量（`runner.py:3640-3644`）、空选 → 全量（3670-3672）、clone 异常 → 全量（3680-3685）。**这一点做得好，值得保留。**

判据四：**决策成本的经济性是否被度量。**
❌ 未见任何门控。`runner.py:3711-3733` 记录了 `agent_tool_schema_sha256` 与选择数量，但**没有记录筛选本身的 token 成本/墙钟成本**，因此「32→8~16 个工具」这类声称的收益目前**不可验证**。

### 2.3 范式层的核心缺口：P 少了一个正交维度

> **P 缺少「配置权威（configuration authority）」子维度。**

本次 22 条红灯里最严重的一条（簇 1）**完全落在这一维度上**，而它此前不在任何人的检查表里：
- 谁有权决定护栏开关？
- 决定通过什么通道生效？
- 生效是否可被审计、可被撤销、可被 CI 断言？

`runtime/approval.py:150-151` 的实现是「读一个进程级环境变量」，
而这个环境变量可以被**任何 import 了 `load_dotenv` 的模块**在**导入期**改写。
这不是模型问题、不是编排问题、不是工具问题 —— 这是**配置权威缺位**。

---

## 3. 六层逐层复审

### 3.1 E 层（执行循环）

**现状证据**
- LangGraph 图定义：`runtime/conversation_workflow.py:171-202`（8 节点 + 6 条条件边）。
- 有界重试：`conversation_workflow.py:33`（`MAX_EXECUTION_ATTEMPTS = 6`）、`:101-109`（retry kind 分派）、`:153-169`（单次补充）。
- 图只管转移、handler 归 Runtime：文件头 `:1-6` 明确声明，`:62-65` 明确 `execute`/`review` 通过返回 `{"terminal": ...}` 把审批挂起/用户澄清交还 Runtime。**这个分层是对的。**
- 接线点：`runtime/runner.py:4392-4398`（`build_conversation_workflow(...)`），handler 为 `_workflow_analyze` / `_workflow_select_tools` / `_workflow_plan` / `_workflow_execute` / `_workflow_review`。

**迁移引入的问题**

| # | 问题 | 证据 | 严重级 |
|---|---|---|---|
| E-1 | **每 Run 的 agent 克隆热路径上新增一次同步 DB 写**，且发生在 `route_agent`（模型档位选择）里 —— 一个与状态存储无关的函数 | `runner.py:2004-2017` | **P1**（详见 S-1） |
| E-2 | **该审计写入是 best-effort（失败仅 WARNING）** → 路由决策链可能静默丢事件 | `runner.py:2016-2017`、`runner.py:3736-3738` | **P1**（审计完整性） |
| E-3 | 重试预算硬编码为模块常量，不可按部署/渠道调节 | `conversation_workflow.py:33` | P2 |

**E-1/E-2 的合并判断**：这两条是同一处设计选择的两面。迁移把「LangGraph 正在筛选」这个**瞬时态**写进了 `task_events`，动机正确（可观测性），但实现上用了**同步、best-effort、每 Run 必写**的方式。正确做法见 §4 修复项 P1-3。

---

### 3.2 T 层（工具注册）

**现状证据**
- 注册表：`runtime/registry.py`（79 行）；目录元数据：`runtime/spec.py:143-144`（`anysearch_search` / `anysearch_batch_search` / `anysearch_extract` → `EXTERNAL_FACT`）。
- MCP 动态登记接入点：`integrations/mcp_bridge.py:354-356`（`register_non_file_tools` + `register_gated_names`）。
- 文件边界（fail-closed）：`runtime/filescope.py:128-130` 注释明确「未登记权限策略的工具不得进入生产执行」，实现在 `:236`。
- 模型档位与工具筛选已解耦：`runtime/router.py:68-79`（`route_profile` 只读 `model_profile`/`channel`/`deep`，**不接触任何工具名**）—— 与边界文档 `:7` 一致，✅。

**迁移引入的问题**

| # | 问题 | 证据 | 严重级 |
|---|---|---|---|
| T-1 | `filescope.NON_FILE_TOOLS` 仍含已下线的 `web_search`，**且无任何 `anysearch_*`** | `runtime/filescope.py:134` | **P1** |
| T-2 | 静态名册 3 处仍写旧工具名 | `benchmark/evaluator.py:57`、`benchmark/matrix.py:91`、`runtime/filescope.py:134` | **P1** |
| T-3 | AnySearch 工具的文件边界覆盖**完全依赖 MCP bridge 启动时的动态登记** | `mcp_bridge.py:356` → `filescope.py:155` | P2（fail-closed 方向正确，但可用性变成启动顺序的函数） |

**T-1 的真实影响（比"名册漂移"更严重）**：`filescope.py:236` 是 fail-closed 的。
`anysearch_*` 三个工具不在静态名册里，**只有**在 MCP bridge 走完 `mcp_bridge.py:356` 的登记后才被覆盖。
一旦 bridge 未启用/策略变更/登记早于工具挂载，严格 Project 下的联网搜索会被**直接 DENY**。
这是"安全方向正确、可用性脆弱"的典型权衡，**不是 bug，但必须在契约里写明**。

---

### 3.3 C 层（上下文管理）

**现状证据**
- 能力自省为**只读事实层**，不建第二套注册表：`runtime/capability_introspection.py:8-11`。
- 展示名登记：`capability_introspection.py:26-85`（`DISPLAY_NAMES`）；`265a558` 从中**删掉了** `"web_search": "联网搜索"`（见 `git show 265a558 -- runtime/capability_introspection.py`），**但没有补任何 `anysearch_*` 条目**。
- 能力问题的**硬隔离**：`runner.py:1993-1998` —— 命中能力问题时 `chosen.clone(tools=[])`，工具被清空。
- 意图门：`capability_introspection.py:135-160`（`_CAPABILITY_RE` / `_CAPABILITY_QUERY_SHAPE`），判定入口 `:187-189`。

**迁移引入 / 遗留的问题**

| # | 问题 | 证据 | 严重级 |
|---|---|---|---|
| C-1 | **能力自省缺 AnySearch 展示名** → 真实挂载的 `anysearch_*` 会显示裸 tool_id | `capability_introspection.py:26-85`（无 `anysearch_*`）；测试 `test_capability_introspection.py:143` | **P1** |
| C-2 | **意图门是会误伤的正则门，且带破坏性副作用**：`tools=[]` | `capability_introspection.py:135-160` + `runner.py:1997` | **P1** |
| C-3 | **工作区未提交改动在继续堆同义句正则**（新增 10 行） | `git diff runtime/capability_introspection.py` | **P1（阻断合并）** |

**对委托简报"是否违反分工宪法"的复核结论：判断成立，且我要给出比"堆规则"更准的定性。**

`AGENTS.md` 宪法原文：**「面向业务语义的判断绝不能用关键字/正则/规则匹配」**。
`capability_introspection.py:135-160` 用 `你(?:现在|目前|当前)?(?:可以|能|会).{0,12}(?:做什么|做啥|做哪些|干什么|干啥|干嘛|帮我做什么|帮我做哪些|帮我干啥)` 判定**用户是否在问能力** —— 这是对自然语言业务语义的关键词匹配，**直接违反宪法**。

但真正的病灶比"堆规则"更深一层：

> **堆同义句是这个设计被放在错误层级的必然结果，不是一次纪律滑坡。**
> 同一宪法违规在 `runtime/task_plan.py` 已经成体系地存在，而且**正是 AnySearch 迁移扩大了它**：

| 位置 | 违规内容 | 引入提交 |
|---|---|---|
| `task_plan.py:114-116` | `re.search(r"天气\|气温\|温度\|天气预报", text)` → 选 `get_weather` | 历史遗留 |
| `task_plan.py:122-127` | `re.search(r"anysearch_batch_search", text)` → 选搜索工具 | **`265a558` 新增** |
| `task_plan.py:143-146` | `re.search(r"今天\|今日\|目前\|现在\|最新\|清单\|榜单\|热门…", text)` → 追加 `source_extract` 阶段 | 历史遗留 |
| `router.py:32-52` | `_CODING_SCENE_MARKERS` 关键词表 → 选 `reasoning_effort` 档 | 历史遗留 |

**所以 `capability_introspection.py` 的改动不是孤立的越界，而是既有反模式的扩散。**
按第一性原理，`infer_task_plan` 整个函数就是一个**用正则实现的工具选择器** —— 它与迁移引入的 LLM 选择器**职责重叠**。
迁移把"选工具"从 `tool_router.py` 挪到了 LangGraph，却把 `task_plan.py` 里**第二套规则选择器**留了下来，两者在同一次 Run 里先后生效（`runner.py:3652` 用 `plan_required_tools` 补入 LangGraph 没选中的工具）。**这是本次迁移最被低估的架构残留。**

---

### 3.4 S 层（状态存储）—— 簇 3 真因所在

**现状证据**
- 连接管理：`runtime/task_manager.py:647-655`

```python
@contextmanager
def _connect(self) -> Iterator[sqlite3.Connection]:
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(self.db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
```

- PRAGMA：`task_manager.py:61-64`（`foreign_keys=ON` / `journal_mode=WAL` / `synchronous=NORMAL` / `busy_timeout=5000`）。
- **`task_manager.py` 未被本次迁移改动**（`git show aa05c1f --stat -- runtime/task_manager.py` 为空）。

**迁移引入的问题**

| # | 问题 | 证据 | 严重级 |
|---|---|---|---|
| S-1 | **连接churn + WAL 在并发下退化为 `SQLITE_READONLY`，且无重试** | 见下方对照实验 | **P1** |
| S-2 | **同一份 DB 上，写失败的处理方式不一致**：审计写 best-effort（`runner.py:2016`、`3736`），业务写 fatal（`runner.py:2420` → `task_manager.py:707` 无保护） | `task_manager.py:707` vs `runner.py:2016` | **P1** |

**簇 3 真因（对照实验，非推断）**

| 实验 | 操作 | 结果 |
|---|---|---|
| 基线 | 原样运行 `test_concurrency_stress.py::ConcurrentRunStressTests` | **1 failed**，`sqlite3.OperationalError: attempt to write a readonly database` |
| 对照 | 仅中和 `add_event(..., "routing.selection.pending")`（该写入由 `b861a73` 引入），其余全不变；共中和 12 次 | **2 passed**（41s） |

**因果链（有行号）**

```
b861a73 在 route_agent 里为每个 Run 新增一次同步事件写
  └─ runner.py:2008  self.tasks.add_event(run_id, "routing.selection.pending", {...})
       └─ task_manager.py:1248  conn.execute(...)        ← 连接 #N
            └─ task_manager.py:647-655 _connect()        ← 每次操作新建一个连接，无池化、无重试
                 └─ WAL（task_manager.py:62）+ 8 路并发 run_turn（test:195）
                      └─ -shm 映射在 Windows 上退化为 SQLITE_READONLY
                           └─ 污染同一 DB 的其它连接 → 下一条无关语句炸了：
                                runner.py:2420 get_or_create_container
                                  └─ task_manager.py:707 conn.execute(INSERT INTO tasks ...)
                                       └─ OperationalError（无 try/except → Run 直接失败）
```

**插桩实测到的失败瞬间状态**（在 `_connect` 层打点，失败当场读取）：

```
db_exists=True  dir_exists=True  dir_mode=0o40777
dir_listing=['agent.db', 'agent.db-shm', 'agent.db-wal']
→ 目录可写、DB 存在，纯粹是 WAL 并发下的 SQLITE_READONLY
```

这排除了「临时目录被删」「磁盘满」「文件权限」三种猜测 —— 我逐一测过：
目录/DB 在失败瞬间**存在且可写**；`C:` 剩余 35G；`_connect` 只删除不涉及本路径。
（`tearDown` 的 `shutil.rmtree`（`tests/test_concurrency_stress.py:95`）发生在测试**之后**，是红字噪声的放大器，不是成因。）

**结论**：S 层的**连接韧性**（池化 / 忙等重试 / 失败分类）是缺失的；E 层的**每 Run 写放大**是压垮它的负载。**修复必须落在 S 层**，在 E 层删掉那行写入只是把阈值往后推。

---

### 3.5 L 层（生命周期 / 护栏）—— 簇 1 真因所在

> 委托简报推断「有副作用的工具不再触发审批挂起 = 安全护栏失效」。
> **该推断不成立。** 下面是实证。

**证据一：迁移前后 4 个相关文件字节级一致**

| 文件 | `aa05c1f~1` vs `HEAD` |
|---|---|
| `runtime/approval.py` | **IDENTICAL**（md5 一致） |
| `tests/test_approval_lifecycle.py` | **IDENTICAL** |
| `tests/test_approval_execution_parity.py` | **IDENTICAL** |
| `runtime/errors.py` | **IDENTICAL** |

文件 mtime 佐证：`runtime/approval.py` = 2026-09-27 00:57（迁移发生在 10-01，未被触碰）。

**证据二：真因是 `.env` 的进程级泄漏**

| 步骤 | 证据 |
|---|---|
| ① 本机 `.env` 第 86 行写着 `APPROVAL=off` | `.env:86`（该文件**未被 git 跟踪**，`.gitignore:2`） |
| ② 多个模块在 **import 期**无条件加载它 | `runtime/compact.py:25`、`runtime/codex_loop.py:21`、`runtime/reply_parser.py:27`（均 `load_dotenv(BASE_DIR / ".env")`） |
| ③ 因此**只要 import `runtime.approval`，进程环境就被改写** | 实测：`import runtime.approval` 后 `os.getenv("APPROVAL")` 由 `None` → `'off'` |
| ④ 审批门读它并整体关闭 | `approval.py:150-151` `enabled` → `approval.py:183-186` `should_gate` 返回 `False` |
| ⑤ `check()` 在**创建审批行之前**就返回 `None` | `approval.py:234-235`（早于 `:297` 建行、`:303-308` 抛异常） |
| ⑥ 15 条测试全部命中这条早退路径 | 与 4 个失败形态逐一对应 |

**证据三：反证实验（决定性）**

```bash
PYTHONPATH= APPROVAL=on ./.venv/Scripts/python.exe -m pytest \
  tests/test_approval_lifecycle.py tests/test_approval_execution_parity.py -q
# → 22 passed in 19.29s
```

**簇 1 真因定级**：`runtime/approval.py:150-151` + `runtime/compact.py:25` 构成的
「**import 期全局配置副作用 → 进程级护栏开关**」，被本机一个未跟踪的 `.env` 单向触发。
**它与 LangGraph 迁移无因果关系。**

**L 层真正的问题（这才是 P0）**

| # | 问题 | 证据 | 严重级 |
|---|---|---|---|
| L-1 | **护栏总开关由单个未跟踪 env 字符串单向控制，无启动断言、无审计、无撤销** | `approval.py:150-151` + `.env:86`（未跟踪） | **P0** |
| L-2 | **配置在 import 期产生全局进程副作用** → 进程状态依赖 import 顺序，不可测 | `compact.py:25` / `codex_loop.py:21` / `reply_parser.py:27` | **P0** |
| L-3 | **15 条断言静默失去鉴别力（test vacuity）** —— `assertRaises(ApprovalRequired)` 挂在一个"不可能抛"的路径上，测了很久的空气，**没人发现** | `test_approval_lifecycle.py:34` 等 | **P0** |
| L-4 | 文档与实配相反：`.env.example:93-96` 把 `APPROVAL=on` 写成注释掉的可选项，实际 `.env:86` 是 `off` | 两文件对照 | P1 |

**L-1 的生产含义（这才是真正严重的地方）**：
本机 `.env` 的 `APPROVAL=off` **不只是让测试变红** —— 任何加载这个 `.env` 的进程
（`agent.py:60`、`main.py:43` 都会 `load_dotenv`）**审批护栏整体关闭**。
`approval.py:66-68` 的 `GATED_DEFAULT` 精心派生自 `spec.py` 的 `side_effect` 元数据（`approval.py:59-64`），
设计意图是"漏一个就静默写盘"的防御 —— **而这个防御可以被一行未跟踪配置整体绕过。**
护栏的**派生逻辑再严密，也抵不过派生结果没有强制生效点**。

---

### 3.6 V 层（评估接口）

**现状证据**
- `annotation/` 标注工具链**已全量删除**（`git ls-files annotation/` = 0，仅剩 `__pycache__`）→ 地面真值的**生产工具**消失。
- `benchmark/layer_behavior/` 的执行器与基线被删：`run_benchmark.py`(-150)、`cases.v1.jsonl`(-31)、`results/baseline_20260926.json`(-645)（见 `git show aa05c1f --stat`）。
- 消费这些基线的门 `.github/workflows/router-gate.yml`(-70) 被删，`.github/` 目录整体消失（`ls .github` → No such file）。
- `benchmark/eval_runner.py` **仍然存在**（见 §7 更正 #1）。

**问题**

| # | 问题 | 证据 | 严重级 |
|---|---|---|---|
| V-1 | **V 层对本次迁移的关键维度是瞎的**：`search_call_count` 仍按 `web_search` 计数，AnySearch 流量全部记为 0 | `benchmark/evaluator.py:57`（`SEARCH_TOOLS`）+ `:321-324`（计数逻辑） | **P1** |
| V-2 | **无基线、无门** → 迁移是否引入行为回归，当前**不可验证** | `docs/architecture/LANGGRAPH_TASK_PLAN_BOUNDARY.md` 全篇无指标；`.ci/known_failures.txt` 写「当前：无」 | **P1** |
| V-3 | 评估口径与生产口径可能已经分叉：`benchmark/matrix.py:91` 同样按 `web_search` 判定 | `benchmark/matrix.py:91` | P2 |

**V-1 是本层最实质的问题**：它不是"名字忘了改"的美观问题，
而是**评估层正在对迁移最想验证的那件事（搜索行为）系统性记 0**。
一个恒为 0 的指标比没有指标更危险 —— 它看起来是"有效"的。

---

## 4. 修复优先级表

> **分级原则**：P0 只放**安全与正确性**；P1 放**功能性**；P2 放**体验与工程卫生**。
> **每一项都必须能被一条具体测试验收**（验收列已写明测试名；转绿即验收，不需额外判断）。

### P0 —— 安全与正确性

| ID | 问题 | 落点文件 | 预计改动面 | 验收测试（转绿即完成） | 回归风险 |
|---|---|---|---|---|---|
| **P0-1** | **配置在 import 期污染进程环境**。把 `.env` 加载从模块导入期移到进程入口显式调用；模块内 `os.getenv` 保持不变但不再被 import 副作用喂值 | `runtime/compact.py:25`、`runtime/codex_loop.py:21`、`runtime/reply_parser.py:27`（移除模块级 `load_dotenv`）；`agent.py:60`、`main.py:43`（保留为唯一显式入口） | 3 删 + 2 注释，**约 5 行**；不改任何行为语义 | `tests/test_approval_lifecycle.py`（6 条）+ `tests/test_approval_execution_parity.py`（9 条）+ `tests/test_approval.py::GateTests` | 低。风险点是某模块脱离 `main.py` 单独跑时读不到 `.env` → **必须同时做 P0-2** |
| **P0-2** | **护栏开关无强制生效点**。新增：启动时若 `APPROVAL` 处于关闭态而存在 `side_effect` 工具，必须写审计事件 + 结构化日志；并提供 `FORGE_APPROVAL_FAILCLOSED=on`（默认 on）使生产 profile 下 `APPROVAL=off` 直接拒绝启动 | 新增 `runtime/startup_guard.py`；接入 `runtime/runner.py` 的 Runtime 初始化；`runtime/approval.py:150` 保持为唯一真值源 | 新增 1 文件 + 2 处接线，约 60 行 | 新增 `tests/test_approval_startup_guard.py::test_failclosed_blocks_startup_when_side_effect_tools_present` | 中。若判据写太严会挡住合法的无人值守部署 → **必须由 `.env` 显式声明 `scheduled/daemon` 例外，不能靠猜** |
| **P0-3** | **断言失去鉴别力（test vacuity）**。给 15 条审批断言加前置自检：先断言"门处于开启态"，再断言行为。防止同类静默失效再次发生 15 条一起瞎 | `tests/test_approval_lifecycle.py:23-29`、`tests/test_approval_execution_parity.py:49-54`（setUp 增加 `self.assertTrue(self.gate.enabled, "APPROVAL 被关闭，本用例已失去鉴别力")`） | 2 处 setUp，**约 6 行** | 全部 15 条；并用 `APPROVAL=off` 跑一次，**期望它们以"明确原因"红，而不是空转** | 无。纯增益 |
| **P0-4** | **S 层连接无韧性**。`_connect()` 增加：连接复用（按 `db_path` 复用单连接 + 锁）或最小化重试（`SQLITE_BUSY`/`SQLITE_LOCKED` 指数退避重试 ≤3 次）；`get_or_create_container` 等业务写路径补齐与审计写一致的保护 | `runtime/task_manager.py:647-655`（`_connect`）、`runtime/runner.py:2420`（调用点） | **约 40 行**，S 层核心，风险最高 | `tests/test_concurrency_stress.py::ConcurrentRunStressTests::test_mixed_parallel_runs_approval_and_filescope_isolation` | **中高。** 改连接管理会影响全部 DB 路径 → **必须配套跑 `tests/test_task_manager.py`、`tests/test_approval_lifecycle.py`、`tests/test_concurrency_stress.py` 全文件** |

### P1 —— 功能性

| ID | 问题 | 落点文件 | 预计改动面 | 验收测试 | 回归风险 |
|---|---|---|---|---|---|
| **P1-1** | **名册补齐**：`web_search` → `anysearch_*`（3 个静态名册） | `runtime/filescope.py:134`、`benchmark/evaluator.py:57`、`benchmark/matrix.py:91` | 3 行 | `tests/test_tool_roster_consistency.py::RosterEntryExplainabilityTests::test_non_registered_entries_are_all_documented` | 低。**注意**：修完 `web_search` 就不再是"未注册工具"，`WRONG_NAMES`(`:104`) 也不必动 |
| **P1-2** | **能力自省补 AnySearch 展示名** | `runtime/capability_introspection.py:26-85` | 3 行 | `tests/test_capability_introspection.py::CapabilityDisplayAndFailureTests::test_testcap06_default_friendly_name_no_id` | 低 |
| **P1-3** | **审计写入去同步化**：把 `routing.selection.pending` 从 `route_agent` 热路径移出，改为随 `routing.decision` 一次性落库（后者已含全部字段，见 `runner.py:3719-3735`） | `runtime/runner.py:2004-2017`（删除）、`runtime/runner.py:3718-3738`（合并） | 删 14 行 + 改 1 处 | `tests/test_concurrency_stress.py::ConcurrentRunStressTests::test_mixed_parallel_runs_approval_and_filescope_isolation`（**必须与 P0-4 一起上**，单上会掩盖 S 层缺陷） | 中。`pending` 事件是迁移刻意加的可观测性，删除需确认无下游消费者（我未查到消费者，列为 unknown #4） |
| **P1-4** | **评估层搜索计数修复**（V-1） | `benchmark/evaluator.py:56-58`、`:321-324`、`benchmark/matrix.py:91` | 约 8 行 | `tests/test_benchmark_evaluator.py`（补一条 anysearch 计数用例） | 低 |
| **P1-5** | **`anysearch_*` 的 FileScope 覆盖写入静态名册**，不再只依赖 MCP bridge 运行时登记（T-1/T-3） | `runtime/filescope.py:131-145` | 3 行 | `tests/test_tool_roster_consistency.py::RosterEntryExplainabilityTests::test_filescope_covers_all_registered_tools`（`:311-320` 已有该断言，补数据即转绿） | 低。方向是**收紧**（把动态登记变静态），不会放宽边界 |
| **P1-6** | **阻断 `capability_introspection.py` 的正则堆叠**（工作区未提交改动），改为「宽召回正则只做候选 + 结构化确认后才施加 `tools=[]`」 | `runtime/capability_introspection.py:135-160`（**建议不合并当前工作区改动**）、`runtime/runner.py:1993-1998` | 保留 1 个宽召回正则 + 1 次轻量结构化判定 | `tests/test_capability_introspection.py`（全 21 条）+ 新增 1 条「非能力问题不得被清空工具」 | 中。`tools=[]` 是安全侧动作，收紧召回会降低误伤但可能漏判 → **必须补一条反向用例**（普通任务不得被误清） |
| **P1-7** | **`task_plan.infer_task_plan` 与 LangGraph 选择器职责重叠**（C 层 §3.3） | `runtime/task_plan.py:110-160`；`runtime/runner.py:3652` | **大（需先做设计评审，本轮只登记）** | 需新增 `tests/test_task_plan_no_regex_authority.py`（断言 `task_plan` 不再用 `re.search` 决定工具集） | **高。** 这是本次复审发现的最大未清偿架构债，**不建议在红灯未清时动** |

### P2 —— 体验与工程卫生

| ID | 问题 | 落点文件 | 预计改动面 | 验收测试 | 回归风险 |
|---|---|---|---|---|---|
| **P2-1** | CI 恢复（3 个 workflow，见 §5） | `.github/workflows/ci.yml`、`guard-consistency.yml`、`nightly.yml` | 新增 3 文件 | 自身即验收：PR 上必跑且能挡住人为注入的失败 | 低 |
| **P2-2** | `.ci/known_failures.txt` 与实际失败对齐 + `ci_gate.py` 映射表扩容（当前仅 4 组，`scripts/ci_gate.py:29-40`） | `.ci/known_failures.txt`、`scripts/ci_gate.py:29-40` | 约 20 行 | `scripts/ci_gate.py` 自身单测 | 低 |
| **P2-3** | 重试预算可配置 | `runtime/conversation_workflow.py:33` | 3 行 | 新增 1 条 | 低 |
| **P2-4** | 决策成本埋点（筛选 token / 墙钟），兑现「32→8~16」的说法 | `runtime/runner.py:3718-3735`（事件体加字段） | 约 6 行 | `tests/test_conversation_workflow.py` 扩展 | 低 |
| **P2-5** | 清理死代码：`laya/__pycache__`（14 个已删模块的 .pyc）、`annotation/__pycache__` | `laya/__pycache__/`、`annotation/` | 删目录 | 无（清理类） | 无。**注意：`laya/_laya_inspect/` 必须保留**（Laya 主攻路线的离线依据，见 §6） |

---

## 5. CI 恢复方案

### 5.1 现状与风险

| 项 | 状态 | 证据 |
|---|---|---|
| GitHub Actions | **整目录不存在** | `ls .github` → No such file or directory |
| 本地门禁 | 机制完好，但覆盖面极窄 | `scripts/ci_gate.py:29-40` 仅 4 组映射 |
| 已知失败基线 | 写着「当前：无，全绿」，**与实际 18 条已归因红灯矛盾** | `.ci/known_failures.txt` 末行 |
| `runner.py` 改动 | **改 4819 行主干文件，`CHANGE_TEST_MAP` 里没有任何一条规则会触发测试** | `scripts/ci_gate.py:29-40` 无 `runtime/runner.py` |

**最后一行是本次 CI 缺口最严重的地方**：
`runtime/runner.py` 是全仓最大的单文件（4819 行），承载 E/T/S/L 四层逻辑，
而**改它不会跑任何测试**。这解释了为什么 V-1 这种"评估层记 0"的问题能长期存活。

### 5.2 三个 Workflow

#### ① `.github/workflows/ci.yml` —— PR 门禁主测试（分片）

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  pytest:
    runs-on: windows-latest          # 与本地一致：WAL/SQLITE_READONLY 行为是平台相关
    strategy:
      fail-fast: false
      matrix:
        shard: [1, 2, 3, 4]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      # 关键：CI 不读开发者 .env，配置必须显式注入（P0-1 的前提）
      - name: Inject explicit env (hermetic)
        run: |
          $env:APPROVAL = "on"          # 护栏默认开，测试不得依赖机器状态
          $env:FORGE_TRUSTED_CODE_ROOTS = ""
          $env:FORGE_OBLIGATION_GATE = "off"
      - name: Shard tests
        run: >-
          python -m pytest tests -q
          --ignore=tests/test_slow_e2e.py
          --junitxml=var/test-reports/ci-${{ matrix.shard }}.xml
          -p no:randomly
      - name: Upload report
        if: always()
        uses: actions/upload-artifact@v4
        with: { name: report-${{ matrix.shard }}, path: var/test-reports/ }
```

**分片依据**：本机全量 11 分 16 秒（委托实测）。
4 片 ≈ 每片 3 分钟，落在 GitHub 免费额度内。
分片用 pytest 内置的 `--shard`（pytest 7.4+）或按文件名前缀切分；
**必须保留 `fail-fast: false`**，否则一片红掩盖其余三片的问题。

**为什么 runner 固定 `windows-latest`**：簇 3 的 `SQLITE_READONLY` 是 Windows + WAL 下的行为，
在 Linux runner 上**根本复现不了**。若为省钱改用 ubuntu，等于把这条红灯永久藏起来。

#### ② `.github/workflows/guard-consistency.yml` —— 一致性护栏（秒级）

**触发**：`pull_request` + `paths` 过滤（只在这些文件变化时跑，秒级反馈）

```yaml
on:
  pull_request:
    paths:
      - "runtime/**"
      - "benchmark/**"
      - "integrations/**"
      - "skills/**"
      - ".ci/**"
jobs:
  consistency:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      # 无需 DB / 无需模型，纯静态与纯逻辑
      - run: python -m pytest -q
             tests/test_tool_roster_consistency.py
             tests/test_capability_introspection.py
             tests/test_mcp_bridge_policy.py
             tests/test_conversation_workflow.py
      # pre-commit 门禁一致性（改动文件 → 选测子集 + 已知失败基线比对）
      - run: git diff --name-only origin/${{ github.base_ref }} > changed.txt
      - run: python scripts/ci_gate.py --stdin < changed.txt
```

**这一份是簇 2 的直接止血位**：名册漂移是**静态可检**的，1 秒级就能挡住，不需要等 11 分钟的全量。

#### ③ `.github/workflows/nightly.yml` —— 全量 + 验收 + 性能

```yaml
on:
  schedule: [{ cron: "0 18 * * *" }]
  workflow_dispatch:
jobs:
  full:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      - run: python -m pytest tests -q --junitxml=var/test-reports/nightly.xml
      - run: python -m pytest tests/test_concurrency_stress.py -q   # 压力用例单独跑
```

夜间跑全量（无缓存、无分片），并把 `test_concurrency_stress.py` 单独跑一遍
—— 簇 3 是**并发压力**问题，共享 CI 机器上的负载条件与本地不同，
**压力用例在并发 job 里跑才有意义**（这也是 4 分片方案里它的归属理由）。

### 5.3 `.ci/known_failures.txt` 的使用纪律

现有机制（`scripts/ci_gate.py:57-81`）设计得很好，有两条值得保留的硬规则：
**必须写 `# 原因`，否则 `load_baseline()` 直接 `SystemExit`**（`:78-81`）；
**已注册工具的名字必须从基线移除**（`test_tool_roster_consistency.py:303-304` 会反过来查）。

**本轮建议的基线内容（严格执行，不许膨胀）**：

| 应否登记 | 条目 | 理由 |
|---|---|---|
| ❌ **不登记** | 簇 1 的 15 条 | **能修**（P0-1 三行改动即全绿）。基线规则第 2 条明确禁止。 |
| ❌ **不登记** | 簇 2/4 的 2 条 | **能修**（P1-1/P1-2，各 3 行） |
| ❌ **不登记** | 簇 3 的 1 条 | **能修**（P0-4） |
| ⚠️ **可登记（唯一候选）** | 简报中我未归因的 4 条 | 需 P2 先查明真因；确认与迁移无关后方可登记，并写明「原因 / 跟进人 / 计划修的时间」 |

> **基线的正确定义是"已确认并排期的债务"，不是"当前测不动的借口"。**
> 现在基线写「无，全绿」而实际有 18 条红灯 —— 这比"有 18 条登记在案"危险得多，
> 因为它让**任何人**都可以相信"当前是全绿的"。

### 5.4 落地顺序（CI 依赖 P0）

```
P0-1（配置去 import 副作用）  ── 必须先做
   ↓ 否则 CI 上 .env 不存在，APPROVAL 默认 on，与开发者本机行为不一致，门禁不可信
P0-3（断言自检）              ── 让 15 条断言重新具备鉴别力
   ↓
P2-1 ① ci.yml               ── 主门禁
P2-1 ② guard-consistency.yml ── 秒级止血（簇 2 后续改动不再复发）
P0-4（S 层韧性）→ P1-3（审计写去同步）── 簇 3
   ↓
P2-1 ③ nightly.yml
```

---

## 6. Laya 接回方案（主攻路线 continues）

> **管家节点铁律：Laya 是主攻路线，永不放弃。** 本节给出把它接回主干的**架构解法**。
> 现状事实（已核实）：`laya/` 仅剩 `_laya_inspect/laya-main/`（离线 fork 源码，含
> `laya/`、`research/`、`notebooks/`、`BENCHMARKS.md`、完整 `pyproject.toml`）与 14 个已删模块的 `.pyc`；
> `runtime/` + `agent.py` + `main.py` 对 laya 的引用数 = **0**（`grep -rln 'laya'` 命中 0 个 .py 文件）。

### 6.1 定位：不是重建 pipeline，是挂一个可插拔决策头

迁移退役的是**关键词 Tool Router**（`runtime/tool_router.py`）与 **Laya 独立 pipeline**（`runtime/pipeline.py`、`runtime/orchestrator.py`，共 778 行）。
这两者与 Laya 的**模型资产**（`_laya_inspect/laya-main`）是三件不同的事。

| 被退役的 | 该不该回来 | 理由 |
|---|---|---|
| 关键词 Tool Router（`tool_router.py`） | **不该** | 规则做语义判断，违反 `AGENTS.md` 分工宪法。已被 LLM 选择器正确取代。 |
| Laya 独立 pipeline（`pipeline.py`/`orchestrator.py`） | **不该** | 与 LangGraph 图重复，且绕过 Runtime 的授权链。 |
| **Laya 路由头（route head）模型资产** | **该，且是主攻方向** | 它是**已训练好的推理加速器**，不是规则。 |

**架构定位**：Laya 路由头是**挂在 LangGraph 工作流上的可插拔路由头/决策节点**，
与现有筛选逻辑**并列、可切换**，不重建旧 pipeline。

### 6.2 挂载点

**挂在 `select_tools` 之前，作为独立前置节点**，与现有节点并列（不改现有节点语义）：

```
START → analyze → [route_hint] → select_tools → plan → execute → review → END
                          │
                          └─ Laya 可用且命中 → 给 select_tools 注入"族级先验"（hint）
                          └─ 不可用/超时/低置信 → 返回 None，链路与今天完全一致
```

对应 `runtime/conversation_workflow.py:171-179` 的节点注册，加一个可选 `route_hint` handler；
`runtime/runner.py:4397-4398` 的 `build_conversation_workflow(...)` 多传一个参数。
**`route_hint=None` 时图结构与行为逐字节等价于现状** —— 这是本方案的回退安全前提。

### 6.3 输入输出契约（可编码）

新增**单文件** `runtime/laya_route_head.py`（不引入包依赖，不进主干的重逻辑）：

```python
"""Laya 路由头：LangGraph 前置的可插拔决策节点。

契约（Runtime 与 LangGraph 之间的唯一接口）：
  - 只产出「族级先验 + 置信度」，**绝不产出工具名授权**；
  - 任何异常/超时/低置信 → 返回 None，链路退化为现状；
  - 不读写模型档位（那是 runtime/router.py 的职责）。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol

#: 允许的运行模式；三档渐进，默认 off（零行为变化）
#: off    → 节点恒返回 None
#: shadow → 计算并记录与 LangGraph 选择的一致率，**不施加影响**
#: on     → 注入先验
MODE_ENV = "FORGE_LAYA_ROUTE_MODE"
CONFIDENCE_ENV = "FORGE_LAYA_ROUTE_CONF"      # 默认 0.90
BACKEND_ENV = "FORGE_LAYA_ROUTE_BACKEND"      # gguf | torch


@dataclass(frozen=True)
class RouteHint:
    """路由头输出。**不是授权，是先验。**"""
    family: str            # 工具族，如 "coding" / "web" / "knowledge" / "chat"
    confidence: float      # [0,1]；低于阈值由调用方丢弃
    backend: str           # 实际使用的后端，供观测
    latency_ms: int        # 墙钟成本，进 V 层指标


class RouteHead(Protocol):
    def available(self) -> bool: ...
    def hint(self, request: str, *, families: list[str]) -> RouteHint | None: ...


def laya_route_head() -> RouteHead | None:
    """进程级单例；不可用时返回 None（调用方据此跳过节点）。"""


def route_hint_node_factory(runtime_handler: Any) -> Any:
    """生成 LangGraph 节点 handler：analyze → route_hint 的边由图定义。"""
```

**关键设计约束（三条都不可让步）**

1. **只给族级先验，永不给工具名。**
   理由：一旦 Laya 能输出工具名，它就成了第二个授权源，
   `LANGGRAPH_TASK_PLAN_BOUNDARY.md:5` 声明的"Runtime 独占授权"边界立刻失守。
2. **`available() == False` 必须让整条链路等价于今天。**
   这让 Laya 可以**长期停在 shadow 档持续积累证据**，而生产风险为零。
3. **不碰 `runtime/router.py`。** `router.py:68-79` 的 `route_profile` 只看
   `model_profile` / `channel` / `deep` 三个元数据字段，与工具族无关，两者零重叠。

### 6.4 与旧 `LayaRouter` 的关系：复活**接口**，不复活实现

被退役的 `runtime/laya_router.py`（473 行）里，**接口形状恰好是对的**：

| 旧接口（`git show aa05c1f~1:runtime/laya_router.py`） | 处置 |
|---|---|
| `laya_available()` `:59` / `laya_router_enabled()` `:67` | ✅ 直接复用为 `available()` |
| `laya_router_backend()` `:78` | ✅ 复用为 backend 抽象（gguf/torch 切换） |
| `LayaDecision` + `LayaDecision.fallback()` `:121,132` | ✅ 复用为 `RouteHint` 的降级形态 |
| `LayaRouter.screen(query)` `:257` | ✅ 复用为**前置快速筛**（只判族，不判工具） |
| `LayaRouter.route_candidate(query, candidates)` `:414` | ✅ **这是最值得保留的形状**：对**已筛选候选集**做重排序。它是纯打分函数，不扩权。 |
| `classify()` `:366` 里的关键词/正则匹配逻辑 | ❌ 不复活（宪法违规） |
| 单例 + `_reset_singleton_for_test()` `:96,104` | ✅ 保留（可测性） |

**一句话**：复活"打分与降级"的接口；不复用"匹配与授权"的旧实现。
`route_candidate` 的形状天然契合新边界 —— **Laya 排序，Runtime 授权**。

### 6.5 分阶段落地（每阶段都可独立验收、可停在任意阶段）

| 阶段 | 交付物 | 位置（遵守 `AGENTS.md`） | 验收方式 |
|---|---|---|---|
| **L0 资产盘点**（1 天） | `_laya_inspect/laya-main` 的 checkpoint/接口/输入格式清点；确认 GGUF 权重是否在库 | 文档 → `docs/architecture/` | 一份能回答"还差哪些权重才能跑起来"的清单 |
| **L1 离线评测台** | 路由头离线准确率/延迟基线 | 代码 → `benchmark/laya_route_head/`（带 topic README）；产物 → `var/benchmark-runs/laya-route-head/<run-id>/` | 与 LangGraph 选择做一致率/召回对比，出具基线 JSON |
| **L2 shadow 接入** | `FORGE_LAYA_ROUTE_MODE=shadow`：只算不施加，事件里记 `laya_family` / `laya_conf` / `laya_agreement` | `runtime/laya_route_head.py` + `runner.py:3718-3735` 事件体加字段 | shadow 跑满 N 轮后，一致率与置信度分布有量化结论 |
| **L3 灰度 on** | `FORGE_LAYA_ROUTE_MODE=on` + 熔断：一致率低于阈值自动降回 shadow | 同上 + `FORGE_LAYA_ROUTE_CONF` | 熔断路径有测试覆盖（掉线/超时/低置信/后端不可用四种） |
| **L4 成本兑现** | 筛选阶段 token/墙钟对比，兑现"更快更省" | V 层事件 | 有 before/after 量化数据支撑保留或调整档位 |

### 6.6 与本次修复的关系

Laya 接回**不依赖**本文档任何 P0/P1 修复，可与红灯清理**并行推进**。
但 L2 依赖 P1-3（事件体字段扩展）与 P2-4（成本埋点）—— 这两项在 §4 已排期。

---

## 7. 对委托简报的事实更正

| # | 简报表述 | 核实结果 | 证据 |
|---|---|---|---|
| 1 | 「删除 `benchmark/eval_runner.py`」 | ❌ **文件仍存在**。`aa05c1f` 只删了其中 47 行与已退役 tool_router 耦合的 `_apply_fixed_tools` 辅助函数 | `ls benchmark/eval_runner.py` 存在；`git show aa05c1f -- benchmark/eval_runner.py` 只显示一个 hunk 的删除 |
| 2 | 「删除整个 `annotation/` 工具链」 | ✅ 属实 | `git ls-files annotation/` = 0；目录仅剩 `__pycache__` |
| 3 | 「`.github/workflows/` 整个目录已不存在」 | ✅ 属实 | `ls .github` → No such file or directory |
| 4 | 「主干对 laya 引用数 0」 | ✅ 属实 | `grep -rln 'laya' --include=*.py runtime/ agent.py main.py` → 0 命中 |
| 5 | 「22 failed」 | ⚠️ **我逐簇实证到 18 条**（15 + 2 + 1），差 4 条未归因 | 见下方 unknown #1 |
| 6 | 「簇 1 = 护栏失效」 | ❌ **成因改写**（护栏是被本机未跟踪 `.env` 关闭的，非迁移打断）；严重级仍为 P0 | §3.5 |

---

## 8. 「默认假设（可推翻）」清单

> 以下是我为了让分析推进而做的假设。每条都标注了**推翻它的方法**。
> 标 🔴 的是如果错了会改变结论方向的。

| # | 假设 | 依据 | 推翻方法 |
|---|---|---|---|
| 1 | 🟡 简报里未归因的 **4 条失败与本次迁移无关** | 我只逐簇验证了 18 条；全量 11 分钟未重跑（受委托约束） | 跑一次 `PYTHONPATH= ./.venv/Scripts/python.exe -m pytest tests -q --junitxml=var/test-reports/review.xml`，比对 22 与 18 的差集。**P2 第一件事** |
| 2 | 🔴 **`.env:86 APPROVAL=off` 是本地开发有意为之**（不是误提交） | 文件未被 git 跟踪（`.gitignore:2`），mtime 2026-10-01 17:01 = 迁移当天 | 问部署者。若它是**误操作**，则 P0-2 的紧迫性从 P0 升到"立即" |
| 3 | 🟡 本机 `SQLITE_READONLY` 在其他平台不复现 | 已知为 Windows+WAL 行为；`ci.yml` 固定 `windows-latest` | 在 ubuntu runner 上跑 `test_concurrency_stress.py` 对比 |
| 4 | 🟡 `routing.selection.pending` 事件**无下游消费者** | 全仓 grep 未见读取方 | `grep -rn "routing.selection.pending" --include=*.py`（我查过=1 处写入 0 处读取）；P1-3 合并前请复核 TUI/benchmark 是否消费 |
| 5 | 🟡 `laya/_laya_inspect/laya-main` 是**当前可用的 Laya 资产** | 文件完整（含 `pyproject.toml`/`research/`/`tests/`） | 跑一次 L0 盘点，确认 checkpoint 权重是否齐备 |
| 6 | 🟡 `AGENTS.md` 的三分工宪法对**所有业务语义判断**有约束力，不只对新增代码 | `AGENTS.md` 无"仅新增适用"的限定 | 若管家节点确认只约束新增代码，则 P1-6/P1-7 降为 P2 |
| 7 | 🟡 `tests/test_concurrency_stress.py::ConcurrentRunStressTests` 只有 2 条用例 | 我跑出 `2 passed`；grep 只找到 1 个 `test_mixed_*` | `pytest --collect-only -q tests/test_concurrency_stress.py` |
| 8 | 🟡 生产部署的 `.env` 与本机不同（本机 `APPROVAL=off` **不代表**生产也关） | 本机是开发机 | 检查部署环境的 `APPROVAL` 实际取值。**若生产也是 off，那 L-1 就是正在生效的 P0 生产事故** |
| 9 | 🟡 11 分 16 秒的全量耗时在 CI 上大致等比 | 本机 1355 passed | 以 4 分片方案首跑实测为准，可能需调整为 3 或 5 片 |
| 10 | 🟡 仓库根无 `conftest.py` 意味着测试环境完全依赖 `.env`（这正是 P0-1 的根因） | `find . -maxdepth 2 -name conftest.py` 无结果 | 若上层目录有 `conftest.py` 注入 env，结论需修正 |

---

## 9. 拟入库知识条目（交主理人经 wiki-creator 编译）

> 均为**跨项目可复用**的方法论/模式，不含本项目私有信息。

| # | 条目 | 归类 | 防污染自检 |
|---|---|---|---|
| K-1 | **「测试全红」不等于「架构回归」**：迁移复审的第一步必须做「失败文件在迁移前后的字节级一致性校验」（`git show <pre>:<file> \| md5sum`），再判定因果。本例 4 个文件字节一致，直接排除了迁移因果。 | 架构复审方法 | ✅ 无项目私有信息 |
| K-2 | **配置在 import 期加载 = 隐藏的全局单例**：任何 `load_dotenv()` 出现在模块顶层，都会让进程环境依赖 import 顺序，并使所有读取该 env 的模块行为不可测。正确形态是"入口显式加载 + 模块只读"。 | 跨领域模式（配置治理） | ✅ |
| K-3 | **护栏开关必须有强制生效点**：再严密派生（名册/metadata/白名单）的护栏，只要总开关是"一个可被未跟踪文件改写的 env"，派生逻辑就等于不存在。护栏需要启动期断言 + 审计事件 + 可 fail-closed 的生产 profile。 | 跨领域模式（安全护栏） | ✅ |
| K-4 | **test vacuity 检测**：断言可能因"被测分支不可达"而静默失去鉴别力。给安全相关断言加"前置自检（先断言开关处于开启态）"，把"空转"变成"明确失败"。 | 跨领域模式（测试有效性） | ✅ |
| K-5 | **审计写与业务写失败语义必须一致**：同一次迁移里，审计事件 best-effort（失败仅 WARNING）而业务写 fatal，会造成"数据库一抖，Run 失败但决策链也缺记录"的双输。 | 跨领域模式（可观测性） | ✅ |
| K-6 | **状态层韧性是编排层写放大的缓冲垫**：编排层每 Run 多写一次事件，若状态层"每操作新建连接 + 无重试"，就会在 WAL 并发下退化为 `SQLITE_READONLY`。**排查此类问题必须做"中和单一变量"的对照实验，而非相关性推断。** | 跨领域模式（状态存储） | ✅ |
| K-7 | **退役架构时，把"规则"和"资产"分开**：被退役的关键词路由（规则，违反宪法）与已训练模型权重（资产，值得保留）常在同一目录。正确处置是复活**接口与打分形状**，不复用**匹配与授权的旧实现**。 | 跨领域模式（架构迁移） | ✅ |
| K-8 | **正则堆叠是"设计放错层级"的症状，不是纪律滑坡**：当同一个"用正则做业务语义判断"的文件被反复要求加同义句，说明它整体放错了层。修复应升层（交给 LLM/结构化判定），而不是继续加词。 | 跨领域模式（AI 系统设计） | ✅ |

---

## 10. 遗留问题与未知区

1. **4 条未归因失败**（见 §8 #1）—— P2 必须先补齐，否则修复优先级表的分母不对。
2. **生产环境的 `APPROVAL` 实际取值未知**（见 §8 #8）—— 这决定 L-1 是"开发环境问题"还是"正在生效的生产事故"。
3. **`infer_task_plan` 的规则引擎与 LangGraph 选择器的职责边界，尚未有正式设计裁决**（P1-7）。我给了方向（Laya/plan 排序，Runtime 授权），但**未评估把 `task_plan.py` 整体 LLM 化的风险** —— 那是 P3 设计议题，不应挤进红灯清理。
4. **`routing.selection.pending` 是否真无下游消费者**，未穷尽核查（P1-3 合并前必须复核）。
5. **迁移的决策成本收益未量化**（P2-4）—— 「32→8~16 工具」的说法目前**没有任何度量支撑**，我既不能证实也不能证伪。
6. **CI 分片数（4）未经 CI 实测**（见 §8 #9）。

---

## 附：本次复审未做的事（明确划界）

- ❌ **未改任何生产代码**（遵委托约束；插桩文件 `var/tmp/probe_*.py` 已全部清理）
- ❌ **未跑全量测试**（遵委托约束；只跑 4 次聚焦测试，全部在本文档中可复现）
- ❌ **未对 Laya 模型资产本身做质量判断**（只给了架构接回方案；模型好坏是 L0/L1 的事）
- ❌ **未评估 `task_plan.py` 整体 LLM 化的风险**（见遗留 #3）

---

## 11. 勘误表（P2/P3 实测更正）

> 本节由主理人在 P2 编码 + P3 独立验证完成后追加。**原文不改**，以存"当时基于有限证据做出判断"的审计痕迹。
> 每条都给出：原文怎么说 → 实测是什么 → 谁推翻的 → 现在该怎么读原文。
>
> **这 6 条里有 5 条是"落点/数字错"，1 条是"方向错"。方向错的那条（簇 3）最值得看** ——
> 它说明"压力测试间歇性失败"很容易被归因到表面现象，而真因在更底层。

### E1 ★ 簇 3 真因方向错：不是「WAL 退化」，是「连接 churn」

| | 内容 |
|---|---|
| **原文** | §3.4 / §4 P0-4：真因是「编排层每 Run 同步写库 → WAL 并发 → 退化为 `SQLITE_READONLY`」；修法建议为**加 3 次指数退避重试** |
| **实测** | 按原文加退避重试后，压力测试**仍间歇红**（run#1 failed，run#2/3 passed），且失败点**漂移**到未包装的 `create_task` / `transition`。真因是 `asyncio.to_thread` 把调用散到线程池，**跨线程并发开闭连接**；最后一个连接关闭时回放/删除 `-shm` 与另一连接的映射冲突 |
| **正确修法** | 按 `db_path` 复用 **RLock** 串行化连接生命周期。实测 9 轮 × 32 线程混合读写 **0 错误** |
| **推翻者** | 编码工程师（P2 第 4 步） |
| **怎么读原文** | §3.4 与 P0-4 的「WAL 并发」因果链**不成立**。K-6 那条知识（状态层韧性是编排层写放大的缓冲垫）方向对，但机制是连接 churn 而非 WAL |
| **附带** | 原文建议的扩展码 `READONLY_DBMOVED` / `RECOVERY` 收窄是**死代码**：Python 3.11 `sqlite3` 只暴露主码，实测权限只读时 `sqlite_errorcode` 仍是 `8`。只能对 `code==8` 做有界重试（≤3 次共 0.15s），真权限只读会在 0.15s 后原样抛出 |

### E2 ★ 簇 1 真凶定位不充分集

| | 内容 |
|---|---|
| **原文** | §3.5 证据二②：真凶是 `runtime/compact.py:25`、`runtime/codex_loop.py:21`、`runtime/reply_parser.py:27` 三处 import 期 `load_dotenv` |
| **实测** | 那 3 处确实是问题，但**不是充分集**。逐模块污染实测发现 `runtime_paths.py:16-20` 的 `load_dotenv(PROJECT_ROOT/".env", override=False)` 被**所有**模块传递导入 —— `runtime_paths` / `code_exec` / `tools` / `skills_loader` / `rag` / `project_edit` / `integrations.*` 全部 POLLUTES。**拆掉那 3 处后 `import runtime.compact` 仍把 `APPROVAL` 喂进进程** |
| **为什么原文的反证实验没暴露这点** | §3.5 证据三用 `APPROVAL=on pytest`（**环境变量前缀**）做反证，恰好**掩盖**了 import 期污染这个事实 |
| **推翻者** | 编码工程师（P2 第 1 步实测） |
| **本轮处置** | `runtime_paths` **有意未改**（P3 判定 `override=False` 使真实 env 恒胜，**无法绕过** P0-2 新增的启动护栏，危害降为 P2 认知风险）。P0-1 只修 3 处是**有意的分层取舍**，不是漏改 |

### E3 P0-3 落点数量错：2 处 → 13 处

原文写「`tests/test_approval_lifecycle.py:23-29`、`tests/test_approval_execution_parity.py:49-54`（setUp 增加…）」共 2 处。实测**会空转的构造点共 13 个**（lifecycle 5：3 个 setUp + 2 个用例内联构造 `rt = AgentRuntime(...)`；parity 8 个 setUp）。**照原文只改 2 处会漏掉 11 条同样静默空转的用例。**

### E4 引用了不存在的文件

原文 P0-4 的「回归风险」列写「必须配套跑 `tests/test_task_manager.py`」。**该文件不存在**（collect 0 项）。实际覆盖 TaskManager 的测试需自行 grep 识别（编码工程师识别出 10 个 / P3 复核实为 **46 个文件触及 DB**，补跑 10 个 → 129 passed）。

### E5 测试条数错：21 条 → 13 条

原文 P1-6 验收列写「`tests/test_capability_introspection.py`（全 21 条）」。该文件实为 **13 条**。

### E6 红灯条数错：15 条 → 19 条

原文多处写簇 1 是「15 条」。全量 junit 逐条解析的真值是 **19 条**（parity 11 + lifecycle 7 + `test_api_layer_phase_b::ApprovedEvidenceAttributionTests` 1）。P2 收尾时已按实测数字重写 `known_failures.txt`。

> 顺带更正主理人自己的一次转述失误：§10 遗留 #1 写「4 条未归因」，那是**主理人把 P1 报告的正则漏匹配当成事实**转述给编码工程师所致。真实情况是 **22 条全部有归属**（19 审批 + 名册 1 + 能力自省 1 + 只读库 1）。

### 本报告的元教训

**跨阶段传递的结论必须带「落点完备性」核验，不能只带「方向正确性」。** 本报告 6 处错误里有 5 处是「方向对但落点/数字错」—— 主理人据此下发指令时，编码工程师若照单执行就会漏 11 条断言。这条已作为知识条目 K-10 入库 wiki。
