# InfiAgent SDK Guide

适用版本：`infiagent>=3.0.2`

这份文档只讲当前 SDK 的实际用法，不讲过时接口。

核心结论先说清楚：

1. `infiagent(...)` 用来定义运行时配置。
2. `run(..., task_id=...)` 和其他 task 方法用来操作具体任务。
3. 你应该把自己的 agent 开发放在一份独立的工作目录里，而不是直接改安装目录或源码仓库。
4. 标准配置文件应该先从默认用户目录复制出来，再在你自己的目录里迭代。

适用代码位置：
- SDK: [infiagent/sdk.py](../infiagent/sdk.py)
- 用户目录与默认资源: [utils/user_paths.py](/utils/user_paths.py)
- 配置加载器: [utils/config_loader.py](../utils/config_loader.py)
- 后台任务启动: [utils/task_runtime.py](../utils/task_runtime.py)

## 1. 安装

推荐直接从 PyPI 安装：

```bash
python -m pip install -U infiagent==3.0.2
```

安装完成后，可以检查版本：

```bash
python - <<'PY'
import importlib.metadata
print(importlib.metadata.version("infiagent"))
PY
```

如果你有多个 Python 环境，请始终使用实际运行 agent 的那个解释器，例如：

```bash
/opt/anaconda3/bin/python -m pip install -U infiagent==3.0.2
```

## 2. InfiAgent 默认会把标准资源放到哪里

安装包本身包含默认配置和内置 skills，但运行时不会直接让你去改 `site-packages`。当前设计是：

