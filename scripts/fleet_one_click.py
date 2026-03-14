#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = REPO_ROOT / "cheapclaw_service.py"
FLEET_WEB_CONSOLE_PATH = REPO_ROOT / "scripts" / "fleet_web_console.py"
TEMPLATE_MANIFEST_PATH = REPO_ROOT / "assets" / "config" / "fleet.manifest.example.json"
AGENT_LIBRARY_ROOT = REPO_ROOT / "assets" / "agent_library"
DEFAULT_HOME_ROOT = (Path.home() / "cheapclaw").resolve()
DEFAULT_RUNTIME_ROOT = (DEFAULT_HOME_ROOT / "runtime").resolve()
DEFAULT_MANIFEST_PATH = (DEFAULT_HOME_ROOT / "fleet.manifest.json").resolve()
PLACEHOLDER_SNIPPETS = (
    "/ABS/PATH/TO/",
    "/ABS/PATH/TO",
    "telegram_token_here",
    "cli_xxx",
)
TELEGRAM_TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")
PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


def _is_placeholder_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if any(snippet in value for snippet in PLACEHOLDER_SNIPPETS):
        return True
    return value in {"xxx", "your_token_here", "your_app_id", "your_app_secret"}


def _child_process_env(manifest: Dict[str, Any]) -> Dict[str, str]:
    env = os.environ.copy()
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)

    proxy_env = manifest.get("proxy_env")
    if not isinstance(proxy_env, dict):
        return env

    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        value = str(proxy_env.get(key) or "").strip()
        if not value:
            continue
        env[key] = value
        env[key.upper()] = value
    return env


def _bot_seed_is_placeholder(bot: Dict[str, Any]) -> bool:
    channel = str(bot.get("channel") or "").strip().lower()
    if channel == "localweb":
        return False
    if channel == "telegram":
        block = bot.get("telegram") if isinstance(bot.get("telegram"), dict) else {}
        token = str(block.get("bot_token") or "").strip()
        return (not token) or _is_placeholder_text(token)
    if channel == "feishu":
        block = bot.get("feishu") if isinstance(bot.get("feishu"), dict) else {}
        app_id = str(block.get("app_id") or "").strip()
        app_secret = str(block.get("app_secret") or "").strip()
        return (not app_id) or (not app_secret) or _is_placeholder_text(app_id) or _is_placeholder_text(app_secret)
    if channel == "whatsapp":
        block = bot.get("whatsapp") if isinstance(bot.get("whatsapp"), dict) else {}
        access_token = str(block.get("access_token") or "").strip()
        phone_number_id = str(block.get("phone_number_id") or "").strip()
        verify_token = str(block.get("verify_token") or "").strip()
        return (
            (not access_token)
            or (not phone_number_id)
            or (not verify_token)
            or _is_placeholder_text(access_token)
            or _is_placeholder_text(phone_number_id)
            or _is_placeholder_text(verify_token)
        )
    if channel == "discord":
        block = bot.get("discord") if isinstance(bot.get("discord"), dict) else {}
        token = str(block.get("bot_token") or "").strip()
        return (not token) or _is_placeholder_text(token)
    if channel in {"qq", "wechat"}:
        block = bot.get(channel) if isinstance(bot.get(channel), dict) else {}
        api_base = str(block.get("onebot_api_base") or "").strip()
        return (not api_base) or _is_placeholder_text(api_base)
    return True


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"[ERROR] Failed to parse JSON: {path} ({exc})")
    if not isinstance(payload, dict):
        raise SystemExit(f"[ERROR] JSON root must be an object: {path}")
    return payload


def _dump_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _disabled_channels() -> Dict[str, Any]:
    return {
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "allowed_chats": [],
        },
        "feishu": {
            "enabled": False,
            "mode": "long_connection",
            "app_id": "",
            "app_secret": "",
            "verify_token": "",
            "encrypt_key": "",
        },
        "whatsapp": {
            "enabled": False,
            "access_token": "",
            "phone_number_id": "",
            "verify_token": "",
            "api_version": "v21.0",
        },
        "discord": {
            "enabled": False,
            "bot_id": "",
            "display_name": "",
            "bot_token": "",
            "intents": 37377,
            "require_mention_in_guild": True,
        },
        "qq": {
            "enabled": False,
            "bot_id": "",
            "display_name": "",
            "onebot_api_base": "http://127.0.0.1:5700",
            "onebot_access_token": "",
            "onebot_post_secret": "",
            "onebot_self_id": "",
            "require_mention_in_group": True,
        },
        "wechat": {
            "enabled": False,
            "bot_id": "",
            "display_name": "",
            "onebot_api_base": "http://127.0.0.1:5701",
            "onebot_access_token": "",
            "onebot_post_secret": "",
            "onebot_self_id": "",
            "require_mention_in_group": True,
        },
        "localweb": {
            "enabled": False,
            "bot_id": "",
            "display_name": "",
            "require_mention_in_group": True,
        },
    }


