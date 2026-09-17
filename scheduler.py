"""定时任务：解析计划表达式、读写 tasks.json、计算下次运行时间。

支持三种写法：
  - 每天：            "08:30"
  - 指定星期：        "周一 09:00" / "Mon 09:00" / "星期天 08:00"
  - 标准 cron 五段：   "30 8 * * 1-5"（分 时 日 月 周，周 0/7=周日）

本模块不依赖任何第三方库；tasks.json 默认放在项目根目录。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
TASKS_PATH = BASE_DIR / "tasks.json"

_CN_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_EN_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_TIME_RE = re.compile(r"(\d{1,2}):(\d{1,2})")
_CN_WEEK_RE = re.compile(r"(?:周|星期|礼拜)([一二三四五六日天])")


def _now() -> datetime:
    return datetime.now()


def _cron_weekday_to_python(value: int) -> int:
    """cron 习惯周日=0/7，转成 Python weekday（周一=0）。"""
    return 6 if value in (0, 7) else value - 1


def _parse_cron_field(token: str, low: int, high: int) -> set[int]:
    if token == "*":
        return set(range(low, high + 1))
    values: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = int(raw_step)
            if step <= 0:
                raise ValueError(f"步长必须为正数：{token}")
        if part == "*":
            base = list(range(low, high + 1))
        elif "-" in part:
            a_text, b_text = part.split("-", 1)
            a, b = int(a_text), int(b_text)
            if a > b:
                raise ValueError(f"区间无效：{token}")
            base = list(range(a, b + 1))
        else:
            base = [int(part)]
        for index, value in enumerate(base):
            if index % step == 0:
                if value < low or value > high:
                    raise ValueError(f"取值超出范围 {low}-{high}：{token}")
                values.add(value)
    return values


def parse_schedule(text: str) -> dict[str, Any]:
    """把计划表达式解析成规范字典：minute/hour/day/month/weekday 集合 + 原始描述。"""
    original = text.strip()
    if not original:
        raise ValueError("计划不能为空，例如：08:30、周一 09:00、30 8 * * 1-5")
    low = original.lower()

    time_match = _TIME_RE.search(original)
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"时间无效：{original}")
        weekday: set[int] | None = None
        week_match = _CN_WEEK_RE.search(original)
        if week_match:
            weekday = {_CN_WEEKDAYS[week_match.group(1)]}
        else:
            for word in _EN_WEEKDAYS:
                if word in low:
                    weekday = {_EN_WEEKDAYS[word]}
                    break
        return {
            "source": original,
            "kind": "cron",
            "minute": {minute},
            "hour": {hour},
            "day": set(range(1, 32)),
            "month": set(range(1, 13)),
            "weekday": weekday,
        }

    fields = original.split()
    if len(fields) != 5:
        raise ValueError(
            "计划无法识别。支持：08:30（每天）、周一 09:00（指定星期）、"
            "30 8 * * 1-5（cron 五段）"
        )
    minute_set = _parse_cron_field(fields[0], 0, 59)
    hour_set = _parse_cron_field(fields[1], 0, 23)
    day_set = _parse_cron_field(fields[2], 1, 31)
    month_set = _parse_cron_field(fields[3], 1, 12)
    dow_raw = _parse_cron_field(fields[4], 0, 7)
    weekday = {_cron_weekday_to_python(v) for v in dow_raw}
    return {
        "source": original,
        "kind": "cron",
        "minute": minute_set,
        "hour": hour_set,
        "day": day_set,
        "month": month_set,
        "weekday": weekday,
    }


def _matches(spec: dict[str, Any], moment: datetime) -> bool:
    if moment.minute not in spec["minute"]:
        return False
    if moment.hour not in spec["hour"]:
        return False
    if moment.day not in spec["day"]:
        return False
    if moment.month not in spec["month"]:
        return False
    weekday = spec.get("weekday")
    if weekday is not None and moment.weekday() not in weekday:
        return False
    return True


def next_run_after(spec: dict[str, Any], after: datetime | None = None, max_days: int = 400) -> datetime:
    """计算 spec 在 after（默认当前时间）之后最近的一次执行时间（分钟精度）。"""
    after = after or _now()
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = max_days * 24 * 60
    for _ in range(limit):
        if _matches(spec, cursor):
            return cursor
        cursor += timedelta(minutes=1)
    raise ValueError("无法在 400 天内找到下一次执行时间，请检查计划表达式")


def load_tasks(path: str | Path | None = None) -> list[dict[str, Any]]:
    file_path = Path(path) if path else TASKS_PATH
    if not file_path.exists():
        return []
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("tasks", []) if isinstance(data, dict) else []


def save_tasks(tasks: list[dict[str, Any]], path: str | Path | None = None) -> None:
    file_path = Path(path) if path else TASKS_PATH
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _find_task(tasks: list[dict], task_id: str) -> dict | None:
    return next((t for t in tasks if t["id"] == task_id), None)


def add_task(
    name: str,
    schedule: str,
    prompt: str,
    enabled: bool = True,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """新增一条定时任务并返回任务记录。"""
    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not name or not prompt:
        raise ValueError("任务名称和提示内容都不能为空")
    spec = parse_schedule(schedule)
    now = _now()
    tasks = load_tasks(path)
    task = {
        "id": "task_" + uuid.uuid4().hex[:8],
        "name": name,
        "schedule": spec["source"],
        "prompt": prompt,
        "enabled": enabled,
        "created_at": now.isoformat(timespec="seconds"),
        "last_run": None,
        "last_status": None,
        "last_summary": None,
        "next_run": next_run_after(spec, now).isoformat(timespec="seconds"),
    }
    tasks.append(task)
    save_tasks(tasks, path)
    return task


def remove_task(task_id: str, path: str | Path | None = None) -> bool:
    tasks = load_tasks(path)
    remaining = [t for t in tasks if t["id"] != task_id]
    if len(remaining) == len(tasks):
        return False
    save_tasks(remaining, path)
    return True


def set_task_enabled(task_id: str, enabled: bool, path: str | Path | None = None) -> dict | None:
    tasks = load_tasks(path)
    task = _find_task(tasks, task_id)
    if task is None:
        return None
    task["enabled"] = bool(enabled)
    if enabled and not task.get("next_run"):
        try:
            spec = parse_schedule(task["schedule"])
            task["next_run"] = next_run_after(spec).isoformat(timespec="seconds")
        except ValueError:
            task["next_run"] = None
    save_tasks(tasks, path)
    return task


def due_tasks(now: datetime | None = None, path: str | Path | None = None) -> list[dict[str, Any]]:
    """返回到当前时间该执行的任务（enabled 且 next_run 已到）。"""
    now = now or _now()
    return [
        t
        for t in load_tasks(path)
        if t.get("enabled") and t.get("next_run") and now >= datetime.fromisoformat(t["next_run"])
    ]


def prepare_next_run(task_id: str, now: datetime | None = None, path: str | Path | None = None) -> dict | None:
    """执行前先推进 next_run，避免执行期间重复触发；返回更新后的任务。"""
    now = now or _now()
    tasks = load_tasks(path)
    task = _find_task(tasks, task_id)
    if task is None:
        return None
    try:
        spec = parse_schedule(task["schedule"])
        task["next_run"] = next_run_after(spec, now).isoformat(timespec="seconds")
    except ValueError:
        task["next_run"] = None
    save_tasks(tasks, path)
    return task


def mark_run_result(
    task_id: str,
    status: str,
    summary: str,
    path: str | Path | None = None,
) -> dict | None:
    tasks = load_tasks(path)
    task = _find_task(tasks, task_id)
    if task is None:
        return None
    task["last_run"] = _now().isoformat(timespec="seconds")
    task["last_status"] = status
    task["last_summary"] = (summary or "")[:500]
    save_tasks(tasks, path)
    return task


def format_task_line(task: dict[str, Any]) -> str:
    status = "启用" if task.get("enabled") else "停用"
    last = task.get("last_summary") or "尚未运行"
    return (
        f"- {task['id']}｜{task['name']}｜{task['schedule']}｜{status}\n"
        f"  提示：{task['prompt'][:100]}\n"
        f"  下次：{task.get('next_run') or '未安排'} ｜ 上次：{task.get('last_run') or '无'}（{last}）"
    )
