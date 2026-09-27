# Agent 结构、工作流、API 与 TUI 审计

- 日期：2026-09-27
- 范围：当前工作区静态代码审计
- 结论类型：代码证据审计；未运行测试、未启动 Agent、未调用模型/provider/MCP、未做网络请求
- 代码基线：审计开始时 HEAD `12fd8a9`；产品代码未修改

## 摘要

当前主要对话入口基本汇入 `AgentRuntime.run_turn()`，由 Runtime 负责任务状态、运行上下文和 Agent 执行。但系统仍有多个入口与生命周期：CLI、Textual TUI、Web/OpenAI 兼容 API、Runtime 运维 CLI 和调度进程。它们并非完全等价：Web 的 `/tools` 可直接触发 Broker；TUI 对取消和并发提交的状态反馈与 Runtime 能力脱节。API 还存在一个确定的非流式响应队列错误。

建议先处理 **P1**：非流式 chat completion、远程 API 鉴权、TUI 真取消和运行中重复提交。接着收敛直接工具执行入口和 API 协议语义，再处理缓存上限、配置路径与测试覆盖。

## 发现

### P1 — 非流式 `/v1/chat/completions` 把首个活动事件当最终结果

**证据**：`runtime/runner.py:2711-2722` 创建 `RunActivityProjector` 并发出 `run.started`；`llama_bridge.py:470-476` 把所有回调事件放入队列；`llama_bridge.py:485-496` 只有在整轮结束后才把 `__end__` 放入同一队列；但非流式分支 `llama_bridge.py:768-775` 只取一次 `queue.get_nowait()`，把该首事件作为 `end` 并清空剩余队列。

**影响**：普通非流式请求会把 `run.started` 当完成事件，输出空内容/错误元数据；真正答案随后被丢弃。该问题不影响采用独立 SSE pump 的 `stream=true` 路径。

**建议**：非流式分支持续消费直到 `__end__` 或 `__error__`；活动事件应忽略或单独收集。增加路由级用例覆盖 `run.started → assistant_delta → __end__`、审批和异常场景。

### P1 — 非回环服务 opt-in 后没有应用层认证/授权

**证据**：`webapp.py:179-195` 非回环监听要求设置 `FORGE_ALLOW_NONLOCAL_UI=1`，但设置后只输出“确认外层已有鉴权”的警告并启动服务；路由注册 `llama_bridge.py:900-930` 没有认证中间件。`llama_bridge.py:168-193` 的 `user_id`、`X-Forge-User`、Bearer 字符串用于 session 标识，调用者可以自行指定；不是 token 验证。执行面包括 `/tools`（`llama_bridge.py:385-430`）、完整 Agent turn（`751` 起）及控制/取消路由（`864` 起）。

**触发条件**：操作者显式在非回环地址运行并开启 opt-in，且反向代理没有正确鉴权或可被绕过。默认回环监听不构成互联网暴露。

**影响**：可达的未认证客户端可能触发工具副作用、消耗模型资源或影响运行控制；伪造用户标识也不能形成可靠的会话隔离边界。

**建议**：对非回环启动实施服务端认证与授权，验证身份而非接受身份标签；绑定会话、run 和取消操作的所有权。若依赖反代，应提供强制校验/部署检查，不能只打印警告。

### P1 — TUI 的 Escape 只显示中断提示，没有取消 Run

**证据**：`cli/tui/app.py:1505-1513` 将 Escape 转交 `_on_key_press`；`1628-1635` 在 running 状态仅写入“已发送中断请求…”。没有调用 Runtime 的 `cancel_run()`。Runtime 已实现 `AgentRuntime.cancel_run()`，Web 控制路由会使用它。

**影响**：用户会以为运行已停止，实际模型/工具仍可能继续，副作用也可能继续发生。`KeyboardInterrupt` 异常处理在 TUI 中也明确提示“后端 Run 可能仍在执行”（`cli/tui/app.py:1052-1058`）。

**建议**：保存当前 run_id 并调用 `cancel_run(run_id)`；只有收到 Runtime 取消确认/终态事件后才显示“已取消”，超时则明确显示仍在运行或取消状态未知。

