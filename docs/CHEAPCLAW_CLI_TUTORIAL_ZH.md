# CheapClaw 使用指南（中文）

这份文档面向第一次接触 CheapClaw 的用户，目标是两件事：

1. 让你从零开始把系统跑起来
2. 让你知道后续怎么加 Bot、改配置、接 Telegram 或飞书

如果你只看最短路径，直接看“5 分钟上手”。

## 1. CheapClaw 是什么

CheapClaw 是一个多 Bot 调度系统。它本身不是某一个聊天机器人，而是一套可以同时管理多个 Bot 的服务层。

它现在支持：

- `localweb`：本地网页聊天，适合先把系统跑起来
- `telegram`
- `feishu`
- `whatsapp`
- `discord`
- `qq`（通过 OneBot v11 网关）
- `wechat`（通过 OneBot v11 网关）

如果你只是想先验证系统能不能工作，推荐第一步先用 `localweb`。

## 2. 安装前准备

最低要求：

- Python `3.10+`
- 一套 LLM 配置
  - 最少只需要：
    - 一个 `base_url`
    - 一个 `api_key`
    - 一个 `model`

如果你只有一个 `api_key` 和一个 `base_url`，完全够用。  
CheapClaw 默认就是“一套 LLM 配置给所有 Bot 共用”，后面你再按需拆分。

## 3. 安装方式

### 3.1 发布到 PyPI 之后

发布后，用户可以直接安装：

```bash
python -m pip install -U cheapclaw
```

### 3.2 当前本地开发版

如果你现在是从项目目录直接使用，执行：

```bash
cd /Users/chenglin/Desktop/research/claw_dev/cheapclaw
python -m pip install -U -e .
```

## 4. 5 分钟上手

### 4.1 交互式初始化

```bash
cheapclaw config --interactive
```

默认目录如下，直接回车即可：

- `manifest`: `~/cheapclaw/fleet.manifest.json`
- `runtime_root`: `~/cheapclaw/runtime`
- `fleet_config_path`: `~/cheapclaw/runtime/fleet.generated.json`
- `llm_config_path`: `~/cheapclaw/runtime/config/llm_config.yaml`

### 4.2 如果你只有一个 `base_url` 和 `api_key`

在交互配置里，按下面填就行：

- `llm.base_url`: 你的模型服务地址
- `llm.api_key`: 你的密钥
- `llm.model`: 模型名

注意：

- 推荐直接填写完整模型标识，例如：
  - `openai/gpt-4o`
  - `openai/google/gemini-3-flash-preview`
  - 某些场景下也可能是 `openrouter/openai/gpt-4o`
- 如果你输入的是不带任何前缀的裸模型名，例如 `gpt-4o`，脚本会自动补成 `openai/gpt-4o`
- 如果你输入的模型名本来就带前缀，脚本会原样保留，不会重复拼接
- 这份 LLM 配置会被所有 Bot 共用

### 4.3 第一个 Bot 推荐怎么建

第一次建议先建一个 `localweb` Bot。

原因很简单：

- 不需要第三方平台凭据
- 不需要先处理 Telegram / 飞书平台配置
- 可以直接在网页上验证机器人是否可工作

### 4.4 启动服务

```bash
cheapclaw up
```

启动后打开：

```text
http://127.0.0.1:8787/dashboard
```

这是统一控制台，不依赖某个单独 Bot。

## 5. 默认目录和文件

默认运行目录：

- `~/cheapclaw/fleet.manifest.json`
- `~/cheapclaw/runtime/`

每个 Bot 的运行目录：

- `~/cheapclaw/runtime/<bot_id>/cheapclaw/`

常见文件：

- Bot 循环日志：
  - `~/cheapclaw/runtime/<bot_id>/cheapclaw/runtime/service.loop.log`
- Bot 服务日志：
  - `~/cheapclaw/runtime/<bot_id>/cheapclaw/runtime/cheapclaw_service.log`
- 统一控制台日志：
  - `~/cheapclaw/runtime/.fleet_web/fleet_web.log`

## 6. 常用命令

### 6.1 初始化 / 配置

```bash
cheapclaw init
cheapclaw config --interactive
cheapclaw prepare
```

说明：

- `init`：只创建默认 manifest 模板
- `config --interactive`：推荐，交互式填配置
- `prepare`：把 manifest 同步成每个 Bot 真正运行需要的文件

### 6.2 启动 / 停止 / 重启

```bash
cheapclaw up
cheapclaw start
cheapclaw stop
cheapclaw restart
```

说明：

- `up`：适合第一次启动，一般就用它
- `start`：启动所有启用的 Bot
- `stop`：默认会停止所有 Bot，同时停止统一控制台
- `restart`：重启全部

如果你希望只停 Bot，不停网页控制台：

```bash
cheapclaw stop --no-web-console
```

