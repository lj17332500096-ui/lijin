# CI 首跑交接清单（2026-10-05）

>对应 commit `c3fd5b7`（三个 workflow 改造成能在真实 Windows runner 上跑）。
>
> **这份文档存在的理由**： coder 在 commit message 里承诺「Items that genuinely
> require a real push are listed in the handover report」，但**那份报告从未落盘**。
> 交付缺口由主理人补上，结论均经主理人独立复核。

## 结论先说

三个 workflow 已按「真实 runner 环境」重做，**修掉 6 个 P0**，其中 4 个属于
**「本地全绿、CI 必红」**。但**全部改动未在真实 GitHub runner 上执行过**——
本机未授权 `git push`。**下面第 2 节的清单不实测就无法关闭。**

## 1. 已修的 6 个 P0（本地可验证部分已验证）

| # | 问题 | 为什么本地看不出来 | 修法 |
|---|---|---|---|
| P0-1 | `run:` 各step 是独立 pwsh 进程，`$env:X=` **不跨 step 传递** | 单 step 内验证「看起来生效」 | 改用 job 级 `env:` 块（runner 在每个 step 前注入） |
| P0-2 | `.env` 不入库 → 6 个键缺失 → **23 条失败** | 本机有 `.env` | job 级显式设 `ALLOW_CODE_EXEC` / `ALLOW_PROJECT_EDIT` / `AGENT_MODEL` / `FORGE_MODEL_PREF` 等，值指向**不可路由端点**（真调用会失败而非打到真服务） |
| P0-3 | `web/llama-ui`（9.3M 前端产物）被 `.gitignore:83` 排除，runner 上不存在；`llama_bridge.py:214` 的 `StaticFiles(directory=...)` 在目录缺失时 Starlette 抛 `RuntimeError` → **12 条失败** | 本机有该目录 | 加一步创建空目录，9.3M 产物仍不入库 |
| P0-4 | `pypdf` 在 `requirements.txt` 里是注释掉的「可选依赖」，但 `test_sources_rag.py::test_docx_pdf_sources` 无条件 import 它 | 本机 `.venv` 恰好装了 | workflow 里显式安装（coder 用import blocker 模拟「未装」确实会红，验证过） |
| P0-5 | `guard-consistency.yml` 用 `origin/${{ github.base_ref }}`，而 `base_ref` **只在 pull_request 时有值**；`workflow_dispatch` 手动触发时为空 → 退化成 `origin/` → `git diff origin/` 退出码 128 | 本地不跑 dispatch | 加空值判断，回退到 `HEAD~1` |
| P0-6 | matrix 用了 `${{ matrix.shard - 1 }}`，但 GitHub 表达式**没有减法运算符**（官方表只有 `() [] . ! < <= > >= == != && \|\|`） | 本地不解析表达式 | 改0-based matrix，使 matrix 值直接等于 `--shard-id` |

另修 P1：`scripts/check_workflow_shells.py` 在**报出错误时崩溃**（用严格 UTF-8 读 pwsh
错误文本，中文 Windows 控制台的 `Set-Content` 输出 OEM 代码页字节 → `UnicodeDecodeError`），
结果是「结论对（exit 1）但说不出哪个 step 错」——**恰好在最需要它的时候失去鉴别力**。
已改为宽松解码。

### 本地验证证据

- 干净 clone（无 `.env` / 无 `data/` / 无 `var/models`）跑全量：**1516 passed**
- 4 个分片按 runner 的展开方式分别执行：0/1/2 绿；shard 3 有一个 flake（已修，见下）
- 中和实验：把 P0-1 那个 `<` 重定向重新注入 pwsh step，校验器**退出 1 并打印
  PowerShell 解析器的原文** → 证明它能鉴别
- `scripts/check_workflow_shells.py`：15/15 steps ALL OK

## 2. 必须真push 才能关闭的清单（本地无法证实）

按 `AGENTS.md`「操作指南」归类存放。以下每一项在真实 runner 上跑过之前，
**都不得声称「CI 已验证」**。

### 2.1 触发与权限
- [ ] **首次 push 能否触发**：`on:` 的分支过滤是否覆盖你的默认分支；新远端仓库
      首次 push 是否会跑起来
- [ ] **Actions 权限**：仓库设置里 workflow 权限是否够（`contents: read` 起步）
- [ ] **`gh` CLI 在 runner 上可用性**（`nightly.yml` 若用到）

### 2.2 耗时与并发
- [ ] **总时长**：本机全量约 **13.5 分钟**（813 秒）。`ci.yml` 4 分片并发时，
      单分片墙钟时间需实测；确认没有 job撞上 runner 的默认超时
- [ ] **并发组冲突**：`guard-consistency.yml` 与 `ci.yml` 是否争抢 self-hosted 或
      限流额度

### 2.3 分片
- [ ] **每分片都真的有测试**：`ci_shard.py:50-62` 曾有 **3 个文件零收集**，
      零收集分片会报 `no tests ran`。本地实测 0/1/2 绿，**但需确认 4 个分片在
      runner 上都非空**
- [ ] **该用例落在 shard 3**（`--num-shards 4` 实测）。

### 2.4 依赖与网络
- [ ] **依赖安装耗时与可达性**：`pypdf` 等显式安装项在 runner 上能否拉取
- [ ] **不可路由端点的错误形态**：P0-2 故意让端点不可达，**确认这些测试断言的是
      「优雅降级」而不是「必须成功」**。若某测试其实需要真模型才绿，这个「修法」
      等于把红灯换成另一种红灯
- [ ] **`FORGE_RUNTIME_DIR` 类指向 `var/` 的配置**：runner 上 `var/` 不存在，
      确认相关代码有fallback 而不是直接崩

### 2.5 首跑后必做
- [ ] 首跑结果回来后，**任何红灯都必须对比 HEAD 归因**（项目铁律：历史上 10 条
      「既有失败」里 8 条是工作区改动自己引入的回归，不要看它「像不像老问题」）
- [ ] 首跑绿灯也不等于稳：**取消测试刚修的 flaky 需在 runner 负载下再观察若干次**

## 3. 本轮一并修掉的相邻问题

`c3fd5b7` 报告 shard 3 有一个 flaky 落在
`tests/test_production_closure.py::CancelSemanticsTests::test_explicit_cancel_stops_execution_and_lands_cancelled`
并明确标注超出它的授权范围（需动 `runtime/` / `tests/`）。

主理人实测：**5/5 全红**（不是它估的 1/3），且根因是**取消失效**（生产缺陷，
非测试抖动）—— 上一次「全量1508 passed」是侥幸。该缺陷已由`7d9a196` 修复，
详见 `docs/audits/` 与本文件 §1 之外的独立 commit。

## 4. 仍开放的既有问题（未修，需单独排期）

- **真故障路径 `task.failed` 事件重复写入 2 次**。已用 HEAD 与修复后逐事件对比
  确认属既有问题、与取消链路无关。该文件不含 cancel 代码。
- `tests/test_tui_artifacts.py` 在**全量负载**下偶发红（`pilot.pause(0.1)` 定时不足，
  Textual 渲染竞态）。主理人单跑 6/6 绿 → 负载相关竞态，非稳定 flaky。
