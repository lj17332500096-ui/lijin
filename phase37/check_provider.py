"""Check model provider status."""
import sys, os, subprocess
sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8')

from agent import local_model_configured, local_model_name
print("Local model configured:", local_model_configured())
print("Local model:", local_model_name())

# Check env vars
for k in ['OPENAI_BASE_URL', 'OPENAI_API_KEY', 'FORGE_LOCAL_BASE_URL', 'FORGE_MODEL_PREF', 'LOCAL_MODEL']:
    v = os.environ.get(k, 'NOT SET')
    print(f"  {k}: {v}")

# Check for llama-server or ollama processes
print("\nChecking for model servers...")
for proc in ['llama-server', 'ollama', 'llama.cpp']:
    result = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {proc}.exe', '/FO', 'CSV'],
                          capture_output=True, text=True)
    if proc in result.stdout.lower():
        print(f"  {proc}: FOUND")
    else:
        print(f"  {proc}: not found")

# Check port 8080
result = subprocess.run(['netstat', '-an'], capture_output=True, text=True)
lines = [l for l in result.stdout.split('\n') if ':8080' in l and 'LISTENING' in l]
print(f"\nPort 8080 listeners: {len(lines)}")
for l in lines[:3]:
    print(f"  {l.strip()}")
