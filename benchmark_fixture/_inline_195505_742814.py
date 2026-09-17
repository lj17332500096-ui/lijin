import subprocess
import sys

# 检查 requirements.txt 中列出的依赖
result = subprocess.run(
    [sys.executable, "-m", "pip", "list"],
    capture_output=True,
    text=True
)
print("已安装的包:")
print(result.stdout)

# 检查 pytest 版本
result2 = subprocess.run(
    [sys.executable, "-m", "pip", "show", "pytest"],
    capture_output=True,
    text=True
)
print("\npytest 详情:")
print(result2.stdout)