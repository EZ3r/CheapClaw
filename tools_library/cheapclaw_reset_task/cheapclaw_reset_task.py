#!/usr/bin/env python3
from pathlib import Path
from infiagent import infiagent
import sys

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from tool_runtime_helpers import assert_managed_task_id
from tool_server_lite.tools.file_tools import BaseTool

class CheapClawResetTaskTool(BaseTool):
    name = "cheapclaw_reset_task"
    def execute(self, task_id, parameters):
        try:
            target_task_id = assert_managed_task_id(str(Path(parameters.get("task_id") or "").expanduser().resolve()))
        except ValueError as exc:
            return {"status": "error", "output": "", "error": str(exc)}
        if not target_task_id:
            return {"status": "error", "output": "", "error": "task_id is required"}
        agent = infiagent(user_data_root=str(Path(__import__('os').environ.get('MLA_USER_DATA_ROOT','~/mla_v3')).expanduser().resolve()))
        return agent.reset_task(
            task_id=target_task_id,
            preserve_history=bool(parameters.get("preserve_history", True)),
            kill_background_processes=bool(parameters.get("kill_background_processes", True)),
            reason=str(parameters.get("reason") or "manual reset"),
        )
