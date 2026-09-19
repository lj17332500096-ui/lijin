"""CLI 的斜杠命令体系。

约定
----
· 每个命令是一个 `async def handler(app, arg)`；`app` 是 `cli.app.ChatApp`，
  命令只通过它的公开方法读写状态，不直接碰 Runtime 内部；
· 注册表是纯函数 `build_registry()` 的产物，测试可以单独构造与断言；
· 命令名与别名都小写，`/Help` 也能用（分发时统一小写）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from cli import theme

Handler = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    summary: str
    handler: Handler
    aliases: tuple[str, ...] = ()

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass
class CommandRegistry:
    _items: dict[str, Command] = field(default_factory=dict)

    def register(self, command: Command) -> Command:
        for key in command.keys:
            self._items[key.lower()] = command
        return command

    def get(self, name: str) -> Command | None:
        return self._items.get(str(name).strip().lower())

    def all(self) -> list[Command]:
        seen: dict[str, Command] = {}
        for command in self._items.values():
            seen.setdefault(command.name, command)
        return sorted(seen.values(), key=lambda c: c.name)

    def parse(self, line: str) -> tuple[Command | None, str]:
        """把 `/name rest...` 拆成 (命令, 参数)；未知命令返回 (None, 原文)。"""
        body = line[1:] if line.startswith("/") else line
        name, _, rest = body.partition(" ")
        return self.get(name), rest.strip()

    def help_text(self, command: Command | None = None) -> str:
        if command is not None:
            lines = [theme.head(f"/{command.name}  —— {command.summary}"), f"  用法: {command.usage}"]
            if command.aliases:
                lines.append(f"  别名: {', '.join('/' + a for a in command.aliases)}")
            return "\n".join(lines)
        width = max(len(c.usage) for c in self.all())
        lines = [theme.head("可用命令"), ""]
        for command in self.all():
            lines.append(f"  {command.usage.ljust(width)}   {command.summary}")
        lines += [
            "",
            theme.dim("普通文本直接回车即发送；行尾加 \\ 可续行；输入 /multi 进多行模式（单独一行 . 结束）。"),
        ]
        return "\n".join(lines)


# ── 参数小工具 ──────────────────────────────────────────────────
def _int_arg(arg: str, default: int) -> int:
    text = (arg or "").strip()
    if not text:
        return default
    try:
        return max(1, int(text.split()[0]))
    except (ValueError, IndexError):
        return default


def _pick(row: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return default


# ── 命令实现 ────────────────────────────────────────────────────
async def cmd_help(app, arg: str) -> None:
    if arg.strip():
        command = app.registry.get(arg.strip().lstrip("/"))
        if command is None:
            app.print(theme.warn(f"没有这个命令：/{arg.strip()}"))
            return
        app.print(app.registry.help_text(command))
        return
    app.print(app.registry.help_text())


async def cmd_sessions(app, arg: str) -> None:
    rows = app.store.list(limit=_int_arg(arg, 30))
    if not rows:
        app.print(theme.dim("还没有任何会话。直接发消息会自动建一个。"))
        return
    current = app.container_id()
    app.print(theme.head(f"会话（共 {len(rows)} 个，按最近活跃排序）"))
    for index, row in enumerate(rows, 1):
        marker = theme.ok("▶") if row.container_id == current else " "
        title = row.display_title[:36]
        latest = _pick(row.latest_run, "state", default="-")
        stamp = (row.updated_at or "")[:19].replace("T", " ")
        app.print(
            f"  {marker} {str(index).rjust(2)}. {title.ljust(36)} "
            f"{theme.dim(f'{row.messages} 条 / run {latest} / {stamp}')}"
        )
    app.print(theme.dim("用 /switch <序号|容器id|会话名> 切换。"))


async def cmd_new(app, arg: str) -> None:
    title = arg.strip()
    await app.new_session(title)
    app.print(theme.ok(f"已新建并切换到会话：{app.session_name}"))
    if title:
        app.print(theme.dim(f"标题：{title}"))


async def cmd_switch(app, arg: str) -> None:
    ref = arg.strip()
    if not ref:
        app.print(theme.warn("用法：/switch <序号|容器id|会话名>（先用 /sessions 看清单）"))
        return
    container_id = app.store.resolve(ref)
    if not container_id:
        app.print(theme.warn(f"找不到会话：{ref}"))
        return
    await app.switch_to(container_id)
    app.print(theme.ok(f"已切换到会话：{app.session_name}"))


async def cmd_rename(app, arg: str) -> None:
    title = arg.strip()
    if not title:
        app.print(theme.warn("用法：/rename <新标题>（只改当前会话）"))
        return
    if app.store.rename(app.container_id(), title):
        app.print(theme.ok(f"已重命名为：{title}"))
    else:
        app.print(theme.warn("重命名失败（会话可能已被归档）。"))


async def cmd_history(app, arg: str) -> None:
    limit = _int_arg(arg, 10)
    messages = app.store.messages(app.container_id(), limit=limit)
    if not messages:
        app.print(theme.dim("当前会话还没有历史消息。"))
        return
    app.print(theme.head(f"最近 {len(messages)} 条消息（会话 {app.session_name}）"))
    for message in messages:
        role = str(message.get("role") or "?")
        who = theme.tag("你") if role == "user" else theme.head("助手")
        stamp = str(message.get("created_at") or "")[:19].replace("T", " ")
        body = str(message.get("content") or "").strip()
        if len(body) > 600:
            body = body[:600] + "…（用 /last 看完整）"
        app.print(f"\n{who}  {theme.dim(stamp)}")
        for line in body.splitlines() or [""]:
            app.print(f"  {line}")


async def cmd_compact(app, arg: str) -> None:
    from compact import maybe_compact

    app.print(theme.dim("正在压缩较早的对话…"))
    try:
        summary = await maybe_compact(app.session)
    except RuntimeError as exc:
        if "closed" in str(exc).lower():
            app.print(theme.warn("当前会话上下文已关闭，跳过压缩。"))
            return
        raise
    if summary:
        app.print(theme.ok("已压缩：较早的对话已写成摘要（summaries/ 目录可查），最近几轮原文保留。"))
    else:
        app.print(theme.dim("当前会话还没到压缩阈值，无需压缩。"))


async def cmd_clear(app, arg: str) -> None:
    if arg.strip().lower() not in {"yes", "y", "确认", "确定"}:
        app.print(
            theme.warn("这会清空当前会话的对话记忆与消息记录（审计表保留）。")
            + "\n"
            + theme.dim("确认请再执行：/clear yes")
        )
        return
    try:
        await app.session.clear_session()
    except Exception as exc:
        app.print(theme.warn(f"清空模型上下文时出错（继续清空消息）：{exc}"))
    deleted = app.store.clear(app.container_id())
    app.print(
        theme.ok(
            f"已清空：消息 {deleted.get('messages', 0)} 条、事件 {deleted.get('events', 0)} 条、"
            f"附件 {deleted.get('attachments', 0)} 个。"
        )
    )


async def cmd_artifacts(app, arg: str) -> None:
    items = app.store.artifacts(session_id=app.session_name, limit=_int_arg(arg, 20))
    if not items:
        app.print(theme.dim("当前会话还没有登记产物。"))
        return
    app.print(theme.head(f"产物（{len(items)} 个）"))
    for item in items:
        name = _pick(item, "name", default="?")
        kind = _pick(item, "kind", default="?")
        artifact_id = _pick(item, "id", default="?")
        app.print(f"  🗂 {name} {theme.dim(f'{kind} / {artifact_id}')}")


async def cmd_last(app, arg: str) -> None:
    messages = app.store.messages(app.container_id(), limit=200)
    for message in reversed(messages):
        if str(message.get("role")) == "assistant":
            app.print(theme.head("上一条助手回复"))
            app.print(str(message.get("content") or ""))
            return
    app.print(theme.dim("当前会话还没有助手回复。"))


async def cmd_diag(app, arg: str) -> None:
    container_id = app.container_id()
    target = arg.strip()
    if target:
        resolved = app.store.resolve(target)
        if not resolved:
            app.print(theme.warn(f"找不到会话：{target}"))
            return
        container_id = resolved
    info = app.store.diagnose(container_id)
    app.print(theme.head(f"诊断 · 容器 {container_id}（会话 {app.session_name}）"))
    app.print(f"  消息 {info['messages']} 条 ｜ Run {info['runs']} 个")
    latest = info.get("latest_run") or {}
    if not latest:
        app.print(theme.dim("  还没有产生过 Run。"))
        return
    app.print(
        f"  最近 Run: {_pick(latest, 'id')} 状态={_pick(latest, 'state', default='?')} "
        f"{theme.dim(_pick(latest, 'created_at')[:19].replace('T', ' '))}"
    )
    goal = _pick(latest, "goal")
    if goal:
        app.print(f"  目标: {goal[:120]}")
    attempts = info.get("provider_attempts") or []
    app.print(theme.head(f"  Provider 尝试（最近 {len(attempts)} 次）"))
    if not attempts:
        app.print(theme.dim("    （无记录：本轮可能没走到模型调用）"))
    for attempt in attempts:
        error = _pick(attempt, "error", default="-")
        app.print(
            f"    · {_pick(attempt, 'kind', default='?')}/{_pick(attempt, 'model', default='?')} "
            f"{_pick(attempt, 'latency_ms', default='-')}ms  {theme.err(error) if error != '-' else ''}"
        )
    events = info.get("events") or []
    app.print(theme.head(f"  事件（末尾 {len(events)} 条）"))
    for event in events:
        app.print(f"    · {_pick(event, 'type', default='?')} {theme.dim(_pick(event, 'at')[:19].replace('T', ' '))}")
    artifacts = info.get("artifacts") or []
    if artifacts:
        app.print(theme.head(f"  产物（{len(artifacts)} 个）"))
        for item in artifacts:
            app.print(f"    · {item.get('name')} ({item.get('kind')})")


async def cmd_tools(app, arg: str) -> None:
    limit = _int_arg(arg, 10)
    info = app.store.diagnose(app.container_id(), event_limit=1, attempt_limit=1)
    run_id = _pick(info.get("latest_run") or {}, "id")
    if not run_id:
        app.print(theme.dim("当前会话还没有 Run 记录。"))
        return
    try:
        rows = app.store.mgr.list_tool_calls(run_id, limit=limit) or []
    except Exception as exc:
        app.print(theme.warn(f"读取工具调用失败：{exc}"))
        return
    if not rows:
        app.print(theme.dim("最近一次 Run 没有工具调用记录。"))
        return
    app.print(theme.head(f"最近一次 Run 的工具调用（{len(rows)} 条）"))
    for row in rows:
        app.print(
            f"  · {_pick(row, 'tool_name', 'name', default='?')} "
            f"状态={_pick(row, 'status', default='?')} "
            f"{theme.dim(_pick(row, 'created_at')[:19].replace('T', ' '))}"
        )


async def cmd_debug(app, arg: str) -> None:
    text = arg.strip().lower()
    app.debug = not app.debug if text in {"", "toggle"} else text in {"1", "on", "true", "yes"}
    app._sync_backend_prints()
    app.print(theme.ok(f"debug 已{'开启' if app.debug else '关闭'}"))
    if app.debug:
        app.print(theme.dim("开启后会打印 SDK 原始流事件类型与内部条目，输出会比较长。"))


async def cmd_mode(app, arg: str) -> None:
    text = arg.strip().lower()
    if not text:
        app.print(f"当前执行方式：{theme.tag(app.mode)}（可选 stream / async / sync）")
        return
    if text not in {"stream", "async", "sync"}:
        app.print(theme.warn("用法：/mode stream|async|sync"))
        return
    app.mode = text
    app.print(theme.ok(f"执行方式已切换为：{text}"))


async def cmd_status(app, arg: str) -> None:
    row = app.store.get(app.container_id())
    app.print(theme.head("当前状态"))
    app.print(f"  会话名: {app.session_name}")
    app.print(f"  容器  : {app.container_id()}")
    app.print(f"  执行方式: {app.mode} ｜ debug: {app.debug} ｜ 单轮上限: {app.max_turns}")
    app.print(f"  自动摘要: {'开' if app.auto_summary else '关'} ｜ 历史回看: {app.history_limit or '完整'}")
    if row:
        latest = _pick(row.latest_run, "state", default="-")
        app.print(f"  消息 {row.messages} 条 ｜ Run {row.runs} 个 ｜ 最近状态 {latest}")


async def cmd_episode(app, arg: str) -> None:
    """/episode —— 情节记忆（episodic memory）运维。

    记忆层是**后置采集**的：所有 Run 终态都已落在 task_events.run.terminal 里，
    这里只是把未采集的补进 episodes 表，因此任何时刻重跑都安全且幂等。
    """
    sub, _, rest = (arg or "").strip().partition(" ")
    sub = sub.lower()

    try:
        from runtime.episode_store import EpisodeStore

        store = EpisodeStore()
    except Exception as exc:  # 依赖缺失不该炸掉 CLI
        app.print(theme.warn(f"记忆层不可用：{exc}"))
        return

    if sub in ("", "stats"):
        try:
            st = store.stats()
        except Exception as exc:
            app.print(theme.warn(f"读取失败：{exc}"))
            return
        app.print(theme.head("情节记忆"))
        app.print(f"  总量: {st['total']} 条 ｜ schema v{st['schema_version']}")
        app.print(f"  库  : {st['db_path']}")
        if st["by_outcome"]:
            detail = "、".join(f"{k} {v}" for k, v in sorted(st["by_outcome"].items()))
            app.print(f"  终态: {detail}")
        if st["by_intent"]:
            detail = "、".join(f"{k} {v}" for k, v in list(st["by_intent"].items())[:8])
            app.print(f"  意图: {detail}")
        from runtime.episode_recall import enabled

        app.print(f"  注入: {'开' if enabled() else '关'}")
        return

    if sub == "ingest":
        try:
            stats = store.ingest_pending()
        except Exception as exc:
            app.print(theme.warn(f"采集失败：{exc}"))
            return
        app.print(
            f"  扫描 {stats['scanned']} 条终态事件 → 新增 {stats['inserted']} / "
            f"跳过 {stats['skipped']} / 失败 {stats['failed']}"
        )
        return

    if sub == "reindex":
        app.print(theme.warn("  正在按当前分词规则全量重建（清表后从原始事件重算）…"))
        try:
            stats = store.reindex()
        except Exception as exc:
            app.print(theme.warn(f"重建失败：{exc}"))
            return
        app.print(f"  重建完成：新增 {stats['inserted']} 条 / 失败 {stats['failed']}")
        return

    if sub in ("recall", "查", "find"):
        if not rest:
            app.print(theme.warn("  用法：/episode recall <请求文本>"))
            return
        from runtime.episode_recall import build_context

        block = build_context(rest)
        app.print(block or "  没有召回任何历史经验（低于相似度阈值）。")
        return

    if sub in ("on", "off"):
        import os

        os.environ["EPISODE_RECALL"] = "1" if sub == "on" else "0"
        app.print(f"  已{'开启' if sub == 'on' else '关闭'}情节记忆注入（本次进程内有效）。")
        return

    if sub == "clear":
        if rest.lower() != "yes":
            app.print(theme.warn("  这会清空全部情节记忆；确认请执行 /episode clear yes"))
            return
        app.print(f"  已清空 {store.clear()} 条。")
        return

    app.print(theme.warn("  子命令：stats | ingest | reindex | recall <文本> | on | off | clear yes"))


async def cmd_exit(app, arg: str) -> None:
    app.request_exit = True
    app.print("再见，随时回来找我。")


# ── 注册表 ─────────────────────────────────────────────────────
def build_registry() -> CommandRegistry:
    """构造命令注册表（纯函数，测试可直接断言）。"""
    registry = CommandRegistry()
    for command in (
        Command("help", "/help [命令]", "查看命令清单或某个命令的用法", cmd_help, ("?", "h")),
        Command("status", "/status", "看当前会话与运行参数", cmd_status),
        Command("sessions", "/sessions [数量]", "列出会话（最近活跃在前）", cmd_sessions, ("ls",)),
        Command("new", "/new [标题]", "新建会话并切过去", cmd_new),
        Command("switch", "/switch <序号|id|会话名>", "切换会话", cmd_switch, ("sw",)),
        Command("rename", "/rename <新标题>", "重命名当前会话", cmd_rename),
        Command("history", "/history [条数]", "查看当前会话最近的消息", cmd_history, ("hist",)),
        Command("last", "/last", "重看上一条助手回复", cmd_last),
        Command("compact", "/compact", "手动压缩较早的对话（生成摘要）", cmd_compact),
        Command("clear", "/clear yes", "清空当前会话的对话记忆与消息", cmd_clear),
        Command("artifacts", "/artifacts [数量]", "列出当前会话登记的产物", cmd_artifacts, ("arts",)),
        Command("tools", "/tools [条数]", "看最近一次 Run 的工具调用", cmd_tools),
        Command("diag", "/diag [会话]", "诊断：最近 Run、provider 尝试、事件时间线", cmd_diag),
        Command("episode", "/episode [stats|ingest|reindex|recall <文本>|on|off|clear yes]",
                "情节记忆：把历史 Run 的成功/失败经验攒起来", cmd_episode, ("mem",)),
        Command("debug", "/debug [on|off]", "开关内部事件打印", cmd_debug),
        Command("mode", "/mode [stream|async|sync]", "切换执行方式", cmd_mode),
        Command("exit", "/exit", "退出 CLI（同 /quit）", cmd_exit, ("quit", "q")),
    ):
        registry.register(command)
    return registry
