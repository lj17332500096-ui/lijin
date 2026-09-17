import subprocess
import sys

result = subprocess.run(
    [sys.executable, "-m", "pytest", "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture/tests/", "-v", "--tb=short"],
    capture_output=True,
    text=True
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)
print("EXIT:", result.returncode)