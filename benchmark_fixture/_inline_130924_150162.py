import sys, os
sys.path.insert(0, 'my_creative_agent/benchmark_fixture')
from tests import test_auth

# 运行所有测试函数
passed = 0
failed = 0
for name in dir(test_auth):
    if name.startswith('test_'):
        func = getattr(test_auth, name)
        if callable(func):
            try:
                func()
                print(f"✓ {name}")
                passed += 1
            except Exception as e:
                print(f"✗ {name}: {e}")
                failed += 1

print(f"\n=== 结果: {passed} passed, {failed} failed ===")