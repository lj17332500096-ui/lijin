import sys, os
sys.path.insert(0, 'F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture')
from app import auth
import time
from unittest.mock import patch

print("=== Confirmed: hash collision under constant time ===")
base_time = 6000000.0
with patch('app.auth.time.time', return_value=base_time):
    auth.SESSIONS.clear()
    s = auth.create_session("admin")
    t1 = s["token"]
    print(f"create_session token: {t1}")
    
    # refresh_token uses same formula → same token
    session = {"token": t1}
    auth.refresh_token(session)
    t2 = session["token"]
    print(f"After refresh_token:  {t2}")
    print(f"Tokens match: {t1 == t2}")
    print(f"SESSIONS empty? {len(auth.SESSIONS) == 0}")
    print(f"Is logged in? {auth.is_logged_in(session)}")
    print()
    print("ROOT CAUSE:")
    print("  create_session / refresh_token / get_language ALL use:")
    print('    raw = f"{username}-{time.time()}"')
    print("  With mocked constant time, they all generate IDENTICAL tokens.")
    print("  refresh_token then does:")
    print("    SESSIONS[new_token] = ...  (writes to SAME key)")
    print("    del SESSIONS[token]        (deletes the same key!)")
    print("  → SESSIONS ends up EMPTY, but session dict still has the token.")
    print("  → is_logged_in checks SESSIONS[token] → not found → falls back to session['expire']")
    print("  → BUT session['expire'] was also overwritten by refresh_token!")
    print()
    print("=== Race between refresh_token and get_language ===")
    auth.SESSIONS.clear()
    s = auth.create_session("admin")
    t1 = s["token"]
    session = {"token": t1}
    
    # Simulate: refresh_token runs first, generates new token (different time)
    import threading
    results = []
    
    def do_refresh():
        auth.refresh_token(session)
        results.append(("refresh", session["token"], "in_SESSIONS" if session["token"] in auth.SESSIONS else "NOT_IN_SESSIONS"))
    
    def do_language():
        auth.get_language(session)
        results.append(("language", session["token"], "in_SESSIONS" if session["token"] in auth.SESSIONS else "NOT_IN_SESSIONS"))
    
    # Run 500 times to find any race
    fail_count = 0
    for i in range(500):
        auth.SESSIONS.clear()
        s = auth.create_session("admin")
        t1 = s["token"]
        session = {"token": t1}
        results_local = []
        def rr():
            auth.refresh_token(session)
            results_local.append(("r", session["token"], session["token"] in auth.SESSIONS))
        def rl():
            auth.get_language(session)
            results_local.append(("l", session["token"], session["token"] in auth.SESSIONS))
        t1r = threading.Thread(target=rr)
        t2r = threading.Thread(target=rl)
        t1r.start(); t2r.start()
        t1r.join(); t2r.join()
        if not session["token"] in auth.SESSIONS:
            fail_count += 1
            if fail_count <= 2:
                print(f"  FAIL #{fail_count}: token={session['token']}, SESSIONS={list(auth.SESSIONS.keys())}, results={results_local}")
    print(f"  Failures: {fail_count}/500")