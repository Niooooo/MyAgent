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
        |-- shared SkillStore <---- .myagent/skills/*.md
        |       `-- dynamic Catalog provider（每次请求重新读取）
        |-- shared ToolResultStore <---- .myagent/memory/tool-results/*.json
        |-- AgentLoop
        |       |-- 基础 instructions + metadata-only Skill Catalog
        |       `-- 独立 ContextMemory（history 块与压缩状态）
        |-- ToolRegistry <---- HookRegistry
        |       |              |-- PermissionHook（首个 PreToolUse）
        |       |              `-- TodoReminder
        |       `-- Bash / Filesystem / TODO / Skill / Memory / SubAgent FunctionTools
        |-- SubAgentManager（有界 ThreadPoolExecutor）
        |       `-- 新 AgentLoop -> 共享 Stores、独立 ContextMemory、不含 SubAgent tools
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
| `skills.py` | Skill 格式、严格 front matter 解析、校验、Catalog、原子持久化及四个 FunctionTool |
| `memory.py` | 大型工具结果持久化、引用分段读取、确定性工具摘要和原子历史块压缩 |
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
| `load_skill` | 按 Catalog 中的精确名称加载一个 Skill 的完整正文 |
| `add_skill` | 创建一个新 Skill，不覆盖同名文件；逐次审批 |
| `update_skill` | 完整替换已有 Skill 的 description 和正文；逐次审批 |
| `delete_skill` | 删除一个已有 Skill，不返回已删除正文；逐次审批 |
| `load_memory` | 按 `memory://tool-result/<id>` 引用分段读取已持久化的完整工具结果 |
| `run_subagent` | 阻塞运行一个全新的子 Agent，只返回最终文本 |
| `fork_subagent` | 在有界后台线程池启动独立子 Agent，立即返回 `fork_id` |
| `collect_subagent` | 轮询或限时等待 Fork；终态结果只可收取一次 |

文件类工具统一限制在启动 `myagent` 时的工作目录内。工具定义、权限检查和执行都经过 `ToolRegistry` 的唯一入口；新增工具时只需提供 schema 与 handler，不需要继续扩展 Agent Loop 的条件分支。

## Skill：持久化与渐进式披露

Skill 是工作区级持久化指令，一个 Skill 对应一个 UTF-8 Markdown 文件：

```text
.myagent/skills/<skill-name>.md
```

文件格式固定为：

```markdown
---
name: python-code-review
description: Review Python changes for correctness, readability and regressions.
---
# Python Code Review

这里是完整的 Skill 指令正文。
```

这里的 front matter parser 是项目专用的严格解析器，只支持依次出现的 `name` 和 `description` 两个字段，并不是通用 YAML parser。`name` 必须匹配 `[a-z0-9][a-z0-9_-]{0,63}`，同时也是唯一文件名；绝对路径、`..`、路径分隔符、控制字符、大小写冲突及文件名/front matter 不一致都会被拒绝。`description` 必须是非空单行文本，默认最多 500 个字符和 2000 个 UTF-8 字节；正文必须是非空文本，默认最多 100000 个 UTF-8 字节；完整文件默认最多 104096 字节；Catalog 默认最多 100 个 Skill。非法 UTF-8、损坏 front matter、过大文件、越界符号链接和外部文件造成的数量超限都会产生明确的完整性错误，不会静默截断或把原始损坏内容注入 instructions。

启动 Agent 或读取空 Catalog 不会创建 `.myagent` 或 `skills` 目录。只有获批的 `add_skill` 真正进入 handler 后才会创建目录。写入使用目标目录中的临时文件、flush/fsync 和 `os.replace` 原子替换；失败会清理临时文件，`update_skill` 失败时旧文件保持不变。

上下文采用两层渐进式披露：

1. 每次 Responses API 请求前，动态 instructions provider 都重新读取 Catalog，并只注入按 `name` 排序的 JSON `name + description`；空目录会注入明确的空数组。正文、路径、修改时间和其他存储元数据不会进入 Catalog。
2. 模型选定相关 Skill 后必须调用 `load_skill(name)`；只有该次 `function_call_output` 才返回该 Skill 的完整 `content`，并保留 Responses API 的原始 `call_id`。不会自动把全部 Skill 正文装入上下文。

`add_skill`、`update_skill` 和 `delete_skill` 成功后，下一次模型请求会立即看到新 Catalog；基础 `AgentLoop.instructions` 不会被修改或逐轮累加。Skill Catalog 属于真实的 Responses API `instructions`，不通过 `UserPromptSubmit` 伪装成普通 developer message。Catalog 只表示 Skill 存在，不表示正文已经加载；Skill 内容不能覆盖 system/developer/user 指令、权限、Hook、安全策略或其他更高优先级规则。任何 Skill 工具失败时，模型都不得声称操作成功。

四个工具都通过 `ToolRegistry`，调用顺序仍是 JSON/签名检查 → 首个 `PermissionHook` → 其他 `PreToolUse` → handler → `PostToolUse` → `function_call_output`。`load_skill` 默认允许；其余三个持久化写工具逐次展示完整原始参数并请求审批，不缓存批准。预期失败使用稳定的 `ok=false`、`code`、`error`，例如 `invalid_skill_name`、`skill_not_found`、`skill_exists`、`skill_file_too_large`、`invalid_utf8`、`invalid_front_matter`、`skill_name_mismatch`、`skill_limit_reached` 和 `permission_denied`，且不返回绝对路径或 traceback。

默认父 Agent 和每个同步/异步子 Agent 都注册四个普通 Skill 工具，并共享 composition root 创建的同一个 `SkillStore`，所以任一 Agent 的修改会在其他 Agent 的下一次请求中出现。各自的 history、TODO、Hook registry/runtime 仍然隔离或按原架构克隆；子 Agent 依旧没有 `run_subagent`、`fork_subagent`、`collect_subagent`。自定义 allowlist 可以逐个隐藏并拒绝 Skill 工具；如果某个 Agent 不允许 `load_skill`，该 Agent 不会收到无法使用的 Skill Catalog。注入自定义 `ToolRegistry` 且不提供 `instructions_provider` 时，不会擅自创建第二套默认 Skill runtime。

同一默认运行时中的共享 `SkillStore` 使用 `RLock` 保护复合读写，因此父 Agent、同步子 Agent和多个 Fork 并发操作时不会在进程内越过存在性检查或互相覆盖；同名并发 add 只会有一个成功。原子替换也保证读取只能看到旧文件或新文件，而不是半个文件。这个保证仅覆盖共享同一 Store 的当前 Agent 进程；当前实现没有跨进程锁或跨进程事务，多个独立 MyAgent 进程同时写同一目录仍需外部协调。

## 上下文记忆与分级压缩

ContextMemory 只控制 Responses 输入的体积，不实现用户画像或语义长期记忆。默认依次应用三级策略：序列化工具结果超过 65536 个 UTF-8 字节时立即把完整 JSON 写入 Store，并只向模型保留最多 2000 字符的确定性预览；请求前，超过 8192 字节的尚未卸载工具结果会被持久化并换成确定性字段摘要；完成这两步后，序列化 history 仍超过 131072 字节且存在可安全裁剪的旧块时，AgentLoop 才使用当前配置的模型发起一次独立的 `tools=[]` Responses 请求，将这些历史数据压缩成最多 4000 字符的普通 assistant 摘要消息。阈值通过 `AgentConfig(memory=MemoryConfig(...))` 集中配置。第三级会增加一次模型请求的延迟和费用；请求失败、返回空文本或没有安全旧块时，主请求继续使用未裁剪的 history。

一个 `response.output` 中的 reasoning、全部 `function_call` 和随后生成的全部 `function_call_output` 作为同一个交换块登记。第三级只能整体移除已经发送过且 call/output 配对完整的旧块；最早任务入口、最近两个用户回合、当前未发送用户输入以及模型刚生成的调用和结果会保留。送给摘要模型的输入被明确标为不可信历史数据，只包含用户/助手文本片段、工具名、状态、错误码、引用和内容提示；reasoning 只替换为“曾存在且内容已省略”的标记。模型摘要成功并通过非空和长度校验后才原子替换旧块；重复压缩会让模型合并已有摘要，不会逐轮追加摘要消息。

完整结果仅在第一次达到保存条件时创建 `.myagent/memory/tool-results/`，以 UTF-8 JSON 保存，并通过随机的 `memory://tool-result/<id>` 引用访问。写入使用同目录临时文件、flush/fsync 和 `os.replace`；`load_memory` 默认只读允许，并用 `offset`、`max_chars` 分段返回，单次硬上限为 16000 字符、默认配置上限为 4000。`reset()` 会清空 Agent history、块索引和摘要状态，但不会删除这些结果文件。父 Agent 和子 Agent 共享 workspace-scoped `ToolResultStore`，同时各自持有独立的 ContextMemory 和 history；显式注入自定义 `ToolRegistry` 而不提供 ContextMemory 时不会创建另一套默认记忆运行时。

持久化失败时，Agent 会保留原始完整工具输出，不会声称已经保存。当前切片不检测敏感信息，不加密，不自动过期、清理或限制磁盘配额；第三级压缩也暂不删除被裁剪块引用的结果文件，因为同一引用可能仍被保留 history 或外部调用方使用。因此大型工具结果可能以明文留在工作区隐藏目录中，使用者应自行控制工作区内容和生命周期。

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

每次同步调用和 Fork 都创建新的 `AgentLoop`、history、`ContextMemory`、`ToolRegistry`、`PermissionManager`、`TodoList` 和 `TodoReminder`，但共享工作区级 `SkillStore` 与 `ToolResultStore`。子 Agent 不继承主 Agent 或其他子 Agent 的对话 history，只接收显式 `task`。其 reasoning、Responses `output`、工具参数、中间结果和完整 history 不会进入管理器记录或主 Agent history；成功边界只保留最终文本。

子 Agent 的普通工具仍完整经过 JSON/签名校验、首个 `PermissionHook`、其他 `PreToolUse`、handler 和 `PostToolUse`，并保留原始 `call_id`。它自身也会触发 `UserPromptSubmit` 和 `Stop`。但三种 SubAgent 管理工具不会注册到子 Registry：schemas 中不可见，伪造 `function_call` 也只会得到 `Unknown tool`，因此不能递归委派。自定义 `allowed_tools` 会先应用，再额外排除整个管理工具族。

后台执行默认最多 4 个 worker，并最多保留 16 个尚未收取的 Fork 记录；达到记录上限后必须先收取终态任务。共享的用户 Hook 和终端逐次审批通过 Hook 执行锁串行化，避免多个后台线程的 `input()` 交错；注入的 Bash handler 也会被串行保护，Skill 文件复合操作由共享 Store 的 `RLock` 保护。默认运行时不会自动批准任何敏感调用。

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
- 一个实现 OpenAI Responses API 的服务，以及通过 `OPENAI_API_KEY` 或 `myagent.config.json` 提供的 API Key

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

先复制示例配置；真实配置 `myagent.config.json` 已加入 `.gitignore`，避免 API Key 被误提交：

```powershell
Copy-Item myagent.config.example.json myagent.config.json
```

CLI 会读取当前工作目录中的 `myagent.config.json`。它可以同时配置 OpenAI 客户端连接、主/备用模型和现有运行参数：

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
  "subagent_max_tasks": 16
}
```

`api_key` 和 `base_url` 为 `null` 时沿用 OpenAI SDK 的环境变量或默认连接；也可以填写任意 OpenAI API 兼容服务的密钥、地址和模型 ID。兼容服务必须实现本项目使用的 Responses API（`/responses`），并支持工具调用、`max_output_tokens` 和 `previous_response_id` 等项目实际使用的语义；只有 Chat Completions 接口的服务不能直接使用。

配置文件不存在时使用内置默认值。命令行和环境变量的优先级高于文件：`--model` → `OPENAI_MODEL` → `myagent.config.json`，`OPENAI_API_KEY`/`OPENAI_BASE_URL` → 文件连接字段，已有数值环境变量 → 文件数值字段。备用模型取自配置文件；连续 529 进入第 4、5 次重试时才会切换，429 不切换。SDK 自身重试固定关闭，不能通过配置覆盖。

可通过环境变量覆盖默认配置：

- `OPENAI_MODEL`：默认 `gpt-5.6-sol`
- `OPENAI_API_KEY`：覆盖配置文件中的 `api_key`
- `OPENAI_BASE_URL`：覆盖配置文件中的 `base_url`
- `AGENT_MAX_TOOL_ROUNDS`：默认 `10`
- `BASH_TIMEOUT_SECONDS`：默认 `30`
- `TODO_REMINDER_TOOL_CALLS`：TODO List 长时间未更新提醒阈值，默认 `4`
- `SUBAGENT_MAX_WORKERS`：同步与 Fork 共用的最大子 Agent worker 数，默认 `4`
- `SUBAGENT_MAX_TASKS`：尚未收取的 Fork 记录上限，默认 `16`

交互模式中的 `exit`、`quit` 或 `Ctrl-D` 会退出程序。

## 安全边界

文件工具会拒绝工作区外路径、绝对或包含 `..` 的越界 Glob，并对读取大小和搜索结果数量设限。写入使用同目录临时文件替换，避免写入中断留下半个文件。

工具执行前会按以下顺序经过独立的权限层：

1. **工具白名单**：默认只暴露并允许内置 Shell、文件、TODO、Skill 和主 Agent 的 SubAgent 管理工具。不在白名单中的工具即使已经注册，也不会发送给模型，并且直接调用同样会被拒绝。
2. **永远禁止**：递归且强制删除的 `rm` 命令（例如 `rm -rf`、`rm -fr`、`rm -r -f` 和 `rm --recursive --force`）直接拒绝，审批回调不能将其放行。
3. **逐次审批**：普通 `rm`、明确的删除/覆盖类 Shell 命令、Shell 输出重定向、`write_file`、`edit_file`、`add_skill`、`update_skill` 和 `delete_skill` 会展示工具名、原因和完整参数。只有用户对本次调用输入 `y` 或 `yes` 后才会执行；直接回车、拒绝、审批回调异常、输入中断或未配置审批器都会拒绝。

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
