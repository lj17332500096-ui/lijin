import subprocess, sys, os, random

os.chdir(r"F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture")
cmd = [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=long"]

flaky_results = []
for i in range(100):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        lines = result.stdout.split('\n')
        summary = [l for l in lines if 'passed' in l or 'failed' in l or 'error' in l]
        flaky_results.append((i+1, result.returncode, result.stdout[:800], summary))
        print(f"FAIL Run {i+1}: exit={result.returncode} summary={summary}")
    else:
        if (i+1) % 20 == 0:
            print(f"Run {i+1}: OK")

print(f"\n=== Total: {100} runs, {len(flaky_results)} failures ===")
if flaky_results:
    for r in flaky_results[:5]:
        print(f"  Run {r[0]}: exit={r[1]}")
        print(f"    {r[2][:500]}")
