# Agent Lightning 接入方案

> 对象：`my_creative_agent`（FORGE 全能助手，OpenAI Agents SDK + ResilientProvider）
> 目标：用 Agent Lightning（微软 agentic RL 框架）优化 agent
> 生成日期：2026-09-18
> 源码位置：`H:\GitHub源码文件\agent-lightning-main\agent-lightning-main`

---

## 0. 结论先行

Agent Lightning 有两个价值层面，难度差一个数量级，建议分阶段用：

| 层面 | 内容 | 门槛 | 建议 |
|---|---|---|---|
| **L1 数据与评估** | 把 agent 的真实执行轨迹（rollout）+ 自动奖励记录下来，作为 RL 训练样本和未来迭代的证据 | 只需 `agl-server` + `agl-controller`（纯 Python，无 GPU），Windows/WSL 均可 | **先做**，零硬件门槛，立即产出可量化的"轨迹+奖励"数据集 |
| **L2 RL 训练** | 用 verl + vLLM + flash-attn GPU 栈，用 L1 的轨迹 GRPO 更新模型权重 | 需要 NVIDIA GPU（CUDA 12.9/13.0）+ 可训练的小模型权重 + 可自动判定的奖励 | 有卡再做；没有卡则 L1 的数据可以攒着，或先做 L1.5（见下） |
| **L1.5 无 GPU 优化** | 用仓库自带的方法论 skill（`skills/agent-lightning/SKILL.md` 的 12 个优化杠杆）直接改 prompt/工具/路由，不动模型 | 不需要任何新硬件 | 与 L1 并行，性价比最高 |

**核心判断：你的 agent 是创意/助手型（写作、调研、办公文档、备忘），奖励难自动判定。Agent Lightning 最强的场景（计算、SQL、代码）你只占 1/3。所以不要指望"L2 一键变强"，而是：L1 攒数据 + L1.5 改杠杆 + 有 GPU 时再 L2 微调。**

---

## 1. 你的 agent 现状与 Agent Lightning 的契合点

### 1.1 模型接入（完全契合）

`agent.py` 里的接入方式：

```
build_model_provider()
  └─ ResilientProvider(api_key, base_url, use_responses)   # runtime/provider_gateway.py
main.py
  └─ build_run_config() → RunConfig(model_provider=...)
  └─ Runner.run / run_streamed(agent, prompt, run_config)
```

- 远程走 `OPENAI_BASE_URL`/`OPENAI_API_KEY`，本地走 `FORGE_LOCAL_MODEL_*`（llama.cpp/Ollama 的 `/v1`）。
- **这正是 Agent Lightning 要求的 OpenAI 兼容接入点**：AGL 的 API Gateway 就是一个 OpenAI 兼容代理，只需把 `base_url` 指过去。

### 1.2 奖励来源（部分契合，需改造）

- 你已有 `evaluate.py`：5 个场景（计算/存笔记/输入安全/JSON结构/审批策略）× 四维自动判分（Outcome/Trajectory/Policy/Efficiency），并写 `tests/reports/eval_report.json`。**这是现成的 reward 信号源。**
- 创意类任务（写文章、调研、生成 Office 文档）**没有自动判定器**，需要 LLM-as-judge 或人工标注才能进 RL。这是最大缺口。

### 1.3 执行入口（需适配）

- 生产入口是 `main.py` 的 `AgentRuntime`（`runtime/runner.py`），交互式、带审批/护栏/会话。
- AGL 要求一个**可被 Controller 拉起、跑完即退出、最后 POST 一次 reward** 的独立 entrypoint（参考 `examples/calc_x/calc_agent.py`）。你的 `evaluate.py` 场景模式最接近这个形态，比 `AgentRuntime` 交互模式好适配。

---

## 2. 架构对照（AGL 三组件 vs 你的项目）

```
                 ┌─────────────────────────────────────────────────┐
                 │  Agent Lightning v1.0                          │
                 │  ┌─────────────┐  ┌──────────┐  ┌──────────┐   │
                 │  │ API Gateway │  │ Controller│  │ Trainer  │   │
                 │  │ agl-server  │  │agl-ctrlr │  │ verl+vllm│   │
                 │  └──────┬──────┘  └────┬─────┘  └────┬─────┘   │
                 └─────────┼──────────────┼──────────────┼────────┘
                           │              │              │
        OpenAI 兼容代理     │  拉起 agent  │  轨迹→训练样本 │
        (记录 token/logprob)│  (local/K8s)│  (GRPO 更新)  │
                           ▼              ▼              ▼
                 ┌─────────────────────────────────────────────────┐
                 │  my_creative_agent                               │
                 │  agent.py (ResilientProvider)                   │
                 │  main.py / evaluate.py (Runner 执行)            │
                 │  任务结束 → POST reward 到 AGL_EVENT_URL         │
                 └─────────────────────────────────────────────────┘
```

