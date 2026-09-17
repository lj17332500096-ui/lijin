#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 github_fetch.py 的 Token 刷新逻辑，并添加测试用例。"""

import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / 'my_creative_agent'
GITHUB_FETCH = BASE / 'github_fetch.py'
TEST_FILE = BASE / 'tests' / 'test_github_fetch.py'


def fix_token_refresh():
    """修复 github_fetch.py 的 HTTPError 降级逻辑。"""
    content = GITHUB_FETCH.read_text(encoding='utf-8')
    
    # 目标代码（有问题）
    old_pattern = r'    except HTTPError:\n        pass'
    new_code = '''    except HTTPError as exc:
        # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
        if exc.code not in (401, 403):
            raise'''
    
    if re.search(old_pattern, content):
        new_content = re.sub(old_pattern, new_code, content)
        GITHUB_FETCH.write_text(new_content, encoding='utf-8')
        print(f"✓ 已修复: {GITHUB_FETCH}")
        print("  修改: 第 111-112 行 - HTTPError 降级逻辑")
        return True
    else:
        print("✗ 未找到目标代码，可能已修复或格式不同")
        # 显示当前代码以便调试
        match = re.search(r'except HTTPError[^:]*:(?:\n        [^\n]+){1,2}', content)
        if match:
            print(f"\n当前代码:\n{match.group()}")
        return False


def add_token_refresh_tests():
    """添加 Token 刷新测试用例到 test_github_fetch.py。"""
    content = TEST_FILE.read_text(encoding='utf-8')
    
    # 检查是否已存在测试类
    if 'class TokenRefreshTests' in content:
        print("⚠ 测试类已存在，跳过")
        return True
    
    # 新的测试代码
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
    
    def test_token_forbidden_falls_back_to_public(self):
        """Token 无效（403）时应降级。"""
        from urllib.error import HTTPError
        from unittest.mock import patch
        
        def mock_http_get(url, token="", timeout=30):
            if "api.github.com" in url and token:
                raise HTTPError(url, 403, "Forbidden", {}, None)
            return b"fake data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            result = gf._download_archive("test-owner", "test-repo", "main", "invalid-token")
            self.assertIsNotNone(result)
    
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
    
    # 在 if __name__ == "__main__": 之前插入测试类
    if 'if __name__ == "__main__":' in content:
        new_content = content.replace(
            'if __name__ == "__main__":',
            new_tests + '\n\nif __name__ == "__main__":'
        )
        TEST_FILE.write_text(new_content, encoding='utf-8')
        print(f"✓ 已添加测试用例: {TEST_FILE}")
        return True
    else:
        print("⚠ 未找到插入位置，请手动添加测试用例")
        return False


if __name__ == '__main__':
    print("GitHub Token 刷新修复脚本")
    print("=" * 40)
    
    fix_ok = fix_token_refresh()
    test_ok = add_token_refresh_tests()
    
    print()
    print("完成!")
    print("运行测试验证:")
    print(f"  cd {BASE}")
    print(f"  python -m pytest tests/test_github_fetch.py -v")