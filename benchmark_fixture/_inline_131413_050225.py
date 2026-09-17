import subprocess, sys
result = subprocess.run(
    [sys.executable, "-m", "pytest", 
     r"F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture/tests/", "-v", "--tb=short"],
    capture_output=True, text=True
)
print(result.stdout)
print(result.stderr)
print(f"Exit code: {result.returncode}")