# Errors

Command failures and integration errors.

---

## [ERR-20260907-004] managed_process_restart_blocked

**Logged**: 2026-09-07T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
The execution environment blocked the command that would stop and restart the local web server after a configuration update.

### Error
The process-management command was rejected by execution policy before it ran.

### Context
- `WORKSPACE_ROOT` is already updated on disk.
- The existing web server was launched before the update and needs a normal manual restart to reload `.env`.

### Suggested Fix
Restart the web application from its normal desktop terminal after configuration changes.

### Metadata
- Reproducible: environment policy dependent
- Related Files: start-web.bat

---

## [ERR-20260907-003] regression_workspace_path_mismatch

**Logged**: 2026-09-07T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
The full deterministic regression suite still contains assertions for the previous H: workspace path.

### Error
21 failures and 1 error: workspace file tool tests tried to read `H:\\Byong-hermes\\my_creative_agent`, which does not exist after the project moved to F:.

### Context
- The local-provider regression tests passed.
- The suite ran 541 tests and failed only after reaching path-sensitive workspace tests.

### Suggested Fix
Make the path-sensitive test fixtures derive their workspace root from the current repository path instead of a fixed drive letter.

### Metadata
- Reproducible: yes
- Related Files: tests/test_tools.py

### Resolution
- **Resolved**: 2026-09-07T00:00:00+08:00
- **Notes**: Updated `WORKSPACE_ROOT` to the current F: workspace; the regression launcher now uses the gateway profile for mocked offline tests, and model-selection fixtures clear inherited local-model settings.

---

## [ERR-20260907-002] task_container_create_endpoint

**Logged**: 2026-09-07T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
The documented-looking task collection path accepts GET only; task creation uses a separate `/api/tasks/create` endpoint.

### Error
`POST /api/tasks` returned HTTP 405 Method Not Allowed.

### Context
- Found while running the local model web compatibility check.
- Route registration maps `POST /api/tasks/create` to the creation handler.

### Suggested Fix
Either expose POST on `/api/tasks` or document `/api/tasks/create` as the supported compatibility endpoint.

### Metadata
- Reproducible: yes
- Related Files: webapp.py

---

## [ERR-20260907-001] powershell_start_process_logging

**Logged**: 2026-09-07T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
PowerShell cannot redirect standard output and standard error to the same file with `Start-Process`.

### Error
`RedirectStandardOutput` and `RedirectStandardError` are same.

### Context
- Attempted to start the local web application for model compatibility testing.
- No application process was started and no project data was changed.

### Suggested Fix
Use separate stdout and stderr log files when starting a background PowerShell process.

### Metadata
- Reproducible: yes
- Related Files: webapp.py

### Resolution
- **Resolved**: 2026-09-07T00:00:00+08:00
- **Notes**: Subsequent launch uses separate log files.

---

## 2026-09-09 Public Activity 审计环境
用户指定目录在当前 checkout 不存在；实际根目录没有 .git。先枚举 runtime/web 的调用关系，不执行猜测路径或 Git 操作。tests/test_run_sse.py 也不存在，后续先 rg --files 定位测试。
