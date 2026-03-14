#!/usr/bin/env python3
import os
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from tool_runtime_helpers import list_peer_bots
from tool_server_lite.tools.file_tools import BaseTool


class CheapClawListPeerBotsTool(BaseTool):
    name = "cheapclaw_list_peer_bots"

    def execute(self, task_id, parameters):
        current_bot_id = str(parameters.get("current_bot_id") or os.environ.get("CHEAPCLAW_BOT_ID", "")).strip()
        include_self = bool(parameters.get("include_self", False))
        items = list_peer_bots(current_bot_id=current_bot_id, include_self=include_self)
        return {
            "status": "success",
            "output": f"peer_bots={len(items)}",
            "bots": items,
        }
