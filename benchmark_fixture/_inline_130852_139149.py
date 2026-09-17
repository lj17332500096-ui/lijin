import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'benchmark_fixture'))
from app import auth
import time
from unittest.mock import patch

# 测试套件
def run_all_tests():
    results = {'passed': [], 'failed': []}
    
    tests = [
        ('test_login_applicant', lambda: auth.login("applicant", "app123") is not None),
        ('test_login_reviewer', lambda: auth.login("reviewer", "rev456") is not None),
        ('test_login_admin', lambda: auth.login("admin", "admin789") is not None),
        ('test_login_wrong', lambda: auth.login("applicant", "wrong") is None),
        ('test_calculate_float', lambda: auth.calculate(2.5) == 5.0),
        ('test_session_expiration', test_session_expiration),
    ]
    
    for name, func in tests:
        try:
            result = func()
            if result:
                results['passed'].append(name)
                print(f"✓ {name}")
            else:
                results['failed'].append(name)
                print(f"✗ {name}")
        except Exception as e:
            results['failed'].append(name)
            print(f"✗ {name}: {e}")
    
    return results

def test_session_expiration():
    """测试session过期逻辑"""
    expired_session = {"token": "test", "expire": 1000.0, "role": "admin", "username": "admin", "language": "zh-CN"}
    if not auth.is_logged_in(expired_session):
        valid_session = {"token": "test", "expire": time.time() + 3600, "role": "admin", "username": "admin", "language": "zh-CN"}
        if auth.is_logged_in(valid_session):
            if not auth.is_logged_in(None):
                if not auth.is_logged_in({"token": "test"}):
                    return True
    return False

if __name__ == "__main__":
    print("=== 运行 benchmark_fixture 测试 ===\n")
    results = run_all_tests()
    print(f"\n=== 结果: {len(results['passed'])} passed, {len(results['failed'])} failed ===")