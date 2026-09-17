
with open('calc.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 清理已失效的 BUG 注释
new_content = content.replace('    # BUG\n', '')

print("清理后内容repr:")
print(repr(new_content))

with open('calc.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("已清理 BUG 注释并写入文件")
