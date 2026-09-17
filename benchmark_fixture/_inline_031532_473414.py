import subprocess
result = subprocess.run(
    ["python", "-m", "pytest", "tests/test_auth.py", "-v"],
    cwd="F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture",
    capture_output=True,
    text=True
)
print(result.stdout)
print(result.stderr)