import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import auth
import time
from unittest.mock import patch

# 测试 login 函数
def test_login():
    auth.SESSIONS.clear()
    assert auth.login("applicant", "app123") is True
    assert auth.login("reviewer", "rev456") is True
    assert auth.login("admin", "admin789") is True
    assert auth.login("guest", "guest123") is True
    assert auth.login("applicant", "wrong") is False
    print("✓ test_login")

# 测试 calculate 函数
def test_calculate():
    assert auth.calculate(5) == 10.0
    assert auth.calculate(2.5) == 5.0
    assert auth.calculate("3") == 6.0
    assert auth.calculate("abc") == 0.0
    print("✓ test_calculate")

# 测试 session 创建
def test_create_session():
    auth.SESSIONS.clear()
    base_time = 1000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant")
        assert s["role"] == "applicant"
        assert s["username"] == "applicant"
        assert s["expire"] == base_time + 3600
        assert s["token"] in auth.SESSIONS
    print("✓ test_create_session")

# 测试 get_role
def test_get_role():
    auth.SESSIONS.clear()
    base_time = 2000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("reviewer")
        assert auth.get_role({"token": s["token"]}) == "reviewer"
        
        s = auth.create_session("admin")
        assert auth.get_role({"token": s["token"]}) == "admin"
    print("✓ test_get_role")

# 测试 session 过期
def test_session_expiration():
    expired = {"token": "test", "expire": 1000.0, "role": "admin", "username": "admin", "language": "zh-CN"}
    assert auth.is_logged_in(expired) is False
    
    valid = {"token": "test", "expire": time.time() + 3600, "role": "admin", "username": "admin", "language": "zh-CN"}
    assert auth.is_logged_in(valid) is True
    
    assert auth.is_logged_in(None) is False
    print("✓ test_session_expiration")

# 测试 refresh_token
def test_refresh_token():
    auth.SESSIONS.clear()
    base_time = 3000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin", "language": "zh-CN"}
        auth.refresh_token(s)
        assert s["expire"] == base_time + 3600
        assert s["token"] != "old"
        assert s["token"].startswith("session-")
    print("✓ test_refresh_token")

if __name__ == "__main__":
    test_login()
    test_calculate()
    test_create_session()
    test_get_role()
    test_session_expiration()
    test_refresh_token()
    print("\n=== 全部测试通过! ===")