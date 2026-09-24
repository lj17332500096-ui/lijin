"""流程编排器（Orchestrator）—— FORGE 七阶段分层架构的核心。

职责：
1. 管理全流程状态（PipelineStatus）
2. 支持步骤重试和回溯（loop back + feedback loop）
3. 实现闭环反馈机制（下游失败 → 上游优化重跑）
4. 全链路 Trace ID 追踪

适配说明（自用户参考实现移植，2026-09-24）：
- 去掉了 `from ..agents.base_agent import AgentResult` 相对导入（FORGE 是扁平结构、
  无 `agents/` 包）。改为**鸭子类型**：凡带 `.success` 属性的对象即按 AgentResult 语义
  处理（`isinstance(output, AgentResult)` 换成 `hasattr(output, "success")`）。
- 保留参考实现的全部语义：register_step 链式注册、拓扑排序、max_retries、
  fallback_step、allow_loop_back、feedback_method、_check_loop_requirement、
  _analyze_failure 建议生成。
- 新增 `Step` / `StepKind`：这是 FORGE 特有的「Laya 判别 → 编排决策」枚举，
  与通用 StepConfig 正交（见 runtime/orchestrator 的顶层 decide() 入口）。

本模块只做**编排**，不 import 具体工具/模型，避免与 runner 循环依赖。
"""

from __future__ import annotations

import uuid
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Callable, Literal
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FORGE 特有：Laya 判别 → 编排决策（Step / StepKind）
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """编排器对单轮 query 的唯一下一步指令。"""

    kind: Literal["direct_tool", "need_llm", "direct_text", "llm_fallback"]
    tool: Optional[str] = None
    args: Optional[dict] = None


def decide(
    intent: Optional[str],
    intent_conf: float,
    completeness: Optional[str],
    route: Optional[str],
    route_conf: float,
    conf_thr: float = 0.85,
    tool_recall_thr: float = 0.995,
    mutation_tools: Optional[frozenset] = None,
) -> Step:
    """把 LayaDecision（三判别）转成唯一下一步指令（纯函数，可离线单测）。

    intent 取值（与 classify() 三判别对齐）：
      greeting / arith / tool_needed / ambiguous / None（降级）

    安全原则（P4 核心）：direct_tool 只对只读/发现类工具生效；
    凡命中 mutation 工具（write_*/save_*），强制走 need_llm（或由上层审批门接管），
    绝不由 Laya 直接触发写操作。

    优先级：
    1. direct_text：intent 明确纯文本（greeting/arith）→ 0 工具
    2. direct_tool：需工具 + 自足 + 路由高置信 + 非 mutation → 直接派发
    3. need_llm：需工具但需补全（或命中 mutation）→ LLM 补全后派发
    4. llm_fallback：其余（置信度不足/降级）→ 全量 LLM 兜底
    """
    mutation = mutation_tools or frozenset()

    # 1. 纯文本（greeting/arith 是三判别的纯文本意图，无需看置信度）
    if intent in ("greeting", "arith"):
        return Step("direct_text")

    # 2/3. 需工具
    if intent in ("tool_needed", "ambiguous"):
        if route is not None and route in mutation:
            # mutation 工具：绝不由 Laya 直连，强制 LLM 补全（再走审批门）
            return Step("need_llm", tool=route)
        if completeness == "self_sufficient" and route is not None and route_conf >= tool_recall_thr:
            return Step("direct_tool", tool=route)
        if completeness == "need_llm" or intent == "ambiguous":
            return Step("need_llm", tool=route)

    # 4. 兜底（intent=None 降级 / 置信度不足）
    return Step("llm_fallback")



class PipelineStatus(Enum):
    """流程状态"""
    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL_SUCCESS = "partial_success"


