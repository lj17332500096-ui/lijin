# FORGE Runtime FROZEN baseline - one-click regression gate.
#   .\regression.ps1          deterministic offline subset: 538 tests
#                             (excludes test_theme_cdp, which needs a browser)
#   .\regression.ps1 -Full    full baseline: 546 tests
#                             (requires webapp on 127.0.0.1:8765 + Edge headless)
# Baseline: 468 OK at freeze (2026-09-07) + 10 aux-provider-retry
#           + 6 MCP-policy + 4 concurrency-stress + 7 markdown-bullet
#           + 6 completion-gate false-positive + 2 tool-router
#             capability tests + 11 capability-introspection + 6 task-readiness
#             + 1 capability-full-tools test + 25 readiness-closure
#             (24 项 RT-GATE/Discovery/Capability/Clarification + 1 项
#              questions 精度门语义更新) = 546 full.

param(
    [switch]$Full
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Error "[regression] python venv not found: $py"
    exit 2
}

# Keep child python output readable regardless of console code page.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

# 回归使用离线替身验证 Runtime 语义，不能继承桌面会话的本地模型默认选择；
# 单独的 test_model_select 会在自身 fixture 中覆盖并验证本地模型分支。
$env:FORGE_MODEL_PREF = 'gateway'

# TMP/TEMP may be in 8.3 short form (C:\Users\ADMINI~1\...) while path
# containment checks see the long form (C:\Users\Administrator\...) and fail.
# Pin both to the long-form path so sandbox/path checks are stable.
$longTemp = Join-Path $env:LOCALAPPDATA 'Temp'
$env:TMP = $longTemp
$env:TEMP = $longTemp

$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = Join-Path $logDir "regression_$stamp.log"

if ($Full) {
    $listening = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if (-not $listening) {
        Write-Host '[regression] -Full needs the web UI running first: start webapp.py (http://127.0.0.1:8765), then rerun.'
        Write-Host '[regression] (or use plain .\regression.ps1 for the deterministic offline subset)'
        exit 3
    }
    Write-Host '[regression] running FULL baseline (546; CDP theme tests use Edge headless against :8765) ...'
    $testArgs = @()
    $expectedRan = 546
} else {
    Write-Host '[regression] running deterministic offline subset (538; browser theme CDP excluded) ...'
    $testArgs = @('--exclude', 'test_theme_cdp')
    $expectedRan = 538
}

# unittest writes progress to stderr; under ErrorActionPreference=Stop those
# native stderr records abort the capture, so relax it just for the call.
$oldEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$out = & $py -X utf8 (Join-Path $root 'tests\run_tests.py') @testArgs 2>&1
$code = $LASTEXITCODE
$ErrorActionPreference = $oldEap
$out | Set-Content -LiteralPath $log -Encoding UTF8

$text = ($out -join "`n")
$ran = if ($text -match 'Ran (\d+) tests?') { [int]$Matches[1] } else { 0 }
$skipped = if ($text -match 'skipped=(\d+)') { [int]$Matches[1] } else { 0 }
$tail = ($out | Select-Object -Last 1)

Write-Host "[regression] ran=$ran skipped=$skipped"
Write-Host "[regression] last line: $tail"

if ($code -eq 0) {
    Write-Host '[regression] PASS (exit 0)'
    if ($ran -ne $expectedRan) {
        Write-Warning "[regression] baseline drift: expected ran=$expectedRan, actual ran=$ran - review before relying on freeze status."
    }
    exit 0
}

Write-Host "[regression] FAILED (exit=$code) - details: $log"
exit $code
