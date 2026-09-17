import sys
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth
auth.SESSIONS.clear()

# 测试1: 正常情况
print("测试1: 正常时间")
session1 = auth.create_session("admin")
print(f"token1: {session1['token']}")

session2 = auth.create_session("admin")
print(f"token2: {session2['token']}")
print(f"不同时间生成token不同: {session1['token'] != session2['token']}")

# 测试2: mock 固定时间
print("\n测试2: mock 固定时间")
from unittest.mock import patch
import time

base_time = 1000000.0
with patch('app.auth.time.time', return_value=base_time):
    auth.SESSIONS.clear()
    session1 = auth.create_session("admin")
    print(f"token1: {session1['token']}")
    
    session2 = auth.create_session("admin")
    print(f"token2: {session2['token']}")
    print(f"不同时间生成token相同: {session1['token'] == session2['token']}")