# 《FORGE-Sources 深度 RAG 实施与测试报告》

> 日期：2026-09-06 · 基线：Runtime Stable Baseline（P0=0/P1=0，431 项离线测试）
> 原则遵守：RAG 只是 Context Builder 的一个上下文来源；不重做 Agent Loop/权限/信任/记忆/Compact/Audit；不建第二套 Context/权限/Trust 系统；不引入外部 Vector DB/Worker；检索不绕过 FileScope。

---

## 0. 第一原则落实

- 检索产物 = “Retrieved Context” → 追加进现有 `runner.run_turn` 的 instructions 注入（与 Project Context/附件同一注入点），走现有 Agent Loop / Completion / Verification / Audit；
- 文件边界：Source 在添加时复制进 `forge_data/projects/{pid}/sources`（既有语义），索引/检索只操作复制件与 agent.db，从不直接读未授权原路径；`search_sources` 仅能在 RunContext 的 Project 内工作；
- Trust：自动检索块与显式工具返回均经 `runtime.trust.tag` 包裹为“数据而非指令”；不升级为 system/developer 指令。

## 1. 审计结论（真实代码，非文件名推测）

| 能力 | 现状 |
|---|---|
| Source 数据模型 | 有（`project_sources`：id/task_id/display_name/stored_path/mime/size/sha256/source_type/parse_status/index_status/metadata_json…） |
| Source API | 有（上传/列表/删除/引用） |
| Message Attachment / Project Source / Artifact 分离 | 有（表+目录分离，本报告未改动） |
| Parser / Indexer / Retriever（Sources 级） | **缺失**（此前只有 rag.py 的“工作区工具检索”与预留字段 index_status=none） |
| RAG 预留字段 | 有（index_status 恒为 none——预留未实现，已证实） |
| Embedding 引擎 | **可复用**（rag.py 的本地 ONNX `OnnxEmbedEngine`/`_embed_query`；models/bge-small-zh-v1.5 已就绪，依赖 onnxruntime/tokenizers 已安装） |
| FTS | 无（新增） |
| Context Builder 自动注入 | 无（新增，接在既有 ctx 注入点后） |
| 前端 Sources UI | 有列表/上传；新增状态列即用现有字段（处理中/可用/失败），不加 RAG 术语 |

重复/死代码：工作区 rag 工具（index_workspace/search_documents）与 Sources 检索是两套不同入口（workspace search vs 参考资料 RAG）——语义不同，保留并存；共享同一 embedding 引擎，无平行向量实现。

## 2. 固定语义

- Workspace File = 当前 Project 可读写的文件（仍受 FileScope/Approval）；Source = Agent 参考资料（默认 READ ONLY）。
- Source 添加 = 复制进项目 Source 存储（删除 Source 记录/索引不删用户磁盘原文件）。
- Memory ≠ Sources：两者只在 Context Builder 汇合。

## 3. 新增真实能力（Sources 包）

```
sources/
├─ schema.py     agent.db 追加表（幂等 ensure，随 TaskManager 初始化）
├─ parser.py     .md/.txt/.py/.js/.ts/.tsx/.jsx/.json/.yaml/.toml/PDF/DOCX
├─ store.py      chunks/FTS(external-content)/vectors 存储与原子替换
├─ indexer.py    parse→chunk→FTS→(可选)本地向量；增量与失败语义；上传后台异步
├─ retriever.py  FTS5(BM25)+余弦向量 → RRF 合并 → 轻量符号精确性 rerank
├─ service.py    Scope/自动检索/预算/格式化/审计载荷
├─ tool.py       search_sources（只读 Agent Tool）
```

### 3.1 Parser
md（标题结构，标题行入块）、代码（class/def/function/export/const= 边界，装饰器归组）、JSON（顶层键）、YAML/TOML（顶层键分块）、PDF（pypdf 分页+标题段；扫描版无文本层→`text_unavailable`，绝不假装 ready）、DOCX（Heading/Paragraph 分组）。解析失败置 `failed+parse_error`。

### 3.2 Chunker
- 结构边界优先；不做固定 N 字符硬切；
- token 估算 `estimate_tokens`（CJK 每字 1，其他按 ~3.5 字符/token），`target=380/max=900`，整段超过上限允许小幅溢出（不截断函数/段落）。

### 3.3 索引与存储
- 表：`source_chunks`（含 cid/heading/section_path/start_line/end_line/page_number/token_count/content_hash/trust_level/index_version）、`source_chunks_fts`（FTS5 external-content）、`source_chunk_vectors`（BLOB float32 + model/dim）；
- 版本化：`INDEX_VERSION=sources-v1` + embedding_model/dim 与 chunk 内容 hash 均记录；Source 未变不重嵌（重建同 sid 原子替换），删除级联清理 chunks+FTS+vectors（manager 容器删除同样清理）；
- 上传后由 webapp 触发 `index_source_async`（无 Worker 系统：asyncio.create_task + to_thread），UI 状态即真实状态。

