import subprocess, sys
result = subprocess.run([sys.executable, "run_tests.py"], capture_output=True, text=True, cwd="F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)
print("EXIT:", result.returncode)