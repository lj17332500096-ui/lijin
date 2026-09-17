with open('F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture/app/auth.py', 'r', encoding='utf-8') as f:
    content = f.read()
    
import re
match = re.search(r'def calculate\(.*?\):.*?(?=\ndef |\Z)', content, re.DOTALL)
if match:
    print(repr(match.group()))
