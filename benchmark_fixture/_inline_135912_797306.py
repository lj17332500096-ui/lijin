import subprocess, sys
result = subprocess.run([sys.executable, "-m", "pytest", "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture/tests/test_auth.py", "-v", "--tb=short"], capture_output=True, text=True)
print(result.stdout[-5000:])
if result.stderr:
    print("STDERR:", result.stderr[-2000:])
print("EXIT:", result.returncode)