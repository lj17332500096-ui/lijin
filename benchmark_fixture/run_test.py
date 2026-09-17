import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), 'benchmark_fixture'))
from app import auth
from unittest.mock import patch

print("auth module loaded")
print("login(applicant,app123):", auth.login("applicant", "app123"))
print("login(reviewer,rev456):", auth.login("reviewer", "rev456"))
print("login(admin,admin789):", auth.login("admin", "admin789"))
print("login(wrong,password):", auth.login("applicant", "wrong"))
print("calculate(2.5):", auth.calculate(2.5))

# Test session creation
base_time = 1000000.0
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("applicant")
    print("session keys:", list(s.keys()))
    print("role:", s.get("role"))
    print("username:", s.get("username"))
    print("expire:", s.get("expire"))
    print("language:", s.get("language"))

# Test get_role
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("reviewer")
    print("get_role(review):", auth.get_role(s))

# Test get_language
with patch('app.auth.time.time', return_value=base_time):
    s = auth.create_session("applicant")
    session_dict = {"token": s["token"]}
    print("get_language(applicant):", auth.get_language(session_dict))
    
    s2 = auth.create_session("reviewer", language="en-US")
    session_dict2 = {"token": s2["token"]}
    print("get_language(rev,en-US):", auth.get_language(session_dict2))

# Test refresh
with patch('app.auth.time.time', return_value=base_time):
    s = {"token": "old", "expire": base_time + 1000, "role": "admin", "username": "admin", "language": "zh-CN"}
    auth.refresh_token(s)
    print("refreshed token:", s["token"])
    print("refreshed expire:", s["expire"])
    print("refreshed role:", s["role"])
    print("refreshed language:", s["language"])

# Test is_logged_in
with patch('app.auth.time.time', return_value=base_time):
    real_session = auth.create_session("admin")
    token = real_session["token"]
    print("is_logged_in(valid):", auth.is_logged_in({"token": token}))
    
    with patch('app.auth.time.time', return_value=base_time + 3601):
        print("is_logged_in(expired):", auth.is_logged_in({"token": token}))
    
    print("is_logged_in(None):", auth.is_logged_in(None))
    print("is_logged_in(invalid):", auth.is_logged_in({"token": "nonexistent"}))
