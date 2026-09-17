# GitHub Token Refresh Fix - Ready to Apply

## Current State
- Problem identified in `github_fetch.py` line 111
- Fix ready in `notes/20260906_0022_GitHub_Token_刷新修复_-_执行摘要.md`
- Tests ready to add

## Apply Fix Now

### Option 1: Manual Edit
Open `my_creative_agent/github_fetch.py` and change line 111:

**From:**
```python
    except HTTPError:
        pass
```

**To:**
```python
    except HTTPError as exc:
        # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
        if exc.code not in (401, 403):
            raise
```

### Option 2: Run Python Script
Save this as `fix_token_refresh.py` and run:

```python
from pathlib import Path

path = Path('github_fetch.py')
content = path.read_text()

old = '    except HTTPError:\n        pass'
new = '''    except HTTPError as exc:
        # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
        if exc.code not in (401, 403):
            raise'''

if old in content:
    path.write_text(content.replace(old, new))
    print('Fixed!')
else:
    print('Pattern not found')
```

### Add Tests
Add to `tests/test_github_fetch.py` before `if __name__`:

```python
class TokenRefreshTests(unittest.TestCase):
    def test_token_expired_falls_back_to_public(self):
        from urllib.error import HTTPError
        from unittest.mock import patch
        
        def mock_http_get(url, token="", timeout=30):
            if "api.github.com" in url and token:
                raise HTTPError(url, 401, "Unauthorized", {}, None)
            return b"fake zip data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            result = gf._download_archive("owner", "repo", "main", "token")
            assert result is not None
            assert result[1] == "zip"  # Should fallback to zip
    
    def test_server_error_raises(self):
        from urllib.error import HTTPError
        from unittest.mock import patch
        
        def mock_http_get(url, token="", timeout=30):
            if "api.github.com" in url:
                raise HTTPError(url, 502, "Bad Gateway", {}, None)
            return b"fake"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            try:
                gf._download_archive("owner", "repo", "main", "token")
                assert False, "Should raise"
            except HTTPError as e:
                assert e.code == 502
```

## Run Tests
```bash
cd my_creative_agent
python -m pytest tests/test_github_fetch.py -v
```