def _required_text(value: Any, field_name: str, bot_id: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SystemExit(f"[ERROR] Missing '{field_name}' for bot '{bot_id}'")
    return text


def _build_channels_config(bot: Dict[str, Any]) -> Dict[str, Any]:
    channel = str(bot.get("channel") or "").strip().lower()
    bot_id = _required_text(bot.get("bot_id"), "bot_id", "<unknown>")
    display_name = str(bot.get("display_name") or bot_id).strip() or bot_id
    cfg = _disabled_channels()

    if channel == "telegram":
        block = bot.get("telegram") if isinstance(bot.get("telegram"), dict) else {}
        token = _required_text(block.get("bot_token"), "telegram.bot_token", bot_id)
        if not TELEGRAM_TOKEN_PATTERN.match(token):
            raise SystemExit(
                "[ERROR] Invalid telegram.bot_token format for bot "
                f"'{bot_id}'. Expected full BotFather token like "
                "'123456789:AA...'."
            )
        allowed_chats = block.get("allowed_chats")
        if not isinstance(allowed_chats, list):
            allowed_chats = []
        cfg["telegram"].update(
            {
                "enabled": True,
                "bot_id": bot_id,
                "display_name": display_name,
                "bot_token": token,
                "allowed_chats": [str(item) for item in allowed_chats if str(item).strip()],
            }
        )
        return cfg

    if channel == "feishu":
        block = bot.get("feishu") if isinstance(bot.get("feishu"), dict) else {}
        app_id = _required_text(block.get("app_id"), "feishu.app_id", bot_id)
        app_secret = _required_text(block.get("app_secret"), "feishu.app_secret", bot_id)
        verify_token = str(block.get("verify_token") or "").strip()
        encrypt_key = str(block.get("encrypt_key") or "").strip()
        mode = str(block.get("mode") or "long_connection").strip() or "long_connection"
        cfg["feishu"].update(
            {
                "enabled": True,
                "bot_id": bot_id,
                "display_name": display_name,
                "mode": mode,
                "app_id": app_id,
                "app_secret": app_secret,
                "verify_token": verify_token,
                "encrypt_key": encrypt_key,
            }
        )
        return cfg

    if channel == "whatsapp":
        block = bot.get("whatsapp") if isinstance(bot.get("whatsapp"), dict) else {}
        access_token = _required_text(block.get("access_token"), "whatsapp.access_token", bot_id)
        phone_number_id = _required_text(block.get("phone_number_id"), "whatsapp.phone_number_id", bot_id)
        verify_token = _required_text(block.get("verify_token"), "whatsapp.verify_token", bot_id)
        api_version = str(block.get("api_version") or "v21.0").strip() or "v21.0"
        cfg["whatsapp"].update(
            {
                "enabled": True,
                "bot_id": bot_id,
                "display_name": display_name,
                "access_token": access_token,
                "phone_number_id": phone_number_id,
                "verify_token": verify_token,
                "api_version": api_version,
            }
        )
        return cfg

    if channel == "discord":
        block = bot.get("discord") if isinstance(bot.get("discord"), dict) else {}
        bot_token = _required_text(block.get("bot_token"), "discord.bot_token", bot_id)
        intents = int(block.get("intents") or 37377)
        cfg["discord"].update(
            {
                "enabled": True,
                "bot_id": bot_id,
                "display_name": display_name,
                "bot_token": bot_token,
                "intents": intents,
                "require_mention_in_guild": bool(block.get("require_mention_in_guild", True)),
            }
        )
        return cfg

    if channel in {"qq", "wechat"}:
        block = bot.get(channel) if isinstance(bot.get(channel), dict) else {}
        onebot_api_base = _required_text(block.get("onebot_api_base"), f"{channel}.onebot_api_base", bot_id)
        cfg[channel].update(
            {
                "enabled": True,
                "bot_id": bot_id,
                "display_name": display_name,
                "onebot_api_base": onebot_api_base,
                "onebot_access_token": str(block.get("onebot_access_token") or "").strip(),
                "onebot_post_secret": str(block.get("onebot_post_secret") or "").strip(),
                "onebot_self_id": str(block.get("onebot_self_id") or "").strip(),
                "require_mention_in_group": bool(block.get("require_mention_in_group", True)),
            }
        )
        return cfg

    if channel == "localweb":
        block = bot.get("localweb") if isinstance(bot.get("localweb"), dict) else {}
        cfg["localweb"].update(
            {
                "enabled": True,
                "bot_id": bot_id,
                "display_name": display_name,
                "require_mention_in_group": bool(block.get("require_mention_in_group", True)),
            }
        )
        return cfg

    raise SystemExit(f"[ERROR] Unsupported channel '{channel}' for bot '{bot_id}'")


def _validate_webhook_ports(bots: List[Dict[str, Any]]) -> None:
    used: Dict[Tuple[str, int], str] = {}
    for bot in bots:
        if not bool(bot.get("serve_webhooks", False)):
            continue
        host = str(bot.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(bot.get("port") or 8765)
        key = (host, port)
        if key in used:
            raise SystemExit(
                f"[ERROR] Webhook port conflict: {host}:{port} used by both '{used[key]}' and '{bot.get('bot_id')}'"
            )
        used[key] = str(bot.get("bot_id") or "")


def _run(cmd: List[str], env: Dict[str, str], check: bool = True) -> int:
    print("[RUN]", " ".join(cmd))
    proc = subprocess.run(cmd, env=env)
    if check and proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return int(proc.returncode)


def _run_capture(cmd: List[str], env: Dict[str, str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit code={proc.returncode}"
        raise SystemExit(f"[ERROR] command failed: {' '.join(cmd)}\n{detail}")
    return proc


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _format_bool(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _format_status_payload(payload: Dict[str, Any]) -> None:
    bots = payload.get("bots") if isinstance(payload.get("bots"), list) else []
    print(f"[STATUS] manifest: {payload.get('manifest_path', '-')}")
    print(f"[STATUS] bots: {len(bots)} total, {payload.get('running_count', 0)} running")
    for item in bots:
        bot_id = str(item.get("bot_id") or "-")
        line = (
            f"- {bot_id} ({item.get('channel') or '-'}) "
            f"enabled={_format_bool(item.get('enabled'))} "
            f"alive={_format_bool(item.get('alive'))} "
            f"pid={item.get('pid') or 0} "
            f"pending={item.get('pending_instruction_count') or 0}"
        )
        print(line)
        print(
            f"  user_data_root={item.get('user_data_root') or '-'} "
            f"main_agent_running={_format_bool(item.get('main_agent_running'))}"
        )


def _format_list_bots_payload(payload: Dict[str, Any]) -> None:
    bots = payload.get("bots") if isinstance(payload.get("bots"), list) else []
    print(f"[BOTS] manifest: {payload.get('manifest_path', '-')}")
    if not bots:
        print("[BOTS] none")
        return
    for item in bots:
        print(
            f"- {item.get('bot_id') or '-'} "
            f"name={item.get('display_name') or '-'} "
            f"channel={item.get('channel') or '-'} "
            f"enabled={_format_bool(item.get('enabled'))} "
            f"webhooks={_format_bool(item.get('serve_webhooks'))}"
        )


def _format_prepare_payload(payload: Dict[str, Any]) -> None:
    print(f"[PREPARE] manifest: {payload.get('manifest_path', '-')}")
    print(f"[PREPARE] runtime_root: {payload.get('runtime_root', '-')}")
    print(f"[PREPARE] fleet_config: {payload.get('fleet_config_path', '-')}")
    bots = payload.get("bots") if isinstance(payload.get("bots"), list) else []
    print(f"[PREPARE] enabled bots: {len(bots)}")
    for item in bots:
        print(
            f"- {item.get('bot_id') or '-'} "
            f"user_data_root={item.get('user_data_root') or '-'} "
            f"webhooks={_format_bool(item.get('serve_webhooks'))}"
        )


def _format_agent_system_payload(payload: Dict[str, Any]) -> None:
    print(f"[AGENT_SYSTEM] status: {payload.get('status', '-')}")
    print(f"[AGENT_SYSTEM] scope: {payload.get('scope', '-')}")
    print(f"[AGENT_SYSTEM] name: {payload.get('system_name', '-')}")
    print(f"[AGENT_SYSTEM] archive: {payload.get('archive_path', '-')}")
    print(f"[AGENT_SYSTEM] target: {payload.get('target_root', '-')}")
    print(f"[AGENT_SYSTEM] files: {payload.get('file_count', 0)}")
    if str(payload.get("bot_id") or "").strip():
        print(f"[AGENT_SYSTEM] bot_id: {payload.get('bot_id')}")
    if str(payload.get("next_step") or "").strip():
        print(f"[NEXT] {payload.get('next_step')}")


def _format_start_stop_payload(payload: Dict[str, Any], *, title: str) -> None:
    if isinstance(payload.get("results"), list):
        print(f"[{title}] manifest: {payload.get('manifest_path', '-')}")
        if any(
            key in payload
            for key in (
                "requested_bot_count",
                "started_count",
                "already_running_count",
                "conflict_count",
            )
        ):
            print(
                f"[{title}] summary: requested={int(payload.get('requested_bot_count') or 0)} "
                f"started={int(payload.get('started_count') or 0)} "
                f"already_running={int(payload.get('already_running_count') or 0)} "
                f"conflict={int(payload.get('conflict_count') or 0)}"
            )
        for item in payload.get("results", []):
            remaining = item.get("remaining_pids") if isinstance(item.get("remaining_pids"), list) else []
            remaining_text = f" remaining={','.join(str(x) for x in remaining)}" if remaining else ""
            error_text = f" error={item.get('error')}" if str(item.get("error") or "").strip() else ""
            print(
                f"- {item.get('bot_id') or '-'} status={item.get('status') or '-'} "
                f"pid={item.get('pid') or 0} "
                f"log={item.get('log_path') or '-'}"
                f"{remaining_text}"
                f"{error_text}"
            )
            if str(item.get("status") or "") == "conflict" and isinstance(item.get("conflicts"), list):
                for conflict in item.get("conflicts", []):
                    if not isinstance(conflict, dict):
                        continue
                    print(
                        "  conflict: "
                        f"pid={int(conflict.get('pid') or 0)} "
                        f"detected={conflict.get('detected_fleet_config_path') or '-'}"
                    )
        return
    remaining = payload.get("remaining_pids") if isinstance(payload.get("remaining_pids"), list) else []
    remaining_text = f" remaining={','.join(str(x) for x in remaining)}" if remaining else ""
    print(
        f"[{title}] {payload.get('bot_id') or '-'} "
        f"status={payload.get('status') or '-'} "
        f"pid={payload.get('pid') or 0}"
        f"{remaining_text}"
    )
    if str(payload.get("error") or "").strip():
        print(f"[{title}] error: {payload.get('error')}")
    if payload.get("log_path"):
        print(f"[{title}] log: {payload.get('log_path')}")
    if str(payload.get("status") or "") == "conflict" and isinstance(payload.get("conflicts"), list):
        for conflict in payload.get("conflicts", []):
            if not isinstance(conflict, dict):
                continue
            print(
                f"[{title}] conflict: pid={int(conflict.get('pid') or 0)} "
                f"detected={conflict.get('detected_fleet_config_path') or '-'}"
            )


def _format_restart_payload(payload: Dict[str, Any]) -> None:
    print(f"[RESTART] manifest: {payload.get('manifest_path', '-')}")
    stop_payload = payload.get("stop") if isinstance(payload.get("stop"), dict) else {}
    start_payload = payload.get("start") if isinstance(payload.get("start"), dict) else {}
    _format_start_stop_payload(stop_payload, title="STOP")
    _format_start_stop_payload(start_payload, title="START")


@contextmanager
def _file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _bot_process_lock_path(user_data_root: Path) -> Path:
    return user_data_root / "cheapclaw" / "runtime" / "process_ctl.lock"


def _fleet_web_lock_path(manifest_path: Path) -> Path:
    state_dir = _fleet_web_runtime_paths(manifest_path)[0].parent
    return state_dir / "process_ctl.lock"


def _format_emergency_stop_payload(payload: Dict[str, Any]) -> None:
    print("[STOP] manifest missing, fallback stop by process scan")
    bot = payload.get("bot_processes") if isinstance(payload.get("bot_processes"), dict) else {}
    web = payload.get("web_processes") if isinstance(payload.get("web_processes"), dict) else {}
    print(
        f"- bots status={bot.get('status') or '-'} "
        f"pids={','.join(str(x) for x in (bot.get('pids') or [])) or '-'} "
        f"remaining={','.join(str(x) for x in (bot.get('remaining_pids') or [])) or '-'}"
    )
    print(
        f"- web status={web.get('status') or '-'} "
        f"pids={','.join(str(x) for x in (web.get('pids') or [])) or '-'} "
        f"remaining={','.join(str(x) for x in (web.get('remaining_pids') or [])) or '-'}"
    )


def _fleet_web_runtime_paths(manifest_path: Path) -> Tuple[Path, Path, Path]:
    runtime_root = DEFAULT_RUNTIME_ROOT
    if manifest_path.exists():
        try:
            manifest = _load_json(manifest_path)
            runtime_raw = str(manifest.get("runtime_root") or "").strip()
            if runtime_raw and not _is_placeholder_text(runtime_raw):
                runtime_root = Path(runtime_raw).expanduser().resolve()
        except Exception:
            pass
    state_dir = (runtime_root / ".fleet_web").resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    return (
        state_dir / "fleet_web.pid",
        state_dir / "fleet_web.log",
        state_dir / "fleet_web.state.json",
    )


def _fleet_web_status_payload(manifest_path: Path) -> Dict[str, Any]:
    pid_path, log_path, state_path = _fleet_web_runtime_paths(manifest_path)
    pid = 0
    if pid_path.exists():
        try:
            pid = int((pid_path.read_text(encoding="utf-8") or "0").strip())
        except Exception:
            pid = 0
    target_pids = _collect_fleet_web_target_pids(manifest_path)
    if target_pids:
        pid = int(target_pids[0])
    else:
        pid = 0
    running = bool(target_pids)
    state = _safe_load_json(state_path, {})
    return {
        "status": "success",
        "manifest_path": str(manifest_path),
        "running": bool(running),
        "pid": int(pid),
        "pids": target_pids,
        "host": str(state.get("host") or ""),
        "port": int(state.get("port") or 0) if state.get("port") else 0,
        "url": str(state.get("url") or ""),
        "log_path": str(log_path),
        "state_path": str(state_path),
    }


def _start_fleet_web(manifest_path: Path, *, host: str, port: int) -> Dict[str, Any]:
    with _file_lock(_fleet_web_lock_path(manifest_path)):
        pid_path, log_path, state_path = _fleet_web_runtime_paths(manifest_path)
        existing = _fleet_web_status_payload(manifest_path)
        if bool(existing.get("running")):
            return {
                **existing,
                "status": "already_running",
                "output": f"fleet web already running at {existing.get('url') or '-'}",
            }
        if not FLEET_WEB_CONSOLE_PATH.exists():
            return {
                "status": "error",
                "manifest_path": str(manifest_path),
                "error": f"fleet web script not found: {FLEET_WEB_CONSOLE_PATH}",
            }

        cmd = [
            sys.executable,
            str(FLEET_WEB_CONSOLE_PATH),
            "--manifest",
            str(manifest_path),
            "--host",
            str(host or "127.0.0.1"),
            "--port",
            str(int(port or 8787)),
        ]
        env = os.environ.copy()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        std_log = open(log_path, "a", encoding="utf-8")
        process = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=std_log,
            stderr=std_log,
            start_new_session=True,
        )
        std_log.close()
        pid_path.write_text(str(int(process.pid)), encoding="utf-8")
        payload = {
            "host": str(host or "127.0.0.1"),
            "port": int(port or 8787),
            "url": f"http://{str(host or '127.0.0.1')}:{int(port or 8787)}/dashboard",
            "manifest_path": str(manifest_path),
            "pid": int(process.pid),
            "started_at": int(time.time()),
        }
        _dump_json(state_path, payload)
        time.sleep(0.4)
        if process.poll() is not None:
            try:
                log_tail = "\n".join(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:])
            except Exception:
                log_tail = ""
            return {
                "status": "error",
                "manifest_path": str(manifest_path),
                "running": False,
                "pid": int(process.pid),
                "host": payload["host"],
                "port": payload["port"],
                "url": payload["url"],
                "log_path": str(log_path),
                "error": "fleet web console exited immediately after start",
                "log_tail": log_tail,
            }
        return {
            "status": "started",
            "manifest_path": str(manifest_path),
            "running": True,
            "pid": int(process.pid),
            "host": payload["host"],
            "port": payload["port"],
            "url": payload["url"],
            "log_path": str(log_path),
        }


def _stop_fleet_web(manifest_path: Path, *, timeout_sec: int = 8) -> Dict[str, Any]:
    with _file_lock(_fleet_web_lock_path(manifest_path)):
        pid_path, log_path, state_path = _fleet_web_runtime_paths(manifest_path)
        pids = _collect_fleet_web_target_pids(manifest_path)
        if not pids:
            status = _fleet_web_status_payload(manifest_path)
            return {
                **status,
                "status": "already_stopped",
                "log_path": str(log_path),
            }
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue

        deadline = time.time() + max(1, int(timeout_sec))
        remaining: List[int] = list(pids)
        while time.time() < deadline:
            remaining = [
                pid
                for pid in pids
                if _is_pid_for_fleet_web(pid, manifest_path=manifest_path)
            ]
            if not remaining:
                break
            time.sleep(0.2)
        if remaining:
            try:
                for pid in remaining:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        continue
            except Exception:
                pass
        time.sleep(0.2)
        final_remaining = [
            pid
            for pid in pids
            if _is_pid_for_fleet_web(pid, manifest_path=manifest_path)
        ]
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass
        if state_path.exists():
            state = _safe_load_json(state_path, {})
            state["stopped_at"] = int(time.time())
            _dump_json(state_path, state)
        return {
            "status": "stopped" if not final_remaining else "partial",
            "manifest_path": str(manifest_path),
            "pid": int(pids[0]),
            "pids": pids,
            "remaining_pids": final_remaining,
            "log_path": str(log_path),
        }


def _format_web_payload(payload: Dict[str, Any], *, title: str) -> None:
    print(f"[{title}] manifest: {payload.get('manifest_path', '-')}")
    print(
        f"[{title}] status={payload.get('status') or '-'} "
        f"running={_format_bool(payload.get('running'))} pid={payload.get('pid') or 0}"
    )
    if payload.get("url"):
        print(f"[{title}] url: {payload.get('url')}")
    if payload.get("log_path"):
        print(f"[{title}] log: {payload.get('log_path')}")
    if payload.get("error"):
        print(f"[{title}] error: {payload.get('error')}")


def _open_config_file(path: Path) -> None:
    editor = str(os.environ.get("EDITOR") or "").strip()
    try:
        if editor:
            subprocess.Popen([editor, str(path)])
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
            return
    except Exception:
        return


def _scaffold_manifest(manifest_path: Path, *, force: bool = False) -> bool:
    if not TEMPLATE_MANIFEST_PATH.exists():
        raise SystemExit(f"[ERROR] template manifest not found: {TEMPLATE_MANIFEST_PATH}")
    if manifest_path.exists() and not force:
        return False
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATE_MANIFEST_PATH, manifest_path)
    return True


def _scan_placeholder_fields(value: Any, path: str = "root") -> List[str]:
    hits: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            hits.extend(_scan_placeholder_fields(item, f"{path}.{key}"))
        return hits
    if isinstance(value, list):
        for idx, item in enumerate(value):
            hits.extend(_scan_placeholder_fields(item, f"{path}[{idx}]"))
        return hits
    if isinstance(value, str):
        text = value.strip()
        if _is_placeholder_text(text):
            hits.append(path)
    return hits


def _prompt_text(label: str, default: str = "", *, required: bool = False) -> str:
    while True:
        hint = f" [{default}]" if default else ""
        raw = input(f"{label}{hint}: ").strip()
        if raw:
            return raw
        if default:
            return default
        if not required:
            return ""
        print("[INPUT] this field is required")


def _prompt_bool(label: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("[INPUT] please answer y or n")


def _prompt_int(label: str, default: int, *, min_value: int = 0) -> int:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            return max(min_value, default)
        try:
            value = int(raw)
        except ValueError:
            print("[INPUT] please enter a valid integer")
            continue
        if value < min_value:
            print(f"[INPUT] value must be >= {min_value}")
            continue
        return value


def _prompt_channel(default: str = "localweb") -> str:
    default_norm = default if default in {"telegram", "feishu", "whatsapp", "discord", "qq", "wechat", "localweb"} else "localweb"
    mapping = {
        "1": "telegram",
        "2": "feishu",
        "3": "whatsapp",
        "4": "discord",
        "5": "qq",
        "6": "wechat",
        "7": "localweb",
        "telegram": "telegram",
        "feishu": "feishu",
        "whatsapp": "whatsapp",
        "discord": "discord",
        "qq": "qq",
        "wechat": "wechat",
        "localweb": "localweb",
    }
    while True:
        raw = input(
            f"channel [1=telegram, 2=feishu, 3=whatsapp, 4=discord, 5=qq, 6=wechat, 7=localweb] [{default_norm}]: "
        ).strip().lower()
        if not raw:
            return default_norm
        value = mapping.get(raw)
        if value:
            return value
        print("[INPUT] channel must be one of: 1/2/3/4/5/6/7 or telegram/feishu/whatsapp/discord/qq/wechat/localweb")


def _normalize_model_name(model_name: str) -> str:
    text = str(model_name or "").strip()
    if not text:
        raise SystemExit("[ERROR] model name is required")
    # Preserve already-qualified model ids such as:
    # - openai/gpt-4o
    # - openai/google/gemini-3-flash-preview
    # - openrouter/openai/gpt-4o
    # Only add the OpenAI-format prefix when the user enters a bare model name.
    return text if "/" in text else f"openai/{text}"


def _write_minimal_llm_config(
    llm_config_path: Path,
    *,
    base_url: str,
    api_key: str,
    model_name: str,
    multimodal: bool,
) -> None:
    model_full = _normalize_model_name(model_name)
    mm = "true" if multimodal else "false"
    content = (
        "# Auto-generated by scripts/fleet_one_click.py interactive setup.\n"
        "# You can add more models manually later.\n"
        "temperature: 0\n"
        "max_tokens: 0\n"
        "max_context_window: 500000\n"
        f"base_url: {json.dumps(str(base_url), ensure_ascii=False)}\n"
        f"api_key: {json.dumps(str(api_key), ensure_ascii=False)}\n"
        "timeout: 300\n"
        "stream_timeout: 30\n"
        "first_chunk_timeout: 30\n"
        "\n"
        "tool_choice:\n"
        "  execution: required\n"
        "  thinking: required\n"
        "  compressor: required\n"
        "  image_generation: required\n"
        "  read_figure: required\n"
        "\n"
        "models:\n"
        f"  - name: {json.dumps(model_full, ensure_ascii=False)}\n"
        "    default: true\n"
        "    tool_choice: required\n"
        "\n"
        "figure_models:\n"
        f"  - name: {json.dumps(model_full, ensure_ascii=False)}\n"
        "    default: true\n"
        "    tool_choice: required\n"
        "\n"
        "compressor_models:\n"
        f"  - name: {json.dumps(model_full, ensure_ascii=False)}\n"
        "    default: true\n"
        "    tool_choice: required\n"
        "\n"
        "read_figure_models:\n"
        f"  - name: {json.dumps(model_full, ensure_ascii=False)}\n"
        "    default: true\n"
        "    tool_choice: required\n"
        "\n"
        "thinking_models:\n"
        f"  - name: {json.dumps(model_full, ensure_ascii=False)}\n"
        "    default: true\n"
        "    tool_choice: required\n"
        "\n"
        f"multimodal: {mm}\n"
        f"compressor_multimodal: {mm}\n"
    )
    llm_config_path.parent.mkdir(parents=True, exist_ok=True)
    llm_config_path.write_text(content, encoding="utf-8")


def _normalize_context_settings(context: Any) -> Dict[str, int]:
    source = context if isinstance(context, dict) else {}
    return {
        "user_history_compress_threshold_tokens": max(
            0,
            int(source.get("user_history_compress_threshold_tokens", 1500) or 1500),
        ),
        "structured_call_info_compress_threshold_agents": max(
            1,
            int(source.get("structured_call_info_compress_threshold_agents", 10) or 10),
        ),
        "structured_call_info_compress_threshold_tokens": max(
            0,
            int(source.get("structured_call_info_compress_threshold_tokens", 2200) or 2200),
        ),
    }


def _apply_context_settings_to_app_config(user_data_root: Path, context: Dict[str, int]) -> None:
    app_config_path = user_data_root / "cheapclaw" / "config" / "app_config.json"
    payload = _safe_load_json(app_config_path, {})
    context_cfg = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    context_cfg.update(_normalize_context_settings(context))
    payload["context"] = context_cfg
    _dump_json(app_config_path, payload)


def _prompt_bot_payload(
    *,
    index: int,
    seed_bot: Dict[str, Any] | None = None,
    existing_bot_ids: set[str] | None = None,
) -> Dict[str, Any]:
    seed = seed_bot if isinstance(seed_bot, dict) else {}
    existing = existing_bot_ids if existing_bot_ids is not None else set()
    seed_channel = str(seed.get("channel") or "localweb").strip().lower()
    print(f"\n[SETUP] bot {index}")
    channel = _prompt_channel(seed_channel)

    while True:
        bot_id_default = str(seed.get("bot_id") or f"{channel}_bot_{index}")
        bot_id = _prompt_text("bot_id", bot_id_default, required=True)
        if bot_id in existing:
            print(f"[INPUT] bot_id already exists: {bot_id}")
            continue
        break

    display_name = _prompt_text("display_name", str(seed.get("display_name") or bot_id), required=True)
    enabled = _prompt_bool("enabled", bool(seed.get("enabled", True)))
    default_webhooks = bool(seed.get("serve_webhooks", channel in {"localweb", "qq", "wechat"}))
    serve_webhooks = _prompt_bool("serve_webhooks", default_webhooks)

    bot_payload: Dict[str, Any] = {
        "bot_id": bot_id,
        "display_name": display_name,
        "channel": channel,
        "enabled": enabled,
        "serve_webhooks": serve_webhooks,
    }
    if serve_webhooks:
        bot_payload["host"] = _prompt_text("host", str(seed.get("host") or "127.0.0.1"), required=True)
        bot_payload["port"] = _prompt_int("port", int(seed.get("port") or (8764 + index)), min_value=1)

    if channel == "telegram":
        seed_telegram = seed.get("telegram") if isinstance(seed.get("telegram"), dict) else {}
        while True:
            token = _prompt_text("telegram.bot_token", str(seed_telegram.get("bot_token") or ""), required=True)
            if TELEGRAM_TOKEN_PATTERN.match(token):
                break
            print("[INPUT] invalid token format. Use full BotFather token like '123456789:AA...'")
        chats_default = ",".join(str(x) for x in seed_telegram.get("allowed_chats", []) if str(x).strip())
        chats_raw = _prompt_text("telegram.allowed_chats (comma-separated)", chats_default)
        bot_payload["telegram"] = {
            "bot_token": token,
            "allowed_chats": [x.strip() for x in chats_raw.split(",") if x.strip()],
        }
    elif channel == "feishu":
        seed_feishu = seed.get("feishu") if isinstance(seed.get("feishu"), dict) else {}
        bot_payload["feishu"] = {
            "mode": _prompt_text("feishu.mode", str(seed_feishu.get("mode") or "long_connection"), required=True),
            "app_id": _prompt_text("feishu.app_id", str(seed_feishu.get("app_id") or ""), required=True),
            "app_secret": _prompt_text("feishu.app_secret", str(seed_feishu.get("app_secret") or ""), required=True),
            "verify_token": _prompt_text("feishu.verify_token", str(seed_feishu.get("verify_token") or "")),
            "encrypt_key": _prompt_text("feishu.encrypt_key", str(seed_feishu.get("encrypt_key") or "")),
        }
    elif channel == "whatsapp":
        seed_wa = seed.get("whatsapp") if isinstance(seed.get("whatsapp"), dict) else {}
        bot_payload["whatsapp"] = {
            "access_token": _prompt_text("whatsapp.access_token", str(seed_wa.get("access_token") or ""), required=True),
            "phone_number_id": _prompt_text("whatsapp.phone_number_id", str(seed_wa.get("phone_number_id") or ""), required=True),
            "verify_token": _prompt_text("whatsapp.verify_token", str(seed_wa.get("verify_token") or ""), required=True),
            "api_version": _prompt_text("whatsapp.api_version", str(seed_wa.get("api_version") or "v21.0"), required=True),
        }
    elif channel == "discord":
        seed_dc = seed.get("discord") if isinstance(seed.get("discord"), dict) else {}
        bot_payload["discord"] = {
            "bot_token": _prompt_text("discord.bot_token", str(seed_dc.get("bot_token") or ""), required=True),
            "intents": _prompt_int("discord.intents", int(seed_dc.get("intents") or 37377), min_value=1),
            "require_mention_in_guild": _prompt_bool(
                "discord.require_mention_in_guild",
                bool(seed_dc.get("require_mention_in_guild", True)),
            ),
        }
    elif channel in {"qq", "wechat"}:
        seed_bridge = seed.get(channel) if isinstance(seed.get(channel), dict) else {}
        bot_payload[channel] = {
            "onebot_api_base": _prompt_text(f"{channel}.onebot_api_base", str(seed_bridge.get("onebot_api_base") or f"http://127.0.0.1:{5700 if channel == 'qq' else 5701}"), required=True),
            "onebot_access_token": _prompt_text(f"{channel}.onebot_access_token", str(seed_bridge.get("onebot_access_token") or "")),
            "onebot_post_secret": _prompt_text(f"{channel}.onebot_post_secret", str(seed_bridge.get("onebot_post_secret") or "")),
            "onebot_self_id": _prompt_text(f"{channel}.onebot_self_id", str(seed_bridge.get("onebot_self_id") or "")),
            "require_mention_in_group": _prompt_bool(
                f"{channel}.require_mention_in_group",
                bool(seed_bridge.get("require_mention_in_group", True)),
            ),
        }
    else:
        seed_local = seed.get("localweb") if isinstance(seed.get("localweb"), dict) else {}
        bot_payload["localweb"] = {
            "require_mention_in_group": _prompt_bool(
                "localweb.require_mention_in_group",
                bool(seed_local.get("require_mention_in_group", True)),
            ),
        }
    return bot_payload


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _iter_process_table() -> List[Tuple[int, str]]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    rows: List[Tuple[int, str]] = []
    for raw in (proc.stdout or "").splitlines():
        line = str(raw or "").strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except Exception:
            continue
        command = parts[1] if len(parts) > 1 else ""
        rows.append((pid, command))
    return rows


def _read_pid_command(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return str(proc.stdout or "").strip()


def _read_pid_arg_value(pid: int, arg_name: str) -> str:
    command = _read_pid_command(pid)
    if not command:
        return ""
    try:
        tokens = shlex.split(command)
    except Exception:
        return ""
    for idx, token in enumerate(tokens):
        if token == arg_name and idx + 1 < len(tokens):
            return str(tokens[idx + 1]).strip()
        if token.startswith(f"{arg_name}="):
            return str(token.split("=", 1)[1]).strip()
    return ""


def _all_needles_in_command(command: str, needles: List[str]) -> bool:
    if not command:
        return False
    return all(str(needle) in command for needle in needles)


def _discover_pids_by_needles(needles: List[str]) -> List[int]:
    pids: List[int] = []
    for pid, command in _iter_process_table():
        if _all_needles_in_command(command, needles):
            pids.append(int(pid))
    return sorted(set(pids))


def _bot_service_needles(bot_id: str, user_data_root: Path) -> List[str]:
    return [
        "cheapclaw_service.py",
        "--run-loop",
        f"--bot-id {str(bot_id)}",
        f"--user-data-root {str(user_data_root)}",
    ]


def _is_pid_for_bot_service(pid: int, *, bot_id: str, user_data_root: Path) -> bool:
    if not _is_pid_alive(pid):
        return False
    command = _read_pid_command(pid)
    return _all_needles_in_command(command, _bot_service_needles(bot_id, user_data_root))


def _discover_bot_service_pids(bot_id: str, user_data_root: Path) -> List[int]:
    return _discover_pids_by_needles(_bot_service_needles(bot_id, user_data_root))


def _collect_bot_service_target_pids(bot_id: str, user_data_root: Path) -> List[int]:
    pids = _discover_bot_service_pids(bot_id, user_data_root)
    state_pid = _read_bot_pid_from_runtime_state(user_data_root)
    if _is_pid_for_bot_service(state_pid, bot_id=bot_id, user_data_root=user_data_root):
        pids.append(int(state_pid))
    return sorted(set(pids))


def _bot_start_conflicts_with_other_fleet(
    *,
    bot_id: str,
    user_data_root: Path,
    expected_fleet_config_path: Path,
) -> List[Dict[str, Any]]:
    expected = str(expected_fleet_config_path.expanduser().resolve())
    conflicts: List[Dict[str, Any]] = []
    for pid in _discover_bot_service_pids(bot_id, user_data_root):
        detected_raw = _read_pid_arg_value(pid, "--fleet-config-path")
        if not detected_raw:
            continue
        try:
            detected = str(Path(detected_raw).expanduser().resolve())
        except Exception:
            detected = str(detected_raw).strip()
        if detected and detected != expected:
            conflicts.append(
                {
                    "pid": int(pid),
                    "detected_fleet_config_path": detected,
                    "expected_fleet_config_path": expected,
                }
            )
    return conflicts


def _fleet_web_needles(manifest_path: Path) -> List[str]:
    return [
        "fleet_web_console.py",
        f"--manifest {str(manifest_path)}",
    ]


def _is_pid_for_fleet_web(pid: int, *, manifest_path: Path) -> bool:
    if not _is_pid_alive(pid):
        return False
    command = _read_pid_command(pid)
    if "fleet_web_console.py" not in command:
        return False
    detected_raw = _read_pid_arg_value(pid, "--manifest")
    if not detected_raw:
        return True
    try:
        detected = str(Path(detected_raw).expanduser().resolve())
    except Exception:
        detected = str(detected_raw).strip()
    expected = str(manifest_path.expanduser().resolve())
    return bool(detected) and detected == expected


def _collect_fleet_web_target_pids(manifest_path: Path) -> List[int]:
    pids = [
        int(pid)
        for pid in _discover_repo_fleet_web_pids()
        if _is_pid_for_fleet_web(int(pid), manifest_path=manifest_path)
    ]
    pid_path, _log_path, _state_path = _fleet_web_runtime_paths(manifest_path)
    pid = 0
    if pid_path.exists():
        try:
            pid = int((pid_path.read_text(encoding="utf-8") or "0").strip())
        except Exception:
            pid = 0
    if _is_pid_for_fleet_web(pid, manifest_path=manifest_path):
        pids.append(int(pid))
    return sorted(set(pids))


def _write_runtime_state_pid(user_data_root: Path, *, pid: int) -> None:
    state_path = user_data_root / "cheapclaw" / "runtime" / "state.json"
    state = _safe_load_json(state_path, {})
    bot_state = state.get("bot") if isinstance(state.get("bot"), dict) else {}
    bot_state["pid"] = int(pid)
    state["bot"] = bot_state
    _dump_json(state_path, state)


def _read_bot_pid_from_runtime_state(user_data_root: Path) -> int:
    state_path = user_data_root / "cheapclaw" / "runtime" / "state.json"
    if not state_path.exists():
        return 0
    try:
        state_payload = _load_json(state_path)
    except Exception:
        return 0
    if not isinstance(state_payload, dict):
        return 0
    return int((((state_payload.get("bot") or {}) if isinstance(state_payload, dict) else {}).get("pid")) or 0)


def _resolve_bot_from_manifest(manifest_path: Path, bot_id: str) -> Tuple[Dict[str, Any], Path, Path, Path]:
    manifest = _load_json(manifest_path)
    llm_config_path = Path(_required_text(manifest.get("llm_config_path"), "llm_config_path", "<manifest>")).expanduser().resolve()
    runtime_root = Path(_required_text(manifest.get("runtime_root"), "runtime_root", "<manifest>")).expanduser().resolve()
    fleet_config_path = Path(
        str(manifest.get("fleet_config_path") or (runtime_root / "fleet.generated.json"))
    ).expanduser().resolve()
    target: Dict[str, Any] | None = None
    for item in manifest.get("bots", []):
        if isinstance(item, dict) and str(item.get("bot_id") or "").strip() == bot_id:
            target = item
            break
    if target is None:
        raise SystemExit(f"[ERROR] bot_id not found in manifest: {bot_id}")
    user_data_root = (runtime_root / bot_id).resolve()
    return manifest, target, user_data_root, llm_config_path


def _agent_system_has_manifest_files(root: Path) -> bool:
    required = (
        "level_0_tools.yaml",
        "level_1_agents.yaml",
        "level_2_agents.yaml",
        "level_3_agents.yaml",
    )
    return any((root / name).exists() for name in required)


def _safe_agent_system_name(name: str, fallback: str) -> str:
    text = str(name or "").strip()
    if not text:
        text = fallback
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return cleaned or fallback


def _install_agent_system_archive(archive_path: Path, destination_root: Path) -> Dict[str, Any]:
    archive_path = archive_path.expanduser().resolve()
    if not archive_path.exists() or not archive_path.is_file():
        raise SystemExit(f"[ERROR] archive not found: {archive_path}")
    if not zipfile.is_zipfile(archive_path):
        raise SystemExit(f"[ERROR] archive is not a valid zip file: {archive_path}")

    destination_root = destination_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cheapclaw-agent-system-") as tmp_dir:
        extract_root = Path(tmp_dir)
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_root)

        children = [
            item for item in extract_root.iterdir()
            if item.name not in {"__MACOSX"} and not item.name.startswith(".")
        ]
        top_dirs = [item for item in children if item.is_dir()]
        top_files = [item for item in children if item.is_file()]

        source_root = extract_root
        system_name = _safe_agent_system_name(archive_path.stem, archive_path.stem or "AgentSystem")
        if len(top_dirs) == 1 and not any(item.suffix.lower() in {".yaml", ".yml"} for item in top_files):
            source_root = top_dirs[0]
            system_name = _safe_agent_system_name(source_root.name, system_name)

        if not _agent_system_has_manifest_files(source_root):
            raise SystemExit(
                "[ERROR] zip does not look like a valid agent system. "
                "Expected files like level_0_tools.yaml or level_3_agents.yaml in the root folder."
            )

        target_root = destination_root / system_name
        if target_root.exists():
            shutil.rmtree(target_root)
        shutil.copytree(source_root, target_root)

        file_count = sum(1 for item in target_root.rglob("*") if item.is_file())
        return {
            "status": "success",
            "system_name": system_name,
            "archive_path": str(archive_path),
            "target_root": str(target_root),
            "file_count": file_count,
        }


def _safe_load_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fallback = default if isinstance(default, dict) else {}
    if not path.exists():
        return dict(fallback)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(fallback)
    if not isinstance(payload, dict):
        return dict(fallback)
    return payload


def _list_manifest_bots(manifest_path: Path, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
    manifest = _load_json(manifest_path)
    bots = [item for item in manifest.get("bots", []) if isinstance(item, dict)]
    if enabled_only:
        bots = [item for item in bots if bool(item.get("enabled", True))]
    return bots


def _read_bot_runtime_snapshot(user_data_root: Path, *, bot_id: str) -> Dict[str, Any]:
    runtime_root = user_data_root / "cheapclaw" / "runtime"
    state_payload = _safe_load_json(runtime_root / "state.json")
    panel_payload = _safe_load_json(user_data_root / "cheapclaw" / "panel" / "panel.json")
    instructions_payload = _safe_load_json(
        user_data_root / "cheapclaw" / "monitor_instructions.json",
        {"version": 1, "instructions": []},
    )
    instructions = instructions_payload.get("instructions")
    if not isinstance(instructions, list):
        instructions = []
    pending_count = sum(1 for item in instructions if isinstance(item, dict) and str(item.get("status") or "") == "pending")
    bot_runtime = state_payload.get("bot") if isinstance(state_payload.get("bot"), dict) else {}
    service_state = panel_payload.get("service_state") if isinstance(panel_payload.get("service_state"), dict) else {}
    state_pid = int(bot_runtime.get("pid") or 0)
    live_pids = _discover_bot_service_pids(bot_id, user_data_root)
    pid = int(live_pids[0]) if live_pids else 0
    if pid <= 0 and _is_pid_for_bot_service(state_pid, bot_id=bot_id, user_data_root=user_data_root):
        pid = int(state_pid)
    return {
        "pid": pid,
        "alive": bool(pid > 0),
        "pids": live_pids,
        "pending_instruction_count": pending_count,
        "main_agent_running": bool(service_state.get("main_agent_running")),
        "main_agent_last_started_at": str(service_state.get("main_agent_last_started_at") or ""),
        "watchdog_last_run_at": str(service_state.get("watchdog_last_run_at") or ""),
        "service_log_path": str(runtime_root / "cheapclaw_service.log"),
        "loop_log_path": str(runtime_root / "service.loop.log"),
    }


def _status_single_bot_from_manifest(manifest_path: Path, bot_id: str) -> Dict[str, Any]:
    _manifest, target, user_data_root, _llm = _resolve_bot_from_manifest(manifest_path, bot_id)
    runtime = _read_bot_runtime_snapshot(user_data_root, bot_id=bot_id)
    return {
        "bot_id": str(target.get("bot_id") or bot_id),
        "display_name": str(target.get("display_name") or bot_id),
        "channel": str(target.get("channel") or ""),
        "enabled": bool(target.get("enabled", True)),
        "serve_webhooks": bool(target.get("serve_webhooks", False)),
        "user_data_root": str(user_data_root),
        **runtime,
    }


def _status_all_bots_from_manifest(manifest_path: Path) -> Dict[str, Any]:
    bots = _list_manifest_bots(manifest_path, enabled_only=False)
    statuses = []
    for item in bots:
        bot_id = str(item.get("bot_id") or "").strip()
        if not bot_id:
            continue
        statuses.append(_status_single_bot_from_manifest(manifest_path, bot_id))
    return {
        "status": "success",
        "manifest_path": str(manifest_path),
        "bot_count": len(statuses),
        "running_count": sum(1 for item in statuses if item.get("alive")),
        "bots": statuses,
    }


def _stop_single_bot_from_manifest(
    manifest_path: Path,
    *,
    bot_id: str,
    timeout_sec: int = 8,
) -> Dict[str, Any]:
    _manifest, _target, user_data_root, _llm = _resolve_bot_from_manifest(manifest_path, bot_id)
    with _file_lock(_bot_process_lock_path(user_data_root)):
        pids = _collect_bot_service_target_pids(bot_id, user_data_root)
        if not pids:
            stale_pid = _read_bot_pid_from_runtime_state(user_data_root)
            if stale_pid:
                try:
                    _write_runtime_state_pid(user_data_root, pid=0)
                except Exception:
                    pass
            return {
                "status": "already_stopped",
                "bot_id": bot_id,
                "pid": stale_pid,
                "pids": [],
                "user_data_root": str(user_data_root),
            }

        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue

        deadline = time.time() + max(1, int(timeout_sec))
        remaining: List[int] = list(pids)
        while time.time() < deadline:
            remaining = [
                pid
                for pid in pids
                if _is_pid_for_bot_service(pid, bot_id=bot_id, user_data_root=user_data_root)
            ]
            if not remaining:
                try:
                    _write_runtime_state_pid(user_data_root, pid=0)
                except Exception:
                    pass
                return {
                    "status": "stopped",
                    "bot_id": bot_id,
                    "pid": int(pids[0]),
                    "pids": pids,
                    "user_data_root": str(user_data_root),
                }
            time.sleep(0.2)

        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        time.sleep(0.2)
        final_remaining = [
            pid
            for pid in pids
            if _is_pid_for_bot_service(pid, bot_id=bot_id, user_data_root=user_data_root)
        ]
        if not final_remaining:
            try:
                _write_runtime_state_pid(user_data_root, pid=0)
            except Exception:
                pass
        return {
            "status": "killed" if not final_remaining else "partial",
            "bot_id": bot_id,
            "pid": int(pids[0]),
            "pids": pids,
            "remaining_pids": final_remaining,
            "user_data_root": str(user_data_root),
        }


def _start_single_bot_from_manifest(
    manifest_path: Path,
    *,
    bot_id: str,
    poll_interval: int = 5,
) -> Dict[str, Any]:
    manifest, target, user_data_root, llm_config_path = _resolve_bot_from_manifest(manifest_path, bot_id)
    runtime_root = Path(_required_text(manifest.get("runtime_root"), "runtime_root", "<manifest>")).expanduser().resolve()
    fleet_config_path = Path(
        str(manifest.get("fleet_config_path") or (runtime_root / "fleet.generated.json"))
    ).expanduser().resolve()
    if not bool(target.get("enabled", True)):
        raise SystemExit(f"[ERROR] bot is disabled in manifest: {bot_id}")

    with _file_lock(_bot_process_lock_path(user_data_root)):
        conflicts = _bot_start_conflicts_with_other_fleet(
            bot_id=bot_id,
            user_data_root=user_data_root,
            expected_fleet_config_path=fleet_config_path,
        )
        if conflicts:
            conflict_pids = sorted(
                set(int(item.get("pid") or 0) for item in conflicts if int(item.get("pid") or 0) > 0)
            )
            return {
                "status": "conflict",
                "bot_id": bot_id,
                "pid": int(conflict_pids[0]) if conflict_pids else 0,
                "pids": conflict_pids,
                "user_data_root": str(user_data_root),
                "fleet_config_path": str(fleet_config_path),
                "conflicts": conflicts,
                "error": (
                    "bot process is already running under a different fleet config; "
                    "stop the conflicting process first"
                ),
            }
        current_pid = _read_bot_pid_from_runtime_state(user_data_root)
        if _is_pid_for_bot_service(current_pid, bot_id=bot_id, user_data_root=user_data_root):
            return {
                "status": "already_running",
                "bot_id": bot_id,
                "pid": current_pid,
                "user_data_root": str(user_data_root),
            }
        live_pids = _discover_bot_service_pids(bot_id, user_data_root)
        if live_pids:
            try:
                _write_runtime_state_pid(user_data_root, pid=int(live_pids[0]))
            except Exception:
                pass
            return {
                "status": "already_running",
                "bot_id": bot_id,
                "pid": int(live_pids[0]),
                "pids": live_pids,
                "user_data_root": str(user_data_root),
            }

        cmd = [
            sys.executable,
            str(SERVICE_PATH),
            "--user-data-root",
            str(user_data_root),
            "--llm-config-path",
            str(llm_config_path),
            "--bot-id",
            str(bot_id),
            "--bot-display-name",
            str(target.get("display_name") or bot_id),
            "--fleet-config-path",
            str(fleet_config_path),
            "--run-loop",
            "--poll-interval",
            str(max(1, int(poll_interval))),
        ]
        if bool(target.get("serve_webhooks", False)):
            cmd.extend(
                [
                    "--serve-webhooks",
                    "--host",
                    str(target.get("host") or "127.0.0.1"),
                    "--port",
                    str(int(target.get("port") or 8765)),
                ]
            )

        env = _child_process_env(manifest)

        log_dir = user_data_root / "cheapclaw" / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        std_log_path = log_dir / "service.loop.log"
        std_log = open(std_log_path, "a", encoding="utf-8")
        process = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=std_log,
            stderr=std_log,
            start_new_session=True,
        )
        std_log.close()
        return {
            "status": "started",
            "bot_id": bot_id,
            "pid": int(process.pid),
            "user_data_root": str(user_data_root),
            "log_path": str(std_log_path),
            "fleet_config_path": str(fleet_config_path),
        }


def _start_all_bots_from_manifest(
    manifest_path: Path,
    *,
    poll_interval: int = 5,
) -> Dict[str, Any]:
    bots = _list_manifest_bots(manifest_path, enabled_only=True)
    results = []
    for item in bots:
        bot_id = str(item.get("bot_id") or "").strip()
        if not bot_id:
            continue
        results.append(
            _start_single_bot_from_manifest(
                manifest_path,
                bot_id=bot_id,
                poll_interval=poll_interval,
            )
        )
    return {
        "status": "success",
        "manifest_path": str(manifest_path),
        "requested_bot_count": len(bots),
        "started_count": sum(1 for item in results if str(item.get("status") or "") == "started"),
        "already_running_count": sum(1 for item in results if str(item.get("status") or "") == "already_running"),
        "conflict_count": sum(1 for item in results if str(item.get("status") or "") == "conflict"),
        "results": results,
    }


def _stop_all_bots_from_manifest(
    manifest_path: Path,
    *,
    timeout_sec: int = 8,
) -> Dict[str, Any]:
    bots = _list_manifest_bots(manifest_path, enabled_only=False)
    results = []
    for item in bots:
        bot_id = str(item.get("bot_id") or "").strip()
        if not bot_id:
            continue
        results.append(
            _stop_single_bot_from_manifest(
                manifest_path,
                bot_id=bot_id,
                timeout_sec=timeout_sec,
            )
        )
    return {
        "status": "success",
        "manifest_path": str(manifest_path),
        "requested_bot_count": len(bots),
        "stopped_count": sum(1 for item in results if str(item.get("status") or "") in {"stopped", "killed"}),
        "already_stopped_count": sum(1 for item in results if str(item.get("status") or "") == "already_stopped"),
        "partial_count": sum(1 for item in results if str(item.get("status") or "") == "partial"),
        "results": results,
    }


def _terminate_pids(pids: List[int], *, timeout_sec: int = 8) -> Dict[str, Any]:
    targets = sorted(set(int(pid) for pid in pids if _is_pid_alive(int(pid))))
    if not targets:
        return {
            "status": "already_stopped",
            "pids": [],
            "remaining_pids": [],
        }
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.time() + max(1, int(timeout_sec))
    remaining = list(targets)
    while time.time() < deadline:
        remaining = [pid for pid in targets if _is_pid_alive(pid)]
        if not remaining:
            break
        time.sleep(0.2)
    if remaining:
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        time.sleep(0.2)
    final_remaining = [pid for pid in targets if _is_pid_alive(pid)]
    return {
        "status": "stopped" if not final_remaining else "partial",
        "pids": targets,
        "remaining_pids": final_remaining,
    }


def _discover_repo_bot_loop_pids() -> List[int]:
    return _discover_pids_by_needles(
        [
            str(SERVICE_PATH),
            "cheapclaw_service.py",
            "--run-loop",
        ]
    )


def _discover_repo_fleet_web_pids() -> List[int]:
    return _discover_pids_by_needles(
        [
            str(FLEET_WEB_CONSOLE_PATH),
            "fleet_web_console.py",
        ]
    )


def _stop_fleet_web_fallback(*, timeout_sec: int = 8) -> Dict[str, Any]:
    payload = _terminate_pids(_discover_repo_fleet_web_pids(), timeout_sec=timeout_sec)
    return {
        "status": payload.get("status") or "already_stopped",
        "running": bool(payload.get("remaining_pids")),
        "mode": "manifest_missing_fallback",
        "manifest_path": "",
        "pid": int((payload.get("pids") or [0])[0]) if payload.get("pids") else 0,
        "pids": payload.get("pids") or [],
        "remaining_pids": payload.get("remaining_pids") or [],
        "log_path": "",
        "note": "manifest not found, stopped web by process scan",
    }


def _stop_all_fallback_without_manifest(*, timeout_sec: int = 8) -> Dict[str, Any]:
    bot_payload = _terminate_pids(_discover_repo_bot_loop_pids(), timeout_sec=timeout_sec)
    web_payload = _terminate_pids(_discover_repo_fleet_web_pids(), timeout_sec=timeout_sec)
    return {
        "status": "success",
        "mode": "manifest_missing_fallback",
        "manifest_path": "",
        "bot_processes": bot_payload,
        "web_processes": web_payload,
        "note": "manifest not found, fallback stop by process scan",
    }


def _print_log_tail(path: Path, lines: int = 200) -> None:
    if not path.exists():
        print(f"[WARN] log file not found: {path}")
        return
    total = max(1, int(lines))
    try:
        content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as exc:
        raise SystemExit(f"[ERROR] failed to read log: {path} ({exc})")
    print(f"[LOG] {path} (last {total} lines)")
    for line in content[-total:]:
        print(line)


def _interactive_manifest_setup(
    default_manifest_path: Path,
    *,
    seed_manifest: Dict[str, Any] | None = None,
) -> Tuple[Path, Dict[str, Any]]:
    seed = seed_manifest if isinstance(seed_manifest, dict) else {}
    seed_bots = seed.get("bots")
    bots_seed: List[Dict[str, Any]] = [b for b in seed_bots if isinstance(b, dict)] if isinstance(seed_bots, list) else []
    if bots_seed and all(_bot_seed_is_placeholder(item) for item in bots_seed):
        bots_seed = []
    print("[SETUP] Interactive manifest setup")
    manifest_input = _prompt_text("manifest path", str(default_manifest_path))
    manifest_path = Path(manifest_input).expanduser().resolve()

    runtime_seed = str(seed.get("runtime_root") or "")
    runtime_default = (
        str(DEFAULT_RUNTIME_ROOT)
        if _is_placeholder_text(runtime_seed)
        else (runtime_seed or str(DEFAULT_RUNTIME_ROOT))
    )
    fleet_seed = str(seed.get("fleet_config_path") or "")
    fleet_default_seed = "" if _is_placeholder_text(fleet_seed) else fleet_seed
    fleet_default = fleet_default_seed or str((Path(runtime_default).expanduser().resolve() / "fleet.generated.json"))
    runtime_root = _prompt_text("runtime_root", runtime_default, required=True)
    fleet_config_path = _prompt_text(
        "fleet_config_path",
        fleet_default or str((Path(runtime_root).expanduser().resolve() / "fleet.generated.json")),
        required=True,
    )

    seed_proxy = seed.get("proxy_env") if isinstance(seed.get("proxy_env"), dict) else {}
    print("[SETUP] proxy_env (leave empty if not needed)")
    http_proxy = _prompt_text("proxy_env.http_proxy", str(seed_proxy.get("http_proxy") or ""))
    https_proxy = _prompt_text("proxy_env.https_proxy", str(seed_proxy.get("https_proxy") or ""))
    all_proxy = _prompt_text("proxy_env.all_proxy", str(seed_proxy.get("all_proxy") or ""))

    bot_count_default = len(bots_seed) if bots_seed else 1
    bot_count = _prompt_int("bot count", bot_count_default, min_value=1)
    bots: List[Dict[str, Any]] = []

    existing_bot_ids: set[str] = set()
    for idx in range(bot_count):
        seed_bot = bots_seed[idx] if idx < len(bots_seed) else {}
        bot_payload = _prompt_bot_payload(
            index=idx + 1,
            seed_bot=seed_bot,
            existing_bot_ids=existing_bot_ids,
        )
        existing_bot_ids.add(str(bot_payload.get("bot_id") or ""))
        bots.append(bot_payload)

    print("\n[SETUP] LLM provider config")
    print("[SETUP] You can add more models manually in llm_config later.")
    llm_seed = str(seed.get("llm_config_path") or "")
    llm_default_path = (
        str(Path(runtime_root).expanduser().resolve() / "config" / "llm_config.yaml")
        if _is_placeholder_text(llm_seed)
        else (llm_seed or str(Path(runtime_root).expanduser().resolve() / "config" / "llm_config.yaml"))
    )
    llm_config_path_text = _prompt_text(
        "llm_config_path (will be created/updated)",
        llm_default_path,
        required=True,
    )
    llm_config_path = Path(llm_config_path_text).expanduser().resolve()
    llm_base_url = _prompt_text("llm.base_url", "https://openrouter.ai/api/v1", required=True)
    llm_api_key_default = os.environ.get("OPENAI_API_KEY", "")
    llm_api_key = _prompt_text("llm.api_key", llm_api_key_default, required=False)
    llm_model_raw = _prompt_text(
        "llm.model (recommended: full id like 'openai/gpt-4o'; bare names auto-add 'openai/')",
        "openai/google/gemini-3-flash-preview",
        required=True,
    )
    llm_model = _normalize_model_name(llm_model_raw)
    llm_multimodal = _prompt_bool("llm model supports multimodal (image input)", True)
    seed_context = _normalize_context_settings(seed.get("context"))
    print("\n[SETUP] Context compression config")
    context_user_history_tokens = _prompt_int(
        "context.user_history_compress_threshold_tokens",
        int(seed_context.get("user_history_compress_threshold_tokens", 1500)),
        min_value=0,
    )
    context_structured_agents = _prompt_int(
        "context.structured_call_info_compress_threshold_agents",
        int(seed_context.get("structured_call_info_compress_threshold_agents", 10)),
        min_value=1,
    )
    context_structured_tokens = _prompt_int(
        "context.structured_call_info_compress_threshold_tokens",
        int(seed_context.get("structured_call_info_compress_threshold_tokens", 2200)),
        min_value=0,
    )
    if llm_config_path.exists():
        overwrite = _prompt_bool(f"llm config exists, overwrite? {llm_config_path}", True)
        if not overwrite:
            print(f"[SETUP] keep existing llm config: {llm_config_path}")
        else:
            _write_minimal_llm_config(
                llm_config_path,
                base_url=llm_base_url,
                api_key=llm_api_key,
                model_name=llm_model,
                multimodal=llm_multimodal,
            )
            print(f"[OK] llm config written: {llm_config_path}")
    else:
        _write_minimal_llm_config(
            llm_config_path,
            base_url=llm_base_url,
            api_key=llm_api_key,
            model_name=llm_model,
            multimodal=llm_multimodal,
        )
        print(f"[OK] llm config written: {llm_config_path}")

    manifest: Dict[str, Any] = {
        "llm_config_path": str(llm_config_path),
        "runtime_root": runtime_root,
        "fleet_config_path": fleet_config_path,
        "proxy_env": {
            "http_proxy": http_proxy,
            "https_proxy": https_proxy,
            "all_proxy": all_proxy,
        },
        "context": {
            "user_history_compress_threshold_tokens": int(context_user_history_tokens),
            "structured_call_info_compress_threshold_agents": int(context_structured_agents),
            "structured_call_info_compress_threshold_tokens": int(context_structured_tokens),
        },
        "bots": bots,
    }
    return manifest_path, manifest


def _interactive_add_bot(
    manifest_path: Path,
) -> str:
    manifest = _load_json(manifest_path)
    bots = manifest.get("bots")
    if not isinstance(bots, list):
        bots = []
        manifest["bots"] = bots

    existing_ids = {
        str(item.get("bot_id") or "").strip()
        for item in bots
        if isinstance(item, dict) and str(item.get("bot_id") or "").strip()
    }
    new_payload = _prompt_bot_payload(index=len(existing_ids) + 1, existing_bot_ids=existing_ids)
    new_bot_id = str(new_payload.get("bot_id") or "").strip()
    bots.append(new_payload)
    _dump_json(manifest_path, manifest)
    return new_bot_id


def prepare_from_manifest(
    manifest_path: Path,
    *,
    write_fleet_to: Path | None = None,
) -> Dict[str, Any]:
    manifest = _load_json(manifest_path)
    context_settings = _normalize_context_settings(manifest.get("context"))
    llm_config_path = Path(_required_text(manifest.get("llm_config_path"), "llm_config_path", "<manifest>")).expanduser().resolve()
    if not llm_config_path.exists():
        raise SystemExit(f"[ERROR] llm_config_path does not exist: {llm_config_path}")

    runtime_root = Path(_required_text(manifest.get("runtime_root"), "runtime_root", "<manifest>")).expanduser().resolve()
    bots_raw = manifest.get("bots")
    if not isinstance(bots_raw, list) or not bots_raw:
        raise SystemExit("[ERROR] 'bots' must be a non-empty list")

    enabled_bots: List[Dict[str, Any]] = []
    seen_bot_ids = set()
    for item in bots_raw:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled", True)):
            continue
        bot_id = _required_text(item.get("bot_id"), "bot_id", "<manifest>")
        if bot_id in seen_bot_ids:
            raise SystemExit(f"[ERROR] Duplicate bot_id: {bot_id}")
        seen_bot_ids.add(bot_id)
        enabled_bots.append(item)
    if not enabled_bots:
        raise SystemExit("[ERROR] no enabled bots in manifest")

    _validate_webhook_ports(enabled_bots)

    fleet_config_path = (
        write_fleet_to.expanduser().resolve()
        if write_fleet_to
        else Path(
            str(manifest.get("fleet_config_path") or runtime_root / "fleet.generated.json")
        ).expanduser().resolve()
    )

    env = _child_process_env(manifest)

    generated_bots: List[Dict[str, Any]] = []
    for item in enabled_bots:
        bot_id = str(item["bot_id"]).strip()
        display_name = str(item.get("display_name") or bot_id).strip() or bot_id
        user_data_root = (runtime_root / bot_id).resolve()
        channels_cfg = _build_channels_config(item)

        channels_path = user_data_root / "cheapclaw" / "config" / "channels.json"
        _dump_json(channels_path, channels_cfg)

        bootstrap_cmd = [
            sys.executable,
            str(SERVICE_PATH),
            "--user-data-root",
            str(user_data_root),
            "--llm-config-path",
            str(llm_config_path),
            "--bot-id",
            bot_id,
            "--bot-display-name",
            display_name,
            "--fleet-config-path",
            str(fleet_config_path),
            "--bootstrap",
        ]
        _run_capture(bootstrap_cmd, env=env, check=True)
        _apply_context_settings_to_app_config(user_data_root, context_settings)

        fleet_item = {
            "bot_id": bot_id,
            "display_name": display_name,
            "enabled": True,
            "user_data_root": str(user_data_root),
            "llm_config_path": str(llm_config_path),
            "serve_webhooks": bool(item.get("serve_webhooks", False)),
        }
        if fleet_item["serve_webhooks"]:
            fleet_item["host"] = str(item.get("host") or "127.0.0.1")
            fleet_item["port"] = int(item.get("port") or 8765)
        generated_bots.append(fleet_item)

    fleet_payload = {"version": 1, "bots": generated_bots}
    _dump_json(fleet_config_path, fleet_payload)

    return {
        "status": "success",
        "manifest_path": str(manifest_path),
        "runtime_root": str(runtime_root),
        "fleet_config_path": str(fleet_config_path),
        "enabled_bot_count": len(generated_bots),
        "bots": generated_bots,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CheapClaw one-click fleet launcher")
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_json_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="Print machine-readable JSON output")

    p_init = sub.add_parser("init")
    p_init.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_init.add_argument("--force", action="store_true", help="Overwrite manifest if it already exists")
    p_init.add_argument("--open-config", action="store_true", help="Open manifest in editor after init")
    p_init.add_argument("--interactive", action="store_true", help="Configure manifest interactively")

    p_config = sub.add_parser("config")
    p_config.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_config.add_argument("--force", action="store_true", help="Overwrite manifest if it already exists")
    p_config.add_argument("--open-config", action="store_true", help="Open manifest in editor after config")
    p_config.add_argument("--interactive", dest="interactive", action="store_true", default=True, help="Configure manifest interactively (default: true)")
    p_config.add_argument("--no-interactive", dest="interactive", action="store_false", help="Disable interactive setup")

    for name in ("prepare", "up", "run"):
        p = sub.add_parser(name)
        p.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
        p.add_argument("--fleet-config-out", default="", help="Optional output path for generated fleet config")
        p.add_argument("--poll-interval", type=int, default=5, help="Polling interval for fleet run")
        p.add_argument(
            "--init-if-missing",
            dest="init_if_missing",
            action="store_true",
            default=True,
            help="Auto-copy template manifest when manifest is missing (default: true)",
        )
        p.add_argument(
            "--no-init-if-missing",
            dest="init_if_missing",
            action="store_false",
            help="Disable auto init when manifest is missing",
        )
        p.add_argument("--open-config-on-init", action="store_true", help="Open manifest when auto-initialized")
        p.add_argument(
            "--interactive",
            action="store_true",
            help="Run interactive setup when manifest is missing or still has placeholders",
        )
        p.add_argument(
            "--foreground",
            action="store_true",
            help="Run in foreground (debug mode). Default starts services in background.",
        )
        p.add_argument(
            "--with-web-console",
            dest="with_web_console",
            action="store_true",
            default=True,
            help="Ensure fleet web console is running (default: true)",
        )
        p.add_argument(
            "--no-web-console",
            dest="with_web_console",
            action="store_false",
            help="Do not start fleet web console automatically",
        )
        p.add_argument("--web-host", default="127.0.0.1", help="Fleet web console host")
        p.add_argument("--web-port", type=int, default=8787, help="Fleet web console port")
        _add_json_flag(p)

    p_list = sub.add_parser("list-bots")
    p_list.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    _add_json_flag(p_list)

    p_status = sub.add_parser("status")
    p_status.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_status.add_argument("--bot-id", default="", help="Optional bot id filter")
    _add_json_flag(p_status)

    p_start_all = sub.add_parser("start")
    p_start_all.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_start_all.add_argument("--bot-id", default="", help="Optional bot id; when omitted starts all enabled bots")
    p_start_all.add_argument("--poll-interval", type=int, default=5, help="Polling interval for started bot process")
    p_start_all.add_argument("--prepare-first", action="store_true", help="Run prepare before starting")
    p_start_all.add_argument(
        "--with-web-console",
        dest="with_web_console",
        action="store_true",
        default=True,
        help="Ensure fleet web console is running (default: true)",
    )
    p_start_all.add_argument(
        "--no-web-console",
        dest="with_web_console",
        action="store_false",
        help="Do not start fleet web console automatically",
    )
    p_start_all.add_argument("--web-host", default="127.0.0.1", help="Fleet web console host")
    p_start_all.add_argument("--web-port", type=int, default=8787, help="Fleet web console port")
    _add_json_flag(p_start_all)

    p_stop_all = sub.add_parser("stop")
    p_stop_all.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_stop_all.add_argument("--bot-id", default="", help="Optional bot id; when omitted stops all bots in manifest")
    p_stop_all.add_argument("--timeout-sec", type=int, default=8, help="Graceful stop timeout before SIGKILL")
    p_stop_all.add_argument(
        "--with-web-console",
        dest="with_web_console",
        action="store_true",
        default=True,
        help="Stop fleet web console together (default: true)",
    )
    p_stop_all.add_argument(
        "--no-web-console",
        dest="with_web_console",
        action="store_false",
        help="Stop bots only and keep fleet web console running",
    )
    _add_json_flag(p_stop_all)

    p_restart = sub.add_parser("restart")
    p_restart.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_restart.add_argument("--bot-id", default="", help="Optional bot id; when omitted restarts all bots")
    p_restart.add_argument("--poll-interval", type=int, default=5, help="Polling interval for started bot process")
    p_restart.add_argument("--timeout-sec", type=int, default=8, help="Graceful stop timeout before SIGKILL")
    p_restart.add_argument("--prepare-first", action="store_true", help="Run prepare before restart")
    p_restart.add_argument(
        "--with-web-console",
        dest="with_web_console",
        action="store_true",
        default=True,
        help="Ensure fleet web console is running (default: true)",
    )
    p_restart.add_argument(
        "--no-web-console",
        dest="with_web_console",
        action="store_false",
        help="Do not start fleet web console automatically",
    )
    p_restart.add_argument("--web-host", default="127.0.0.1", help="Fleet web console host")
    p_restart.add_argument("--web-port", type=int, default=8787, help="Fleet web console port")
    _add_json_flag(p_restart)

    p_logs = sub.add_parser("logs")
    p_logs.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_logs.add_argument("--bot-id", required=True, help="Bot id to inspect logs")
    p_logs.add_argument("--lines", type=int, default=200, help="How many lines to print")
    p_logs.add_argument(
        "--kind",
        choices=["loop", "service"],
        default="loop",
        help="loop=service.loop.log, service=cheapclaw_service.log",
    )

    p_add = sub.add_parser("add-bot")
    p_add.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_add.add_argument("--poll-interval", type=int, default=5, help="Polling interval for started bot process")
    p_add.add_argument("--prepare", dest="prepare_after", action="store_true", default=True, help="Run prepare after adding bot (default: true)")
    p_add.add_argument("--no-prepare", dest="prepare_after", action="store_false", help="Do not run prepare after adding bot")
    p_add.add_argument("--start", dest="start_after", action="store_true", default=True, help="Start new bot process after prepare (default: true)")
    p_add.add_argument("--no-start", dest="start_after", action="store_false", help="Do not start new bot process")
    _add_json_flag(p_add)

    p_start = sub.add_parser("start-bot")
    p_start.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_start.add_argument("--bot-id", required=True, help="Bot id to start")
    p_start.add_argument("--poll-interval", type=int, default=5, help="Polling interval for started bot process")
    p_start.add_argument("--prepare-first", action="store_true", help="Run prepare before starting bot")
    _add_json_flag(p_start)

    p_stop = sub.add_parser("stop-bot")
    p_stop.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_stop.add_argument("--bot-id", required=True, help="Bot id to stop")
    p_stop.add_argument("--timeout-sec", type=int, default=8, help="Graceful stop timeout before SIGKILL")
    _add_json_flag(p_stop)

    p_reload = sub.add_parser("reload-bot")
    p_reload.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_reload.add_argument("--bot-id", required=True, help="Bot id to reload")
    p_reload.add_argument("--poll-interval", type=int, default=5, help="Polling interval for started bot process")
    p_reload.add_argument("--prepare-first", action="store_true", help="Run prepare before reloading bot")
    p_reload.add_argument("--timeout-sec", type=int, default=8, help="Graceful stop timeout before SIGKILL")
    _add_json_flag(p_reload)

    p_web = sub.add_parser("web", help="Run fleet web console in foreground")
    p_web.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_web.add_argument("--host", default="127.0.0.1", help="Host to bind web console")
    p_web.add_argument("--port", type=int, default=8787, help="Port to bind web console")

    p_web_start = sub.add_parser("web-start", help="Start fleet web console in background")
    p_web_start.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_web_start.add_argument("--host", default="127.0.0.1", help="Host to bind web console")
    p_web_start.add_argument("--port", type=int, default=8787, help="Port to bind web console")
    _add_json_flag(p_web_start)

    p_web_stop = sub.add_parser("web-stop", help="Stop fleet web console")
    p_web_stop.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_web_stop.add_argument("--timeout-sec", type=int, default=8, help="Graceful stop timeout before SIGKILL")
    _add_json_flag(p_web_stop)

    p_web_status = sub.add_parser("web-status", help="Show fleet web console status")
    p_web_status.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    _add_json_flag(p_web_status)

    p_agent_system = sub.add_parser(
        "bot-agent-system",
        aliases=["bot-agent-sysyem"],
        help="Install or manage extra agent systems",
    )
    p_agent_system_sub = p_agent_system.add_subparsers(dest="agent_system_command", required=True)

    p_agent_add = p_agent_system_sub.add_parser("add", help="Install an agent system from a zip archive")
    p_agent_add.add_argument("archive", help="Path to an agent system zip archive")
    p_agent_add.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to fleet manifest JSON")
    p_agent_scope = p_agent_add.add_mutually_exclusive_group()
    p_agent_scope.add_argument("--bot-id", default="", help="Install only into one bot runtime")
    p_agent_scope.add_argument("--global", dest="install_global", action="store_true", help="Install into project assets/agent_library")
    p_agent_add.add_argument("--reload-after", action="store_true", help="Reload target bot after install (requires --bot-id)")
    p_agent_add.add_argument("--poll-interval", type=int, default=5, help="Polling interval when reloading bot")
    _add_json_flag(p_agent_add)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    json_output = bool(getattr(args, "json", False))
    manifest_path = Path(args.manifest).expanduser().resolve()

    if args.command in {"init", "config"}:
        if bool(args.interactive):
            seed_manifest: Dict[str, Any] = {}
            if manifest_path.exists():
                seed_manifest = _load_json(manifest_path)
            target_manifest_path, payload = _interactive_manifest_setup(
                manifest_path,
                seed_manifest=seed_manifest,
            )
            if target_manifest_path.exists() and not bool(args.force):
                should_overwrite = _prompt_bool(
                    f"manifest exists, overwrite? {target_manifest_path}",
                    False,
                )
                if not should_overwrite:
                    print("[ABORT] manifest not changed")
                    return 1
            _dump_json(target_manifest_path, payload)
            print(f"[OK] manifest saved: {target_manifest_path}")
            print("[NEXT] run: python scripts/fleet_one_click.py up --manifest", target_manifest_path)
            if bool(args.open_config):
                _open_config_file(target_manifest_path)
            return 0

        created = _scaffold_manifest(manifest_path, force=bool(args.force))
        if created:
            print(f"[OK] manifest created from template: {manifest_path}")
        else:
            print(f"[OK] manifest already exists: {manifest_path}")
        print("[NEXT] fill llm_config_path/runtime_root and bot config (credentials if needed) before running 'up'")
        if bool(args.open_config):
            _open_config_file(manifest_path)
        return 0

    if args.command == "web":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
        if not FLEET_WEB_CONSOLE_PATH.exists():
            raise SystemExit(f"[ERROR] fleet web script not found: {FLEET_WEB_CONSOLE_PATH}")
        run_cmd = [
            sys.executable,
            str(FLEET_WEB_CONSOLE_PATH),
            "--manifest",
            str(manifest_path),
            "--host",
            str(args.host),
            "--port",
            str(int(args.port)),
        ]
        env = os.environ.copy()
        return _run(run_cmd, env=env, check=False)

    if args.command == "web-start":
        payload = _start_fleet_web(
            manifest_path,
            host=str(args.host or "127.0.0.1"),
            port=int(args.port or 8787),
        )
        if json_output:
            _print_json(payload)
        else:
            _format_web_payload(payload, title="WEB")
        return 0

    if args.command == "web-stop":
        if manifest_path.exists():
            payload = _stop_fleet_web(
                manifest_path,
                timeout_sec=int(args.timeout_sec),
            )
        else:
            payload = _stop_fleet_web_fallback(
                timeout_sec=int(args.timeout_sec),
            )
        if json_output:
            _print_json(payload)
        else:
            _format_web_payload(payload, title="WEB")
        return 0

    if args.command == "web-status":
        payload = _fleet_web_status_payload(manifest_path)
        if json_output:
            _print_json(payload)
        else:
            _format_web_payload(payload, title="WEB")
        return 0

    if args.command == "bot-agent-system":
        if args.agent_system_command == "add":
            archive_path = Path(str(args.archive)).expanduser().resolve()
            bot_id = str(args.bot_id or "").strip()
            if bool(args.reload_after) and not bot_id:
                raise SystemExit("[ERROR] --reload-after requires --bot-id")

            if bot_id:
                if not manifest_path.exists():
                    raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
                _manifest, _target, user_data_root, _llm = _resolve_bot_from_manifest(manifest_path, bot_id)
                destination_root = (user_data_root / "agent_library").resolve()
                payload = _install_agent_system_archive(archive_path, destination_root)
                payload["scope"] = "bot_runtime"
                payload["bot_id"] = bot_id
                payload["next_step"] = f"cheapclaw reload-bot --bot-id {bot_id}"
                if bool(args.reload_after):
                    reload_result = _stop_single_bot_from_manifest(
                        manifest_path,
                        bot_id=bot_id,
                        timeout_sec=8,
                    )
                    start_result = _start_single_bot_from_manifest(
                        manifest_path,
                        bot_id=bot_id,
                        poll_interval=int(args.poll_interval),
                    )
                    payload["reload"] = {"stop": reload_result, "start": start_result}
                    payload["next_step"] = "已自动 reload 目标 bot"
            else:
                destination_root = AGENT_LIBRARY_ROOT.resolve()
                payload = _install_agent_system_archive(archive_path, destination_root)
                payload["scope"] = "global_assets"
                payload["next_step"] = "运行 `cheapclaw prepare` 或 `cheapclaw reload-bot --bot-id <id> --prepare-first` 使 bot 生效"

            if json_output:
                _print_json(payload)
            else:
                _format_agent_system_payload(payload)
            return 0

    if args.command == "list-bots":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
        bots = _list_manifest_bots(manifest_path, enabled_only=False)
        payload = {
            "status": "success",
            "manifest_path": str(manifest_path),
            "bot_count": len(bots),
            "bots": [
                {
                    "bot_id": str(item.get("bot_id") or ""),
                    "display_name": str(item.get("display_name") or item.get("bot_id") or ""),
                    "channel": str(item.get("channel") or ""),
                    "enabled": bool(item.get("enabled", True)),
                    "serve_webhooks": bool(item.get("serve_webhooks", False)),
                }
                for item in bots
            ],
        }
        if json_output:
            _print_json(payload)
        else:
            _format_list_bots_payload(payload)
        return 0

    if args.command == "status":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
        bot_id = str(args.bot_id or "").strip()
        if bot_id:
            payload = {
                "status": "success",
                "manifest_path": str(manifest_path),
                "bot_count": 1,
                "running_count": 1 if _status_single_bot_from_manifest(manifest_path, bot_id).get("alive") else 0,
                "bots": [_status_single_bot_from_manifest(manifest_path, bot_id)],
            }
        else:
            payload = _status_all_bots_from_manifest(manifest_path)
        if json_output:
            payload = {
                **payload,
                "web_console": _fleet_web_status_payload(manifest_path),
            }
            _print_json(payload)
        else:
            _format_status_payload(payload)
            _format_web_payload(_fleet_web_status_payload(manifest_path), title="WEB")
        return 0

    if args.command == "start":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
        if bool(args.prepare_first):
            result = prepare_from_manifest(manifest_path, write_fleet_to=None)
            if json_output:
                _print_json(result)
            else:
                _format_prepare_payload(result)
        bot_id = str(args.bot_id or "").strip()
        if bot_id:
            payload = _start_single_bot_from_manifest(
                manifest_path,
                bot_id=bot_id,
                poll_interval=int(args.poll_interval),
            )
        else:
            payload = _start_all_bots_from_manifest(
                manifest_path,
                poll_interval=int(args.poll_interval),
            )
        if json_output:
            _print_json(payload)
        else:
            _format_start_stop_payload(payload, title="START")
        if bool(args.with_web_console):
            web_payload = _start_fleet_web(
                manifest_path,
                host=str(args.web_host or "127.0.0.1"),
                port=int(args.web_port or 8787),
            )
            if json_output:
                _print_json(web_payload)
            else:
                _format_web_payload(web_payload, title="WEB")
        return 0

    if args.command == "stop":
        if not manifest_path.exists():
            bot_id = str(args.bot_id or "").strip()
            if bot_id:
                raise SystemExit(
                    f"[ERROR] manifest not found: {manifest_path}\n"
                    f"[HINT] cannot stop specific bot '{bot_id}' without manifest; "
                    "use `cheapclaw stop` to stop all by fallback scan."
                )
            payload = _stop_all_fallback_without_manifest(
                timeout_sec=int(args.timeout_sec),
            )
            if json_output:
                _print_json(payload)
            else:
                _format_emergency_stop_payload(payload)
            return 0
        bot_id = str(args.bot_id or "").strip()
        if bot_id:
            payload = _stop_single_bot_from_manifest(
                manifest_path,
                bot_id=bot_id,
                timeout_sec=int(args.timeout_sec),
            )
        else:
            payload = _stop_all_bots_from_manifest(
                manifest_path,
                timeout_sec=int(args.timeout_sec),
            )
        if json_output:
            _print_json(payload)
        else:
            _format_start_stop_payload(payload, title="STOP")
        if bool(args.with_web_console):
            web_payload = _stop_fleet_web(
                manifest_path,
                timeout_sec=int(args.timeout_sec),
            )
            if json_output:
                _print_json(web_payload)
            else:
                _format_web_payload(web_payload, title="WEB")
        return 0

    if args.command == "restart":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
        if bool(args.prepare_first):
            result = prepare_from_manifest(manifest_path, write_fleet_to=None)
            if json_output:
                _print_json(result)
            else:
                _format_prepare_payload(result)
        bot_id = str(args.bot_id or "").strip()
        if bot_id:
            stop_payload = _stop_single_bot_from_manifest(
                manifest_path,
                bot_id=bot_id,
                timeout_sec=int(args.timeout_sec),
            )
            start_payload = _start_single_bot_from_manifest(
                manifest_path,
                bot_id=bot_id,
                poll_interval=int(args.poll_interval),
            )
        else:
            stop_payload = _stop_all_bots_from_manifest(
                manifest_path,
                timeout_sec=int(args.timeout_sec),
            )
            start_payload = _start_all_bots_from_manifest(
                manifest_path,
                poll_interval=int(args.poll_interval),
            )
        payload = {
            "status": "success",
            "manifest_path": str(manifest_path),
            "stop": stop_payload,
            "start": start_payload,
        }
        if json_output:
            _print_json(payload)
        else:
            _format_restart_payload(payload)
        if bool(args.with_web_console):
            web_payload = _start_fleet_web(
                manifest_path,
                host=str(args.web_host or "127.0.0.1"),
                port=int(args.web_port or 8787),
            )
            if json_output:
                _print_json(web_payload)
            else:
                _format_web_payload(web_payload, title="WEB")
        return 0

    if args.command == "logs":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
        status = _status_single_bot_from_manifest(manifest_path, str(args.bot_id))
        log_key = "loop_log_path" if str(args.kind) == "loop" else "service_log_path"
        log_path = Path(str(status.get(log_key) or "")).expanduser().resolve()
        _print_log_tail(log_path, lines=int(args.lines))
        return 0

    if args.command == "add-bot":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}\n[HINT] run `cheapclaw config` first.")
        new_bot_id = _interactive_add_bot(manifest_path)
        print(f"[OK] bot added to manifest: {new_bot_id}")
        if bool(args.prepare_after):
            result = prepare_from_manifest(manifest_path, write_fleet_to=None)
            if json_output:
                _print_json(result)
            else:
                _format_prepare_payload(result)
        if bool(args.start_after):
            start_result = _start_single_bot_from_manifest(
                manifest_path,
                bot_id=new_bot_id,
                poll_interval=int(args.poll_interval),
            )
            if json_output:
                _print_json(start_result)
            else:
                _format_start_stop_payload(start_result, title="START")
        return 0

    if args.command == "start-bot":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
        if bool(args.prepare_first):
            result = prepare_from_manifest(manifest_path, write_fleet_to=None)
            if json_output:
                _print_json(result)
            else:
                _format_prepare_payload(result)
        start_result = _start_single_bot_from_manifest(
            manifest_path,
            bot_id=str(args.bot_id),
            poll_interval=int(args.poll_interval),
        )
        if json_output:
            _print_json(start_result)
        else:
            _format_start_stop_payload(start_result, title="START")
        return 0

    if args.command == "stop-bot":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
        stop_result = _stop_single_bot_from_manifest(
            manifest_path,
            bot_id=str(args.bot_id),
            timeout_sec=int(args.timeout_sec),
        )
        if json_output:
            _print_json(stop_result)
        else:
            _format_start_stop_payload(stop_result, title="STOP")
        return 0

    if args.command == "reload-bot":
        if not manifest_path.exists():
            raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
        if bool(args.prepare_first):
            result = prepare_from_manifest(manifest_path, write_fleet_to=None)
            if json_output:
                _print_json(result)
            else:
                _format_prepare_payload(result)
        stop_result = _stop_single_bot_from_manifest(
            manifest_path,
            bot_id=str(args.bot_id),
            timeout_sec=int(args.timeout_sec),
        )
        if json_output:
            _print_json(stop_result)
        else:
            _format_start_stop_payload(stop_result, title="STOP")
        start_result = _start_single_bot_from_manifest(
            manifest_path,
            bot_id=str(args.bot_id),
            poll_interval=int(args.poll_interval),
        )
        if json_output:
            _print_json(start_result)
        else:
            _format_start_stop_payload(start_result, title="START")
        return 0

    if not manifest_path.exists():
        if bool(args.interactive):
            seed_manifest: Dict[str, Any] = {}
            target_manifest_path, payload = _interactive_manifest_setup(
                manifest_path,
                seed_manifest=seed_manifest,
            )
            _dump_json(target_manifest_path, payload)
            manifest_path = target_manifest_path
            print(f"[OK] manifest saved: {manifest_path}")
            if bool(args.open_config_on_init):
                _open_config_file(manifest_path)
        else:
            if not bool(args.init_if_missing):
                raise SystemExit(f"[ERROR] manifest not found: {manifest_path}")
            _scaffold_manifest(manifest_path, force=False)
            print(f"[INIT] manifest auto-created from template: {manifest_path}")
            print("[NEXT] edit manifest first, then rerun the same command.")
            if bool(args.open_config_on_init):
                _open_config_file(manifest_path)
            return 0

    manifest_preview = _load_json(manifest_path)
    placeholder_fields = _scan_placeholder_fields(manifest_preview)
    if placeholder_fields and bool(args.interactive):
        print("[SETUP] placeholder values detected, entering interactive setup")
        target_manifest_path, payload = _interactive_manifest_setup(
            manifest_path,
            seed_manifest=manifest_preview,
        )
        _dump_json(target_manifest_path, payload)
        manifest_path = target_manifest_path
        manifest_preview = payload
        placeholder_fields = _scan_placeholder_fields(manifest_preview)
    if placeholder_fields:
        print("[ERROR] manifest still contains placeholder values:")
        for item in placeholder_fields[:20]:
            print(" -", item)
        if len(placeholder_fields) > 20:
            print(f" - ... and {len(placeholder_fields) - 20} more")
        print(f"[HINT] Please edit: {manifest_path}")
        if bool(args.open_config_on_init):
            _open_config_file(manifest_path)
        return 2

    fleet_out = Path(args.fleet_config_out).expanduser().resolve() if str(args.fleet_config_out).strip() else None
    result = prepare_from_manifest(manifest_path, write_fleet_to=fleet_out)
    if json_output:
        _print_json(result)
    else:
        _format_prepare_payload(result)

    if args.command == "prepare":
        print("[OK] prepare done")
        return 0

    if args.command in {"up", "run"} and not bool(args.foreground):
        payload = _start_all_bots_from_manifest(
            manifest_path,
            poll_interval=int(args.poll_interval),
        )
        if json_output:
            _print_json(payload)
        else:
            _format_start_stop_payload(payload, title="START")
        if bool(args.with_web_console):
            web_payload = _start_fleet_web(
                manifest_path,
                host=str(args.web_host or "127.0.0.1"),
                port=int(args.web_port or 8787),
            )
            if json_output:
                _print_json(web_payload)
            else:
                _format_web_payload(web_payload, title="WEB")
        print("[OK] services are running in background; use `cheapclaw status` and `cheapclaw logs --bot-id <id>`")
        return 0

    run_cmd = [
        sys.executable,
        str(SERVICE_PATH),
        "--fleet-config-path",
        str(result["fleet_config_path"]),
        "--run-fleet",
        "--poll-interval",
        str(max(1, int(args.poll_interval))),
    ]
    manifest = _load_json(manifest_path)
    env = _child_process_env(manifest)
    return _run(run_cmd, env=env, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