class StepStatus(Enum):
    """步骤状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    SKIPPED = "skipped"


@dataclass
class StepRecord:
    """步骤执行记录（对应「过程记忆」的 run_plan 表的内存形态）"""
    step_name: str
    status: StepStatus
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    input_data: Any = None
    output_data: Any = None
    error_info: Optional[str] = None
    retry_count: int = 0
    feedback_data: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return {
            "step_name": self.step_name,
            "status": self.status.value,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "retry_count": self.retry_count,
            "error_info": self.error_info,
            "feedback_data": self.feedback_data,
        }


@dataclass
class PipelineResult:
    """流程执行结果"""
    trace_id: str
    status: PipelineStatus
    steps: List[StepRecord] = field(default_factory=list)
    final_output: Any = None
    error_message: Optional[str] = None
    execution_time_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "trace_id": self.trace_id,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "final_output": self.final_output,
            "error_message": self.error_message,
            "execution_time_ms": self.execution_time_ms,
            "metadata": self.metadata,
        }


@dataclass
class StepConfig:
    """步骤配置"""
    name: str
    component: Any
    execute_method: str = "run"
    feedback_method: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    retryable: bool = True
    max_retries: int = 3
    fallback_step: Optional[str] = None
    allow_loop_back: bool = False


class Orchestrator:
    """流程编排器：闭环控制 + 全链路状态追踪。

    闭环机制：
    - 当下游步骤失败时，自动触发上游重试（feedback loop）
    - 支持反馈驱动的优化（feedback_method）
    - allow_loop_back 步骤可基于返回值回溯到上游目标
    """

    def __init__(
        self,
        max_retries: int = 3,
        enable_feedback_loop: bool = True,
        trace_id: Optional[str] = None,
        step_start_callback: Optional[Callable[[str, Any], None]] = None,
        step_complete_callback: Optional[Callable[[str, Any, bool], None]] = None,
    ):
        self.max_retries = max_retries
        self.enable_feedback_loop = enable_feedback_loop
        self.trace_id = trace_id or str(uuid.uuid4())[:16]
        self.step_start_callback = step_start_callback
        self.step_complete_callback = step_complete_callback

        self.steps: Dict[str, StepConfig] = {}
        self.step_records: List[StepRecord] = []
        self.step_outputs: Dict[str, Any] = {}
        self.current_status = PipelineStatus.PENDING

        self.step_execution_counts: Dict[str, int] = {}

        logger.info(f"Orchestrator initialized with trace_id: {self.trace_id}")

    def register_step(
        self,
        name: str,
        component: Any,
        execute_method: str = "run",
        feedback_method: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
        retryable: bool = True,
        max_retries: Optional[int] = None,
        fallback_step: Optional[str] = None,
        allow_loop_back: bool = False,
    ) -> "Orchestrator":
        self.steps[name] = StepConfig(
            name=name,
            component=component,
            execute_method=execute_method,
            feedback_method=feedback_method,
            dependencies=dependencies or [],
            retryable=retryable,
            max_retries=max_retries or self.max_retries,
            fallback_step=fallback_step,
            allow_loop_back=allow_loop_back,
        )
        self.step_execution_counts[name] = 0
        logger.debug(f"Registered step: {name}")
        return self

    def execute(self, pipeline_input: Dict[str, Any]) -> PipelineResult:
        start_time = datetime.now()
        self.current_status = PipelineStatus.RUNNING

        logger.info(f"Starting pipeline execution [trace_id: {self.trace_id}]")

        try:
            execution_plan = self._topological_sort()
            logger.info(f"Initial execution plan: {execution_plan}")

            current_idx = 0
            while current_idx < len(execution_plan):
                step_name = execution_plan[current_idx]

                step_config = self.steps[step_name]
                if self.step_execution_counts[step_name] > (step_config.max_retries * 2 + 5):
                    logger.error(f"Step {step_name} exceeded absolute max execution limit")
                    self.current_status = PipelineStatus.FAILED
                    break

                success = self._execute_step(step_name, pipeline_input)
                self.step_execution_counts[step_name] += 1

                loop_target = self._check_loop_requirement(step_name)
                if loop_target:
                    logger.info(f"Loop back triggered: {step_name} -> {loop_target}")
                    try:
                        target_idx = execution_plan.index(loop_target)
                        current_idx = target_idx
                        continue
                    except ValueError:
                        logger.error(f"Loop target {loop_target} not in execution plan")

                if success:
                    current_idx += 1
                else:
                    can_retry = self._handle_step_failure(step_name)

                    if not can_retry:
                        self.current_status = PipelineStatus.FAILED
                        break

            if self.current_status != PipelineStatus.FAILED:
                failed_steps = [s for s in self.step_records if s.status == StepStatus.FAILED]
                if failed_steps:
                    self.current_status = PipelineStatus.PARTIAL_SUCCESS
                else:
                    self.current_status = PipelineStatus.COMPLETED

        except Exception as e:
            logger.error(f"Pipeline execution failed: {e}")
            self.current_status = PipelineStatus.FAILED
            import traceback
            logger.error(traceback.format_exc())

        execution_time = (datetime.now() - start_time).total_seconds() * 1000

        final_step = self.step_records[-1] if self.step_records else None
        result = PipelineResult(
            trace_id=self.trace_id,
            status=self.current_status,
            steps=self.step_records,
            final_output=final_step.output_data if final_step else None,
            error_message=self._get_error_message(),
            execution_time_ms=execution_time,
            metadata={
                "total_steps": len(self.steps),
                "completed_steps": len([s for s in self.step_records if s.status == StepStatus.SUCCESS]),
                "retry_count": sum(s.retry_count for s in self.step_records),
            },
        )

        logger.info(f"Pipeline completed with status: {self.current_status.value}")
        return result

    def _check_loop_requirement(self, step_name: str) -> Optional[str]:
        """检查步骤输出是否要求回溯。

        支持：① control_signal 显式 loop_back；② 执行失败找上游反馈目标；
        ③ 字典含 passed=False；④ 字典 _control_signal 显式 loop_back。
        """
        step_config = self.steps[step_name]
        if not step_config.allow_loop_back:
            return None

        output = self.step_outputs.get(step_name)
        if not output:
            return None

        target = None
        feedback_data = None
        loop_reason = None

        if hasattr(output, "control_signal") and hasattr(output, "success"):
            # 鸭子类型：等价于 AgentResult
            if output.control_signal and output.control_signal.get("action") == "loop_back":
                target = output.control_signal.get("target_step")
                feedback_data = output.control_signal.get("feedback_data")
                loop_reason = "control_signal_loop_back"
                if not target:
                    target = self._find_feedback_target(step_name)
                    loop_reason = "auto_find_target"
            elif not output.success:
                target = self._find_feedback_target(step_name)
                feedback_data = {"error": output.error, "from_step": step_name}
                loop_reason = "execution_failed"

            if target:
                target_config = self.steps[target]
                target_exec_count = self.step_execution_counts.get(target, 0)
                logger.info(
                    f"[{loop_reason}] Checking loop back to {target}: "
                    f"count={target_exec_count}, max={target_config.max_retries}"
                )
                if target_exec_count >= target_config.max_retries:
                    logger.warning(
                        f"Loop back to {target} suppressed: "
                        f"max iterations ({target_config.max_retries}) reached"
                    )
                    return None
                if feedback_data:
                    self._send_feedback(target, feedback_data)
                return target
            return None

        if isinstance(output, dict):
            control = output.get("_control_signal")
            if control and control.get("action") == "loop_back":
                target = control.get("target_step")
                feedback_data = control.get("feedback_data")
                loop_reason = "dict_control_signal"
                if not target:
                    target = self._find_feedback_target(step_name)
                    loop_reason = "dict_auto_find_target"
            elif "passed" in output and output["passed"] is False:
                target = self._find_feedback_target(step_name)
                feedback_data = output.get("feedback", {})
                loop_reason = "evaluation_failed"
                if isinstance(feedback_data, dict):
                    feedback_data["from_step"] = step_name
                    feedback_data["evaluation_output"] = {
                        "passed": output.get("passed"),
                        "match_rate": output.get("match_rate"),
                    }

            if target:
                target_config = self.steps[target]
                target_exec_count = self.step_execution_counts.get(target, 0)
                logger.info(
                    f"[{loop_reason}] Checking loop back to {target}: "
                    f"count={target_exec_count}, max={target_config.max_retries}"
                )
                if target_exec_count >= target_config.max_retries:
                    logger.warning(
                        f"Loop back to {target} suppressed: "
                        f"max iterations ({target_config.max_retries}) reached"
                    )
                    return None
                if feedback_data:
                    self._send_feedback(target, feedback_data)
                return target

        return None

    def _send_feedback(self, target_step: str, feedback_data: Any):
        """发送反馈数据给目标步骤（含效果趋势增强）。"""
        target_config = self.steps[target_step]
        if target_config.feedback_method:
            try:
                enhanced_feedback = feedback_data if isinstance(feedback_data, dict) else {"data": feedback_data}

                for _step_name, config in self.steps.items():
                    component = config.component
                    if hasattr(component, "should_rollback") and hasattr(component, "get_iteration_trend"):
                        should_rollback, rollback_reason = component.should_rollback()
                        trend_info = component.get_iteration_trend()
                        max_iterations = (
                            component.get_max_iterations()
                            if hasattr(component, "get_max_iterations") else 5
                        )
                        enhanced_feedback["should_rollback"] = should_rollback
                        enhanced_feedback["rollback_reason"] = rollback_reason
                        enhanced_feedback["iteration_trend"] = trend_info
                        enhanced_feedback["max_iterations"] = max_iterations
                        break

                feedback_fn = getattr(target_config.component, target_config.feedback_method)
                feedback_fn(enhanced_feedback)
                logger.info(f"Feedback sent to {target_step}")
            except Exception as e:
                logger.error(f"Failed to send feedback to {target_step}: {e}")

    def _execute_step(self, step_name: str, pipeline_input: Dict) -> bool:
        step_config = self.steps[step_name]

        record = StepRecord(
            step_name=step_name,
            status=StepStatus.RUNNING,
            start_time=datetime.now(),
        )

        try:
            step_input = self._prepare_step_input(step_name, pipeline_input)
            record.input_data = step_input

            logger.info(f"Executing step: {step_name}")

            if self.step_start_callback:
                try:
                    self.step_start_callback(step_name, record)
                except Exception as e:
                    logger.error(f"Step start callback failed: {e}")

            component = step_config.component
            execute_fn = getattr(component, step_config.execute_method)
            output = execute_fn(step_input)

            if hasattr(output, "success"):
                if not output.success:
                    record.status = StepStatus.FAILED
                    record.error_info = getattr(output, "error", None) or "Agent returned failure"
                    record.end_time = datetime.now()
                    self.step_records.append(record)

                    processed_output = output.data if output.data is not None else {}
                    if output.control_signal:
                        processed_output["_control_signal"] = output.control_signal
                    self.step_outputs[step_name] = processed_output

                    logger.error(f"Step {step_name} failed: {record.error_info}")

                    if self.step_complete_callback:
                        try:
                            self.step_complete_callback(step_name, record, False)
                        except Exception as e:
                            logger.error(f"Step complete callback failed: {e}")
                    return False

                processed_output = output.data if output.data is not None else {}
                if output.control_signal:
                    processed_output["_control_signal"] = output.control_signal

                record.output_data = processed_output
                record.status = StepStatus.SUCCESS
                record.end_time = datetime.now()
                self.step_outputs[step_name] = processed_output
                self.step_records.append(record)

                logger.info(f"Step {step_name} completed successfully (AgentResult-like)")

                if self.step_complete_callback:
                    try:
                        self.step_complete_callback(step_name, record, True)
                    except Exception as e:
                        logger.error(f"Step complete callback failed: {e}")
                return True

            record.output_data = output
            record.status = StepStatus.SUCCESS
            record.end_time = datetime.now()
            self.step_outputs[step_name] = output
            self.step_records.append(record)

            logger.info(f"Step {step_name} completed successfully")

            if self.step_complete_callback:
                try:
                    self.step_complete_callback(step_name, record, True)
                except Exception as e:
                    logger.error(f"Step complete callback failed: {e}")
            return True

        except Exception as e:
            record.status = StepStatus.FAILED
            record.error_info = str(e)
            record.end_time = datetime.now()
            self.step_records.append(record)

            logger.error(f"Step {step_name} failed: {e}")

            if self.step_complete_callback:
                try:
                    self.step_complete_callback(step_name, record, False)
                except Exception as e:
                    logger.error(f"Step complete callback failed: {e}")
            return False

    def _handle_step_failure(self, failed_step_name: str) -> bool:
        failed_record = self._get_step_record(failed_step_name)
        step_config = self.steps[failed_step_name]

        if not step_config.retryable:
            logger.warning(f"Step {failed_step_name} is not retryable")
            return False

        if failed_record.retry_count >= step_config.max_retries:
            logger.warning(f"Step {failed_step_name} exceeded max retries")
            return False

        if self.enable_feedback_loop and step_config.feedback_method:
            logger.info(f"Initiating feedback loop for step: {failed_step_name}")
            upstream_step = self._find_feedback_target(failed_step_name)
            if upstream_step:
                feedback_data = self._generate_feedback(failed_step_name, failed_record)
                upstream_config = self.steps[upstream_step]
                if upstream_config.feedback_method:
                    try:
                        feedback_fn = getattr(upstream_config.component, upstream_config.feedback_method)
                        feedback_fn(feedback_data)
                        logger.info(f"Feedback sent to {upstream_step}")
                    except Exception as e:
                        logger.error(f"Failed to send feedback: {e}")

        failed_record.retry_count += 1
        failed_record.status = StepStatus.RETRYING

        logger.info(f"Retrying step {failed_step_name} (attempt {failed_record.retry_count})")

        self.step_records.remove(failed_record)
        return self._execute_step(failed_step_name, {})

    def _find_feedback_target(self, failed_step: str) -> Optional[str]:
        execution_order = self._topological_sort()
        failed_idx = execution_order.index(failed_step)

        for i in range(failed_idx - 1, -1, -1):
            step_name = execution_order[i]
            step_config = self.steps[step_name]
            if step_config.feedback_method:
                return step_name

        return None

    def _generate_feedback(self, failed_step: str, record: StepRecord) -> Dict[str, Any]:
        return {
            "from_step": failed_step,
            "trace_id": self.trace_id,
            "error_info": record.error_info,
            "retry_count": record.retry_count,
            "timestamp": datetime.now().isoformat(),
            "suggestions": self._analyze_failure(failed_step, record),
        }

    def _analyze_failure(self, step_name: str, record: StepRecord) -> List[str]:
        suggestions = []
        error_info = record.error_info or ""

        if "timeout" in error_info.lower():
            suggestions.append("考虑增加超时时间或优化处理逻辑")
        elif "parse" in error_info.lower() or "json" in error_info.lower():
            suggestions.append("输出格式不符合预期，需要调整 Prompt 或解析逻辑")
        elif "similarity" in error_info.lower():
            suggestions.append("审核结果与期望差异较大，需要优化审核规则")
        else:
            suggestions.append("需要检查组件配置和输入数据")

        return suggestions

    def _prepare_step_input(self, step_name: str, pipeline_input: Dict) -> Any:
        step_config = self.steps[step_name]

        dep_outputs = {}
        for dep in step_config.dependencies:
            if dep in self.step_outputs:
                dep_outputs[dep] = self.step_outputs[dep]

        if dep_outputs:
            return {
                "pipeline_input": pipeline_input,
                "dependencies": dep_outputs,
                "trace_id": self.trace_id,
            }
        else:
            return {
                "pipeline_input": pipeline_input,
                "trace_id": self.trace_id,
            }

    def _topological_sort(self) -> List[str]:
        in_degree = {name: 0 for name in self.steps}
        graph = {name: [] for name in self.steps}

        for name, config in self.steps.items():
            for dep in config.dependencies:
                if dep in self.steps:
                    graph[dep].append(name)
                    in_degree[name] += 1

        queue = [name for name, degree in in_degree.items() if degree == 0]
        result = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            for neighbor in graph[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(self.steps):
            raise ValueError("Circular dependency detected in pipeline steps")

        return result

    def _get_step_record(self, step_name: str) -> Optional[StepRecord]:
        for record in self.step_records:
            if record.step_name == step_name:
                return record
        return None

    def _get_error_message(self) -> Optional[str]:
        failed_steps = [s for s in self.step_records if s.status == StepStatus.FAILED]
        if failed_steps:
            return f"Steps failed: {[s.step_name for s in failed_steps]}"
        return None

    def get_execution_trace(self) -> List[Dict]:
        return [record.to_dict() for record in self.step_records]

    def get_step_output(self, step_name: str) -> Any:
        return self.step_outputs.get(step_name)
