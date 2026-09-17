import sys
sys.path.insert(0, r'F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture')
from app import auth
import time
from unittest.mock import patch

# 运行所有测试
tests = [
    ('test_login_applicant', lambda: auth.login("applicant", "app123") is not None),
    ('test_login_reviewer', lambda: auth.login("reviewer", "rev456") is not None),
    ('test_login_admin', lambda: auth.login("admin", "admin789") is not None),
    ('test_login_wrong', lambda: auth.login("applicant", "wrong") is False),
    ('test_calculate_float', lambda: auth.calculate(2.5) == 5.0),
]

# test_create_session_roles
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

tests.append(('test_create_session_roles', test_create_session_roles))

# test_create_session_default_language
def test_create_session_default_language():
    base_time = 1100000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant")
        assert s["language"] == "zh-CN"

        s = auth.create_session("reviewer")
        assert s["language"] == "zh-CN"

        s = auth.create_session("admin")
        assert s["language"] == "zh-CN"

tests.append(('test_create_session_default_language', test_create_session_default_language))

# test_create_session_custom_language
def test_create_session_custom_language():
    base_time = 1200000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant", language="en-US")
        assert s["language"] == "en-US"

        s = auth.create_session("reviewer", language="ja-JP")
        assert s["language"] == "ja-JP"

        s = auth.create_session("admin", language="ko-KR")
        assert s["language"] == "ko-KR"

tests.append(('test_create_session_custom_language', test_create_session_custom_language))

# test_get_role
def test_get_role():
    base_time = 2000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("reviewer")
        assert auth.get_role(s) == "reviewer"

        s = auth.create_session("admin")
        assert auth.get_role(s) == "admin"

tests.append(('test_get_role', test_get_role))

# test_get_language
def test_get_language():
    base_time = 2100000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = auth.create_session("applicant")
        session_dict = {"token": s["token"]}
        assert auth.get_language(session_dict) == "zh-CN"

        s = auth.create_session("reviewer", language="en-US")
        session_dict = {"token": s["token"]}
        assert auth.get_language(session_dict) == "en-US"

tests.append(('test_get_language', test_get_language))

# test_get_language_not_logged_in
def test_get_language_not_logged_in():
    assert auth.get_language(None) is None
    assert auth.get_language({"token": "invalid"}) is None

tests.append(('test_get_language_not_logged_in', test_get_language_not_logged_in))

# test_token_kept_after_refresh
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

tests.append(('test_token_kept_after_refresh', test_token_kept_after_refresh))

# test_refresh_preserves_custom_language
def test_refresh_preserves_custom_language():
    base_time = 3100000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin", "language": "fr-FR"}
        auth.refresh_token(s)
        assert s["language"] == "fr-FR"

tests.append(('test_refresh_preserves_custom_language', test_refresh_preserves_custom_language))

# test_session_expiration
def test_session_expiration():
    base_time = 5000000.0
    with patch('app.auth.time.time', return_value=base_time):
        real_session = auth.create_session("admin")
        token = real_session["token"]
        
        assert auth.is_logged_in({"token": token}) is True
        
        with patch('app.auth.time.time', return_value=base_time + 3601):
            assert auth.is_logged_in({"token": token}) is False
        
        assert auth.is_logged_in(None) is False
        assert auth.is_logged_in({"token": "nonexistent"}) is False

tests.append(('test_session_expiration', test_session_expiration))

# test_login_then_refresh_page
def test_login_then_refresh_page():
    base_time = 4000000.0
    with patch('app.auth.time.time', return_value=base_time):
        assert auth.login("applicant", "app123") is not None
        
        s = auth.create_session("applicant", language="en-US")
        assert s["token"] in auth.SESSIONS
        
        refreshed_session = {"token": s["token"]}
        
        assert auth.is_logged_in(refreshed_session) is True
        assert auth.get_role(refreshed_session) == "applicant"
        assert auth.get_language(refreshed_session) == "en-US"

tests.append(('test_login_then_refresh_page', test_login_then_refresh_page))

# 运行所有测试
passed = failed = 0
for name, test in tests:
    try:
        test()
        print(f"✓ {name}")
        passed += 1
    except AssertionError as e:
        print(f"✗ {name}: {e}")
        failed += 1
    except Exception as e:
        print(f"✗ {name}: {type(e).__name__}: {e}")
        failed += 1

print(f"\n=== 结果: {passed} passed, {failed} failed ===")
