import sys, os
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE / '.env')
OUT = BASE / 'tests/public_activity_e2e'
os.environ['ALLOW_CODE_EXEC'] = 'true'
os.environ['ALLOW_PROJECT_EDIT'] = 'true'
os.environ['WORKSPACE_ROOT'] = str(OUT / 'workspace')
# Retain approval for destructive operations, run_python is confined to this test sandbox.
os.environ['APPROVAL_GATED_TOOLS'] = 'code_loop,forget_memory,sandbox_rollback,schedule_remove'
os.environ['OPENAI_AGENTS_DISABLE_TRACING'] = '1'
os.environ['MCP_SERVERS'] = '{}' # E2E cases exercise native tools only.
(OUT / 'workspace').mkdir(exist_ok=True)
from runtime.runner import AgentRuntime
AgentRuntime._default = AgentRuntime(db_path=OUT / 'activity.db')
import main
main.SESSIONS_DB = OUT / 'sessions.sqlite'
import webapp
import uvicorn
if __name__ == '__main__':
    uvicorn.run(webapp.app, host='127.0.0.1', port=8776, log_level='warning')

