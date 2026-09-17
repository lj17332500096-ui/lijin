# Micro Fixture Bug Fix

## Issue
Fix the `add` function in `micro_fixture/calc.py` to return `a+b` instead of `a-b`.

## Findings
The `add` function was **already correct** — it returns `a + b`.

### Code (calc.py)
```python
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
```

## Test Results
- Project: `micro_fixture`
- Target: `test_calc.py`
- Result: **2 passed** in 0.01s
- Exit code: 0

### Tests
- `test_add`: verifies `add(1, 2) == 3`, `add(0, 0) == 0`, `add(-1, 1) == 0`
- `test_multiply`: verifies `multiply(2, 3) == 6`, `multiply(0, 5) == 0`, `multiply(-2, 3) == -6`

## Conclusion
No fix was required. The implementation already matches the expected behavior.