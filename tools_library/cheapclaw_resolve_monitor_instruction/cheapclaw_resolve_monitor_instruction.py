#!/usr/bin/env python3
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from tool_runtime_helpers import assert_managed_task_id, resolve_monitor_instruction
from tool_server_lite.tools.file_tools import BaseTool


class CheapClawResolveMonitorInstructionTool(BaseTool):
    name = "cheapclaw_resolve_monitor_instruction"

    def execute(self, task_id, parameters):
        instruction_id = str(parameters.get("instruction_id") or "").strip()
        if not instruction_id:
            return {"status": "error", "output": "", "error": "instruction_id is required"}

        resolved_by_task_id = str(parameters.get("resolved_by_task_id") or task_id or "").strip()
        if resolved_by_task_id:
            try:
                resolved_by_task_id = assert_managed_task_id(resolved_by_task_id, allow_monitor_task=True)
            except ValueError as exc:
                return {"status": "error", "output": "", "error": str(exc)}

        return resolve_monitor_instruction(
            instruction_id,
            resolution_note=str(parameters.get("resolution_note") or "").strip(),
            resolved_by_task_id=resolved_by_task_id,
        )
