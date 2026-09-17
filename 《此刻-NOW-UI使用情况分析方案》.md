# 《此刻 · NOW UI 使用情况分析方案》

> 日期：2026-09-08
> 范围：**方案设计**（不包含实现代码）。目标是：从**真实使用**角度回答「UI 上哪些功能被用了、用得深不深、用户在流程的哪一步流失」。
> 场景约束：**单机 / 个人使用**；本方案不开第三方分析服务，全部数据本地落盘、可在关网环境运行。
> 配套原则：**不为分析去复制后端已存在的事实**；埋点只记录「用户与界面交互」这一层，后端已有的事实（任务/轮次/审批/工具）通过 `task_id / run_id` 关联复用。

---

## 0. 一句话结论

- **要采集什么**：用户的 UI 交互事件（页面视图 + 关键动作），带 `page / action / target / task_id / run_id / 时长`。
- **放哪里**：本地新增 `usage.sqlite`（独立库，不动 `agent.db` / `sessions.sqlite` 结构），前端 `navigator.sendBeacon` 批量落库。
- **怎么看**：一个分析脚本 `usage_report.py`，输出两类结果——**任务主链路漏斗**（哪一步流失）与**功能使用深度表**（哪些功能没用/用得浅）。
- **关联事实**：漏斗中「进入处理中 / 等你确认 / 已完成」这些**状态点不靠埋点**，直接读 `agent.db` 的 `runs`/`task_events`/`approvals`（带时间戳），避免埋点丢失或对不齐。

---

## 1. 目标与分析对象（从用户视角划边界）

「此刻 · NOW」对普通用户呈现的可见功能面（据 `web/runtime.html` + `workspace.js` 审计）：

```
此刻 NOW
├─ 左栏导航
│   ├─ ＋ 新建任务 (newDraftBtn / composer 内的 新任务)
│   ├─ ⌕ 搜索任务 (Ctrl K / btnSearch)  → 结果打开任务
│   ├─ 全部任务 / 进行中 / 等你确认（三个过滤）
│   ├─ 最近任务 rail（点击进入对话）
│   ├─ 查看全部任务
│   └─ 底部：资料 / 设置
├─ 顶栏
│   ├─ ☰ / 收起导航
│   ├─ 面包屑：此刻 / 任务
│   └─ ⋯ 更多菜单：资料 / 设置 / 回收站 / 关于此刻
├─ 任务对话主区（一个任务 = 一个持续对话）
│   ├─ 消息流（你说 → 此刻正在处理 → 结果/需要确认/失败卡）
│   ├─ 卡片动作：详情 / 归档 / 暂停 / 停止
│   ├─ 审批卡：看看会改什么 → Drawer 影响 diff；暂不 / 继续
│   ├─ 失败卡：查看问题 / 再试一次
│   └─ 交付卡：下载成果 / 使用材料 / 查看运行记录
├─ 输入区（Composer）
│   ├─ 选择文件夹（工作位置）
│   ├─ 添加文件：上传文件（仅本次）｜从项目来源选择
│   └─ 发送 / 停止
├─ 资料页 / 设置页（常规/外观/通知/记忆/数据/高级） / 回收站
├─ Drawer「任务详情」：文件 / 修改 / 测试 / 运行记录
└─ 移动底栏：任务 / 资料 / 设置
```

**本方案分析的用户动作=上述「点按/输入」事件**；运行时的内部过程（工具名、token、模型）不属于用户层，不进入本套数据（已有 traces 覆盖）。

---

## 2. 数据源设计（两层，职责分离）

| 层 | 数据 | 来源 | 用途 |
|---|---|---|---|
| **A. UI 交互层**（新增） | 用户点了什么、停留多久、输入了什么行为意图 | 前端新增 `telemetry.js`，经 `/api/telemetry` 批量 POST → `usage.sqlite` | 功能使用率 / 深度 / 各入口偏好 |
| **B. 事实层**（已有，零新增） | 任务、轮次、状态迁移、审批、工具调用 | `agent.db`（`tasks/runs/messages/task_events/approvals/tool_calls/model_calls`） | 漏斗的「状态节点」与时间戳、结果口径 |

