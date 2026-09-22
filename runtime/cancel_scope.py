"""取消登记表（审计 P1-2）：让已启动的子进程树随「取消」一起停下。

问题：同步工具由 SDK 用 ``asyncio.to_thread`` 执行（见 ``agents/tool.py`` 的
``_invoke_function_tool``）。``task.cancel()`` 只能取消 await 中的 Future，
worker 线程与它拉起的子进程会继续跑完。用户看到「本轮任务已被取消」，
但跑测试 / 写文件的副作用仍然发生 —— **取消语义对用户是谎言**。

能力边界（必须写清楚，否则又是一次「声称已解决」）：

  ✅ 已启动的子进程树：工具在拿到进程句柄后 ``register(killer)``，取消时
     ``kill_run()`` 关掉 Windows Job Object 句柄 → ``KILL_ON_JOB_CLOSE``
     原子杀掉整棵树（含孙进程）；无 job 句柄时退化为 ``taskkill /T``。
     覆盖 ``run_tests`` / ``run_python`` / ``code_loop``（``code_loop`` 内部复用
     前两者的进程创建路径，因此天然被覆盖）。

  ❌ 纯 Python 的同步计算（例如 ``index_workspace`` 扫 5000 个文件）：Python 无法
     安全 kill 线程，这类工具只能靠 ``runtime/spec.py`` 的超时上限约束耗时，
     不能靠取消。**不要**在本模块之外声称取消能中断它们。

登记按 ``run_id`` 分桶：webapp 可以并发跑多个 Run，杀树必须只杀被取消的那一个，
否则「取消 A」会顺手把 B 正在跑的 pytest 一起杀掉。

取消路径上的一切异常都必须被吞掉：真正的原因（``CancelledError``）不能被登记表掩盖。
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
from typing import Callable

_logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
# token -> (run_id, killer)
_HOOKS: dict[int, tuple[str | None, Callable[[], None]]] = {}
_SEQ = itertools.count(1)


def current_run_id() -> str | None:
    """当前 Run 的 id。

    在 ``asyncio.to_thread`` 的 worker 线程里同样有效：线程启动时会复制调用点的
    contextvars 上下文，所以同步工具能自己认领到「我属于哪个 Run」。
    """
    try:
        from runtime.runctx import current as _rc

        ctx = _rc()
        return getattr(ctx, "run_id", None) or None
    except Exception:
        return None


def register(killer: Callable[[], None], *, run_id: str | None = None) -> int:
    """登记「立刻停掉这份副作用」的回调，返回 token（供 ``unregister`` 使用）。

    ``run_id`` 缺省时取 ``current_run_id()``；取不到（离线调用、无 Run 上下文）时
    记为 ``None`` 桶，只会被 ``kill_run(None)`` 命中。
    """
    if not callable(killer):
        return 0
    with _LOCK:
        token = next(_SEQ)
        _HOOKS[token] = (run_id if run_id is not None else current_run_id(), killer)
        return token


def unregister(token: int) -> None:
    """撤销登记。工具无论正常结束还是异常退出都必须调用，否则登记表无界增长。"""
    if not token:
        return
    with _LOCK:
        _HOOKS.pop(token, None)


def active_count(run_id: str | None = None) -> int:
    """当前登记项数量（``run_id=None`` 表示全部）。诊断用。"""
    with _LOCK:
        if run_id is None:
            return len(_HOOKS)
        return sum(1 for rid, _ in _HOOKS.values() if rid == run_id)


def kill_run(run_id: str | None = None) -> int:
    """执行取消：停掉该 Run 登记的全部副作用（``run_id=None`` → 全部）。

    返回实际调用的 killer 数。语义：
    - **一次性**：命中的登记项在调用前就被摘除，重复调用不会重复杀 —— 句柄号可能已被
      系统复用，第二次 ``CloseHandle`` 会关掉别的对象；
    - **逐个隔离**：某个 killer 抛异常不影响其余，异常一律吞掉。
    """
    with _LOCK:
        victims = [(token, killer)
                   for token, (rid, killer) in _HOOKS.items()
                   if run_id is None or rid == run_id]
        for token, _killer in victims:
            _HOOKS.pop(token, None)
    for token, killer in victims:
        try:
            killer()
        except Exception:
            # 取消路径上不得因为清理失败而掩盖真正原因；也不污染评测/测试日志
            # （沿用 guardrails 的通道隔离约定：测试/评测进程不写生产日志）。
            if (os.environ.get("FORGE_TEST_MODE") != "1"
                    and os.environ.get("FORGE_EVAL_MODE") != "1"):
                _logger.warning("取消回调执行失败（token=%s）", token, exc_info=True)
    return len(victims)


def reset_for_tests() -> None:
    """清空登记表（仅供测试隔离使用；生产代码不得调用）。"""
    with _LOCK:
        _HOOKS.clear()
