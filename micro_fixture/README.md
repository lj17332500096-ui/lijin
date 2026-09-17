# micro_fixture

这是一个微型测试项目，用于演示 pytest 的基本使用。项目包含两个文件：
- `calc.py` — 计算模块，提供 `add()` 和 `multiply()` 两个函数
- `test_calc.py` — 测试文件，测试上述两个函数的正确性

## 测试

**运行测试**
- `pytest` — 运行当前目录下所有 `test_*.py` 测试文件，显示通过/失败结果。
- `pytest -v` — 详细模式：逐个打印测试模块名和测试名，方便追踪执行到哪个测试了。
- `pytest --tb=short` — 失败时只输出简短错误追踪，不展示完整调用链，快速定位到错误行。
- `pytest -k <关键词>` — 只运行名称包含关键词的测试（如 `pytest -k add` 只跑 `test_add`）。
- `pytest --collect-only` — 只收集不执行，查看 pytest 能发现哪些测试。

**示例输出**
```
================= test session starts =================
test_calc.py::test_add  PASSED          [ 50%]
test_calc.py::test_multiply  PASSED     [100%]
================= 2 passed in 0.04s ==================
```
两个测试用例都通过了。`PASSED` 表示通过，`[ 50%]` 表示已完成一半测试。

**项目测试结构**
- 项目只有 `calc.py`（模块）和 `test_calc.py`（测试文件），`test_calc.py` 测试 `calc` 模块中的 `add()` 和 `multiply()` 两个函数。
- 任何以 `test_` 开头的文件，pytest 都会自动发现并运行，不需要额外配置。
- 如需覆盖率报告：安装 pytest-cov 后运行 `pytest --cov=calc --cov-report=html`，会生成 HTML 覆盖率报告。
