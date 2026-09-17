import os
import subprocess
import sys
from datetime import datetime

# 检查工作区 git 日志（如有）
os.chdir(r'F:\Byong-hermes\Byong-hermes\my_creative_agent')
result = subprocess.run(['git', 'log', '-1', '--format=%cd', '--date=short'], capture_output=True, text=True)
print('Git last commit date:', result.stdout.strip() if result.returncode == 0 else 'Not a git repo')

# 检查 benchmark_fixture 目录的最新文件修改时间
fixture_dir = r'F:\Byong-hermes\Byong-hermes\my_creative_agent\benchmark_fixture'
files = []
for root, dirs, filenames in os.walk(fixture_dir):
    for f in filenames:
        if f.endswith(('.py', '.md', '.txt')) and not f.startswith('_'):
            filepath = os.path.join(root, f)
            mtime = os.path.getmtime(filepath)
            files.append((filepath, mtime))

# 按修改时间排序
files.sort(key=lambda x: x[1], reverse=True)
print('\n最近修改的文件:')
for fp, mt in files[:5]:
    dt = datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M:%S')
    print(f'  {dt} - {os.path.relpath(fp, fixture_dir)}')