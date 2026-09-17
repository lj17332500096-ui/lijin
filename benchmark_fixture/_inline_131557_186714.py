import sys
sys.path.insert(0, "F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture")
try:
    from app import auth
    print("import OK")
    print(dir(auth))
except Exception as e:
    print(f"IMPORT ERROR: {type(e).__name__}: {e}")