- **API Gateway**：你的 agent 把 `OPENAI_BASE_URL` 指到 `http://<host>:8181/proxy/rollout/{rollout_id}/attempt/{attempt_id}/mode/train/openai/v1`，每次模型调用被自动记录为 `model_request` 事件（含 prompt/response token ID、logprob）。
- **Rollout Controller**：本地模式每个 rollout 起一个子进程（你的 agent entrypoint）；K8s 模式每个 rollout 起一个 K8s Job。
- **Trainer**：把 Gateway 里的事件转成 verl 训练样本并更新策略（需要 GPU）。

---

## 3. 接入步骤

### 3.1 前置：装 AGL（训练栈可后补）

```bash
cd "H:\GitHub源码文件\agent-lightning-main\agent-lightning-main"
uv sync                                   # 装基础环境到 .venv
# 仅 L1（数据/评估）时：跳过 GPU 栈；仅做采集不需要 verl/vllm
# L2 训练时（必须 Linux + NVIDIA GPU）：
#   source .venv/bin/activate
#   bash scripts/setup_verl.sh 0.8.0 cu130
#   uv run wandb login
```

> 注意：`runner_type=local` 不支持原生 Windows。你有 WSL（Ubuntu），L1/L2 都在 WSL 里跑；agent 代码是纯 Python，WSL 里直接复用 `my_creative_agent` 即可。

### 3.2 给 agent 加 AGL 接入层（3 处改动）

**改动 1：新增 `agl_entry.py`（AGL rollout 的独立入口，仿 `calc_agent.py`）**

```python
"""AGL rollout 入口：读 env → 跑一个 evaluate 场景 → POST reward。
由 agl-controller(local) 为每个 rollout 拉起一个子进程执行。
"""
import os, asyncio, httpx

async def main() -> None:
    # AGL Controller 注入的环境变量
    scenario  = os.environ["SCENARIO"]          # 自定义：场景 id
    agl_key   = os.environ["AGL_KEY"]
    event_url = os.environ["AGL_EVENT_URL"]     # POST reward 的地址
    base_url  = os.environ["AGL_OPENAI_BASE_URL"]  # rollout 专属代理 URL

    import main, evaluate
    # 1) 把 provider 指向 AGL Gateway（复用你现有 ResilientProvider，仅换 base_url）
    agent = main_module.build_assistant_agent()
    cfg   = main_module.build_run_config_for_agl(base_url=base_url, key=agl_key)

    # 2) 跑指定场景（无副作用、独立会话，evaluate.py 已有）
    report = evaluate.run_single(scenario, run_config=cfg)

    # 3) 把多维判分合成一个标量奖励上报
    reward = report.combined_score            # 0~1，见 §4 奖励设计
    httpx.post(event_url,
        json={"event_type": "reward", "data": {"value": reward}},
        headers={"Authorization": f"Bearer {agl_key}"}, timeout=10.0,
    ).raise_for_status()

if __name__ == "__main__":
    asyncio.run(main())
```

**改动 2：`main.py` 加一个 AGL 专用 RunConfig 构造器（不改现有路径）**

```python
def build_run_config_for_agl(base_url: str, key: str, model: str = "auto") -> RunConfig:
    """AGL 采集/训练专用：provider 指向 agl-server 的 rollout 专属代理。"""
    from runtime.provider_gateway import ResilientProvider
    provider = ResilientProvider(api_key=key, base_url=base_url, use_responses=False)
    return RunConfig(model_provider=provider)   # 其余参数同 build_run_config
```

**改动 3：`.env` 增加 AGL 段（可选，便于手动跑）**

```
# Agent Lightning
AGL_BASE_URL=http://localhost:8181
AGL_KEY=dummy
```

### 3.3 启动采集（L1，无需 GPU）

```bash
# 终端 A：Gateway
agl-server port=8181 key=dummy \
    default_proxy.model_name=Qwen/Qwen2.5-1.5B-Instruct   # L2 时换成你的训练模型

# 终端 B：Controller（本地模式）
agl-controller runner_type=local \
    agl_server.url=http://localhost:8181 \
    agl_server.key=dummy
```

之后每个 rollout 由 Controller 拉起 `python agl_entry.py`（需配 Controller 的 job 命令，见 §5）。Gateway 自动落 `model_request` + `reward` 事件，即可导出为训练数据。

---

## 4. 奖励（reward）设计 —— 最关键、也最难的缺口

AGL 的 RL 价值 100% 取决于奖励可自动判定。按你的场景分三档：

| 场景类 | 判定方式 | 难度 | 进 RL |
|---|---|---|---|
| 计算 / 数据核对 | 精确匹配（你有 `calculate`，同 Calc-X 的 `scalar_are_results_same`） | 低 | ✅ 直接进 |
| 文件落盘（save_note） | 检查 `saved_file` 真实存在 + 非空（evaluate.py 已判） | 低 | ✅ 直接进 |
| 输入安全 / JSON 结构 | guardrail 是否拦截 + JSON 是否合法（evaluate.py 已判） | 中 | ✅ 直接进 |
| 审批策略 | 期望动作（拒绝/放行）是否命中（`_DENY_MARKERS` 已判） | 中 | ✅ 直接进 |
| 写作 / 调研 / Office 产出 | **LLM-as-judge**（另一模型打 0~1 分）或人工标注 | 高 | ⚠️ 仅当 judge 稳定时用 |

