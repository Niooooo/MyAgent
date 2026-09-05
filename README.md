<h1 align="center">MyAgent</h1>

<p align="center">运行在本地工作区的 Python Agent</p>
<p align="center">桌面工作台 · 命令行 · Python API</p>

<p align="center">
  <a href="#环境要求"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&amp;logo=python&amp;logoColor=white&amp;labelColor=30363D" alt="Python 3.11+"></a>
  <a href="#2-启动桌面工作台"><img src="https://img.shields.io/badge/Electron-42-47848F?style=flat&amp;logo=electron&amp;logoColor=white&amp;labelColor=30363D" alt="Electron 42"></a>
  <a href="#环境要求"><img src="https://img.shields.io/badge/Node.js-22.12%2B-339933?style=flat&amp;logo=nodedotjs&amp;logoColor=white&amp;labelColor=30363D" alt="Node.js 22.12+"></a>
  <a href="#它是怎么运行的"><img src="https://img.shields.io/badge/OpenAI-Responses%20API-412991?style=flat&amp;labelColor=30363D" alt="OpenAI Responses API"></a>
  <a href="docs/rag-mcp.md"><img src="https://img.shields.io/badge/MCP-Streamable%20HTTP-0EA5E9?style=flat&amp;labelColor=30363D" alt="MCP Streamable HTTP"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#myagent-能做什么">功能概览</a> ·
  <a href="#配置">配置说明</a> ·
  <a href="docs/rag-mcp.md">RAG / MCP 接入</a> ·
  <a href="#开发与测试">开发与测试</a>
</p>

一个运行在本地工作区的 Python Agent。它使用 OpenAI Responses API，能读写文件、执行命令、维护任务和记忆，也可以把工作交给子 Agent 或长期运行的 Agent Team 成员。

MyAgent 提供两种直接入口：Windows 桌面工作台适合日常使用，命令行适合脚本和快速任务。项目也暴露 Python API，方便把运行时嵌入其他应用。

