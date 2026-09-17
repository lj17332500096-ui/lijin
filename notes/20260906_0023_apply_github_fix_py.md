#!/usr/bin/env python3
"""
GitHub Token Refresh Fix - Автоматическое применение исправлений
Запуск: python apply_fix.py
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
GITHUB_FETCH = BASE / "github_fetch.py"
TEST_FILE = BASE / "tests" / "test_github_fetch.py"


def apply_fix():
    """Применяет исправление в github_fetch.py"""
    print("=" * 60)
    print("GitHub Token Refresh Fix")
    print("=" * 60)
    
    # Читаем файл
    if not GITHUB_FETCH.exists():
        print(f"❌ Файл не найден: {GITHUB_FETCH}")
        return False
    
    content = GITHUB_FETCH.read_text(encoding="utf-8")
    
    # Проверяем есть ли проблема
    if "except HTTPError:\n        pass" not in content:
        print("✓ Исправление уже применено")
        return True
    
    # Создаем备份
    backup = GITHUB_FETCH.with_suffix(".py.bak")
    backup.write_text(content, encoding="utf-8")
    print(f"✓ Backup создан: {backup}")
    
    # Применяем исправление
    old_code = '''    except HTTPError:
        pass'''
    
    new_code = '''    except HTTPError as exc:
        # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
        if exc.code not in (401, 403):
            raise'''
    
    new_content = content.replace(old_code, new_code)
    GITHUB_FETCH.write_text(new_content, encoding="utf-8")
    print(f"✓ Исправление применено: {GITHUB_FETCH}")
    
    return True


def add_tests():
    """Добавляет тесты в test_github_fetch.py"""
    print("\nДобавление тестов...")
    
    if not TEST_FILE.exists():
        print(f"❌ Тестовый файл не найден: {TEST_FILE}")
        return False
    
    content = TEST_FILE.read_text(encoding="utf-8")
    
    # Проверяем есть ли уже тесты
    if "class TokenRefreshTests" in content:
        print("✓ Тесты уже существуют")
        return True
    
    # Новый код тестов
    new_tests = '''

class TokenRefreshTests(unittest.TestCase):
    """Тесты для проверки логики обновления токена GitHub."""
    
    def test_token_401_falls_back(self):
        """Токен 401 (expired) -> fallback на публичный API"""
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
    
    def test_token_403_falls_back(self):
        """Токен 403 (invalid) -> fallback на публичный API"""
        from urllib.error import HTTPError
        from unittest.mock import patch
        
        def mock_http_get(url, token="", timeout=30):
            if "api.github.com" in url and token:
                raise HTTPError(url, 403, "Forbidden", {}, None)
            return b"fake data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            result = gf._download_archive("test-owner", "test-repo", "main", "invalid-token")
            self.assertIsNotNone(result)
    
    def test_server_error_raises(self):
        """Ошибка сервера 502 -> выбросить исключение, не фоллбэксить"""
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
        """Валидный токен -> использовать API, не фоллбэксить"""
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
    
    # Добавляем тесты перед if __name__
    if 'if __name__ == "__main__":' in content:
        lines = content.split('\n')
        # Находим строку с if __name__
        for i, line in enumerate(lines):
            if 'if __name__' in line:
                # Вставляем тесты перед этой строкой
                lines = lines[:i] + new_tests.split('\n') + lines[i:]
                break
        new_content = '\n'.join(lines)
    else:
        new_content = content + new_tests + '\n\n\nif __name__ == "__main__":\n    unittest.main()\n'
    
    # Backup
    backup = TEST_FILE.with_suffix(".py.bak")
    backup.write_text(content, encoding="utf-8")
    print(f"✓ Backup создан: {backup}")
    
    # Write
    TEST_FILE.write_text(new_content, encoding="utf-8")
    print(f"✓ Тесты добавлены: {TEST_FILE}")
    
    return True


def main():
    success = True
    success = apply_fix() and success
    success = add_tests() and success
    
    print("\n" + "=" * 60)
    if success:
        print("✅ Все исправления применены!")
        print("\nЗапустите тесты:")
        print("  cd my_creative_agent")
        print("  python -m pytest tests/test_github_fetch.py::TokenRefreshTests -v")
    else:
        print("❌ Ошибка при применении исправлений")
    print("=" * 60)
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
