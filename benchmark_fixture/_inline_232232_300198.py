import sys, subprocess, os
os.chdir(r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture")
result = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"], capture_output=True, text=True)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)
print("EXIT:", result.returncode)