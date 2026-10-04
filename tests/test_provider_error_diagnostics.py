"""P1：失败尝试必须留下**能定位原因**的字段（`error_type` / `error_message` / `error`）。

回归背景（真事故）：上一轮排查「agent 报 `provider_internal_error`、完全答不了话」时，
用户提示与 CLI 诊断都写着「详情见运行记录」/「用 /diag 看本轮 provider_attempts」，
但运行记录里**根本没有能定位原因的字段** —— `record_attempt` 只记
`kind` / `status` / `latency_ms`，而 `kind` 是分类结果（`provider_internal_error`
底下可能是连接拒绝、401、DNS、模型名写错……），`status` 常常是 None。
最后靠三轮对照实验才定位到根因：`.env` 里 `FORGE_MODEL_PREF=local`
但本地 llama.cpp 服务没启动。排查成本 ≈ 5 分钟 vs 三轮实验。

本文件锁住三件事：
1. 失败记录带 `error_type`（异常类名）+ `error_message`（脱敏后的消息）；
2. 凭据不泄露（复用 A8「四条审计出口统一脱敏」的 `runtime.audit.redact_text`）；
3. 字段落在**读者真正读的地方** —— `provider_attempts.error` 列
   （`/diag` 显示的就是它；修复前该列恒为空，判据与生产端写出的值不对齐）。

全部离线：不联网、不调真实模型。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


class ErrorFieldExtractionTests(unittest.TestCase):
    """`_error_fields` 本身的契约：类名 + 脱敏消息 + 截断上限。"""

    def test_keeps_exception_type_and_message(self) -> None:
        from runtime.provider_gateway import _error_fields

        fields = _error_fields(ConnectionRefusedError("localhost:8081 refused"))
        self.assertEqual(fields["error_type"], "ConnectionRefusedError")
        self.assertIn("localhost:8081", fields["error_message"])
        self.assertIn("ConnectionRefusedError", fields["error_message"],
                      "消息应自带类型前缀，否则只有 error_message 被单独读时失去类型")

    def test_error_key_is_present_for_the_reader_that_uses_it(self) -> None:
        """`error` 是 /diag 显示的那一列，必须与 error_message 同值。"""
        from runtime.provider_gateway import _error_fields

        fields = _error_fields(RuntimeError("boom"))
        self.assertEqual(fields["error"], fields["error_message"])

    def test_redacts_api_keys(self) -> None:
        """凭据不得进库（异常消息里真的会出现 key / Authorization / 带凭据 URL）。"""
        from runtime.provider_gateway import _error_fields

        fields = _error_fields(RuntimeError(
            "POST https://api.example.com failed, api_key=sk-abcdefghijklmnop1234"))
        self.assertNotIn("sk-abcdefghijklmnop1234", fields["error_message"])
        self.assertIn("***", fields["error_message"])

    def test_redacts_authorization_header(self) -> None:
        from runtime.provider_gateway import _error_fields

        fields = _error_fields(RuntimeError(
            "headers: Authorization: Bearer supersecrettoken123"))
        self.assertNotIn("supersecrettoken123", fields["error_message"])

    def test_redacts_credentials_in_base_url(self) -> None:
        """`OPENAI_BASE_URL` 允许内嵌 user:pw@host，连接错误会原样带出。"""
        from runtime.provider_gateway import _error_fields

        fields = _error_fields(RuntimeError(
            "connection to https://alice:hunter2secret@llama.internal/v1 failed"))
        self.assertNotIn("hunter2secret", fields["error_message"])

    def test_truncates_oversized_message(self) -> None:
        from runtime.provider_gateway import _ERROR_MESSAGE_MAX, _error_fields

        fields = _error_fields(RuntimeError("x" * (_ERROR_MESSAGE_MAX * 4)))
        self.assertLessEqual(len(fields["error_message"]), _ERROR_MESSAGE_MAX + 32)
        self.assertTrue(fields["error_message"].endswith("(truncated)"))

    def test_never_raises_on_exotic_exception(self) -> None:
        """诊断代码本身不能把原始故障换成新的故障。"""
        from runtime.provider_gateway import _error_fields

        class Hostile(Exception):
            def __str__(self) -> str:
                raise ValueError("__str__ exploded")

        fields = _error_fields(Hostile())
        self.assertEqual(fields["error_type"], "Hostile")
        self.assertTrue(fields["error_message"])


class FailedAttemptCarriesDiagnosisTests(unittest.TestCase):
    """真实失败路径必须把诊断字段写进尝试记录（不只是 helper 自己对）。"""

    def setUp(self) -> None:
        from runtime import provider_gateway as pg

        self.pg = pg
        self.run_id = "run_diag_fields"
        pg._ATTEMPTS.pop(self.run_id, None)
        patcher = mock.patch.dict(os.environ, {"FORGE_PROVIDER_ATTEMPTS_PERSIST": "off"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(pg._ATTEMPTS.pop, self.run_id, None)

    def _records(self) -> list[dict]:
        return self.pg.read_attempts(self.run_id)

    def test_get_response_failure_records_type_and_message(self) -> None:
        """非流式路径真的产出带诊断字段的记录。"""

        boom = ConnectionRefusedError("127.0.0.1:8081 connection refused")

        class _FailingModel:
            async def get_response(self, *a, **k):
                raise boom

        run_id = self.run_id
        rt = self.pg.ResilientModel.__new__(self.pg.ResilientModel)
        rt._inner = _FailingModel()
        rt._gateway = mock.Mock()
        rt._gateway._fallback_config.return_value = None
        import asyncio
        with mock.patch.object(self.pg, "_current_run_id", return_value=run_id), \
                mock.patch.object(self.pg, "_provider_max_attempts", return_value=1), \
                mock.patch.object(self.pg, "_provider_max_total_attempts", return_value=1), \
                mock.patch.object(self.pg, "_total_timeout_seconds", return_value=5), \
                self.assertRaises(ConnectionRefusedError):
            asyncio.run(rt.get_response("hi"))

        recs = self._records()
        self.assertTrue(recs, "失败必须留下尝试记录")
        failed = [r for r in recs if r.get("error_type")]
        self.assertTrue(failed, f"没有任何记录带 error_type：{recs}")
        self.assertEqual(failed[0]["error_type"], "ConnectionRefusedError")
        self.assertIn("8081", failed[0]["error_message"])
        # 终态记录也要带 —— 用户看到的 provider_internal_error 正是在那里抛出
        exhausted = [r for r in recs if r.get("kind") == "exhausted"]
        self.assertTrue(exhausted, "exhausted 终态记录缺失")
        self.assertEqual(exhausted[0]["error_type"], "ConnectionRefusedError")

    def test_every_failure_record_has_diagnosable_error(self) -> None:
        """计数断言：所有「失败类」记录都必须带诊断字段，不能靠 assertIn 蒙混。"""
        boom = TimeoutError("模型流式输出超时中断")
        with mock.patch.dict(os.environ, {"FORGE_PROVIDER_ATTEMPTS_PERSIST": "off"}):
            self.pg.record_attempt(self.run_id, {"kind": "unavailable"})
            self.pg.record_attempt(self.run_id, {
                "kind": "provider_internal_error", "tag": "primary",
                "status": None, "latency_ms": 12, **self.pg._error_fields(boom)})
        recs = self._records()
        internal = [r for r in recs if r.get("kind") == "provider_internal_error"]
        self.assertEqual(len(internal), 1)
        self.assertEqual(internal[0]["error_type"], "TimeoutError")
        self.assertEqual(internal[0]["error"], internal[0]["error_message"],
                         "判据字段必须与读者实际读到的列同源同值")


class ErrorColumnReachesDiagTests(unittest.TestCase):
    """字段必须落到用户看得到的地方（DB `error` 列 → /diag），不是只记在内存里。"""

    def test_error_column_is_populated_end_to_end(self) -> None:
        import json
        import tempfile

        from runtime import provider_gateway as pg
        from runtime.task_manager import TaskManager

        run_id = "run_diag_db"
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            tm = TaskManager(db)
            with mock.patch.dict(os.environ, {"FORGE_PROVIDER_ATTEMPTS_PERSIST": "off"}):
                pg._ATTEMPTS.pop(run_id, None)
                pg.record_attempt(run_id, {
                    "kind": "provider_internal_error", "tag": "primary",
                    "model": "local-model", "latency_ms": 42,
                    **pg._error_fields(ConnectionRefusedError(
                        "127.0.0.1:8081 refused, api_key=sk-abcdefghijklmnop1234")),
                })
                for rec in pg.read_attempts(run_id):
                    tm.record_provider_attempt(run_id, rec.get("kind", "ok"), rec)
                pg._ATTEMPTS.pop(run_id, None)

            rows = tm.list_provider_attempts(run_id)
            self.assertEqual(len(rows), 1, "尝试记录必须真的落库")
            row = rows[0]
            # /diag (cli/commands.py) 显示的就是这一列 —— 修复前恒为 ""。
            self.assertTrue(row.get("error"),
                            "provider_attempts.error 列仍为空 ⇒ /diag 显示不出原因")
            self.assertIn("ConnectionRefusedError", row["error"])
            self.assertIn("8081", row["error"], "脱敏后仍须能定位到端点")
            # 脱敏在入库前就已生效：DB 的两处副本都不得出现明文 key
            self.assertNotIn("sk-abcdefghijklmnop1234", row["error"])
            self.assertNotIn("sk-abcdefghijklmnop1234", json.dumps(row["detail"]))
            # detail_json 里 error_type 也在（供审计按类型聚合）
            self.assertEqual(row["detail"].get("error_type"), "ConnectionRefusedError")


if __name__ == "__main__":
    unittest.main()
