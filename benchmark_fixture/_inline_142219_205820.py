import sys, os
sys.path.insert(0, 'F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture')
from app import auth
import threading
import time
from unittest.mock import patch

print("=== BUG A detailed race trace (mock time) ===")
base_time = 5000000.0
with patch('app.auth.time.time', return_value=base_time):
    for i in range(200):
        auth.SESSIONS.clear()
        s = auth.create_session("admin")
        token1 = s["token"]
        session = {"token": token1}
        results = []
        def do_refresh():
            auth.refresh_token(session)
            results.append(("refresh", session["token"], auth.SESSIONS.get(session["token"], {}).get("expire", 0)))
        def do_language():
            auth.get_language(session)
            results.append(("language", session.get("token"), auth.SESSIONS.get(session.get("token"), {}).get("expire", 0)))
        t1 = threading.Thread(target=do_refresh)
        t2 = threading.Thread(target=do_language)
        t1.start(); t2.start()
        t1.join(); t2.join()
        final_token = session["token"]
        if final_token not in auth.SESSIONS:
            print(f"FAIL run={i}: token={final_token} NOT in SESSIONS, SESSIONS keys={list(auth.SESSIONS.keys())}, results={results}")
            break
    else:
        print("No failure in 200 mock-time runs")

print("\n=== BUG B: hash collision check ===")
base_time = 6000000.0
with patch('app.auth.time.time', return_value=base_time):
    auth.SESSIONS.clear()
    s1 = auth.create_session("admin")
    token1 = s1["token"]
    session = {"token": token1}
    auth.refresh_token(session)
    token2 = session["token"]
    print(f"token1={token1}")
    print(f"token2={token2}")
    print(f"SAME TOKEN: {token1 == token2}")
    if token1 == token2:
        print("BUG CONFIRMED: create_session and refresh_token produce same token under mock time!")
        print(f"SESSIONS after refresh: {list(auth.SESSIONS.keys())}")
        print(f"session dict now has: token={session['token']}, but this token maps to... wait it IS the same key")
        print(f"This means refresh_token deleted the token it just created!")