import subprocess
import sys
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")

# Run all test files in the directory
result = subprocess.run([sys.executable, "-m", "pytest", "-v"], 
                       capture_output=True, text=True, 
                       cwd="F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
print(result.stdout)
print(result.stderr)
print(f"Exit code: {result.returncode}")