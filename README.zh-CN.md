<p align="center">
  <img src="assets/logo.png" alt="CheapClaw logo" width="220">
</p>

<p align="center">
  <a href="README.md">English</a> | 简体中文
</p>

<p align="center">
  <a href="https://pypi.org/project/cheapclaw/"><img src="https://img.shields.io/pypi/v/cheapclaw" alt="PyPI"></a>
  <a href="https://pypi.org/project/cheapclaw/"><img src="https://img.shields.io/pypi/pyversions/cheapclaw" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License"></a>
</p>

# CheapClaw

CheapClaw 是一个基于 `infiagent` SDK 构建的多 Bot 调度应用。

## 项目简介

CheapClaw 的初衷很简单：笔者看着自己 OpenClaw 的 token 账单感到心痛。

同时，大多数人其实只是想体验一下 OpenClaw，而不是真正有高强度、长期使用它的需求。因此，CheapClaw 的目标**不是**最小化代码量，也不是在不同场景下复刻 OpenClaw。相反，CheapClaw 更希望在保留 OpenClaw 大部分功能的基础上，进一步扩展其中一部分能力，并提升相同模型处理复杂任务时的效果，从而实现更低成本的执行。

它最核心的不同，在于**在单个 agent loop 中混合执行强模型与弱模型**。在 OpenClaw 中，你已经可以依赖不同的 bot 来协作：由强模型 bot 负责规划，再由弱模型 execution bot 负责执行。但 execution bot 有一个很现实的问题：如果执行过程中的实际进展没有按照原计划推进，弱模型往往就会崩溃。

基于 InfiAgent SDK 的特性，CheapClaw 允许在同一个 agent loop 中进行阶段式思考，并且按照固定间隔直接截断前面的全部对话历史，例如每 10 步或 20 步进行一次。这样一来，不仅可以让小模型始终在更短的上下文窗口中执行，也能持续获得来自强模型的实时修正计划。

所以，虽然对于“你好”这样简单的输入，CheapClaw 和 OpenClaw 一样，仍然需要经过一套相对较重的流程，但一旦任务变得更复杂，比如：

- “将一个列表中的 500 个 PDF 总结成一张表格”
- “完成一份至少 100 页的 EV 调研报告”

那么这种细粒度的强弱模型混合执行方式，就能为你节省相当可观的成本。


## 核心能力
- 接入飞书，telegram，web(前三已测试)，whatsapp，discord。
- 多 bot 支持，bot自定义支持，也可以在 bot的运行目录下写入 system-add.md 为 bot 提供额外角色和提示词
- skills支持，多阶段披露 skills 支持

## 额外能力

除此之外，CheapClaw还支持一些新的特性，允许用户通过改造提升垂类任务的稳定性：

