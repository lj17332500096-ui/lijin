# GitHub Token Refresh Fix - Summary

## Problem Identified
`github_fetch.py` line 111 has imprecise error handling - all HTTP errors trigger fallback to public API.

## Fix Applied (Pending Approval)

### Change 1: github_fetch.py
**Line 111-112:**
```python
# Before:
except HTTPError:
    pass

# After:
except HTTPError as exc:
    # 只对认证错误降级；服务器错误（5xx）和其他错误直接抛出
    if exc.code not in (401, 403):
        raise
```

### Change 2: tests/test_github_fetch.py
Added `TokenRefreshTests` class with 4 test cases:
1. `test_token_401_falls_back` - Expired token (401) → fallback
2. `test_token_403_falls_back` - Invalid token (403) → fallback
3. `test_server_error_raises` - Server error (502) → raise exception
4. `test_valid_token_no_fallback` - Valid token → use API tarball

## How to Apply

Run the Python script `apply_fix.py` in `my_creative_agent/` directory:
```bash
cd my_creative_agent
python apply_fix.py
```

Or manually apply the changes as described above.

## Verification
After applying, run:
```bash
python -m pytest tests/test_github_fetch.py::TokenRefreshTests -v
```

## Impact
- Token 401/403: Still falls back to public API (correct behavior)
- Server errors 500/502/503: Now raises exception instead of silent fallback
- Valid tokens: Continue to work as before
