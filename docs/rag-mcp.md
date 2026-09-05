# MyAgent 与 Spring RAG 的 HTTP MCP 联动

RAG 项目位于 `D:\CODE_FIELD_D\JavaField\springAIProject\project\ai-interview`。
RAG 作为独立 Spring Web 服务提供 `/mcp`；MyAgent 通过 Streamable HTTP 连接，不启动 Java 子进程。

## 1. 启动 RAG

使用 RAG 原有的数据库、Redis、对象存储和模型配置。当前检索链需要支持 `pg_search` 的 ParadeDB 和 pgvector。
在 RAG 项目目录执行：

```powershell
$env:DEEPSEEK_API_KEY = '<已配置的 DeepSeek key>'
$env:ZHIPU_API_KEY = '<已配置的智谱 key>'
$env:RAG_MCP_ENABLED = 'true'
.\gradlew.bat :app:bootRun
```

也可将 `mcp` 追加到已有 Spring profile：`--spring.profiles.active=dev,mcp`（仅在已有 dev 配置时使用）。
默认监听路径 `/mcp`、应用端口 `8080`，可以通过 Spring 部署配置改变。服务端等待预算默认 `120s`。

MCP 默认关闭。启用但未设置 `RAG_MCP_API_KEY` 时，仅接受回环地址的客户端；通过另一台机器、容器端口映射或反向代理访问时，设置该变量，并在 MyAgent 配置 `Authorization: Bearer <同一令牌>`。跨机器使用 HTTPS。端点拒绝带 `Origin` 的浏览器请求；普通 MCP 应用客户端无需该头。

## 2. 配置 MyAgent

更新 MyAgent 安装：`python -m pip install -e .`。在启动 MyAgent 的目录中，将
`myagent.rag.config.example.json` 的 `mcp_servers` 合并到现有 `myagent.config.json`；保留原有模型和服务配置。
尚无配置时，可以复制示例后修改模型连接参数。

```json
{
  "name": "rag",
  "transport": "streamable_http",
  "url": "http://127.0.0.1:8080/mcp",
  "connect_timeout_seconds": 10,
  "call_timeout_seconds": 120,
  "headers": {}
}
```

`connect_timeout_seconds` 限制连接、协议协商和工具发现；`call_timeout_seconds` 限制一次工具调用，必须是有限正数。
HTTP 使用直连，不继承代理环境变量，也不自动跟随重定向。使用服务的最终 URL。
凭据只放在被 Git 忽略的本机配置中；不要提交带真实令牌的配置。

CLI 与桌面入口共用配置加载逻辑。配置变更后重建 Agent/重新启动会话，工具在连接建立时发现。

## 3. 验证

不调用模型，只发现工具并列出知识库：

```powershell
python scripts/check_rag_mcp.py --url http://127.0.0.1:8080/mcp
```

选取列表中 `COMPLETED` 状态的真实 ID，执行一次问答：

```powershell
python scripts/check_rag_mcp.py --knowledge-base-id 1 --question 'volatile 能保证复合操作的原子性吗？'
```

重复 `--knowledge-base-id` 可以选择多个知识库。诊断脚本从 `RAG_MCP_API_KEY` 环境变量读取 HTTP 令牌，并通过 MyAgent 工具注册表执行指定调用。

正常对话示例：“列出可用知识库，使用 Java 知识库解释 volatile，并保留参考来源。”主 Agent 会使用
`mcp__rag__list_knowledge_bases` 和 `mcp__rag__ask_knowledge_base`；默认每次调用仍由现有权限 Hook 审批。
追问由 MyAgent 补全为独立问题后传给 RAG。

## 4. 结果与错误

问答返回 `status`、`answer`、`knowledge_base_ids`、`sources` 和 `truncated`。
来源包含本次生成实际使用片段的 `chunk_id`、知识库 ID/名称、文件名和片段摘要；当前数据没有页码，来源列表也不表示逐句引用匹配。
`insufficient_evidence` 是正常业务结果。无效 ID、未就绪知识库及服务故障使用 `isError=true`。

每次问答会沿用 RAG 的提问计数和模型调用。超时不自动重放；超时或客户端断开也不保证服务端已经取消正在执行的模型请求。
答案最多保留 6000 个 Unicode 码点，最多 5 条来源，每条摘要 500 个码点；发生截断时 `truncated=true`。
服务端同时限制完整 MCP JSON 的大小，MyAgent 仍保留 64 KiB 的最终结果上限。

## 5. 开发验证

```powershell
python -m unittest tests.test_mcp tests.test_mcp_http tests.test_cli tests.test_composition tests.test_permissions tests.test_gui -q
```

RAG 侧运行：

```powershell
$env:MYAGENT_PYTHON = '<已安装 MyAgent 的 python.exe 绝对路径>'
.\gradlew.bat :app:test --tests '*knowledgebase.mcp.*' --tests '*KnowledgeBaseQueryServiceTest'
```

Java 集成测试启动随机端口的真实 Spring MCP Server；配置 `MYAGENT_PYTHON` 后额外使用 MyAgent 发起 HTTP 调用。
该测试使用固定问答依赖，不需要生产密钥或数据库；实际模型和实际知识库需要按第 3 步另行联调。
