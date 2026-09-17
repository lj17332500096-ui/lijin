#!/bin/bash
# pre-commit 钩子：改动 router 相关文件时强制跑 79 条测试
# 安装位置：.git/hooks/pre-commit
# 调用方式：git commit 时自动触发
# 放行条件：scripts/ci_router_gate.py 全过（退出码 0）

set -e

PROJECT_ROOT="$(git rev-parse --show-toplevel)"
cd "$PROJECT_ROOT"

# 检测是否有 router 相关文件被改动
CHANGED=$(git diff --cached --name-only 2>/dev/null || true)
ROUTER_FILES=(
    "runtime/tool_router.py"
    "runtime/runner.py"
    "tests/test_tool_router.py"
    "agent.py"
    "main.py"
)

NEED_CHECK=false
for f in "${ROUTER_FILES[@]}"; do
    if echo "$CHANGED" | grep -qx "$f"; then
        NEED_CHECK=true
        break
    fi
done

if [ "$NEED_CHECK" = false ]; then
    echo "[pre-commit] 未改动 router 相关文件，跳过测试。"
    exit 0
fi

echo "[pre-commit] 检测到 router 相关文件改动，跑 79 条测试..."

# 用项目 .venv 的 python
if [ -f ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"
elif [ -f ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python"
fi

"$PY" scripts/ci_router_gate.py
RC=$?

if [ $RC -ne 0 ]; then
    echo ""
    echo "[pre-commit] 测试未通过，commit 已阻断。"
    echo "  修复后重跑，或用 --no-verify 跳过（不推荐）。"
    exit 1
fi

echo "[pre-commit] 测试通过，允许 commit。"
