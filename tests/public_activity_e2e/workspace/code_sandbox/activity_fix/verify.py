from app import add

result = add(2, 3)
assert result == 5, f"Expected 5, got {result}"
print(f"Test passed: add(2, 3) = {result}")
