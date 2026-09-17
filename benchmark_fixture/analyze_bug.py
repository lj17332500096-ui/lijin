import sys
sys.path.insert(0, r'F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture')
from app import auth
import time
from unittest.mock import patch

# 分析 bug
print("=== Bug Analysis ===\n")

# Bug 1: _generate_token 是死代码，login() 调用的是 create_session()
print("1. _generate_token() never called:")
print(f"   login() calls create_session(), not _generate_token()")
print(f"   _generate_token uses counter, create_session does not")
print()

# Bug 2: is_logged_in 会隐式刷新 expire
print("2. is_logged_in implicitly refreshes expire:")
base_time = 1000000.0
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("admin")
    token = s["token"]
    original_expire = s["expire"]
    print(f"   Created session with expire={original_expire}")
    auth.is_logged_in({"token": token})
    refreshed_expire = auth.SESSIONS[token]["expire"]
    print(f"   After is_logged_in, expire={refreshed_expire}")
    print(f"   Bug: expire was modified from {original_expire} to {refreshed_expire}")
print()

# Bug 3: get_language 生成新 token 但测试可能依赖旧 token
print("3. get_language generates new token:")
base_time = 2000000.0
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("applicant", language="en-US")
    old_token = s["token"]
    session_dict = {"token": old_token}
    auth.get_language(session_dict)
    new_token = session_dict["token"]
    print(f"   Old token: {old_token}")
    print(f"   New token: {new_token}")
    print(f"   Bug: token changed, original token no longer valid in SESSIONS")
print()

# 检查 SESSIONS 状态
print(f"Current SESSIONS count: {len(auth.SESSIONS)}")
for token, data in auth.SESSIONS.items():
    print(f"  {token}: role={data['role']}, username={data['username']}, expire={data['expire']}")
