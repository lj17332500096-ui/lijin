import sys, os
sys.path.insert(0, 'F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture')
from app import auth
import threading
import time
from unittest.mock import patch

print("=== BUG A: detailed trace with mock time ===")
base_time = 5000000.0
with patch('app.auth.time.time', return_value=base_time):
    auth.SESSIONS.clear()
    s = auth.create_session("admin")
    token1 = s["token"]
    session = {"token": token1}
    print(f"Created: token1={token1}")
    print(f"SESSIONS before race: {list(auth.SESSIONS.keys())}")

    # Interleaved manually to show the race:
    # Step 1: refresh_token acquires lock, reads old token, releases lock
    # Step 2: get_language acquires lock, reads old token (same!), releases lock
    # Step 3: refresh_token generates new_token (same as get_language will), writes, deletes old
    # Step 4: get_language generates new_token (SAME hash!), writes to SAME key, deletes old (which is the new token!)
    
    # Let's verify: both generate same new_token?
    raw1 = f"admin-{base_time}"
    h1 = auth.hashlib.sha256(raw1.encode()).hexdigest()[:16]
    new_token_same = f"session-{h1}-admin"
    print(f"Expected new_token from both: {new_token_same}")
    print(f"token1 == new_token_same? {token1 == new_token_same}")
    
    # Simulate the race
    print("\nSimulating the race manually:")
    auth.SESSIONS.clear()
    auth.SESSIONS[token1] = {"token": token1, "expire": base_time + 3600, "role": "admin", "username": "admin", "language": "zh-CN"}
    
    # Thread 1 (refresh_token): reads old token under lock
    with auth.SESSION_LOCK:
        persisted_r = auth.SESSIONS.get(token1)
    print(f"  refresh_token read persisted: {persisted_r['token'][:30]}...")
    
    # Thread 2 (get_language): reads old token under lock (after Thread 1 released)
    with auth.SESSION_LOCK:
        persisted_l = auth.SESSIONS.get(token1)
    print(f"  get_language read persisted: {persisted_l['token'][:30]}...")
    
    # Both generate same new_token (same username + same time)
    print(f"  Both generate new_token = {new_token_same}")
    print(f"  new_token_same == token1? {new_token_same == token1}")
    
    # refresh_token writes new_token and deletes token1
    with auth.SESSION_LOCK:
        auth.SESSIONS[new_token_same] = {"token": new_token_same, "expire": base_time + 3600, "role": "admin", "username": "admin", "language": "zh-CN"}
        if token1 in auth.SESSIONS:
            del auth.SESSIONS[token1]
    print(f"  After refresh_token: SESSIONS keys = {list(auth.SESSIONS.keys())}")
    
    # get_language writes new_token_same (OVERWRITES same key) and deletes token1 (ALREADY GONE)
    with auth.SESSION_LOCK:
        auth.SESSIONS[new_token_same] = {"token": new_token_same, "expire": base_time + 3600, "role": "admin", "username": "admin", "language": "zh-CN"}
        if token1 in auth.SESSIONS:
            del auth.SESSIONS[token1]  # token1 already deleted! This is fine but...
    print(f"  After get_language: SESSIONS keys = {list(auth.SESSIONS.keys())}")
    print(f"  Final session['token'] = {session['token']}")
    print(f"  session['token'] in SESSIONS? {session['token'] in auth.SESSIONS}")
    print("\nIn this case it works because they generate the SAME new_token.")
    print("BUT the REAL race is when time.time() differs between the two calls!")

print("\n=== BUG A real race: time differs between lock releases ===")
call_count = [0]
def fake_time():
    call_count[0] += 1
    return base_time + (call_count[0] * 0.0001)  # slightly increasing

with patch('app.auth.time.time', side_effect=fake_time):
    auth.SESSIONS.clear()
    s = auth.create_session("admin")
    token1 = s["token"]
    session = {"token": token1}
    print(f"Created: token1={token1}")
    
    # Manually simulate the race with different time values
    t1 = fake_time()  # refresh_token reads
    t2 = fake_time()  # get_language reads
    print(f"time used by refresh_token: {t1}")
    print(f"time used by get_language: {t2}")
    
    raw_r = f"admin-{t1}"
    h_r = auth.hashlib.sha256(raw_r.encode()).hexdigest()[:16]
    new_r = f"session-{h_r}-admin"
    
    raw_l = f"admin-{t2}"
    h_l = auth.hashlib.sha256(raw_l.encode()).hexdigest()[:16]
    new_l = f"session-{h_l}-admin"
    
    print(f"refresh_token would write: {new_r}")
    print(f"get_language would write:  {new_l}")
    print(f"new_r == new_l? {new_r == new_l}")
    print(f"new_r == token1? {new_r == token1}")
    print(f"new_l == token1? {new_l == token1}")
    
    # Simulate the interleaving:
    # 1. refresh_token deletes token1, writes new_r
    # 2. get_language deletes token1 (already gone), writes new_l
    # 3. session['token'] gets set to new_r by refresh_token, then new_l by get_language
    # 4. But only one of new_r or new_l is in SESSIONS at a time!
    
    auth.SESSIONS[token1] = {"token": token1, "expire": base_time + 3600, "role": "admin", "username": "admin", "language": "zh-CN"}
    
    # refresh_token does its thing
    with auth.SESSION_LOCK:
        auth.SESSIONS[new_r] = {"token": new_r, "expire": base_time + 3600, "role": "admin", "username": "admin", "language": "zh-CN"}
        if token1 in auth.SESSIONS:
            del auth.SESSIONS[token1]
    session["token"] = new_r
    print(f"\nAfter refresh_token: SESSIONS={list(auth.SESSIONS.keys())}, session token={session['token']}")
    
    # get_language does its thing (sees token1 is gone, but session still says token1)
    # Wait - get_language reads from SESSIONS using session['token'] which is now new_r
    # Let me re-simulate properly...
    
print("\n=== Let me trace the actual concurrent execution ===")
fail_count = 0
for i in range(200):
    auth.SESSIONS.clear()
    base = 7000000.0 + i * 0.001
    call_idx = [0]
    def ft():
        call_idx[0] += 1
        return base + call_idx[0] * 0.0001
    with patch('app.auth.time.time', side_effect=ft):
        s = auth.create_session("admin")
        token1 = s["token"]
        sess = {"token": token1}
        
        errors = []
        def refresh():
            auth.refresh_token(sess)
        def language():
            auth.get_language(sess)
        
        t1 = threading.Thread(target=refresh)
        t2 = threading.Thread(target=language)
        t1.start(); t2.start()
        t1.join(); t2.join()
        
        final = sess["token"]
        if final not in auth.SESSIONS:
            fail_count += 1
            if fail_count <= 3:
                print(f"FAIL #{fail_count}: final_token={final}, SESSIONS keys={list(auth.SESSIONS.keys())}")
if fail_count == 0:
    print("No failures in 200 runs with varying time")
else:
    print(f"\nTotal failures: {fail_count}/200")