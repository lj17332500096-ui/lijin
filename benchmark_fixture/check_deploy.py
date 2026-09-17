import subprocess
import sys
import os

# Check git log in parent directories
dirs_to_check = [
    r"F:\Byong-hermes\Byong-hermes",
    r"F:\Byong-hermes\Byong-hermes\my_creative_agent",
    r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture"
]

for d in dirs_to_check:
    if os.path.exists(d):
        print(f"\n=== Checking: {d} ===")
        result = subprocess.run(
            ["git", "log", "--oneline", "-10", "--decorate"],
            cwd=d,
            capture_output=True,
            text=True,
            timeout=15
        )
        print(result.stdout)
        if result.stderr:
            print("Error:", result.stderr[:200])
