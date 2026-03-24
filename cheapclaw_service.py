#!/usr/bin/env python3
"""Standalone CheapClaw application built on top of the public InfiAgent SDK."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from infiagent import InfiAgent, infiagent

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

try:
    from .tool_runtime_helpers import (
        ack_outbox_event,
        ack_task_event,
        append_history,
        assert_managed_task_id,
        bind_messages_to_task,
        compute_next_scheduled_run,
        create_monitor_instruction,
        ensure_conversation,
        format_monitor_instruction_message,
        generate_task_id,
        get_peer_bot,
        get_channels_root,
        list_global_skills,
        list_monitor_instructions,
        list_peer_bots,
        list_outbox_events,
        list_task_events,
        load_fleet_config,
        load_monitor_instructions_for_root,
        move_outbox_event_to_deadletter,
        OUTBOX_DEFAULT_MAX_RETRIES,
        load_plans,
        now_iso,
        pending_monitor_instruction_count,
        parse_iso,
        queue_outbound_message,
        refresh_conversation_context_file,
        resolve_monitor_instruction,
        save_plans,
        set_task_visible_skills,
        _short_text,
        save_outbox_event,
        slugify,
        update_conversation_task,
    )
except ImportError:
    from tool_runtime_helpers import (
        ack_outbox_event,
        ack_task_event,
        append_history,
        assert_managed_task_id,
        bind_messages_to_task,
        compute_next_scheduled_run,
        create_monitor_instruction,
        ensure_conversation,
        format_monitor_instruction_message,
        generate_task_id,
        get_peer_bot,
        get_channels_root,
        list_global_skills,
        list_monitor_instructions,
        list_peer_bots,
        list_outbox_events,
        list_task_events,
        load_fleet_config,
        load_monitor_instructions_for_root,
        move_outbox_event_to_deadletter,
        OUTBOX_DEFAULT_MAX_RETRIES,
        load_plans,
        now_iso,
        pending_monitor_instruction_count,
        parse_iso,
        queue_outbound_message,
        refresh_conversation_context_file,
        resolve_monitor_instruction,
        save_plans,
        set_task_visible_skills,
        _short_text,
        save_outbox_event,
        slugify,
        update_conversation_task,
    )

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover
    import websocket as websocket_client
except Exception:  # pragma: no cover
    websocket_client = None


APP_ROOT = Path(__file__).resolve().parent
ASSET_ROOT = APP_ROOT / "assets"
ASSET_AGENT_LIBRARY_ROOT = ASSET_ROOT / "agent_library"
ASSET_CONFIG_ROOT = ASSET_ROOT / "config"
ASSET_APP_CONFIG_EXAMPLE_PATH = ASSET_CONFIG_ROOT / "app_config.example.json"
ASSET_CHANNELS_EXAMPLE_PATH = ASSET_CONFIG_ROOT / "channels.example.json"
APP_TOOLS_ROOT = APP_ROOT / "tools_library"
APP_SKILLS_ROOT = APP_ROOT / "skills"
APP_WEB_ROOT = APP_ROOT / "web"
SERVICE_LOG_PATH: Optional[Path] = None
SERVICE_LOG_MAX_BYTES = 5 * 1024 * 1024
SERVICE_LOG_BACKUP_COUNT = 3
OUTBOX_BASE_BACKOFF_SECONDS = 5
OUTBOX_MAX_BACKOFF_SECONDS = 300
LOGGER = logging.getLogger("cheapclaw.service")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
LOGGER_INITIALIZED = False
ACTIVE_SERVICE: Optional["CheapClawService"] = None
FINAL_OUTPUT_HOOK_CALLBACK = f"{(APP_ROOT / 'cheapclaw_hooks.py').resolve()}:on_tool_event"
CHEAPCLAW_SYSTEM_BLOCK_START = "<cheapclaw_system_结构>"
CHEAPCLAW_SYSTEM_BLOCK_END = "</cheapclaw_system_结构>"


def _tail_text(path: Path, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _upsert_marked_block(content: str, *, start_tag: str, end_tag: str, block_text: str) -> str:
    original = str(content or "")
    body = str(block_text or "").strip()
    managed = f"{start_tag}\n{body}\n{end_tag}"

    start_idx = original.find(start_tag)
    if start_idx >= 0:
        end_idx = original.find(end_tag, start_idx + len(start_tag))
        if end_idx >= 0:
            end_idx += len(end_tag)
            updated = f"{original[:start_idx]}{managed}{original[end_idx:]}"
            return updated.rstrip() + "\n"

    if not original.strip():
        return managed + "\n"
    return original.rstrip() + "\n\n" + managed + "\n"


def _configure_service_logger(log_path: Path, *, max_bytes: int, backup_count: int) -> None:
    global LOGGER_INITIALIZED
    if LOGGER_INITIALIZED:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter(
            fmt="[CheapClaw %(asctime)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        file_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=max(1024, int(max_bytes)),
            backupCount=max(1, int(backup_count)),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        LOGGER.handlers.clear()
        LOGGER.addHandler(file_handler)
        # CHEAPCLAW_LOG_STDOUT=0 disables duplicate stdout logs while keeping file logs.
        if str(os.environ.get("CHEAPCLAW_LOG_STDOUT", "1")).strip().lower() not in {"0", "false", "no"}:
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(formatter)
            LOGGER.addHandler(stream_handler)
        LOGGER_INITIALIZED = True
    except Exception:
        LOGGER_INITIALIZED = False


def _log(message: str, *, level: str = "info") -> None:
    text = str(message or "")
    if LOGGER_INITIALIZED:
        try:
            getattr(LOGGER, level, LOGGER.info)(text)
            return
        except Exception:
            pass
    line = f"[CheapClaw {datetime.now().astimezone().isoformat(timespec='seconds')}] {text}"
    print(line, flush=True)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_cheapclaw_app_config_example() -> Dict[str, Any]:
    fallback = {
        "runtime": {
            "action_window_steps": 20,
            "thinking_interval": 20,
            "fresh_enabled": False,
            "fresh_interval_sec": 0,
        },
        "env": {
            "command_mode": "direct",
            "seed_builtin_resources": False,
        },
        "cheapclaw": {
            "watchdog_interval_sec": 86400,
            "default_exposed_skills": ["docx", "pptx", "xlsx", "find-skills"],
            "default_mcp_servers": [],
            "feishu_mode": "long_connection",
            "service_log_file": "cheapclaw_service.log",
            "service_log_max_bytes": 5242880,
            "service_log_backup_count": 3,
        },
    }
    return _load_json(ASSET_APP_CONFIG_EXAMPLE_PATH, fallback)


def _extract_cheapclaw_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    example_payload = _load_cheapclaw_app_config_example()
    example_cheapclaw = example_payload.get("cheapclaw", {}) if isinstance(example_payload.get("cheapclaw"), dict) else {}
    cheapclaw = payload.get("cheapclaw", {}) if isinstance(payload.get("cheapclaw"), dict) else {}
    default_skills = cheapclaw.get("default_exposed_skills", example_cheapclaw.get("default_exposed_skills", ["docx", "pptx", "xlsx", "find-skills"]))
    if not isinstance(default_skills, list):
        default_skills = list(example_cheapclaw.get("default_exposed_skills", ["docx", "pptx", "xlsx", "find-skills"]))
    default_mcp_servers = cheapclaw.get("default_mcp_servers", example_cheapclaw.get("default_mcp_servers", []))
    if not isinstance(default_mcp_servers, list):
        default_mcp_servers = list(example_cheapclaw.get("default_mcp_servers", []))
    return {
        "watchdog_interval_sec": max(60, int(cheapclaw.get("watchdog_interval_sec", example_cheapclaw.get("watchdog_interval_sec", 86400)) or example_cheapclaw.get("watchdog_interval_sec", 86400))),
        "default_exposed_skills": [str(item).strip() for item in default_skills if str(item).strip()],
        "default_mcp_servers": [item for item in default_mcp_servers if isinstance(item, dict)],
        "feishu_mode": str(cheapclaw.get("feishu_mode", example_cheapclaw.get("feishu_mode", "long_connection")) or example_cheapclaw.get("feishu_mode", "long_connection")).strip(),
        "service_log_file": str(cheapclaw.get("service_log_file", example_cheapclaw.get("service_log_file", "cheapclaw_service.log")) or example_cheapclaw.get("service_log_file", "cheapclaw_service.log")).strip(),
        "service_log_max_bytes": max(
            1024,
            int(cheapclaw.get("service_log_max_bytes", SERVICE_LOG_MAX_BYTES) or SERVICE_LOG_MAX_BYTES),
        ),
        "service_log_backup_count": max(
            1,
            int(cheapclaw.get("service_log_backup_count", SERVICE_LOG_BACKUP_COUNT) or SERVICE_LOG_BACKUP_COUNT),
        ),
    }


@dataclass(frozen=True)
class CheapClawPaths:
    user_data_root: Path
    cheapclaw_root: Path
    tools_root: Path
    panel_dir: Path
    panel_path: Path
    panel_lock_path: Path
    panel_backups_dir: Path
    plans_path: Path
    config_dir: Path
    app_config_path: Path
    app_config_example_path: Path
    channels_config_path: Path
    channels_example_path: Path
    channels_root: Path
    outbox_dir: Path
    tasks_root: Path
    task_skills_root: Path
    supervisor_task_id: Path
    monitor_instructions_path: Path
    monitor_system_add_path: Path
    runtime_dir: Path
    runtime_state_path: Path

    @classmethod
    def from_user_data_root(cls, user_data_root: str | Path, app_name: str = "cheapclaw") -> "CheapClawPaths":
        root = Path(user_data_root).expanduser().resolve()
        cheapclaw_root = root / app_name
        panel_dir = cheapclaw_root / "panel"
        runtime_dir = cheapclaw_root / "runtime"
        config_dir = cheapclaw_root / "config"
        return cls(
            user_data_root=root,
            cheapclaw_root=cheapclaw_root,
            tools_root=root / "tools_library",
            panel_dir=panel_dir,
            panel_path=panel_dir / "panel.json",
            panel_lock_path=panel_dir / "panel.lock",
            panel_backups_dir=panel_dir / "backups",
            plans_path=cheapclaw_root / "plans.json",
            config_dir=config_dir,
            app_config_path=config_dir / "app_config.json",
            app_config_example_path=config_dir / "app_config.example.json",
            channels_config_path=config_dir / "channels.json",
            channels_example_path=config_dir / "channels.example.json",
            channels_root=cheapclaw_root / "channels",
            outbox_dir=cheapclaw_root / "outbox",
            tasks_root=cheapclaw_root / "tasks",
            task_skills_root=cheapclaw_root / "task_skills",
            supervisor_task_id=cheapclaw_root / "supervisor_task",
            monitor_instructions_path=cheapclaw_root / "monitor_instructions.json",
            monitor_system_add_path=cheapclaw_root / "supervisor_task" / "system-add.md",
            runtime_dir=runtime_dir,
            runtime_state_path=runtime_dir / "state.json",
        )


class CheapClawPanelStore:
    def __init__(self, paths: CheapClawPaths, history_preview_limit: int = 50):
        self.paths = paths
        self.history_preview_limit = max(1, int(history_preview_limit))
        self._thread_lock = threading.RLock()
        self.ensure_layout()

    def ensure_layout(self) -> None:
        for path in [
            self.paths.cheapclaw_root,
            self.paths.tools_root,
            self.paths.panel_dir,
            self.paths.panel_backups_dir,
            self.paths.config_dir,
            self.paths.runtime_dir,
            self.paths.channels_root,
            self.paths.outbox_dir,
            self.paths.tasks_root,
            self.paths.task_skills_root,
            self.paths.supervisor_task_id,
        ]:
            path.mkdir(parents=True, exist_ok=True)
        if not self.paths.panel_path.exists():
            _atomic_write_text(
                self.paths.panel_path,
                json.dumps(
                    {
                        "version": 1,
                        "channels": {},
                        "service_state": {
                            "main_agent_task_id": str(self.paths.supervisor_task_id),
                            "main_agent_running": False,
                            "main_agent_run_id": "",
                            "main_agent_last_started_at": "",
                            "main_agent_last_finished_at": "",
                            "watchdog_last_run_at": "",
                            "last_backup_path": "",
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        if not self.paths.plans_path.exists():
            _atomic_write_text(self.paths.plans_path, json.dumps({"version": 1, "plans": []}, ensure_ascii=False, indent=2))
        if not self.paths.runtime_state_path.exists():
            _atomic_write_text(self.paths.runtime_state_path, json.dumps({"webhook_server": {}, "telegram_offsets": {}}, ensure_ascii=False, indent=2))
        if not self.paths.monitor_instructions_path.exists():
            _atomic_write_text(self.paths.monitor_instructions_path, json.dumps({"version": 1, "instructions": []}, ensure_ascii=False, indent=2))

    @contextmanager
    def _file_lock(self):
        self.paths.panel_dir.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with open(self.paths.panel_lock_path, "a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def load_panel(self) -> Dict[str, Any]:
        return self._normalize_panel(_load_json(self.paths.panel_path, {"version": 1, "channels": {}, "service_state": {}}))

    def save_panel(self, panel: Dict[str, Any], *, backup: bool = True) -> Dict[str, Any]:
        normalized = self._normalize_panel(panel)
        with self._file_lock():
            self._write_panel_locked(normalized, backup=backup)
        return normalized

    def mutate(self, updater: Callable[[Dict[str, Any]], Dict[str, Any] | None]) -> Dict[str, Any]:
        with self._file_lock():
            current = self.load_panel()
            updated = updater(current)
            panel = current if updated is None else updated
            normalized = self._normalize_panel(panel)
            self._write_panel_locked(normalized, backup=True)
            return normalized

    def _write_panel_locked(self, panel: Dict[str, Any], *, backup: bool) -> None:
        if backup and self.paths.panel_path.exists():
            backup_path = self.paths.panel_backups_dir / f"panel_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
            backup_path.write_text(self.paths.panel_path.read_text(encoding="utf-8"), encoding="utf-8")
            panel.setdefault("service_state", {})["last_backup_path"] = str(backup_path)
        _atomic_write_text(self.paths.panel_path, json.dumps(panel, ensure_ascii=False, indent=2))

    def _normalize_panel(self, panel: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(panel, dict):
            panel = {}
        panel.setdefault("version", 1)
        panel.setdefault("channels", {})
        panel.setdefault("service_state", {})
        defaults = {
            "main_agent_task_id": str(self.paths.supervisor_task_id),
            "main_agent_running": False,
            "main_agent_run_id": "",
            "main_agent_last_started_at": "",
            "main_agent_last_finished_at": "",
            "watchdog_last_run_at": "",
            "last_backup_path": "",
        }
        for key, value in defaults.items():
            panel["service_state"].setdefault(key, value)
        panel["service_state"].pop("main_agent_dirty", None)
        for channel, payload in list(panel["channels"].items()):
            if not isinstance(payload, dict):
                panel["channels"][channel] = {"conversations": {}}
                payload = panel["channels"][channel]
            payload.setdefault("conversations", {})
            for conversation_id, conversation in list(payload["conversations"].items()):
                payload["conversations"][conversation_id] = self._normalize_conversation(channel, conversation_id, conversation)
        return panel

    def _normalize_conversation(self, channel: str, conversation_id: str, conversation: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(conversation, dict):
            conversation = {}
        defaults = {
            "channel": channel,
            "conversation_id": conversation_id,
            "conversation_type": "group",
            "display_name": conversation_id,
            "trigger_policy": {"require_mention": True},
            "message_history_path": str(get_channels_root() / slugify(channel) / slugify(conversation_id) / "social_history.jsonl"),
            "context_summary_path": str(get_channels_root() / slugify(channel) / slugify(conversation_id) / "latest_context.md"),
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
        for key, value in defaults.items():
            conversation.setdefault(key, value)
        conversation.pop("dirty", None)
        normalized_tasks = []
        for item in conversation.get("linked_tasks", []):
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("task_id") or "").strip()
            if not task_id:
                continue
            task_defaults = {
                "task_id": task_id,
                "created_at": "",
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
                "pid_alive": None,
                "watchdog_observation": "",
                "watchdog_suspected_state": "",
            }
            task_defaults.update(item)
            normalized_tasks.append(task_defaults)
        conversation["linked_tasks"] = normalized_tasks
        conversation["running_task_count"] = sum(1 for item in normalized_tasks if item.get("status") == "running")
        conversation["unread_event_count"] = len(conversation.get("pending_events", []))
        return conversation

    def record_social_message(self, **kwargs) -> Dict[str, Any]:
        timestamp = kwargs.get("timestamp") or now_iso()
        channel = str(kwargs.get("channel") or "").strip()
        conversation_id = str(kwargs.get("conversation_id") or "").strip()
        message_text = str(kwargs.get("message_text") or "").strip()
        attachments = kwargs.get("attachments") or []
        if not channel or not conversation_id or (not message_text and not attachments):
            raise ValueError("channel, conversation_id and message_text/attachments are required")

        def _update(panel: Dict[str, Any]) -> Dict[str, Any]:
            conv = ensure_conversation(
                panel,
                channel=channel,
                conversation_id=conversation_id,
                conversation_type=str(kwargs.get("conversation_type") or "group"),
                display_name=kwargs.get("display_name") or conversation_id,
                require_mention=bool(kwargs.get("require_mention", True)),
            )
            event = {
                "message_id": str(kwargs.get("message_id") or "").strip(),
                "timestamp": timestamp,
                "sender_id": str(kwargs.get("sender_id") or "").strip(),
                "sender_name": str(kwargs.get("sender_name") or "").strip(),
                "text": message_text,
                "attachments": attachments,
                "is_mention_to_bot": bool(kwargs.get("is_mention_to_bot", False)),
                "direction": "inbound",
            }
            history_path = Path(conv["message_history_path"])
            history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(history_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            conv.setdefault("messages", []).append(event)
            del conv["messages"][:-self.history_preview_limit]
            conv.setdefault("pending_events", []).append({"type": "social_message", "timestamp": timestamp, "message_id": event["message_id"]})
            conv["updated_at"] = timestamp
            conv["latest_user_message_at"] = timestamp
            conv["unread_event_count"] = len(conv["pending_events"])
            return panel
        panel = self.mutate(_update)
        refresh_conversation_context_file(channel, conversation_id, panel)
        return panel

    def set_main_agent_state(self, *, running: bool, run_id: str = "") -> Dict[str, Any]:
        def _update(panel: Dict[str, Any]) -> Dict[str, Any]:
            state = panel["service_state"]
            state["main_agent_running"] = bool(running)
            if run_id:
                state["main_agent_run_id"] = run_id
            if running:
                state["main_agent_last_started_at"] = now_iso()
            else:
                state["main_agent_last_finished_at"] = now_iso()
            return panel
        return self.mutate(_update)

    def mark_watchdog_tick(self) -> Dict[str, Any]:
        def _update(panel: Dict[str, Any]) -> Dict[str, Any]:
            panel["service_state"]["watchdog_last_run_at"] = now_iso()
            return panel
        return self.mutate(_update)


class ChannelAdapter:
    name = "base"

    def __init__(self, config: Dict[str, Any], service: "CheapClawService"):
        self.config = config
        self.service = service

    def poll_events(self) -> List[Dict[str, Any]]:
        return []

    def send_message(self, conversation_id: str, message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
        raise NotImplementedError

    @staticmethod
    def _normalize_attachments(attachments: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        items = []
        for item in attachments or []:
            if not isinstance(item, dict):
                continue
            local_path = str(item.get("local_path") or item.get("path") or "").strip()
            if not local_path:
                continue
            path = Path(local_path).expanduser().resolve()
            if not path.exists() or not path.is_file():
                continue
            guessed_mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            items.append({
                "path": path,
                "filename": str(item.get("filename") or path.name),
                "mime_type": str(item.get("mime_type") or guessed_mime),
                "kind": str(item.get("kind") or "auto").strip().lower() or "auto",
                "caption": str(item.get("caption") or "").strip(),
            })
        return items

    def handle_webhook_get(self, path: str, query: Dict[str, List[str]], headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"

    def handle_webhook_post(self, path: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"


def _ensure_cheapclaw_app_config(paths: CheapClawPaths) -> Dict[str, Any]:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    example_payload = _load_cheapclaw_app_config_example()
    _atomic_write_text(paths.app_config_example_path, json.dumps(example_payload, ensure_ascii=False, indent=2))
    if not paths.app_config_path.exists():
        _atomic_write_text(paths.app_config_path, json.dumps(example_payload, ensure_ascii=False, indent=2))
    payload = _load_json(paths.app_config_path, example_payload)
    if not isinstance(payload, dict):
        payload = {}
    runtime_cfg = payload.setdefault("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
        payload["runtime"] = runtime_cfg
    runtime_cfg.setdefault("action_window_steps", example_payload["runtime"]["action_window_steps"])
    runtime_cfg.setdefault("thinking_interval", example_payload["runtime"]["thinking_interval"])
    runtime_cfg.setdefault("fresh_enabled", example_payload["runtime"]["fresh_enabled"])
    runtime_cfg.setdefault("fresh_interval_sec", example_payload["runtime"]["fresh_interval_sec"])
    if int(runtime_cfg.get("action_window_steps", 20) or 20) == 20 and int(runtime_cfg.get("thinking_interval", 20) or 20) == 10:
        runtime_cfg["thinking_interval"] = 20
    env_cfg = payload.setdefault("env", {})
    if not isinstance(env_cfg, dict):
        env_cfg = {}
        payload["env"] = env_cfg
    env_cfg["seed_builtin_resources"] = False
    env_cfg.setdefault("command_mode", example_payload["env"]["command_mode"])
    context_cfg = payload.setdefault("context", {})
    if not isinstance(context_cfg, dict):
        context_cfg = {}
        payload["context"] = context_cfg
    example_context = example_payload.get("context", {})
    if not isinstance(example_context, dict):
        example_context = {}
    context_cfg.setdefault(
        "user_history_compress_threshold_tokens",
        int(example_context.get("user_history_compress_threshold_tokens", 1500) or 1500),
    )
    context_cfg.setdefault(
        "structured_call_info_compress_threshold_agents",
        int(example_context.get("structured_call_info_compress_threshold_agents", 10) or 10),
    )
    context_cfg.setdefault(
        "structured_call_info_compress_threshold_tokens",
        int(example_context.get("structured_call_info_compress_threshold_tokens", 2200) or 2200),
    )
    context_cfg["user_history_compress_threshold_tokens"] = max(
        0,
        int(context_cfg.get("user_history_compress_threshold_tokens", 1500) or 1500),
    )
    context_cfg["structured_call_info_compress_threshold_agents"] = max(
        1,
        int(context_cfg.get("structured_call_info_compress_threshold_agents", 10) or 10),
    )
    context_cfg["structured_call_info_compress_threshold_tokens"] = max(
        0,
        int(context_cfg.get("structured_call_info_compress_threshold_tokens", 2200) or 2200),
    )
    cheapclaw_cfg = payload.setdefault("cheapclaw", {})
    if not isinstance(cheapclaw_cfg, dict):
        cheapclaw_cfg = {}
        payload["cheapclaw"] = cheapclaw_cfg
    for key, value in example_payload["cheapclaw"].items():
        cheapclaw_cfg.setdefault(key, value)
    default_skills = cheapclaw_cfg.get("default_exposed_skills", [])
    if not isinstance(default_skills, list):
        default_skills = list(example_payload["cheapclaw"].get("default_exposed_skills", []))
    for skill_name in example_payload["cheapclaw"].get("default_exposed_skills", []):
        if skill_name not in default_skills:
            default_skills.append(skill_name)
    cheapclaw_cfg["default_exposed_skills"] = default_skills
    if not isinstance(cheapclaw_cfg.get("default_mcp_servers"), list):
        cheapclaw_cfg["default_mcp_servers"] = list(example_payload["cheapclaw"].get("default_mcp_servers", []))
    _atomic_write_text(paths.app_config_path, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _sync_root_app_config_from_cheapclaw(user_data_root: Path, cheapclaw_cfg: Dict[str, Any]) -> None:
    root_path = Path(user_data_root).expanduser().resolve() / "config" / "app_config.json"
    root_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_json(root_path, {})
    if not isinstance(payload, dict):
        payload = {}
    runtime_cfg = cheapclaw_cfg.get("runtime", {}) if isinstance(cheapclaw_cfg, dict) else {}
    context_cfg = cheapclaw_cfg.get("context", {}) if isinstance(cheapclaw_cfg, dict) else {}
    env_cfg = payload.setdefault("env", {})
    env_cfg["seed_builtin_resources"] = False
    env_cfg.setdefault("command_mode", "direct")
    runtime = payload.setdefault("runtime", {})
    if isinstance(runtime_cfg, dict):
        for key in ("action_window_steps", "thinking_interval", "fresh_enabled", "fresh_interval_sec"):
            if key in runtime_cfg:
                runtime[key] = runtime_cfg[key]
    context = payload.setdefault("context", {})
    if isinstance(context_cfg, dict):
        for key in (
            "user_history_compress_threshold_tokens",
            "structured_call_info_compress_threshold_agents",
            "structured_call_info_compress_threshold_tokens",
        ):
            if key in context_cfg:
                context[key] = context_cfg[key]
    payload.pop("cheapclaw", None)
    root_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_channels_config_for_root(user_data_root: str | Path) -> Dict[str, Any]:
    root = Path(user_data_root).expanduser().resolve()
    channels_path = root / "cheapclaw" / "config" / "channels.json"
    example_path = ASSET_CHANNELS_EXAMPLE_PATH.resolve()
    return _load_json(channels_path, _load_json(example_path, {}))


def _enabled_channel_bot_specs(config: Dict[str, Any]) -> List[Dict[str, str]]:
    specs: List[Dict[str, str]] = []
    if not isinstance(config, dict):
        return specs
    required_fields = {
        "telegram": ["bot_token"],
        "feishu": ["app_id", "app_secret"],
        "whatsapp": ["access_token", "phone_number_id"],
        "discord": ["bot_token"],
        "qq": ["onebot_api_base"],
        "wechat": ["onebot_api_base"],
        "localweb": [],
    }
    for channel, fields in required_fields.items():
        payload = config.get(channel) or {}
        if not isinstance(payload, dict) or not payload.get("enabled"):
            continue
        if not all(str(payload.get(field) or "").strip() for field in fields):
            continue
        bot_id = str(payload.get("bot_id") or channel).strip() or channel
        display_name = str(payload.get("display_name") or bot_id).strip() or bot_id
        specs.append({
            "channel": channel,
            "bot_id": bot_id,
            "display_name": display_name,
        })
    return specs


class TelegramAdapter(ChannelAdapter):
    name = "telegram"

    def __init__(self, config: Dict[str, Any], service: "CheapClawService"):
        super().__init__(config, service)
        self.bot_token = str(config.get("bot_token") or "").strip()
        self.allowed_chats = {str(item) for item in config.get("allowed_chats", [])}
        self._api_root = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""
        self._state = self.service.load_runtime_state()
        self._me_cache = self._state.get("telegram_bot_me") or {}

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        if not self._api_root:
            return {}
        response = requests.request(method, self._api_root + endpoint, timeout=30, **kwargs)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def _get_me(self) -> Dict[str, Any]:
        if self._me_cache:
            return self._me_cache
        data = self._request("GET", "/getMe")
        self._me_cache = data.get("result", {}) if data.get("ok") else {}
        state = self.service.load_runtime_state()
        state["telegram_bot_me"] = self._me_cache
        self.service.save_runtime_state(state)
        return self._me_cache

    def _message_mentions_bot(self, message: Dict[str, Any], text: str, is_group: bool) -> bool:
        if not is_group:
            return True
        me = self._get_me() or {}
        bot_username = str(me.get("username") or "").strip()
        bot_id = str(me.get("id") or "").strip()
        if bot_username and f"@{bot_username}".lower() in text.lower():
            return True

        for entity in (message.get("entities") or []) + (message.get("caption_entities") or []):
            if not isinstance(entity, dict):
                continue
            entity_type = str(entity.get("type") or "")
            if entity_type == "mention" and bot_username:
                offset = int(entity.get("offset") or 0)
                length = int(entity.get("length") or 0)
                if text[offset:offset + length].lower() == f"@{bot_username}".lower():
                    return True
            if entity_type == "text_mention":
                user = entity.get("user") or {}
                if bot_id and str(user.get("id") or "") == bot_id:
                    return True

        reply_to = message.get("reply_to_message") or {}
        reply_from = reply_to.get("from") or {}
        if bot_id and str(reply_from.get("id") or "") == bot_id:
            return True
        if reply_from.get("is_bot") and bot_username and str(reply_from.get("username") or "").lower() == bot_username.lower():
            return True
        return False

    def poll_events(self) -> List[Dict[str, Any]]:
        if not self.bot_token:
            return []
        state = self.service.load_runtime_state()
        offsets = state.setdefault("telegram_offsets", {})
        offset = int(offsets.get("default", 0) or 0)
        data = self._request("GET", "/getUpdates", params={"timeout": 1, "offset": offset + 1})
        items = []
        for result in data.get("result", []):
            update_id = int(result.get("update_id") or 0)
            message = (
                result.get("message")
                or result.get("edited_message")
                or result.get("channel_post")
                or result.get("edited_channel_post")
                or result.get("business_message")
                or result.get("edited_business_message")
                or {}
            )
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id") or "").strip()
            if not chat_id:
                continue
            if self.allowed_chats and chat_id not in self.allowed_chats:
                continue
            text = str(message.get("text") or message.get("caption") or "").strip()
            is_group = chat.get("type") in {"group", "supergroup"}
            mention = self._message_mentions_bot(message, text, is_group)
            if is_group and not mention:
                offsets["default"] = update_id
                continue
            from_user = message.get("from") or message.get("sender_chat") or {}
            sender_name = " ".join(
                part for part in [
                    str(from_user.get("first_name") or "").strip(),
                    str(from_user.get("last_name") or "").strip(),
                ] if part
            ).strip()
            if not sender_name:
                sender_name = str(from_user.get("title") or from_user.get("username") or "")
            items.append({
                "event_id": f"telegram_{update_id}",
                "channel": "telegram",
                "conversation_id": chat_id,
                "conversation_type": "group" if is_group else "person",
                "display_name": str(chat.get("title") or chat.get("username") or chat_id),
                "sender_id": str(from_user.get("id") or ""),
                "sender_name": sender_name,
                "message_id": str(message.get("message_id") or ""),
                "message_text": text,
                "attachments": [],
                "timestamp": datetime.fromtimestamp(int(message.get("date") or time.time())).astimezone().isoformat(timespec="seconds"),
                "is_mention_to_bot": mention,
                "require_mention": is_group,
            })
            offsets["default"] = update_id
        self.service.save_runtime_state(state)
        return items

    def send_message(self, conversation_id: str, message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
        if not self.bot_token:
            return False, "telegram bot_token is missing"
        last_remote_id = ""
        normalized = self._normalize_attachments(attachments)
        if message:
            payload = {"chat_id": conversation_id, "text": message}
            try:
                data = self._request("POST", "/sendMessage", json=payload)
            except Exception as exc:
                return False, str(exc)
            if not data.get("ok"):
                return False, json.dumps(data, ensure_ascii=False)
            last_remote_id = str((data.get("result") or {}).get("message_id") or "")
        for item in normalized:
            mime_type = item["mime_type"]
            method = "/sendPhoto" if mime_type.startswith("image/") else "/sendDocument"
            field = "photo" if method == "/sendPhoto" else "document"
            data_payload = {"chat_id": conversation_id}
            caption = item["caption"] or (message if not last_remote_id else "")
            if caption:
                data_payload["caption"] = caption
            with open(item["path"], "rb") as fh:
                files = {field: (item["filename"], fh, mime_type)}
                try:
                    data = self._request("POST", method, data=data_payload, files=files)
                except Exception as exc:
                    return False, str(exc)
            if not data.get("ok"):
                return False, json.dumps(data, ensure_ascii=False)
            last_remote_id = str((data.get("result") or {}).get("message_id") or "")
        if not message and not normalized:
            return False, "telegram message or attachments are required"
        return True, last_remote_id


class FeishuAdapter(ChannelAdapter):
    name = "feishu"

    def __init__(self, config: Dict[str, Any], service: "CheapClawService"):
        super().__init__(config, service)
        self.app_id = str(config.get("app_id") or "").strip()
        self.app_secret = str(config.get("app_secret") or "").strip()
        self.verify_token = str(config.get("verify_token") or "").strip()
        self.encrypt_key = str(config.get("encrypt_key") or "").strip()
        self.mode = str(config.get("mode") or "long_connection").strip() or "long_connection"
        self.api_root = "https://open.feishu.cn/open-apis"
        self._queue_lock = threading.Lock()
        self._queued_events: List[Dict[str, Any]] = []
        self._long_conn_thread: Optional[threading.Thread] = None
        self._long_conn_started = False
        if self.mode == "long_connection":
            self._start_long_connection()

    def _enqueue_event(self, payload: Dict[str, Any]) -> None:
        with self._queue_lock:
            self._queued_events.append(payload)

    def _normalize_message_event(self, header: Dict[str, Any], event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return None
        text = ""
        content_raw = message.get("content")
        if isinstance(content_raw, str):
            try:
                content = json.loads(content_raw)
            except Exception:
                content = {}
            text = str(content.get("text") or "").strip()
        chat_type = str(message.get("chat_type") or "")
        chat_id = str(message.get("chat_id") or "").strip()
        if not chat_id:
            return None
        mention = (chat_type != "group") or ("<at" in text)
        return {
            "event_id": f"feishu_{message.get('message_id')}",
            "channel": "feishu",
            "conversation_id": chat_id,
            "conversation_type": "group" if chat_type == "group" else "person",
            "display_name": chat_id,
            "sender_id": str((sender.get("sender_id") or {}).get("open_id") or ""),
            "sender_name": str((sender.get("sender_id") or {}).get("user_id") or ""),
            "message_id": str(message.get("message_id") or ""),
            "message_text": text,
            "attachments": [],
            "timestamp": now_iso(),
            "is_mention_to_bot": mention,
            "require_mention": chat_type == "group",
        }

    def _start_long_connection(self) -> None:
        if self._long_conn_started:
            return
        self._long_conn_started = True
        if not self.app_id or not self.app_secret:
            _log("Feishu long connection skipped: missing app_id/app_secret")
            return

        def _runner() -> None:
            try:
                import lark_oapi as lark

                def _handle_message(data) -> None:
                    try:
                        payload = json.loads(lark.JSON.marshal(data) or "{}")
                        normalized = self._normalize_message_event(payload.get("header") or {}, payload.get("event") or {})
                        if normalized:
                            self._enqueue_event(normalized)
                    except Exception as exc:
                        _log(f"Feishu long connection event parse failed: {exc}")

                event_handler = lark.EventDispatcherHandler.builder(
                    self.encrypt_key,
                    self.verify_token,
                ).register_p2_im_message_receive_v1(_handle_message).build()

                client = lark.ws.Client(
                    self.app_id,
                    self.app_secret,
                    event_handler=event_handler,
                    log_level=lark.LogLevel.INFO,
                )
                _log("Feishu long connection started")
                client.start()
            except Exception as exc:
                detail = str(exc)
                if "python-socks" in detail:
                    detail += " (detected SOCKS proxy; clear all_proxy/ALL_PROXY or install python-socks)"
                _log(f"Feishu long connection stopped: {detail}")

        self._long_conn_thread = threading.Thread(target=_runner, daemon=True, name="cheapclaw-feishu-long-conn")
        self._long_conn_thread.start()

    def poll_events(self) -> List[Dict[str, Any]]:
        if self.mode != "long_connection":
            return []
        with self._queue_lock:
            items = list(self._queued_events)
            self._queued_events.clear()
            return items

    def _tenant_access_token(self) -> str:
        state = self.service.load_runtime_state()
        cached = state.get("feishu_token") or {}
        expire_at = parse_iso(cached.get("expire_at", ""))
        if cached.get("token") and expire_at and expire_at > datetime.now().astimezone() + timedelta(seconds=60):
            return str(cached["token"])
        if not self.app_id or not self.app_secret:
            return ""
        response = requests.post(
            f"{self.api_root}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        token = str(data.get("tenant_access_token") or "")
        expire_at = datetime.now().astimezone() + timedelta(seconds=int(data.get("expire", 0) or 0))
        state["feishu_token"] = {"token": token, "expire_at": expire_at.isoformat(timespec="seconds")}
        self.service.save_runtime_state(state)
        return token

    def handle_webhook_post(self, path: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return 400, {"Content-Type": "application/json"}, b'{"error":"invalid json"}'

        challenge = payload.get("challenge")
        if challenge:
            return 200, {"Content-Type": "application/json"}, json.dumps({"challenge": challenge}).encode("utf-8")

        normalized = self._normalize_message_event(payload.get("header") or {}, payload.get("event") or {})
        if normalized:
            self.service.ingest_event(normalized)
        return 200, {"Content-Type": "application/json"}, b'{"ok":true}'

    def send_message(self, conversation_id: str, message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
        token = self._tenant_access_token()
        if not token:
            return False, "feishu app_id/app_secret are missing"
        last_remote_id = ""
        normalized = self._normalize_attachments(attachments)
        if message:
            response = requests.post(
                f"{self.api_root}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
                json={"receive_id": conversation_id, "msg_type": "text", "content": json.dumps({"text": message}, ensure_ascii=False)},
                timeout=30,
            )
            if response.status_code >= 300:
                return False, response.text
            data = response.json()
            last_remote_id = str(((data.get("data") or {}).get("message_id")) or "")
        for item in normalized:
            mime_type = item["mime_type"]
            is_image = mime_type.startswith("image/")
            upload_url = f"{self.api_root}/im/v1/images" if is_image else f"{self.api_root}/im/v1/files"
            with open(item["path"], "rb") as fh:
                files = {"image" if is_image else "file": (item["filename"], fh, mime_type)}
                data_payload = {"image_type": "message"} if is_image else {"file_type": "stream", "file_name": item["filename"]}
                upload_resp = requests.post(
                    upload_url,
                    headers={"Authorization": f"Bearer {token}"},
                    data=data_payload,
                    files=files,
                    timeout=60,
                )
            if upload_resp.status_code >= 300:
                return False, upload_resp.text
            upload_data = upload_resp.json()
            key_name = "image_key" if is_image else "file_key"
            media_key = str(((upload_data.get("data") or {}).get(key_name)) or "")
            if not media_key:
                return False, json.dumps(upload_data, ensure_ascii=False)
            msg_type = "image" if is_image else "file"
            content = {key_name: media_key}
            send_resp = requests.post(
                f"{self.api_root}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
                json={"receive_id": conversation_id, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)},
                timeout=30,
            )
            if send_resp.status_code >= 300:
                return False, send_resp.text
            data = send_resp.json()
            last_remote_id = str(((data.get("data") or {}).get("message_id")) or "")
        if not message and not normalized:
            return False, "feishu message or attachments are required"
        return True, last_remote_id


class DiscordAdapter(ChannelAdapter):
    name = "discord"
    DEFAULT_INTENTS = 1 + 512 + 4096 + 32768  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT

    def __init__(self, config: Dict[str, Any], service: "CheapClawService"):
        super().__init__(config, service)
        self.bot_token = str(config.get("bot_token") or "").strip()
        self.bot_id = str(config.get("bot_id") or service.bot_id).strip() or service.bot_id
        self.display_name = str(config.get("display_name") or service.bot_display_name or self.bot_id).strip() or self.bot_id
        self.require_mention_in_guild = bool(config.get("require_mention_in_guild", True))
        self.intents = int(config.get("intents") or self.DEFAULT_INTENTS)
        self.api_root = "https://discord.com/api/v10"
        self._queue_lock = threading.Lock()
        self._queued_events: List[Dict[str, Any]] = []
        self._gateway_thread: Optional[threading.Thread] = None
        self._gateway_started = False
        self._stop_event = threading.Event()
        self._seq: Optional[int] = None
        self._bot_user_id = str(config.get("bot_user_id") or "").strip()
        if self.bot_token:
            self._start_gateway()

    def _enqueue_event(self, payload: Dict[str, Any]) -> None:
        with self._queue_lock:
            self._queued_events.append(payload)

    def poll_events(self) -> List[Dict[str, Any]]:
        with self._queue_lock:
            items = list(self._queued_events)
            self._queued_events.clear()
            return items

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json",
        }

    def _get_gateway_url(self) -> str:
        if not self.bot_token:
            return ""
        try:
            response = requests.get(f"{self.api_root}/gateway/bot", headers=self._auth_headers(), timeout=30)
            response.raise_for_status()
            payload = response.json() if response.content else {}
            return str(payload.get("url") or "").strip()
        except Exception as exc:
            _log(f"Discord gateway url fetch failed: {exc}")
            return ""

    def _identify_gateway(self, ws: Any) -> None:
        payload = {
            "op": 2,
            "d": {
                "token": self.bot_token,
                "intents": self.intents,
                "properties": {
                    "os": sys.platform,
                    "browser": "cheapclaw",
                    "device": "cheapclaw",
                },
            },
        }
        ws.send(json.dumps(payload, ensure_ascii=False))

    def _normalize_gateway_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict):
            return None
        author = message.get("author") or {}
        if bool(author.get("bot")):
            return None
        channel_id = str(message.get("channel_id") or "").strip()
        if not channel_id:
            return None
        guild_id = str(message.get("guild_id") or "").strip()
        content = str(message.get("content") or "")
        mentions_payload = message.get("mentions") or []
        mentioned_ids: List[str] = []
        for item in mentions_payload:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("id") or "").strip()
            if user_id and user_id not in mentioned_ids:
                mentioned_ids.append(user_id)
        mention_to_bot = True
        require_mention = False
        if guild_id:
            require_mention = self.require_mention_in_guild
            mention_to_bot = False
            if self._bot_user_id and self._bot_user_id in mentioned_ids:
                mention_to_bot = True
            if self._bot_user_id and (f"<@{self._bot_user_id}>" in content or f"<@!{self._bot_user_id}>" in content):
                mention_to_bot = True
        attachment_items: List[Dict[str, Any]] = []
        for item in (message.get("attachments") or []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            attachment_items.append({
                "kind": "url",
                "url": url,
                "filename": str(item.get("filename") or "").strip(),
                "mime_type": str(item.get("content_type") or "").strip(),
            })
        sender_id = str(author.get("id") or "").strip()
        sender_name = str(author.get("global_name") or author.get("username") or sender_id).strip() or sender_id
        conversation_id = f"guild:{guild_id}:channel:{channel_id}" if guild_id else f"dm:{channel_id}"
        conversation_type = "group" if guild_id else "person"
        timestamp_raw = str(message.get("timestamp") or "").strip()
        return {
            "event_id": f"discord_{message.get('id')}",
            "channel": "discord",
            "conversation_id": conversation_id,
            "conversation_type": conversation_type,
            "display_name": conversation_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_id": str(message.get("id") or ""),
            "message_text": content,
            "attachments": attachment_items,
            "timestamp": timestamp_raw or now_iso(),
            "is_mention_to_bot": bool(mention_to_bot),
            "require_mention": bool(require_mention),
        }

    def _start_gateway(self) -> None:
        if self._gateway_started:
            return
        self._gateway_started = True
        if websocket_client is None:
            _log("Discord gateway skipped: websocket-client is not installed")
            return

        def _runner() -> None:
            while not self._stop_event.is_set():
                gateway_url = self._get_gateway_url()
                if not gateway_url:
                    time.sleep(5)
                    continue
                ws = None
                try:
                    ws = websocket_client.create_connection(
                        f"{gateway_url}?v=10&encoding=json",
                        timeout=30,
                        enable_multithread=True,
                    )
                    ws.settimeout(1.0)
                    heartbeat_interval = 45.0
                    next_heartbeat = time.time() + heartbeat_interval
                    identified = False
                    while not self._stop_event.is_set():
                        if identified and time.time() >= next_heartbeat:
                            ws.send(json.dumps({"op": 1, "d": self._seq}))
                            next_heartbeat = time.time() + heartbeat_interval
                        try:
                            raw = ws.recv()
                        except websocket_client.WebSocketTimeoutException:
                            continue
                        if not raw:
                            continue
                        packet = json.loads(raw)
                        if not isinstance(packet, dict):
                            continue
                        op = int(packet.get("op") or 0)
                        seq = packet.get("s")
                        event_type = str(packet.get("t") or "")
                        data = packet.get("d") if isinstance(packet.get("d"), dict) else packet.get("d")
                        if isinstance(seq, int):
                            self._seq = seq
                        if op == 10 and isinstance(data, dict):
                            heartbeat_interval = max(
                                5.0,
                                float(data.get("heartbeat_interval") or 45000) / 1000.0,
                            )
                            next_heartbeat = time.time() + heartbeat_interval
                            self._identify_gateway(ws)
                            identified = True
                            continue
                        if op == 11:
                            continue
                        if op in {7, 9}:
                            break
                        if op == 0 and event_type == "READY" and isinstance(data, dict):
                            user = data.get("user") or {}
                            if isinstance(user, dict):
                                self._bot_user_id = str(user.get("id") or self._bot_user_id).strip() or self._bot_user_id
                            continue
                        if op == 0 and event_type == "MESSAGE_CREATE" and isinstance(data, dict):
                            normalized = self._normalize_gateway_message(data)
                            if normalized:
                                self._enqueue_event(normalized)
                except Exception as exc:
                    _log(f"Discord gateway loop error: {exc}")
                finally:
                    if ws is not None:
                        try:
                            ws.close()
                        except Exception:
                            pass
                time.sleep(3)

        self._gateway_thread = threading.Thread(target=_runner, daemon=True, name="cheapclaw-discord-gateway")
        self._gateway_thread.start()

    @staticmethod
    def _channel_id_from_conversation(conversation_id: str) -> str:
        raw = str(conversation_id or "").strip()
        if ":channel:" in raw:
            return raw.split(":channel:", 1)[1].strip()
        if raw.startswith("dm:"):
            return raw[3:].strip()
        return raw

    def send_message(self, conversation_id: str, message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
        if not self.bot_token:
            return False, "discord bot_token is missing"
        channel_id = self._channel_id_from_conversation(conversation_id)
        if not channel_id:
            return False, "discord conversation_id is missing channel id"
        normalized = self._normalize_attachments(attachments)
        if not message and not normalized:
            return False, "discord message or attachments are required"

        url = f"{self.api_root}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {self.bot_token}"}
        try:
            if normalized:
                file_handles = []
                files = []
                attachment_meta: List[Dict[str, Any]] = []
                for idx, item in enumerate(normalized):
                    fh = open(item["path"], "rb")
                    file_handles.append(fh)
                    files.append((f"files[{idx}]", (item["filename"], fh, item["mime_type"])))
                    attachment_meta.append({
                        "id": idx,
                        "filename": item["filename"],
                        "description": item["caption"] or "",
                    })
                payload = {
                    "content": message or "",
                    "attachments": attachment_meta,
                }
                data = {"payload_json": json.dumps(payload, ensure_ascii=False)}
                response = requests.post(url, headers=headers, data=data, files=files, timeout=60)
                for fh in file_handles:
                    fh.close()
            else:
                response = requests.post(
                    url,
                    headers={**headers, "Content-Type": "application/json"},
                    json={"content": message},
                    timeout=30,
                )
            if response.status_code >= 300:
                return False, response.text
            payload = response.json() if response.content else {}
            return True, str(payload.get("id") or "")
        except Exception as exc:
            return False, str(exc)


class OneBotV11Adapter(ChannelAdapter):
    CQ_AT_PATTERN = re.compile(r"\[CQ:at,qq=([^\],]+)")

    def __init__(self, config: Dict[str, Any], service: "CheapClawService", *, channel_name: str):
        super().__init__(config, service)
        self.channel_name = channel_name
        self.api_base = str(config.get("onebot_api_base") or config.get("api_base") or "").strip().rstrip("/")
        self.access_token = str(config.get("onebot_access_token") or config.get("access_token") or "").strip()
        self.post_secret = str(config.get("onebot_post_secret") or config.get("post_secret") or "").strip()
        self.self_id = str(config.get("onebot_self_id") or config.get("self_id") or "").strip()
        self.bot_id = str(config.get("bot_id") or service.bot_id).strip() or service.bot_id
        self.require_mention_in_group = bool(config.get("require_mention_in_group", True))

    def _auth_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _verify_signature(self, body: bytes, headers: Dict[str, str]) -> bool:
        if not self.post_secret:
            return True
        provided = str(headers.get("X-Signature") or headers.get("x-signature") or "").strip()
        expected = "sha1=" + hmac.new(self.post_secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
        return bool(provided) and hmac.compare_digest(provided, expected)

    def _extract_text_and_mention(self, payload: Dict[str, Any]) -> Tuple[str, bool]:
        raw_message = payload.get("message")
        text_parts: List[str] = []
        mention_to_bot = False
        if isinstance(raw_message, str):
            text_parts.append(raw_message)
            if self.self_id and f"[CQ:at,qq={self.self_id}" in raw_message:
                mention_to_bot = True
            if self.self_id:
                for target in self.CQ_AT_PATTERN.findall(raw_message):
                    if str(target).strip() == self.self_id:
                        mention_to_bot = True
                        break
        elif isinstance(raw_message, list):
            for segment in raw_message:
                if not isinstance(segment, dict):
                    continue
                seg_type = str(segment.get("type") or "").strip().lower()
                data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
                if seg_type == "text":
                    text_parts.append(str(data.get("text") or ""))
                    continue
                if seg_type == "at":
                    target = str(data.get("qq") or data.get("id") or data.get("user_id") or "").strip()
                    if self.self_id and target == self.self_id:
                        mention_to_bot = True
                    text_parts.append(f"@{target}" if target else "@")
                    continue
                text_parts.append(f"[{seg_type}]")
        text = "".join(text_parts).strip()
        return text, mention_to_bot

    def handle_webhook_post(self, path: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        if not self._verify_signature(body, headers):
            return 403, {"Content-Type": "application/json"}, b'{"error":"invalid signature"}'
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return 400, {"Content-Type": "application/json"}, b'{"error":"invalid json"}'
        if not isinstance(payload, dict):
            return 400, {"Content-Type": "application/json"}, b'{"error":"invalid payload"}'
        if str(payload.get("post_type") or "") != "message":
            return 200, {"Content-Type": "application/json"}, b'{"ok":true}'

        message_type = str(payload.get("message_type") or "").strip().lower()
        sender_id = str(payload.get("user_id") or "").strip()
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        sender_name = str(sender.get("card") or sender.get("nickname") or sender_id).strip() or sender_id
        text, mention_to_bot = self._extract_text_and_mention(payload)
        require_mention = False
        conversation_id = ""
        conversation_type = "person"
        if message_type == "group":
            group_id = str(payload.get("group_id") or "").strip()
            if not group_id:
                return 200, {"Content-Type": "application/json"}, b'{"ok":true}'
            conversation_id = f"group:{group_id}"
            conversation_type = "group"
            require_mention = self.require_mention_in_group
        else:
            if not sender_id:
                return 200, {"Content-Type": "application/json"}, b'{"ok":true}'
            conversation_id = f"user:{sender_id}"
            conversation_type = "person"
            mention_to_bot = True

        ts = payload.get("time")
        timestamp = now_iso()
        if isinstance(ts, (int, float)) and ts > 0:
            timestamp = datetime.fromtimestamp(float(ts), tz=datetime.now().astimezone().tzinfo).isoformat(timespec="seconds")

        message_id = str(payload.get("message_id") or "").strip()
        self.service.ingest_event({
            "event_id": f"{self.channel_name}_{message_id or uuid.uuid4().hex[:10]}",
            "channel": self.channel_name,
            "conversation_id": conversation_id,
            "conversation_type": conversation_type,
            "display_name": conversation_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_id": message_id,
            "message_text": text,
            "attachments": [],
            "timestamp": timestamp,
            "is_mention_to_bot": bool(mention_to_bot),
            "require_mention": bool(require_mention),
        })
        return 200, {"Content-Type": "application/json"}, b'{"ok":true}'

    def send_message(self, conversation_id: str, message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
        if not self.api_base:
            return False, f"{self.channel_name} onebot_api_base is missing"
        if attachments:
            return False, f"{self.channel_name} attachments are not supported in OneBot adapter yet"
        if not message:
            return False, f"{self.channel_name} message is required"

        conversation_text = str(conversation_id or "").strip()
        action = "send_private_msg"
        payload: Dict[str, Any] = {"message": message}
        if conversation_text.startswith("group:"):
            action = "send_group_msg"
            payload["group_id"] = conversation_text.split("group:", 1)[1].strip()
        elif conversation_text.startswith("user:"):
            action = "send_private_msg"
            payload["user_id"] = conversation_text.split("user:", 1)[1].strip()
        else:
            payload["user_id"] = conversation_text

        try:
            response = requests.post(
                f"{self.api_base}/{action}",
                headers=self._auth_headers(),
                json=payload,
                timeout=30,
            )
            if response.status_code >= 300:
                return False, response.text
            data = response.json() if response.content else {}
            status = str(data.get("status") or "").strip().lower()
            retcode = int(data.get("retcode") or 0)
            if status not in {"ok", "async"} or retcode != 0:
                return False, json.dumps(data, ensure_ascii=False)
            message_id = str(((data.get("data") or {}).get("message_id")) or "")
            return True, message_id
        except Exception as exc:
            return False, str(exc)


class WhatsAppCloudAdapter(ChannelAdapter):
    name = "whatsapp"

    def __init__(self, config: Dict[str, Any], service: "CheapClawService"):
        super().__init__(config, service)
        self.access_token = str(config.get("access_token") or "").strip()
        self.phone_number_id = str(config.get("phone_number_id") or "").strip()
        self.verify_token = str(config.get("verify_token") or "").strip()
        self.api_version = str(config.get("api_version") or "v21.0").strip()
        self.api_root = f"https://graph.facebook.com/{self.api_version}"

    def handle_webhook_get(self, path: str, query: Dict[str, List[str]], headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        mode = (query.get("hub.mode") or [""])[0]
        token = (query.get("hub.verify_token") or [""])[0]
        challenge = (query.get("hub.challenge") or [""])[0]
        if mode == "subscribe" and token and token == self.verify_token:
            return 200, {"Content-Type": "text/plain; charset=utf-8"}, challenge.encode("utf-8")
        return 403, {"Content-Type": "text/plain; charset=utf-8"}, b"forbidden"

    def handle_webhook_post(self, path: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return 400, {"Content-Type": "application/json"}, b'{"error":"invalid json"}'
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                contacts = value.get("contacts") or []
                contact_name = str((contacts[0].get("profile") or {}).get("name") or "") if contacts else ""
                for message in value.get("messages", []) or []:
                    text = str(((message.get("text") or {}).get("body")) or "").strip()
                    from_id = str(message.get("from") or "").strip()
                    self.service.ingest_event({
                        "event_id": f"whatsapp_{message.get('id')}",
                        "channel": "whatsapp",
                        "conversation_id": from_id,
                        "conversation_type": "person",
                        "display_name": contact_name or from_id,
                        "sender_id": from_id,
                        "sender_name": contact_name,
                        "message_id": str(message.get("id") or ""),
                        "message_text": text,
                        "attachments": [],
                        "timestamp": now_iso(),
                        "is_mention_to_bot": True,
                        "require_mention": False,
                    })
        return 200, {"Content-Type": "application/json"}, b'{"ok":true}'

    def send_message(self, conversation_id: str, message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
        if not self.access_token or not self.phone_number_id:
            return False, "whatsapp access_token or phone_number_id is missing"
        last_remote_id = ""
        normalized = self._normalize_attachments(attachments)
        if message:
            response = requests.post(
                f"{self.api_root}/{self.phone_number_id}/messages",
                headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
                json={"messaging_product": "whatsapp", "to": conversation_id, "type": "text", "text": {"body": message}},
                timeout=30,
            )
            if response.status_code >= 300:
                return False, response.text
            data = response.json()
            if data.get("messages"):
                last_remote_id = str((data.get("messages") or [{}])[0].get("id") or "")
        for item in normalized:
            mime_type = item["mime_type"]
            media_type = "image" if mime_type.startswith("image/") else "document"
            with open(item["path"], "rb") as fh:
                files = {"file": (item["filename"], fh, mime_type)}
                upload_resp = requests.post(
                    f"{self.api_root}/{self.phone_number_id}/media",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    data={"messaging_product": "whatsapp", "type": mime_type},
                    files=files,
                    timeout=60,
                )
            if upload_resp.status_code >= 300:
                return False, upload_resp.text
            upload_data = upload_resp.json()
            media_id = str(upload_data.get("id") or "")
            if not media_id:
                return False, json.dumps(upload_data, ensure_ascii=False)
            payload = {
                "messaging_product": "whatsapp",
                "to": conversation_id,
                "type": media_type,
                media_type: {"id": media_id},
            }
            if media_type == "document":
                payload[media_type]["filename"] = item["filename"]
            caption = item["caption"] or (message if not last_remote_id else "")
            if caption:
                payload[media_type]["caption"] = caption
            send_resp = requests.post(
                f"{self.api_root}/{self.phone_number_id}/messages",
                headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            if send_resp.status_code >= 300:
                return False, send_resp.text
            data = send_resp.json()
            if data.get("messages"):
                last_remote_id = str((data.get("messages") or [{}])[0].get("id") or "")
        if not message and not normalized:
            return False, "whatsapp message or attachments are required"
        return True, last_remote_id


class LocalWebAdapter(ChannelAdapter):
    name = "localweb"
    MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_.-]{1,80})")

    def __init__(self, config: Dict[str, Any], service: "CheapClawService"):
        super().__init__(config, service)
        self.bot_id = str(config.get("bot_id") or service.bot_id).strip() or service.bot_id
        self.display_name = str(config.get("display_name") or service.bot_display_name or self.bot_id).strip() or self.bot_id
        self.require_mention_in_group = bool(config.get("require_mention_in_group", True))
        self.shared_root = self._resolve_shared_root()
        self.shared_root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.shared_root / "events.jsonl"
        self.events_lock_path = self.shared_root / "events.lock"
        self.conversations_path = self.shared_root / "conversations.json"
        self._ensure_storage()

    def _resolve_shared_root(self) -> Path:
        if self.service.fleet_config_path:
            fleet_path = Path(self.service.fleet_config_path).expanduser().resolve()
            return fleet_path.parent / "localweb"
        return self.service.paths.user_data_root / "cheapclaw" / "localweb"

    def _ensure_storage(self) -> None:
        if not self.conversations_path.exists():
            _atomic_write_text(
                self.conversations_path,
                json.dumps({"version": 1, "conversations": {}}, ensure_ascii=False, indent=2),
            )
        self.events_path.touch(exist_ok=True)
        self.events_lock_path.touch(exist_ok=True)

    def _load_conversations_payload(self) -> Dict[str, Any]:
        payload = _load_json(self.conversations_path, {"version": 1, "conversations": {}})
        if not isinstance(payload, dict):
            payload = {"version": 1, "conversations": {}}
        payload.setdefault("version", 1)
        conversations = payload.get("conversations")
        if not isinstance(conversations, dict):
            conversations = {}
        normalized: Dict[str, Dict[str, Any]] = {}
        for raw_id, item in conversations.items():
            conversation_id = str(raw_id or "").strip()
            if not conversation_id or not isinstance(item, dict):
                continue
            participant_bots = []
            for bot_id in item.get("participant_bots", []):
                bot_text = str(bot_id or "").strip()
                if bot_text and bot_text not in participant_bots:
                    participant_bots.append(bot_text)
            normalized[conversation_id] = {
                "conversation_id": conversation_id,
                "display_name": str(item.get("display_name") or conversation_id).strip() or conversation_id,
                "conversation_type": "group" if str(item.get("conversation_type") or "group").strip() == "group" else "person",
                "participant_bots": participant_bots,
                "require_mention": bool(item.get("require_mention", self.require_mention_in_group)),
                "created_at": str(item.get("created_at") or now_iso()),
                "updated_at": str(item.get("updated_at") or now_iso()),
                "created_by": str(item.get("created_by") or "system"),
            }
        payload["conversations"] = normalized
        return payload

    def _save_conversations_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _atomic_write_text(self.conversations_path, json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    def list_conversations(self) -> List[Dict[str, Any]]:
        payload = self._load_conversations_payload()
        items = [item for item in payload.get("conversations", {}).values() if isinstance(item, dict)]
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items

    def ensure_conversation_record(
        self,
        *,
        conversation_id: str,
        display_name: str = "",
        conversation_type: str = "",
        participant_bots: Optional[List[str]] = None,
        require_mention: Optional[bool] = None,
        created_by: str = "",
        include_self_participant: bool = True,
    ) -> Dict[str, Any]:
        conv_id = str(conversation_id or "").strip()
        if not conv_id:
            conv_id = f"lc_{uuid.uuid4().hex[:12]}"
        payload = self._load_conversations_payload()
        conversations = payload.setdefault("conversations", {})
        existing = conversations.get(conv_id) if isinstance(conversations.get(conv_id), dict) else {}
        if not existing:
            existing = {
                "conversation_id": conv_id,
                "display_name": conv_id,
                "conversation_type": "group",
                "participant_bots": [],
                "require_mention": self.require_mention_in_group,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "created_by": created_by or "system",
            }
        if display_name.strip():
            existing["display_name"] = display_name.strip()
        if conversation_type.strip() in {"group", "person"}:
            existing["conversation_type"] = conversation_type.strip()
        if require_mention is not None:
            existing["require_mention"] = bool(require_mention)
        members = list(existing.get("participant_bots") or [])
        if participant_bots is not None:
            members = []
            for bot_id in participant_bots:
                bot_text = str(bot_id or "").strip()
                if bot_text and bot_text not in members:
                    members.append(bot_text)
        if include_self_participant and self.bot_id not in members:
            members.append(self.bot_id)
        existing["participant_bots"] = members
        existing["updated_at"] = now_iso()
        conversations[conv_id] = existing
        self._save_conversations_payload(payload)
        return existing

    def _known_bot_id_map(self) -> Dict[str, str]:
        items = list_peer_bots(
            current_bot_id=self.service.bot_id,
            include_self=True,
            path=self.service.fleet_config_path,
        )
        if not items:
            items = [{
                "bot_id": self.service.bot_id,
                "display_name": self.service.bot_display_name,
            }]
        mapping: Dict[str, str] = {}
        for item in items:
            bot_id = str(item.get("bot_id") or "").strip()
            display_name = str(item.get("display_name") or "").strip()
            if bot_id:
                mapping[bot_id.lower()] = bot_id
            if display_name:
                mapping[display_name.lower()] = bot_id or display_name
        return mapping

    def parse_mentions(self, text: str) -> List[str]:
        bot_map = self._known_bot_id_map()
        mentions: List[str] = []
        for token in self.MENTION_PATTERN.findall(str(text or "")):
            key = str(token or "").strip().lower()
            if not key:
                continue
            resolved = bot_map.get(key) or str(token or "").strip()
            if resolved and resolved not in mentions:
                mentions.append(resolved)
        return mentions

    def _append_event_line(self, payload: Dict[str, Any]) -> None:
        lock_file = open(self.events_lock_path, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def emit_event(
        self,
        *,
        conversation_id: str,
        message: str,
        sender_id: str,
        sender_name: str,
        sender_type: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
        display_name: str = "",
        participant_bots: Optional[List[str]] = None,
        require_mention: Optional[bool] = None,
    ) -> Dict[str, Any]:
        text = str(message or "").strip()
        if not text and not (attachments or []):
            return {"status": "error", "error": "localweb message or attachments are required"}
        mentions = self.parse_mentions(text)
        merged_participants = list(participant_bots or [])
        for mention in mentions:
            if mention not in merged_participants:
                merged_participants.append(mention)
        conv = self.ensure_conversation_record(
            conversation_id=conversation_id,
            display_name=display_name,
            conversation_type="group" if len(merged_participants) > 1 else "person",
            participant_bots=merged_participants if merged_participants else None,
            require_mention=require_mention,
            created_by=sender_id,
        )
        event_payload = {
            "event_id": f"lwevt_{uuid.uuid4().hex[:12]}",
            "channel": "localweb",
            "conversation_id": str(conv.get("conversation_id") or ""),
            "conversation_type": str(conv.get("conversation_type") or "group"),
            "display_name": str(conv.get("display_name") or conv.get("conversation_id") or ""),
            "participant_bots": list(conv.get("participant_bots") or []),
            "require_mention": bool(conv.get("require_mention", self.require_mention_in_group)),
            "sender_id": str(sender_id or ""),
            "sender_name": str(sender_name or sender_id or ""),
            "sender_type": str(sender_type or "user"),
            "message_text": text,
            "attachments": attachments or [],
            "mentions": mentions,
            "timestamp": now_iso(),
        }
        self._append_event_line(event_payload)
        return {"status": "success", "event": event_payload}

    def _load_new_bus_events(self) -> List[Tuple[int, Dict[str, Any]]]:
        state = self.service.load_runtime_state()
        offsets = state.setdefault("localweb_offsets", {})
        line_offset = int(offsets.get("line") or 0)
        lines = self.events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        new_events: List[Tuple[int, Dict[str, Any]]] = []
        next_offset = line_offset
        total_lines = len(lines)
        for idx, line in enumerate(lines[line_offset:], start=line_offset + 1):
            if not line.strip():
                next_offset = idx
                continue
            try:
                payload = json.loads(line)
            except Exception:
                # 可能读到正在写入中的最后一行，保留到下一轮再解析，避免丢事件。
                if idx == total_lines:
                    break
                next_offset = idx
                continue
            if isinstance(payload, dict):
                new_events.append((idx, payload))
            next_offset = idx
        offsets["line"] = next_offset
        state["localweb_offsets"] = offsets
        self.service.save_runtime_state(state)
        return new_events

    def _should_deliver(self, payload: Dict[str, Any]) -> Tuple[bool, bool, Dict[str, Any]]:
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id:
            return False, False, {}
        conv = self.ensure_conversation_record(
            conversation_id=conversation_id,
            display_name=str(payload.get("display_name") or ""),
            conversation_type=str(payload.get("conversation_type") or ""),
            participant_bots=list(payload.get("participant_bots") or []),
            require_mention=payload.get("require_mention"),
            include_self_participant=False,
        )
        sender_type = str(payload.get("sender_type") or "user").strip().lower() or "user"
        sender_id = str(payload.get("sender_id") or "").strip()
        mentions = [str(item).strip() for item in payload.get("mentions", []) if str(item).strip()]
        mention_to_bot = self.bot_id in mentions
        # Bot-originated messages are user-facing by default and should not wake
        # other bots unless explicitly @mentioned.
        if sender_type == "bot":
            if sender_id == self.bot_id:
                return False, False, conv
            if not mention_to_bot:
                return False, False, conv
        # If the message explicitly @mentions someone, only deliver to mentioned bots.
        if mentions and not mention_to_bot:
            return False, False, conv
        participants = [str(item).strip() for item in conv.get("participant_bots", []) if str(item).strip()]
        if participants and self.bot_id not in participants and not mention_to_bot:
            return False, mention_to_bot, conv
        conversation_type = str(conv.get("conversation_type") or "group")
        require_mention = bool(conv.get("require_mention", self.require_mention_in_group))
        is_group = conversation_type == "group"
        if is_group and require_mention and not mention_to_bot:
            return False, mention_to_bot, conv
        return True, mention_to_bot or (not (is_group and require_mention)), conv

    def poll_events(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for index, payload in self._load_new_bus_events():
            deliver, is_mention_to_bot, conv = self._should_deliver(payload)
            if not deliver:
                continue
            sender_name = str(payload.get("sender_name") or payload.get("sender_id") or "").strip()
            sender_id = str(payload.get("sender_id") or "").strip()
            event_id = str(payload.get("event_id") or f"lwevt_{index}")
            events.append({
                "event_id": f"{event_id}:{self.bot_id}",
                "channel": "localweb",
                "conversation_id": str(conv.get("conversation_id") or payload.get("conversation_id") or ""),
                "conversation_type": str(conv.get("conversation_type") or payload.get("conversation_type") or "group"),
                "display_name": str(conv.get("display_name") or payload.get("display_name") or payload.get("conversation_id") or ""),
                "sender_id": sender_id,
                "sender_name": sender_name or "localweb_user",
                "message_id": event_id,
                "message_text": str(payload.get("message_text") or ""),
                "attachments": payload.get("attachments") or [],
                "timestamp": str(payload.get("timestamp") or now_iso()),
                "is_mention_to_bot": bool(is_mention_to_bot),
                "require_mention": bool(conv.get("require_mention", self.require_mention_in_group)),
            })
        return events

    def send_message(self, conversation_id: str, message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
        result = self.emit_event(
            conversation_id=conversation_id,
            message=message,
            sender_id=self.service.bot_id,
            sender_name=self.service.bot_display_name,
            sender_type="bot",
            attachments=attachments or [],
        )
        if result.get("status") != "success":
            return False, str(result.get("error") or "localweb send failed")
        event = result.get("event") if isinstance(result.get("event"), dict) else {}
        return True, str(event.get("event_id") or "")


class CheapClawService:
    def __init__(
        self,
        *,
        user_data_root: str,
        llm_config_path: Optional[str] = None,
        bot_id: str = "",
        bot_display_name: str = "",
        fleet_config_path: Optional[str] = None,
        default_agent_system: str = "CheapClawWorkerGeneral",
        default_agent_name: str = "worker_agent",
        supervisor_agent_system: str = "CheapClawSupervisor",
        supervisor_agent_name: str = "supervisor_agent",
        tools_dir: Optional[str] = None,
        skills_dir: Optional[str] = None,
        history_preview_limit: int = 50,
        watchdog_interval_sec: Optional[int] = None,
    ):
        global ACTIVE_SERVICE
        resolved_user_root = Path(user_data_root).expanduser().resolve()
        self.paths = CheapClawPaths.from_user_data_root(resolved_user_root)
        cheapclaw_cfg = _ensure_cheapclaw_app_config(self.paths)
        _sync_root_app_config_from_cheapclaw(resolved_user_root, cheapclaw_cfg)
        cheapclaw_settings = _extract_cheapclaw_settings(cheapclaw_cfg)
        self.bot_id = str(bot_id or resolved_user_root.name or "cheapclaw-bot").strip() or "cheapclaw-bot"
        self.bot_display_name = str(bot_display_name or self.bot_id).strip() or self.bot_id
        self.fleet_config_path = str(Path(fleet_config_path).expanduser().resolve()) if fleet_config_path else ""
        # Pin process-level roots to this bot instance to avoid falling back to ~/mla_v3.
        os.environ["CHEAPCLAW_USER_DATA_ROOT"] = str(resolved_user_root)
        os.environ["MLA_USER_DATA_ROOT"] = str(resolved_user_root)
        if self.fleet_config_path:
            os.environ["CHEAPCLAW_FLEET_CONFIG_PATH"] = self.fleet_config_path
        os.environ["CHEAPCLAW_BOT_ID"] = self.bot_id
        runtime_cfg = cheapclaw_cfg.get("runtime", {}) if isinstance(cheapclaw_cfg, dict) else {}
        self.app_tools_dir = str((Path(tools_dir).expanduser().resolve()) if tools_dir else self.paths.tools_root.resolve())
        if tools_dir is None:
            self._sync_runtime_tools(force=False)
        self.sdk: InfiAgent = infiagent(
            user_data_root=str(resolved_user_root),
            llm_config_path=llm_config_path,
            default_agent_system=default_agent_system,
            default_agent_name=default_agent_name,
            tools_dir=self.app_tools_dir,
            skills_dir=skills_dir,
            action_window_steps=runtime_cfg.get("action_window_steps"),
            thinking_interval=runtime_cfg.get("thinking_interval"),
            fresh_enabled=runtime_cfg.get("fresh_enabled"),
            fresh_interval_sec=runtime_cfg.get("fresh_interval_sec"),
            seed_builtin_resources=False,
        )
        runtime = self.sdk.describe_runtime()
        self.runtime = runtime
        global SERVICE_LOG_PATH
        SERVICE_LOG_PATH = Path(runtime["logs_dir"]) / cheapclaw_settings["service_log_file"]
        _configure_service_logger(
            SERVICE_LOG_PATH,
            max_bytes=int(cheapclaw_settings.get("service_log_max_bytes", SERVICE_LOG_MAX_BYTES)),
            backup_count=int(cheapclaw_settings.get("service_log_backup_count", SERVICE_LOG_BACKUP_COUNT)),
        )
        self.panel_store = CheapClawPanelStore(self.paths, history_preview_limit=history_preview_limit)
        self.default_agent_system = default_agent_system
        self.default_agent_name = default_agent_name
        self.supervisor_agent_system = supervisor_agent_system
        self.supervisor_agent_name = supervisor_agent_name
        self.default_exposed_skills = cheapclaw_settings["default_exposed_skills"]
        self.default_mcp_servers = cheapclaw_settings["default_mcp_servers"]
        self.asset_agent_library_root = ASSET_AGENT_LIBRARY_ROOT.resolve()
        self.asset_channels_example_path = ASSET_CHANNELS_EXAMPLE_PATH.resolve()
        self.app_skills_root = APP_SKILLS_ROOT.resolve()
        self._supervisor_lock = threading.Lock()
        self.watchdog_interval_sec = max(60, int(watchdog_interval_sec or cheapclaw_settings["watchdog_interval_sec"]))
        self.adapters: Dict[str, ChannelAdapter] = {}
        self.bootstrap_assets(force=False)
        self._ensure_monitor_task_files()
        self.reload_adapters()
        self._register_signal_handlers()
        state = self.load_runtime_state()
        state["bot"] = {
            "bot_id": self.bot_id,
            "display_name": self.bot_display_name,
            "fleet_config_path": self.fleet_config_path,
            "pid": os.getpid(),
        }
        self.save_runtime_state(state)
        ACTIVE_SERVICE = self
        _log(
            f"service initialized: bot_id={self.bot_id} user_data_root={runtime['user_data_root']} "
            f"watchdog_interval_sec={self.watchdog_interval_sec}"
        )

    @contextmanager
    def _runtime_scope(self):
        with self.sdk._runtime_scope():
            yield

    @staticmethod
    def _extract_unregistered_tool_names(run_result: Dict[str, Any]) -> List[str]:
        if not isinstance(run_result, dict):
            return []
        names: List[str] = []
        history = run_result.get("action_history")
        if not isinstance(history, list):
            return names
        for item in history:
            if not isinstance(item, dict):
                continue
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            error_text = str(result.get("error_information") or "")
            marker = "工具未注册到运行时:"
            if marker not in error_text:
                continue
            tool_name = error_text.split(marker, 1)[-1].strip()
            if tool_name:
                names.append(tool_name)
        # 去重并保持首次出现顺序
        seen = set()
        deduped: List[str] = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            deduped.append(name)
        return deduped

    def _expected_cheapclaw_tool_names(self) -> List[str]:
        names: List[str] = []
        if not APP_TOOLS_ROOT.exists():
            return names
        for item in sorted(APP_TOOLS_ROOT.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_dir():
                continue
            name = str(item.name or "").strip()
            if not name:
                continue
            if name.startswith("cheapclaw_") and (item / f"{name}.py").exists():
                names.append(name)
        return names

    def _preflight_supervisor_tools(self) -> None:
        try:
            from tool_server_lite.registry import get_runtime_registry
        except Exception as exc:
            _log(f"supervisor tool preflight skipped: cannot import runtime registry: {exc}")
            return

        expected = self._expected_cheapclaw_tool_names()
        if not expected:
            return

        registry = get_runtime_registry(force_reload=True)
        missing = sorted([name for name in expected if name not in registry])
        if not missing:
            return

        _log(
            "supervisor tool preflight detected missing tools: "
            + ", ".join(missing)
            + "; forcing runtime tool resync"
        )
        try:
            self.bootstrap_assets(force=True)
            self._sync_runtime_tools(force=True)
        except Exception as exc:
            _log(f"supervisor tool preflight resync failed: {exc}")
            return

        registry = get_runtime_registry(force_reload=True)
        still_missing = sorted([name for name in expected if name not in registry])
        if still_missing:
            _log("supervisor tool preflight still missing tools: " + ", ".join(still_missing))
        else:
            _log("supervisor tool preflight passed after runtime tool resync")

    def _recover_from_unregistered_tool_error(self, run_result: Dict[str, Any]) -> None:
        names = self._extract_unregistered_tool_names(run_result)
        if not names:
            return
        _log(f"detected unregistered supervisor tools: {', '.join(names)}; attempting runtime resync")
        try:
            self.bootstrap_assets(force=True)
            self._sync_runtime_tools(force=True)
            _log("runtime resync completed after unregistered-tool anomaly")
        except Exception as exc:
            _log(f"runtime resync failed after unregistered-tool anomaly: {exc}")

    def _sync_runtime_tools(self, force: bool = False) -> List[str]:
        target_root = Path(self.app_tools_dir).expanduser().resolve()
        if target_root != self.paths.tools_root.resolve():
            return []
        target_root.mkdir(parents=True, exist_ok=True)
        source_items = []
        for item in sorted(APP_TOOLS_ROOT.iterdir(), key=lambda entry: entry.name.lower()):
            if item.is_dir():
                has_tool_script = any(
                    child.is_file()
                    and child.suffix == ".py"
                    and child.name != "__init__.py"
                    for child in item.iterdir()
                )
                if not has_tool_script:
                    continue
            source_items.append(item)
        source_entries = {item.name for item in source_items}
        # Keep runtime tool set aligned with source tree: remove stale cheapclaw_* tools.
        for existing in list(target_root.iterdir()):
            name = existing.name
            if not name.startswith("cheapclaw_"):
                continue
            if name in source_entries:
                continue
            try:
                if existing.is_dir():
                    shutil.rmtree(existing)
                else:
                    existing.unlink()
            except FileNotFoundError:
                pass
        copied = []
        for source in source_items:
            destination = target_root / source.name
            if force and destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
            copied.append(str(destination))
        for support_file in ["tool_runtime_helpers.py", "cheapclaw_hooks.py"]:
            source = APP_ROOT / support_file
            if not source.exists():
                continue
            destination = target_root.parent / support_file
            if force and destination.exists():
                destination.unlink()
            shutil.copy2(source, destination)
            copied.append(str(destination))
        return copied

    def bootstrap_assets(self, force: bool = False) -> Dict[str, Any]:
        runtime = self.sdk.describe_runtime()
        user_root = Path(runtime["user_data_root"])
        cheapclaw_cfg = _ensure_cheapclaw_app_config(self.paths)
        _sync_root_app_config_from_cheapclaw(user_root, cheapclaw_cfg)
        agent_root = Path(runtime["agent_library_dir"])
        skills_root = Path(runtime["skills_dir"])
        agent_root.mkdir(parents=True, exist_ok=True)
        skills_root.mkdir(parents=True, exist_ok=True)
        # Keep user-installed agent systems in the runtime directory.
        # We only seed the built-in systems from assets when they are missing.
        # If a runtime folder already exists, treat it as user-owned and do not
        # overwrite it during prepare/reload/bootstrap.

        installed_systems = []
        for system_dir in sorted(self.asset_agent_library_root.iterdir(), key=lambda item: item.name.lower()):
            if not system_dir.is_dir():
                continue
            target = agent_root / system_dir.name
            if target.exists():
                installed_systems.append(str(target))
                continue
            shutil.copytree(system_dir, target)
            installed_systems.append(str(target))

        for skill_dir in sorted(self.app_skills_root.iterdir(), key=lambda item: item.name.lower()) if self.app_skills_root.exists() else []:
            if not skill_dir.is_dir():
                continue
            target = skills_root / skill_dir.name
            if force and target.exists():
                shutil.rmtree(target)
            shutil.copytree(skill_dir, target, dirs_exist_ok=True)

        if force or not self.paths.channels_example_path.exists():
            shutil.copyfile(self.asset_channels_example_path, self.paths.channels_example_path)
        if not self.paths.channels_config_path.exists():
            shutil.copyfile(self.asset_channels_example_path, self.paths.channels_config_path)
        copied_tools = self._sync_runtime_tools(force=force)

        return {
            "status": "success",
            "tools_dir": self.app_tools_dir,
            "copied_tools": copied_tools,
            "installed_agent_systems": installed_systems,
            "supervisor_agent_system": self.supervisor_agent_system,
            "worker_agent_system": self.default_agent_system,
            "app_config_path": str(self.paths.app_config_path),
            "app_config_example_path": str(self.paths.app_config_example_path),
            "channels_config_path": str(self.paths.channels_config_path),
            "channels_example_path": str(self.paths.channels_example_path),
            "skills_root": str(skills_root),
        }

    def describe_runtime(self) -> Dict[str, Any]:
        payload = self.sdk.describe_runtime()
        payload["bot_id"] = self.bot_id
        payload["bot_display_name"] = self.bot_display_name
        payload["fleet_config_path"] = self.fleet_config_path
        return payload

    def _filter_agent_systems_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        agent_root = Path(str(self.runtime.get("agent_library_dir") or "")).expanduser().resolve()
        allowed_names = set()
        if agent_root.exists():
            allowed_names = {item.name for item in agent_root.iterdir() if item.is_dir()}
        items = payload.get("agent_systems")
        if not isinstance(items, list):
            items = []
        filtered = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name and name in allowed_names:
                filtered.append(item)
        out = dict(payload or {})
        out["status"] = str(out.get("status") or "success")
        out["agent_systems"] = filtered
        out["runtime_agent_library_dir"] = str(agent_root)
        out["visible_agent_system_names"] = [
            str(item.get("name") or "") for item in filtered if str(item.get("name") or "")
        ]
        return out

    def list_agent_systems(self) -> Dict[str, Any]:
        return self._filter_agent_systems_payload(self.sdk.list_agent_systems())

    def list_global_skills(self) -> Dict[str, Any]:
        return {"status": "success", "skills_root": self.runtime["skills_dir"], "skills": list_global_skills(self.runtime["skills_dir"])}

    def _find_task_entry(self, task_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        target_task_id = str(Path(task_id).expanduser().resolve())
        panel = self.panel_store.load_panel()
        for channel_payload in panel.get("channels", {}).values():
            for conv in channel_payload.get("conversations", {}).values():
                for item in conv.get("linked_tasks", []):
                    if str(item.get("task_id") or "") == target_task_id:
                        return conv, item
        return None, None

    def get_task_preferences(self, *, task_id: str) -> Dict[str, Any]:
        _, item = self._find_task_entry(task_id)
        if item is None:
            return {
                "status": "success",
                "task_id": str(Path(task_id).expanduser().resolve()),
                "default_exposed_skills": list(self.default_exposed_skills),
                "mcp_servers": list(self.default_mcp_servers),
            }
        return {
            "status": "success",
            "task_id": str(Path(task_id).expanduser().resolve()),
            "default_exposed_skills": list(item.get("default_exposed_skills") or self.default_exposed_skills),
            "mcp_servers": list(item.get("mcp_servers") or self.default_mcp_servers),
        }

    def update_task_preferences(
        self,
        *,
        task_id: str,
        default_exposed_skills: Optional[Iterable[str]] = None,
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        with self._runtime_scope():
            try:
                resolved_task_id = self._ensure_managed_task_id(task_id)
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            conv, item = self._find_task_entry(resolved_task_id)
            if conv is None or item is None:
                return {"status": "error", "error": f"task_id not found in panel: {resolved_task_id}"}

            task_patch: Dict[str, Any] = {"updated_at": now_iso()}
            if default_exposed_skills is not None:
                selected_skills = [str(name).strip() for name in default_exposed_skills if str(name).strip()]
                set_task_visible_skills(resolved_task_id, selected_skills)
                task_patch["default_exposed_skills"] = selected_skills
                task_patch["skills_dir"] = ""
            if mcp_servers is not None:
                task_patch["mcp_servers"] = [entry for entry in mcp_servers if isinstance(entry, dict)]

            panel = update_conversation_task(
                str(conv.get("channel") or ""),
                str(conv.get("conversation_id") or ""),
                resolved_task_id,
                task_patch,
            )
            updated_item = next(
                (linked for linked in panel.get("channels", {}).get(str(conv.get("channel") or ""), {}).get("conversations", {}).get(str(conv.get("conversation_id") or ""), {}).get("linked_tasks", []) if linked.get("task_id") == resolved_task_id),
                {},
            )
            return {
                "status": "success",
                "task_id": resolved_task_id,
                "default_exposed_skills": list(updated_item.get("default_exposed_skills") or self.default_exposed_skills),
                "mcp_servers": list(updated_item.get("mcp_servers") or self.default_mcp_servers),
                "skills_dir": str(updated_item.get("skills_dir") or ""),
            }

    def get_task_system_add(self, *, task_id: str) -> Dict[str, Any]:
        with self._runtime_scope():
            try:
                resolved_task_id = self._ensure_managed_task_id(task_id, allow_monitor_task=True)
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            path = Path(resolved_task_id) / "system-add.md"
            content = ""
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="ignore")
            return {
                "status": "success",
                "task_id": resolved_task_id,
                "path": str(path),
                "content": content,
            }

    def update_task_system_add(self, *, task_id: str, content: str) -> Dict[str, Any]:
        with self._runtime_scope():
            try:
                resolved_task_id = self._ensure_managed_task_id(task_id, allow_monitor_task=True)
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            task_path = Path(resolved_task_id)
            task_path.mkdir(parents=True, exist_ok=True)
            path = task_path / "system-add.md"
            text = str(content or "")
            _atomic_write_text(path, text)
            return {
                "status": "success",
                "task_id": resolved_task_id,
                "path": str(path),
                "bytes": len(text.encode("utf-8")),
            }

    def load_runtime_state(self) -> Dict[str, Any]:
        self.paths.runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
        return _load_json(self.paths.runtime_state_path, {"webhook_server": {}, "telegram_offsets": {}})

    def save_runtime_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _atomic_write_text(self.paths.runtime_state_path, json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    def load_channel_config(self) -> Dict[str, Any]:
        return _load_json(self.paths.channels_config_path, _load_json(self.paths.channels_example_path, {}))

    def reload_adapters(self) -> Dict[str, ChannelAdapter]:
        config = self.load_channel_config()
        adapters: Dict[str, ChannelAdapter] = {}
        if config.get("telegram", {}).get("enabled"):
            adapters["telegram"] = TelegramAdapter(config.get("telegram", {}), self)
        if config.get("feishu", {}).get("enabled"):
            adapters["feishu"] = FeishuAdapter(config.get("feishu", {}), self)
        if config.get("whatsapp", {}).get("enabled"):
            adapters["whatsapp"] = WhatsAppCloudAdapter(config.get("whatsapp", {}), self)
        if config.get("discord", {}).get("enabled"):
            adapters["discord"] = DiscordAdapter(config.get("discord", {}), self)
        if config.get("qq", {}).get("enabled"):
            adapters["qq"] = OneBotV11Adapter(config.get("qq", {}), self, channel_name="qq")
        if config.get("wechat", {}).get("enabled"):
            adapters["wechat"] = OneBotV11Adapter(config.get("wechat", {}), self, channel_name="wechat")
        if config.get("localweb", {}).get("enabled"):
            adapters["localweb"] = LocalWebAdapter(config.get("localweb", {}), self)
        self.adapters = adapters
        return adapters

    def _managed_task_counts(self) -> Tuple[int, int]:
        panel = self.panel_store.load_panel()
        managed = 0
        running = 0
        for channel_payload in panel.get("channels", {}).values():
            for conv in channel_payload.get("conversations", {}).values():
                for item in conv.get("linked_tasks", []):
                    if not isinstance(item, dict):
                        continue
                    task_id = str(item.get("task_id") or "").strip()
                    if not task_id:
                        continue
                    managed += 1
                    if str(item.get("status") or "") == "running":
                        running += 1
        return managed, running

    def _ensure_managed_task_id(self, task_id: str, *, allow_monitor_task: bool = False) -> str:
        try:
            return assert_managed_task_id(task_id, allow_monitor_task=allow_monitor_task)
        except ValueError as exc:
            raise ValueError(str(exc))

    def _supervisor_running_marker_path(self) -> Path:
        task_id = str(self.paths.supervisor_task_id)
        task_hash = hashlib.md5(task_id.encode("utf-8")).hexdigest()[:8]
        task_name = Path(task_id).name or "task"
        return self.paths.user_data_root / "runtime" / "running_tasks" / f"{task_hash}_{task_name}.json"

    def _supervisor_running_marker_healthy(self) -> bool:
        marker_path = self._supervisor_running_marker_path()
        marker = _load_json(marker_path, {})
        if not isinstance(marker, dict):
            return False
        pid = int(marker.get("pid") or 0)
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

        # 防止 PID 复用导致“误判运行中”：校验命令行里包含当前 supervisor task_id。
        try:
            proc = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except Exception:
            # 无法读取命令行时，保持保守：把它当作运行中。
            return True
        if proc.returncode != 0:
            return False
        command = str(proc.stdout or "").strip()
        if not command:
            return False
        return str(self.paths.supervisor_task_id) in command

    def _clear_stale_supervisor_running_state(self, *, reason: str) -> None:
        marker_path = self._supervisor_running_marker_path()
        try:
            if marker_path.exists():
                marker_path.unlink()
        except Exception:
            pass
        try:
            panel = self.panel_store.load_panel()
            service_state = panel.setdefault("service_state", {})
            if service_state.get("main_agent_running"):
                service_state["main_agent_running"] = False
                service_state["main_agent_last_finished_at"] = now_iso()
                self.panel_store.save_panel(panel)
        except Exception:
            pass
        _log(f"supervisor running state corrected: {reason}")

    def _monitor_running(self) -> bool:
        try:
            snapshot = self.sdk.task_snapshot(task_id=str(self.paths.supervisor_task_id))
            running = bool(snapshot.get("running"))
            if not running:
                return False
            if self._supervisor_running_marker_healthy():
                return True
            self._clear_stale_supervisor_running_state(reason="sdk snapshot says running but marker is stale/mismatched")
            return False
        except Exception as exc:
            panel = self.panel_store.load_panel()
            panel_running = bool(panel.get("service_state", {}).get("main_agent_running"))
            if panel_running and self._supervisor_lock.locked():
                return True
            if panel_running and self._supervisor_running_marker_healthy():
                return True
            if panel_running:
                self._clear_stale_supervisor_running_state(reason=f"sdk snapshot failed: {exc}")
            return False

    def _pending_monitor_instruction_count(self) -> int:
        with self._runtime_scope():
            return pending_monitor_instruction_count()

    def _trigger_monitor_if_idle_async(self, reason: str) -> None:
        if self._monitor_running() or self._pending_monitor_instruction_count() <= 0:
            return

        def _runner() -> None:
            try:
                result = self.run_supervisor_once(reason=reason)
                if isinstance(result, dict) and result.get("status") == "success":
                    self.process_outbox()
            except Exception as exc:
                _log(f"async monitor trigger failed: {exc}")

        thread = threading.Thread(target=_runner, daemon=True, name=f"cheapclaw-monitor-{self.bot_id}")
        thread.start()

    def _ensure_monitor_task_files(self) -> None:
        self.paths.supervisor_task_id.mkdir(parents=True, exist_ok=True)
        peer_bots = list_peer_bots(current_bot_id=self.bot_id, path=self.fleet_config_path)
        peer_lines = ["- (none)"] if not peer_bots else [
            f"- {item.get('bot_id')}: {item.get('display_name')} @ {item.get('user_data_root')}"
            for item in peer_bots
        ]
        system_add = f"""你是当前 bot 的监控 agent。

