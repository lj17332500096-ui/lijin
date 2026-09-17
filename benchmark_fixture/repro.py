import sys, os
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth
import time
from unittest.mock import patch

fails = []

# ---- Bug 1: refresh_token with fake session (token not in SESSIONS) ----
def test_bug1():
    base_time = 3000000.0
    with patch('app.auth.time.time', return_value=base_time):
        s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin"}
        auth.refresh_token(s)
        assert s["expire"] == base_time + 3600, f"expire mismatch: {s['expire']}"
        assert s["role"] == "admin"
        assert s["token"] != "old", f"token unchanged! token={s['token']}"
        assert s["token"].startswith("session-"), f"token format wrong: {s['token']}"
    print("✓ test_bug1 passed")

try:
    test_bug1()
except AssertionError as e:
    fails.append(f"Bug1: {e}")
    print(f"✗ BUG1: {e}")

# ---- Bug 2: session_expiration uses real time.time (race condition) ----
def test_bug2():
    # Uses real time.time() for valid session check - vulnerable to timing
    auth.SESSIONS.clear()
    base = time.time()
    token = auth.create_session("admin")
    # Any delay between create_session and is_logged_in could theoretically cause issues
    # But realistically the issue is: expired_session has expire=1000.0 which is always < time.time()
    # The test itself should pass - let's verify it doesn't have hidden issues
    expired = {"token": "nonexistent", "expire": 1000.0, "role": "admin", "username": "admin"}
    assert auth.is_logged_in(expired) is False
    # The "valid_session" uses time.time() + 3600, should be fine
    valid = {"token": "test", "expire": time.time() + 3600, "role": "admin", "username": "admin"}
    assert auth.is_logged_in(valid) is True
    assert auth.is_logged_in(None) is False
    assert auth.is_logged_in({"token": "test"}) is False
    print("✓ test_bug2 passed")

try:
    test_bug2()
except Exception as e:
    fails.append(f"Bug2: {e}")
    print(f"✗ BUG2: {e}")

# ---- Bug 3: race condition in is_logged_in with SESSION_LOCK ----
def test_bug3():
    # Concurrent access test
    import threading
    auth.SESSIONS.clear()
    results = []
    
    def worker(tid):
        token = auth.create_session("user" + str(tid))
        result = auth.is_logged_in({"token": token["token"]})
        results.append(result)
    
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert all(r is True for r in results), f"Some sessions lost: {results}"
    print("✓ test_bug3 passed (threading)")

try:
    test_bug3()
except Exception as e:
    fails.append(f"Bug3: {e}")
    print(f"✗ BUG3: {e}")

# ---- Bug 4: test_run.py vs test_test_auth.py inconsistency ----
def test_bug4():
    # test_run.py and test_test_auth.py test different things subtly
    # test_run.py's test_session_expiration calls test_session_expiration() INSIDE test_login_then_refresh_page
    # but doesn't return from that function - this is a latent bug if run via pytest
    auth.SESSIONS.clear()
    base_time = 4000000.0
    with patch('app.auth.time.time', return_value=base_time):
        assert auth.login("applicant", "app123") is True
        token = auth.create_session("applicant")
        refreshed = {"token": token["token"]}
        assert auth.is_logged_in(refreshed) is True
        assert auth.get_role(refreshed) == "applicant"
    print("✓ test_bug4 passed (logic consistency)")

try:
    test_bug4()
except Exception as e:
    fails.append(f"Bug4: {e}")
    print(f"✗ BUG4: {e}")

# ---- Bug 5: refresh_token doesn't handle missing token in SESSIONS ----
def test_bug5():
    base_time = 3000000.0
    with patch('app.auth.time.time', return_value=base_time):
        # Create a session, then remove it manually
        token = auth.create_session("admin")
        del auth.SESSIONS[token["token"]]
        session = {"token": token["token"]}
        result = auth.refresh_token(session)
        # Should NOT crash, should return session unchanged or handle gracefully
        print(f"  refresh_token on missing session returned: token={result['token']}, expire={result.get('expire')}")
        # If it returns unchanged, that's the bug - should regenerate or handle
    print("✓ test_bug5 passed")

try:
    test_bug5()
except Exception as e:
    fails.append(f"Bug5: {e}")
    print(f"✗ BUG5: {e}")

print(f"\n=== Bugs found: {len(fails)} ===")
for f in fails:
    print(f"  - {f}")
