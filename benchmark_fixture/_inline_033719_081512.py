# 检查 is_logged_in 的问题
import sys
sys.path.insert(0, r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture")
from app import auth
import time
auth.SESSIONS.clear()

# 测试场景：valid_session 没有存入 SESSIONS
valid_session = {"token": "test", "expire": time.time() + 3600, "role": "admin", "username": "admin", "language": "zh-CN"}
print(f"'test' in SESSIONS: {'test' in auth.SESSIONS}")
print(f"is_logged_in(valid_session): {auth.is_logged_in(valid_session)}")

# 正确的做法：先存入 SESSIONS
auth.SESSIONS["test"] = valid_session
print(f"After adding to SESSIONS, is_logged_in: {auth.is_logged_in(valid_session)}")