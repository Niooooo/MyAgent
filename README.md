# MyAgent

一个结构清晰、可扩展的 Python Agent Loop，使用 OpenAI Responses API，并提供本地命令与工作区文件工具。

## 工作流程

1. 将用户输入和工具定义发送给 Responses API。
2. 如果模型返回 `function_call`，在本地执行对应工具。
3. 将 `function_call_output` 及其 `call_id` 发送回模型。
4. 重复以上过程，直到模型返回最终文本。

每轮都会保留完整的 `response.output`，因此 reasoning item 也会正确传回后续请求。

## 架构与模块边界

默认运行时通过 `composition.py` 统一创建和连接组件，CLI 不再把具体工具逐个塞进 Agent Loop：

```text
CLI / AgentConfig
        |
        v
composition.create_default_agent
        |-- AgentLoop
        |-- ToolRegistry <---- HookRegistry
        |       |              |-- PermissionHook（首个 PreToolUse）
        |       |              `-- TodoReminder
        |       `-- Bash / Filesystem / TODO / SubAgent FunctionTools
        |-- SubAgentManager（有界 ThreadPoolExecutor）
        |       `-- 新 AgentLoop -> 不含 SubAgent tools 的新 Registry
        `-- TodoList（每个 Agent 实例独立）
```

一次工具调用的实际路径是：

```text
AgentLoop -> Responses API -> function_call
          -> ToolRegistry（JSON 解析与签名绑定）
          -> PreToolUse / PermissionHook -> handler -> PostToolUse
          -> function_call_output（保留原始 call_id）-> Responses API
```

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 命令行参数、环境变量配置、OpenAI 客户端创建和终端输入输出 |
| `composition.py` | 唯一的默认 composition root；创建并连接 Agent、Registry、Hook、权限、TODO 和本地工具 |
| `agent.py` | Responses API 循环、跨轮历史、工具结果回传、轮数上限、`reset()` 与运行生命周期 |
| `tooling.py` | `FunctionTool`、注册顺序、参数解析/签名校验、Pre/handler/Post 执行流水线和结构化错误 |
| `hooks.py` | `UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop` 事件及同步注册表 |
| `permissions.py` | 工具白名单、Shell 静态分类、逐次审批、拒绝结果和 `PermissionHook` |
| `tools.py` | Bash schema、Bash 执行和永久拒绝的纵深检查 |
| `filesystem.py` | 工作区内文件读写、精确编辑、Glob/Grep、安全路径解析及对应 schemas |
| `todo.py` | 实例级 TODO 状态、版本化验证、TODO tools 和 Hook 驱动提醒 |
| `subagents.py` | 同步子 Agent、后台 Fork、结果状态/收取、有界并发和 executor 关闭 |

应用入口推荐使用 `create_default_agent(client, config=AgentConfig(...))`。现有的 `AgentLoop(client, bash_tool=..., workspace_root=..., ...)` 构造方式与 `myagent.tools.build_default_tool_registry` 导入路径仍作为兼容入口保留；它们最终也委托给同一个 composition root。

## 内置工具

| 工具 | 用途 |
| --- | --- |
| `bash` | 在工作目录中运行 Bash 命令 |
| `read_file` | 按行读取 UTF-8 文本文件，可分块读取大文件 |
| `write_file` | 新建或完整覆盖文件，并自动创建父目录 |
| `edit_file` | 精确替换文本，默认拒绝有歧义的多处替换 |
| `glob` | 使用相对 Glob 模式查找文件和目录 |
| `grep` | 使用正则表达式搜索文本，返回文件、行号、列号和片段 |
| `update_todo_list` | 创建或整体更新全局 TODO List |
| `get_todo_list` | 读取 TODO List 与验证状态 |
| `record_todo_verification` | 在实际验证后记录具体证据 |
| `run_subagent` | 阻塞运行一个全新的子 Agent，只返回最终文本 |
| `fork_subagent` | 在有界后台线程池启动独立子 Agent，立即返回 `fork_id` |
| `collect_subagent` | 轮询或限时等待 Fork；终态结果只可收取一次 |

文件类工具统一限制在启动 `myagent` 时的工作目录内。工具定义、权限检查和执行都经过 `ToolRegistry` 的唯一入口；新增工具时只需提供 schema 与 handler，不需要继续扩展 Agent Loop 的条件分支。

## 子 Agent：同步调用与后台 Fork

主 Agent 可以按任务性质选择同步或后台执行。同步工具会等待一个全新的子 Agent 完成：