> 为什么漏斗不全部靠埋点：
> 单机场景 SSE 断连/重连、页面刷新、离开页面都会让前端丢事件；而 `agent.db` 是权威状态机（`task_events` 每次状态迁移都有时间戳）。UI 埋点负责「用户动作」，agent.db 负责「发生了什么」，两者按 `task_id (+run_id)` 关联，互不依赖。

---

## 3. UI 事件模型（采什么）

### 3.1 事件结构（统一 schema）

```
{
  "v": 1,
  "ts": "2026-09-08T10:20:31.000+08:00",   // 后端接收时间
  "client_ts": 1694... ,                     // 前端发生时间(ms)
  "sid": "…",                               // 安装ID / 浏览器实例ID（见 §4）
  "page": "tasklist | task | materials | settings | trash | about",
  "action": "task.open | message.send | approval.decide | ...",   // §3.2 字典
  "target": "sendBtn | navWaiting | drawerTab.tests | ...",       // 具体控件
  "task_id": "…",    // 可空：当时在哪个任务对话里
  "run_id": "…",     // 可空：当时哪一轮
  "props": { },      // 结构化补充（决策值、文件数、切换目标等，不放正文）
  "dur_ms": 0        // 视图停留等带时长事件才填
}
```

### 3.2 动作字典（先收敛到 ~30 个，够出报表，别过度埋）

**视图（page view，进页面/切 tab 各记一条，可带 dur_ms）**
- `tasklist` / `task`（某任务对话）/ `materials` / `settings` / `trash` / `about`
- `tasklist.filter`：全部/进行中/等你确认（target=navRunning…）
- `drawer.open` / `drawer.tab`：文件 / 修改 / 测试 / 运行记录（target=drawerTab.*）

**核心价值动作**
- `message.send`：target=sendBtn｜props{chars, attachments}（普通用户的核心「开工」）
- `task.stop` / `task.pause` / `task.archive` / `task.pin`
- `task.open`（从 rail/列表/搜索结果/资料进入某任务，target=来源）
- `approval.decide`：props{decision:allow|deny}（**只记决定动作，不记参数内容**）
- `approval.detail`（点了「看看会改什么」）
- `retry.run`（失败卡「再试一次」） / `error.detail`（「查看问题」）

**周边能力**
- `search.open` / `search.select`
- `materials.open` / `artifact.download` / `material.use`
- `file.attach`：props{kind:upload|source, n}
- `worklocation.pick`
- `settings.open`（target=常规/外观/通知/记忆/数据/高级）
- `theme.set`（target=auto|dark|light）—— 判断外观设置是否被动过
- `notify.open`（铃铛/通知面板，若存在）
- `more.menu`：资料 / 设置 / 回收站 / 关于（判断顶栏 ⋯ 的渗透）

> 规则：
> - **不进事件体**：消息内容、审批 arguments、文件名全文（单机可本地留痕，但不必进分析库）；需区分文件类型只记扩展名/长度档。
> - **不做** 每次 keyup、每格 hover、纯 CSS 效果等噪音。
> - 事件名用「用户语言」，与 `activity.js` 投影一致，禁止直接暴露 Run/Tool/Approval 词（保持产品语义）。

### 3.3 幂等与去重

- 每个事件前端生成 `uuid`，后端 `PRIMARY KEY(ev_id)` 去重（SSE/页面重试不致重复计数）。
- 发送失败（网络/服务未起）回退到 localStorage 队列，下次打开 flush；队列上限 ~500 条。

---

## 4. 用户身份（单机方案）

- 首次加载在 localStorage 生成并持久化 **`sid`**（随机 32 位）→ 代表「这台机器的这个浏览器」；换浏览器/清数据即新 ID（单机个人场景可接受）。
- `start-web.bat` 启动时可选把 sid 与主机名绑定 → `usage.sqlite.sessions` 表存 `{sid, host, first_seen, last_seen}`。
- 会话级用 `page` 连续 + 30min 无动作切分「使用会话」（sessionization），供「一次使用里走没走完流程」分析。