### P1/P2 — TUI 运行中允许再次提交，第二次失败会覆盖第一次的界面状态

**证据**：`cli/tui/app.py:855-896` 普通输入无 running 检查；`995-1017` 每次提交都会清空共享流/工具状态并以 `exclusive=False` 创建 worker。Runtime 同一容器只允许一个活跃 Run（`runtime/runner.py:2517-2522`）；第二次运行抛错后 TUI 通用异常分支会设置 header 为 `failed` 并停止 spinner（`cli/tui/app.py:1060-1065`），但第一个 Run 仍可能继续。

**影响**：第一次回答流可能丢失/错位，状态栏报告失败但后台仍在工作；用户容易重复提交或误判副作用状态。

**建议**：运行期间禁用输入或将新消息排队；捕获“容器已有活跃 Run”作为可恢复 UI 提示；流与工具渲染状态按 run_id 隔离。增加快速双提交与首个 Run 仍运行时拒绝第二个 Run 的交互验收。

### P2 — `/tools` 直调没有完整 Run 上下文和持久执行证据

**证据**：`llama_bridge.py:385-430` 取得 Runtime 后直接 `rt.broker.execute(tool, params)`，没有调用 `run_turn()`，也没有创建 Task/Run。Broker 构造的 SDK `ToolContext` 使用 `context=None`（`runtime/broker.py:47-53`）。Runner 中大量门控明确只在 `rctx is not None` 时生效，例如用户约束/补信息、Run 级工具预算、文件范围（`runtime/runner.py:455-485`、`714-737`、`746-782`）；副作用写前账本也依赖 `run_rid`（`1122-1153`）。

**影响**：直接工具请求不具备常规 Run 的任务状态、运行证据和 Run 级策略上下文；目前 API 层单独检查了 ApprovalGate，但这不能替代整个 Run 契约。其余外围能力是否由具体工具自身覆盖需逐工具确认。

**建议**：优先将 `/tools` 改为经过 Runtime 建立的专用受控 Run/Tool Invocation；若必须保留直调，使用明确的工具 allowlist，并建立 invocation ID、任务/审计事件、文件范围及审批一致性，不要把它描述为与 Agent 工具执行等价。

### P2 — 请求 `model` 字段被回显，但没有用于选择实际模型

**证据**：`llama_bridge.py:761` 读取请求 model，`463-465` 将其用于 session ID；`485-493` 调用 `rt.run_turn()` 时没有传 model；Runtime 的方法签名 `runtime/runner.py:2388-2405` 无 model 参数，模型由进程配置决定。响应仍在 `llama_bridge.py:783-788` 回显请求值。

**影响**：客户端指定模型时可能得到另一模型的答案，却收到请求模型标签；不同 model 值还会拆分 session 历史。

**建议**：要么把字段路由到受允许列表约束的实际 Provider/model，并报告有效模型；要么拒绝不匹配值，或统一回显实际配置模型。

### P2 — usage 指标不是 token 数

**证据**：`llama_bridge.py:795` 固定返回 `prompt_tokens=0`，并用 Python 字符数 `len(content)` 填充 completion/total tokens。

**影响**：OpenAI 兼容客户端的用量、成本统计和限额决策会失真；中文尤其会出现字符数与 token 数偏差。

**建议**：从 Provider/Run usage 读取实际 token 计数；不可用时返回明确的未知/不支持状态，不伪装成精确 token 数据。

### P2 — Stream 恢复缓存没有字节上限，且可能淘汰活跃流/覆盖同会话流

**证据**：`llama_bridge.py:535-537` 限制条目数为 64，但 `ConvStream.publish()` 对 bytearray 持续追加且没有每流/全局字节上限（`561-565`）。超量清理先删已完成条目，仍超量时会按最旧时间直接删条目，包含活跃流（`622-638`）。相同 key 新开流直接覆盖旧流（`644-652`）。缓存只在当前进程内。

