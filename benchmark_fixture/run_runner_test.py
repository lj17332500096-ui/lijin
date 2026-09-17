import subprocess
import sys

result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/test_auth_runner.py", "-v", "--tb=long"],
    capture_output=True,
    text=True,
    cwd=r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture"
)
print(result.stdout)
print("STDERR:", result.stderr)
print("EXIT:", result.returncode)