你的任务目录说明：
- 当前 bot_id: {self.bot_id}
- 当前 bot 显示名称: {self.bot_display_name}
- 当前监控 task_id: {self.paths.supervisor_task_id}
- 待处理 instruction 队列文件: {self.paths.monitor_instructions_path}
- 会话历史目录: {self.paths.channels_root}
- CheapClaw worker 任务根目录: {self.paths.tasks_root}

当前可见其他 bots:
{chr(10).join(peer_lines)}

工作规则：
1. 优先处理 monitor instruction 队列中的 pending 项。
2. 当你已经完成某条 instruction 的回复、派工、续跑、转发、或状态处理后，必须调用 resolve instruction 工具标记完成。
3. 只有当 pending instruction 全部清空后，你本轮才算真正处理完毕。
4. 你只能操作当前 bot 自己的 task_id；任何越界 task_id 都视为非法。
"""
        existing = ""
        if self.paths.monitor_system_add_path.exists():
            existing = self.paths.monitor_system_add_path.read_text(encoding="utf-8", errors="ignore")
        merged = _upsert_marked_block(
            existing,
            start_tag=CHEAPCLAW_SYSTEM_BLOCK_START,
            end_tag=CHEAPCLAW_SYSTEM_BLOCK_END,
            block_text=system_add,
        )
        _atomic_write_text(self.paths.monitor_system_add_path, merged)

    def _register_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            signal.signal(signal.SIGUSR1, lambda *_args: self._trigger_monitor_if_idle_async(reason="signal"))
        except Exception:
            pass

    def _queue_monitor_instruction(self, *, instruction_type: str, summary: str, channel: str = "", conversation_id: str = "", source_message_id: str = "", task_id: str = "", sender_name: str = "", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry = create_monitor_instruction(
            instruction_type=instruction_type,
            summary=summary,
            channel=channel,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            task_id=task_id,
            sender_name=sender_name,
            payload=payload or {},
        )
        managed_count, running_count = self._managed_task_counts()
        if self._monitor_running():
            message = format_monitor_instruction_message(entry, managed_task_count=managed_count, running_task_count=running_count)
            result = self.add_task_message(
                task_id=str(self.paths.supervisor_task_id),
                message=message,
                source="system",
                resume_if_needed=True,
                agent_system=self.supervisor_agent_system,
            )
            if result.get("status") == "success" and (result.get("running") or result.get("resumed")):
                entry["injected_into_monitor"] = True
            else:
                entry["injected_into_monitor"] = False
                self._trigger_monitor_if_idle_async(reason="monitor_instruction_queue")
        else:
            self._trigger_monitor_if_idle_async(reason="monitor_instruction_queue")
        return entry

    @staticmethod
    def _timestamps_equivalent(left: str, right: str, tolerance_sec: float = 1.0) -> bool:
        left_text = str(left or "").strip()
        right_text = str(right or "").strip()
        if left_text == right_text:
            return True
        left_dt = parse_iso(left_text)
        right_dt = parse_iso(right_text)
        if left_dt is None or right_dt is None:
            return False
        return abs((left_dt - right_dt).total_seconds()) <= max(0.0, float(tolerance_sec))

    def _task_result_instruction_exists(self, *, task_id: str, output_at: str, summary: str) -> bool:
        try:
            queue = load_monitor_instructions_for_root(self.paths.user_data_root)
        except Exception:
            return False
        instructions = queue.get("instructions", []) if isinstance(queue, dict) else []
        target_task_id = str(task_id or "").strip()
        target_output_at = str(output_at or "").strip()
        target_summary = str(summary or "")
        for item in instructions:
            if not isinstance(item, dict):
                continue
            if str(item.get("instruction_type") or "") != "TaskResult":
                continue
            if str(item.get("task_id") or "").strip() != target_task_id:
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if str(payload.get("event_type") or "task_final_output") != "task_final_output":
                continue
            existing_output_at = str(payload.get("output_at") or "").strip()
            if target_output_at and existing_output_at:
                if self._timestamps_equivalent(existing_output_at, target_output_at):
                    return True
            elif not target_output_at and not existing_output_at:
                if str(item.get("summary") or "") == target_summary:
                    return True
        return False

    def _pending_monitor_instructions(self, limit: int = 100) -> List[Dict[str, Any]]:
        return list_monitor_instructions(status="pending", limit=limit)

    def ingest_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        with self._runtime_scope():
            panel = self.panel_store.record_social_message(**event)
            text = str(event.get("message_text") or "").strip()
            attachments = event.get("attachments") or []
            summary = text or (f"收到 {len(attachments)} 个附件" if attachments else "(empty message)")
            self._queue_monitor_instruction(
                instruction_type="UserMessage",
                summary=summary,
                channel=str(event.get("channel") or ""),
                conversation_id=str(event.get("conversation_id") or ""),
                source_message_id=str(event.get("message_id") or ""),
                sender_name=str(event.get("sender_name") or ""),
                payload={
                    "conversation_type": str(event.get("conversation_type") or ""),
                    "attachments": attachments,
                    "is_mention_to_bot": bool(event.get("is_mention_to_bot", False)),
                },
            )
            return panel

    def build_task_id(self, *, channel: str, conversation_id: str, task_name: str) -> str:
        with self._runtime_scope():
            return generate_task_id(channel, conversation_id, task_name)

    def build_task_skills_overlay(self, *, task_id: str, exposed_skills: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        with self._runtime_scope():
            try:
                resolved_task_id = self._ensure_managed_task_id(task_id)
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            payload = set_task_visible_skills(resolved_task_id, exposed_skills or [])
        return {"status": "success", **payload}

    def record_social_message(self, **kwargs) -> Dict[str, Any]:
        with self._runtime_scope():
            return self.panel_store.record_social_message(**kwargs)

    def start_task(self, **kwargs) -> Dict[str, Any]:
        with self._runtime_scope():
            channel = str(kwargs.get("channel") or "").strip()
            conversation_id = str(kwargs.get("conversation_id") or "").strip()
            task_name = str(kwargs.get("task_name") or "").strip()
            user_input = str(kwargs.get("user_input") or "").strip()
            if not channel or not conversation_id or not task_name or not user_input:
                return {"status": "error", "error": "channel, conversation_id, task_name and user_input are required"}
            try:
                resolved_task_id = self._ensure_managed_task_id(
                    kwargs.get("task_id") or self.build_task_id(channel=channel, conversation_id=conversation_id, task_name=task_name)
                )
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            requested_agent_system = str(kwargs.get("agent_system") or self.default_agent_system).strip() or self.default_agent_system
            requested_agent_name = str(kwargs.get("agent_name") or "").strip()
            if requested_agent_system == self.supervisor_agent_system:
                return {"status": "error", "error": f"禁止使用 {self.supervisor_agent_system} 启动后台任务，主 agent 不能调用本身。"}
            systems_payload = self.list_agent_systems()
            systems = {
                str(item.get("name") or ""): item
                for item in systems_payload.get("agent_systems", [])
                if isinstance(item, dict)
            }
            system_info = systems.get(requested_agent_system, {})
            available_agent_names = [str(name).strip() for name in system_info.get("agent_names", []) if str(name).strip()]
            if requested_agent_system == "CheapClawWorkerGeneral":
                selected_agent_name = "worker_agent"
            elif requested_agent_name and requested_agent_name in available_agent_names:
                selected_agent_name = requested_agent_name
            elif available_agent_names:
                selected_agent_name = available_agent_names[0]
            else:
                selected_agent_name = requested_agent_name or self.default_agent_name
            merged_config = dict(kwargs.get("config") or {})
            merged_config.setdefault("tools_dir", self.app_tools_dir)
            exposed_skills = kwargs.get("exposed_skills")
            task_preferences = self.get_task_preferences(task_id=resolved_task_id)
            if exposed_skills is None:
                exposed_skills = list(task_preferences.get("default_exposed_skills") or self.default_exposed_skills)
            else:
                merged_visible = []
                for item in list(task_preferences.get("default_exposed_skills") or self.default_exposed_skills) + list(exposed_skills or []):
                    name = str(item).strip()
                    if name and name not in merged_visible:
                        merged_visible.append(name)
                exposed_skills = merged_visible
            if "mcp_servers" not in merged_config:
                merged_config["mcp_servers"] = list(task_preferences.get("mcp_servers") or self.default_mcp_servers)
            if exposed_skills is not None:
                merged_config["visible_skills"] = list(exposed_skills)
            existing_hooks = list(merged_config.get("tool_hooks") or [])
            if not any(str(item.get("callback") or "") == FINAL_OUTPUT_HOOK_CALLBACK for item in existing_hooks if isinstance(item, dict)):
                existing_hooks.append({
                    "name": "cheapclaw-final-output",
                    "callback": FINAL_OUTPUT_HOOK_CALLBACK,
                    "when": "after",
                    "tool_names": ["final_output"],
                    "include_arguments": False,
                    "include_result": True,
                })
            merged_config["tool_hooks"] = existing_hooks
            dispatch_input = f"{user_input}\n\n[dispatched_at {now_iso()}]"
            result = self.sdk.start_background_task(
                task_id=resolved_task_id,
                user_input=dispatch_input,
                agent_system=requested_agent_system,
                agent_name=selected_agent_name,
                force_new=bool(kwargs.get("force_new", False)),
                config=merged_config or None,
            )
            if result.get("status") != "success":
                return result
            snapshot = self.sdk.task_snapshot(task_id=resolved_task_id)
            set_task_visible_skills(resolved_task_id, exposed_skills or [])
            latest_instruction = snapshot.get("latest_instruction") or {}
            if not isinstance(latest_instruction, dict):
                latest_instruction = {}
            update_conversation_task(
                channel,
                conversation_id,
                resolved_task_id,
                {
                    "agent_system": result.get("agent_system") or requested_agent_system,
                    "agent_name": result.get("agent_name") or selected_agent_name,
                    "status": "running",
                    "share_context_path": snapshot.get("share_context_path", ""),
                    "stack_path": snapshot.get("stack_path", ""),
                    "log_path": result.get("log_path", ""),
                    "skills_dir": "",
                    "default_exposed_skills": list(exposed_skills or []),
                    "mcp_servers": list(merged_config.get("mcp_servers") or []),
                    "last_thinking": snapshot.get("latest_thinking", ""),
                    "last_thinking_at": snapshot.get("latest_thinking_at", ""),
                    "last_final_output": snapshot.get("last_final_output", ""),
                    "last_final_output_at": snapshot.get("last_final_output_at", ""),
                    "last_action_at": snapshot.get("last_updated", ""),
                    "last_log_at": now_iso(),
                    "last_launch_at": now_iso(),
                    "last_watchdog_note": "task launched",
                    "user_input": str((snapshot.get("runtime") or {}).get("user_input") or dispatch_input or ""),
                    "latest_instruction": str(latest_instruction.get("instruction") or dispatch_input or ""),
                    "created_at": next(
                        (
                            str(existing.get("created_at") or "")
                            for existing in self.panel_store.load_panel().get("channels", {}).get(channel, {}).get("conversations", {}).get(conversation_id, {}).get("linked_tasks", [])
                            if existing.get("task_id") == resolved_task_id
                        ),
                        now_iso(),
                    ) or now_iso(),
                },
            )
            source_message_ids = kwargs.get("source_message_ids") or []
            if source_message_ids:
                bind_messages_to_task(
                    channel,
                    conversation_id,
                    resolved_task_id,
                    source_message_ids,
                    note="task started from supervisor decision",
                )
            return {
                **result,
                "requested_agent_name": requested_agent_name,
                "selected_agent_name": selected_agent_name,
                "available_agent_names": available_agent_names,
                "share_context_path": snapshot.get("share_context_path", ""),
                "stack_path": snapshot.get("stack_path", ""),
            }

    def add_task_message(
        self,
        *,
        task_id: str,
        message: str,
        source: str = "agent",
        resume_if_needed: bool = False,
        agent_system: Optional[str] = None,
        channel: Optional[str] = None,
        conversation_id: Optional[str] = None,
        source_message_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        with self._runtime_scope():
            try:
                resolved_task_id = self._ensure_managed_task_id(task_id, allow_monitor_task=True)
            except ValueError as exc:
                return {"status": "error", "error": str(exc), "output": ""}
            timestamped = f"{message}\n\n[message_appended_at {now_iso()}]"
            result = self.sdk.add_message(timestamped, task_id=resolved_task_id, source=source, resume_if_needed=resume_if_needed, agent_system=agent_system)
            if result.get("status") == "success" and channel and conversation_id and source_message_ids:
                bind_messages_to_task(
                    str(channel),
                    str(conversation_id),
                    resolved_task_id,
                    source_message_ids,
                    note="message appended to existing task",
                )
            return result

    def fresh_task(self, *, task_id: str, reason: str = "") -> Dict[str, Any]:
        with self._runtime_scope():
            try:
                resolved_task_id = self._ensure_managed_task_id(task_id)
            except ValueError as exc:
                return {"status": "error", "error": str(exc), "output": ""}
            return self.sdk.fresh(task_id=resolved_task_id, reason=reason)

    def reset_task(self, *, task_id: str, preserve_history: bool = True, kill_background_processes: bool = True, reason: str = "") -> Dict[str, Any]:
        with self._runtime_scope():
            try:
                resolved_task_id = self._ensure_managed_task_id(task_id)
            except ValueError as exc:
                return {"status": "error", "error": str(exc), "output": ""}
            return self.sdk.reset_task(task_id=resolved_task_id, preserve_history=preserve_history, kill_background_processes=kill_background_processes, reason=reason)

    @staticmethod
    def _iso_timestamp(value: Any) -> float:
        parsed = parse_iso(str(value or ""))
        if parsed is None:
            return 0.0
        return parsed.timestamp()

    def _latest_root_final_output_from_share(self, share_context_path: str) -> Dict[str, Any]:
        path = Path(str(share_context_path or "")).expanduser()
        if not path.exists():
            return {"resolved": False}
        payload = _load_json(path, {})
        if not isinstance(payload, dict):
            return {"resolved": False}

        records: List[Dict[str, Any]] = []

        def _collect_from_entry(entry: Dict[str, Any], *, source: str, sequence: int) -> None:
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
            roots.sort(
                key=lambda item: (
                    self._iso_timestamp(item[1].get("start_time")),
                    self._iso_timestamp(item[1].get("end_time")),
                )
            )
            root_agent_id, root_agent = roots[-1]
            run_start_at = str(entry.get("start_time") or root_agent.get("start_time") or "")
            run_completed_at = str(entry.get("completion_time") or root_agent.get("end_time") or "")
            final_output = str(root_agent.get("final_output") or "")
            final_output_at = str(root_agent.get("end_time") or run_completed_at or "")
            records.append(
                {
                    "source": source,
                    "sequence": sequence,
                    "run_start_at": run_start_at,
                    "run_completed_at": run_completed_at,
                    "final_output": final_output,
                    "final_output_at": final_output_at,
                    "root_agent_id": root_agent_id,
                    "root_agent_name": str(root_agent.get("agent_name") or ""),
                    "run_start_ts": self._iso_timestamp(run_start_at),
                    "final_output_ts": self._iso_timestamp(final_output_at),
                }
            )

        history = payload.get("history", [])
        if isinstance(history, list):
            for idx, entry in enumerate(history):
                if isinstance(entry, dict):
                    _collect_from_entry(entry, source="history", sequence=idx)

        current = payload.get("current", {})
        if isinstance(current, dict):
            _collect_from_entry(current, source="current", sequence=len(records))

        if not records:
            return {
                "resolved": True,
                "final_output": "",
                "final_output_at": "",
                "root_agent_id": "",
                "root_agent_name": "",
                "source": "",
            }

        records_with_output = [item for item in records if str(item.get("final_output") or "").strip()]
        if not records_with_output:
            return {
                "resolved": True,
                "final_output": "",
                "final_output_at": "",
                "root_agent_id": "",
                "root_agent_name": "",
                "source": "",
            }

        latest = max(
            records_with_output,
            key=lambda item: (
                float(item.get("run_start_ts") or 0.0),
                float(item.get("final_output_ts") or 0.0),
                int(item.get("sequence") or 0),
            ),
        )
        return {
            "resolved": True,
            "final_output": str(latest.get("final_output") or ""),
            "final_output_at": str(latest.get("final_output_at") or ""),
            "root_agent_id": str(latest.get("root_agent_id") or ""),
            "root_agent_name": str(latest.get("root_agent_name") or ""),
            "source": str(latest.get("source") or ""),
        }

    def get_task_snapshot(self, *, task_id: str) -> Dict[str, Any]:
        with self._runtime_scope():
            try:
                resolved_task_id = self._ensure_managed_task_id(task_id, allow_monitor_task=True)
            except ValueError as exc:
                return {"status": "error", "error": str(exc), "output": ""}
            snapshot = self.sdk.task_snapshot(task_id=resolved_task_id)
            root_final = self._latest_root_final_output_from_share(str(snapshot.get("share_context_path") or ""))
            if root_final.get("resolved"):
                snapshot["last_final_output"] = str(root_final.get("final_output") or "")
                snapshot["last_final_output_at"] = str(root_final.get("final_output_at") or "")
                snapshot["last_final_output_root_agent_id"] = str(root_final.get("root_agent_id") or "")
                snapshot["last_final_output_root_agent_name"] = str(root_final.get("root_agent_name") or "")
                snapshot["last_final_output_source"] = str(root_final.get("source") or "")
            panel = self.panel_store.load_panel()
            for channel_payload in panel.get("channels", {}).values():
                for conv in channel_payload.get("conversations", {}).values():
                    for item in conv.get("linked_tasks", []):
                        if item.get("task_id") == snapshot["task_id"]:
                            snapshot["log_path"] = item.get("log_path", "")
                            snapshot["conversation"] = {"channel": conv.get("channel"), "conversation_id": conv.get("conversation_id"), "display_name": conv.get("display_name")}
                            return snapshot
            snapshot.setdefault("log_path", "")
            return snapshot

    def refresh_task_view(self, *, channel: str, conversation_id: str, task_id: str) -> Dict[str, Any]:
        snapshot = self.get_task_snapshot(task_id=task_id)
        patch = {
            "status": "running" if snapshot.get("running") else "idle",
            "share_context_path": snapshot.get("share_context_path", ""),
            "stack_path": snapshot.get("stack_path", ""),
            "last_thinking": snapshot.get("latest_thinking", ""),
            "last_thinking_at": snapshot.get("latest_thinking_at", ""),
            "last_final_output": snapshot.get("last_final_output", ""),
            "last_final_output_at": snapshot.get("last_final_output_at", ""),
            "last_action_at": snapshot.get("last_updated", ""),
            "last_log_at": now_iso(),
        }
        return update_conversation_task(channel, conversation_id, str(Path(task_id).expanduser().resolve()), patch)

    def process_task_events(self) -> List[Dict[str, Any]]:
        with self._runtime_scope():
            panel = self.panel_store.load_panel()
            results: List[Dict[str, Any]] = []
            changed = False
            for event in list_task_events():
                event_id = str(event.get("event_id") or "")
                event_type = str(event.get("event_type") or "")
                task_id = str(event.get("task_id") or "")
                observed_at = str(event.get("observed_at") or now_iso())
                matched = False

                if event_type == "task_final_output" and task_id:
                    for channel_payload in panel.get("channels", {}).values():
                        for conv in channel_payload.get("conversations", {}).values():
                            for item in conv.get("linked_tasks", []):
                                if item.get("task_id") != task_id:
                                    continue
                                previous_final_at = str(item.get("last_final_output_at") or "")
                                previous_output = str(item.get("last_final_output") or "")
                                output_text = str(event.get("output") or "")
                                item.update({
                                    "status": "idle",
                                    "last_final_output": output_text,
                                    "last_final_output_at": observed_at,
                                    "last_action_at": observed_at,
                                    "pid_alive": False,
                                    "watchdog_observation": "",
                                    "watchdog_suspected_state": "healthy",
                                })
                                already_queued = self._task_result_instruction_exists(
                                    task_id=task_id,
                                    output_at=observed_at,
                                    summary=output_text,
                                )
                                final_time_changed = not self._timestamps_equivalent(previous_final_at, observed_at)
                                if (final_time_changed or previous_output != output_text) and not already_queued:
                                    conv.setdefault("pending_events", []).append({
                                        "type": "task_completed",
                                        "task_id": task_id,
                                        "timestamp": observed_at,
                                    })
                                    self._queue_monitor_instruction(
                                        instruction_type="TaskResult",
                                        summary=str(event.get("output") or "") or "task completed",
                                        channel=str(conv.get("channel") or ""),
                                        conversation_id=str(conv.get("conversation_id") or ""),
                                        task_id=task_id,
                                        sender_name=str(item.get("agent_name") or "worker_agent"),
                                        payload={
                                            "event_type": event_type,
                                            "output_at": observed_at,
                                            "error_information": str(event.get("error_information") or ""),
                                        },
                                    )
                                conv["updated_at"] = now_iso()
                                conv["running_task_count"] = sum(1 for linked in conv.get("linked_tasks", []) if linked.get("status") == "running")
                                conv["unread_event_count"] = len(conv.get("pending_events", []))
                                matched = True
                                changed = True
                                break
                            if matched:
                                break
                        if matched:
                            break

                ack_task_event(event_id)
                results.append({
                    "event_id": event_id,
                    "event_type": event_type,
                    "task_id": task_id,
                    "matched": matched,
                })

            if changed:
                self.panel_store.save_panel(panel)
            return results

    def reconcile_task_statuses(self) -> List[Dict[str, Any]]:
        observations: List[Dict[str, Any]] = []
        panel = self.panel_store.load_panel()
        changed = False
        for channel_name, channel_payload in panel.get("channels", {}).items():
            for conversation_id, conv in channel_payload.get("conversations", {}).items():
                for item in conv.get("linked_tasks", []):
                    task_id = str(item.get("task_id") or "")
                    if not task_id:
                        continue
                    snapshot = self.get_task_snapshot(task_id=task_id)
                    if snapshot.get("status") == "error":
                        observations.append({"channel": channel_name, "conversation_id": conversation_id, "task_id": task_id, "status": "error", "error": snapshot.get("error")})
                        continue
                    now_dt = datetime.now().astimezone()
                    latest_instruction = snapshot.get("latest_instruction") or {}
                    if not isinstance(latest_instruction, dict):
                        latest_instruction = {}
                    log_path = Path(item.get("log_path") or snapshot.get("log_path") or "")
                    last_log_at = datetime.fromtimestamp(log_path.stat().st_mtime).astimezone().isoformat(timespec="seconds") if log_path.exists() else ""
                    patch = {
                        "status": "running" if snapshot.get("running") else "idle",
                        "share_context_path": snapshot.get("share_context_path", ""),
                        "stack_path": snapshot.get("stack_path", ""),
                        "last_thinking": snapshot.get("latest_thinking", ""),
                        "last_thinking_at": snapshot.get("latest_thinking_at", ""),
                        "last_final_output": snapshot.get("last_final_output", ""),
                        "last_final_output_at": snapshot.get("last_final_output_at", ""),
                        "last_action_at": snapshot.get("last_updated", ""),
                        "last_log_at": last_log_at,
                        "last_launch_at": str(item.get("last_launch_at") or ""),
                        "pid_alive": bool(snapshot.get("running")),
                        "watchdog_observation": "" if snapshot.get("last_final_output") else str(item.get("watchdog_observation") or ""),
                        "watchdog_suspected_state": "healthy" if snapshot.get("last_final_output") else str(item.get("watchdog_suspected_state") or ""),
                        "user_input": str((snapshot.get("runtime") or {}).get("user_input") or item.get("user_input") or ""),
                        "latest_instruction": str(latest_instruction.get("instruction") or item.get("latest_instruction") or ""),
                    }
                    snapshot_final_output = str(snapshot.get("last_final_output") or "")
                    snapshot_final_at = str(snapshot.get("last_final_output_at") or "")
                    item_final_output = str(item.get("last_final_output") or "")
                    item_final_at = str(item.get("last_final_output_at") or "")
                    final_changed = (
                        bool(snapshot_final_output)
                        and (
                            not self._timestamps_equivalent(item_final_at, snapshot_final_at)
                            or item_final_output != snapshot_final_output
                        )
                    )
                    failed_changed = (
                        not snapshot.get("running")
                        and not snapshot.get("last_final_output")
                        and (
                            (parse_iso(str(item.get("last_launch_at") or "")) or now_dt) <= now_dt - timedelta(seconds=45)
                        )
                        and (
                            not last_log_at
                            or (parse_iso(last_log_at) is not None and parse_iso(last_log_at) <= now_dt - timedelta(seconds=15))
                        )
                        and (
                            str(item.get("status") or "") == "running"
                            or bool(item.get("pid_alive"))
                        )
                    )
                    if final_changed:
                        final_output_at = str(snapshot_final_at or now_iso())
                        final_output_text = str(snapshot_final_output or "")
                        already_queued = self._task_result_instruction_exists(
                            task_id=task_id,
                            output_at=final_output_at,
                            summary=final_output_text,
                        )
                        if not already_queued:
                            conv.setdefault("pending_events", []).append({
                                "type": "task_completed",
                                "task_id": task_id,
                                "timestamp": final_output_at,
                            })
                            self._queue_monitor_instruction(
                                instruction_type="TaskResult",
                                summary=final_output_text or "task completed",
                                channel=channel_name,
                                conversation_id=conversation_id,
                                task_id=task_id,
                                sender_name=str(item.get("agent_name") or "worker_agent"),
                                payload={
                                    "event_type": "task_final_output",
                                    "output_at": final_output_at,
                                    "fallback_from_reconcile": True,
                                },
                            )
                    elif failed_changed:
                        conv.setdefault("pending_events", []).append({
                            "type": "task_failed",
                            "task_id": task_id,
                            "timestamp": str(snapshot.get("last_updated") or now_iso()),
                            "suspected_state": "process_dead",
                            "summary": "task stopped without final_output",
                        })
                        self._queue_monitor_instruction(
                            instruction_type="TaskFailure",
                            summary="task stopped without final_output",
                            channel=channel_name,
                            conversation_id=conversation_id,
                            task_id=task_id,
                            sender_name=str(item.get("agent_name") or "worker_agent"),
                            payload={"suspected_state": "process_dead"},
                        )
                    if any(str(item.get(key) or "") != str(value or "") for key, value in patch.items()):
                        item.update(patch)
                        changed = True
                    observations.append({"channel": channel_name, "conversation_id": conversation_id, "task_id": task_id, **patch})
                conv["running_task_count"] = sum(1 for linked in conv.get("linked_tasks", []) if linked.get("status") == "running")
                conv["has_stale_running_tasks"] = any(
                    linked.get("status") == "running" and not linked.get("pid_alive")
                    for linked in conv.get("linked_tasks", [])
                )
                conv["unread_event_count"] = len(conv.get("pending_events", []))
        if changed:
            self.panel_store.save_panel(panel)
        return observations

    def queue_message(self, *, channel: str, conversation_id: str, message: str, attachments: Optional[List[Dict[str, Any]]] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._runtime_scope():
            return queue_outbound_message(channel=channel, conversation_id=conversation_id, message=message, attachments=attachments, metadata=metadata)

    def send_message_now(self, *, channel: str, conversation_id: str, message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        adapter = self.adapters.get(str(channel).strip())
        if adapter is None:
            return {"status": "error", "output": "", "error": f"adapter not configured: {channel}"}
        ok, remote_id = adapter.send_message(str(conversation_id), str(message), attachments or [])
        if not ok:
            return {"status": "error", "output": "", "error": str(remote_id)}
        append_history(
            channel=str(channel),
            conversation_id=str(conversation_id),
            event={
                "message_id": str(remote_id or uuid.uuid4().hex[:12]),
                "timestamp": now_iso(),
                "sender_id": "cheapclaw",
                "sender_name": "cheapclaw",
                "text": str(message or ""),
                "attachments": attachments or [],
                "is_mention_to_bot": False,
                "direction": "outbound",
            },
        )
        return {
            "status": "success",
            "output": f"sent outbound message {remote_id}",
            "remote_id": str(remote_id or ""),
            "channel": str(channel),
            "conversation_id": str(conversation_id),
        }

    def process_outbox(self) -> List[Dict[str, Any]]:
        with self._runtime_scope():
            results = []
            for event in list_outbox_events():
                metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
                max_retries = max(0, int(metadata.get("max_retries", OUTBOX_DEFAULT_MAX_RETRIES) or OUTBOX_DEFAULT_MAX_RETRIES))
                retry_count = max(0, int(metadata.get("retry_count", 0) or 0))
                next_retry_at = parse_iso(str(metadata.get("next_retry_at") or ""))
                now_dt = datetime.now().astimezone()
                if next_retry_at and next_retry_at > now_dt:
                    results.append(
                        {
                            "event_id": event.get("event_id"),
                            "status": "deferred",
                            "next_retry_at": next_retry_at.isoformat(timespec="seconds"),
                        }
                    )
                    continue
                channel = str(event.get("channel") or "").strip()
                adapter = self.adapters.get(channel)
                if adapter is None:
                    results.append({"event_id": event.get("event_id"), "status": "error", "error": f"adapter not configured: {channel}"})
                    continue
                ok, remote_id = adapter.send_message(str(event.get("conversation_id") or ""), str(event.get("message") or ""), event.get("attachments") or [])
                if ok:
                    ack_outbox_event(str(event.get("event_id") or ""))
                    append_history(
                        channel=channel,
                        conversation_id=str(event.get("conversation_id") or ""),
                        event={
                            "message_id": remote_id or str(event.get("event_id") or ""),
                            "timestamp": now_iso(),
                            "sender_id": "cheapclaw",
                            "sender_name": "cheapclaw",
                            "text": str(event.get("message") or ""),
                            "attachments": event.get("attachments") or [],
                            "is_mention_to_bot": False,
                            "direction": "outbound",
                        },
                    )
                    results.append({"event_id": event.get("event_id"), "status": "success", "remote_id": remote_id})
                else:
                    retry_count += 1
                    if retry_count > max_retries:
                        move_outbox_event_to_deadletter(event, reason=str(remote_id or "max retries exceeded"))
                        _log(
                            f"outbox event moved to deadletter: event_id={event.get('event_id')} "
                            f"channel={channel} retries={retry_count} reason={remote_id}",
                            level="warning",
                        )
                        results.append(
                            {
                                "event_id": event.get("event_id"),
                                "status": "deadletter",
                                "error": str(remote_id),
                                "retry_count": retry_count,
                            }
                        )
                        continue
                    # Exponential backoff by retry_count=1,2,3... => 5s,10s,20s,40s... capped at 300s.
                    backoff_seconds = min(
                        OUTBOX_MAX_BACKOFF_SECONDS,
                        OUTBOX_BASE_BACKOFF_SECONDS * (2 ** max(0, retry_count - 1)),
                    )
                    metadata["retry_count"] = retry_count
                    metadata["next_retry_at"] = (now_dt + timedelta(seconds=backoff_seconds)).isoformat(timespec="seconds")
                    event["metadata"] = metadata
                    save_outbox_event(event)
                    results.append(
                        {
                            "event_id": event.get("event_id"),
                            "status": "error",
                            "error": remote_id,
                            "retry_count": retry_count,
                            "next_retry_at": metadata["next_retry_at"],
                        }
                    )
            return results

    def poll_channels(self) -> List[Dict[str, Any]]:
        events = []
        for adapter in self.adapters.values():
            for event in adapter.poll_events():
                self.ingest_event(event)
                events.append(event)
        return events

    def tick_watchdog(self) -> List[Dict[str, Any]]:
        observations = []
        panel = self.panel_store.load_panel()
        for channel_name, channel_payload in panel.get("channels", {}).items():
            for conversation_id, conv in channel_payload.get("conversations", {}).items():
                for item in conv.get("linked_tasks", []):
                    task_id = item.get("task_id")
                    if not task_id:
                        continue
                    snapshot = self.get_task_snapshot(task_id=task_id)
                    if snapshot.get("status") == "error":
                        observations.append({"channel": channel_name, "conversation_id": conversation_id, "task_id": task_id, "status": "error", "error": snapshot.get("error")})
                        continue
                    log_path = Path(item.get("log_path") or snapshot.get("log_path") or "")
                    last_log_at = datetime.fromtimestamp(log_path.stat().st_mtime).astimezone().isoformat(timespec="seconds") if log_path.exists() else ""
                    latest_thinking_at = snapshot.get("latest_thinking_at", "")
                    suspected = "healthy"
                    note = ""
                    if snapshot.get("running") and not latest_thinking_at and not last_log_at:
                        suspected = "quiet_but_alive"
                        note = "running but no thinking/log timestamp yet"
                    elif snapshot.get("running") and latest_thinking_at:
                        thinking_dt = parse_iso(latest_thinking_at)
                        if thinking_dt and thinking_dt < datetime.now().astimezone() - timedelta(hours=1):
                            suspected = "possibly_stalled"
                            note = "thinking has not moved for over 1 hour"
                    elif not snapshot.get("running") and not snapshot.get("last_final_output"):
                        suspected = "process_dead"
                        note = "task stopped without final output"
                    patch = {
                        "status": "running" if snapshot.get("running") else "idle",
                        "share_context_path": snapshot.get("share_context_path", ""),
                        "stack_path": snapshot.get("stack_path", ""),
                        "last_thinking": snapshot.get("latest_thinking", ""),
                        "last_thinking_at": latest_thinking_at,
                        "last_final_output": snapshot.get("last_final_output", ""),
                        "last_final_output_at": snapshot.get("last_final_output_at", ""),
                        "last_action_at": snapshot.get("last_updated", ""),
                        "last_log_at": last_log_at,
                        "pid_alive": snapshot.get("running"),
                        "watchdog_observation": note,
                        "watchdog_suspected_state": suspected,
                    }
                    if item.get("last_final_output_at") != snapshot.get("last_final_output_at") and snapshot.get("last_final_output"):
                        conv.setdefault("pending_events", []).append({"type": "task_completed", "task_id": task_id, "timestamp": now_iso()})
                    elif item.get("watchdog_suspected_state") != suspected and suspected != "healthy":
                        conv.setdefault("pending_events", []).append({"type": "watchdog_tick", "task_id": task_id, "suspected_state": suspected, "timestamp": now_iso()})
                        self._queue_monitor_instruction(
                            instruction_type="WatchDog",
                            summary=note or suspected,
                            channel=channel_name,
                            conversation_id=conversation_id,
                            task_id=str(task_id),
                            sender_name="watchdog",
                            payload={"suspected_state": suspected},
                        )
                    item.update(patch)
                    observations.append({"channel": channel_name, "conversation_id": conversation_id, "task_id": task_id, **patch})
                conv["running_task_count"] = sum(1 for item in conv.get("linked_tasks", []) if item.get("status") == "running")
                conv["unread_event_count"] = len(conv.get("pending_events", []))
        panel["service_state"]["watchdog_last_run_at"] = now_iso()
        self.panel_store.save_panel(panel)
        return observations

    def _watchdog_due(self) -> bool:
        panel = self.panel_store.load_panel()
        last_run_at = parse_iso(str(panel.get("service_state", {}).get("watchdog_last_run_at") or ""))
        if last_run_at is None:
            return True
        return last_run_at <= datetime.now().astimezone() - timedelta(seconds=self.watchdog_interval_sec)

    def tick_plans(self) -> List[Dict[str, Any]]:
        payload = load_plans()
        results = []
        now = datetime.now().astimezone()
        changed = False
        for plan in payload.get("plans", []):
            if not plan.get("enabled", True):
                continue
            due_at = parse_iso(str(plan.get("next_run_at") or ""))
            if not due_at or due_at > now:
                continue
            task_id = str(plan.get("task_id") or "").strip()
            if plan.get("scope") == "task" and task_id:
                snapshot = self.get_task_snapshot(task_id=task_id)
                if snapshot.get("running"):
                    plan["last_result"] = "deferred: task running"
                    plan["next_run_at"] = (now + timedelta(minutes=5)).isoformat(timespec="seconds")
                    changed = True
                    results.append({"plan_id": plan.get("plan_id"), "status": "deferred"})
                    continue
                self.add_task_message(task_id=task_id, message=str(plan.get("message") or "scheduled task tick"), source="system", resume_if_needed=True)
                plan["last_result"] = "appended task message"
            else:
                channel = str(plan.get("channel") or "").strip()
                conversation_id = str(plan.get("conversation_id") or "").strip()
                if channel and conversation_id:
                    panel = self.panel_store.load_panel()
                    conv = ensure_conversation(panel, channel=channel, conversation_id=conversation_id)
                    conv.setdefault("pending_events", []).append({"type": "plan_tick", "plan_id": plan.get("plan_id"), "timestamp": now_iso(), "message": str(plan.get("message") or "")})
                    self.panel_store.save_panel(panel)
                    self._queue_monitor_instruction(
                        instruction_type="Plan",
                        summary=str(plan.get("message") or "scheduled plan tick"),
                        channel=channel,
                        conversation_id=conversation_id,
                        sender_name=str(plan.get("name") or "plan"),
                        payload={"plan_id": str(plan.get("plan_id") or ""), "scope": str(plan.get("scope") or "")},
                    )
                plan["last_result"] = "queued main_agent tick"
            plan["last_run_at"] = now_iso()
            schedule_type = str(plan.get("schedule_type") or "").strip()
            if schedule_type in {"daily", "weekly"} and str(plan.get("time_of_day") or "").strip():
                plan["next_run_at"] = compute_next_scheduled_run(
                    schedule_type=schedule_type,
                    time_of_day=str(plan.get("time_of_day") or ""),
                    days_of_week=plan.get("days_of_week") or [],
                    now=now,
                )
            elif int(plan.get("interval_sec") or 0) > 0:
                plan["next_run_at"] = (now + timedelta(seconds=int(plan.get("interval_sec") or 0))).isoformat(timespec="seconds")
            else:
                plan["enabled"] = False
            changed = True
            results.append({"plan_id": plan.get("plan_id"), "status": plan.get("last_result")})
        if changed:
            save_plans(payload)
        return results

    def _build_supervisor_input(self, reason: str) -> str:
        panel = self.panel_store.load_panel()
        pending_instructions = self._pending_monitor_instructions(limit=100)
        managed_count, running_count = self._managed_task_counts()
        conversation_hints: Dict[str, Dict[str, Any]] = {}
        for item in pending_instructions:
            channel = str(item.get("channel") or "").strip()
            conversation_id = str(item.get("conversation_id") or "").strip()
            if not channel or not conversation_id:
                continue
            key = f"{channel}:{conversation_id}"
            if key in conversation_hints:
                continue
            conv = panel.get("channels", {}).get(channel, {}).get("conversations", {}).get(conversation_id, {})
            linked = [entry for entry in conv.get("linked_tasks", []) if isinstance(entry, dict)]
            linked.sort(
                key=lambda entry: (
                    str(entry.get("last_action_at") or ""),
                    str(entry.get("last_final_output_at") or ""),
                    str(entry.get("created_at") or ""),
                ),
                reverse=True,
            )
            conversation_hints[key] = {
                "existing_task_count": len(linked),
                "running_task_count": sum(1 for entry in linked if str(entry.get("status") or "") == "running"),
                "recent_task_ids": [str(entry.get("task_id") or "") for entry in linked[:3] if str(entry.get("task_id") or "")],
                "latest_bound_task_id": next(
                    (
                        str(binding.get("task_id") or "")
                        for binding in sorted(
                            [binding for binding in conv.get("message_task_bindings", []) if isinstance(binding, dict)],
                            key=lambda binding: str(binding.get("bound_at") or ""),
                            reverse=True,
                        )
                        if str(binding.get("task_id") or "")
                    ),
                    "",
                ),
            }
        payload = {
            "trigger_reason": reason,
            "summary_rules": "这里优先给你 pending monitor instructions 的短摘要。若你已经能判断意图和关联 task_id，请直接处理；否则调用 CheapClaw 工具查询历史消息、任务列表、message/task 绑定或 task 状态。",
            "pending_instruction_count": len(pending_instructions),
            "pending_instructions": [
                {
                    "instruction_id": str(item.get("instruction_id") or ""),
                    "instruction_type": str(item.get("instruction_type") or ""),
                    "created_at": str(item.get("created_at") or ""),
                    "channel": str(item.get("channel") or ""),
                    "conversation_id": str(item.get("conversation_id") or ""),
                    "source_message_id": str(item.get("source_message_id") or ""),
                    "task_id": str(item.get("task_id") or ""),
                    "sender_name": str(item.get("sender_name") or ""),
                    "summary": _short_text(str(item.get("summary") or ""), limit=360),
                    "conversation_task_hint": conversation_hints.get(
                        f"{str(item.get('channel') or '').strip()}:{str(item.get('conversation_id') or '').strip()}",
                        {},
                    ),
                }
                for item in pending_instructions
            ],
            "task_overview": {
                "managed_task_count": managed_count,
                "running_task_count": running_count,
            },
            "service_state": {
                "main_agent_running": bool(panel.get("service_state", {}).get("main_agent_running")),
                "main_agent_last_started_at": str(panel.get("service_state", {}).get("main_agent_last_started_at") or ""),
                "watchdog_last_run_at": str(panel.get("service_state", {}).get("watchdog_last_run_at") or ""),
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def run_supervisor_once(self, reason: str = "monitor_instruction_queue") -> Dict[str, Any]:
        if not self._supervisor_lock.acquire(blocking=False):
            return {"status": "busy", "output": "supervisor already running"}
        run_id = f"sup_{uuid.uuid4().hex[:10]}"
        try:
            self.panel_store.set_main_agent_state(running=True, run_id=run_id)
            with self._runtime_scope():
                self._ensure_monitor_task_files()
                self._preflight_supervisor_tools()
                result = self.sdk.run(
                    self._build_supervisor_input(reason),
                    task_id=str(self.paths.supervisor_task_id),
                    agent_system=self.supervisor_agent_system,
                    agent_name=self.supervisor_agent_name,
                    force_new=False,
                )
                self._recover_from_unregistered_tool_error(result if isinstance(result, dict) else {})
            return result
        finally:
            self.panel_store.set_main_agent_state(running=False)
            self._supervisor_lock.release()

    def run_once(self) -> Dict[str, Any]:
        polled = self.poll_channels()
        plans = self.tick_plans()
        task_events = self.process_task_events()
        reconciled = self.reconcile_task_statuses()
        watchdog = self.tick_watchdog() if self._watchdog_due() else []
        outbox = self.process_outbox()
        supervisor_runs = []
        if (
            not self._monitor_running()
            and self._pending_monitor_instruction_count() > 0
        ):
            result = self.run_supervisor_once(reason="pending_monitor_instructions")
            supervisor_runs.append(result)
            outbox.extend(self.process_outbox())
        supervisor = supervisor_runs[-1] if supervisor_runs else None
        return {
            "status": "success",
            "polled_events": polled,
            "plan_results": plans,
            "task_events": task_events,
            "reconciled_tasks": reconciled,
            "watchdog": watchdog,
            "outbox": outbox,
            "supervisor": supervisor,
            "supervisor_runs": supervisor_runs,
        }

    def run_forever(self, poll_interval: int = 15) -> None:
        poll_interval = max(1, int(poll_interval))
        while True:
            try:
                result = self.run_once()
                _log(
                    "cycle complete: "
                    f"polled={len(result.get('polled_events', []))}, "
                    f"plans={len(result.get('plan_results', []))}, "
                    f"task_events={len(result.get('task_events', []))}, "
                    f"reconciled={len(result.get('reconciled_tasks', []))}, "
                    f"watchdog={len(result.get('watchdog', []))}, "
                    f"outbox={len(result.get('outbox', []))}, "
                    f"supervisor={result.get('supervisor', {}).get('status') if isinstance(result.get('supervisor'), dict) else 'idle'}"
                )
            except Exception as exc:
                _log(f"cycle failed: {exc}")
                traceback.print_exc()
            time.sleep(poll_interval)

    @staticmethod
    def _task_created_sort_value(task: Dict[str, Any]) -> str:
        created_at = str(task.get("created_at") or "")
        if created_at:
            return created_at
        task_id = str(task.get("task_id") or "")
        try:
            name = Path(task_id).name
            stamp = name.split("_", 2)[:2]
            if len(stamp) == 2:
                parsed = datetime.strptime("_".join(stamp), "%Y%m%d_%H%M%S")
                return parsed.astimezone().isoformat(timespec="seconds")
        except Exception:
            pass
        return ""

    def _fleet_dashboard_url(self) -> str:
        fleet_config_path = str(os.environ.get("CHEAPCLAW_FLEET_CONFIG_PATH") or "").strip()
        if fleet_config_path:
            state_path = Path(fleet_config_path).expanduser().resolve().parent / ".fleet_web" / "fleet_web.state.json"
            state = _load_json(state_path, {})
            url = str(state.get("url") or "").strip()
            if url:
                return url
        return "http://127.0.0.1:8787/dashboard"

    def serve_webhooks(self, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def _dispatch(self):
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/")
                headers = {key: value for key, value in self.headers.items()}
                if self.command == "GET":
                    if path in {"", "/", "/dashboard"}:
                        self.send_response(302)
                        self.send_header("Location", service._fleet_dashboard_url())
                        self.end_headers()
                        return
                    if path == "/api/global-skills":
                        body = json.dumps(service.list_global_skills(), ensure_ascii=False, indent=2).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if path == "/api/fleet":
                        body = json.dumps(service.fleet_payload(), ensure_ascii=False, indent=2).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if path == "/api/localchat/conversations":
                        body = json.dumps(service.localchat_conversations_payload(), ensure_ascii=False, indent=2).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if path == "/api/monitor-instructions":
                        query = parse_qs(parsed.query)
                        body = json.dumps(
                            service.monitor_instructions_payload(
                                bot_id=str((query.get("bot_id") or [""])[0] or "").strip(),
                                status=str((query.get("status") or [""])[0] or "").strip(),
                                limit=int((query.get("limit") or ["100"])[0] or "100"),
                            ),
                            ensure_ascii=False,
                            indent=2,
                        ).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if path == "/api/task-settings":
                        task_id = str((parse_qs(parsed.query).get("task_id") or [""])[0] or "").strip()
                        if not task_id:
                            self.send_error(400, "task_id is required")
                            return
                        body = json.dumps(service.get_task_preferences(task_id=task_id), ensure_ascii=False, indent=2).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if path == "/api/task-system-add":
                        task_id = str((parse_qs(parsed.query).get("task_id") or [""])[0] or "").strip()
                        if not task_id:
                            self.send_error(400, "task_id is required")
                            return
                        body = json.dumps(service.get_task_system_add(task_id=task_id), ensure_ascii=False, indent=2).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    query = parse_qs(parsed.query)
                    adapter = service._adapter_for_webhook(path)
                    if adapter is None:
                        self.send_error(404)
                        return
                    status, out_headers, body = adapter.handle_webhook_get(path, query, headers)
                else:
                    body_raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
                    if path == "/api/monitor-trigger":
                        try:
                            payload = json.loads(body_raw.decode("utf-8") or "{}")
                        except Exception:
                            self.send_error(400, "invalid json body")
                            return
                        result = service.trigger_monitor(
                            bot_id=str(payload.get("bot_id") or "").strip(),
                            reason=str(payload.get("reason") or "manual_console").strip() or "manual_console",
                        )
                        status = 200 if result.get("status") == "success" else 400
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
                        return
                    if path == "/api/task-settings":
                        try:
                            payload = json.loads(body_raw.decode("utf-8") or "{}")
                        except Exception:
                            self.send_error(400, "invalid json body")
                            return
                        task_id = str(payload.get("task_id") or "").strip()
                        if not task_id:
                            self.send_error(400, "task_id is required")
                            return
                        result = service.update_task_preferences(
                            task_id=task_id,
                            default_exposed_skills=payload.get("default_exposed_skills"),
                            mcp_servers=payload.get("mcp_servers"),
                        )
                        status = 200 if result.get("status") == "success" else 400
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
                        return
                    if path == "/api/task-system-add":
                        try:
                            payload = json.loads(body_raw.decode("utf-8") or "{}")
                        except Exception:
                            self.send_error(400, "invalid json body")
                            return
                        task_id = str(payload.get("task_id") or "").strip()
                        if not task_id:
                            self.send_error(400, "task_id is required")
                            return
                        result = service.update_task_system_add(
                            task_id=task_id,
                            content=str(payload.get("content") or ""),
                        )
                        status = 200 if result.get("status") == "success" else 400
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
                        return
                    if path == "/api/localchat/create":
                        try:
                            payload = json.loads(body_raw.decode("utf-8") or "{}")
                        except Exception:
                            self.send_error(400, "invalid json body")
                            return
                        result = service.create_localchat_conversation(
                            conversation_id=str(payload.get("conversation_id") or "").strip(),
                            display_name=str(payload.get("display_name") or "").strip(),
                            participant_bots=[
                                str(item).strip()
                                for item in (payload.get("participant_bots") or [])
                                if str(item).strip()
                            ],
                            require_mention=(
                                bool(payload.get("require_mention"))
                                if "require_mention" in payload
                                else None
                            ),
                        )
                        status = 200 if result.get("status") == "success" else 400
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
                        return
                    if path == "/api/localchat/send":
                        try:
                            payload = json.loads(body_raw.decode("utf-8") or "{}")
                        except Exception:
                            self.send_error(400, "invalid json body")
                            return
                        conversation_id = str(payload.get("conversation_id") or "").strip()
                        message = str(payload.get("message") or "").strip()
                        if not conversation_id or not message:
                            self.send_error(400, "conversation_id and message are required")
                            return
                        result = service.send_localchat_message(
                            conversation_id=conversation_id,
                            message=message,
                            sender_name=str(payload.get("sender_name") or "web_user").strip() or "web_user",
                            sender_id=str(payload.get("sender_id") or "web_user").strip() or "web_user",
                            participant_bots=[
                                str(item).strip()
                                for item in (payload.get("participant_bots") or [])
                                if str(item).strip()
                            ],
                            require_mention=(
                                bool(payload.get("require_mention"))
                                if "require_mention" in payload
                                else None
                            ),
                        )
                        status = 200 if result.get("status") == "success" else 400
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
                        return
                    adapter = service._adapter_for_webhook(path)
                    if adapter is None:
                        self.send_error(404)
                        return
                    status, out_headers, body = adapter.handle_webhook_post(path, body_raw, headers)
                self.send_response(status)
                for key, value in out_headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._dispatch()

            def do_POST(self):
                self._dispatch()

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer((host, int(port)), Handler)
        state = self.load_runtime_state()
        state["webhook_server"] = {"host": host, "port": int(port), "started_at": now_iso()}
        self.save_runtime_state(state)
        return server

    def _localweb_adapter(self) -> Optional[LocalWebAdapter]:
        adapter = self.adapters.get("localweb")
        return adapter if isinstance(adapter, LocalWebAdapter) else None

    def localchat_conversations_payload(self) -> Dict[str, Any]:
        adapter = self._localweb_adapter()
        if adapter is None:
            return {"status": "error", "error": "localweb adapter is not enabled in channels config"}
        items = adapter.list_conversations()
        return {
            "status": "success",
            "channel": "localweb",
            "conversation_count": len(items),
            "conversations": items,
        }

    def create_localchat_conversation(
        self,
        *,
        conversation_id: str = "",
        display_name: str = "",
        participant_bots: Optional[List[str]] = None,
        require_mention: Optional[bool] = None,
    ) -> Dict[str, Any]:
        adapter = self._localweb_adapter()
        if adapter is None:
            return {"status": "error", "error": "localweb adapter is not enabled in channels config"}
        participants = [str(item).strip() for item in (participant_bots or []) if str(item).strip()]
        conv = adapter.ensure_conversation_record(
            conversation_id=conversation_id,
            display_name=display_name,
            conversation_type="group" if len(participants) > 1 else "person",
            participant_bots=participants if participants else None,
            require_mention=require_mention,
            created_by=self.bot_id,
        )

        # 让当前 bot 的面板中也能马上看到该会话。
        def _update(panel: Dict[str, Any]) -> Dict[str, Any]:
            ensure_conversation(
                panel,
                channel="localweb",
                conversation_id=str(conv.get("conversation_id") or ""),
                conversation_type=str(conv.get("conversation_type") or "group"),
                display_name=str(conv.get("display_name") or conv.get("conversation_id") or ""),
                require_mention=bool(conv.get("require_mention", True)),
            )
            return panel

        panel = self.panel_store.mutate(_update)
        refresh_conversation_context_file("localweb", str(conv.get("conversation_id") or ""), panel)
        return {"status": "success", "conversation": conv}

    def send_localchat_message(
        self,
        *,
        conversation_id: str,
        message: str,
        sender_name: str = "web_user",
        sender_id: str = "web_user",
        participant_bots: Optional[List[str]] = None,
        require_mention: Optional[bool] = None,
    ) -> Dict[str, Any]:
        adapter = self._localweb_adapter()
        if adapter is None:
            return {"status": "error", "error": "localweb adapter is not enabled in channels config"}
        result = adapter.emit_event(
            conversation_id=conversation_id,
            message=message,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_type="user",
            participant_bots=participant_bots,
            require_mention=require_mention,
        )
        if result.get("status") != "success":
            return {"status": "error", "error": str(result.get("error") or "localweb send failed")}
        event = result.get("event") if isinstance(result.get("event"), dict) else {}
        return {
            "status": "success",
            "output": f"queued localweb inbound event {event.get('event_id')}",
            "event": event,
        }

    def _adapter_for_webhook(self, path: str) -> Optional[ChannelAdapter]:
        if path.endswith("/feishu"):
            return self.adapters.get("feishu")
        if path.endswith("/whatsapp"):
            return self.adapters.get("whatsapp")
        if path.endswith("/qq"):
            return self.adapters.get("qq")
        if path.endswith("/wechat"):
            return self.adapters.get("wechat")
        return None

    def credentials_needed(self) -> Dict[str, List[str]]:
        return {
            "telegram": ["bot_token"],
            "feishu": ["app_id", "app_secret", "verify_token"],
            "whatsapp": ["access_token", "phone_number_id", "verify_token"],
            "discord": ["bot_token"],
            "qq": ["onebot_api_base"],
            "wechat": ["onebot_api_base"],
            "localweb": [],
        }

    def _bot_runtime_summary(self, bot_entry: Dict[str, Any]) -> Dict[str, Any]:
        user_data_root = Path(str(bot_entry.get("user_data_root") or "")).expanduser().resolve()
        cheapclaw_root = user_data_root / "cheapclaw"
        panel_path = cheapclaw_root / "panel" / "panel.json"
        runtime_state_path = cheapclaw_root / "runtime" / "state.json"
        panel = _load_json(panel_path, {"channels": {}, "service_state": {}})
        runtime_state = _load_json(runtime_state_path, {})
        instructions_payload = load_monitor_instructions_for_root(user_data_root)
        conversations = 0
        linked_tasks = 0
        running_tasks = 0
        pending_event_conversations = 0
        for channel_payload in panel.get("channels", {}).values():
            if not isinstance(channel_payload, dict):
                continue
            for conv in channel_payload.get("conversations", {}).values():
                if not isinstance(conv, dict):
                    continue
                conversations += 1
                if conv.get("pending_events"):
                    pending_event_conversations += 1
                tasks = [item for item in conv.get("linked_tasks", []) if isinstance(item, dict)]
                linked_tasks += len(tasks)
                running_tasks += sum(1 for item in tasks if str(item.get("status") or "") == "running")
        pending_instructions = [
            item for item in instructions_payload.get("instructions", [])
            if str(item.get("status") or "") == "pending"
        ]
        bot_runtime = runtime_state.get("bot") or {}
        webhook_runtime = runtime_state.get("webhook_server") or {}
        return {
            "bot_id": str(bot_entry.get("bot_id") or ""),
            "display_name": str(bot_entry.get("display_name") or bot_entry.get("bot_id") or ""),
            "user_data_root": str(user_data_root),
            "enabled": bool(bot_entry.get("enabled", True)),
            "pid": int(bot_runtime.get("pid") or 0),
            "main_agent_running": bool(panel.get("service_state", {}).get("main_agent_running")),
            "main_agent_last_started_at": str(panel.get("service_state", {}).get("main_agent_last_started_at") or ""),
            "main_agent_last_finished_at": str(panel.get("service_state", {}).get("main_agent_last_finished_at") or ""),
            "watchdog_last_run_at": str(panel.get("service_state", {}).get("watchdog_last_run_at") or ""),
            "conversation_count": conversations,
            "pending_event_conversation_count": pending_event_conversations,
            "task_count": linked_tasks,
            "running_task_count": running_tasks,
            "pending_instruction_count": len(pending_instructions),
            "latest_instruction_at": max((str(item.get("created_at") or "") for item in pending_instructions), default=""),
            "recent_instructions": [
                {
                    "instruction_id": str(item.get("instruction_id") or ""),
                    "instruction_type": str(item.get("instruction_type") or ""),
                    "created_at": str(item.get("created_at") or ""),
                    "summary": _short_text(str(item.get("summary") or ""), limit=120),
                }
                for item in sorted(pending_instructions, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:5]
            ],
            "webhook_server": {
                "host": str(webhook_runtime.get("host") or ""),
                "port": int(webhook_runtime.get("port") or 0) if webhook_runtime.get("port") else 0,
                "started_at": str(webhook_runtime.get("started_at") or ""),
            },
        }

    def _fleet_bot_entry(self, bot_id: str = "") -> Optional[Dict[str, Any]]:
        target = str(bot_id or "").strip() or self.bot_id
        if target == self.bot_id:
            return {
                "bot_id": self.bot_id,
                "display_name": self.bot_display_name,
                "user_data_root": str(self.paths.user_data_root),
                "enabled": True,
            }
        peer = get_peer_bot(target, path=self.fleet_config_path)
        if peer is None:
            return None
        return peer

    def fleet_payload(self) -> Dict[str, Any]:
        if self.fleet_config_path:
            config = load_fleet_config(self.fleet_config_path)
            bots = [self._bot_runtime_summary(item) for item in config.get("bots", [])]
        else:
            bots = [self._bot_runtime_summary({
                "bot_id": self.bot_id,
                "display_name": self.bot_display_name,
                "user_data_root": str(self.paths.user_data_root),
                "enabled": True,
            })]
        bots.sort(key=lambda item: (item["bot_id"] != self.bot_id, item.get("display_name") or item.get("bot_id") or ""))
        return {
            "status": "success",
            "current_bot_id": self.bot_id,
            "current_bot_display_name": self.bot_display_name,
            "fleet_config_path": self.fleet_config_path,
            "bots": bots,
        }

    def monitor_instructions_payload(self, *, bot_id: str = "", status: str = "", limit: int = 100) -> Dict[str, Any]:
        target = self._fleet_bot_entry(bot_id)
        if target is None:
            return {"status": "error", "error": f"bot not found: {bot_id}"}
        instructions_payload = load_monitor_instructions_for_root(target["user_data_root"])
        items = [item for item in instructions_payload.get("instructions", []) if isinstance(item, dict)]
        if status:
            items = [item for item in items if str(item.get("status") or "") == status]
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {
            "status": "success",
            "bot_id": str(target.get("bot_id") or ""),
            "display_name": str(target.get("display_name") or target.get("bot_id") or ""),
            "instruction_count": len(items),
            "instructions": items[: max(1, int(limit))],
        }

    def trigger_monitor(self, *, bot_id: str = "", reason: str = "manual_console") -> Dict[str, Any]:
        target = self._fleet_bot_entry(bot_id)
        if target is None:
            return {"status": "error", "error": f"bot not found: {bot_id}"}

        target_bot_id = str(target.get("bot_id") or "")
        if target_bot_id == self.bot_id:
            self._trigger_monitor_if_idle_async(reason=reason)
            return {
                "status": "success",
                "bot_id": self.bot_id,
                "output": f"trigger scheduled for {self.bot_id}",
            }

        runtime_state_path = Path(str(target.get("user_data_root") or "")).expanduser().resolve() / "cheapclaw" / "runtime" / "state.json"
        runtime_state = _load_json(runtime_state_path, {})
        pid = int(((runtime_state.get("bot") or {}).get("pid")) or 0)
        if pid <= 0:
            return {"status": "error", "bot_id": target_bot_id, "error": f"target bot has no live pid: {target_bot_id}"}
        try:
            os.kill(pid, signal.SIGUSR1)
        except Exception as exc:
            return {"status": "error", "bot_id": target_bot_id, "error": str(exc)}
        return {
            "status": "success",
            "bot_id": target_bot_id,
            "output": f"signal sent to {target_bot_id}",
            "pid": pid,
        }


def run_fleet_services(
    *,
    fleet_config_path: str,
    default_poll_interval: int = 15,
) -> int:
    config = load_fleet_config(fleet_config_path)
    bots = [item for item in config.get("bots", []) if item.get("enabled", True)]
    if not bots:
        print(json.dumps({"status": "error", "error": "no enabled bots in fleet config"}, ensure_ascii=False, indent=2))
        return 1

    processes = []
    already_running: List[Dict[str, Any]] = []
    script_path = Path(__file__).resolve()

    def _is_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except Exception:
            return False
        return True

    def _pid_matches_bot_service(pid: int, *, bot_id: str, user_data_root: Path) -> bool:
        if not _is_pid_alive(pid):
            return False
        try:
            proc = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except Exception:
            return False
        if proc.returncode != 0:
            return False
        command = str(proc.stdout or "").strip()
        needles = [
            str(script_path),
            "--run-loop",
            f"--bot-id {str(bot_id)}",
            f"--user-data-root {str(user_data_root)}",
        ]
        return all(needle in command for needle in needles)

    try:
        for item in bots:
            bot_id = str(item["bot_id"])
            user_data_root = Path(str(item["user_data_root"])).expanduser().resolve()
            state_path = user_data_root / "cheapclaw" / "runtime" / "state.json"
            state_payload = _load_json(state_path, {})
            state_pid = int((((state_payload.get("bot") or {}) if isinstance(state_payload.get("bot"), dict) else {}).get("pid")) or 0)
            if _pid_matches_bot_service(state_pid, bot_id=bot_id, user_data_root=user_data_root):
                already_running.append({
                    "bot_id": bot_id,
                    "pid": int(state_pid),
                    "status": "already_running",
                })
                continue
            args = [
                sys.executable,
                str(script_path),
                "--user-data-root",
                str(user_data_root),
                "--bot-id",
                bot_id,
                "--bot-display-name",
                str(item.get("display_name") or bot_id),
                "--fleet-config-path",
                str(Path(fleet_config_path).expanduser().resolve()),
                "--run-loop",
                "--poll-interval",
                str(default_poll_interval),
            ]
            llm_config_path = str(item.get("llm_config_path") or "").strip()
            if llm_config_path:
                args.extend(["--llm-config-path", llm_config_path])
            if item.get("serve_webhooks"):
                args.extend(["--serve-webhooks", "--host", str(item.get("host") or "127.0.0.1"), "--port", str(int(item.get("port") or 8765))])
            process = subprocess.Popen(args, cwd=str(script_path.parent))
            processes.append({
                "bot_id": bot_id,
                "pid": process.pid,
                "process": process,
            })
        print(json.dumps({
            "status": "success",
            "fleet_config_path": str(Path(fleet_config_path).expanduser().resolve()),
            "processes": [{"bot_id": item["bot_id"], "pid": item["pid"]} for item in processes],
            "already_running": already_running,
        }, ensure_ascii=False, indent=2))
        exit_code = 0
        for item in processes:
            code = item["process"].wait()
            if code != 0 and exit_code == 0:
                exit_code = code
        return exit_code
    finally:
        for item in processes:
            process = item["process"]
            if process.poll() is None:
                process.terminate()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CheapClaw standalone service")
    parser.add_argument("--user-data-root", help="User data root used by MLA runtime")
    parser.add_argument("--llm-config-path", default=None, help="Optional llm_config.yaml override")
    parser.add_argument("--bot-id", default="", help="Logical bot id for this CheapClaw service")
    parser.add_argument("--bot-display-name", default="", help="Display name for this bot")
    parser.add_argument("--fleet-config-path", default=None, help="Optional fleet config path shared by multiple bots")
    parser.add_argument("--bootstrap", action="store_true", help="Install CheapClaw tools, agent systems and example configs into the target user_data_root")
    parser.add_argument("--show-runtime", action="store_true", help="Print runtime description and exit")
    parser.add_argument("--show-panel", action="store_true", help="Print panel JSON and exit")
    parser.add_argument("--show-credentials", action="store_true", help="Print live channel credentials required for testing and exit")
    parser.add_argument("--run-fleet", action="store_true", help="Launch multiple CheapClaw bot services from a fleet config")
    parser.add_argument("--run-once", action="store_true", help="Run one CheapClaw polling/watchdog/supervisor cycle")
    parser.add_argument("--run-loop", action="store_true", help="Run the CheapClaw service loop")
    parser.add_argument("--poll-interval", type=int, default=15, help="Polling interval in seconds for --run-loop")
    parser.add_argument("--serve-webhooks", action="store_true", help="Start the webhook server for Feishu / WhatsApp / QQ(OneBot) / WeChat(OneBot)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.run_fleet:
        if not args.fleet_config_path:
            parser.error("--fleet-config-path is required for --run-fleet")
        return run_fleet_services(fleet_config_path=args.fleet_config_path, default_poll_interval=args.poll_interval)
    if not args.user_data_root:
        parser.error("--user-data-root is required unless --run-fleet is used")
    if args.run_once or args.run_loop or args.serve_webhooks:
        enabled_bots = _enabled_channel_bot_specs(_load_channels_config_for_root(args.user_data_root))
        external_bots = [item for item in enabled_bots if str(item.get("channel") or "") != "localweb"]
        if len(external_bots) > 1:
            labels = ", ".join(f"{item['channel']}:{item['bot_id']}" for item in external_bots)
            parser.error(
                "single-service mode now only supports one bot per process. "
                f"Found multiple enabled bots in channels.json: {labels}. "
                "Please split them into separate user_data_root directories and launch with --run-fleet."
            )

    service = CheapClawService(
        user_data_root=args.user_data_root,
        llm_config_path=args.llm_config_path,
        bot_id=args.bot_id,
        bot_display_name=args.bot_display_name,
        fleet_config_path=args.fleet_config_path,
    )

    if args.bootstrap:
        print(json.dumps(service.bootstrap_assets(force=True), ensure_ascii=False, indent=2))
        return 0
    if args.show_runtime:
        print(json.dumps(service.describe_runtime(), ensure_ascii=False, indent=2))
        return 0
    if args.show_panel:
        print(json.dumps(service.panel_store.load_panel(), ensure_ascii=False, indent=2))
        return 0
    if args.show_credentials:
        print(json.dumps(service.credentials_needed(), ensure_ascii=False, indent=2))
        return 0
    if args.run_once:
        print(json.dumps(service.run_once(), ensure_ascii=False, indent=2))
        return 0
    if args.serve_webhooks and not args.run_loop:
        server = service.serve_webhooks(host=args.host, port=args.port)
        _log(f"webhook server started on {args.host}:{args.port}")
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    if args.run_loop:
        server = None
        thread = None
        if args.serve_webhooks:
            server = service.serve_webhooks(host=args.host, port=args.port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            _log(f"webhook server started on {args.host}:{args.port}")
        try:
            _log(f"service loop started with poll_interval={args.poll_interval}s")
            service.run_forever(poll_interval=args.poll_interval)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
