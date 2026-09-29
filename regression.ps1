# One-command runner for the active offline regression suite.

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

$runtimeRoot = if ($env:FORGE_RUNTIME_DIR) { $env:FORGE_RUNTIME_DIR } else { Join-Path $root 'var' }
if (-not [System.IO.Path]::IsPathRooted($runtimeRoot)) {
    $runtimeRoot = Join-Path $root $runtimeRoot
}
$reportDir = Join-Path $runtimeRoot 'test-reports'
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = Join-Path $reportDir "regression_$stamp.log"

Write-Host '[regression] running active offline regression suite ...'
$testArgs = @()

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
    exit 0
}

Write-Host "[regression] FAILED (exit=$code) - details: $log"
exit $code
