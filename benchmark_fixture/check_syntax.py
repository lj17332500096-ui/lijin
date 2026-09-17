# 检查并修复 test_auth.py 的语法问题
import re

file_path = r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture\tests\test_auth.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 找出问题区域
lines = content.split('\n')
for i, line in enumerate(lines[60:85], start=61):
    print(f"{i}: {repr(line)}")
