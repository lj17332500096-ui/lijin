import sys, os
os.chdir('F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture')
import subprocess
result = subprocess.run([sys.executable, '-m', 'pytest', '.', '-v'], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)
