import pytest
import sys
sys.path.insert(0, r"F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
result = pytest.main([r"F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture/tests/", "-v"])
print(f"\nExit code: {result}")