import subprocess
import sys
import os

project_dir = r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture"
os.chdir(project_dir)

# 检查 pytest 安装
result = subprocess.run(
    [sys.executable, "-m", "pip", "show", "pytest"],
    capture_output=True,
    text=True
)
print("=== pytest 信息 ===")
print(result.stdout if result.stdout else result.stderr)

# 尝试安装依赖
result2 = subprocess.run(
    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
    capture_output=True,
    text=True
)
print("=== pip install 输出 ===")
print(result2.stdout)
if result2.stderr:
    print("STDERR:", result2.stderr)

# 运行测试
result3 = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"],
    capture_output=True,
    text=True
)
print("=== pytest 运行结果 ===")
print(result3.stdout)
if result3.stderr:
    print("STDERR:", result3.stderr)
print("EXIT:", result3.returncode)
