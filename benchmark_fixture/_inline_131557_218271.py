import subprocess, sys
result = subprocess.run([sys.executable, "-m", "pytest", "--version"], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)
print("returncode:", result.returncode)