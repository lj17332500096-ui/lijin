import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'benchmark_fixture'))
from app import auth
import time
from unittest.mock import patch

# 运行所有测试函数
for name in dir(auth):
    if name.startswith('test_'):
        func = getattr(auth, name)
        if callable(func):
            try:
                func()
                print(f"✓ {name}")
            except Exception as e:
                print(f"✗ {name}: {e}")