---

## 5. 转化 / 流失分析（指标定义）

### 5.1 主链路漏斗（任务 = 一个持续对话）

以「任务」为分析单位，从 **agent.db 事实**（含埋点关联补齐 UI 起点）组装：

```
S0 新建入口被使用          — UI 埋点 task.new / message.send(首条)
S1 首次消息已发出          — agent.db messages(role=user, 该 task 第一条)
S2 任务进入「正在处理」     — agent.db runs.started_at / task_events 首次 running
S3 出现「等你确认」         — agent.db approvals.status 首条 pending（仅该任务需要审批时）
S4 用户做出决定             — agent.db approvals.decided_at（并行关联 UI approval.decide）
S5 本轮完成/交付            — agent.db run completed_at / task_events completed
S6 结果后继续对话/再次发起   — UI message.send(次条) / 新任务（回流）
```

- **报告口径**：`S1→S2→…→S5` 每步人数（单机=任务数）与转化率、平均耗时（P50/P90）、中位时延；在 S3 需要审批的任务子集单独看 S3→S4→S5（用户在审批卡流失最多）。
- **「流失」解读提示（单机）**：主要看**哪些类型的任务反复卡在同一层**（如多次 waiting_approval 后被停）、首条后有没有发出、失败后有没有「再试一次」。

### 5.2 回流与存留（轻量）

- 日活/周活次数、每次「一次会话完成的动作数」；
- 一个任务从创建到「完成」跨了多少天、追加了几轮（消息条数 > 首轮 = 持续对话成立）。

---

## 6. 功能使用深度分析（指标定义）

**单功能指标（功能 = §3.2 的一个 action / target）**
- 使用天数、总次数、涉及任务数；
- **深度分级**：
  - `未发现`：完全 0 次（判断：入口是否难找/是否需要）；
  - `浅用`：出现 ≤1 次或只用默认路径；
  - `复用`：多天 / 多任务重复触发；
  - `深度`：同一功能上还有次级动作（如审批前都先点「看看会改什么」，drawer 切到 修改/测试 tab）。
- **启用率/覆盖率**：本期里「至少用过 1 次」的功能数 / 全部功能数。

**重点观测清单（结合当前产品对普通用户的收敛意图）**
- Drawer 四个 tab 里用户真实切到几个（还是永远停在运行记录）？
- 「资料」页使用 → 是否只是路过（open 无 download/use）？
- 「设置」各分区是否碰过（尤其记忆/数据/高级，判断高级项是否该藏更深的判断依据）？
- 「更多 ⋯」菜单渗透 vs 左栏直达（重复入口哪个被用）？
- 移动底栏 vs 桌面左栏（若你横竖屏都用过，可判断哪套入口在生效）。

---

## 7. 采集与存储实现要点（不写代码，给落点）

| 层 | 落点（据现有文件） |
|---|---|
| 前端采集模块 | 新增 `web/runtime/telemetry.js`（IIFE，风格同 `eventClient.js`）；`runtime.html` 在 `<script>` 序列中引入（版本号 `?v=` 与现有 227 一档递增即可） |
| 埋点插入面 | 优先在 `workspace.js` 里**既有事件处理器**做最小侵入：`sendMessageRun`/`openTask`/`decideAll`/`createNewTask`/`openDrawer`/`goHome`/`openSearch` 等函数入口一行 `RT.track(...)`；页面视图在 `state.page` 切换处记一次 |
| 上报通道 | `navigator.sendBeacon('/api/telemetry', blob)`（关页不丢）＋ `fetch` 批量兜底 |
| 后端接收 | `webapp.py` 新增 `Route("/api/telemetry", api_telemetry, methods=["POST"])`；参考 `api_material_upload` 的 JSON 读取与错误包装 |
| 落库 | 新增 `usage.sqlite`（`webapp.py` 同目录级 `BASE_DIR / "usage.sqlite"`），WAL；`CREATE TABLE ui_events` 见 §3.1 字段 + `sessions` 表。**不改动** `task_manager.py` schema（agent.db 保持纯净，避免拖累回归基线） |
| 只读交叉 | 分析脚本以只读打开 `usage.sqlite` 与 `agent.db`（sqlite URI mode=ro，参考 `webapp.py:135` 既有写法） |
| 关闭开关 | 默认开；`.env` 提供 `NOW_USAGE_TRACKING=0` 一键关（避免「个人也可能不想要记录」） |
| 存量兼容 | 前端各模块用 `window.RT.telemetry` 存在性判断调用（同现有多处 `RT.xxx &&` 模式），不引入新依赖、不破坏 `node --check` 与 `regression.ps1` |

