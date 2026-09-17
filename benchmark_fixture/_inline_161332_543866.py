import sys
sys.path.insert(0, 'F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture')
import subprocess
result = subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_auth.py', '-v'], cwd='F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture', capture_output=True, text=True)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)
print(f"Exit code: {result.returncode}")