import sys, os
os.chdir(r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture")
sys.path.insert(0, '.')
from app import auth

# Check what login returns
result = auth.login("applicant", "app123")
print(f"login returns: {type(result).__name__}, value={result}")
print(f"login is not None: {result is not None}")
print(f"login is True: {result is True}")
print(f"login == True: {result == True}")