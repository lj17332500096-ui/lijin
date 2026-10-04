"""CLI 消息平台主循环。

定位
----
CLI 消息入口：进程内直连 `AgentRuntime.run_turn`，不经 HTTP，
所以拿到的是原始异常、真实耗时与完整过程事件。Textual TUI 复用本模块的 ChatApp。

一条消息的生命周期
------------------
    读入（可多行） → run_turn(stream, stream_events_cb=渲染器)
        ├─ 过程：工具活动行 + 终稿正文流式分片
        ├─ 异常：Diagnosis 定性（E-XXX + 人话 + 下一步）
        └─ 等待审批：复用 main 的交互式审批，批准后自动续跑同一 Run
    → 最终答复（与流式正文去重）→ 产物登记 → 自动摘要 → 回到输入

会话模型
--------
`session_name` 同时是「LLM 上下文键」和「容器 session_id」，所以切换会话 = 换一个
`SQLiteSession` + 换它对应的容器；历史与产物都由 `cli.store.SessionStore` 从 agent.db 读。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from datetime import datetime

from agents.memory import SQLiteSession

from cli import theme
from cli.commands import build_registry
from cli.render import TurnRenderer
from cli.store import SessionStore
from runtime.errors import StartupGuardBlocked
from runtime.startup_guard import format_block_message

#: 审批护栏拒绝启动时的进程退出码。选 78（EX_CONFIG）而不是 1：
#: 1 在本项目里表示"一般运行失败"，无法把"配置导致的拒绝启动"与
#: "Run 执行失败"区分开 —— 而这两者的处置完全不同（改配置 vs 看报错）。
EXIT_GUARD_BLOCKED = 78


def _same_text(left: str, right: str) -> bool:
    """忽略空白后比较两段文本（判断流式正文与最终答复是否同一份）。"""
    if not right:
        return False
    squeeze = lambda text: re.sub(r"\s+", "", text or "")  # noqa: E731
    return squeeze(left) == squeeze(right)


def _stdin_has_pending() -> bool:
    """Windows 控制台里是否还有已缓冲的输入（用于把粘贴进来的多行合并成一条消息）。"""
    if os.name != "nt":
        return False
    try:
        import msvcrt

        return bool(msvcrt.kbhit())
    except Exception:
        return False


class ChatApp:
    """CLI 消息平台。构造参数全部可选，便于测试注入替身。"""

    def __init__(
        self,
        *,
        session_name: str = "personal",
        mode: str = "stream",
        debug: bool = False,
        max_turns: int = 20,
        history_limit: int | None = None,
        auto_summary: bool = True,
        runtime: object | None = None,
        store: SessionStore | None = None,
        stream=None,
        input_fn=None,
        sessions_db: str | None = None,
        show_activity: bool = True,
        show_phases: bool = False,
        merge_paste: bool = True,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.input_fn = input_fn or input
        self.mode = mode
        self.debug = debug
        self.max_turns = max_turns
        self.history_limit = history_limit
        self.auto_summary = auto_summary
        self.session_name = session_name or "personal"

        self.registry = build_registry()
        self.request_exit = False
        self.turns = 0
        self.last_diagnosis = None

        self._runtime = runtime
        self._store = store
        self._sessions_db = sessions_db
        self._show_activity = show_activity
        self._show_phases = show_phases
        self._merge_paste = merge_paste
        self._session: SQLiteSession | None = None
        self._container_id: str | None = None

    # ── 依赖装配 ────────────────────────────────────────────────
    @property
    def runtime(self):
        if self._runtime is None:
            from runtime.runner import AgentRuntime

            self._runtime = AgentRuntime.get_default()
        return self._runtime

    @property
    def store(self) -> SessionStore:
        if self._store is None:
            runtime = self.runtime
            runtime._ensure()
            self._store = SessionStore(runtime.tasks)
        return self._store

    @property
    def session(self) -> SQLiteSession:
        if self._session is None:
            self._session = self._open_session(self.session_name)
        return self._session

    def _open_session(self, name: str) -> SQLiteSession:
        db_path = self._sessions_db
        if db_path is None:
            from main import SESSIONS_DB

            db_path = str(SESSIONS_DB)
        return SQLiteSession(name, db_path=str(db_path))

    def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def container_id(self) -> str:
        """当前会话的容器 id（不存在则创建，保证 /status 之类命令总有值可显示）。"""
        try:
            if self._container_id:
                existing = self.store.mgr.get_container(self._container_id)
                if existing is not None and existing.get("session_id") == self.session_name:
                    return self._container_id
            container = self.store.mgr.get_or_create_container(self.session_name)
            self._container_id = str(container.get("id") or "") or None
            return self._container_id or ""
        except Exception:
            return ""

    def _sync_backend_prints(self) -> None:
        """把「过程输出」的归属交给本平台：非 debug 时关掉后端的裸 print。

        后端（main._run_attempt）历史上会直接 print `[调用工具: xxx]` / `[流事件]`。
        那属于表现层，在本平台里会与渲染层重复输出并破坏行状态；debug 模式下保留，
        因为那时就是要看 SDK 底层事件。
        """
        try:
            from main import set_process_prints

            set_process_prints(self.debug)
        except Exception:
            pass

    # ── 输出 ────────────────────────────────────────────────────
    def print(self, text: object = "") -> None:
        try:
            self.stream.write(str(text) + "\n")
            self.stream.flush()
        except Exception:
            pass

    def banner(self) -> None:
        self.print("=" * 62)
        self.print(theme.head("  全能助手 · CLI 消息平台（后端优化阶段入口）"))
        self.print(f"  会话: {theme.tag(self.session_name)}  ｜ 容器: {self.container_id() or '-'}")
        self.print(
            f"  执行方式: {self.mode} ｜ 单轮上限: {self.max_turns} ｜ "
            f"自动摘要: {'开' if self.auto_summary else '关'}"
        )
        self.print(theme.dim("  直接输入回车即发送 ｜ /help 看命令 ｜ /exit 退出"))
        self.print(theme.dim("  网页界面已停用（原因与开启方式见 ui_frozen.py）"))
        self.print("=" * 62)

    # ── 输入 ────────────────────────────────────────────────────
    def _read_message(self) -> str:
        first = self.input_fn("你 > ")
        if first is None:
            raise EOFError
        if first.strip() == "/multi":
            self.print(theme.dim("（多行模式：单独一行 . 结束）"))
            buffer: list[str] = []
            while True:
                line = self.input_fn("... ")
                if line is None:
                    raise EOFError
                if line.strip() == ".":
                    break
                buffer.append(line)
            return "\n".join(buffer).strip()

        chunks = [first]
        while chunks[-1].rstrip().endswith("\\"):
            # 行尾反斜杠 = 续行：去掉反斜杠与其前的多余空白，换成换行
            chunks[-1] = chunks[-1].rstrip()[:-1].rstrip()
            nxt = self.input_fn("... ")
            if nxt is None:
                raise EOFError
            chunks.append(nxt)
        if self._merge_paste:
            while _stdin_has_pending():
                extra = self.input_fn("")
                if extra is None:
                    break
                chunks.append(extra)
        return "\n".join(chunks).strip()

    # ── 主循环 ──────────────────────────────────────────────────
    async def run(self) -> int:
        theme.configure(stream=self.stream)
        self._sync_backend_prints()
        self.banner()
        while not self.request_exit:
            try:
                line = self._read_message()
            except EOFError:
                self.print("\n（输入结束）" + theme.dim("再见，随时回来找我。"))
                break
            except KeyboardInterrupt:
                self.print("\n" + theme.dim("（已忽略 Ctrl+C；输入 /exit 或按 Ctrl+D 退出）"))
                continue
            if not line:
                continue
            if line.strip().lower() in {"exit", "quit", "退出"}:
                self.print("再见，随时回来找我。")
                break
            if line.startswith("/"):
                try:
                    await self.dispatch(line)
                except KeyboardInterrupt:
                    self.print("\n" + theme.dim("（命令已中断）"))
                except Exception as exc:  # 命令自身出错不该终结整个会话
                    self._report_error(exc)
                continue
            await self.submit(line)
        self._close_session()
        return 0

    async def dispatch(self, line: str) -> None:
        command, arg = self.registry.parse(line)
        if command is None:
            self.print(theme.warn(f"未知命令 {line.split()[0]}；/help 看清单。"))
            return
        if command.handler is None:  # pragma: no cover - 注册表保证有处理器
            return
        await command.handler(self, arg)

    # ── 一轮对话 ────────────────────────────────────────────────
    async def submit(self, text: str) -> None:
        self.turns += 1
        self._sync_backend_prints()
        renderer = TurnRenderer(
            self.stream, show_activity=self._show_activity, show_phases=self._show_phases
        )
        started = time.monotonic()
        try:
            result = await self.runtime.run_turn(
                text,
                session=self.session,
                session_id=self.session_name,
                mode=self.mode,
                debug=self.debug,
                max_turns=self.max_turns,
                history_limit=self.history_limit,
                metadata={"channel": "cli"},
                raise_on_error=True,
                context_guard=self.auto_summary,
                stream_events_cb=renderer.on_event,
            )
        except KeyboardInterrupt:
            renderer.finish()
            self.print("\n" + theme.warn("已中断本地等待。后端 Run 可能仍在执行，用 /diag 看状态。"))
            return
        except Exception as exc:
            renderer.finish()
            self._report_error(exc)
            return

        renderer.finish()

        if getattr(result, "waiting_approval", False):
            result = await self._resolve_approvals(result)
            if getattr(result, "waiting_approval", False):
                self.print(theme.warn("仍停留在「等待审批」：可重发同样的请求继续，或用 /diag 查看明细。"))
                return

        self._render_result(result, renderer, started)
        await self._maybe_compact()

    async def _resolve_approvals(self, result):
        from main import _resolve_approvals_interactive

        return await _resolve_approvals_interactive(
            self.runtime,
            result,
            session=self.session,
            mode=self.mode,
            debug=self.debug,
            max_turns=self.max_turns,
            history_limit=self.history_limit,
        )

    def _render_result(self, result, renderer: TurnRenderer, started: float) -> None:
        from main import _KIND_LABELS, _clean_display_text, coerce_reply
        from schemas import AgentReply

        reply = coerce_reply(getattr(result, "final_output", None))
        streamed = renderer.streamed_text()
        elapsed = float(getattr(result, "elapsed_seconds", 0) or 0) or (time.monotonic() - started)

        if isinstance(reply, AgentReply):
            content = _clean_display_text(reply.content or "")
            self.print("")
            self.print(f"── {_KIND_LABELS.get(reply.kind, reply.kind)} ──")
            if reply.summary:
                self.print(f"📌 {reply.summary}")
            if reply.questions:
                self.print("")
                for index, question in enumerate(reply.questions, 1):
                    self.print(f"  {index}. {question}")
            if content and not _same_text(content, streamed):
                self.print("")
                self.print(content)
            if reply.saved_file:
                self.print("")
                self.print(f"💾 已保存: {reply.saved_file}")
            if reply.next_step:
                self.print("")
                self.print(f"➡️ 下一步: {reply.next_step}")
        else:
            text = str(reply)
            if text.strip() and not _same_text(text, streamed):
                self.print("")
                self.print(text)

        error_text = str(getattr(result, "error", "") or "").strip()
        if getattr(result, "ok", True) is False or error_text:
            self.print(theme.err(f"⚠ 本轮未成功：{error_text[:400] or '未知原因'}"))

        for artifact in getattr(result, "artifacts", []) or []:
            self.print(
                f"🗂 产物已登记：{artifact.get('name', '?')}"
                f"（{artifact.get('id', '?')}，{artifact.get('kind', '?')}）"
            )

        task_id = getattr(getattr(result, "task", None), "id", "") or ""
        parts = [f"⏱ {elapsed:.1f}s", f"run={task_id or '?'}", f"过程活动 {len(renderer.activity_lines)} 条"]
        if renderer.failed_activities:
            parts.append(f"失败活动 {len(renderer.failed_activities)} 条")
        self.print(theme.dim("  " + " ｜ ".join(parts)))

    async def _maybe_compact(self) -> None:
        if not self.auto_summary:
            return
        from main import _auto_compact

        try:
            await _auto_compact(self.session, True)
        except Exception as exc:
            if "closed" not in str(exc).lower():
                self.print(theme.dim(f"（自动摘要跳过：{exc}）"))

    def _report_error(self, exc: BaseException) -> None:
        from cli.diagnostics import diagnose

        conclusion = diagnose(exc)
        self.last_diagnosis = conclusion
        self.print("")
        self.print(theme.err("⚠ ") + theme.err(conclusion.head()))
        if conclusion.detail:
            self.print(theme.dim(f"  详情：{conclusion.detail}"))
        for hint in conclusion.hints:
            self.print(f"  · {hint}")

    # ── 会话切换 ────────────────────────────────────────────────
    async def new_session(self, title: str = "") -> None:
        self._close_session()
        self.session_name = self._next_session_name()
        try:
            row = self.store.mgr.get_or_create_container(self.session_name, title=title or None)
            self._container_id = str(row.get("id") or "") or None
        except Exception:
            self._container_id = None

    async def new_project(self, name: str, root_path: str) -> str:
        """创建一个严格 Project 会话并把所选目录绑定为 WorkLocation。"""
        from pathlib import Path
        import uuid

        title = (name or Path(root_path).name or "新项目").strip()[:120]
        resolved_root = str(Path(root_path).expanduser().resolve(strict=True))
        if not Path(resolved_root).is_dir():
            raise ValueError("项目工作位置必须是现有文件夹。")
        session_name = f"proj-{uuid.uuid4().hex[:12]}"
        container = self.store.mgr.get_or_create_container(session_name, title=title)
        container_id = str(container.get("id") or "")
        if not container_id:
            raise RuntimeError("创建项目会话失败。")
        work_location = self.store.mgr.create_work_location(
            title, local_path=resolved_root, permission_profile="read-write"
        )
        updated = self.store.mgr.update_project(
            container_id,
            name=title,
            memory_scope="project_only",
            work_location_id=str(work_location.get("id") or ""),
        )
        if not updated or updated.get("work_location_id") != work_location.get("id"):
            raise RuntimeError("项目已创建，但工作文件夹绑定未成功。")
        self._close_session()
        self.session_name = session_name
        self._container_id = container_id
        return container_id

    async def switch_to(self, container_id: str) -> None:
        row = self.store.get(container_id)
        if row is None:
            raise ValueError(f"找不到会话容器：{container_id}")
        self._close_session()
        self.session_name = row.session_id or "personal"
        self._container_id = row.container_id

    def _next_session_name(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = f"cli-{stamp}"
        existing = {row.session_id for row in self.store.list(limit=500)}
        suffix = 1
        while candidate in existing:
            suffix += 1
            candidate = f"cli-{stamp}-{suffix}"
        return candidate


def run_cli(**kwargs) -> int:
    """同步入口：给 main.py 调用。

    P3 返工项 2：护栏阻断必须**穿透到进程顶层**，以非零退出码结束，
    而不是被 `ChatApp.container_id()` 之类的 `except Exception:` 兜底吞掉
    （实测：APPROVAL=off 时横幅正常、退出码 0、报错推迟到第一次 Run
    并被包装成 E-UNKNOWN —— 护栏在最典型的绕过路径上失效）。

    `StartupGuardBlocked` 刻意继承 `BaseException` 而非 `Exception`，
    所以任何"局部失败不影响整体"的兜底都捕不到它；这里显式接住并渲染
    成可读提示。**方向不对称是刻意的**：宁可在这里友好退出，
    也不可让安全阻断被静默吞掉。
    """
    try:
        app = ChatApp(**kwargs)
    except StartupGuardBlocked as exc:
        print(format_block_message(exc), file=sys.stderr)
        return EXIT_GUARD_BLOCKED
    try:
        async def _run_with_mcp_lifecycle() -> int:
            try:
                return await app.run()
            finally:
                from integrations.mcp_bridge import close_servers

                await close_servers()

        return asyncio.run(_run_with_mcp_lifecycle())
    except StartupGuardBlocked as exc:
        # 运行期才触发的阻断（例如补充轮次里第一次真正建 Runtime）
        print(format_block_message(exc), file=sys.stderr)
        return EXIT_GUARD_BLOCKED
    except KeyboardInterrupt:
        print("\n已中断，退出。")
        return 130