### 3.4 Hybrid Retrieval
- Lexical：FTS5 BM25（unicode61；查询词含 CJK 段、camelCase/符号拆分，支持 `normalized_args_hash` 精确命中）；
- Semantic：本地 ONNX embedding（复用 rag 引擎；CLS+L2，512 维 bge）；向量不可用自动降级 lexical-only 并记录原因；
- Merge：RRF（k=60，稳定可测）→ 轻量 score-based rerank（精确符号 token 命中加分，无 LLM）；
- 返回结果带全量 provenance：source_id/title/section_path/heading/lines/page/score/token_count；**绝不丢弃来源**；
- 区分 `NO_RESULTS` 与 `RETRIEVAL_FAILED`/`retrieval_unavailable`。

### 3.5 Context 接入与预算
- 自动检索：run 的 Context 注入点，触发条件（Project 有 ready+enabled Sources & 非寒暄 & 消息长度）& 预算（`FORGE_SOURCES_MAX_CONTEXT_CHARS` 默认 6000）→ 独立 "【参考资料 · 自动检索】" 段（每块 `runtime.trust.tag` 包裹），超预算按 score/覆盖截断；
- 显式 `search_sources(query, source_ids?, limit?)` 共用同一 retriever；只读；Scope=RunContext 的 Project（无 Run 上下文直接拒绝）；审计载荷只存 ids/score/count/latency/model，不存全文。

### 3.6 审计事件
每次检索记录 `sources.retrieval`（run_id/container_id/mode/chunk_ids/count/latency_ms/embedding_model/error）——真实 E2E 实证两条 run 各产生一条事件（mode=hybrid、local-onnx、16ms）。

## 4. 数据位置
- 元数据/状态：`project_sources`（既有表+追加列 enabled/text_unavailable/parse_error/indexed_at/index_version）；
- 分块/FTS/向量：agent.db 三张追加表（随现有 Snapshot/Restore 备份）；源文件仍在 `forge_data/projects/{pid}/sources`。

## 5. 测试

### 5.1 语料覆盖（tests/test_sources_rag.py 运行时构造）
中文/英文 md、Python（符号）、DOCX、PDF（含无文本层扫描版→failed+text_unavailable）、同义语义、精确函数名、冲突双文档、Prompt Injection、同 run 重复申请（语义+关键字双查）、更新重索引、删除清理。

### 5.2 关键断言（9 项全绿）
- Hybrid/降级均可命中；“same-run pending approval should be short-circuited”（英文句）与中文整句查询都返回正确 Source；
- `normalized_args_hash` 精确命中且带行号；
- Project A/B 隔离（B 检索 A 资料 = no_results；B chunks=0）；
- no_results vs retrieval_failed 区分；
- 注入文档：内容保留 + 外部数据/非指令声明；
- 更新同 sid 重建=单 chunk 无幽灵；删除后索引清零不可检索；
- 冲突文档双方都在结果里（不合成、不静默删旧）；
- search_sources 无 Run 上下文拒绝/有上下文可用且带 Trust；
- DOCX/PDF（扫描版明确失败语义）。

### 5.3 性能（小样本实测：6 Sources / 6 chunks，本地 ONNX 已加载）
- 索引：单 Source ≈ 128ms（含 embedding）；6 Sources 共 0.77s；
- 检索：15–16ms（hybrid）；db 增量 ≈287KB（6 docs）。个人规模（数百份资料/数千 chunk）预计检索 <100ms、启动不重建（哈希+版本复用）。10/100/5000-chunk 规模化曲线留待容量测试章（P2）。

## 6. 真网关 E2E 结果（含如实说明）

- **Pipeline（真实链路，已验证）**：上传 md → parse/后台索引 → `('ok','ready')`；自动检索真实触发 `sources.retrieval`（mode=hybrid, embedding_model=local-onnx, latency≈16ms）；模型前所有环节在当前真实代码上工作。
- **模型侧（如实）**：当次网关账号出现持续 Provider 故障（503 No available channel…TokenPlan / 401 令牌数据库查询出错），模型调用阶段多次失败；期间一次成功生成的内容为正确的资料落地回答（“同 run…只保留一条 pending…normalized_args_hash 去重键”），但因当时 Completion 判定对“申请审批”中“请+审批”的口头审批误报被拦截——**该误报是本轮真实暴露的判定精度问题，已修复并加回归**（负向 lookbehind `(?<!申)请` + 如需/若/无需假设语气窗口；新增断言：含“申请审批/申请批准/如需人工批准”的良性句子不触发 APPROVAL_INCONSISTENT，“等待你批准…”仍触发）。
- 结论：RAG 管线进入模型的路径与证据完整；模型层的最终完整度受 Provider 可用性影响（与 RAG 无关）。恢复 Provider 后建议补一次“按资料修改/验证”的完整闭环 E2E（登记为 P2 观察项）。

## 7. 全量回归
`tests/run_tests.py`：**Ran 431 tests，OK（skipped=2 主题 CDP）**。新增 9 项 Sources RAG 测试；既有 Approval/FileScope/Trust/Memory/Compact/Completion/Verification/Session/Snapshot/并发 全部保持通过；未删除/skip/弱化任何旧断言。

## 8. 报告 21 问回答