- 除了skills外，可以给 bot添加完整的智能体系统给其调用，支持自定义的单或者多智能体系统，支持自定义的工具，通过自己或他人的垂类智能体更可靠的完成复杂任务（例如科学研究，论文撰写）。具体使用方式可以查看文档内的教程，也可以参考 [infiAgent](https://github.com/polyuiislab/infiAgent) 仓库的智能体系统组织方式。注意不要直接使用仓库外来源不明的智能体系统，接入前请先确认其中没有加入 `human_in_loop` 这类会在后台完全阻塞执行的工具。
- 完全拆分的分层记忆，bot的调度智能体只专注于维护和用户的交互记忆，其调度的子智能体系统只维护同一个任务下对类似任务的记忆，而不会得知其他类型任务的噪声信息。
- 用户可以基于 infiagent 项目的说明，基于配置文件编辑出自己的智能体系统，并添加到对应 bot 下。
- 最后一层的智能体系统内，不同子智能体都可以单独配置思考模型，执行模型，读图模型，压缩模型等。

![CheapClaw framework](assets/framework.png)




## 安装

### 从 PyPI 安装

```bash
python -m pip install -U cheapclaw
```

### 从源码安装

```bash
cd /Users/chenglin/Desktop/research/claw_dev/cheapclaw
python -m pip install -U -e .
```

## 快速开始

### 方案 A：正常首次配置

```bash
cheapclaw config --interactive
cheapclaw up
```

启动后打开：

```text
http://127.0.0.1:8787/dashboard
```

### 方案 B：先生成模板

```bash
cheapclaw init
cheapclaw config --interactive
cheapclaw up
```

`cheapclaw init` 会在默认位置生成 `fleet.manifest.json` 模板。

## 默认目录

CheapClaw 默认把运行文件放在：

```text
~/cheapclaw
```

最常用的几个路径是：

```text
~/cheapclaw/fleet.manifest.json
~/cheapclaw/runtime
~/cheapclaw/runtime/config/llm_config.yaml
```

大多数 CLI 命令都会自动使用这个默认 manifest，所以一般不需要手动传 `--manifest`。

## 如果你只有一个 API Key 和一个 Base URL

这已经足够。

在 `cheapclaw config --interactive` 中填写：

- `llm.base_url`
- `llm.api_key`
- `llm.model`

CheapClaw 会生成一份共享的 `llm_config.yaml`，默认所有 Bot 都共用它。

### 关于 `llm.model` 应该怎么填

你的模型的 api格式/服务商提供的模型名称：
例如 openrouter 中的 openai/gpt-4o，由于该提供商提供 openai 和 openrouter 两种格式的调用模式，因此模型名称可以写成：
openai/openai/gpt-4o
或
openrouter/openai/gpt-4o
也就是说，通常要把“API 格式前缀”写在模型名前面。

默认配置文件均采用gemini-3-flash，可以在 thinking 模型不变的情况下，将其他模型换成更小的模型以节省费用。也可以混合使用本地和云端模型，具体见 infiagent 仓库。



## 推荐的第一次使用路径

如果你第一次部署 CheapClaw，最稳的路径是：

1. 先建一个 `localweb` Bot
2. 启动 CheapClaw
3. 打开 Dashboard，确认网页聊天能正常回复
4. 再去接 Telegram 或飞书

这样可以避免第一次就同时排查：

- LLM 配置
- 渠道凭据
- 网络代理
- 平台权限

## Dashboard

内置控制台地址：

```text
http://127.0.0.1:8787/dashboard
```

在这个页面里你可以：

- 查看所有 Bot
- 看运行状态
- 编辑 Fleet 配置
- 使用本地网页聊天
- 创建多 Bot 群聊
- 查看历史记录
- 管理运行时而不必手动改文件

### 在服务器上开放 Dashboard

如果要给局域网或远程机器访问：

```bash
cheapclaw up --web-host 0.0.0.0 --web-port 8787
```

然后通过：

```text
http://<服务器IP>:8787/dashboard
```

访问。

### Dashboard 示例

![CheapClaw dashboard](assets/web_shot.png)

这个截图展示的是：通过 CLI 安装了额外的自定义 Agent System 之后，Bot 的调度能力从内置系统扩展到了更多系统。

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1 --reload-after
```

## 添加 Bot

你可以通过两种方式添加 Bot：

- Dashboard
- CLI

### 用 CLI 添加 Bot

```bash
cheapclaw add-bot
```

这个命令是交互式的，会依次问你：

- bot id
- display name
- channel 类型
- 凭据
- 是否立即启动

### 单独管理一个 Bot

```bash
cheapclaw start-bot --bot-id bot_1
cheapclaw stop-bot --bot-id bot_1
cheapclaw reload-bot --bot-id bot_1 --prepare-first
```

### 查看 Bot 状态

```bash
cheapclaw list-bots
cheapclaw status
cheapclaw status --bot-id bot_1
cheapclaw logs --bot-id bot_1
```

## 本地网页聊天（localweb）

`localweb` 是最适合第一次测试的渠道，因为它不需要任何第三方凭据。

适用场景：

- 首次验证
- 本地调试
- 演示
- 多 Bot 群聊实验

在 localweb 群聊里：

- 可以把多个 localweb Bot 放进一个会话
- `@bot_id` 会把消息路由给对应 Bot
- 如果一条消息里提到了一个或多个 Bot，那么只有被提到的 Bot 会被触发

## 连接 Telegram

准备：

- 一个 `@BotFather` 创建的 Bot Token
- 可选的 `allowed_chats`

在 `cheapclaw config --interactive` 或 `cheapclaw add-bot` 中：

- 选择 `telegram`
- 填 `telegram.bot_token`
- 如有需要，填 `telegram.allowed_chats`

启动：

```bash
cheapclaw start-bot --bot-id telegram_bot_1 --prepare-first
```

如果 Telegram 轮询报 `404 getUpdates`，通常就是 token 错了或格式不对。

## 连接飞书

准备：

- `app_id`
- `app_secret`

配置时：

- 选择 `feishu`
- 填 `feishu.app_id`
- 填 `feishu.app_secret`

CheapClaw 默认使用飞书长连接模式。

如果你的环境有代理，记得确认 CheapClaw 实际读取到的是你希望它使用的代理配置。

## 其他渠道

当前还支持：

- Discord
- WhatsApp Cloud API
- QQ（通过 OneBot v11 bridge）
- WeChat（通过 OneBot v11 bridge）

但对大多数用户来说，依然建议先跑通：

1. `localweb`
2. Telegram 或飞书

再接桥接型渠道。

## 安装自定义 Agent System

CheapClaw 支持把完整 Agent System 作为 zip 安装进去。

安装到单个 Bot：

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1
```

安装后立刻重载：

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1 --reload-after
```

安装到全局共享 assets：

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --global
```

说明：

- `--bot-id` 只安装到该 Bot 的 runtime
- `--reload-after` 会立刻重启这个 Bot，使新系统马上可见
- 运行时安装的系统在正常 `prepare / reload` 流程中会被保留，不会被自动删掉

## 常用命令

### 主流程

```bash
cheapclaw init
cheapclaw config --interactive
cheapclaw prepare
cheapclaw up
cheapclaw stop
cheapclaw restart
```

### 单 Bot 控制

```bash
cheapclaw add-bot
cheapclaw start-bot --bot-id bot_1
cheapclaw stop-bot --bot-id bot_1
cheapclaw reload-bot --bot-id bot_1 --prepare-first
```

### 状态和日志

```bash
cheapclaw list-bots
cheapclaw status
cheapclaw status --bot-id bot_1
cheapclaw logs --bot-id bot_1
```

### Dashboard 控制

```bash
cheapclaw web-start
cheapclaw web-stop
cheapclaw web-status
```

### Agent System 管理

```bash
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --bot-id bot_1 --reload-after
cheapclaw bot-agent-system add "/path/to/agent_system.zip" --global
```

## 使用案例

### 手机端接入：飞书 + Telegram

下面两张图展示的是 CheapClaw 接入手机聊天渠道后的效果。

| 飞书 | Telegram |
|---|---|
| ![CheapClaw on Feishu](assets/screenshot.jpg) | ![CheapClaw on Telegram](assets/screenshot_2_telegram.jpg) |

这是一个非常实用的部署方式：

- 调度逻辑运行在你的服务器上
- 用户仍然在熟悉的聊天软件里和系统交互
- Supervisor 负责决定该复用旧任务还是新开分支

### 网页控制台 + 扩展 Agent System

这个场景展示的是：一个最初只有内置系统的 Bot，后来通过 CLI 安装了额外系统，于是调度能力随之扩展。

## 注意事项

### 1. Watchdog 默认是低频的

当前默认值：

```text
watchdog_interval_sec = 86400
```

也就是 24 小时。

这样可以减少维护噪音。如果你确实需要更短的周期，可以在初始化后改：

```text
~/cheapclaw/runtime/<bot_id>/cheapclaw/config/app_config.json
```

### 2. `system-add.md` 现在是可共存的

CheapClaw 只会更新 `system-add.md` 里自己的保留区块。

所以现在：

- 你可以在区块外写自己的提示
- 智能体也可以把简短经验和用户偏好记录进去
- CheapClaw 不会整文件覆盖

### 3. 删除 runtime 之前先停服务

先执行：

```bash
cheapclaw stop
```

再去删 runtime 或改里面的文件。

### 4. Agent System 可见性是基于 runtime 的

CheapClaw 只会暴露当前 Bot runtime 里实际存在的 Agent System。

也就是说：

- `infiagent` 自带系统不会自动注入
- Bot 只会看到你明确准备或安装进去的系统

### 5. 出问题时先回到 `localweb`

如果某个社交渠道不响应，先验证：

- Dashboard 能打开
- localweb Bot 能回复
- LLM 配置是有效的

再继续查 Telegram 或飞书配置。

## 仓库结构

```text
assets/                    内置配置、Agent System、截图
docs/                      补充文档
scripts/                   CLI 和 Dashboard 启动逻辑
tools_library/             CheapClaw 运行时工具
web/                       Dashboard 前端文件
cheapclaw_service.py       主服务循环
README.md                  英文说明
README.zh-CN.md            中文说明
```

## 补充文档

- [English README](README.md)
- [中文 CLI 教程](/Users/chenglin/Desktop/research/claw_dev/cheapclaw/docs/CHEAPCLAW_CLI_TUTORIAL_ZH.md)
- [SDK 指南](/Users/chenglin/Desktop/research/claw_dev/cheapclaw/SDK_GUIDE.md)
