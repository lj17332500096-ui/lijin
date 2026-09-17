import sys, os
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth
import time
from unittest.mock import patch

def test_login_applicant():
    assert auth.login("applicant", "app123") is True

def test_login_reviewer():
    assert auth.login("reviewer", "rev456") is True

def test_login_admin():
    assert auth.login("admin", "admin789") is True

def test_login_wrong():
    assert auth.login("applicant", "wrong") is False

def test_calculate_float():
    assert auth.calculate(2.5) == 5.0

def test_create_session_roles():
    base_time = 1000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant")
        assert s["role"] == "applicant"
        assert s["username"] == "applicant"
        assert s["expire"] == base_time + 3600
        assert s["language"] == "zh-CN"

        s = auth.create_session("reviewer")
        assert s["role"] == "reviewer"
        assert s["expire"] == base_time + 3600
        assert s["language"] == "zh-CN"

        s = auth.create_session("admin")
        assert s["role"] == "admin"
        assert s["expire"] == base_time + 3600
        assert s["language"] == "zh-CN"
    print("✓ test_create_session_roles")

def test_create_session_default_language():
    base_time = 1100000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant")
        assert s["language"] == "zh-CN"
        s = auth.create_session("reviewer")
        assert s["language"] == "zh-CN"
        s = auth.create_session("admin")
        assert s["language"] == "zh-CN"
    print("✓ test_create_session_default_language")

def test_create_session_custom_language():
    base_time = 1200000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant", language="en-US")
        assert s["language"] == "en-US"
        s = auth.create_session("reviewer", language="ja-JP")
        assert s["language"] == "ja-JP"
        s = auth.create_session("admin", language="ko-KR")
        assert s["language"] == "ko-KR"
    print("✓ test_create_session_custom_language")

def test_get_role():
    base_time = 2000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("reviewer")
        assert auth.get_role(s) == "reviewer"
        s = auth.create_session("admin")
        assert auth.get_role(s) == "admin"
    print("✓ test_get_role")

def test_get_language():
    base_time = 2100000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant")
        session_dict = {"token": s["token"]}
        assert auth.get_language(session_dict) == "zh-CN"
        s = auth.create_session("reviewer", language="en-US")
        session_dict = {"token": s["token"]}
        assert auth.get_language(session_dict) == "en-US"
    print("✓ test_get_language")

def test_get_language_not_logged_in():
    assert auth.get_language(None) is None
    assert auth.get_language({"token": "invalid"}) is None
    print("✓ test_get_language_not_logged_in")

def test_token_kept_after_refresh():
    base_time = 3000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin", "language": "zh-CN"}
        auth.refresh_token(s)
        assert s["expire"] == base_time + 3600
        assert s["role"] == "admin"
        assert s["username"] == "admin"
        assert s["language"] == "zh-CN"
        assert s["token"] != "old"
        assert s["token"].startswith("session-")
    print("✓ test_token_kept_after_refresh")

def test_refresh_preserves_custom_language():
    base_time = 3100000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin", "language": "fr-FR"}
        auth.refresh_token(s)
        assert s["language"] == "fr-FR"
    print("✓ test_refresh_preserves_custom_language")

def test_session_expiration():
    expired_session = {"token": "test", "expire": 1000.0, "role": "admin", "username": "admin", "language": "zh-CN"}
    assert auth.is_logged_in(expired_session) is False
    valid_session = {"token": "test", "expire": time.time() + 3600, "role": "admin", "username": "admin", "language": "zh-CN"}
    assert auth.is_logged_in(valid_session) is True
    assert auth.is_logged_in(None) is False
    assert auth.is_logged_in({"token": "test"}) is False
    print("✓ test_session_expiration")

def test_login_then_refresh_page():
    base_time = 4000000.0
    with patch('app.auth.time.time', return_value=base_time):
        assert auth.login("applicant", "app123") is True
        s = auth.create_session("applicant", language="en-US")
        assert s["token"] in auth.SESSIONS
        refreshed_session = {"token": s["token"]}
        assert auth.is_logged_in(refreshed_session) is True
        assert auth.get_role(refreshed_session) == "applicant"
        assert auth.get_language(refreshed_session) == "en-US"
    print("✓ test_login_then_refresh_page")

# Run all tests
test_login_applicant()
test_login_reviewer()
test_login_admin()
test_login_wrong()
test_calculate_float()
test_create_session_roles()
test_create_session_default_language()
test_create_session_custom_language()
test_get_role()
test_get_language()
test_get_language_not_logged_in()
test_token_kept_after_refresh()
test_refresh_preserves_custom_language()
test_session_expiration()
test_login_then_refresh_page()
print("\n=== 全部测试通过! ===")