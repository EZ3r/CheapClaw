#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

OUTBOX_DEFAULT_MAX_RETRIES = 8


def _compute_outbox_dedupe_hash(
    *,
    channel: str,
    conversation_id: str,
    message: str,
    attachments: List[Dict[str, Any]],
) -> str:
    dedupe_basis = {
        "channel": str(channel or ""),
        "conversation_id": str(conversation_id or ""),
        "message": str(message or ""),
        "attachments": attachments,
    }
    return hashlib.sha256(json.dumps(dedupe_basis, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value: str) -> Optional[datetime]:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed
    except Exception:
        return None


def get_user_data_root() -> Path:
    # Prefer explicit CheapClaw root hint, then MLA runtime override.
    for env_key in ("CHEAPCLAW_USER_DATA_ROOT", "MLA_USER_DATA_ROOT"):
        env_root = os.environ.get(env_key, "").strip()
        if env_root:
            return Path(env_root).expanduser().resolve()

    # Fallback: infer from copied helper location:
    # <user_data_root>/tool_runtime_helpers.py + <user_data_root>/cheapclaw/...
    helper_parent = Path(__file__).resolve().parent
    if (helper_parent / "cheapclaw").exists():
        return helper_parent

    return (Path.home() / "mla_v3").resolve()


def get_cheapclaw_root() -> Path:
    return get_user_data_root() / "cheapclaw"


def get_panel_path() -> Path:
    return get_cheapclaw_root() / "panel" / "panel.json"


def get_panel_backups_dir() -> Path:
    return get_cheapclaw_root() / "panel" / "backups"


def get_plans_path() -> Path:
    return get_cheapclaw_root() / "plans.json"


def get_outbox_dir() -> Path:
    return get_cheapclaw_root() / "outbox"


def get_deadletter_outbox_dir() -> Path:
    return get_cheapclaw_root() / "outbox_deadletter"


def get_task_events_dir() -> Path:
    return get_user_data_root() / "runtime" / "task_events"


def get_task_skills_root() -> Path:
    return get_cheapclaw_root() / "task_skills"


def get_channels_root() -> Path:
    return get_cheapclaw_root() / "channels"


def get_monitor_instructions_path() -> Path:
    return get_cheapclaw_root() / "monitor_instructions.json"


def get_monitor_task_id() -> Path:
    return get_cheapclaw_root() / "supervisor_task"


def get_tasks_root() -> Path:
    return get_cheapclaw_root() / "tasks"


def get_fleet_config_path() -> Optional[Path]:
    value = str(os.environ.get("CHEAPCLAW_FLEET_CONFIG_PATH", "")).strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


def ensure_cheapclaw_layout() -> None:
    for path in [
        get_cheapclaw_root(),
        get_panel_path().parent,
        get_panel_backups_dir(),
        get_outbox_dir(),
        get_deadletter_outbox_dir(),
        get_task_events_dir(),
        get_task_skills_root(),
        get_channels_root(),
        get_tasks_root(),
    ]:
        path.mkdir(parents=True, exist_ok=True)
    if not get_panel_path().exists():
        _atomic_write_json(
            get_panel_path(),
            {
                "version": 1,
                "channels": {},
                "service_state": {
                    "main_agent_task_id": str(get_cheapclaw_root() / "supervisor_task"),
                    "main_agent_running": False,
                    "main_agent_run_id": "",
                    "main_agent_last_started_at": "",
                    "main_agent_last_finished_at": "",
                    "watchdog_last_run_at": "",
                    "last_backup_path": "",
                },
            },
        )
    if not get_plans_path().exists():
        _atomic_write_json(get_plans_path(), {"version": 1, "plans": []})
    if not get_monitor_instructions_path().exists():
        _atomic_write_json(get_monitor_instructions_path(), {"version": 1, "instructions": []})


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(payload, tmp_file, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _write_backup(path: Path, payload: str) -> Path:
    backup_path = get_panel_backups_dir() / f"panel_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    backup_path.write_text(payload, encoding="utf-8")
    return backup_path


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def slugify(value: str, fallback: str = "item", max_len: int = 80) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    chars: List[str] = []
    last_dash = False
    for ch in text:
        if ch.isalnum():
            chars.append(ch.lower())
            last_dash = False
        elif ch in {"-", "_"}:
            chars.append(ch)
            last_dash = False
        else:
            if not last_dash:
                chars.append("-")
            last_dash = True
    return "".join(chars).strip("-_")[:max_len] or fallback


def load_panel() -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    try:
        panel = json.loads(get_panel_path().read_text(encoding="utf-8"))
    except Exception:
        panel = {"version": 1, "channels": {}, "service_state": {}}
    return _strip_legacy_panel_fields(panel)


def load_monitor_instructions() -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    try:
        payload = json.loads(get_monitor_instructions_path().read_text(encoding="utf-8"))
    except Exception:
        payload = {"version": 1, "instructions": []}
    if not isinstance(payload, dict):
        payload = {"version": 1, "instructions": []}
    payload.setdefault("version", 1)
    items = payload.get("instructions")
    if not isinstance(items, list):
        items = []
    payload["instructions"] = [item for item in items if isinstance(item, dict)]
    return payload


def load_monitor_instructions_for_root(user_data_root: str | Path) -> Dict[str, Any]:
    root = Path(user_data_root).expanduser().resolve() / "cheapclaw" / "monitor_instructions.json"
    if not root.exists():
        return {"version": 1, "instructions": []}
    try:
        payload = json.loads(root.read_text(encoding="utf-8"))
    except Exception:
        payload = {"version": 1, "instructions": []}
    if not isinstance(payload, dict):
        payload = {"version": 1, "instructions": []}
    payload.setdefault("version", 1)
    items = payload.get("instructions")
    if not isinstance(items, list):
        items = []
    payload["instructions"] = [item for item in items if isinstance(item, dict)]
    return payload


def save_monitor_instructions(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    normalized = payload if isinstance(payload, dict) else {}
    normalized.setdefault("version", 1)
    items = normalized.get("instructions")
    if not isinstance(items, list):
        items = []
    normalized["instructions"] = [item for item in items if isinstance(item, dict)]
    _atomic_write_json(get_monitor_instructions_path(), normalized)
    return normalized


def save_monitor_instructions_for_root(user_data_root: str | Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(user_data_root).expanduser().resolve() / "cheapclaw" / "monitor_instructions.json"
    normalized = payload if isinstance(payload, dict) else {}
    normalized.setdefault("version", 1)
    items = normalized.get("instructions")
    if not isinstance(items, list):
        items = []
    normalized["instructions"] = [item for item in items if isinstance(item, dict)]
    _atomic_write_json(path, normalized)
    return normalized


def save_panel(panel: Dict[str, Any], backup: bool = True) -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    panel = _strip_legacy_panel_fields(panel)
    if backup and get_panel_path().exists():
        backup_path = _write_backup(get_panel_path(), get_panel_path().read_text(encoding="utf-8"))
        panel.setdefault("service_state", {})["last_backup_path"] = str(backup_path)
    _atomic_write_json(get_panel_path(), panel)
    return panel


def mutate_panel(mutator):
    panel = load_panel()
    updated = mutator(panel)
    return save_panel(panel if updated is None else updated)


def _strip_legacy_panel_fields(panel: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(panel, dict):
        panel = {}
    panel.setdefault("version", 1)
    channels = panel.setdefault("channels", {})
    service_state = panel.setdefault("service_state", {})
    service_state.pop("main_agent_dirty", None)
    for channel_payload in channels.values():
        if not isinstance(channel_payload, dict):
            continue
        conversations = channel_payload.get("conversations")
        if not isinstance(conversations, dict):
            continue
        for conversation in conversations.values():
            if isinstance(conversation, dict):
                conversation.pop("dirty", None)
    return panel


def is_managed_task_id(task_id: str, *, allow_monitor_task: bool = True) -> bool:
    resolved = Path(task_id or "").expanduser().resolve()
    if allow_monitor_task and resolved == get_monitor_task_id().resolve():
        return True
    return _is_relative_to(resolved, get_tasks_root().resolve())


def assert_managed_task_id(task_id: str, *, allow_monitor_task: bool = True) -> str:
    resolved = str(Path(task_id or "").expanduser().resolve())
    if not is_managed_task_id(resolved, allow_monitor_task=allow_monitor_task):
        raise ValueError(f"task_id 超出 CheapClaw 当前 bot 的受管范围: {resolved}")
    return resolved


def create_monitor_instruction(
    *,
    instruction_type: str,
    summary: str,
    channel: str = "",
    conversation_id: str = "",
    source_message_id: str = "",
    task_id: str = "",
    sender_name: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    queue = load_monitor_instructions()
    instruction_id = f"moninstr_{uuid.uuid4().hex[:12]}"
    entry = {
        "instruction_id": instruction_id,
        "instruction_type": str(instruction_type or "event").strip() or "event",
        "summary": str(summary or "").strip(),
        "channel": str(channel or "").strip(),
        "conversation_id": str(conversation_id or "").strip(),
        "source_message_id": str(source_message_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "sender_name": str(sender_name or "").strip(),
        "payload": payload if isinstance(payload, dict) else {},
        "status": "pending",
        "created_at": now_iso(),
        "resolved_at": "",
        "resolution_note": "",
        "resolved_by_task_id": "",
    }
    queue.setdefault("instructions", []).append(entry)
    save_monitor_instructions(queue)
    return entry


def create_monitor_instruction_for_root(
    user_data_root: str | Path,
    *,
    instruction_type: str,
    summary: str,
    channel: str = "",
    conversation_id: str = "",
    source_message_id: str = "",
    task_id: str = "",
    sender_name: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    queue = load_monitor_instructions_for_root(user_data_root)
    instruction_id = f"moninstr_{uuid.uuid4().hex[:12]}"
    entry = {
        "instruction_id": instruction_id,
        "instruction_type": str(instruction_type or "event").strip() or "event",
        "summary": str(summary or "").strip(),
        "channel": str(channel or "").strip(),
        "conversation_id": str(conversation_id or "").strip(),
        "source_message_id": str(source_message_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "sender_name": str(sender_name or "").strip(),
        "payload": payload if isinstance(payload, dict) else {},
        "status": "pending",
        "created_at": now_iso(),
        "resolved_at": "",
        "resolution_note": "",
        "resolved_by_task_id": "",
    }
    queue.setdefault("instructions", []).append(entry)
    save_monitor_instructions_for_root(user_data_root, queue)
    return entry


def list_monitor_instructions(*, status: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    queue = load_monitor_instructions()
    items = [item for item in queue.get("instructions", []) if isinstance(item, dict)]
    if status:
        items = [item for item in items if str(item.get("status") or "") == status]
    items.sort(key=lambda item: (str(item.get("status") or ""), str(item.get("created_at") or ""), str(item.get("instruction_id") or "")))
    return items[: max(1, int(limit))]


def resolve_monitor_instruction(
    instruction_id: str,
    *,
    resolution_note: str = "",
    resolved_by_task_id: str = "",
) -> Dict[str, Any]:
    queue = load_monitor_instructions()
    target = None
    for item in queue.get("instructions", []):
        if str(item.get("instruction_id") or "") == str(instruction_id or "").strip():
            target = item
            break
    if target is None:
        return {"status": "error", "error": f"instruction not found: {instruction_id}"}
    target["status"] = "resolved"
    target["resolved_at"] = now_iso()
    target["resolution_note"] = str(resolution_note or "").strip()
    target["resolved_by_task_id"] = str(resolved_by_task_id or "").strip()
    save_monitor_instructions(queue)
    return {"status": "success", "instruction": target}


def pending_monitor_instruction_count() -> int:
    return sum(1 for item in load_monitor_instructions().get("instructions", []) if str(item.get("status") or "") == "pending")


def load_fleet_config(path: Optional[str] = None) -> Dict[str, Any]:
    candidate = Path(path).expanduser().resolve() if path else get_fleet_config_path()
    if candidate is None or not candidate.exists():
        return {"bots": []}
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        payload = {"bots": []}
    if not isinstance(payload, dict):
        payload = {"bots": []}
    bots = payload.get("bots")
    if not isinstance(bots, list):
        bots = []
    normalized = []
    for item in bots:
        if not isinstance(item, dict):
            continue
        bot_id = str(item.get("bot_id") or "").strip()
        user_data_root = str(item.get("user_data_root") or "").strip()
        if not bot_id or not user_data_root:
            continue
        normalized.append({
            "bot_id": bot_id,
            "display_name": str(item.get("display_name") or bot_id).strip() or bot_id,
            "user_data_root": str(Path(user_data_root).expanduser().resolve()),
            "llm_config_path": str(item.get("llm_config_path") or "").strip(),
            "host": str(item.get("host") or "127.0.0.1").strip() or "127.0.0.1",
            "port": int(item.get("port") or 0),
            "serve_webhooks": bool(item.get("serve_webhooks", False)),
            "enabled": bool(item.get("enabled", True)),
        })
    payload["bots"] = normalized
    return payload


def list_peer_bots(*, current_bot_id: str = "", include_self: bool = False, path: Optional[str] = None) -> List[Dict[str, Any]]:
    bots = load_fleet_config(path).get("bots", [])
    items = []
    for item in bots:
        bot_id = str(item.get("bot_id") or "").strip()
        if not include_self and current_bot_id and bot_id == current_bot_id:
            continue
        items.append(dict(item))
    return items


def get_peer_bot(bot_id: str, *, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    target = str(bot_id or "").strip()
    if not target:
        return None
    for item in load_fleet_config(path).get("bots", []):
        if str(item.get("bot_id") or "").strip() == target:
            return dict(item)
    return None


def format_monitor_instruction_message(entry: Dict[str, Any], *, managed_task_count: int = 0, running_task_count: int = 0) -> str:
    payload = entry.get("payload") or {}
    lines = [
        f"消息时间: {entry.get('created_at') or now_iso()}",
        f"instruction_id: {entry.get('instruction_id') or '-'}",
        f"消息类型: {entry.get('instruction_type') or 'event'}",
        f"发信者: {entry.get('sender_name') or '-'}",
        f"渠道: {entry.get('channel') or '-'}",
        f"会话: {entry.get('conversation_id') or '-'}",
        f"消息ID: {entry.get('source_message_id') or '-'}",
        f"关联任务: {entry.get('task_id') or '-'}",
        f"内容: {entry.get('summary') or ''}",
        f"当前你管理的 task_id 数量: {int(managed_task_count)}",
        f"正在运行: {int(running_task_count)}",
        "如果历史交互已经足够明确，请直接回复/派工/续跑；否则使用 CheapClaw 工具查询并在处理完成后 resolve 这个 instruction_id。",
    ]
    if isinstance(payload, dict) and payload:
        lines.append(f"补充信息: {json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(lines)


def _default_task_view(task_id: str) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "created_at": now_iso(),
        "last_launch_at": "",
        "agent_system": "",
        "agent_name": "",
        "status": "unknown",
        "share_context_path": "",
        "stack_path": "",
        "log_path": "",
        "skills_dir": "",
        "default_exposed_skills": [],
        "mcp_servers": [],
        "last_thinking": "",
        "last_thinking_at": "",
        "last_final_output": "",
        "last_final_output_at": "",
        "last_action_at": "",
        "last_log_at": "",
        "fresh_retry_count": 0,
        "last_watchdog_note": "",
    }


def ensure_conversation(
    panel: Dict[str, Any],
    *,
    channel: str,
    conversation_id: str,
    conversation_type: str = "group",
    display_name: Optional[str] = None,
    require_mention: bool = True,
) -> Dict[str, Any]:
    channel_payload = panel.setdefault("channels", {}).setdefault(channel, {"conversations": {}})
    conversations = channel_payload.setdefault("conversations", {})
    if conversation_id not in conversations:
        conversations[conversation_id] = {
            "channel": channel,
            "conversation_id": conversation_id,
            "conversation_type": conversation_type,
            "display_name": display_name or conversation_id,
            "trigger_policy": {"require_mention": bool(require_mention)},
            "message_history_path": str(history_path(channel, conversation_id)),
            "context_summary_path": str(conversation_context_path(channel, conversation_id)),
            "messages": [],
            "linked_tasks": [],
            "pending_events": [],
            "last_snapshot_path": "",
            "updated_at": "",
            "running_task_count": 0,
            "has_stale_running_tasks": False,
            "latest_user_message_at": "",
            "latest_bot_message_at": "",
            "unread_event_count": 0,
            "last_reply_summary": "",
            "conversation_tags": [],
            "message_task_bindings": [],
        }
    conversation = conversations[conversation_id]
    conversation["conversation_type"] = conversation_type or conversation.get("conversation_type") or "group"
    conversation["display_name"] = display_name or conversation.get("display_name") or conversation_id
    conversation["trigger_policy"] = {"require_mention": bool(require_mention)}
    conversation["message_history_path"] = str(history_path(channel, conversation_id))
    conversation["context_summary_path"] = str(conversation_context_path(channel, conversation_id))
    conversation.pop("dirty", None)
    conversation.setdefault("messages", [])
    conversation.setdefault("linked_tasks", [])
    conversation.setdefault("pending_events", [])
    conversation.setdefault("message_task_bindings", [])
    return conversation


def history_path(channel: str, conversation_id: str) -> Path:
    directory = get_channels_root() / slugify(channel, fallback="channel") / slugify(conversation_id, fallback="conversation")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "social_history.jsonl"


def conversation_context_path(channel: str, conversation_id: str) -> Path:
    directory = get_channels_root() / slugify(channel, fallback="channel") / slugify(conversation_id, fallback="conversation")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "latest_context.md"


def _short_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def refresh_conversation_context_file(
    channel: str,
    conversation_id: str,
    panel: Optional[Dict[str, Any]] = None,
    recent_message_limit: int = 10,
) -> Path:
    panel = panel or load_panel()
    conv = panel.get("channels", {}).get(channel, {}).get("conversations", {}).get(conversation_id, {})
    path = conversation_context_path(channel, conversation_id)
    if not conv:
        path.write_text("# Conversation Context\n\nConversation not found.\n", encoding="utf-8")
        return path

    messages = [item for item in conv.get("messages", []) if isinstance(item, dict)][-max(1, int(recent_message_limit)) :]
    linked_tasks = [item for item in conv.get("linked_tasks", []) if isinstance(item, dict)]
    pending_events = [item for item in conv.get("pending_events", []) if isinstance(item, dict)][-10:]
    bindings = [item for item in conv.get("message_task_bindings", []) if isinstance(item, dict)][-10:]

    lines = [
        f"# Conversation Context: {conv.get('display_name') or conversation_id}",
        "",
        f"- channel: {channel}",
        f"- conversation_id: {conversation_id}",
        f"- conversation_type: {conv.get('conversation_type') or ''}",
        f"- latest_user_message_at: {conv.get('latest_user_message_at') or ''}",
        f"- latest_bot_message_at: {conv.get('latest_bot_message_at') or ''}",
        "",
        "## Recent Messages",
    ]
    if not messages:
        lines.append("- (none)")
    else:
        for item in messages:
            lines.extend(
                [
                    f"- [{item.get('timestamp') or ''}] {item.get('direction') or ''} message_id={item.get('message_id') or ''}",
                    f"  {str(item.get('text') or '').strip()}",
                ]
            )

    lines.extend(["", "## Pending Events"])
    if not pending_events:
        lines.append("- (none)")
    else:
        for event in pending_events:
            lines.append(
                f"- type={event.get('type') or ''} message_id={event.get('message_id') or ''} task_id={event.get('task_id') or ''} ts={event.get('timestamp') or ''}"
            )

    lines.extend(["", "## Task List"])
    if not linked_tasks:
        lines.append("- (none)")
    else:
        linked_tasks.sort(key=lambda item: str(item.get("created_at") or item.get("task_id") or ""), reverse=True)
        for task in linked_tasks:
            lines.extend(
                [
                    f"- task_id: {task.get('task_id') or ''}",
                    f"  status: {task.get('status') or ''}",
                    f"  agent_system: {task.get('agent_system') or ''}",
                    f"  agent_name: {task.get('agent_name') or ''}",
                    f"  last_final_output_at: {task.get('last_final_output_at') or ''}",
                    f"  last_final_output_summary: {_short_text(task.get('last_final_output') or '', 200)}",
                ]
            )

    lines.extend(["", "## Recent Message Bindings"])
    if not bindings:
        lines.append("- (none)")
    else:
        for binding in bindings:
            lines.append(
                f"- message_id={binding.get('message_id') or ''} -> task_id={binding.get('task_id') or ''} ({binding.get('binding_type') or ''}) note={_short_text(binding.get('note') or '', 120)}"
            )

    lines.extend(
        [
            "",
            "## Retrieval Hints",
            f"- Full social history: {history_path(channel, conversation_id)}",
            f"- Full panel: {get_panel_path()}",
            "- If recent messages are insufficient, use cheapclaw_read_social_history with a larger limit or a time range.",
            "- If you need to search old records from files, use grep on the social history JSONL or panel JSON.",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def append_history(
    *,
    channel: str,
    conversation_id: str,
    event: Dict[str, Any],
    limit: int = 50,
) -> None:
    panel = load_panel()
    conversation = ensure_conversation(panel, channel=channel, conversation_id=conversation_id)
    history_file = Path(conversation["message_history_path"])
    history_file.parent.mkdir(parents=True, exist_ok=True)
    with open(history_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    messages = conversation.setdefault("messages", [])
    messages.append(event)
    del messages[:-max(1, int(limit))]
    if event.get("direction") == "outbound":
        conversation["latest_bot_message_at"] = event.get("timestamp", "")
    else:
        conversation["latest_user_message_at"] = event.get("timestamp", "")
    conversation["updated_at"] = event.get("timestamp", now_iso())
    save_panel(panel)
    refresh_conversation_context_file(channel, conversation_id, panel)


def read_social_history(
    *,
    channel: str,
    conversation_id: str,
    limit: int = 30,
    only_mentions_to_bot: bool = False,
    include_bot_replies: bool = True,
    from_message_id: str = "",
    to_message_id: str = "",
    before_timestamp: str = "",
    after_timestamp: str = "",
) -> List[Dict[str, Any]]:
    path = history_path(channel, conversation_id)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if only_mentions_to_bot and not item.get("is_mention_to_bot"):
            continue
        if not include_bot_replies and item.get("direction") == "outbound":
            continue
        if from_message_id and str(item.get("message_id") or "") < from_message_id:
            continue
        if to_message_id and str(item.get("message_id") or "") > to_message_id:
            continue
        timestamp = str(item.get("timestamp") or "")
        if before_timestamp and timestamp and timestamp >= before_timestamp:
            continue
        if after_timestamp and timestamp and timestamp <= after_timestamp:
            continue
        events.append(item)
    return events[-max(1, int(limit)):]


def queue_outbound_message(
    *,
    channel: str,
    conversation_id: str,
    message: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    normalized_message = str(message or "").strip()
    normalized_attachments = attachments or []
    metadata_payload = dict(metadata or {})
    metadata_payload.setdefault("retry_count", 0)
    metadata_payload.setdefault("max_retries", OUTBOX_DEFAULT_MAX_RETRIES)
    metadata_payload.setdefault("next_retry_at", now_iso())
    dedupe_hash = _compute_outbox_dedupe_hash(
        channel=str(channel or ""),
        conversation_id=str(conversation_id or ""),
        message=normalized_message,
        attachments=normalized_attachments,
    )
    metadata_payload.setdefault("dedupe_hash", dedupe_hash)

    for existing in list_outbox_events():
        existing_metadata = existing.get("metadata")
        if not isinstance(existing_metadata, dict):
            continue
        if str(existing_metadata.get("dedupe_hash") or "") == dedupe_hash:
            existing["deduplicated"] = True
            return existing

    event_id = f"out_{uuid.uuid4().hex[:12]}"
    payload = {
        "event_id": event_id,
        "channel": channel,
        "conversation_id": conversation_id,
        "message": normalized_message,
        "attachments": normalized_attachments,
        "metadata": metadata_payload,
        "created_at": now_iso(),
    }
    _atomic_write_json(get_outbox_dir() / f"{event_id}.json", payload)
    return payload


def list_outbox_events() -> List[Dict[str, Any]]:
    ensure_cheapclaw_layout()
    events = []
    for path in sorted(get_outbox_dir().glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        # 兼容早期/手工写入的 outbox 文件（可能缺少 event_id），避免成功发送后无法 ack 导致重复发送。
        normalized = dict(payload)
        if not str(normalized.get("event_id") or "").strip():
            normalized["event_id"] = path.stem
        events.append(normalized)
    return events


def ack_outbox_event(event_id: str) -> None:
    event_id = str(event_id or "").strip()
    if not event_id:
        return
    path = get_outbox_dir() / f"{event_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def save_outbox_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("outbox event_id is required")
    normalized = dict(payload)
    normalized["event_id"] = event_id
    normalized.setdefault("created_at", now_iso())
    if not isinstance(normalized.get("metadata"), dict):
        normalized["metadata"] = {}
    _atomic_write_json(get_outbox_dir() / f"{event_id}.json", normalized)
    return normalized


def move_outbox_event_to_deadletter(event: Dict[str, Any], *, reason: str) -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        return {}
    archived = dict(event)
    archived["deadletter_reason"] = str(reason or "").strip()
    archived["deadletter_at"] = now_iso()
    _atomic_write_json(get_deadletter_outbox_dir() / f"{event_id}.json", archived)
    ack_outbox_event(event_id)
    return archived


def emit_task_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    event_id = str(payload.get("event_id") or f"taskevt_{uuid.uuid4().hex[:12]}")
    event_payload = dict(payload)
    event_payload["event_id"] = event_id
    event_payload.setdefault("created_at", now_iso())
    _atomic_write_json(get_task_events_dir() / f"{event_id}.json", event_payload)
    return event_payload


def list_task_events() -> List[Dict[str, Any]]:
    ensure_cheapclaw_layout()
    events = []
    for path in sorted(get_task_events_dir().glob("*.json")):
        try:
            events.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return events


def ack_task_event(event_id: str) -> None:
    path = get_task_events_dir() / f"{event_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def load_plans() -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    try:
        return json.loads(get_plans_path().read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "plans": []}


def save_plans(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_cheapclaw_layout()
    _atomic_write_json(get_plans_path(), payload)
    return payload


def create_plan(
    *,
    name: str,
    scope: str,
    task_id: str = "",
    channel: str = "",
    conversation_id: str = "",
    interval_sec: int = 0,
    once_at: str = "",
    schedule_type: str = "",
    time_of_day: str = "",
    days_of_week: Optional[Iterable[str]] = None,
    message: str = "",
    enabled: bool = True,
) -> Dict[str, Any]:
    payload = load_plans()
    plans = payload.setdefault("plans", [])
    plan_id = f"plan_{uuid.uuid4().hex[:10]}"
    now = now_iso()
    schedule_type = str(schedule_type or ("once" if once_at else "interval")).strip() or "interval"
    weekday_names = [str(item).strip().lower() for item in (days_of_week or []) if str(item).strip()]
    next_run_at = once_at or (datetime.now().astimezone() + timedelta(seconds=max(1, int(interval_sec or 3600)))).isoformat(timespec="seconds")
    if schedule_type in {"daily", "weekly"} and time_of_day:
        next_run_at = compute_next_scheduled_run(
            schedule_type=schedule_type,
            time_of_day=time_of_day,
            days_of_week=weekday_names,
            now=parse_iso(now) or datetime.now().astimezone(),
        )
    plan = {
        "plan_id": plan_id,
        "name": name,
        "scope": scope,
        "task_id": task_id,
        "channel": channel,
        "conversation_id": conversation_id,
        "interval_sec": max(0, int(interval_sec or 0)),
        "once_at": once_at,
        "schedule_type": schedule_type,
        "time_of_day": time_of_day,
        "days_of_week": weekday_names,
        "next_run_at": next_run_at,
        "message": message,
        "enabled": bool(enabled),
        "created_at": now,
        "last_run_at": "",
        "last_result": "",
    }
    plans.append(plan)
    save_plans(payload)
    return plan


def cancel_plan(plan_id: str) -> bool:
    payload = load_plans()
    changed = False
    for plan in payload.get("plans", []):
        if plan.get("plan_id") == plan_id:
            plan["enabled"] = False
            plan["last_result"] = "cancelled"
            changed = True
    if changed:
        save_plans(payload)
    return changed


def list_plans(scope: str = "", enabled_only: bool = False) -> List[Dict[str, Any]]:
    items = load_plans().get("plans", [])
    result = []
    for item in items:
        if scope and item.get("scope") != scope:
            continue
        if enabled_only and not item.get("enabled", True):
            continue
        result.append(item)
    return result


def compute_next_scheduled_run(
    *,
    schedule_type: str,
    time_of_day: str,
    days_of_week: Optional[Iterable[str]] = None,
    now: Optional[datetime] = None,
) -> str:
    current = now or datetime.now().astimezone()
    raw = str(time_of_day or "").strip()
    try:
        hour_str, minute_str = raw.split(":", 1)
        hour = max(0, min(23, int(hour_str)))
        minute = max(0, min(59, int(minute_str)))
    except Exception:
        hour, minute = 8, 0
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if schedule_type == "daily":
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate.isoformat(timespec="seconds")

    weekday_names = [str(item).strip().lower() for item in (days_of_week or []) if str(item).strip()]
    if not weekday_names:
        weekday_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    mapping = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    targets = [mapping[item] for item in weekday_names if item in mapping]
    if not targets:
        targets = list(range(7))
    for delta in range(0, 8):
        test = candidate + timedelta(days=delta)
        if test.weekday() in targets and test > current:
            return test.isoformat(timespec="seconds")
    return (candidate + timedelta(days=7)).isoformat(timespec="seconds")


def list_conversation_tasks(channel: str, conversation_id: str) -> List[Dict[str, Any]]:
    panel = load_panel()
    conv = panel.get("channels", {}).get(channel, {}).get("conversations", {}).get(conversation_id, {})
    tasks = list(conv.get("linked_tasks", []))
    tasks.sort(
        key=lambda item: (
            str(item.get("last_final_output_at") or ""),
            str(item.get("last_action_at") or ""),
            str(item.get("last_thinking_at") or ""),
            str(item.get("last_log_at") or ""),
            str(item.get("task_id") or ""),
        ),
        reverse=True,
    )
    return tasks


def bind_messages_to_task(
    channel: str,
    conversation_id: str,
    task_id: str,
    message_ids: Iterable[str],
    *,
    note: str = "",
    binding_type: str = "task",
) -> Dict[str, Any]:
    message_ids = [str(item).strip() for item in (message_ids or []) if str(item).strip()]
    if not message_ids:
        return load_panel()

    def _mutate(panel: Dict[str, Any]) -> Dict[str, Any]:
        conv = ensure_conversation(panel, channel=channel, conversation_id=conversation_id)
        bindings = conv.setdefault("message_task_bindings", [])
        for message_id in message_ids:
            existing = next((item for item in bindings if item.get("message_id") == message_id), None)
            payload = {
                "message_id": message_id,
                "task_id": task_id,
                "binding_type": binding_type,
                "note": note,
                "bound_at": now_iso(),
            }
            if existing is None:
                bindings.append(payload)
            else:
                existing.update(payload)
        conv["updated_at"] = now_iso()
        return panel

    panel = mutate_panel(_mutate)
    refresh_conversation_context_file(channel, conversation_id, panel)
    return panel


def update_conversation_task(channel: str, conversation_id: str, task_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    def _mutate(panel: Dict[str, Any]) -> Dict[str, Any]:
        conv = ensure_conversation(panel, channel=channel, conversation_id=conversation_id)
        linked = conv.setdefault("linked_tasks", [])
        existing = next((item for item in linked if item.get("task_id") == task_id), None)
        if existing is None:
            existing = _default_task_view(task_id)
            linked.append(existing)
        existing.update(patch)
        conv["running_task_count"] = sum(1 for item in linked if item.get("status") == "running")
        conv["updated_at"] = now_iso()
        return panel
    panel = mutate_panel(_mutate)
    refresh_conversation_context_file(channel, conversation_id, panel)
    return panel


def set_task_visible_skills(task_id: str, skill_names: Iterable[str]) -> Dict[str, Any]:
    from core.hierarchy_manager import get_hierarchy_manager

    selected = []
    for name in skill_names or []:
        item = str(name).strip()
        if item and item not in selected:
            selected.append(item)

    manager = get_hierarchy_manager(str(Path(task_id).expanduser().resolve()))
    manager.set_runtime_metadata(visible_skills=selected)
    return {
        "task_id": manager.task_id,
        "visible_skills": selected,
        "updated_at": now_iso(),
    }


def extend_task_visible_skills(task_id: str, skill_names: Iterable[str]) -> Dict[str, Any]:
    from core.hierarchy_manager import get_hierarchy_manager

    manager = get_hierarchy_manager(str(Path(task_id).expanduser().resolve()))
    runtime = manager.get_runtime_metadata()
    current = runtime.get("visible_skills", []) if isinstance(runtime, dict) else []
    merged = []
    for name in list(current or []) + list(skill_names or []):
        item = str(name).strip()
        if item and item not in merged:
            merged.append(item)
    manager.set_runtime_metadata(visible_skills=merged)
    return {
        "task_id": manager.task_id,
        "visible_skills": merged,
        "updated_at": now_iso(),
    }


def clear_conversation_pending_events(channel: str, conversation_id: str) -> Dict[str, Any]:
    def _mutate(panel: Dict[str, Any]) -> Dict[str, Any]:
        conv = ensure_conversation(panel, channel=channel, conversation_id=conversation_id)
        conv["pending_events"] = []
        conv["unread_event_count"] = 0
        conv["updated_at"] = now_iso()
        return panel
    panel = mutate_panel(_mutate)
    refresh_conversation_context_file(channel, conversation_id, panel)
    return panel


def generate_task_id(channel: str, conversation_id: str, task_name: str) -> str:
    task_dir = get_cheapclaw_root() / "tasks" / slugify(channel, fallback="channel") / slugify(conversation_id, fallback="conversation")
    task_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str((task_dir / f"{stamp}_{slugify(task_name, fallback='task')}").resolve())


def list_global_skills(skills_root: Optional[str] = None) -> List[Dict[str, str]]:
    root = Path(skills_root).expanduser().resolve() if skills_root else Path(os.environ.get("MLA_SKILLS_LIBRARY_DIR", str(Path.home() / ".agent" / "skills"))).expanduser().resolve()
    skills = []
    if root.exists():
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir() and (child / "SKILL.md").exists():
                skills.append({"name": child.name, "path": str(child)})
    return skills


def reveal_skills_for_task(task_id: str, skill_names: Iterable[str], skills_root: Optional[str] = None) -> Dict[str, Any]:
    available = {item["name"]: Path(item["path"]) for item in list_global_skills(skills_root)}
    overlay_root = get_task_skills_root() / slugify(Path(task_id).name or "task", fallback="task")
    overlay_root.mkdir(parents=True, exist_ok=True)
    revealed = []
    missing = []
    for name in sorted(set(skill_names or [])):
        src = available.get(name)
        if not src:
            missing.append(name)
            continue
        dest = overlay_root / name
        if dest.exists():
            revealed.append(name)
            continue
        try:
            os.symlink(src, dest, target_is_directory=True)
        except OSError:
            shutil.copytree(src, dest)
        revealed.append(name)
    manifest = {
        "task_id": str(Path(task_id).expanduser().resolve()),
        "overlay_root": str(overlay_root),
        "revealed_skills": revealed,
        "missing_skills": missing,
        "updated_at": now_iso(),
    }
    (overlay_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def set_task_skills_for_task(task_id: str, skill_names: Iterable[str], skills_root: Optional[str] = None) -> Dict[str, Any]:
    available = {item["name"]: Path(item["path"]) for item in list_global_skills(skills_root)}
    overlay_root = get_task_skills_root() / slugify(Path(task_id).name or "task", fallback="task")
    overlay_root.mkdir(parents=True, exist_ok=True)
    selected_names = [str(name).strip() for name in (skill_names or []) if str(name).strip()]
    for entry in list(overlay_root.iterdir()):
        if entry.name == "manifest.json":
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink(missing_ok=True)
        elif entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    revealed = []
    missing = []
    for name in selected_names:
        src = available.get(name)
        if not src:
            missing.append(name)
            continue
        dest = overlay_root / name
        try:
            os.symlink(src, dest, target_is_directory=True)
        except OSError:
            shutil.copytree(src, dest)
        revealed.append(name)
    manifest = {
        "task_id": str(Path(task_id).expanduser().resolve()),
        "overlay_root": str(overlay_root),
        "revealed_skills": revealed,
        "missing_skills": missing,
        "updated_at": now_iso(),
        "mode": "exact",
    }
    (overlay_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
