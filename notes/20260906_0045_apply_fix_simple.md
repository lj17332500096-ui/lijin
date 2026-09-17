#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动修复 github_fetch.py 的 Token 刷新逻辑。"""

import re
from pathlib import Path

# 文件路径
GITHUB_FETCH = Path('my_creative_agent/github_fetch.py')

print(f"修复文件: {GITHUB_FETCH}")
print()

# 读取原文件
content = GITHUB_FETCH.read_text(encoding='utf-8')

# 检查是否已经修复
if 'except HTTPError as exc:' in content and 'if exc.code not in (401, 403):' in content:
    print("✓ 修复已存在，无需重复修改")
else:
    # 目标代码（有问题）
    old_pattern = r'    except HTTPError:\n        pass'
    new_code = '''    except HTTPError as exc:
        # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
        if exc.code not in (401, 403):
            raise'''
    
    if re.search(old_pattern, content):
        new_content = re.sub(old_pattern, new_code, content)
        GITHUB_FETCH.write_text(new_content, encoding='utf-8')
        print("✓ 已修复 HTTPError 降级逻辑")
        print("  - 401/403 (认证错误) → 降级到公开 API")
        print("  - 500/502/503 (服务器错误) → 直接抛出")
    else:
        print("✗ 未找到目标代码，可能已修复或格式不同")
        # 显示当前代码以便调试
        match = re.search(r'except HTTPError[^:]*:(?:\n        [^\n]+){1,2}', content)
        if match:
            print(f"\n当前代码:\n{match.group()}")

print()
print("验证修改:")
print("-" * 40)
# 显示修改后的代码
lines = content.split('\n')
for i, line in enumerate(lines[105:120], start=106):
    print(f"{i}: {line}")