建议：
1. 先把**有自动判定的 4 类**纳入 AGL rollout 集，作为 L2 训练的可靠奖励；
2. 创意类用 LLM-judge（固定 judge 模型 + rubric），但**只在 L1 攒证据**，暂不直接进 GRPO（judge 噪声大，reward hacking 风险高，README 也专门强调"reward-hacking prevention"）；
3. `reward` 合成公式建议：`0.5*Outcome + 0.2*Policy + 0.2*Trajectory + 0.1*Efficiency`（权重可微调），效率项用"是否超 token/调用数上限"做惩罚。

---

## 5. Controller 的 rollout 命令配置

- 本地模式：Controller 需要知道"怎么拉起一个 agent"。参考 `examples/calc_x` 的 `job-template`（K8s）与 local reconciler，你的 local 场景就是把上面 §3.2 的 `agl_entry.py` 作为子进程命令，并注入 `SCENARIO/AGL_KEY/AGL_EVENT_URL/AGL_OPENAI_BASE_URL` 四个环境变量。
- 具体字段名以 `docs/30-controller-configuration.md` 与 `agentlightning/controller/local_reconciler.py` 为准（实现时再核对）。

---

## 6. L1.5：不动模型、纯杠杆优化（与 L1 并行，性价比最高）

用仓库里的 `skills/agent-lightning/SKILL.md`（12 个优化杠杆 + 评估纪律）直接改你的 `agent.py` 人设/工具/路由，**不需要任何 GPU**：

| 杠杆 | 对应你 agent 的落点 |
|---|---|
| Input grounding | 把 `search_documents` 检索结果、`get_current_datetime` 锚定的日期喂得更足（你已部分做） |
| Prompt | 人设 18 条规则 + readiness 三分法，可做 A/B |
| Tools | `calculate`/`read_spreadsheet` 等确定性工具，减少模型心算 |
| Routing | 创意 vs 事实性任务分流（deep_research vs web_search 你已有雏形） |
| Conditional repair | code_loop 的 3 次自动修复就是典型 repair 杠杆，可扩展 |
| Critique/selection | 对高价值产出（长文）用第二次 judge 模型挑优 |

纪律（来自该 skill，必须遵守）：
- 保持部署契约不变（你对外仍是 OpenAI 兼容 + AgentReply JSON）；
- 固定评测集（`benchmark/` 已有）做验证，改一处测一处，别一次性大改；
- 记录成本预算（开发预算 ≠ 单次运行成本），别为了开发把 agent 变重。

---

## 7. 里程碑计划

| 阶段 | 内容 | 产出 | 依赖 |
|---|---|---|---|
| M0 | WSL 里 `uv sync` AGL，起 agl-server + local controller，手动发一条模型请求确认被记录 | Gateway 跑通、事件可见 | 无 GPU |
| M1 | 加 `agl_entry.py` + `build_run_config_for_agl`，跑通 1 个有自动判定的场景（计算） | 一条完整轨迹 + reward 落库 | M0 |
| M2 | 扩到 4 类可判定场景，定 reward 合成公式，攒 ≥ 200 条轨迹 | 可导出的 RL 训练样本集 | M1 |
| M3 | 有 NVIDIA GPU 后：`setup_verl.sh` + W&B，用 M2 数据 GRPO 微调小模型 | 微调后的模型 + 前后 benchmark 对比 | GPU + M2 |
| 并行 | L1.5 杠杆优化（prompt/工具/路由/修复），固定 benchmark 验证 | 不依赖 GPU 的稳定性提升 | 立即可开始 |

---

## 8. 风险与红线

1. **创意任务 reward 不可自动判定** → 只能 LLM-judge，噪声大，暂不进 GRPO，先 L1 攒证据（防 reward hacking）。
2. **local runner 不支持原生 Windows** → 一律走 WSL；agent 纯 Python 可直接复用。
3. **训练栈版本耦合紧**（verl/vllm/flash-attn 必须用 `setup_verl.sh` 固定版本），别手装。
4. **Guardrail 与 AGL 的关系**：采集/训练路径要**关掉** input/output guardrail（`build_assistant_agent(enable_input_guardrail=False, ...)`），否则安全闸会拦截 Agent Lightning 要求的原始轨迹；但**生产部署路径保持全开**。
5. **成本纪律**：RL 多 rollout（GRPO 一个样本要跑多次）成本非线性，先用 1.5B 小模型 + 小批量验证收敛，再放大。

---

## 9. 参考（仓库内）

- `docs/00-installation.md`、`01-quick-start.md`、`05-basics.md`、`20/25/30/35` 配置章节
- `examples/calc_x/`：`calc_agent.py`（入口范式）、`train_calc_agent.py`（训练入口）、`run_local.sh`（一键起服务）
- `agentlightning/client.py`：`AgentLightningSync/AsyncClient`（rollout/reward 事件客户端）
- `skills/agent-lightning/SKILL.md`：12 杠杆优化方法论（L1.5 直接用）