> MyAgent 会在你的电脑上执行工具。文件写入、危险命令和多数持久化变更会先请求批准，但这不是完整的操作系统沙箱。请先阅读[安全边界](#安全边界)。

## 快速开始

### 环境要求

- Python 3.11 或更高版本
- Git，以及可从命令行调用的 Bash
- 一个实现 Responses API 的模型服务
- Node.js 22.12 或更高版本，仅桌面工作台需要

Windows 会优先使用 Git 自带的 Bash。需要指定其他 Bash 时，可设置 `MYAGENT_BASH` 为可执行文件的完整路径。

### 1. 安装 Python 项目

```powershell
git clone https://github.com/Niooooo/MyAgent.git
Set-Location MyAgent

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

macOS 或 Linux 使用下面的激活命令：

```bash
source .venv/bin/activate
```

### 2. 启动桌面工作台

```powershell
Set-Location desktop
npm install
Set-Location ..

.\start_gui.cmd
```

打开桌面端后：

1. 在“模型”页面添加模型 ID、API Key 和可选的 Base URL。
2. 选择一个工作目录。
3. 新建对话并发送任务。

桌面端支持流式回复、多个对话标签、按工作区保存的历史记录，以及主 Agent 和子 Agent 分别选模。模型凭据不会进入 Electron renderer 状态，但会以明文保存在当前 Windows 用户的配置目录中。

常用启动检查：

```powershell
.\start_gui.cmd --check
.\start_gui.cmd --tk
.\start_gui.cmd --tk-check
```

`--tk` 会启动保留的 Tk 界面，默认入口仍是 Electron。

桌面启动器会在 `%LOCALAPPDATA%\MyAgent\runtime` 维护独立的 Python
运行环境。首次启动以及 `pyproject.toml` 依赖发生变化后，会自动同步依赖并先做
sidecar 导入检查；模型和会话数据不放在该运行环境中。

#### 桌面运行环境与数据持久化

`start_gui.cmd` 使用独立的托管 Python 环境启动桌面端。首次启动或
`pyproject.toml` 依赖发生变化时，启动器会同步依赖并验证 sidecar 导入；依赖未变化时
直接复用现有环境。

桌面数据与源码、运行环境分离。模型配置和设置保存在统一数据目录；会话按 JSONL
持久化，`catalog.jsonl` 作为侧栏索引。应用启动时会验证会话文件，并可从现存的数字
命名会话 JSONL 生成缺失的目录索引。索引生成只读取会话文件；遇到无效 JSONL 时会
报告具体错误，不改写原文件。兼容格式的 `conversations.json` 可转换为逐会话 JSONL，
转换后保留原文件。

运行环境检查：

```powershell
.\start_gui.cmd --check
```

该命令会同步所需依赖，并分别验证 Python sidecar 和 Electron 资源。sidecar 启动失败时，
桌面错误提示会包含 Python 报错的最后一行。

### 3. 使用命令行

先复制配置示例：

```powershell
Copy-Item myagent.config.example.json myagent.config.json
$env:OPENAI_API_KEY = "your-api-key"
```

进入交互模式：

```powershell
myagent
myagent -c
myagent -r 3
```

执行一次任务后退出：

```powershell
myagent "列出当前目录中的 Python 文件，并说明每个模块的职责"
```

CLI 默认创建并保存一个会话；`-c` 继续最近激活的会话，`-r SESSION_ID` 恢复指定会话。恢复时会继续使用会话保存的工作区。只需要一次性临时运行、完全不读写会话文件时使用 `--no-session`。

新会话把启动命令所在的目录当作工作区。文件工具不能访问这个目录之外的路径。

## MyAgent 能做什么

| 能力 | 当前实现 |
| --- | --- |
| 本地工具 | Bash、后台 Bash、文件读写、精确编辑、Glob、Grep |
| 任务管理 | TODO List、带依赖的持久化 Task、认领与完成状态 |
| 上下文与记忆 | 大型工具结果卸载、历史压缩、长期记忆、渐进式 Skill 加载 |
| 任务分派 | 同步 SubAgent、后台 Fork、可持续通信的 Agent Team |
| 工作区隔离 | 可为 Task 创建独立 Git worktree，成员只在任务执行期间切换工作目录 |
| 自动化与扩展 | 生命周期 Hooks、进程内定时任务、stdio / Streamable HTTP MCP 工具接入 |
| 桌面使用 | 多标签对话、工作区历史、流式输出、模型管理、敏感操作审批 |

这些能力共用同一条工具执行链。模型不能绕过注册表直接调用本地 handler，子 Agent 和 Agent Team 成员也有各自的工具范围与审批规则。

## 它是怎么运行的

```text
Electron / CLI / Python API
            |
            v
  create_default_agent()
            |
            v
        AgentLoop <----------> Responses API
            |
            | function_call
            v
       ToolRegistry
            |
            v
PermissionHook -> PreToolUse -> handler -> PostToolUse
            |
            | function_call_output + call_id
            v
        AgentLoop
```

每次模型返回 `function_call`，`ToolRegistry` 会解析参数、检查函数签名、执行 Hook 和权限判断，再调用工具。结果会带着原始 `call_id` 返回 Responses API。循环一直运行到模型给出最终文本，或者达到工具轮数上限。

默认组件都在 `src/myagent/composition.py` 中组装。`AgentLoop` 只负责模型协议和运行生命周期，文件、Shell、权限、记忆、任务与协作功能各自放在独立模块中。

### 工具与协作方式

- SubAgent 每次从空白上下文开始，适合一次性的独立任务。`run_subagent` 会等待结果，`fork_subagent` 会立即返回任务 ID，随后用 `collect_subagent` 收取结果。
- Agent Team 成员在独立线程中持续存在，有自己的上下文、收件箱和工具注册表。主 Agent 可以发送消息、审核执行计划，并在成员空闲后请求关闭。
- Task 保存在工作区中，可以声明依赖、检查是否可执行、认领和完成。任务可绑定独立 Git worktree，减少多人或多 Agent 修改同一目录时的干扰。
- Skill 只把名称和简介放进模型 instructions。模型需要具体内容时再调用 `load_skill`，不会在每轮请求中加载全部 Skill 正文。
- 长期记忆先搜索元数据，再按需读取正文。大型工具输出则保存在单独的结果文件中，history 只保留摘要和引用。

## 本地数据放在哪里

| 路径 | 内容 | 生命周期 |
| --- | --- | --- |
| `.myagent/skills/` | 工作区 Skill | 跨会话保留 |
| `.myagent/tasks/tasks.json` | Task、依赖、owner 和 worktree 绑定 | 跨会话保留 |
| `.myagent/memories/` | 长期记忆目录与正文 | 跨会话保留 |
| `.myagent/memory/tool-results/` | 从上下文卸载的大型工具结果 | 跨会话保留 |
| `.myagent/agent-team/inboxes/` | Agent Team 消息文件 | 工作区文件，运行时消费 |
| `%APPDATA%\MyAgent\models.json` | 桌面模型 ID、API Key 和 Base URL | 当前 Windows 用户下保留 |
| `%APPDATA%\MyAgent\settings.json` | 桌面运行参数 | 当前 Windows 用户下保留 |
| `%APPDATA%\MyAgent\conversations\catalog.jsonl` | 会话侧栏索引和活动会话 | 当前 Windows 用户下保留，可由会话 JSONL 重建 |
| `%APPDATA%\MyAgent\conversations\<id>.jsonl` | CLI/桌面共用的逐会话消息与协议历史 | 当前 Windows 用户下保留 |
| `%APPDATA%\MyAgent\conversations.json` | 兼容格式的会话快照输入 | 转换为逐会话 JSONL 后保留原文件 |
| `%LOCALAPPDATA%\MyAgent\runtime\` | 桌面专属 Python 环境和依赖指纹 | 可重建，不包含模型或会话数据 |

`myagent.config.json` 已加入 `.gitignore`。桌面端的 API Key 也保存在仓库外，但仍是本机明文文件，请按敏感凭据保护。

设置 `MYAGENT_HOME` 可以同时覆盖模型、设置和会话的数据根目录；共用会话位于
`$env:MYAGENT_HOME\conversations`。旧的 `MYAGENT_GUI_HOME` 仍兼容，但仅在没有设置
`MYAGENT_HOME` 时使用。

## 配置

CLI 会读取当前目录中的 `myagent.config.json`。可以先复制仓库里的 [`myagent.config.example.json`](./myagent.config.example.json)，再按需修改：

```json
{
  "api_key": null,
  "base_url": null,
  "model": "gpt-5.6-sol",
  "fallback_model": "gpt-5.6-terra",
  "max_tool_rounds": 10,
  "bash_timeout_seconds": 30,
  "todo_reminder_tool_calls": 4,
  "subagent_max_workers": 4,
  "subagent_max_tasks": 16,
  "mcp_servers": []
}
```

`api_key` 和 `base_url` 为 `null` 时，OpenAI SDK 会使用环境变量或自己的默认连接。其他兼容服务必须实现本项目使用的 Responses API 语义，只提供 Chat Completions 接口还不够。

配置优先级如下：

- 模型：`--model`，然后是 `OPENAI_MODEL`，最后是配置文件。
- 连接信息：`OPENAI_API_KEY` 和 `OPENAI_BASE_URL` 会覆盖配置文件。
- 数值参数：对应环境变量会覆盖配置文件。

可用的数值环境变量包括 `AGENT_MAX_TOOL_ROUNDS`、`BASH_TIMEOUT_SECONDS`、`TODO_REMINDER_TOOL_CALLS`、`SUBAGENT_MAX_WORKERS` 和 `SUBAGENT_MAX_TASKS`。

<details>
<summary>连接 stdio MCP Server</summary>

把 `mcp_servers` 改为：

```json
[
  {
    "name": "local",
    "transport": "stdio",
    "command": "python",
    "args": ["path/to/server.py"],
    "env": {}
  }
]
```

远端工具会以 `mcp__local__<tool-name>` 注册到主 Agent。支持 stdio、Streamable HTTP 和启动时工具发现；不支持旧版 SSE transport、resources、prompts 或运行中刷新。MCP 工具不会暴露给 SubAgent、Agent Team 成员和定时任务。

</details>


<details>
<summary>连接 RAG 的 Streamable HTTP MCP Server</summary>

RAG 独立运行，MyAgent 只连接它的 `/mcp` URL。配置示例见
[`myagent.rag.config.example.json`](./myagent.rag.config.example.json)，两边的启动、鉴权、验证步骤见
[`docs/rag-mcp.md`](./docs/rag-mcp.md)。

```json
{
  "mcp_servers": [{
    "name": "rag",
    "transport": "streamable_http",
    "url": "http://127.0.0.1:8080/mcp",
    "connect_timeout_seconds": 10,
    "call_timeout_seconds": 120,
    "headers": {}
  }]
}
```

远程服务使用对应主机的 URL，可通过 `headers.Authorization` 配置 Bearer 令牌。HTTP 连接直接访问指定地址，不使用进程的代理环境变量。现有 stdio 配置仍可同时使用。

</details>

## 在 Python 中使用

```python
from openai import OpenAI

from myagent import AgentConfig, create_default_agent


agent = create_default_agent(
    OpenAI(),
    config=AgentConfig(model="gpt-5.6-sol"),
    cwd="path/to/workspace",
)

try:
    answer = agent.run("阅读项目并找出测试入口")
    print(answer)
finally:
    agent.close()
```

需要接入自己的界面时，可以传入 `approval_callback` 和 `stream_callback`。需要限制能力时，使用 `AgentConfig(allowed_tools=...)` 缩小工具白名单。应用退出前应调用 `agent.close()`，让后台 Agent、调度器和 MCP 连接正常收尾。

如果要观察或改写运行过程，可以注册四类 Hook：

- `UserPromptSubmit`：用户输入进入 history 前
- `PreToolUse`：工具 handler 执行前
- `PostToolUse`：工具产生结果后
- `Stop`：一次 `run()` 返回或抛出异常前

## 安全边界

MyAgent 默认做了几层限制：

1. 文件工具把路径限制在启动工作区内，并限制读取大小和搜索结果数量。
2. `PermissionHook` 是第一个 `PreToolUse` Hook。写文件、编辑文件、删除操作和持久化状态变更需要逐次批准。
3. 递归且强制删除的 `rm` 命令会直接拒绝，审批回调也不能放行。
4. Electron renderer 通过受限 preload 调用 Python sidecar，API Key 不进入 renderer 快照和审批内容。

Shell 检查依赖静态分类，无法识别所有脚本、变量展开和解释器间接执行。当前版本不适合直接交给不受信任的用户，也不能替代容器、低权限账户或其他操作系统级隔离。

## 当前边界

- 定时任务、TODO 状态和未收取的 Fork 记录只存在于当前进程，重启后不会恢复。
- Agent Team 成员会持续到收到关闭请求或运行时退出，但正在执行的模型请求不会被强制取消。
- MCP 支持 stdio 与 Streamable HTTP tools 接入，能力范围见上面的配置说明。
- 工作区中的 Skill、Task、记忆和工具结果可能包含敏感信息，项目不会自动加密、过期或清理这些文件。
- 多个独立 MyAgent 进程同时写同一个工作区时，需要调用方自行协调。

## 项目结构

```text
MyAgent/
|-- desktop/                 Electron 主进程、preload 和 renderer
|-- evaluations/             自建小仓库评测集、隐藏 grader 与离线回放脚本
|-- src/myagent/             Agent Loop、工具、权限、记忆与协作运行时
|-- tests/                   Python 单元测试与协议回归测试
|-- myagent.config.example.json
|-- pyproject.toml
`-- start_gui.cmd            Windows 桌面启动器
```

建议从这些文件开始读：

- `src/myagent/agent.py`：Responses API 循环
- `src/myagent/composition.py`：默认组件装配入口
- `src/myagent/tooling.py`：工具注册与执行流水线
- `src/myagent/permissions.py`：工具可见性和审批策略
- `src/myagent/desktop_sidecar.py`：Electron 与 Python 的本地协议边界
- `src/myagent/agent_team.py`：长期运行的协作成员
- `src/myagent/tasks.py` 和 `src/myagent/worktrees.py`：任务与 Git worktree 生命周期

## 开发与测试

运行 Python 测试：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

运行桌面测试和启动检查：

```powershell
Set-Location desktop
npm test
npm run check
Set-Location ..

.\start_gui.cmd --check
```

`npm run check` 检查 Electron 资源和本地依赖，`start_gui.cmd --check` 还会检查 Python sidecar 入口。

## 评测

CI 快速回放集包含四个自建小仓库任务，覆盖单文件 bugfix、功能补齐、配置调整和
跨文件修改；它只负责确定性门禁。下方的完整模型评测包含 50 题、150 次真实模型运行。
真实模型评测必须手动选择，因此不会在普通 PR 中使用密钥或产生模型费用。

校验评测集：

```powershell
$env:PYTHONPATH = "src"
python -m myagent.evaluation validate
```

运行离线评测并生成 `runs.jsonl`、`summary.json` 和 `summary.md`：

```powershell
python -m myagent.evaluation run `
  --provider replay `
  --allow-code-execution `
  --output eval-results/replay
```

运行完整开发门禁：

```powershell
./scripts/quality-gate.ps1
```

评测会执行临时工作区中的样例代码；临时目录不是操作系统安全沙箱。数据集、真实模型
命令、判分规则和安全边界见 `evaluations/README.md`。

### 完整模型评测

当前评测集共 50 题：25 条组件题评估必要工具、调用顺序和工具克制，25 条集成题
评估目标达成、约束遵守和完成声明。评测包含两类任务和六个评分维度；每题运行 3 次，
共 150 次真实模型运行。

2026-08-24 的脱敏审计结果如下。Agent 请求模型为 `deepseek-v4-flash`，API 返回模型
同名，Thinking 为 enabled；`DeepSeek-V4-Flash-0731` 是本次报告声明的发布标签，
不是服务端版本锁定。

| 指标 | 结果 |
| --- | ---: |
| Hard-verifier | 150 / 150 |
| 严格 Codex 离线 Judge | 146 / 150（97.33%） |
| Judge Pass@3 | 100% |
| Judge Pass^3 | 94% |
| 平均工具调用 | 3.987 |
| P50 / P95 延迟 | 5.057 s / 16.589 s |
| 总 Token | 1,072,261 |

| 评分维度 | 均分 |
| --- | ---: |
| 必要工具覆盖 | 4.947 / 5 |
| 调用顺序正确性 | 4.922 / 5 |
| 工具克制 | 4.893 / 5 |
| 目标达成 | 5.000 / 5 |
| 约束遵守 | 4.080 / 5 |
| 完成声明诚实性 | 4.960 / 5 |

严格 Judge 拒绝了 4 条功能结果正确但违反工具契约的组件运行；75 条集成运行全部通过。
这是 MyAgent 的自建私有评测，不是公开 benchmark。

脱敏报告和汇总数据已纳入仓库。原始运行轨迹、Judge packets 和 verdict 保持在忽略的
本地审计目录中；数据集位置、完整复现与判分步骤见
[`evaluations/README.md`](evaluations/README.md)。
