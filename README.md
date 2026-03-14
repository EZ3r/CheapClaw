<p align="center">
  <img src="assets/logo.png" alt="CheapClaw logo" width="220">
</p>

<p align="center">
  English | <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/cheapclaw/"><img src="https://img.shields.io/pypi/v/cheapclaw" alt="PyPI"></a>
  <a href="https://pypi.org/project/cheapclaw/"><img src="https://img.shields.io/pypi/pyversions/cheapclaw" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License"></a>
</p>

# CheapClaw

CheapClaw is a multi-bot orchestration app built on top of the `infiagent` SDK.

## Introduction

CheapClaw started from a simple feeling: watching my own OpenClaw token bill hurt.

At the same time, most people just want to try OpenClaw rather than use it heavily in practice. So the goal of CheapClaw is **not** to minimize code volume, nor to reproduce OpenClaw across every possible scenario. Instead, CheapClaw aims to preserve most of OpenClaw’s functionality, extend some of its capabilities, and improve how the same models handle complex tasks more cost-effectively.

The main difference lies in **mixed execution of strong and weak models within a single agent loop**. In OpenClaw, you can already rely on different bots: a stronger-model bot can make the plan, and a weaker execution bot can carry it out. However, the execution bot has a real weakness: if the actual progress during execution does not follow the original plan as expected, the weaker model often collapses.

Leveraging the characteristics of the InfiAgent SDK, CheapClaw allows a single agent loop to think in stages and directly cut away the entire earlier conversation history at intervals, such as every 10 or 20 steps. This not only keeps smaller models operating within a shorter context window, but also provides continuously updated plans from stronger models in time.

So although even replying to something as simple as “hello” still goes through a relatively heavy workflow—just like OpenClaw—CheapClaw becomes much more worthwhile once you start assigning more complex tasks, such as:

- “summarize 500 PDFs from a list into one table”
- “complete an EV research report of at least 100 pages”

For this kind of task, fine-grained mixed execution can save you a meaningful amount of money.

## Core Capabilities

- Connects to Feishu, Telegram, and web chat, which are already tested, and also supports WhatsApp and Discord.
- Supports multiple bots and bot customization. You can also write `system-add.md` inside a bot runtime directory to give that bot additional role instructions and prompt guidance.
- Supports skills and multi-stage skill reveal.

## Additional Capabilities

CheapClaw also supports a number of extra features that let users improve stability for vertical or domain-specific tasks:

- In addition to skills, you can attach complete agent systems to a bot. It supports custom single-agent or multi-agent systems and custom tools, allowing you to rely on your own or third-party domain-specific agents to complete complex tasks more reliably, such as scientific research or academic writing. See the documentation for usage details.
- Memory is fully layered. The bot's supervisor only maintains user interaction memory, while the dispatched sub-agent systems only maintain memory that belongs to similar work under the same task, without being polluted by unrelated task noise.
- Users can build their own agent systems by editing configuration files according to the infiagent project conventions, then add them under a specific bot. Do not use the built-in `human_in_loop` tool inside those agent systems.
- At the last layer of the agent system, different sub-agents can independently configure their thinking model, execution model, image reading model, compression model, and more.

![CheapClaw framework](assets/framework.png)

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

### Option A: normal first-time setup

```bash
cheapclaw config --interactive
cheapclaw up
```

Then open:

```text
http://127.0.0.1:8787/dashboard
```

### Option B: create the template first

```bash
cheapclaw init
cheapclaw config --interactive
cheapclaw up
```

`cheapclaw init` creates `fleet.manifest.json` in the default location.

## Default Paths

By default, CheapClaw stores its runtime files under:

```text
~/cheapclaw
```

The most important paths are:

```text
~/cheapclaw/fleet.manifest.json
~/cheapclaw/runtime
~/cheapclaw/runtime/config/llm_config.yaml
```

Most CLI commands automatically use this default manifest, so you usually do not need to pass `--manifest` manually.

## If You Only Have One API Key and One Base URL

That is already enough.

In `cheapclaw config --interactive`, fill:

- `llm.base_url`
- `llm.api_key`
- `llm.model`

CheapClaw will generate a shared `llm_config.yaml`, and all bots use it by default.

### How to fill `llm.model`

This part is easy to misunderstand, so here is the direct rule:

Use your model's API format prefix plus the model name provided by the service vendor.

For example, for `openai/gpt-4o` on OpenRouter, because that provider supports both OpenAI-format and OpenRouter-format calling styles, the model name may be written as:

- `openai/openai/gpt-4o`
- `openrouter/openai/gpt-4o`

In other words, you usually need to put the API-format prefix in front of the model name.

The default config examples use `gemini-3-flash`. You can keep the thinking model unchanged and swap other model slots to smaller models to save money. You can also mix local and cloud models. See the infiagent repository for details.

## Recommended First-Time Path

If this is your first CheapClaw deployment, the safest path is:

1. Create one `localweb` bot first
2. Start CheapClaw
3. Open the dashboard and verify that web chat replies normally
4. Only then connect Telegram or Feishu

This avoids debugging all of these at once:

- LLM config
- channel credentials
- network proxy
- platform permissions

## Dashboard

The built-in control console is available at:

```text
http://127.0.0.1:8787/dashboard
```

From this page you can:

- view all bots
- check running status
- edit fleet config
- use local web chat
- create multi-bot group chats
- read conversation history
- manage runtime state without editing files by hand

### Expose the dashboard on a server

If you want LAN or remote access:

