import sys; sys.path.insert(0, 'F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture')
import importlib
mods = ['auth', 'test_auth', 'test_auth_fixed', 'test_auth_runner']
for m in mods:
    try:
        mod = importlib.import_module(m)
        print(f'OK: {m}')
    except Exception as e:
        print(f'FAIL: {m} -> {type(e).__name__}: {e}')