---

## 8. 报表输出（交付形态建议）

1. **`usage_report.py`（首选，零前端成本）**
   - 参数：`--since` / `--days 7|30|90` / `--output md|html|json`；
   - 输出：主漏斗表（含耗时）+ 流失点标注 + 功能深度分级表 + 未使用功能清单 + 逐日活跃；
   - 目标读者是自己：一张 Markdown 直接丢进编辑器，或生成自包含 HTML（可复用 `design.css` 风格，无外部 CDN）。
2. **内置查看面（二期可选）**：设置页「数据」分区加一个「查看使用统计」→ 跳 `GET /usage`（只读渲染脚本结果）；或在「关于此刻」展示最小累计数字。属于加分项，非必需。

---

## 9. 分期计划与验收

### P0 — 采集通道（半天~1天）
- `telemetry.js` + `runtime.html` 引入 + `sid` 生成；
- `webapp.py` `/api/telemetry` + `usage.sqlite` schema；
- 先埋 **~8 个关键动作**：`message.send / task.open / approval.decide / retry.run / artifact.download / materials.open / settings.open / page-view(tasklist|task)`。
- 验收：真实浏览器点一轮后 `usage.sqlite.ui_events` 有 ≥8 条且字段完整；刷新丢事件仍能补发；`node --check` 通过；`regression.ps1` 全绿。

### P1 — 漏斗 + 功能深度报表（半天）
- `usage_report.py`：漏斗（§5.1）与功能深度（§6）；
- 交叉 agent.db：S2–S5 节点与耗时；任务级别组装一条龙。
- 验收：对存量 `agent.db`（已有历史任务）直接能出 90 天漏斗；空库/新库不出错；输出 md 可读。

### P2 — 收敛与深度观测（可选，随用随加）
- 全量动作字典（~30 个）补齐；设置/资料/搜索/抽屉次级动作；
- 内置 `/usage` 只读页或「关于」摘要；
- 季度性用报表结论回灌产品决策（如把 0 次功能入口隐藏、把高频动作入口上提）——这是本方案最终目的。

---

## 10. 风险与取舍（诚实清单）

1. **单机样本量小**：不做统计显著性；报告按「任务数 / 天数 / 行为模式」而非人群百分比解读。
2. **埋点滞后于真实使用**：今天埋，数据从今天开始攒；历史行为只能从 agent.db 反推（漏斗的状态点能回补，UI 点击不可回补）。
3. **事件冗余 vs 价值**：先 P0 的 8 个保质量，别一次铺 30 个——埋了不看的动作等于日志噪音。
4. **隐私**：全本地；事件不含对话正文与审批参数；仍建议保留一键关闭开关。
5. **不进入** agent.db schema：避免任何 P0/P1 回归风险；usage.sqlite 可随时删掉重来，不影响产品数据。
6. **Web 与终端双入口**：`main.py` 终端聊天不经过本 UI 埋点，其使用由 agent.db 消息/任务体现，漏斗仍可覆盖「任务完成」段，只缺「点击入口」段——报告需注明口径。

---

## 11. 本方案不做的（防膨胀）

- 不做热力图 / 会话录屏（个人单机收益低）；
- 不接 PostHog/Amplitude/遥测三方（本地优先 + 关网可跑是约束）；
- 不分析「模型层/token/工具耗时」——那是 traces/observability 的域，已存在 `observability.py`；
- 不做实时流式看板（脚本生成报表足够，个人不需要秒级）。
