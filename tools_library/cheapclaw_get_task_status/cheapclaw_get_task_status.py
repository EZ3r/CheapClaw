#!/usr/bin/env python3
from pathlib import Path
from infiagent import infiagent
import sys
from datetime import datetime

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from tool_runtime_helpers import assert_managed_task_id, load_panel
from tool_server_lite.tools.file_tools import BaseTool


def _iso_ts(value):
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _latest_root_final_output(share_context_path):
    path = Path(str(share_context_path or "")).expanduser()
    if not path.exists():
        return {"resolved": False}
    try:
        payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"resolved": False}
    if not isinstance(payload, dict):
        return {"resolved": False}

    records = []

    def _collect(entry, source, sequence):
        agents_status = entry.get("agents_status", {})
        if not isinstance(agents_status, dict):
            return
        roots = []
        for agent_id, agent in agents_status.items():
            if not isinstance(agent, dict):
                continue
            if str(agent.get("parent_id") or "").strip():
                continue
            roots.append((str(agent_id), agent))
        if not roots:
            return
        roots.sort(key=lambda item: (_iso_ts(item[1].get("start_time")), _iso_ts(item[1].get("end_time"))))
        root_agent_id, root_agent = roots[-1]
        run_start_at = str(entry.get("start_time") or root_agent.get("start_time") or "")
        run_completed_at = str(entry.get("completion_time") or root_agent.get("end_time") or "")
        final_output = str(root_agent.get("final_output") or "")
        final_output_at = str(root_agent.get("end_time") or run_completed_at or "")
        records.append(
            {
                "source": source,
                "sequence": sequence,
                "run_start_ts": _iso_ts(run_start_at),
                "final_output_ts": _iso_ts(final_output_at),
                "final_output": final_output,
                "final_output_at": final_output_at,
                "root_agent_id": root_agent_id,
                "root_agent_name": str(root_agent.get("agent_name") or ""),
            }
        )

    history = payload.get("history", [])
    if isinstance(history, list):
        for idx, entry in enumerate(history):
            if isinstance(entry, dict):
                _collect(entry, "history", idx)
    current = payload.get("current", {})
    if isinstance(current, dict):
        _collect(current, "current", len(records))

    if not records:
        return {"resolved": True, "final_output": "", "final_output_at": "", "root_agent_id": "", "root_agent_name": "", "source": ""}
    records_with_output = [item for item in records if str(item.get("final_output") or "").strip()]
    if not records_with_output:
        return {"resolved": True, "final_output": "", "final_output_at": "", "root_agent_id": "", "root_agent_name": "", "source": ""}
    latest = max(
        records_with_output,
        key=lambda item: (float(item.get("run_start_ts") or 0.0), float(item.get("final_output_ts") or 0.0), int(item.get("sequence") or 0)),
    )
    return {
        "resolved": True,
        "final_output": str(latest.get("final_output") or ""),
        "final_output_at": str(latest.get("final_output_at") or ""),
        "root_agent_id": str(latest.get("root_agent_id") or ""),
        "root_agent_name": str(latest.get("root_agent_name") or ""),
        "source": str(latest.get("source") or ""),
    }


class CheapClawGetTaskStatusTool(BaseTool):
    name = "cheapclaw_get_task_status"
    def execute(self, task_id, parameters):
        try:
            target_task_id = assert_managed_task_id(str(Path(parameters.get("task_id") or "").expanduser().resolve()), allow_monitor_task=True)
        except ValueError as exc:
            return {"status": "error", "output": "", "error": str(exc)}
        if not target_task_id:
            return {"status": "error", "output": "", "error": "task_id is required"}
        agent = infiagent(user_data_root=str(Path(__import__('os').environ.get('MLA_USER_DATA_ROOT','~/mla_v3')).expanduser().resolve()))
        snapshot = agent.task_snapshot(task_id=target_task_id)
        root_final = _latest_root_final_output(snapshot.get("share_context_path", ""))
        if root_final.get("resolved"):
            snapshot["last_final_output"] = str(root_final.get("final_output") or "")
            snapshot["last_final_output_at"] = str(root_final.get("final_output_at") or "")
            snapshot["last_final_output_root_agent_id"] = str(root_final.get("root_agent_id") or "")
            snapshot["last_final_output_root_agent_name"] = str(root_final.get("root_agent_name") or "")
            snapshot["last_final_output_source"] = str(root_final.get("source") or "")
        log_path = ""
        panel = load_panel()
        for channel_payload in panel.get("channels", {}).values():
            for conv in channel_payload.get("conversations", {}).values():
                for item in conv.get("linked_tasks", []):
                    if item.get("task_id") == target_task_id:
                        log_path = item.get("log_path", "")
                        snapshot["conversation"] = {"channel": conv.get("channel", ""), "conversation_id": conv.get("conversation_id", ""), "display_name": conv.get("display_name", "")}
                        break
        snapshot["log_path"] = log_path
        return snapshot
