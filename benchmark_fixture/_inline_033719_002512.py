import subprocess
import sys
import os
import time

project_dir = r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture"
os.chdir(project_dir)

# 运行多次 pytest 观察稳定性
print("=== 连续运行 5 次 pytest ===")
for i in range(5):
    result = subprocess.run([sys.executable, "-m", "pytest", "tests/test_auth.py", "-v", "--tb=short"], capture_output=True, text=True)
    lines = result.stdout.strip().split('\n')
    summary = [l for l in lines if 'FAILED' in l or 'passed' in l or 'error' in l.lower()]
    print(f"Run {i+1}: {summary[-1] if summary else 'no summary'}")
    if 'FAILED' in result.stdout:
        print(f"  Failures: {[l for l in lines if 'FAILED' in l]}")