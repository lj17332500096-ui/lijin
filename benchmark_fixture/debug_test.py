import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "benchmark_fixture"))
os.chdir("benchmark_fixture")

from app import auth
from unittest.mock import patch

# Test 1: run_tests.py issue - create_session returns dict, not string
base_time = 1000000.0
with patch('app.auth.time.time', return_value=base_time):
    token = auth.create_session("applicant")
    print(f"create_session returns: {token}")
    print(f"type: {type(token)}")
    
    # Check what's in SESSIONS
    print(f"SESSIONS keys: {list(auth.SESSIONS.keys())}")
    for k, v in auth.SESSIONS.items():
        print(f"  key={k}, value={v}")

print("\n--- Test 2: refresh_token expire issue ---")
base_time = 3000000.0
with patch('app.auth.time.time', return_value=base_time):
    s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin", "language": "zh-CN"}
    print(f"Before refresh: expire={s['expire']}")
    auth.refresh_token(s)
    print(f"After refresh: expire={s['expire']}")
    print(f"Expected: {base_time + 3600}")
