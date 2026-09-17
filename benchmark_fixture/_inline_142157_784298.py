import sys, os
sys.path.insert(0, 'F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture')
from app import auth
import threading
import time
from unittest.mock import patch

fails = []

# Bug A: race between refresh_token and get_language
def test_race_refresh_language():
    fails_local = []
    base_time = 5000000.0
    with patch('app.auth.time.time', return_value=base_time):
        for run in range(100):
            auth.SESSIONS.clear()
            s = auth.create_session("admin")
            token = s["token"]
            session = {"token": token}
            results = []
            def do_refresh():
                auth.refresh_token(session)
                results.append(("refresh", session.get("token")))
            def do_language():
                lang = auth.get_language(session)
                results.append(("language", lang, session.get("token")))
            t1 = threading.Thread(target=do_refresh)
            t2 = threading.Thread(target=do_language)
            t1.start(); t2.start()
            t1.join(); t2.join()
            final_token = session.get("token")
            if final_token is None or final_token not in auth.SESSIONS:
                fails_local.append(f"run={run} token={final_token} not in SESSIONS, results={results}")
                break
            if auth.SESSIONS.get(final_token, {}).get("expire", 0) <= base_time:
                fails_local.append(f"run={run} expired={auth.SESSIONS.get(final_token, {}).get('expire')}")
                break
    if fails_local:
        print(f"BUG A (race): {fails_local[:3]}")
    else:
        print("BUG A passed (no race in 100 runs)")
test_race_refresh_language()

# Bug B: hash collision between create_session and refresh_token under mock time
def test_hash_collision():
    fails_local = []
    base_time = 6000000.0
    with patch('app.auth.time.time', return_value=base_time):
        for run in range(100):
            auth.SESSIONS.clear()
            s1 = auth.create_session("admin")
            token1 = s1["token"]
            session = {"token": token1}
            auth.refresh_token(session)
            token2 = session["token"]
            if token1 == token2:
                fails_local.append(f"run={run} SAME TOKEN after refresh!")
                break
    if fails_local:
        print(f"BUG B (hash collision): {fails_local[:3]}")
    else:
        print("BUG B passed (no collision in 100 runs)")
test_hash_collision()

# Bug C: concurrent refresh_token calls
def test_concurrent_refresh():
    fails_local = []
    base_time = 8000000.0
    with patch('app.auth.time.time', return_value=base_time):
        for run in range(100):
            auth.SESSIONS.clear()
            s = auth.create_session("admin")
            token1 = s["token"]
            session = {"token": token1}
            results = []
            def do_refresh1():
                auth.refresh_token(session)
                results.append(("r1", session["token"]))
            def do_refresh2():
                auth.refresh_token(session)
                results.append(("r2", session["token"]))
            t1 = threading.Thread(target=do_refresh1)
            t2 = threading.Thread(target=do_refresh2)
            t1.start(); t2.start()
            t1.join(); t2.join()
            final_token = session["token"]
            if final_token not in auth.SESSIONS:
                fails_local.append(f"run={run} final token not in SESSIONS, results={results}")
                break
    if fails_local:
        print(f"BUG C (concurrent refresh): {fails_local[:3]}")
    else:
        print("BUG C passed (no issue in 100 runs)")
test_concurrent_refresh()

# Bug D: is_logged_in fallback uses session expire but persisted is gone
def test_is_logged_in_after_refresh():
    fails_local = []
    base_time = 9000000.0
    with patch('app.auth.time.time', return_value=base_time):
        for run in range(100):
            auth.SESSIONS.clear()
            s = auth.create_session("admin")
            token1 = s["token"]
            session = {"token": token1}
            auth.refresh_token(session)
            token2 = session["token"]
            # is_logged_in should check SESSIONS[token2] which has expire=base+3600
            if not auth.is_logged_in(session):
                fails_local.append(f"run={run} is_logged_in=False after refresh!")
                break
    if fails_local:
        print(f"BUG D (is_logged_in after refresh): {fails_local[:3]}")
    else:
        print("BUG D passed")
test_is_logged_in_after_refresh()

print("\nDone.")