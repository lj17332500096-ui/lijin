#!/bin/bash
# pre-commit 钩子：按改动文件精确选测试，全过才允许提交
# 入库位置：scripts/pre_commit_hook.sh（本文件）
# 安装：cp scripts/pre_commit_hook.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
#
# 放行条件：scripts/ci_gate.py 退出码 0
#   - 改动文件 → 测试文件映射见 ci_gate.py::CHANGE_TEST_MAP（无映射则不跑）
#   - 已登记的既有失败见 .ci/known_failures.txt（只警告不阻断，必须写原因）

set -e

PROJECT_ROOT="$(git rev-parse --show-toplevel)"
cd "$PROJECT_ROOT"

CHANGED=$(git diff --cached --name-only 2>/dev/null || true)
if [ -z "$CHANGED" ]; then
    echo "[pre-commit] 没有暂存的改动，跳过。"
    exit 0
fi

# 用项目 .venv 的 python
if [ -f ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"
elif [ -f ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python"
fi

printf '%s\n' "$CHANGED" | "$PY" scripts/ci_gate.py --stdin
RC=$?

if [ $RC -ne 0 ]; then
    echo ""
    echo "[pre-commit] 门禁未通过，commit 已阻断。"
    echo "  修复后重跑；确认失败与本次改动无关时，把它登记进 .ci/known_failures.txt。"
    exit 1
fi

echo "[pre-commit] 门禁通过，允许 commit。"
