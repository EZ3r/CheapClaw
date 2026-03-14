#!/usr/bin/env python3
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from tool_runtime_helpers import list_monitor_instructions
from tool_server_lite.tools.file_tools import BaseTool


class CheapClawListMonitorInstructionsTool(BaseTool):
    name = "cheapclaw_list_monitor_instructions"

    def execute(self, task_id, parameters):
        status = str(parameters.get("status") or "pending").strip()
        limit = int(parameters.get("limit") or 100)
        items = list_monitor_instructions(status=status, limit=limit)
        return {
            "status": "success",
            "output": f"instructions={len(items)}",
            "instructions": items,
        }
