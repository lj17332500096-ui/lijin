#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用 GitHub Token 刷新修复并添加测试"""
import re
from pathlib import Path

BASE = Path('my_creative_agent')
GITHUB_FETCH = BASE / 'github_fetch.py'
TEST_FILE = BASE / 'tests' / 'test_github_fetch.py'

print("=" * 60)
print("GitHub Token 刷新修复")
print("=" * 60)

# 1. 修复 github_fetch.py
print(f"\n📄 修改: {GITHUB_FETCH}")
content = GITHUB_FETCH.read_text(encoding='utf-8')

if 'except HTTPError as exc:' not in content:
    old_code = '    except HTTPError:\n        pass'
    new_code = '''    except HTTPError as exc:
        # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
        if exc.code not in (401, 403):
            raise'''
    
    if old_code in content:
        new_content = content.replace(old_code, new_code)
        GITHUB_FETCH.write_text(new_content, encoding='utf-8')
        print("✓ 已修复 HTTPError 降级逻辑")
    else:
        print("⚠ 未找到目标代码")
else:
    print("✓ 修复已存在")

# 2. 添加测试用例
print(f"\n📝 添加测试: {TEST_FILE}")
test_content = TEST_FILE.read_text(encoding='utf-8')

if 'class TokenRefreshTests' not in test_content:
    new_tests = '''

class TokenRefreshTests(unittest.TestCase):
    """测试 GitHub Token 失效时的降级逻辑。"""
    
    def test_token_expired_falls_back_to_public(self):
        """Token 过期（401）时应降级到公开仓库访问。"""
        from urllib.error import HTTPError
        from unittest.mock import patch
        
        call_count = {"api": 0, "public": 0}
        
        def mock_http_get(url, token="", timeout=30):
            if "api.github.com" in url:
                call_count["api"] += 1
                if token:
                    raise HTTPError(url, 401, "Unauthorized", {}, None)
            elif "codeload.github.com" in url:
                call_count["public"] += 1
            return b"fake zip data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            result = gf._download_archive("test-owner", "test-repo", "main", "expired-token")
            self.assertIsNotNone(result)
            self.assertEqual(len(result), 2)
            self.assertGreater(call_count["api"], 0)
            self.assertGreater(call_count["public"], 0)
    
    def test_server_error_raises_not_fallback(self):
        """GitHub API 500/502 错误时不应降级，应直接抛出。"""
        from urllib.error import HTTPError
        from unittest.mock import patch
        
        def mock_http_get(url, token="", timeout=30):
            if "api.github.com" in url:
                raise HTTPError(url, 502, "Bad Gateway", {}, None)
            return b"fake data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            with self.assertRaises(HTTPError) as context:
                gf._download_archive("test-owner", "test-repo", "main", "some-token")
            self.assertEqual(context.exception.code, 502)
    
    def test_valid_token_no_fallback(self):
        """有效 token 时不应降级。"""
        from unittest.mock import patch
        
        call_count = {"api": 0, "public": 0}
        
        def mock_http_get(url, token="", timeout=30):
            if "api.github.com" in url:
                call_count["api"] += 1
                return b"fake tgz data"
            elif "codeload.github.com" in url:
                call_count["public"] += 1
            return b"fake data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            result = gf._download_archive("test-owner", "test-repo", "main", "valid-token")
            self.assertIsNotNone(result)
            self.assertEqual(result[1], "tgz")
            self.assertGreater(call_count["api"], 0)
            self.assertEqual(call_count["public"], 0)
'''
    
    if 'if __name__ == "__main__":' in test_content:
        new_test_content = test_content.replace(
            'if __name__ == "__main__":',
            new_tests + '\n\nif __name__ == "__main__":'
        )
        TEST_FILE.write_text(new_test_content, encoding='utf-8')
        print("✓ 已添加 Token 刷新测试用例")
    else:
        print("⚠ 未找到插入位置")
else:
    print("⚠ 测试类已存在")

print("\n" + "=" * 60)
print("✅ 修复完成！")
print("=" * 60)