**影响**：慢客户端/超长输出可能占用持续增长的内存；大量并发时活跃连接续读会丢失；同一会话并发请求互相覆盖；多 worker 部署时续读请求可能落到没有该缓存的进程。

**建议**：限制每条与全局缓冲字节数，活跃流与已完成流采用不同淘汰策略，对同会话并发定义拒绝/排队/独立 stream ID 语义；多 worker 场景改用共享存储或强制粘性路由并文档化。

### P2 — TUI API 配置写入位置依赖启动时当前目录

**证据**：`cli/tui/app.py:767-796` 将设置写入 `os.path.join(os.getcwd(), ".env")`。项目配置则按项目根目录加载，例如 `main.py:20,35` 和 `agent.py:8,60` 使用 `BASE_DIR / ".env"`。

**影响**：从仓库目录以外启动 TUI 时，配置可能写入错误位置；当前进程因环境变量已更新而看似生效，重启后却读不到刚保存的配置，或在无关目录创建 `.env`。

**建议**：写入 `PROJECT_ROOT/.env` 或由配置层统一管理；写后报告实际路径并验证重新加载后的有效值（不得展示密钥）。

### P3 — 模型加载/卸载兼容端点返回成功，但只是状态桩

**证据**：`llama_bridge.py:286-293` 的 load/unload handler 返回成功形状及 stub 说明，没有执行模型生命周期操作。

**影响**：若调用方只依据 success 字段，会把兼容占位误判为成功加载/卸载。

**建议**：返回明确的 `unsupported`/`stub` 状态或 HTTP 501；若前端依赖 success，则确保 UI 同时显示不可用语义。

## 架构与工作流观察

- CLI 与 TUI 通过 `ChatApp` 调用 `AgentRuntime.run_turn()`；OpenAI chat 接口的流式路径也调用它。因此核心完整对话执行链已集中到 Runtime。
- `main.py` 的 SDK 执行辅助函数仍被 Runtime 调用（`runtime/runner.py` 调用 `execute_turn`，约 `3354` 行），所以不是一条闲置的第二生产链；但它让 Runtime 的边界仍依赖顶层 CLI 模块。建议后续把 Provider/SDK 执行原语迁入明确的 Runtime/Provider 模块，避免反向依赖。
- `runtime/__main__.py` 是诊断/任务运维入口，不是独立对话生产链。
- `webapp.py` 虽标为冻结 UI，仍会注册 llama_bridge 的活跃 API；“UI 冻结”不等于接口停止维护或不暴露。
- `/tools` 是与 `run_turn()` 不同的副作用入口，是目前状态契约与执行证据最明显的边界例外。

## 测试与验收覆盖缺口

本次没有运行测试。静态搜索发现已有 API 与 TUI 单测，但未看到覆盖以下缺陷的路由/交互测试：

- 非流式响应消费完整 Runtime 事件队列。
- 非回环 opt-in 下实际拒绝未认证请求，以及 run/session 所有权检查。
- TUI Escape 调用真实 `cancel_run()` 并等到取消终态。
- 正在运行时双提交不会重置首个 Run 的显示状态。
- stream 缓存字节上限、活跃流淘汰、同会话并发及多 worker 恢复语义。
- API request model 与 response usage 的真实契约。

建议修复 P1 后用隔离 Runtime 和本机测试客户端做动态验收；真实模型与外部 MCP 验收应单独标记，避免和路由/状态的确定性回归测试混在一起。

## 优先修复顺序

1. 修复非流式 API 队列终止条件并增加完整路由测试。
2. 为非回环监听建立可执行的认证、授权和 run 所有权策略。
3. 让 TUI 取消接入 Runtime，并锁定/排队运行期间的新输入。
4. 统一 `/tools` 和完整 Run 的安全门、任务状态与副作用证据契约。
5. 修正 model/usage API 语义。
6. 为 stream 缓存加字节预算与稳定 stream identity。
7. 修正 TUI `.env` 目标路径，补充模型桩端点契约。
8. 把顶层模块的 SDK 执行依赖从 `main.py` 下沉到 Runtime/Provider 层。
