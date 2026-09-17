# GitHub Token Refresh Fix - Applied Successfully

## Changes Made

### 1. github_fetch.py (Line 111)

**Before:**
```python
except HTTPError:
    pass
```

**After:**
```python
except HTTPError as exc:
    # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
    if exc.code not in (401, 403):
        raise
```

### 2. tests/test_github_fetch.py

Added `TokenRefreshTests` class with 4 test cases:
- `test_token_401_falls_back` - Token expired (401) → fallback to public API
- `test_token_403_falls_back` - Token invalid (403) → fallback to public API
- `test_server_error_raises` - Server error (502) → raise exception
- `test_valid_token_no_fallback` - Valid token → use API tarball

## Verification

Run tests:
```bash
cd my_creative_agent
python -m pytest tests/test_github_fetch.py::TokenRefreshTests -v
```

## Impact

| Scenario | Before | After |
|----------|--------|-------|
| Token 401 | Fallback | Fallback ✓ |
| Token 403 | Fallback | Fallback ✓ |
| GitHub 502 | Fallback (wrong) | Raise exception ✓ |
| Repo 404 | Fallback (useless) | Raise exception ✓ |
| Valid token | API tarball | API tarball ✓ |

## Files Modified

- `github_fetch.py` - Line 111-112
- `tests/test_github_fetch.py` - Added TokenRefreshTests class

## Backup Created

- `github_fetch.py.bak` - Original file backup