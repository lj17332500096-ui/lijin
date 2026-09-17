import sys
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
from app import auth

# Test calculate with various inputs
print("Testing calculate function:")
print(f"calculate(2.5) = {auth.calculate(2.5)}")
print(f"calculate('3.14') = {auth.calculate('3.14')}")
print(f"calculate(5) = {auth.calculate(5)}")

# Test what happens with edge cases
try:
    result = auth.calculate("")
    print(f"calculate('') = {result}")
except Exception as e:
    print(f"calculate('') error: {type(e).__name__}: {e}")

try:
    result = auth.calculate(None)
    print(f"calculate(None) = {result}")
except Exception as e:
    print(f"calculate(None) error: {type(e).__name__}: {e}")

try:
    result = auth.calculate("abc")
    print(f"calculate('abc') = {result}")
except Exception as e:
    print(f"calculate('abc') error: {type(e).__name__}: {e}")