- 配置、agent systems、tools 会种到用户数据目录 (必须启动一次才会到用户目录，因此可以使用下方代码）
```python
from infiagent import infiagent

agent = infiagent()
print(agent.describe_runtime())
```

- skills 会种到用户 skills 目录


默认路径如下：

- 用户数据根目录：`~/mla_v3`
- LLM 配置：`~/mla_v3/config/llm_config.yaml`
- App 配置：`~/mla_v3/config/app_config.json`
- Agent systems：`~/mla_v3/agent_library/`
- 动态工具目录：`~/mla_v3/tools_library/`
- 会话/状态：`~/mla_v3/conversations/`
- 日志：`~/mla_v3/logs/`
- 运行时状态：`~/mla_v3/runtime/`
- Skills 主库：`~/.agent/skills/`

注意：
- `skills` 默认不跟随 `~/mla_v3`，而是放在 `~/.agent/skills`
- 这是为了让 skills 更像全局可复用能力库，并且统一主流智能体格式

## 3. 如何确认当前环境实际在用哪些路径

最稳的方法不是猜，而是直接用 SDK 打印运行时：

```python
from infiagent import infiagent

agent = infiagent()
print(agent.describe_runtime())
```

你会看到类似这些字段：
- `user_data_root`
- `config_dir`
- `llm_config_path`
- `agent_library_dir`
- `tools_dir`
- `skills_dir`
- `conversations_dir`
- `logs_dir`
- `runtime_dir`
- `seed_builtin_resources`

如果你是第一次安装，第一次实例化或运行时会自动补齐默认目录和样例配置。

## 4. 标准配置文件怎么找

如果你只是想基于官方标准配置开始开发，不要去改 `site-packages` 里的文件。应该以用户目录里已经种好的标准文件为基准。

你要找的就是这些：

```text
~/mla_v3/config/llm_config.yaml
~/mla_v3/config/app_config.json
~/mla_v3/agent_library/
~/mla_v3/tools_library/
~/.agent/skills/
```

如果这些文件还没出现，可以先运行一次：

```python
from infiagent import infiagent
infiagent()
```

或者跑一次最小任务，让默认资源自动就位。

## 5. 推荐的开发方式：复制一份到你自己的工作目录

不要直接修改：
- `~/mla_v3`
- `~/.agent/skills`
- `site-packages`

推荐做法是新建你自己的项目目录，例如：

```bash
mkdir -p /path/to/my_agent_project/runtime
mkdir -p /path/to/my_agent_project/runtime/config
mkdir -p /path/to/my_agent_project/runtime/agent_library
mkdir -p /path/to/my_agent_project/runtime/tools_library
```

然后把标准配置复制进去：

```bash
cp ~/mla_v3/config/llm_config.yaml /path/to/my_agent_project/runtime/config/
cp ~/mla_v3/config/app_config.json /path/to/my_agent_project/runtime/config/
cp -R ~/mla_v3/agent_library/OpenCowork /path/to/my_agent_project/runtime/agent_library/
cp -R ~/mla_v3/agent_library/Researcher /path/to/my_agent_project/runtime/agent_library/
cp -R ~/mla_v3/tools_library/. /path/to/my_agent_project/runtime/tools_library/
```

如果你只想基于一套 system 开发，也可以只复制那一套。

例如只基于 `OpenCowork`：

```bash
cp -R ~/mla_v3/agent_library/OpenCowork /path/to/my_agent_project/runtime/agent_library/MyAgentSystem
```

然后你再去改：
- `general_prompts.yaml`
- `level_0_tools.yaml`
- `level_3_agents.yaml`
- 其他 system 配置文件

## 6. 用自己的目录启动 SDK

你的项目代码应该显式指向自己的 runtime 目录，而不是依赖默认用户目录。

```python
from infiagent import infiagent

agent = infiagent(
    user_data_root="/path/to/my_agent_project/runtime",
    default_agent_system="MyAgentSystem",
    default_agent_name="alpha_agent",
)
```

如果你的 `runtime` 目录已经包含：
- `config/llm_config.yaml`
- `config/app_config.json`
- `agent_library/...`
- `tools_library/...`

通常就不需要再额外传：
- `llm_config_path`
- `agent_library_dir`
- `tools_dir`

## 7. `user_data_root` 到底控制了什么

一旦你指定：

```python
user_data_root="/path/to/my_agent_project/runtime"
```

下面这些目录都会一起切换：

- `/path/to/my_agent_project/runtime/config`
- `/path/to/my_agent_project/runtime/agent_library`
- `/path/to/my_agent_project/runtime/tools_library`
- `/path/to/my_agent_project/runtime/conversations`
- `/path/to/my_agent_project/runtime/logs`
- `/path/to/my_agent_project/runtime/runtime`

其中：
- `share_context.json`
- `stack.json`
- `actions.json`

都在 `conversations/` 下。

例外：
- `skills` 默认仍然是全局目录 `~/.agent/skills`
- 只有显式传 `skills_dir` 才会覆盖

## 8. 一个最小可运行示例

```python
from infiagent import infiagent

agent = infiagent(
    user_data_root="/path/to/my_agent_project/runtime",
    default_agent_system="MyAgentSystem",
    default_agent_name="alpha_agent",
    action_window_steps=20,
    thinking_interval=20,
)

result = agent.run(
    "请分析这个目录并给出重构建议",
    task_id="/path/to/my_agent_project/tasks/refactor_task",
)

print(result)
```

注意：
- `task_id` 现在是必填
- `task_id` 本质上就是这个任务的工作目录绝对路径
- 同一个 `task_id` 对应同一份任务记忆、share_context、stack 和运行状态

## 9. 如何开发自己的 Agent System

最简单的方式是复制现有系统，然后改名、改 prompt、改 tools。

例如：

```bash
cp -R ~/mla_v3/agent_library/OpenCowork /path/to/my_agent_project/runtime/agent_library/MyAgentSystem
```

你至少会改这几个文件：

- `general_prompts.yaml`
- `level_0_tools.yaml`
- `level_3_agents.yaml`

常见做法：

1. 改 `general_prompts.yaml`
- 写你自己的系统角色、规则、任务边界

2. 改 `level_0_tools.yaml`
- 调整根 agent 能看到的工具

3. 改 `level_3_agents.yaml`
- 定义你的执行 agent
- 配置不同 agent 用不同模型

## 10. 如何开发自己的工具

SDK 支持动态工具目录。推荐把你自己的工具写在：

```text
/path/to/my_agent_project/runtime/tools_library/
```

或者你的项目自定义目录里，然后在初始化时传给 SDK。

例如：

```python
agent = infiagent(
    user_data_root="/path/to/my_agent_project/runtime",
    tools_dir="/path/to/my_agent_project/runtime/tools_library",
)
```

如果你的工具目录已经放在 `user_data_root/tools_library`，一般不需要再重复传。

## 11. 如何使用默认 Skills

默认 skills 主库是：

```text
~/.agent/skills/
```

内置技能和你安装的 skills 都应该进这个目录。

运行时逻辑是：
- agent 先发现 `available_skills`
- 真正使用时通过 `load_skill` 把 skill 部署到当前 task 的 `.skills/`

也就是说：
- “能看到” skill
- 和 “当前任务已经加载使用” skill

是两件不同的事。

如果你想显式覆盖 skills 根目录，也可以：

```python
agent = infiagent(
    user_data_root="/path/to/my_agent_project/runtime",
    skills_dir="/path/to/my_agent_project/skills",
)
```

## 12. 多模型、多供应商是怎么配置的

当前框架支持：
- 思考模型
- 执行模型
- 压缩模型
- 读图模型
- 不同 provider 混用

这些主要在 `llm_config.yaml` 里控制。

你可以在同一个配置里混用：
- OpenRouter
- OpenAI-compatible provider
- 本地模型网关
- 其他兼容接口

不同 agent 还可以在 agent system 配置里指定不同模型偏好字段：
- `execution_model`
- `thinking_model`
- `compressor_model`
- `image_generation_model`
- `read_figure_model`

注意：
- `tool_choice` 不在 agent YAML 里配置，而是在 `llm_config.yaml` 里配置
- 可以按用途配置：
  - `execution`
  - `thinking`
  - `compressor`
  - `image_generation`
  - `read_figure`
- 也可以在某个模型对象里单独覆盖 `tool_choice`
- 如果 agent YAML 没写某类模型，则回退到 `llm_config.yaml` 该用途列表中的默认模型；没有显式默认时，使用该列表第一个模型

另外，每个 `task_id` 根目录下都可以放一个 `system-add.md`：
- 路径：`<task_id>/system-add.md`
- 作用：每次构建系统提示词时，都会把这个文件内容注入到系统提示词中
- 适合做任务级的长期附加规则，而不是临时用户消息
- 如果文件里存在 `<cheapclaw_system_结构>...</cheapclaw_system_结构>`，这一段是 CheapClaw 保留区；系统只会更新这一段，不会改动区块外内容
- 因此你或智能体可以把短小稳定的经验、风格偏好、输出约束写在区块外，避免被覆盖

建议：
- 小模型请用更短步长
- 但 `action_window_steps` 不要低于 `10`

## 13. 运行时任务管理接口

### 13.1 `fresh`

```python
agent.fresh(
    task_id="/path/to/my_agent_project/tasks/task_a",
    reason="reload runtime config",
)
```

行为：
- 任务正在运行：发送定向 fresh 请求
- 任务未运行：重载配置后 resume

### 13.2 `add_message`

```python
agent.add_message(
    "补充需求：保留已有结果，只做增量修改。",
    task_id="/path/to/my_agent_project/tasks/task_a",
    source="user",
    resume_if_needed=True,
)
```

行为：
- 给同一个 task 追加消息
- 运行中的 agent 会在下一轮上下文构建时看到
- 不会被当成一个全新任务

### 13.3 `start_background_task`

```python
agent.start_background_task(
    task_id="/path/to/my_agent_project/tasks/task_b",
    user_input="后台整理日志并生成总结",
    agent_system="MyAgentSystem",
    agent_name="alpha_agent",
    force_new=True,
)
```

行为：
- 启动独立后台 Python 进程
- 日志写到 `<user_data_root>/runtime/launched_tasks`

### 13.4 `task_snapshot`

```python
snapshot = agent.task_snapshot(task_id="/path/to/my_agent_project/tasks/task_a")
print(snapshot)
```

适合外部应用或 dashboard 用来查看：
- 是否还在运行
- 最新 thinking
- 最新 final_output
- share_context / stack 路径
- 最新 instruction

### 13.5 `reset_task`

```python
agent.reset_task(
    task_id="/path/to/my_agent_project/tasks/task_a",
    reason="clear broken loop",
    preserve_history=True,
    kill_background_processes=True,
)
```

## 14. Hooks：如何在 SDK 外层做集成

### 14.1 Tool Hooks

你可以在任意工具调用前后挂钩：

```python
agent = infiagent(
    user_data_root="/path/to/my_agent_project/runtime",
    tool_hooks=[
        {
            "name": "observe-final-output",
            "callback": "/abs/path/to/my_hooks.py:on_tool_event",
            "when": "after",
            "tool_names": ["final_output"],
            "include_arguments": False,
            "include_result": True,
        }
    ],
)
```

适合做：
- 外部事件回流
- 审计
- 面板更新
- 与第三方应用集成

### 14.2 Context Hooks

你也可以在上下文送进 LLM 前挂钩：

```python
agent = infiagent(
    user_data_root="/path/to/my_agent_project/runtime",
    context_hooks=[
        {
            "name": "rewrite-context",
            "callback": "/abs/path/to/my_hooks.py:on_context",
        }
    ],
)
```

适合做：
- 注入外部上下文
- 精简上下文
- 做额外安全规则

## 15. 一套推荐的项目结构

推荐你自己的项目长成这样：

```text
my_agent_project/
├── runtime/
│   ├── config/
│   │   ├── llm_config.yaml
│   │   └── app_config.json
│   ├── agent_library/
│   │   └── MyAgentSystem/
│   ├── tools_library/
│   ├── conversations/
│   ├── logs/
│   └── runtime/
├── hooks/
│   └── my_hooks.py
├── tasks/
│   ├── task_a/
│   └── task_b/
└── main.py
```

这样做的好处是：
- 配置和任务状态都在你的项目内
- 不污染全局 `~/mla_v3`
- 更容易迁移、打包和部署

## 16. 一个完整的起步流程

### 第一步：安装 SDK

```bash
python -m pip install -U infiagent==3.0.2
```

### 第二步：让默认资源先种出来

```python
from infiagent import infiagent
infiagent()
```

### 第三步：复制标准配置到你自己的 runtime

```bash
mkdir -p /path/to/my_agent_project/runtime/config
mkdir -p /path/to/my_agent_project/runtime/agent_library
mkdir -p /path/to/my_agent_project/runtime/tools_library

cp ~/mla_v3/config/llm_config.yaml /path/to/my_agent_project/runtime/config/
cp ~/mla_v3/config/app_config.json /path/to/my_agent_project/runtime/config/
cp -R ~/mla_v3/agent_library/OpenCowork /path/to/my_agent_project/runtime/agent_library/MyAgentSystem
```

### 第四步：修改你的 system prompt 和 tools

至少改：
- `general_prompts.yaml`
- `level_0_tools.yaml`
- `level_3_agents.yaml`

### 第五步：用 SDK 跑起来

```python
from infiagent import infiagent

agent = infiagent(
    user_data_root="/path/to/my_agent_project/runtime",
    default_agent_system="MyAgentSystem",
    default_agent_name="alpha_agent",
    action_window_steps=20,
    thinking_interval=20,
)

result = agent.run(
    "先阅读项目，再生成一份改造计划",
    task_id="/path/to/my_agent_project/tasks/plan_task",
)

print(result)
```

## 17. 最后两条建议

1. 不要直接改安装目录
- 包括 `site-packages/infiagent/...`
- 升级后这些改动都会丢

2. 不要把所有实验都堆在默认 `~/mla_v3`
- 先复制一份标准配置出来
- 再在你自己的 `runtime/` 下开发
- 这是当前 SDK 最稳的使用方式

执行层开发粒度  skills->自写工具->钩子修改上下文构造方式/工具执行审查
应用层粒度  直接单个run-->串行调度层处理-->提供的工具进行需求控制，消息传递，在活动 agent 之间传递消息
