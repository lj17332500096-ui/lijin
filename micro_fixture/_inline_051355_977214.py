
# 读取当前文件内容
with open('calc.py', 'r', encoding='utf-8') as f:
    content = f.read()

print("原始内容repr:")
print(repr(content))
print("---")

# 修复：将 a - b 改为 a + b
new_content = content.replace('return a - b', 'return a + b')

print("修复后内容repr:")
print(repr(new_content))
print("---")

# 写入文件
with open('calc.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("已写入修复后文件")