1. 当前 Sources 原来到底具有什么能力？—— 只有“复制文件 + 清单 + 删除”，无解析/索引/检索（index_status 预留恒 none）。
2. 本轮新增哪些真实能力？—— parser/chunker/hybrid index(全文+语义)/retriever/自动注入/显式工具/增量重建/删除清理/状态与审计。
3. Source 数据保存在哪里？—— 文件在 `forge_data/projects/{pid}/sources`；记录/状态在 `project_sources`。
4. Chunk 保存在哪里？—— agent.db `source_chunks`（含结构元数据与 hash）。
5. FTS 使用什么实现？—— SQLite FTS5（external content，`source_chunks_fts`，unicode61）。
6. Vector Search 使用什么实现？—— 本地 ONNX bge（复用 rag.OnnxEmbedEngine），BLOB 存 agent.db，Python 余弦。
7. Embedding 用什么 Provider/Model？—— Provider=本地 ONNX（无远程），模型=bge-small-zh-v1.5 目录（local-onnx），512 维，可替换（EmbeddingProvider 语义即 rag 引擎接口）。
8. 如何进行 Hybrid Merge？—— RRF(k=60) + score-based 符号精确性 rerank；无 LLM 参与。
9. 是否存在 Rerank？—— 轻量规则/分数 rerank（非模型）。
10. Context Builder 如何接入？—— runner 既有 instructions 注入点追加“参考资料”段（带预算与 trust 边界），先于模型循环；显式工具经工具通道。
11. Sources 是否严格 Project 隔离？—— 是：表级 project_id + retriever 强制过滤 + RunContext scope；跨项目实测 no_results。
12. 是否全部经过 Trust Boundary？—— 是：自动块与工具返回逐 chunk `runtime.trust.tag`。
13. 是否可能绕过 FileScope？—— 不能：只索引 Source 存储复制件；工具检索 scope 由 Runtime 注入；Source 添加路径沿用上传白名单与复制语义。
14. Source 删除是否清理全部索引？—— 是（chunks+FTS+vectors；容器删除级联同清；测试）。
15. Source 修改是否增量 Reindex？—— 是（同 source 原子替换，按 content/版本重建，无幽灵）。
16. Retrieval 失败是否可降级？—— 是：向量不可用→lexical-only；全失败→retrieval_unavailable（非 no_results）。
17. 是否区分 NO_RESULTS 与 RETRIEVAL_FAILED？—— 是（mode=no_results vs retrieval_failed/unavailable + 事件 error 字段）。
18. 是否有 Prompt Injection 测试？—— 有（注入文档：内容保留+外部数据声明+非指令声明）。
19. 是否有真实 Agent E2E？—— Pipeline 全链真实验证（上传→索引 ready→hybrid 检索事件）；模型落地回答曾成功生成一次并发现/修复 Completion 误报；当次 Provider 故障导致模型侧闭环不完整（如实记录，登记后续复测）。
20. 是否影响原有 404+ Runtime Tests？—— 否：全量 431 OK（新增 9 项后基线继续全绿）。
21. 当前还有什么 P0/P1/P2？—— P0=0、P1=0；P2：① Provider 恢复后补做“按资料修改+验证”完整模型闭环 E2E；② 10/100/5000-chunk 规模化容量曲线（当前小样本 16ms 检索/0.13s 单源索引）；③ 前端“资料已索引/可用”状态气泡与显式关闭单 Source 的交互（后端 enabled 列已就绪）。
22. Sources RAG 是否达到可长期使用标准？—— **CONDITIONALLY YES**：本地混合检索、隔离、Trust、增量、删除、预算、审计全部四层验证（代码+DB+离线测试+真实链路事件）；距无条件 YES 仅差上述 P2-①（需要 Provider 侧可用时段的模型闭环复测）与规模化曲线。

## 9. 验收对照

| 项 | 状态 |
|---|---|
| 用户添加 Source（真实 API+后台索引） | ✓ |
| 自动解析（md/txt/代码/json/yaml/toml/pdf/docx，失败=failed+原因） | ✓ |
| 结构化 Chunk（结构边界优先+provenance 字段） | ✓ |
| 全文索引（FTS5） | ✓ |
| 语义索引（本地 ONNX，可降级） | ✓ |
| Hybrid Retrieval（RRF） | ✓ |
| 来源 Metadata（source/section/lines/page/title） | ✓ |
| 严格 Project 隔离 | ✓ |
| Trust Boundary | ✓ |
| Context Token 预算（6000 字符默认） | ✓ |
| Agent 自动获得资料（run 注入 + 事件实证） | ✓ |
| 显式 search_sources（只读、同 retriever） | ✓ |
| Source 更新增量重建 / 删除无幽灵 | ✓ |
| Embedding 故障可降级 | ✓（代码+路径验证） |
| Prompt Injection 不突破边界 | ✓ |
| 真实 E2E（管道级）+ 全量旧测试 | ✓（模型侧复测列为 P2-①） |

## 10. 冻结说明

主链与 Sources 边界保持 **Runtime Stable Baseline**：本报告后，Agent 主循环不做结构性改动；Sources 仅按 P2 清单增量演进（容量曲线、状态 UI、Provider 恢复后模型闭环复测）。