```bash
cheapclaw up --web-host 0.0.0.0 --web-port 8787
```

Then access:

```text
http://<server-ip>:8787/dashboard
```

### Dashboard example

![CheapClaw dashboard](assets/web_shot.png)

This screenshot shows a bot whose orchestration capability expanded from the built-in systems to more systems after installing an extra custom agent system through the CLI.

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1 --reload-after
```

## Adding Bots

You can add bots in two ways:

- Dashboard
- CLI

### Add a bot from the CLI

```bash
cheapclaw add-bot
```

This command is interactive and will ask for:

- bot id
- display name
- channel type
- credentials
- whether to start immediately

### Manage a single bot

```bash
cheapclaw start-bot --bot-id bot_1
cheapclaw stop-bot --bot-id bot_1
cheapclaw reload-bot --bot-id bot_1 --prepare-first
```

### View bot status

```bash
cheapclaw list-bots
cheapclaw status
cheapclaw status --bot-id bot_1
cheapclaw logs --bot-id bot_1
```

## Local Web Chat (`localweb`)

`localweb` is the best first testing channel because it does not require any third-party credentials.

Good use cases:

- first validation
- local debugging
- demos
- multi-bot group chat experiments

In localweb group chat:

- you can place multiple localweb bots into one conversation
- `@bot_id` routes the message to the corresponding bot
- if a message mentions one or more bots, only the mentioned bots will be triggered

## Connecting Telegram

Prepare:

- a BotFather bot token
- optional `allowed_chats`

Inside `cheapclaw config --interactive` or `cheapclaw add-bot`:

- choose `telegram`
- fill `telegram.bot_token`
- if needed, fill `telegram.allowed_chats`

Start it:

```bash
cheapclaw start-bot --bot-id telegram_bot_1 --prepare-first
```

If Telegram polling returns `404 getUpdates`, that usually means the token is wrong or malformed.

## Connecting Feishu

Prepare:

- `app_id`
- `app_secret`

During setup:

- choose `feishu`
- fill `feishu.app_id`
- fill `feishu.app_secret`

CheapClaw uses Feishu long connection mode by default.

If your environment uses a proxy, make sure CheapClaw is actually reading the proxy configuration you intend it to use.

## Other Channels

Current support also includes:

- Discord
- WhatsApp Cloud API
- QQ through OneBot v11 bridge
- WeChat through OneBot v11 bridge

For most users, it is still best to validate:

1. `localweb`
2. Telegram or Feishu

before adding bridge-based channels.

## Installing a Custom Agent System

CheapClaw supports installing a complete agent system as a zip package.

Install into a single bot:

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1
```

Install and reload immediately:

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1 --reload-after
```

Install into global shared assets:

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --global
```

Notes:

- `--bot-id` installs only into that bot runtime
- `--reload-after` restarts that bot immediately so the new system becomes visible
- runtime-installed systems are preserved during normal `prepare / reload` flows and are not automatically deleted

## Common Commands

### Main flow

```bash
cheapclaw init
cheapclaw config --interactive
cheapclaw prepare
cheapclaw up
cheapclaw stop
cheapclaw restart
```

### Single-bot control

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

## Use Cases

### Mobile access: Feishu + Telegram

The two screenshots below show CheapClaw connected to mobile chat channels.

| Feishu | Telegram |
|---|---|
| ![CheapClaw on Feishu](assets/screenshot.jpg) | ![CheapClaw on Telegram](assets/screenshot_2_telegram.jpg) |

This is a very practical deployment shape:

- orchestration logic runs on your server
- users still interact through familiar messaging apps
- the supervisor decides whether to reuse an old task or open a new branch

### Web console + expanding agent systems

![CheapClaw web console](assets/web_shot.png)

This case shows a bot that originally had only built-in systems, then gained more orchestration capability after an extra system was installed through the CLI.

## Notes

### 1. Watchdog is low-frequency by default

The current default is:

```text
watchdog_interval_sec = 86400
```

That is 24 hours.

This reduces maintenance noise. If you really need a shorter interval, change it after initialization in:

```text
~/cheapclaw/runtime/<bot_id>/cheapclaw/config/app_config.json
```

### 2. `system-add.md` is now coexistence-friendly

CheapClaw only updates its own reserved block inside `system-add.md`.

So now:

- you can write your own prompts outside the reserved block
- agents can also record short experience and user preferences there
- CheapClaw will not overwrite the entire file

### 3. Stop services before deleting runtime files

Run:

```bash
cheapclaw stop
```

before deleting runtime directories or editing files inside them.

### 4. Agent system visibility is runtime-based

CheapClaw only exposes the agent systems that actually exist inside the current bot runtime.

That means:

- built-in `infiagent` systems are not auto-injected
- a bot only sees the systems you explicitly prepared or installed

### 5. When something goes wrong, return to `localweb`

If a social channel is not responding, first verify:

- the dashboard opens
- the localweb bot can reply
- the LLM config is valid

Then continue debugging Telegram or Feishu.

## Repository Layout

```text
assets/                    built-in config, agent systems, screenshots
docs/                      additional documentation
scripts/                   CLI and dashboard startup logic
tools_library/             CheapClaw runtime tools
web/                       dashboard frontend files
cheapclaw_service.py       main service loop
README.md                  English guide
README.zh-CN.md            Chinese guide
```

## Additional Documentation

- [English README](README.md)
- [Chinese CLI tutorial](docs/CHEAPCLAW_CLI_TUTORIAL_ZH.md)
- [SDK guide](SDK_GUIDE.md)
