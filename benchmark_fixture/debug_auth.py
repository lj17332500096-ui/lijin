import sys
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth
from unittest.mock import patch

print("=== 深入测试 auth.py ===\n")

# 测试 1: _generate_token 是否被使用
print("测试 1: 检查 _generate_token 使用情况")
print(f"  _generate_token 定义: {hasattr(auth, '_generate_token')}")
print(f"  create_session 内部: 是否调用 _generate_token? ", end="")
import inspect
src = inspect.getsource(auth.create_session)
print("YES" if "_generate_token" in src else "NO - 这是设计缺陷!")

# 测试 2: 同一秒内创建多个 session 是否有 token 冲突
print("\n测试 2: 快速连续创建 session")
auth.SESSIONS.clear()
base_time = 5000000.0
with patch('app.auth.time.time', return_value=base_time):
    s1 = auth.create_session("admin")
    s2 = auth.create_session("applicant")
    print(f"  admin token: {s1['token']}")
    print(f"  applicant token: {s2['token']}")
    print(f"  tokens 相同: {s1['token'] == s2['token']}")
    if s1['token'] == s2['token']:
        print("  ❌ BUG: 相同时间戳导致 token 冲突!")
    else:
        print("  ✓ tokens 不同")

# 测试 3: refresh_token 后 token 是否在 SESSIONS 中
print("\n测试 3: refresh_token 后 SESSIONS 状态")
auth.SESSIONS.clear()
base_time = 6000000.0
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("admin")
    print(f"  原始 token 在 SESSIONS: {s['token'] in auth.SESSIONS}")
    session = {"token": s["token"]}
    result = auth.refresh_token(session)
    print(f"  原始 token 仍在 SESSIONS: {s['token'] in auth.SESSIONS}")
    print(f"  新 token 在 SESSIONS: {result['token'] in auth.SESSIONS}")

# 测试 4: guest 角色测试
print("\n测试 4: guest 角色")
auth.SESSIONS.clear()
base_time = 7000000.0
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("guest")
    print(f"  guest role: {s['role']}")
    print(f"  guest username: {s['username']}")

# 测试 5: invalid token 的 get_language
print("\n测试 5: invalid token 处理")
result = auth.get_language({"token": "nonexistent"})
print(f"  get_language(invalid_token): {result}")

print("\n=== 检查完成 ===")
