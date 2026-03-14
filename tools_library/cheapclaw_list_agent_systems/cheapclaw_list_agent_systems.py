#!/usr/bin/env python3
from pathlib import Path
from infiagent import infiagent
from tool_server_lite.tools.file_tools import BaseTool
import sys

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from tool_runtime_helpers import get_user_data_root

class CheapClawListAgentSystemsTool(BaseTool):
    name = "cheapclaw_list_agent_systems"
    def execute(self, task_id, parameters):
        user_root = get_user_data_root()
        agent_root = user_root / "agent_library"
        visible_names = set()
        if agent_root.exists():
            visible_names = {item.name for item in agent_root.iterdir() if item.is_dir()}
        agent = infiagent(user_data_root=str(user_root), seed_builtin_resources=False)
        payload = agent.list_agent_systems()
        items = payload.get("agent_systems")
        if not isinstance(items, list):
            items = []
        payload["agent_systems"] = [
            item
            for item in items
            if isinstance(item, dict) and str(item.get("name") or "").strip() in visible_names
        ]
        payload["runtime_agent_library_dir"] = str(agent_root)
        payload["visible_agent_system_names"] = [
            str(item.get("name") or "") for item in payload["agent_systems"] if str(item.get("name") or "")
        ]
        payload["recommended_defaults"] = {
            "CheapClawSupervisor": "supervisor_agent",
            "CheapClawWorkerGeneral": "worker_agent",
        }
        return payload
