# BFCL-inspired single 评测

这套评测借鉴 BFCL 的工具调用分类和执行结果验证，使用 MyAgent 的本地工具构造
330 道任务，分为 11 个维度，每个维度各 30 题。每题由一个 Agent 在独立临时工作区
完成，通过生产 `AgentLoop → ToolRegistry → Hook` 链路记录工具调用、文件终态与最终回答。

## 题库

| 维度 | 题数 | 检查内容 |
|---|---:|---|
| tool_selection | 30 | 工具选择与必要调用 |
| argument_accuracy | 30 | 路径、参数类型、范围与替换内容 |
| call_order | 30 | 发现、读取、修改、验收之间的依赖顺序 |
| parallel_calls | 30 | 独立调用是否出现在同一模型响应中 |
| tool_restraint | 30 | 额外搜索、重复读取与多余验收 |
| irrelevance | 30 | 无需工具时直接作答 |
| missing_parameters | 30 | 缺参时提出澄清 |
| error_recovery | 30 | 根据受控错误反馈修正调用 |
| goal_achievement | 30 | 跨文件任务的最终状态 |
| constraint_compliance | 30 | 修改范围、权限与保留字段 |
| completion_honesty | 30 | 完成、受阻和待补充信息的状态与证据 |

前 8 个维度共 240 道工具题，后 3 个维度共 90 道集成题。每维包含 6 个任务家族，
每个家族有 5 个输入实例。默认每题重复 3 次，总计 990 次运行。

工具包括 `read_file`、`write_file`、`edit_file`、`glob`、`grep` 和固定文件状态验收
`run_acceptance_tests`。题目输入写入临时工作区，评分所用的预期调用与文件留在评测进程。
最终回答约定为 `{"status":"completed|blocked|needs_input","answer":"..."}`。

## 运行

PowerShell 脚本优先使用项目虚拟环境，启动前检查 Python 和依赖。缺少密钥时提示隐藏输入；
`-PromptApiKey` 强制重新输入。密钥通过进程环境传给模型客户端，脚本退出后恢复原环境。

```powershell
./scripts/run-bfcl-eval.ps1 -CheckOnly
./scripts/run-bfcl-eval.ps1 -Provider deepseek -PromptApiKey -Repeat 3 -Output eval-results/bfcl-deepseek
```

结果位于指定目录的 `single/`。再次运行相同参数并加 `-Resume`，会跳过已保存的
`case_id / attempt / variant` 组合，包括已判定失败的记录。

```powershell
./scripts/run-bfcl-eval.ps1 -Provider deepseek -Repeat 3 -Output eval-results/bfcl-deepseek -Resume
```

也可通过 Python 入口选取维度、题目和请求预算：

```powershell
$env:PYTHONPATH = 'src'
python -m myagent.evaluation.bfcl validate
python -m myagent.evaluation.bfcl run --provider replay --repeat 1 --output eval-results/bfcl-replay
python -m myagent.evaluation.bfcl run --provider deepseek --dimension call_order --repeat 3 --output eval-results/bfcl-order
```

`--case` 和 `--dimension` 可以重复传入。`--request-limit`、`--output-tokens`、
`--tool-limit` 分别设置请求数、单次输出 token 和工具调用上限。模型默认读取项目的
provider 配置，可通过 `--model` 指定；PowerShell 对应参数为 `-Model`。
解释器可通过 `-PythonExecutable` 指定。

## 判分与结果

工具题检查调用及参数、依赖顺序、并行批次、额外调用、文件状态、回答和协议闭合。
集成题检查最终文件、用户要求的读取/验收证据、写入范围、权限和最后一次修改后的验收。
严格通过要求全部适用检查成功；报告同时列出各检查的失败次数。

每条完成记录立即追加并刷新 `runs.jsonl`。正常结束或中断时生成汇总。

| 文件 | 内容 |
|---|---|
| config.json | 题库与实现哈希、模型、预算、题目清单和运行次数 |
| runs.jsonl | 调用轨迹、返回值、文件终态、最终回答、各检查结果、用量和耗时 |
| summary.json | 各维度题数、运行数、通过率、失败检查和资源用量 |
| summary.md | 可阅读的维度与失败检查表 |

续跑校验配置、题库与实现哈希。退出码为 0（完整且全部通过）、1（存在失败）、
2（配置或数据错误）。报告中的 `mechanics_only` 表示固定参考轨迹验证，
`model_measurement` 表示模型实际执行。

`python -m myagent.evaluation.bfcl.generate` 生成固定题库。
`scripts/quality-gate.ps1` 执行单元测试、题库校验和 330 道参考轨迹回放。
