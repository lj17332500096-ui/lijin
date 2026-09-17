import sys, os
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth
from unittest.mock import patch

print("=== 测试 refresh_token bug ===\n")

# 测试 1: token 不在 SESSIONS 中（原始 bug 场景）
print("测试 1: token='old' 不在 SESSIONS 中")
auth.SESSIONS.clear()
base_time = 3000000.0
with patch('app.auth.time.time', return_value=base_time):
    s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin", "language": "zh-CN"}
    result = auth.refresh_token(s)
    print(f"  原 token: 'old'")
    print(f"  新 token: '{result['token']}'")
    print(f"  新 expire: {result['expire']} (期望: {base_time + 3600})")
    print(f"  role 保持: {result['role']}")
    print(f"  language 保持: {result['language']}")
    if result['token'] == 'old':
        print("  ✗ BUG: token 未改变!")
    else:
        print("  ✓ token 已刷新")
    if result['expire'] != base_time + 3600:
        print(f"  ✗ BUG: expire 不正确: {result['expire']}")
    else:
        print("  ✓ expire 正确")
    if result['role'] != 'admin':
        print("  ✗ BUG: role 丢失!")
    else:
        print("  ✓ role 保持")
    if result['language'] != 'zh-CN':
        print("  ✗ BUG: language 丢失!")
    else:
        print("  ✓ language 保持")

print()

# 测试 2: token 在 SESSIONS 中（正常场景）
print("测试 2: token 在 SESSIONS 中")
auth.SESSIONS.clear()
base_time = 3100000.0
with patch('app.auth.time.time', return_value=base_time):
    token = auth.create_session("admin")
    session = {"token": token["token"]}
    result = auth.refresh_token(session)
    print(f"  原 token: '{token['token']}'")
    print(f"  新 token: '{result['token']}'")
    print(f"  新 expire: {result['expire']} (期望: {base_time + 3600})")
    if result['token'] == token['token']:
        print("  ✗ BUG: token 未改变!")
    else:
        print("  ✓ token 已刷新")
    if result['expire'] != base_time + 3600:
        print(f"  ✗ BUG: expire 不正确")
    else:
        print("  ✓ expire 正确")
    if result['token'] not in auth.SESSIONS:
        print("  ✗ BUG: 新 token 不在 SESSIONS 中!")
    else:
        print("  ✓ 新 token 在 SESSIONS 中")

print()

# 测试 3: 重复刷新（关键 bug 场景）
print("测试 3: 重复刷新 - 同一 mock 时间")
auth.SESSIONS.clear()
base_time = 4000000.0
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("admin")
    token1 = s["token"]
    print(f"  初始 token: {token1}")
    
    # 第一次刷新
    session = {"token": token1}
    result1 = auth.refresh_token(session)
    token2 = result1["token"]
    print(f"  第一次刷新后: {token2}")
    
    # 第二次刷新
    result2 = auth.refresh_token(session)
    token3 = result2["token"]
    print(f"  第二次刷新后: {token3}")
    
    print(f"  SESSIONS keys: {list(auth.SESSIONS.keys())}")
    print(f"  is_logged_in(session): {auth.is_logged_in(session)}")
    
    if token1 == token2:
        print("  ✗ BUG: 第一次刷新 token 未改变!")
    if token2 == token3:
        print("  ✗ BUG: 第二次刷新 token 未改变!")
    if len(auth.SESSIONS) == 0:
        print("  ✗ BUG: SESSIONS 为空!")
    if not auth.is_logged_in(session):
        print("  ✗ BUG: 刷新后无法登录!")