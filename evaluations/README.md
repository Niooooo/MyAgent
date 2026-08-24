# MyAgent 评测

首版采用“双车道”：同一组自建小仓库任务既能用固定 Responses 脚本离线回放，
也能手动交给真实模型执行。普通单元测试和 PR 门禁只跑离线车道，不读取密钥、
不请求网络，也不把离线结果冒充为模型能力分数。

## 为什么先用自建集

MyAgent 当前最需要验证的是单主 Agent 能否完成“读小仓库、选择工具、修改文件、
运行验收、正确结束”这条真实链路。公开编码榜单不能直接覆盖 MyAgent 的权限 Hook、
工具协议和 Windows 运行边界，完整公开基准还会引入镜像、依赖、模型费用和波动。
因此，v1 用项目内可审查的确定性任务做持续回归；公开基准留作后续外部校准，
不得与本套分数混写。

## v1 数据集

`live/cases.jsonl` 目前包含四条任务：

| 用例 | 类型 | 目标 |
| --- | --- | --- |
| `fix_order_total` | 单文件 bugfix | 修复漏算和输入集合被修改的问题 |
| `normalize_slug` | 单文件 feature | 补齐标点、重复分隔符和空输入边界 |
| `repair_retry_config` | configuration | 修改 JSON 类型和值，同时保留无关字段 |
| `cross_file_greeting` | 跨文件 feature | 实现共享名称规范化并由调用方复用 |

每条用例都有：

- `fixtures/<id>/`：复制给 Agent 的初始小仓库；
- `graders/<id>/`：不复制进 Agent 工作区的隐藏 `unittest`；
- `replays/<id>.json`：CI 使用的固定工具调用脚本；
- 明确的允许写审批、最大工具轮数、验收超时和预期变更文件。

## 使用方式

只校验数据和资源，不执行样例代码：

```powershell
$env:PYTHONPATH = "src"
python -m myagent.evaluation validate
```

运行离线回放。参数是显式的，因为 grader 会执行临时工作区中的 Python：

```powershell
python -m myagent.evaluation run `
  --provider replay `
  --allow-code-execution `
  --output eval-results/replay
```

真实模型评测只手动运行，不在普通 PR 中启用：

```powershell
$env:OPENAI_API_KEY = "..."
$env:OPENAI_MODEL = "..."
python -m myagent.evaluation run `
  --provider openai `
  --allow-code-execution `
  --output eval-results/openai
```

可以重复传入 `--case <id>` 只跑指定任务。开发门禁入口为：

```powershell
./scripts/quality-gate.ps1
```

## 判分与报告

每条任务必须同时满足：基线处于预期失败状态、Agent 正常结束、隐藏验收通过、
最后一次文件修改后再次调用固定验收工具且结果成功、变更文件集合精确匹配、每个 `call_id` 恰好对应一个
`function_call_output`、审批没有越权、`Stop` 事件恰好一次。确定性回放还必须完整
消费脚本。v1 门禁要求全部用例通过，不给确定性契约设置宽松阈值。

输出目录包含：

- `runs.jsonl`：逐用例终止状态、断言、工具轨迹、验收结果和工作区差异；
- `summary.json`：CI 使用的聚合结果；
- `summary.md`：人工阅读和 GitHub Actions 任务摘要。

usage 未由 provider 提供时记为 `null`，不能写成 0；评测器不内置价格表，成本始终
记为 `null`。退出码为 `0`（门禁通过）、`1`（评测完成但有用例失败）、`2`（数据、
配置或运行前提错误）。

## 安全边界

每次运行都会把 fixture 复制到新的临时目录，文件工具不能越出该工作区，并且不向
Agent 暴露通用 Bash，只提供固定的 `run_acceptance_tests`。不过，隐藏 grader 仍会
执行模型修改后的 Python；临时目录和子进程都不是操作系统安全沙箱。只应在可信
代码和受控机器上使用 `--allow-code-execution`。若要发布公开成绩，应先把每条 live
case 放进真正的容器或受限执行环境，并增加父进程级总超时。

## resume-eval-v2

简历展示集位于 live/resume-cases.jsonl，共 50 条：25 条组件题测必要工具、
调用顺序和工具克制，25 条集成题测目标达成、约束遵守和完成声明。评分仍然只有
原来的两条 track 和六个维度，没有为了增加题量扩张简历表述。默认建议每题运行
3 次，因此完整真实评测包含 150 条 run。

2026-08-17 的 16 题、48 run 报告仍是 `resume-eval-v1` 的冻结历史结果；新增题目后
必须重新运行并重新 Judge，不能把旧成绩写成 50 题成绩。

2026-08-24 的 50 题、150 run 脱敏报告位于
[`reports/resume-eval-v2/deepseek-v4-flash-0731-20260824/`](reports/resume-eval-v2/deepseek-v4-flash-0731-20260824/)。
硬验证为 150/150，严格 Codex 离线 Judge 为 146/150（97.33%），Judge Pass@3
为 100%。这是自建私有评测，不是公开 benchmark 成绩。

DeepSeek 官方接口使用 Chat Completions；评测专用适配器把消息、tool_calls、
reasoning_content 和 usage 转成 AgentLoop 已有的 Responses 子集。适配器不替换
AgentLoop、ToolRegistry、PermissionHook 或 Hook 链。

验证数据集：

    python -m myagent.evaluation validate --dataset evaluations/live/resume-cases.jsonl

运行一次协议 smoke：

    python -m myagent.evaluation run --provider deepseek --case component_no_tool_answer --dataset evaluations/live/resume-cases.jsonl --allow-code-execution --output eval-results/deepseek-smoke

使用可重复运行脚本执行完整 150 次（默认 50 题 x 3 次）：

    ./scripts/run-resume-eval.ps1

也可以手动运行：

    python -m myagent.evaluation run --provider deepseek --model deepseek-v4-flash --repeat 3 --thinking enabled --dataset evaluations/live/resume-cases.jsonl --allow-code-execution --output eval-results/deepseek-v4-flash-0731

DeepSeek 密钥只从 DEEPSEEK_API_KEY 读取；DEEPSEEK_BASE_URL 默认使用官方地址。
输出中的 judge-packets.jsonl 不包含 Agent 模型名。当前 Codex 按
judge/judge_prompt.md 和 judge/rubric.json 生成 judge-results.jsonl 后，运行：

    python -m myagent.evaluation judge --packets eval-results/deepseek-v4-flash-0731/judge-packets.jsonl --verdicts eval-results/deepseek-v4-flash-0731/judge-results.jsonl --run-summary eval-results/deepseek-v4-flash-0731/summary.json --output eval-results/deepseek-v4-flash-0731/final

最终判分不使用平均分掩盖硬失败。隐藏测试、权限、call_id 闭合和修改后复验仍是
Verifier 硬门槛；Codex Judge 负责开放式的轨迹与结果 rubric。该成绩属于 MyAgent
私有评测集，不是公开 benchmark。
