import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import auth
import time
from unittest.mock import patch

def test_login_applicant():
    assert auth.login("applicant", "app123") is not None

def test_login_reviewer():
    assert auth.login("reviewer", "rev456") is not None

def test_login_admin():
    assert auth.login("admin", "admin789") is not None

def test_login_wrong():
    assert auth.login("applicant", "wrong") is False

def test_calculate_float():
    assert auth.calculate(2.5) == 5.0

def test_create_session_roles():
    # 使用固定的基准时间，避免系统时间波动导致的偶发失败
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
    """测试默认语言为 zh-CN"""
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
    """测试自定义语言设置"""
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
    """测试 get_language 函数"""
    base_time = 2100000.0
    with patch('app.auth.time.time', return_value=base_time):
        # 默认语言
        s = auth.create_session("applicant")
        session_dict = {"token": s["token"]}
        assert auth.get_language(session_dict) == "zh-CN"

        # 自定义语言
        s = auth.create_session("reviewer", language="en-US")
        session_dict = {"token": s["token"]}
        assert auth.get_language(session_dict) == "en-US"
    print("✓ test_get_language")

def test_get_language_not_logged_in():
    """测试未登录时返回 None"""
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
    """测试刷新 token 后自定义语言保持不变"""
    base_time = 3100000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin", "language": "fr-FR"}
        auth.refresh_token(s)
        assert s["language"] == "fr-FR"
    print("✓ test_refresh_preserves_custom_language")

def test_session_expiration():
    """测试session过期逻辑"""
    base_time = 5000000.0
    # 创建 session 时也需要 mock 时间，否则 expire 会用真实系统时间（约 17亿秒），
    # 后续检查过期时 mock 时间只有 500万秒，导致 expire 永远大于检查时间而不会过期
    with patch('app.auth.time.time', return_value=base_time):
        # 创建真实的session（expire 将为 base_time + 3600）
        real_session = auth.create_session("admin")
        token = real_session["token"]
        
        # 1. 有效session (当前时间 = base_time)
        assert auth.is_logged_in({"token": token}) is True
        
        # 2. 过期session (时间超过expire = base_time + 3600)
        with patch('app.auth.time.time', return_value=base_time + 3601):
            assert auth.is_logged_in({"token": token}) is False
        
        # 3. None session
        assert auth.is_logged_in(None) is False
        
        # 4. 缺少必要字段的session
        assert auth.is_logged_in({"token": "nonexistent"}) is False
    
    print("✓ test_session_expiration")

def test_login_then_refresh_page():
    """模拟登录后刷新页面仍能保持登录状态"""
    base_time = 4000000.0
    with patch('app.auth.time.time', return_value=base_time):
        # 1. 登录
        assert auth.login("applicant", "app123") is not None

        # 2. 创建 session（模拟服务端存储）
        session_data = auth.create_session("applicant", language="en-US")
        token = session_data["token"]
        assert token in auth.SESSIONS

        # 3. 模拟刷新页面 - 前端重新发送 token
        refreshed_session = {"token": token}

        # 4. 刷新后应仍能登录
        assert auth.is_logged_in(refreshed_session) is True
        assert auth.get_role(refreshed_session) == "applicant"
        assert auth.get_language(refreshed_session) == "en-US"
    print("✓ test_login_then_refresh_page")

if __name__ == "__main__":
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
