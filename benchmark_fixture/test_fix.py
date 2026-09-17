import sys
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth
from unittest.mock import patch

print("=== 测试 Bug 修复 ===")
print()

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
    assert result['token'] != 'old', "BUG: token 未改变!"
    assert result['expire'] == base_time + 3600, f"BUG: expire 不正确: {result['expire']}"
    assert result['role'] == 'admin', "BUG: role 丢失!"
    assert result['language'] == 'zh-CN', "BUG: language 丢失!"
    print("  ✓ 通过")

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
    assert result['token'] != token['token'], "BUG: token 未改变!"
    assert result['expire'] == base_time + 3600, f"BUG: expire 不正确"
    assert result['token'] in auth.SESSIONS, "BUG: 新 token 不在 SESSIONS 中!"
    print("  ✓ 通过")

print()
print("=== 所有测试通过! Bug 已修复 ===")