```json
{"task": "检查 src/myagent/tooling.py 的错误边界并给出结论"}
```

`run_subagent` 成功时只返回：

```json
{"ok": true, "output": "子 Agent 的最终回答"}
```

后台任务分两步。先调用 `fork_subagent`：

```json
{"task": "独立审阅测试覆盖并列出遗漏"}
```

它会立即返回不可预测的 `fork_id` 和 `status=running`。随后调用：

```json
{"fork_id": "返回的 fork_id", "wait": true, "timeout_seconds": 30}
```

`collect_subagent` 在任务未完成时返回 `running`；等待超时会返回 `code=timeout`，但不会取消仍在运行的任务；完成时返回 `status=completed` 和唯一的 `output` 文本；失败只返回有界错误摘要。终态只可收取一次，再次收取会得到 `status=cleaned`，从未存在的 ID 则为 `status=unknown`。

每次同步调用和 Fork 都创建新的 `AgentLoop`、history、`ToolRegistry`、`PermissionManager`、`TodoList` 和 `TodoReminder`。子 Agent 不继承主 Agent 或其他子 Agent 的对话 history，只接收显式 `task`。其 reasoning、Responses `output`、工具参数、中间结果和完整 history 不会进入管理器记录或主 Agent history；成功边界只保留最终文本。

子 Agent 的普通工具仍完整经过 JSON/签名校验、首个 `PermissionHook`、其他 `PreToolUse`、handler 和 `PostToolUse`，并保留原始 `call_id`。它自身也会触发 `UserPromptSubmit` 和 `Stop`。但三种 SubAgent 管理工具不会注册到子 Registry：schemas 中不可见，伪造 `function_call` 也只会得到 `Unknown tool`，因此不能递归委派。自定义 `allowed_tools` 会先应用，再额外排除整个管理工具族。

后台执行默认最多 4 个 worker，并最多保留 16 个尚未收取的 Fork 记录；达到记录上限后必须先收取终态任务。共享的用户 Hook 和终端逐次审批通过 Hook 执行锁串行化，避免多个后台线程的 `input()` 交错；注入的 Bash handler 也会被串行保护。默认运行时不会自动批准任何敏感调用。

程序化使用结束后应调用 `agent.close()`；CLI 的所有退出路径都会自动调用它。关闭过程拒绝新任务、取消尚未启动的 Future、等待正在执行的子 Agent 收尾，并清理内存状态，因此进程退出可能等待已开始的 API 请求或工具调用完成。TODO、Fork 状态和已收取标记都只保存在当前进程内，重启后不会恢复。

## 生命周期 Hooks

`HookRegistry` 提供四个按注册顺序执行的扩展阶段：

| Hook | 触发点 | 可执行的操作 |
| --- | --- | --- |
| `UserPromptSubmit` | 用户输入提交后、写入历史并进入 Loop 前 | 修改或拒绝输入，通过 `add_context()` 注入上下文 |
| `PreToolUse` | 工具参数完成 JSON 与签名检查后、handler 执行前 | 权限判断、审计日志，通过 `deny()` 拒绝执行 |
| `PostToolUse` | handler 已执行并生成结构化结果后 | 检查或替换输出，以及按需执行 `git add` 等副作用 |
| `Stop` | 每次 `AgentLoop.run()` 返回或抛出异常前 | 根据成功/失败状态执行清理和收尾 |

四类 Hook 使用同一实例，因此 Agent Loop 与工具执行路径共享一致的注册顺序和状态：

```python
from myagent import (
    HookRegistry,
    PostToolUse,
    PreToolUse,
    Stop,
    UserPromptSubmit,
    create_default_agent,
)

hooks = HookRegistry()


def prepare_prompt(event: UserPromptSubmit) -> None:
    if len(event.prompt) > 10_000:
        event.reject("prompt is too long")
        return
    event.add_context("Current repository: MyAgent")


def audit_before_tool(event: PreToolUse) -> None:
    print("tool start", event.tool_name, dict(event.arguments))


def validate_tool_output(event: PostToolUse) -> None:
    if "ok" not in event.result:
        event.replace_result({"ok": False, "error": "missing ok field"})


def cleanup(event: Stop) -> None:
    print("agent stopped", event.reason.value)


hooks.register(UserPromptSubmit, prepare_prompt)
hooks.register(PreToolUse, audit_before_tool)
hooks.register(PostToolUse, validate_tool_output)
hooks.register(Stop, cleanup)

agent = create_default_agent(client, hooks=hooks)
```

