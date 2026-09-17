import subprocess
import sys
import os

os.chdir(r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture")

# Run pytest
result = subprocess.run([sys.executable, "-m", "pytest", "tests/test_auth.py", "-v"], capture_output=True, text=True)
print("=== PYTEST OUTPUT ===")
print(result.stdout)
print(result.stderr)
print(f"Exit code: {result.returncode}")

# Also run the script tests
print("\n=== RUN_TESTS OUTPUT ===")
result2 = subprocess.run([sys.executable, "run_tests.py"], capture_output=True, text=True)
print(result2.stdout)
print(result2.stderr)
print(f"Exit code: {result2.returncode}")

print("\n=== RUN_TESTS_CHECK OUTPUT ===")
result3 = subprocess.run([sys.executable, "run_tests_check.py"], capture_output=True, text=True)
print(result3.stdout)
print(result3.stderr)
print(f"Exit code: {result3.returncode}")