### 6.3 状态 / 列表 / 日志

```bash
cheapclaw list-bots
cheapclaw status
cheapclaw status --bot-id bot_1
cheapclaw logs --bot-id bot_1
cheapclaw logs --bot-id bot_1 --kind service
cheapclaw logs --bot-id bot_1 --lines 300
```

### 6.4 单独操作某个 Bot

```bash
cheapclaw add-bot
cheapclaw start-bot --bot-id bot_2
cheapclaw stop-bot --bot-id bot_2
cheapclaw reload-bot --bot-id bot_2 --prepare-first
```

说明：

- `add-bot` 默认会：
  - 写入 manifest
  - 自动 `prepare`
  - 自动启动新 Bot
- 配置改完后，推荐用 `reload-bot --prepare-first`

### 6.5 给 Bot 安装新的 agent system

```bash
cheapclaw bot-agent-system add /path/to/agent_system.zip --bot-id bot_2
cheapclaw bot-agent-system add /path/to/agent_system.zip --bot-id bot_2 --reload-after
cheapclaw bot-agent-system add /path/to/agent_system.zip --global
```

说明：

- `--bot-id`：只安装到某一个 Bot 的运行目录
- `--reload-after`：安装后立刻重载该 Bot
- `--global`：安装到项目共享 `assets/agent_library`，之后执行 `cheapclaw prepare` 或 `cheapclaw reload-bot --bot-id <id> --prepare-first` 生效
- 如果你不小心把命令打成 `cheapclaw bot-agent-sysyem add ...`，现在也兼容这个拼写

### 6.6 网页控制台

```bash
cheapclaw web
cheapclaw web-start
cheapclaw web-stop
cheapclaw web-status
```

说明：

- `web`：前台启动网页控制台，适合调试
- `web-start`：后台启动
- `web-stop`：停止网页控制台
- `web-status`：查看控制台状态

## 7. 统一网页控制台能做什么

地址：

```text
http://127.0.0.1:8787/dashboard
```

这个页面现在是整个系统的统一入口，可以做：

- 修改 manifest
- 修改 LLM 配置
- 设置代理
- 查看系统状态
- 查看 Bot 列表
- 新增 / 编辑 / 删除 Bot
- 启动 / 停止 / 重载某个 Bot
- 查看日志
- 使用 `localweb` 建本地会话、建群、`@bot_id` 对话

推荐使用方式：

1. 先用 CLI 跑起系统
2. 后续配置和管理尽量放到 Dashboard 上做

## 8. 如果只有一个 LLM 提供商，应该怎么配

最简单的做法是：

1. 运行：

```bash
cheapclaw config --interactive
```

2. 填：

- `llm.base_url`
- `llm.api_key`
- `llm.model`

3. 之后所有 Bot 都共用这一个 `llm_config.yaml`

这对绝大多数用户已经足够。

什么时候才需要多套 LLM 配置？

- 你希望 supervisor 和 worker 用不同模型
- 某个 Bot 要单独走另一家供应商
- 你想把便宜模型和强模型分开用

在这之前，不要过度设计，先共用一套。

## 9. 如何连接 Telegram

CheapClaw 的 Telegram 方式是轮询，不需要你自己先架 webhook。

### 9.1 准备 Telegram Bot Token

去 Telegram 的 `@BotFather`：

1. `/newbot`
2. 创建机器人
3. 拿到形如下面的 token：

```text
123456789:AA...
```

### 9.2 添加 Telegram Bot

方式一：交互式命令

```bash
cheapclaw add-bot
```

然后在 `channel` 里选 `telegram`，填：

- `bot_id`
- `display_name`
- `telegram.bot_token`
- `telegram.allowed_chats`

说明：

- `allowed_chats` 可以先留空
- 留空表示不额外做 chat 白名单过滤

方式二：网页添加

- 打开 `http://127.0.0.1:8787/dashboard`
- 新增 Bot
- `channel` 选 `telegram`
- 填 token 并保存

### 9.3 启动或重载

```bash
cheapclaw reload-bot --bot-id <your_telegram_bot_id> --prepare-first
```

或者如果还没启动：

```bash
cheapclaw start-bot --bot-id <your_telegram_bot_id>
```

### 9.4 常见问题

如果日志里出现：

```text
404 Client Error ... api.telegram.org/bot.../getUpdates
```

基本就是 `bot_token` 错了，重新从 BotFather 复制完整 token。

## 10. 如何连接飞书

CheapClaw 当前推荐飞书使用 `long_connection` 模式。

代理说明：

- 如果 `http_proxy / https_proxy / all_proxy` 留空，CheapClaw 不会为 bot 进程启用代理。
- 从当前版本开始，bot 进程不会再隐式继承你外层 shell 里的代理变量；是否走代理以 manifest 里的 `proxy_env` 为准。
- 如果你显式配置了 `all_proxy=socks5://...`，环境里需要有 `python-socks`。

