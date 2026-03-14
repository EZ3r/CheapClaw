<p align="center">
  <img src="assets/logo.png" alt="CheapClaw logo" width="220">
</p>

<p align="center">
  English | <a href="README.zh-CN.md">简体中文</a>
</p>

# CheapClaw

CheapClaw is a multi-bot orchestration app built on top of the `infiagent` SDK.

## Introduction

CheapClaw started from a very practical feeling: watching an OpenClaw token bill hurt.

The goal of CheapClaw is to keep most of the useful capabilities of OpenClaw, extend some of them, and improve how the same underlying models behave on complex tasks so the total system becomes cheaper in practice.

The most important difference is mixed strong-model and weak-model execution inside a single agent loop. In OpenClaw, you can already split work across different bots, letting a stronger model plan and a weaker model execute. But the execution bot has a real weakness: if the task stops unfolding the way the plan expected, the weaker model often collapses.

CheapClaw leans on the characteristics of the `infiagent` SDK instead. Within one agent loop, it thinks in stages and periodically truncates earlier conversation history, for example every 10 or 20 steps. That gives smaller models a much shorter working context while also letting stronger-model planning be corrected continuously in real time.

So while replying to “hello” still goes through a fairly heavy pipeline, just like OpenClaw, the benefit shows up on more complex tasks such as:

- summarizing 500 PDFs from a list into a structured table
- producing an EV research report with 100+ pages

On tasks like these, this fine-grained mixed execution pattern can save a meaningful amount of money.

CheapClaw also adds several features that make vertical or domain-specific work more stable:

- In addition to skills, you can attach complete agent systems to a bot. CheapClaw supports custom single-agent or multi-agent systems, as well as custom tools, so you can use your own or other people’s domain-specific agent systems for work such as scientific research or academic writing.
- Memory is fully layered. The bot’s supervisor only maintains user interaction memory, while the worker-side agent systems only maintain memory that belongs to the same task type. They do not get polluted by unrelated task noise.
- Users can build their own agent systems from configuration files based on the infiagent project conventions and add them to a bot. Do not enable the built-in `human_in_loop` tool for those agent systems.

![CheapClaw framework](assets/framework.png)

## What CheapClaw Does

- Runs one or more bots on top of a shared LLM config.
- Lets you start from built-in web chat, then add social channels later.
- Routes new user messages intelligently:
  - direct reply
  - continue an old `task_id`
  - append to a running task
  - start a new task branch
- Supports custom agent systems installed as zip packages.
- Includes a built-in dashboard for configuration, monitoring, local chat, and group chat.

## Install

### Install from PyPI

```bash
python -m pip install -U cheapclaw
```

### Install from source

```bash
git clone https://github.com/polyuiislab/CheapClaw.git
cd CheapClaw
python -m pip install -U -e .
```

## Quick Start

### Option A: the normal first-time setup

```bash
cheapclaw config --interactive
cheapclaw up
```

Open the dashboard:

```text
http://127.0.0.1:8787/dashboard
```

### Option B: create a template first

```bash
cheapclaw init
cheapclaw config --interactive
cheapclaw up
```

`cheapclaw init` creates the default manifest template if it does not exist.

## Default Paths

By default, CheapClaw stores runtime files under:

```text
~/cheapclaw
```

The most important default paths are:

```text
~/cheapclaw/fleet.manifest.json
~/cheapclaw/runtime
~/cheapclaw/runtime/config/llm_config.yaml
```

Most CLI commands use that default manifest automatically, so you usually do not need to pass `--manifest`.

## If You Only Have One API Key and One Base URL

That is enough.

During `cheapclaw config --interactive`, fill:

- `llm.base_url`
- `llm.api_key`
- `llm.model`

CheapClaw writes one shared LLM config file and all bots use it by default.

For InfiAgent-style model ids, it is best to enter the full model id explicitly.

Examples:

- `openai/gpt-4o`
- `openai/google/gemini-3-flash-preview`
- in some deployments, `openrouter/openai/gpt-4o`

Interactive setup only auto-adds `openai/` when you enter a bare model name such as `gpt-4o`. If your value already contains a prefix, CheapClaw keeps it unchanged.

## Recommended First Run Flow

If this is your first deployment, the smoothest path is:

1. Configure one `localweb` bot first.
2. Start CheapClaw.
3. Open the dashboard and confirm local web chat works.
4. Add Telegram or Feishu only after localweb is stable.

This avoids debugging credentials, proxies, and channel permissions at the same time.

## Dashboard

The built-in dashboard is available at:

```text
http://127.0.0.1:8787/dashboard
```

From there you can:

- view all bots
- inspect running state
- edit fleet configuration
- use local web chat
- create multi-bot local group conversations
- inspect history
- operate CheapClaw without hand-editing runtime files

### Exposing the dashboard on a server

If you want LAN or remote access:

```bash
cheapclaw up --web-host 0.0.0.0 --web-port 8787
```

Then open:

```text
http://<server-ip>:8787/dashboard
```

### Dashboard example

![CheapClaw dashboard](assets/web_shot.png)

This screenshot shows the web console after installing an extra custom agent system by CLI, so the bot can orchestrate more than the built-in systems.

## Adding Bots

You can add bots either from the dashboard or from the CLI.

### Add a bot from the CLI

```bash
cheapclaw add-bot
```

The command is interactive and will ask for:

- bot id
- display name
- channel type
- credentials
- whether to start the bot immediately

### Manage a single bot

