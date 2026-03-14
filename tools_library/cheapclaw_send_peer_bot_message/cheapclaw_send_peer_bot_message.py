#!/usr/bin/env python3
import os
import signal
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from tool_runtime_helpers import create_monitor_instruction_for_root, get_peer_bot
from tool_server_lite.tools.file_tools import BaseTool


class CheapClawSendPeerBotMessageTool(BaseTool):
    name = "cheapclaw_send_peer_bot_message"

    def execute(self, task_id, parameters):
        target_bot_id = str(parameters.get("target_bot_id") or "").strip()
        message = str(parameters.get("message") or "").strip()
        if not target_bot_id or not message:
            return {"status": "error", "output": "", "error": "target_bot_id and message are required"}

        target = get_peer_bot(target_bot_id)
        if target is None:
            return {"status": "error", "output": "", "error": f"target bot not found: {target_bot_id}"}

        sender_name = str(parameters.get("sender_name") or os.environ.get("CHEAPCLAW_BOT_ID", "peer-bot")).strip() or "peer-bot"
        entry = create_monitor_instruction_for_root(
            target["user_data_root"],
            instruction_type="PeerBotMessage",
            summary=message,
            sender_name=sender_name,
            payload={
                "from_bot_id": os.environ.get("CHEAPCLAW_BOT_ID", ""),
                "from_task_id": str(task_id or ""),
            },
        )
        wake_sent = False
        try:
            runtime_state_path = Path(target["user_data_root"]).expanduser().resolve() / "cheapclaw" / "runtime" / "state.json"
            if runtime_state_path.exists():
                payload = __import__("json").loads(runtime_state_path.read_text(encoding="utf-8"))
                pid = int(((payload.get("bot") or {}).get("pid")) or 0)
                if pid > 0:
                    os.kill(pid, signal.SIGUSR1)
                    wake_sent = True
        except Exception:
            wake_sent = False
        return {
            "status": "success",
            "output": f"queued peer bot message to {target_bot_id}",
            "target_bot": target,
            "instruction": entry,
            "wake_signal_sent": wake_sent,
        }
