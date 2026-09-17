import sys
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth
auth.SESSIONS.clear()
from unittest.mock import patch
import time

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
        token = auth.create_session("applicant")
        assert token["token"] in auth.SESSIONS
        s = auth.SESSIONS[token["token"]]
        assert s["role"] == "applicant"
        assert s["username"] == "applicant"
        assert s["expire"] == base_time + 3600

        token = auth.create_session("reviewer")
        s = auth.SESSIONS[token["token"]]
        assert s["role"] == "reviewer"
        assert s["expire"] == base_time + 3600

        token = auth.create_session("admin")
        s = auth.SESSIONS[token["token"]]
        assert s["role"] == "admin"
        assert s["expire"] == base_time + 3600
    print("✓ test_create_session_roles")

def test_get_role():
    base_time = 2000000.0
    with patch('app.auth.time.time', return_value=base_time):
        session_data = auth.create_session("reviewer")
        session = {"token": session_data["token"]}
        assert auth.get_role(session) == "reviewer"

        session_data = auth.create_session("admin")
        session = {"token": session_data["token"]}
        assert auth.get_role(session) == "admin"
    print("✓ test_get_role")

def test_token_kept_after_refresh():
    base_time = 3000000.0
    with patch('app.auth.time.time', return_value=base_time):
        session_data = auth.create_session("admin")
        session = {"token": session_data["token"]}
        auth.refresh_token(session)
        assert session["expire"] == base_time + 3600
        assert session["token"] != session_data["token"]
        assert session["token"].startswith("session-")
        assert session["token"] in auth.SESSIONS
        assert auth.SESSIONS[session["token"]]["role"] == "admin"
    print("✓ test_token_kept_after_refresh")

def test_session_expiration():
    expired_session = {"token": "nonexistent", "expire": 1000.0, "role": "admin", "username": "admin"}
    assert auth.is_logged_in(expired_session) is False
    
    base_time = time.time()
    token = auth.create_session("admin")
    assert auth.is_logged_in({"token": token}) is True
    
    assert auth.is_logged_in(None) is False
    assert auth.is_logged_in({"token": "test"}) is False
    print("✓ test_session_expiration")

def test_login_then_refresh_page():
    base_time = 4000000.0
    with patch('app.auth.time.time', return_value=base_time):
        assert auth.login("applicant", "app123") is True
        session_data = auth.create_session("applicant")
        token = session_data["token"]
        assert token in auth.SESSIONS
        refreshed_session = {"token": token}
        assert auth.is_logged_in(refreshed_session) is True
        assert auth.get_role(refreshed_session) == "applicant"
    print("✓ test_login_then_refresh_page")

test_login_applicant()
test_login_reviewer()
test_login_admin()
test_login_wrong()
test_calculate_float()
test_create_session_roles()
test_get_role()
test_token_kept_after_refresh()
test_session_expiration()
test_login_then_refresh_page()
print("\n=== 全部测试通过! ===")