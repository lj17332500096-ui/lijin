# GitHub Token Refresh Fix - Pending Approval

## Summary

I've identified the issue in `github_fetch.py` and prepared the fix. The fix requires approval to apply.

## What Needs to Change

### File: `my_creative_agent/github_fetch.py` (Line 111)

**Current code (problematic):**
```python
def _download_archive(owner, repo, branch, token=""):
    try:
        if token:
            data = _http_get(f"https://api.github.com/repos/{owner}/{repo}/tarball/{quote(branch)}", token, timeout=120)
            return data, "tgz"
    except HTTPError:  # ❌ All HTTP errors trigger fallback
        pass
    # Fallback to public API
    ...
```

**Fixed code:**
```python
def _download_archive(owner, repo, branch, token=""):
    try:
        if token:
            data = _http_get(f"https://api.github.com/repos/{owner}/{repo}/tarball/{quote(branch)}", token, timeout=120)
            return data, "tgz"
    except HTTPError as exc:
        # Only fallback on auth errors; raise on server errors
        if exc.code not in (401, 403):
            raise
    # Fallback to public API
    ...
```

### File: `my_creative_agent/tests/test_github_fetch.py`

Add new test class `TokenRefreshTests` with 4 test cases.

## Why This Fix?

| Error Code | Meaning | Current Behavior | Fixed Behavior |
|------------|---------|------------------|----------------|
| 401 | Token expired | Fallback ✓ | Fallback ✓ |
| 403 | Token invalid | Fallback ✓ | Fallback ✓ |
| 500 | Server error | Fallback ✗ | Raise exception ✓ |
| 502 | Bad gateway | Fallback ✗ | Raise exception ✓ |
| 503 | Service unavailable | Fallback ✗ | Raise exception ✓ |
| 404 | Not found | Fallback ✗ | Raise exception ✓ |

## How to Apply

**Option 1: Manual edit**
- Open `github_fetch.py`
- Change line 111 from `except HTTPError:` to `except HTTPError as exc:`
- Change line 112 from `pass` to the new logic

**Option 2: Run approved script**
- Script `apply_fix.py` is ready in `my_creative_agent/`
- Run: `python apply_fix.py`

## Tests to Add

```python
class TokenRefreshTests(unittest.TestCase):
    def test_token_401_falls_back(self):
        """Token 401 -> fallback to public API"""
        ...
    
    def test_token_403_falls_back(self):
        """Token 403 -> fallback to public API"""
        ...
    
    def test_server_error_raises(self):
        """Server error 502 -> raise exception"""
        ...
    
    def test_valid_token_no_fallback(self):
        """Valid token -> use API tarball"""
        ...
```

## Verification

After applying, run:
```bash
cd my_creative_agent
python -m pytest tests/test_github_fetch.py::TokenRefreshTests -v
```