优点：

- 不需要公网 webhook
- 本机启动后即可接消息

### 10.1 准备飞书应用

去飞书开放平台创建一个应用，拿到：

- `app_id`
- `app_secret`

### 10.2 添加飞书 Bot

```bash
cheapclaw add-bot
```

`channel` 选 `feishu`，填：

- `feishu.mode`: 推荐 `long_connection`
- `feishu.app_id`
- `feishu.app_secret`
- `feishu.verify_token`：可留空
- `feishu.encrypt_key`：可留空

### 10.3 启动

```bash
cheapclaw reload-bot --bot-id <your_feishu_bot_id> --prepare-first
```

### 10.4 飞书侧还要注意什么

除了 CheapClaw 本地配置外，你还需要在飞书平台侧确认：

- 机器人或应用已经启用
- 有接收消息相关权限
- 已经被加入你要测试的群或会话

如果这些没做好，本地服务是启动成功的，但不会收到消息。

## 11. 其他渠道说明

### WhatsApp

需要：

- `access_token`
- `phone_number_id`
- `verify_token`

### Discord

需要：

- `bot_token`
- 可选 `intents`

说明：

- 当前实现是原生 Discord Gateway 收消息 + REST 发消息
- 适合继续推进，但建议在你自己环境先做一轮真实联调再公开给用户

### QQ / WeChat

这两个当前都走 OneBot v11 网关桥接。

需要：

- `onebot_api_base`
- 可选 `onebot_access_token`
- 可选 `onebot_post_secret`
- 可选 `onebot_self_id`

说明：

- 这不是官方原生 SDK 路线
- 更准确地说，是“CheapClaw 对接 OneBot 网关”，不是直接自己实现 QQ / 微信协议

## 12. 运行中如何修改提示词

### 方法一：改文件

监控任务提示词补丁文件：

- `~/cheapclaw/runtime/<bot_id>/cheapclaw/supervisor_task/system-add.md`

说明：

- `system-add.md` 适合写长期有效的附加规则、经验和用户风格偏好
- CheapClaw 自己维护的运行时内容会写在 `<cheapclaw_system_结构>...</cheapclaw_system_结构>` 区块中
- 这个区块会被系统按区块更新，但区块外的内容会保留，适合你或智能体自行补充短备注
- 建议保持简短，只写稳定规则，不要写临时状态

改完之后执行：

```bash
cheapclaw reload-bot --bot-id <bot_id> --prepare-first
```

### 方法二：后续可通过网页做

如果你后面把网页编辑能力继续完善，也可以改成从 Dashboard 直接编辑。  
目前最稳妥的方式仍然是改文件后 `reload-bot`。

## 13. 常见排错

### 13.1 `manifest not found`

说明默认配置文件不存在。

执行：

```bash
cheapclaw config --interactive
```

### 13.2 `manifest still contains placeholder values`

说明 manifest 里还有占位符还没填完。

你需要补齐：

- `runtime_root`
- `fleet_config_path`
- `llm_config_path`
- 对应渠道的凭据

### 13.3 网页打开了，但机器人没响应

先看：

```bash
cheapclaw status
cheapclaw logs --bot-id <bot_id>
```

重点确认：

- bot 进程是否 alive
- channel 是否配置正确
- 本地会话里是否真的 `@` 了目标 bot

### 13.4 修改了配置，但运行结果没变

一般是忘了 `prepare` 或 `reload-bot`。

推荐直接执行：

```bash
cheapclaw reload-bot --bot-id <bot_id> --prepare-first
```

## 14. 现在这个项目适不适合发 PyPI

从“安装链路”角度，当前已经可以继续往 PyPI 推进：

- wheel 构建已通过
- 从 wheel 安装后，CLI 入口、核心模块、资源文件都能正常落地

但从“稳定性预期”角度，我建议按下面理解：

- `localweb / telegram / feishu`：可以作为首发主路径
- `discord`：可放进首发，但建议标成 beta
- `qq / wechat / whatsapp`：建议明确标注为实验性或桥接模式

原因不是它们不能用，而是：

- `qq / wechat` 依赖外部 OneBot 网关
- `discord / qq / wechat` 这几条链路还需要更多真实环境联调
- 控制台与调度主链路当前已经统一围绕 pending monitor instructions、会话历史与 task 状态工作

## 15. 给最终用户的一句话建议

如果你是第一次用：

1. 先 `cheapclaw config --interactive`
2. 先建一个 `localweb` Bot
3. 运行 `cheapclaw up`
4. 打开 `http://127.0.0.1:8787/dashboard`
5. 确认本地网页聊天正常
6. 再去接 Telegram 或飞书

这是最稳、最省时间的路径。
