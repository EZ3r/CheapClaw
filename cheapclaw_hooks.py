#!/usr/bin/env python3
"""CheapClaw runtime hooks for background worker tool events."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

try:
    from .tool_runtime_helpers import emit_task_event, now_iso
except ImportError:
    from tool_runtime_helpers import emit_task_event, now_iso


def _iso_ts(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _latest_root_agent_id(agents_status: Any) -> str:
    if not isinstance(agents_status, dict):
        return ""
    roots = []
    for agent_id, agent in agents_status.items():
        if not isinstance(agent, dict):
            continue
        if str(agent.get("parent_id") or "").strip():
            continue
        roots.append((str(agent_id), agent))
    if not roots:
        return ""
    roots.sort(
        key=lambda item: (
            _iso_ts(str(item[1].get("start_time") or "")),
            _iso_ts(str(item[1].get("end_time") or "")),
        )
    )
    return roots[-1][0]


def _resolve_latest_root_agent_id(task_id: str) -> str:
    try:
        from infiagent.sdk import get_task_share_paths
    except Exception:
        return ""
    try:
        paths = get_task_share_paths(task_id)
        share_context_path = Path(str(paths.get("share_context_path") or "")).expanduser().resolve()
    except Exception:
        return ""
    if not share_context_path.exists():
        return ""
    try:
        payload = json.loads(share_context_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""

    current = payload.get("current", {})
    current_root = _latest_root_agent_id(current.get("agents_status", {}) if isinstance(current, dict) else {})
    if current_root:
        return current_root

    history = payload.get("history", [])
    if isinstance(history, list):
        for entry in reversed(history):
            if not isinstance(entry, dict):
                continue
            root_id = _latest_root_agent_id(entry.get("agents_status", {}))
            if root_id:
                return root_id
    return ""


def on_tool_event(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("when") != "after":
        return
    if payload.get("tool_name") != "final_output":
        return

    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        return
    if Path(task_id).name == "supervisor_task":
        return
    agent_id = str(payload.get("agent_id") or "").strip()
    root_agent_id = _resolve_latest_root_agent_id(task_id)
    # Only emit completion events for the latest root agent (parent_id == null).
    # If root resolution fails, skip and let reconcile fallback detect completion from share_context.
    if not root_agent_id or not agent_id or agent_id != root_agent_id:
        return

    result = payload.get("result") or {}
    if not isinstance(result, dict):
        result = {}

    emit_task_event({
        "event_type": "task_final_output",
        "task_id": task_id,
        "agent_id": agent_id,
        "agent_name": str(payload.get("agent_name") or ""),
        "root_agent_id": root_agent_id,
        "status": str(result.get("status") or ""),
        "output": str(result.get("output") or ""),
        "error_information": str(result.get("error_information") or ""),
        "observed_at": now_iso(),
        "pid": os.getpid(),
    })
