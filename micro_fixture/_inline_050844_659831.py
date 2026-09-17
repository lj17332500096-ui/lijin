
# 读取 calc.py
with open('calc.py', 'r', encoding='utf-8') as f:
    content = f.read()

print("=== 原始内容 ===")
print(repr(content))

# 修复 add 函数：将 return a - b 改为 return a + b
content = content.replace('return a - b', 'return a + b')

# 确认 BUG 注释是否还需要保留。我们保留注释但修正逻辑更清晰
# 但根据要求只修复错误，把 return 改为 +
with open('calc.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("=== 修复后内容 ===")
print(content)
