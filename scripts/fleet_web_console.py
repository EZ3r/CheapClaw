#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in os.sys.path:
    os.sys.path.insert(0, str(PACKAGE_PARENT))

try:
    from cheapclaw.scripts import fleet_one_click as cli
except ImportError:
    try:
        from . import fleet_one_click as cli
    except ImportError:
        from scripts import fleet_one_click as cli

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"
PAGE_PATH = WEB_ROOT / "fleet_console.html"
BOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_.-]{1,80})")


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _now_status_error(error: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"status": "error", "error": str(error)}
    payload.update(extra)
    return payload


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class LocalWebBus:
    def __init__(self, *, manifest: Dict[str, Any]):
        runtime_root_raw = str(manifest.get("runtime_root") or "").strip()
        runtime_root = Path(runtime_root_raw).expanduser().resolve() if runtime_root_raw else cli.DEFAULT_RUNTIME_ROOT
        fleet_cfg_raw = str(manifest.get("fleet_config_path") or "").strip()
        if fleet_cfg_raw:
            fleet_cfg_path = Path(fleet_cfg_raw).expanduser().resolve()
        else:
            fleet_cfg_path = (runtime_root / "fleet.generated.json").resolve()
        self.shared_root = fleet_cfg_path.parent / "localweb"
        self.shared_root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.shared_root / "events.jsonl"
        self.events_lock_path = self.shared_root / "events.lock"
        self.conversations_path = self.shared_root / "conversations.json"
        self.events_path.touch(exist_ok=True)
        self.events_lock_path.touch(exist_ok=True)
        if not self.conversations_path.exists():
            self.conversations_path.write_text(
                json.dumps({"version": 1, "conversations": {}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        bots = [item for item in (manifest.get("bots") or []) if isinstance(item, dict)]
        self._localweb_bot_ids: set[str] = {
            str(item.get("bot_id") or "").strip()
            for item in bots
            if str(item.get("channel") or "").strip().lower() == "localweb"
            and bool(item.get("enabled", True))
            and str(item.get("bot_id") or "").strip()
        }
        self._bot_map: Dict[str, str] = {}
        for item in bots:
            bot_id = str(item.get("bot_id") or "").strip()
            display = str(item.get("display_name") or "").strip()
            if bot_id and bot_id in self._localweb_bot_ids:
                self._bot_map[bot_id.lower()] = bot_id
                if display:
                    self._bot_map[display.lower()] = bot_id

    def _load_conversations(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.conversations_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {"version": 1, "conversations": {}}
        if not isinstance(payload, dict):
            payload = {"version": 1, "conversations": {}}
        conversations = payload.get("conversations")
        if not isinstance(conversations, dict):
            conversations = {}
        normalized: Dict[str, Dict[str, Any]] = {}
        for raw_id, item in conversations.items():
            conv_id = str(raw_id or "").strip()
            if not conv_id or not isinstance(item, dict):
                continue
            participants: List[str] = []
            for bot_id in item.get("participant_bots", []):
                text = str(bot_id or "").strip()
                if text and text in self._localweb_bot_ids and text not in participants:
                    participants.append(text)
            normalized[conv_id] = {
                "conversation_id": conv_id,
                "display_name": str(item.get("display_name") or conv_id).strip() or conv_id,
                "conversation_type": "group" if str(item.get("conversation_type") or "group").strip() == "group" else "person",
                "participant_bots": participants,
                "require_mention": bool(item.get("require_mention", True)),
                "created_at": str(item.get("created_at") or _now_iso()),
                "updated_at": str(item.get("updated_at") or _now_iso()),
                "created_by": str(item.get("created_by") or "system"),
            }
        payload["version"] = 1
        payload["conversations"] = normalized
        return payload

    def _save_conversations(self, payload: Dict[str, Any]) -> None:
        self.conversations_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_conversations(self) -> List[Dict[str, Any]]:
        payload = self._load_conversations()
        items = [item for item in payload.get("conversations", {}).values() if isinstance(item, dict)]
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items

    def ensure_conversation(
        self,
        *,
        conversation_id: str = "",
        display_name: str = "",
        conversation_type: str = "",
        participant_bots: Optional[List[str]] = None,
        require_mention: Optional[bool] = None,
        created_by: str = "web_console",
    ) -> Dict[str, Any]:
        conv_id = str(conversation_id or "").strip() or f"lc_{uuid.uuid4().hex[:12]}"
        payload = self._load_conversations()
        conversations = payload.setdefault("conversations", {})
        current = conversations.get(conv_id) if isinstance(conversations.get(conv_id), dict) else {}
        if not current:
            current = {
                "conversation_id": conv_id,
                "display_name": conv_id,
                "conversation_type": "group",
                "participant_bots": [],
                "require_mention": True,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "created_by": created_by,
            }
        if str(display_name or "").strip():
            current["display_name"] = str(display_name).strip()
        if str(conversation_type or "").strip() in {"group", "person"}:
            current["conversation_type"] = str(conversation_type).strip()
        if require_mention is not None:
            current["require_mention"] = bool(require_mention)
        members = list(current.get("participant_bots") or [])
        if participant_bots is not None:
            members = []
            for item in participant_bots:
                text = str(item or "").strip()
                if text and text in self._localweb_bot_ids and text not in members:
                    members.append(text)
        current["participant_bots"] = members
        if current["conversation_type"] == "person" and len(members) > 1:
            current["conversation_type"] = "group"
        if current["conversation_type"] == "group" and len(members) <= 1:
            current["conversation_type"] = "person"
        current["updated_at"] = _now_iso()
        conversations[conv_id] = current
        self._save_conversations(payload)
        return current

    def _resolve_mentions(self, text: str) -> List[str]:
        mentions: List[str] = []
        for token in MENTION_PATTERN.findall(str(text or "")):
            key = str(token or "").strip().lower()
            if not key:
                continue
            resolved = self._bot_map.get(key)
            if resolved and resolved in self._localweb_bot_ids and resolved not in mentions:
                mentions.append(resolved)
        return mentions

    def _append_event(self, payload: Dict[str, Any]) -> None:
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

    def emit_user_message(
        self,
        *,
        conversation_id: str,
        message: str,
        sender_id: str = "web_user",
        sender_name: str = "web_user",
        participant_bots: Optional[List[str]] = None,
        require_mention: Optional[bool] = None,
    ) -> Dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            return _now_status_error("message is required")
        mentions = self._resolve_mentions(text)
        merged = list(participant_bots or [])
        for item in mentions:
            if item not in merged:
                merged.append(item)
        conv = self.ensure_conversation(
            conversation_id=conversation_id,
            participant_bots=merged if merged else None,
            require_mention=require_mention,
            conversation_type="group" if len(merged) > 1 else "person",
            created_by=sender_id,
        )
        event = {
            "event_id": f"lwevt_{uuid.uuid4().hex[:12]}",
            "channel": "localweb",
            "conversation_id": str(conv.get("conversation_id") or ""),
            "conversation_type": str(conv.get("conversation_type") or "group"),
            "display_name": str(conv.get("display_name") or conv.get("conversation_id") or ""),
            "participant_bots": list(conv.get("participant_bots") or []),
            "require_mention": bool(conv.get("require_mention", True)),
            "sender_id": str(sender_id or "web_user"),
            "sender_name": str(sender_name or sender_id or "web_user"),
            "sender_type": "user",
            "message_text": text,
            "attachments": [],
            "mentions": mentions,
            "timestamp": _now_iso(),
        }
        self._append_event(event)
        return {"status": "success", "event": event, "conversation": conv}

    def recent_events(self, *, conversation_id: str = "", limit: int = 120) -> Dict[str, Any]:
        lines = self.events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        items: List[Dict[str, Any]] = []
        conv_id = str(conversation_id or "").strip()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("channel") or "") != "localweb":
                continue
            if conv_id and str(payload.get("conversation_id") or "") != conv_id:
                continue
            items.append(payload)
            if len(items) >= max(1, int(limit)):
                break
        items.reverse()
        return {
            "status": "success",
            "conversation_id": conv_id,
            "count": len(items),
            "events": items,
        }

class FleetConsoleService:
    def __init__(self, *, manifest_path: Path, host: str, port: int):
        self.manifest_path = manifest_path.expanduser().resolve()
        self.host = str(host or "127.0.0.1")
        self.port = int(port)

    def _localweb_bus(self) -> LocalWebBus:
        manifest = self._load_manifest()
        return LocalWebBus(manifest=manifest)

    def _manifest_exists(self) -> bool:
        return self.manifest_path.exists()

    def _load_manifest(self) -> Dict[str, Any]:
        return cli._load_json(self.manifest_path)

    def _save_manifest(self, payload: Dict[str, Any]) -> None:
        cli._dump_json(self.manifest_path, payload)

    def _enabled_localweb_bot_ids(self) -> List[str]:
        manifest = self._load_manifest()
        ids: List[str] = []
        for item in (manifest.get("bots") or []):
            if not isinstance(item, dict):
                continue
            if not bool(item.get("enabled", True)):
                continue
            if str(item.get("channel") or "").strip().lower() != "localweb":
                continue
            bot_id = str(item.get("bot_id") or "").strip()
            if bot_id and bot_id not in ids:
                ids.append(bot_id)
        return ids

    def _enabled_localweb_bot_aliases(self) -> Dict[str, str]:
        manifest = self._load_manifest()
        aliases: Dict[str, str] = {}
        for item in (manifest.get("bots") or []):
            if not isinstance(item, dict):
                continue
            if not bool(item.get("enabled", True)):
                continue
            if str(item.get("channel") or "").strip().lower() != "localweb":
                continue
            bot_id = str(item.get("bot_id") or "").strip()
            display_name = str(item.get("display_name") or "").strip()
            if bot_id:
                aliases[bot_id.lower()] = bot_id
            if display_name and bot_id:
                aliases[display_name.lower()] = bot_id
        return aliases

    def _resolve_requested_localweb_bots(self, raw_participant_bots: Any) -> tuple[Optional[List[str]], List[str]]:
        if raw_participant_bots is None:
            return None, []
        if isinstance(raw_participant_bots, str):
            source_values = [item.strip() for item in raw_participant_bots.split(",") if item.strip()]
        elif isinstance(raw_participant_bots, list):
            source_values = [str(item).strip() for item in raw_participant_bots if str(item).strip()]
        else:
            source_values = []
        requested = [
            str(item).strip()
            for item in source_values
            if str(item).strip()
        ]
        alias_map = self._enabled_localweb_bot_aliases()
        participant_bots: List[str] = []
        ignored: List[str] = []
        for item in requested:
            resolved = alias_map.get(str(item).lower())
            if resolved and resolved not in participant_bots:
                participant_bots.append(resolved)
                continue
            if not resolved and item not in ignored:
                ignored.append(item)
        return participant_bots, ignored

    def _signal_bot_monitor(self, bot_id: str) -> Dict[str, Any]:
        bot = str(bot_id or "").strip()
        if not bot:
            return {"bot_id": "", "status": "error", "error": "empty bot_id"}
        manifest = self._load_manifest()
        runtime_root_raw = str(manifest.get("runtime_root") or "").strip()
        runtime_root = Path(runtime_root_raw).expanduser().resolve() if runtime_root_raw else cli.DEFAULT_RUNTIME_ROOT
        state_path = runtime_root / bot / "cheapclaw" / "runtime" / "state.json"
        payload = cli._safe_load_json(state_path, {})
        pid = int((((payload.get("bot") or {}) if isinstance(payload.get("bot"), dict) else {}).get("pid")) or 0)
        user_data_root = runtime_root / bot

        def _find_live_pid() -> int:
            try:
                proc = subprocess.run(
                    ["ps", "-eo", "pid=,command="],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except Exception:
                return 0
            if proc.returncode != 0:
                return 0
            needle_1 = "cheapclaw_service.py"
            needle_2 = f"--bot-id {bot}"
            needle_3 = f"--user-data-root {str(user_data_root)}"
            for line in (proc.stdout or "").splitlines():
                text = str(line or "").strip()
                if not text:
                    continue
                if needle_1 not in text or needle_2 not in text or needle_3 not in text:
                    continue
                parts = text.split(None, 1)
                if not parts:
                    continue
                try:
                    return int(parts[0])
                except Exception:
                    continue
            return 0

        if pid <= 0 or not cli._is_pid_alive(pid):
            pid = _find_live_pid()
            if pid > 0:
                state = payload if isinstance(payload, dict) else {}
                bot_state = state.get("bot") if isinstance(state.get("bot"), dict) else {}
                bot_state["pid"] = int(pid)
                state["bot"] = bot_state
                try:
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
        if pid <= 0:
            return {"bot_id": bot, "status": "error", "error": "pid not found"}
        try:
            os.kill(pid, signal.SIGUSR1)
            return {"bot_id": bot, "status": "success", "pid": pid}
        except Exception as exc:
            return {"bot_id": bot, "status": "error", "pid": pid, "error": str(exc)}

    def _status_all(self) -> Dict[str, Any]:
        try:
            payload = cli._status_all_bots_from_manifest(self.manifest_path)
            manifest = self._load_manifest()
        except SystemExit as exc:
            return _now_status_error(str(exc), manifest_path=str(self.manifest_path))
        bots = payload.get("bots") if isinstance(payload.get("bots"), list) else []
        bot_cfg_map = {
            str(item.get("bot_id") or "").strip(): item
            for item in (manifest.get("bots") if isinstance(manifest.get("bots"), list) else [])
            if isinstance(item, dict)
        }
        pending_total = 0
        for item in bots:
            pending_total += int(item.get("pending_instruction_count") or 0)
            bot_id = str(item.get("bot_id") or "").strip()
            cfg = bot_cfg_map.get(bot_id, {})
            if bool(cfg.get("serve_webhooks", False)):
                item["endpoint_url"] = (
                    f"http://{str(cfg.get('host') or '127.0.0.1')}:{int(cfg.get('port') or 8765)}"
                )
            else:
                item["endpoint_url"] = ""
        payload["pending_instruction_total"] = pending_total
        payload["manifest_path"] = str(self.manifest_path)
        payload["web_console"] = {
            "host": self.host,
            "port": self.port,
            "url": f"http://{self.host}:{self.port}/dashboard",
        }
        return payload

    def health(self) -> Dict[str, Any]:
        return {
            "status": "success",
            "manifest_path": str(self.manifest_path),
            "web_console_url": f"http://{self.host}:{self.port}/dashboard",
        }

    def localchat_conversations_payload(self) -> Dict[str, Any]:
        if not self._manifest_exists():
            return _now_status_error(f"manifest not found: {self.manifest_path}", manifest_path=str(self.manifest_path))
        try:
            bus = self._localweb_bus()
            items = bus.list_conversations()
        except SystemExit as exc:
            return _now_status_error(str(exc))
        return {
            "status": "success",
            "conversation_count": len(items),
            "conversations": items,
        }

    def localchat_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._manifest_exists():
            return _now_status_error(f"manifest not found: {self.manifest_path}", manifest_path=str(self.manifest_path))
        try:
            participant_bots, ignored = self._resolve_requested_localweb_bots(payload.get("participant_bots"))
            bus = self._localweb_bus()
            conv = bus.ensure_conversation(
                conversation_id=str(payload.get("conversation_id") or "").strip(),
                display_name=str(payload.get("display_name") or "").strip(),
                conversation_type=str(payload.get("conversation_type") or "").strip(),
                participant_bots=participant_bots,
                require_mention=(
                    bool(payload.get("require_mention"))
                    if "require_mention" in payload
                    else None
                ),
                created_by=str(payload.get("created_by") or "web_console"),
            )
        except SystemExit as exc:
            return _now_status_error(str(exc))
        return {
            "status": "success",
            "conversation": conv,
            "ignored_participant_bots": ignored,
        }

    def localchat_send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._manifest_exists():
            return _now_status_error(f"manifest not found: {self.manifest_path}", manifest_path=str(self.manifest_path))
        conversation_id = str(payload.get("conversation_id") or "").strip()
        message = str(payload.get("message") or "").strip()
        if not conversation_id or not message:
            return _now_status_error("conversation_id and message are required")
        try:
            supported = set(self._enabled_localweb_bot_ids())
            participant_bots, ignored = self._resolve_requested_localweb_bots(
                payload.get("participant_bots") if "participant_bots" in payload else None
            )
            bus = self._localweb_bus()
            result = bus.emit_user_message(
                conversation_id=conversation_id,
                message=message,
                sender_id=str(payload.get("sender_id") or "web_user"),
                sender_name=str(payload.get("sender_name") or "web_user"),
                participant_bots=participant_bots,
                require_mention=(
                    bool(payload.get("require_mention"))
                    if "require_mention" in payload
                    else None
                ),
            )
        except SystemExit as exc:
            return _now_status_error(str(exc))
        if str(result.get("status") or "") != "success":
            return result
        event = result.get("event") if isinstance(result.get("event"), dict) else {}
        participant_targets: List[str] = []
        for item in (event.get("participant_bots") or []):
            bot = str(item or "").strip()
            if bot and bot not in participant_targets and bot in supported:
                participant_targets.append(bot)
        mention_targets: List[str] = []
        for item in (event.get("mentions") or []):
            bot = str(item or "").strip()
            if bot and bot not in mention_targets and bot in supported:
                mention_targets.append(bot)
        targets = mention_targets if mention_targets else participant_targets
        monitor_results = [self._signal_bot_monitor(bot) for bot in targets]
        return {
            **result,
            "ignored_participant_bots": ignored,
            "monitor_triggered": monitor_results,
        }

    def localchat_events(self, *, conversation_id: str = "", limit: int = 120) -> Dict[str, Any]:
        if not self._manifest_exists():
            return _now_status_error(f"manifest not found: {self.manifest_path}", manifest_path=str(self.manifest_path))
        try:
            bus = self._localweb_bus()
            return bus.recent_events(conversation_id=conversation_id, limit=limit)
        except SystemExit as exc:
            return _now_status_error(str(exc))

    def manifest_payload(self) -> Dict[str, Any]:
        if not self._manifest_exists():
            return _now_status_error(f"manifest not found: {self.manifest_path}", manifest_path=str(self.manifest_path))
        try:
            manifest = self._load_manifest()
        except SystemExit as exc:
            return _now_status_error(str(exc), manifest_path=str(self.manifest_path))
        return {
            "status": "success",
            "manifest_path": str(self.manifest_path),
            "manifest": manifest,
            "placeholder_fields": cli._scan_placeholder_fields(manifest),
        }

    def save_manifest_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._manifest_exists():
            return _now_status_error(f"manifest not found: {self.manifest_path}", manifest_path=str(self.manifest_path))
        try:
            manifest = self._load_manifest()
        except SystemExit as exc:
            return _now_status_error(str(exc), manifest_path=str(self.manifest_path))

        runtime_root = str(payload.get("runtime_root") or manifest.get("runtime_root") or "").strip()
        llm_config_path = str(payload.get("llm_config_path") or manifest.get("llm_config_path") or "").strip()
        fleet_config_path = str(payload.get("fleet_config_path") or manifest.get("fleet_config_path") or "").strip()
        if not runtime_root or not llm_config_path or not fleet_config_path:
            return _now_status_error("runtime_root, llm_config_path, fleet_config_path are required")

        manifest["runtime_root"] = runtime_root
        manifest["llm_config_path"] = llm_config_path
        manifest["fleet_config_path"] = fleet_config_path
        proxy_env = payload.get("proxy_env")
        if isinstance(proxy_env, dict):
            manifest["proxy_env"] = {
                "http_proxy": str(proxy_env.get("http_proxy") or "").strip(),
                "https_proxy": str(proxy_env.get("https_proxy") or "").strip(),
                "all_proxy": str(proxy_env.get("all_proxy") or "").strip(),
            }
        context = payload.get("context")
        if isinstance(context, dict):
            manifest["context"] = cli._normalize_context_settings(context)
        self._save_manifest(manifest)
        return {
            "status": "success",
            "manifest_path": str(self.manifest_path),
            "manifest": manifest,
        }

    def llm_config_payload(self) -> Dict[str, Any]:
        manifest_result = self.manifest_payload()
        if manifest_result.get("status") != "success":
            return manifest_result
        manifest = manifest_result.get("manifest") if isinstance(manifest_result.get("manifest"), dict) else {}
        llm_path = Path(str(manifest.get("llm_config_path") or "")).expanduser().resolve()
        content = ""
        if llm_path.exists():
            content = llm_path.read_text(encoding="utf-8", errors="ignore")
        return {
            "status": "success",
            "manifest_path": str(self.manifest_path),
            "llm_config_path": str(llm_path),
            "exists": llm_path.exists(),
            "content": content,
        }

    def save_llm_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        manifest_result = self.manifest_payload()
        if manifest_result.get("status") != "success":
            return manifest_result
        manifest = manifest_result.get("manifest") if isinstance(manifest_result.get("manifest"), dict) else {}
        llm_config_path_raw = str(payload.get("llm_config_path") or manifest.get("llm_config_path") or "").strip()
        if not llm_config_path_raw:
            return _now_status_error("llm_config_path is required")
        llm_path = Path(llm_config_path_raw).expanduser().resolve()
        content = str(payload.get("content") or "")
        llm_path.parent.mkdir(parents=True, exist_ok=True)
        llm_path.write_text(content, encoding="utf-8")
        if str(manifest.get("llm_config_path") or "").strip() != str(llm_path):
            manifest["llm_config_path"] = str(llm_path)
            self._save_manifest(manifest)
        return {
            "status": "success",
            "llm_config_path": str(llm_path),
            "bytes": len(content.encode("utf-8")),
        }

    def _normalize_bot_payload(
        self,
        payload: Dict[str, Any],
        *,
        existing_bot_ids: List[str],
        allow_existing_bot_id: bool,
    ) -> Dict[str, Any]:
        bot_id = str(payload.get("bot_id") or "").strip()
        if not BOT_ID_PATTERN.match(bot_id):
            raise ValueError("bot_id is required and must match [A-Za-z0-9_.-], length <= 80")
        if (not allow_existing_bot_id) and bot_id in existing_bot_ids:
            raise ValueError(f"bot_id already exists: {bot_id}")
        channel = str(payload.get("channel") or "").strip().lower()
        if channel not in {"telegram", "feishu", "whatsapp", "discord", "qq", "wechat", "localweb"}:
            raise ValueError("channel must be one of: telegram, feishu, whatsapp, discord, qq, wechat, localweb")
        display_name = str(payload.get("display_name") or bot_id).strip() or bot_id
        enabled = bool(payload.get("enabled", True))
        serve_webhooks = bool(payload.get("serve_webhooks", channel in {"localweb", "qq", "wechat"}))
        out: Dict[str, Any] = {
            "bot_id": bot_id,
            "display_name": display_name,
            "channel": channel,
            "enabled": enabled,
            "serve_webhooks": serve_webhooks,
        }
        if serve_webhooks:
            out["host"] = str(payload.get("host") or "127.0.0.1").strip() or "127.0.0.1"
            out["port"] = int(payload.get("port") or 8765)

        if channel == "telegram":
            telegram = payload.get("telegram") if isinstance(payload.get("telegram"), dict) else {}
            token = str(telegram.get("bot_token") or "").strip()
            if not token:
                raise ValueError("telegram.bot_token is required")
            if not cli.TELEGRAM_TOKEN_PATTERN.match(token):
                raise ValueError("invalid telegram.bot_token format")
            chats = telegram.get("allowed_chats")
            if not isinstance(chats, list):
                chats = []
            out["telegram"] = {
                "bot_token": token,
                "allowed_chats": [str(x).strip() for x in chats if str(x).strip()],
            }
        elif channel == "feishu":
            feishu = payload.get("feishu") if isinstance(payload.get("feishu"), dict) else {}
            app_id = str(feishu.get("app_id") or "").strip()
            app_secret = str(feishu.get("app_secret") or "").strip()
            if not app_id or not app_secret:
                raise ValueError("feishu.app_id and feishu.app_secret are required")
            out["feishu"] = {
                "mode": str(feishu.get("mode") or "long_connection").strip() or "long_connection",
                "app_id": app_id,
                "app_secret": app_secret,
                "verify_token": str(feishu.get("verify_token") or "").strip(),
                "encrypt_key": str(feishu.get("encrypt_key") or "").strip(),
            }
        elif channel == "whatsapp":
            wa = payload.get("whatsapp") if isinstance(payload.get("whatsapp"), dict) else {}
            access_token = str(wa.get("access_token") or "").strip()
            phone_number_id = str(wa.get("phone_number_id") or "").strip()
            verify_token = str(wa.get("verify_token") or "").strip()
            if not access_token or not phone_number_id or not verify_token:
                raise ValueError("whatsapp.access_token, whatsapp.phone_number_id, whatsapp.verify_token are required")
            out["whatsapp"] = {
                "access_token": access_token,
                "phone_number_id": phone_number_id,
                "verify_token": verify_token,
                "api_version": str(wa.get("api_version") or "v21.0").strip() or "v21.0",
            }
        elif channel == "discord":
            discord = payload.get("discord") if isinstance(payload.get("discord"), dict) else {}
            token = str(discord.get("bot_token") or "").strip()
            if not token:
                raise ValueError("discord.bot_token is required")
            out["discord"] = {
                "bot_token": token,
                "intents": int(discord.get("intents") or 37377),
                "require_mention_in_guild": bool(discord.get("require_mention_in_guild", True)),
            }
        elif channel in {"qq", "wechat"}:
            bridge = payload.get(channel) if isinstance(payload.get(channel), dict) else {}
            api_base = str(bridge.get("onebot_api_base") or "").strip()
            if not api_base:
                raise ValueError(f"{channel}.onebot_api_base is required")
            out[channel] = {
                "onebot_api_base": api_base,
                "onebot_access_token": str(bridge.get("onebot_access_token") or "").strip(),
                "onebot_post_secret": str(bridge.get("onebot_post_secret") or "").strip(),
                "onebot_self_id": str(bridge.get("onebot_self_id") or "").strip(),
                "require_mention_in_group": bool(bridge.get("require_mention_in_group", True)),
            }
        else:
            local = payload.get("localweb") if isinstance(payload.get("localweb"), dict) else {}
            out["localweb"] = {
                "require_mention_in_group": bool(local.get("require_mention_in_group", True)),
            }
        return out

    def list_bots_payload(self) -> Dict[str, Any]:
        manifest_result = self.manifest_payload()
        if manifest_result.get("status") != "success":
            return manifest_result
        manifest = manifest_result.get("manifest") if isinstance(manifest_result.get("manifest"), dict) else {}
        bots = [item for item in manifest.get("bots", []) if isinstance(item, dict)]
        return {
            "status": "success",
            "manifest_path": str(self.manifest_path),
            "bot_count": len(bots),
            "bots": bots,
        }

    def add_bot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        manifest_result = self.manifest_payload()
        if manifest_result.get("status") != "success":
            return manifest_result
        manifest = manifest_result.get("manifest") if isinstance(manifest_result.get("manifest"), dict) else {}
        bots = manifest.get("bots")
        if not isinstance(bots, list):
            bots = []
            manifest["bots"] = bots
        existing_ids = [str(item.get("bot_id") or "").strip() for item in bots if isinstance(item, dict)]
        try:
            new_bot = self._normalize_bot_payload(
                payload,
                existing_bot_ids=existing_ids,
                allow_existing_bot_id=False,
            )
        except ValueError as exc:
            return _now_status_error(str(exc))
        bots.append(new_bot)
        self._save_manifest(manifest)
        prepare_result: Dict[str, Any] = {}
        try:
            prepare_result = cli.prepare_from_manifest(self.manifest_path, write_fleet_to=None)
        except SystemExit as exc:
            return _now_status_error(f"manifest saved but prepare failed: {exc}")
        return {
            "status": "success",
            "manifest_path": str(self.manifest_path),
            "bot": new_bot,
            "bot_count": len(bots),
            "prepare": prepare_result,
        }

    def update_bot(self, bot_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        target_bot_id = str(bot_id or "").strip()
        if not target_bot_id:
            return _now_status_error("bot_id is required")
        manifest_result = self.manifest_payload()
        if manifest_result.get("status") != "success":
            return manifest_result
        manifest = manifest_result.get("manifest") if isinstance(manifest_result.get("manifest"), dict) else {}
        bots = manifest.get("bots")
        if not isinstance(bots, list):
            return _now_status_error("manifest.bots is invalid")
        target_index = -1
        current: Dict[str, Any] = {}
        for idx, item in enumerate(bots):
            if isinstance(item, dict) and str(item.get("bot_id") or "").strip() == target_bot_id:
                target_index = idx
                current = item
                break
        if target_index < 0:
            return _now_status_error(f"bot not found: {target_bot_id}")
        merged = {**current, **payload, "bot_id": target_bot_id}
        existing_ids = [str(item.get("bot_id") or "").strip() for item in bots if isinstance(item, dict)]
        try:
            normalized = self._normalize_bot_payload(
                merged,
                existing_bot_ids=[item for item in existing_ids if item != target_bot_id],
                allow_existing_bot_id=True,
            )
        except ValueError as exc:
            return _now_status_error(str(exc))
        bots[target_index] = normalized
        self._save_manifest(manifest)
        prepare_result: Dict[str, Any] = {}
        try:
            prepare_result = cli.prepare_from_manifest(self.manifest_path, write_fleet_to=None)
        except SystemExit as exc:
            return _now_status_error(f"manifest saved but prepare failed: {exc}")
        return {
            "status": "success",
            "manifest_path": str(self.manifest_path),
            "bot": normalized,
            "prepare": prepare_result,
        }

    def delete_bot(self, bot_id: str) -> Dict[str, Any]:
        target_bot_id = str(bot_id or "").strip()
        if not target_bot_id:
            return _now_status_error("bot_id is required")
        manifest_result = self.manifest_payload()
        if manifest_result.get("status") != "success":
            return manifest_result
        manifest = manifest_result.get("manifest") if isinstance(manifest_result.get("manifest"), dict) else {}
        bots = manifest.get("bots")
        if not isinstance(bots, list):
            return _now_status_error("manifest.bots is invalid")
        next_bots = [
            item for item in bots
            if not (isinstance(item, dict) and str(item.get("bot_id") or "").strip() == target_bot_id)
        ]
        if len(next_bots) == len(bots):
            return _now_status_error(f"bot not found: {target_bot_id}")
        manifest["bots"] = next_bots
        self._save_manifest(manifest)
        prepare_result: Dict[str, Any] = {}
        try:
            prepare_result = cli.prepare_from_manifest(self.manifest_path, write_fleet_to=None)
        except SystemExit as exc:
            return _now_status_error(f"manifest saved but prepare failed: {exc}")
        return {
            "status": "success",
            "manifest_path": str(self.manifest_path),
            "deleted_bot_id": target_bot_id,
            "bot_count": len(next_bots),
            "prepare": prepare_result,
        }

    def _maybe_prepare(self, prepare_first: bool) -> Optional[Dict[str, Any]]:
        if not bool(prepare_first):
            return None
        try:
            return cli.prepare_from_manifest(self.manifest_path, write_fleet_to=None)
        except SystemExit as exc:
            return _now_status_error(str(exc))

    def prepare(self) -> Dict[str, Any]:
        try:
            return cli.prepare_from_manifest(self.manifest_path, write_fleet_to=None)
        except SystemExit as exc:
            return _now_status_error(str(exc), manifest_path=str(self.manifest_path))

    def start(self, *, bot_id: str = "", poll_interval: int = 5, prepare_first: bool = True) -> Dict[str, Any]:
        pre = self._maybe_prepare(prepare_first)
        if isinstance(pre, dict) and pre.get("status") == "error":
            return pre
        try:
            if str(bot_id or "").strip():
                result = cli._start_single_bot_from_manifest(
                    self.manifest_path,
                    bot_id=str(bot_id).strip(),
                    poll_interval=int(poll_interval),
                )
            else:
                result = cli._start_all_bots_from_manifest(
                    self.manifest_path,
                    poll_interval=int(poll_interval),
                )
        except SystemExit as exc:
            return _now_status_error(str(exc))
        return {"status": "success", "prepare": pre or {}, "result": result, "fleet": self._status_all()}

    def stop(self, *, bot_id: str = "", timeout_sec: int = 8) -> Dict[str, Any]:
        try:
            if str(bot_id or "").strip():
                result = cli._stop_single_bot_from_manifest(
                    self.manifest_path,
                    bot_id=str(bot_id).strip(),
                    timeout_sec=int(timeout_sec),
                )
            else:
                result = cli._stop_all_bots_from_manifest(
                    self.manifest_path,
                    timeout_sec=int(timeout_sec),
                )
        except SystemExit as exc:
            return _now_status_error(str(exc))
        return {"status": "success", "result": result, "fleet": self._status_all()}

    def reload_bot(self, *, bot_id: str, poll_interval: int = 5, timeout_sec: int = 8, prepare_first: bool = True) -> Dict[str, Any]:
        target = str(bot_id or "").strip()
        if not target:
            return _now_status_error("bot_id is required")
        pre = self._maybe_prepare(prepare_first)
        if isinstance(pre, dict) and pre.get("status") == "error":
            return pre
        try:
            stop_result = cli._stop_single_bot_from_manifest(
                self.manifest_path,
                bot_id=target,
                timeout_sec=int(timeout_sec),
            )
            start_result = cli._start_single_bot_from_manifest(
                self.manifest_path,
                bot_id=target,
                poll_interval=int(poll_interval),
            )
        except SystemExit as exc:
            return _now_status_error(str(exc))
        return {
            "status": "success",
            "prepare": pre or {},
            "stop": stop_result,
            "start": start_result,
            "fleet": self._status_all(),
        }

    def restart(self, *, bot_id: str = "", poll_interval: int = 5, timeout_sec: int = 8, prepare_first: bool = True) -> Dict[str, Any]:
        pre = self._maybe_prepare(prepare_first)
        if isinstance(pre, dict) and pre.get("status") == "error":
            return pre
        try:
            if str(bot_id or "").strip():
                stop_result = cli._stop_single_bot_from_manifest(
                    self.manifest_path,
                    bot_id=str(bot_id).strip(),
                    timeout_sec=int(timeout_sec),
                )
                start_result = cli._start_single_bot_from_manifest(
                    self.manifest_path,
                    bot_id=str(bot_id).strip(),
                    poll_interval=int(poll_interval),
                )
            else:
                stop_result = cli._stop_all_bots_from_manifest(
                    self.manifest_path,
                    timeout_sec=int(timeout_sec),
                )
                start_result = cli._start_all_bots_from_manifest(
                    self.manifest_path,
                    poll_interval=int(poll_interval),
                )
        except SystemExit as exc:
            return _now_status_error(str(exc))
        return {
            "status": "success",
            "prepare": pre or {},
            "stop": stop_result,
            "start": start_result,
            "fleet": self._status_all(),
        }


class FleetConsoleHandler(BaseHTTPRequestHandler):
    service: FleetConsoleService

    def _send_json(self, payload: Dict[str, Any], *, status: Optional[int] = None) -> None:
        code = status
        if code is None:
            code = 200 if str(payload.get("status") or "") == "success" else 400
        body = _json_bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path in {"", "/", "/dashboard"}:
            if PAGE_PATH.exists():
                body = PAGE_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404, "fleet console page not found")
            return
        if path == "/api/health":
            self._send_json(self.service.health())
            return
        if path == "/api/manifest":
            self._send_json(self.service.manifest_payload())
            return
        if path == "/api/status":
            self._send_json(self.service._status_all())
            return
        if path == "/api/bots":
            self._send_json(self.service.list_bots_payload())
            return
        if path == "/api/llm-config":
            self._send_json(self.service.llm_config_payload())
            return
        if path == "/api/localchat/conversations":
            self._send_json(self.service.localchat_conversations_payload())
            return
        if path == "/api/localchat/events":
            query = parse_qs(parsed.query)
            conversation_id = str((query.get("conversation_id") or [""])[0] or "").strip()
            limit = max(20, int((query.get("limit") or ["120"])[0] or "120"))
            self._send_json(self.service.localchat_events(conversation_id=conversation_id, limit=limit))
            return
        if path == "/api/log-tail":
            query = parse_qs(parsed.query)
            bot_id = str((query.get("bot_id") or [""])[0] or "").strip()
            kind = str((query.get("kind") or ["loop"])[0] or "loop").strip()
            lines = max(20, int((query.get("lines") or ["120"])[0] or "120"))
            if not bot_id:
                self._send_json(_now_status_error("bot_id is required"))
                return
            try:
                status = cli._status_single_bot_from_manifest(self.service.manifest_path, bot_id)
            except SystemExit as exc:
                self._send_json(_now_status_error(str(exc)))
                return
            key = "loop_log_path" if kind != "service" else "service_log_path"
            log_path = Path(str(status.get(key) or "")).expanduser().resolve()
            if not log_path.exists():
                self._send_json({
                    "status": "success",
                    "bot_id": bot_id,
                    "kind": kind,
                    "log_path": str(log_path),
                    "content": "",
                })
                return
            content_lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            content = "\n".join(content_lines[-lines:])
            self._send_json({
                "status": "success",
                "bot_id": bot_id,
                "kind": kind,
                "log_path": str(log_path),
                "content": content,
            })
            return
        self.send_error(404)

    def _dispatch_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        payload = self._read_json_body()

        if path == "/api/manifest/save":
            self._send_json(self.service.save_manifest_settings(payload))
            return
        if path == "/api/llm-config/save":
            self._send_json(self.service.save_llm_config(payload))
            return
        if path == "/api/localchat/create":
            self._send_json(self.service.localchat_create(payload))
            return
        if path == "/api/localchat/send":
            self._send_json(self.service.localchat_send(payload))
            return
        if path == "/api/bots/add":
            bot = payload.get("bot") if isinstance(payload.get("bot"), dict) else payload
            self._send_json(self.service.add_bot(bot))
            return
        if path == "/api/bots/update":
            bot_id = str(payload.get("bot_id") or "").strip()
            patch = payload.get("bot") if isinstance(payload.get("bot"), dict) else payload
            self._send_json(self.service.update_bot(bot_id, patch))
            return
        if path == "/api/bots/delete":
            self._send_json(self.service.delete_bot(str(payload.get("bot_id") or "").strip()))
            return
        if path == "/api/prepare":
            self._send_json(self.service.prepare())
            return
        if path == "/api/start":
            self._send_json(
                self.service.start(
                    bot_id=str(payload.get("bot_id") or "").strip(),
                    poll_interval=int(payload.get("poll_interval") or 5),
                    prepare_first=bool(payload.get("prepare_first", True)),
                )
            )
            return
        if path == "/api/stop":
            self._send_json(
                self.service.stop(
                    bot_id=str(payload.get("bot_id") or "").strip(),
                    timeout_sec=int(payload.get("timeout_sec") or 8),
                )
            )
            return
        if path == "/api/restart":
            self._send_json(
                self.service.restart(
                    bot_id=str(payload.get("bot_id") or "").strip(),
                    poll_interval=int(payload.get("poll_interval") or 5),
                    timeout_sec=int(payload.get("timeout_sec") or 8),
                    prepare_first=bool(payload.get("prepare_first", True)),
                )
            )
            return
        if path == "/api/reload":
            self._send_json(
                self.service.reload_bot(
                    bot_id=str(payload.get("bot_id") or "").strip(),
                    poll_interval=int(payload.get("poll_interval") or 5),
                    timeout_sec=int(payload.get("timeout_sec") or 8),
                    prepare_first=bool(payload.get("prepare_first", True)),
                )
            )
            return
        self.send_error(404)

    def do_GET(self) -> None:
        self._dispatch_get()

    def do_POST(self) -> None:
        self._dispatch_post()

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def run_server(*, manifest_path: Path, host: str, port: int) -> int:
    service = FleetConsoleService(manifest_path=manifest_path, host=host, port=port)
    handler_cls = type("FleetConsoleHandlerBound", (FleetConsoleHandler,), {})
    handler_cls.service = service
    server = ThreadingHTTPServer((host, int(port)), handler_cls)
    print(json.dumps(
        {
            "status": "success",
            "output": "fleet web console started",
            "manifest_path": str(manifest_path),
            "url": f"http://{host}:{int(port)}/dashboard",
        },
        ensure_ascii=False,
    ))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CheapClaw fleet web console")
    parser.add_argument("--manifest", default=str(cli.DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    parser.add_argument("--port", type=int, default=8787, help="Port to bind")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest_path = Path(args.manifest).expanduser().resolve()
    host = str(args.host or "127.0.0.1").strip() or "127.0.0.1"
    port = int(args.port or 8787)
    return run_server(manifest_path=manifest_path, host=host, port=port)


if __name__ == "__main__":
    raise SystemExit(main())
