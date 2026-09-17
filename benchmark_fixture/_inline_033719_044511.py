# 检查 refresh_token 的行为：当 token 不在 SESSIONS 时会发生什么
import sys
sys.path.insert(0, r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture")
from app import auth
auth.SESSIONS.clear()

# 模拟测试场景
base_time = 3000000.0
s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin", "language": "zh-CN"}

print(f"Before refresh: s={s}")
print(f"'old' in SESSIONS: {'old' in auth.SESSIONS}")

# 看看 refresh_token 会返回什么
result = auth.refresh_token(s)
print(f"After refresh: result={result}")
print(f"s after refresh: {s}")
print(f"s is result: {s is result}")