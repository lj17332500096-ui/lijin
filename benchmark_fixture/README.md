# benchmark_fixture

这是一个用于 FORGE Agent Behavior Benchmark 的迷你项目。

## 启动

```bash
pip install -r requirements.txt   # 无依赖（纯标准库）
python -m pytest tests
或 python -m pytest tests/test_auth.py -v
```

## 结构

- app/auth.py: 登录、token 刷新、calculate 逻辑
- tests/test_auth.py: 自动化测试
