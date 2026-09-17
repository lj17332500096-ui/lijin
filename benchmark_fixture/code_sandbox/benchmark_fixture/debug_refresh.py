import sys
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth
from unittest.mock import patch

print("=== 调试 refresh_token bug ===\n")

auth.SESSIONS.clear()
base_time = 6000000.0
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("admin")
    original_token = s['token']
    print(f"原始 token: {original_token}")
    print(f"SESSIONS keys before refresh: {list(auth.SESSIONS.keys())}")
    
    session = {"token": original_token}
    result = auth.refresh_token(session)
    new_token = result['token']
    
    print(f"\n刷新后:")
    print(f"  新 token: {new_token}")
    print(f"  session dict token: {session['token']}")
    print(f"  SESSIONS keys after refresh: {list(auth.SESSIONS.keys())}")
    print(f"  原始 token 在 SESSIONS: {original_token in auth.SESSIONS}")
    print(f"  新 token 在 SESSIONS: {new_token in auth.SESSIONS}")
    
    # 验证功能
    print(f"\n验证 is_logged_in:")
    print(f"  用原始 token: {auth.is_logged_in({'token': original_token})}")
    print(f"  用新 token: {auth.is_logged_in({'token': new_token})}")
    print(f"  用 session dict: {auth.is_logged_in(session)}")