默认权限系统已经通过内置 `PermissionHook` 接入 `PreToolUse`，原有白名单、永久拒绝和逐次人工审批行为保持不变。内置权限 Hook 会优先执行，但即使调用被拒绝，后续注册的 `PreToolUse` 审计 Hook 仍会收到事件。`PostToolUse` 不会默认运行 `git add`；只有显式注册对应 Hook 后才会产生该副作用。

## 全局 TODO List

每个 `AgentLoop` 实例持有一份内存中的全局 TODO List，在该实例的多轮对话间共享。任务状态严格限定为 `pending`、`in_progress` 和 `completed`。模型应通过 `update_todo_list` 发送完整列表；真正发生内容或状态变化时，列表版本会递增，并使旧的验证记录失效。

TODO 提醒通过已有 Hook 接入，不在 Agent Loop 中增加工具名分支：

- 当存在未完成任务，且连续多次非 TODO 工具调用没有真正修改列表时，`PostToolUse` 会把当前列表和更新提示附加到工具结果中。默认阈值为 4 次，可通过 `TODO_REMINDER_TOOL_CALLS` 调整。
- 当所有任务都标记为 `completed`、但当前列表版本还没有验证证据时，每次相关工具结果以及下一轮用户输入前都会注入验证提醒。模型实际运行测试或检查后，必须调用 `record_todo_verification` 记录具体命令与结果，才能消除提醒。

验证是列表级元数据，不会引入第四种任务状态。TODO List 当前不写入磁盘；重启进程后会重新开始。

## 环境要求

- Python 3.11+
- 可从命令行调用的 `bash`（Linux/macOS 自带；Windows 可使用 Git Bash 或 WSL 中的 Bash）
- `OPENAI_API_KEY`

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
export OPENAI_API_KEY="your-api-key"
```

Windows PowerShell 激活虚拟环境及设置密钥：

```powershell
.\.venv\Scripts\Activate.ps1
$env:OPENAI_API_KEY = "your-api-key"
```

## 运行

交互模式：

```bash
myagent
```

单次任务：

```bash
myagent "列出当前目录中的 Python 文件"
```

可通过环境变量覆盖默认配置：

- `OPENAI_MODEL`：默认 `gpt-5.6-sol`
- `AGENT_MAX_TOOL_ROUNDS`：默认 `10`
- `BASH_TIMEOUT_SECONDS`：默认 `30`
- `TODO_REMINDER_TOOL_CALLS`：TODO List 长时间未更新提醒阈值，默认 `4`
- `SUBAGENT_MAX_WORKERS`：同步与 Fork 共用的最大子 Agent worker 数，默认 `4`
- `SUBAGENT_MAX_TASKS`：尚未收取的 Fork 记录上限，默认 `16`

交互模式中的 `exit`、`quit` 或 `Ctrl-D` 会退出程序。

## 安全边界

文件工具会拒绝工作区外路径、绝对或包含 `..` 的越界 Glob，并对读取大小和搜索结果数量设限。写入使用同目录临时文件替换，避免写入中断留下半个文件。

工具执行前会按以下顺序经过独立的权限层：

1. **工具白名单**：默认只暴露并允许内置 Shell、文件和 TODO 工具。不在白名单中的工具即使已经注册，也不会发送给模型，并且直接调用同样会被拒绝。
2. **永远禁止**：递归且强制删除的 `rm` 命令（例如 `rm -rf`、`rm -fr`、`rm -r -f` 和 `rm --recursive --force`）直接拒绝，审批回调不能将其放行。
3. **逐次审批**：普通 `rm`、明确的删除/覆盖类 Shell 命令、Shell 输出重定向、`write_file` 和 `edit_file` 会展示工具名、原因和完整参数。只有用户对本次调用输入 `y` 或 `yes` 后才会执行；直接回车、拒绝、输入中断或未配置审批器都会拒绝。

通过 Python 创建 Agent 时，可以缩小工具白名单并注入自己的审批 UI：

```python
agent = AgentLoop(
    client,
    allowed_tools={"read_file", "glob", "grep"},
    approval_callback=my_approval_callback,
)
```

当前 Shell 分类覆盖常见的删除、覆盖、进程终止、磁盘修改和 Git 数据丢失命令，但静态分析不能识别脚本、变量展开或解释器间接执行等全部绕过方式，因此它不是完整的命令沙箱。不要把当前版本直接暴露给不受信任的用户；生产环境仍应同时使用操作系统级最小权限和沙箱隔离。

## 测试

```bash
python -m unittest discover -s tests -v
```
