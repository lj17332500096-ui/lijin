path = "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture/run_tests.py"
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: test_get_role
old1 = '''def test_get_role():
    base_time = 2000000.0
    with patch('app.auth.time.time', return_value=base_time):
        token = auth.create_session("reviewer")
        session = {"token": token}
        assert auth.get_role(session) == "reviewer"

        token = auth.create_session("admin")
        session = {"token": token}
        assert auth.get_role(session) == "admin"
    print("✓ test_get_role")'''

new1 = '''def test_get_role():
    base_time = 2000000.0
    with patch('app.auth.time.time', return_value=base_time):
        session_data = auth.create_session("reviewer")
        session = {"token": session_data["token"]}
        assert auth.get_role(session) == "reviewer"

        session_data = auth.create_session("admin")
        session = {"token": session_data["token"]}
        assert auth.get_role(session) == "admin"
    print("✓ test_get_role")'''

content = content.replace(old1, new1)

# Fix 2: test_token_kept_after_refresh
old2 = '''def test_token_kept_after_refresh():
    base_time = 3000000.0
    with patch('app.auth.time.time', return_value=base_time):
        token = auth.create_session("admin")
        session = {"token": token}
        auth.refresh_token(session)
        assert session["expire"] == base_time + 3600
        assert session["token"] != token  # token must change
        assert session["token"].startswith("session-")
        # 新 token 也应在 SESSIONS 中
        assert session["token"] in auth.SESSIONS
        assert auth.SESSIONS[session["token"]]["role"] == "admin"
    print("✓ test_token_kept_after_refresh")'''

new2 = '''def test_token_kept_after_refresh():
    base_time = 3000000.0
    with patch('app.auth.time.time', return_value=base_time):
        session_data = auth.create_session("admin")
        session = {"token": session_data["token"]}
        auth.refresh_token(session)
        assert session["expire"] == base_time + 3600
        assert session["token"] != session_data["token"]  # token must change
        assert session["token"].startswith("session-")
        # 新 token 也应在 SESSIONS 中
        assert session["token"] in auth.SESSIONS
        assert auth.SESSIONS[session["token"]]["role"] == "admin"
    print("✓ test_token_kept_after_refresh")'''

content = content.replace(old2, new2)

# Fix 3: test_login_then_refresh_page
old3 = '''def test_login_then_refresh_page():
    """模拟登录后刷新页面仍能保持登录状态"""
    base_time = 4000000.0
    with patch('app.auth.time.time', return_value=base_time):
        # 1. 登录
        assert auth.login("applicant", "app123") is True
        
        # 2. 创建 session（模拟服务端存储）
        token = auth.create_session("applicant")
        assert token in auth.SESSIONS
        
        # 3. 模拟刷新页面 - 前端重新发送 token
        refreshed_session = {"token": token}
        
        # 4. 刷新后应仍能登录
        assert auth.is_logged_in(refreshed_session) is True
        assert auth.get_role(refreshed_session) == "applicant"
    print("✓ test_login_then_refresh_page")'''

new3 = '''def test_login_then_refresh_page():
    """模拟登录后刷新页面仍能保持登录状态"""
    base_time = 4000000.0
    with patch('app.auth.time.time', return_value=base_time):
        # 1. 登录
        assert auth.login("applicant", "app123") is True
        
        # 2. 创建 session（模拟服务端存储）
        session_data = auth.create_session("applicant")
        token = session_data["token"]
        assert token in auth.SESSIONS
        
        # 3. 模拟刷新页面 - 前端重新发送 token
        refreshed_session = {"token": token}
        
        # 4. 刷新后应仍能登录
        assert auth.is_logged_in(refreshed_session) is True
        assert auth.get_role(refreshed_session) == "applicant"
    print("✓ test_login_then_refresh_page")'''

content = content.replace(old3, new3)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed!")