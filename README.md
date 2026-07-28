# MyAgent

一个结构清晰、可扩展的 Python Agent Loop，使用 OpenAI Responses API，并提供本地命令与工作区文件工具。

## 工作流程

1. 将用户输入和工具定义发送给 Responses API。
2. 如果模型返回 `function_call`，在本地执行对应工具。
3. 将 `function_call_output` 及其 `call_id` 发送回模型。
4. 重复以上过程，直到模型返回最终文本。

每轮都会保留完整的 `response.output`，因此 reasoning item 也会正确传回后续请求。

## 内置工具

| 工具 | 用途 |
| --- | --- |
| `bash` | 在工作目录中运行 Bash 命令 |
| `read_file` | 按行读取 UTF-8 文本文件，可分块读取大文件 |
| `write_file` | 新建或完整覆盖文件，并自动创建父目录 |
| `edit_file` | 精确替换文本，默认拒绝有歧义的多处替换 |
| `glob` | 使用相对 Glob 模式查找文件和目录 |
| `grep` | 使用正则表达式搜索文本，返回文件、行号、列号和片段 |

文件类工具统一限制在启动 `myagent` 时的工作目录内。工具定义、权限检查和执行都经过 `ToolRegistry` 的唯一入口；新增工具时只需提供 schema 与 handler，不需要继续扩展 Agent Loop 的条件分支。

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

交互模式中的 `exit`、`quit` 或 `Ctrl-D` 会退出程序。

## 安全边界

文件工具会拒绝工作区外路径、绝对或包含 `..` 的越界 Glob，并对读取大小和搜索结果数量设限。写入使用同目录临时文件替换，避免写入中断留下半个文件。

工具执行前会按以下顺序经过独立的权限层：

1. **工具白名单**：默认只暴露并允许 `bash`、`read_file`、`write_file`、`edit_file`、`glob` 和 `grep`。不在白名单中的工具即使已经注册，也不会发送给模型，并且直接调用同样会被拒绝。
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
