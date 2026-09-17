import sys, os
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth
import time
from unittest.mock import patch

# 详细调试 Bug2
auth.SESSIONS.clear()
base = time.time()
print(f"base={base}")
token = auth.create_session("admin")
print(f"created token: {token['token']}")
print(f"SESSIONS keys: {list(auth.SESSIONS.keys())}")

expired = {"token": "nonexistent", "expire": 1000.0, "role": "admin", "username": "admin"}
r1 = auth.is_logged_in(expired)
print(f"is_logged_in(expired) = {r1}")

valid = {"token": "test", "expire": time.time() + 3600, "role": "admin", "username": "admin"}
r2 = auth.is_logged_in(valid)
print(f"is_logged_in(valid) = {r2} (expected True)")

# Bug4
auth.SESSIONS.clear()
base_time = 4000000.0
with patch('app.auth.time.time', return_value=base_time):
    result = auth.login("applicant", "app123")
    print(f"login result type: {type(result)}, value: {result}")
    print(f"result is True: {result is True}")
    print(f"result is not None: {result is not None}")
