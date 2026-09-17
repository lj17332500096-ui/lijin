import sys
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")

from app import auth
auth.SESSIONS.clear()

import time
from unittest.mock import patch

def test_login_applicant():
    assert auth.login("applicant", "app123") is True
    print("✓ test_login_applicant")

def test_login_reviewer():
    assert auth.login("reviewer", "rev456") is True
    print("✓ test_login_reviewer")

def test_login_admin():
    assert auth.login("admin", "admin789") is True
    print("✓ test_login_admin")

def test_login_wrong():
    assert auth.login("applicant", "wrong") is False
    print("✓ test_login_wrong")

def test_calculate_float():
    assert auth.calculate(2.5) == 5.0
    print("✓ test_calculate_float")

def test_create_session_roles():
    base_time = 1000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant")
        assert s["role"] == "applicant"
        assert s["username"] == "applicant"
        assert s["expire"] == base_time + 3600
        assert s["language"] == "zh-CN"
    print("✓ test_create_session_roles")

def test_session_expiration():
    base_time = 3100000.0
    with patch('app.auth.time.time', return_value=base_time):
        auth.login("applicant", "app123")
        s = auth.create_session("applicant")
        assert auth.is_logged_in(s) is True
        with patch('app.auth.time.time', return_value=base_time + 3601):
            assert auth.is_logged_in(s) is False
    print("✓ test_session_expiration")

if __name__ == "__main__":
    test_login_applicant()
    test_login_reviewer()
    test_login_admin()
    test_login_wrong()
    test_calculate_float()
    test_create_session_roles()
    test_session_expiration()
    print("\n=== 全部测试通过! ===")