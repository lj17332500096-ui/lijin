"""自动修复 github_fetch.py 的 Token 刷新逻辑"""
from pathlib import Path

GITHUB_FETCH = Path('my_creative_agent/github_fetch.py')

# 读取原文件
content = GITHUB_FETCH.read_text(encoding='utf-8')

# 目标代码（有问题）
old_code = '''    except HTTPError:
        pass'''

# 修复后的代码
new_code = '''    except HTTPError as exc:
        # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
        if exc.code not in (401, 403):
            raise'''

if old_code in content:
    new_content = content.replace(old_code, new_code)
    GITHUB_FETCH.write_text(new_content, encoding='utf-8')
    print(f"✓ 已修复: {GITHUB_FETCH}")
    print("  修改: 第 111-112 行 - HTTPError 降级逻辑")
else:
    print("✗ 未找到目标代码，可能已修复或格式不同")
    print("\n请手动检查 github_fetch.py 第 111 行附近的内容")