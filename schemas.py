"""Agent 回复的固定 JSON 结构（结构化输出）。

除文字主体外，可携带可选的「交互卡片」数组（ui），供网页端渲染成
指标卡/图表/表格/清单/表单/文件清单等白名单组件；终端模式只显示文字。
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 交互卡片（生成式 UI）数据模型——网页端按 type 白名单渲染，绝不渲染任意 HTML
# ---------------------------------------------------------------------------


class UiMetric(BaseModel):
    """指标卡：大数字 + 说明。"""

    type: Literal["metric"]
    title: str = Field(default="", description="指标名")
    value: str = Field(default="", description="主数值（已格式化好的文本）")
    hint: str | None = Field(default=None, description="副标题/说明")
    delta: str | None = Field(default=None, description="变化提示，如 +12% / 较上月")
    tone: Literal["up", "down", "flat"] | None = Field(default=None, description="趋势色调")


class UiSeries(BaseModel):
    name: str = Field(default="", description="系列名")
    values: list[int | float] = Field(default_factory=list, description="与 labels 等长的数值")


class UiChart(BaseModel):
    """柱状/折线/饼图：labels + 一到多组 series。"""

    type: Literal["bar", "line", "pie"]
    title: str = Field(default="", description="图表标题")
    labels: list[str] = Field(default_factory=list, description="类别（x 轴/饼图分块）")
    series: list[UiSeries] = Field(default_factory=list, description="数据系列（饼图只取第一组）")
    unit: str | None = Field(default=None, description="数值单位后缀，如 元 / %")
    note: str | None = Field(default=None, description="底部说明（来源、口径等）")


class UiTable(BaseModel):
    """表格：列定义 + 行数据。"""

    type: Literal["table"]
    title: str = Field(default="", description="表格标题")
    columns: list[str | int | float | bool] = Field(default_factory=list, description="列名")
    rows: list[list[str | int | float | bool]] = Field(
        default_factory=list, description="行数据，每行长度需与 columns 一致"
    )
    note: str | None = Field(default=None, description="底部说明")


class UiTodoItem(BaseModel):
    label: str = Field(description="事项描述")
    done: bool = False


class UiTodo(BaseModel):
    """待办清单：网页端可勾选（勾选即回灌成新消息）。"""

    type: Literal["todo"]
    title: str = Field(default="", description="清单标题")
    items: list[UiTodoItem] = Field(default_factory=list)
    note: str | None = None


class UiFormField(BaseModel):
    """表单字段；type 决定网页端控件。"""

    name: str = Field(description="字段标识（机器用，回灌消息里用）")
    label: str | None = Field(default=None, description="展示名，缺省用 name")
    type: Literal["text", "number", "select", "bool"] = "text"
    options: list[str] = Field(default_factory=list, description="select 的可选项")
    value: str | int | float | bool | None = Field(default=None, description="当前值")


class UiForm(BaseModel):
    """可编辑表单：用户改完点“应用”后，把字段值作为一条新消息回灌给 Agent。"""

    type: Literal["form"]
    title: str = Field(default="", description="表单标题")
    fields: list[UiFormField] = Field(default_factory=list)
    action_label: str = Field(default="应用修改", description="提交按钮文字")
    hint: str | None = Field(default=None, description="说明：改完会发生什么")


class UiFileItem(BaseModel):
    name: str = Field(default="", description="文件名/相对路径")
    size: str | int | None = Field(default=None, description="大小（可给文本）")
    hint: str | None = Field(default=None, description="说明（如来自哪个目录）")


class UiFileList(BaseModel):
    """文件/仓库文件清单：网页端提供「查看」「对话」等按钮（回灌成新消息）。"""

    type: Literal["file_list"]
    title: str = Field(default="", description="清单标题")
    files: list[UiFileItem] = Field(default_factory=list)
    note: str | None = Field(default=None, description="底部说明（如存放目录）")


UiBlock = Annotated[
    Union[
        UiMetric,
        UiChart,
        UiTable,
        UiTodo,
        UiForm,
        UiFileList,
    ],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Agent 每轮回复
# ---------------------------------------------------------------------------


class AgentReply(BaseModel):
    """Agent 每一轮回复必须符合这个结构。"""

    kind: Literal["answer", "plan", "note", "questions", "done"] = Field(
        description="回复类型：answer=普通回答/汇报，plan=方案/计划，note=内容产出并已保存文件，questions=需要澄清，done=任务收尾"
    )
    summary: str = Field(description="给用户的一句话摘要")
    public_summary: str | None = Field(default=None, max_length=120,
        description="无重要新事实时为 null；否则仅 1–2 句已确认事实与下一步，禁止推理、自言自语、猜测和敏感参数")
    content: str = Field(description="主体内容：回答正文 / 方案全文 / 产出内容全文")
    questions: list[str] = Field(
        default_factory=list,
        description="需要用户澄清的问题列表；没有澄清需求时为空数组",
    )
    saved_file: str | None = Field(
        default=None,
        description="若用 save_note 保存了文件产出，填工具返回的文件路径；否则为 null",
    )
    next_step: str | None = Field(
        default=None,
        description="建议用户下一步做什么；没有明确建议时为 null",
    )
    ui: list[UiBlock] = Field(
        default_factory=list,
        description="可选的交互卡片数组（metric/bar/line/pie/table/todo/form/file_list），"
        "用于网页端渲染；终端忽略。没有合适卡片时为空数组",
    )
    readiness: dict | None = Field(
        default=None,
        description="任务准备度（可选，仅供观测与约束）：{status: READY|DISCOVERABLE|NEEDS_USER|UNKNOWN,"
        " missing_count: int, missing: [{name, reason}], reason: str}；NEEDS_USER 必须给出"
        " missing 字段名并让 questions 点名缺失项（禁止只写“请补充更多信息”）；普通问答不填",
    )
