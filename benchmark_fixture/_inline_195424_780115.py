import sys
sys.path.insert(0, 'F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture')
from app import auth
from unittest.mock import patch
import time

# 测试 get_language 是否修改 SESSIONS
base_time = 2100000.0
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("applicant")
    old_token = s["token"]
    print(f"创建 session, token: {old_token}")
    
    # 检查 SESSIONS 中的 token
    print(f"SESSIONS 中的 token: {list(auth.SESSIONS.keys())}")
    
    # 调用 get_language
    session_dict = {"token": old_token}
    result = auth.get_language(session_dict)
    print(f"get_language 返回: {result}")
    
    # 检查调用后 SESSIONS 是否有变化
    print(f"调用后 SESSIONS: {list(auth.SESSIONS.keys())}")
    print(f"新 token 是否与旧 token 相同: {old_token in auth.SESSIONS}")