```bash
cheapclaw start-bot --bot-id bot_1
cheapclaw stop-bot --bot-id bot_1
cheapclaw reload-bot --bot-id bot_1 --prepare-first
```

### Inspect bots

```bash
cheapclaw list-bots
cheapclaw status
cheapclaw status --bot-id bot_1
cheapclaw logs --bot-id bot_1
```

## Local Web Chat

`localweb` is the easiest channel because it needs no third-party credentials.

It is useful for:

- first-run verification
- demos
- local testing
- multi-bot group chat experiments

In local web group chats:

- you can include multiple localweb bots
- `@bot_id` routes the message to the mentioned bot
- if a message mentions one or more bots, only the mentioned bots are triggered

## Connecting Telegram

To add a Telegram bot, prepare:

- a BotFather bot token
- optional allowed chat ids if you want to restrict usage

During `cheapclaw config --interactive` or `cheapclaw add-bot`:

- choose channel `telegram`
- fill `telegram.bot_token`
- optionally fill `telegram.allowed_chats`

Start the bot:

```bash
cheapclaw start-bot --bot-id telegram_bot_1 --prepare-first
```

If Telegram returns `404` on `getUpdates`, the token is invalid or malformed.

## Connecting Feishu

To add a Feishu bot, prepare:

- `app_id`
- `app_secret`

During setup:

- choose channel `feishu`
- fill `feishu.app_id`
- fill `feishu.app_secret`

CheapClaw uses Feishu long connection mode by default.

If your environment uses a SOCKS proxy, make sure the environment actually intends to use it. CheapClaw uses explicit proxy settings from its manifest-configured environment, and current releases ship with `python-socks`.

## Other Channels

CheapClaw also has support for:

- Discord
- WhatsApp Cloud API
- QQ through OneBot v11 bridge
- WeChat through OneBot v11 bridge

For most users, it is still best to validate `localweb`, then Telegram or Feishu, before adding bridge-based channels.

## Installing a Custom Agent System

CheapClaw can install a full agent system from a zip archive.

Install to one bot:

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1
```

Install and reload immediately:

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1 --reload-after
```

Install globally into shared project assets:

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --global
```

Notes:

- `--bot-id` installs only into that bot runtime
- `--reload-after` restarts the target bot so the new system is visible immediately
- runtime-installed systems are preserved across normal prepare and reload flows

## Common Commands

### Main lifecycle

```bash
cheapclaw init
cheapclaw config --interactive
cheapclaw prepare
cheapclaw up
cheapclaw stop
cheapclaw restart
```

### Per-bot control

```bash
cheapclaw add-bot
cheapclaw start-bot --bot-id bot_1
cheapclaw stop-bot --bot-id bot_1
cheapclaw reload-bot --bot-id bot_1 --prepare-first
```

### Status and logs

```bash
cheapclaw list-bots
cheapclaw status
cheapclaw status --bot-id bot_1
cheapclaw logs --bot-id bot_1
```

### Dashboard control

```bash
cheapclaw web-start
cheapclaw web-stop
cheapclaw web-status
```

### Agent system management

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1 --reload-after
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --global
```

## Cases

### Mobile chat: Feishu + Telegram

The two screenshots below show CheapClaw connected to mobile chat surfaces.

| Feishu | Telegram |
|---|---|
| ![CheapClaw on Feishu](assets/screenshot.jpg) | ![CheapClaw on Telegram](assets/screenshot_2_telegram.jpg) |

This is a practical deployment pattern:

- keep orchestration on your server
- keep user interaction in familiar messaging apps
- let the supervisor decide when to reuse or branch work

### Dashboard + expanding agent systems

![CheapClaw web console](assets/web_shot.png)

This case shows a bot that started with built-in orchestration systems, then gained a third system after a CLI install.

## Notes and Operational Advice

### Watchdog defaults

CheapClaw now defaults to:

```text
watchdog_interval_sec = 86400
```

That is 24 hours.

This keeps maintenance noise low by default. If you want a shorter interval, edit the bot runtime config after initialization:

```text
~/cheapclaw/runtime/<bot_id>/cheapclaw/config/app_config.json
```

### `system-add.md`

CheapClaw updates only its own reserved block inside `system-add.md`.

That means:

- you can keep your own short notes outside the reserved block
- an agent can also keep short preference or experience notes there
- CheapClaw will not replace the whole file

### Stop before deleting runtime files

Always stop services first:

```bash
cheapclaw stop
```

Then edit or remove runtime files if you really need to.

### Agent systems are runtime-visible, not auto-injected

CheapClaw only exposes the agent systems that actually exist in the current bot runtime.

That means:

- built-in `infiagent` agent systems are not auto-injected into CheapClaw bots
- a bot only sees the systems you explicitly prepared or installed for it

### Start with `localweb` when something feels off

If a social channel is not responding, first verify:

- the dashboard is reachable
- a localweb bot can reply
- the shared LLM config is valid

Then move on to Telegram or Feishu credentials.

## Repository Layout

```text
assets/                    built-in config, agent systems, screenshots
docs/                      additional documentation
scripts/                   CLI and dashboard startup logic
tools_library/             CheapClaw runtime tools
web/                       dashboard frontend files
cheapclaw_service.py       main service loop
README.md                  this guide
```

## Extra Documentation

- [Simplified Chinese README](README.zh-CN.md)
- [Chinese CLI tutorial](docs/CHEAPCLAW_CLI_TUTORIAL_ZH.md)
- [SDK guide](SDK_GUIDE.md)
