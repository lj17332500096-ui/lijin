#!/usr/bin/env python3
"""
GitHub Token 刷新逻辑修复脚本
运行方式: python fix_token_refresh.py
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve()
GITHUB_FETCH = BASE_DIR / "github_fetch.py"
TEST_FILE = BASE_DIR / "tests" / "test_github_fetch.py"


def fix_github_fetch():
    """修复 github_fetch.py 中的 token 降级逻辑"""
    print(f"读取: {GITHUB_FETCH}")
    content = GITHUB_FETCH.read_text(encoding="utf-8")
    
    # 原始代码
    old_code = '''def _download_archive(owner: str, repo: str, branch: str, token: str = "") -> tuple[bytes, str]:
    """下载仓库归档；返回 (原始字节, 格式: zip|tgz)。公开走 codeload，带 token 走 API tarball。"""
    try:
        if token:
            data = _http_get(
                f"https://api.github.com/repos/{owner}/{repo}/tarball/{quote(branch)}",
                token,
                timeout=120,
            )
            return data, "tgz"
    except HTTPError:
        pass
    data = _http_get(
        f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{quote(branch)}",
        timeout=180,
    )
    return data, "zip"'''
    
    # 新代码
    new_code = '''def _download_archive(owner: str, repo: str, branch: str, token: str = "") -> tuple[bytes, str]:
    """下载仓库归档；返回 (原始字节, 格式: zip|tgz)。公开走 codeload，带 token 走 API tarball。
    
    Token 失效（401/403）时降级到公开 API；其他错误（500/502/503 等）直接抛出。
    """
    try:
        if token:
            data = _http_get(
                f"https://api.github.com/repos/{owner}/{repo}/tarball/{quote(branch)}",
                token,
                timeout=120,
            )
            return data, "tgz"
    except HTTPError as exc:
        # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
        if exc.code not in (401, 403):
            raise
    data = _http_get(
        f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{quote(branch)}",
        timeout=180,
    )
    return data, "zip"'''
    
    if old_code not in content:
        print("✗ 未找到目标代码块")
        return False
    
    print("✓ 找到目标代码块")
    
    # 备份
    backup = GITHUB_FETCH.with_suffix(".py.bak")
    backup.write_text(content, encoding="utf-8")
    print(f"✓ 已备份: {backup}")
    
    # 替换
    new_content = content.replace(old_code, new_code)
    GITHUB_FETCH.write_text(new_content, encoding="utf-8")
    print(f"✓ 已更新: {GITHUB_FETCH}")
    
    return True


def add_tests():
    """添加 token 刷新测试用例"""
    print(f"\n读取: {TEST_FILE}")
    content = TEST_FILE.read_text(encoding="utf-8")
    
    # 检查是否已存在
    if "class TokenRefreshTests" in content:
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
    
    # 在文件末尾添加（在 if __name__ 之前）
    lines = content.rstrip().split('\n')
    
    # 移除最后的 if __name__ 块
    while lines and lines[-1].strip():
        lines.pop()
    
    new_content = '\n'.join(lines) + new_tests + '\n\n\nif __name__ == "__main__":\n    unittest.main()\n'
    
    # 备份
    backup = TEST_FILE.with_suffix(".py.bak")
    backup.write_text(content, encoding="utf-8")
    print(f"✓ 已备份: {backup}")
    
    # 写入
    TEST_FILE.write_text(new_content, encoding="utf-8")
    print(f"✓ 已更新: {TEST_FILE}")
    
    return True


def main():
    print("=" * 60)
    print("GitHub Token 刷新逻辑修复")
    print("=" * 60)
    
    success = True
    success = fix_github_fetch() and success
    success = add_tests() and success
    
    print("\n" + "=" * 60)
    if success:
        print("✓ 修复完成！")
        print("\n运行测试验证:")
        print("  cd my_creative_agent")
        print("  python -m pytest tests/test_github_fetch.py -v")
    else:
        print("✗ 修复失败，请检查上方错误信息")
    print("=" * 60)
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
