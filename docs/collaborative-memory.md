# 多 Agent 协作记忆

协作记忆为主 Agent、同步 SubAgent、后台 Fork 和 Team 保存同一任务的可观察交互，再用明确的验证结果提炼经验。它独立于原有长期记忆和上下文压缩，不使用 embedding 或向量服务。默认关闭；离线回放只能验证机制，不能证明真实模型的协作能力提高。

## 启用与任务边界

复制 `myagent.config.example.json` 中的 `collaborative_memory` 配置，或在 CLI 传入模式：

```powershell
python -m myagent --collaborative-memory record --accept "重试测试通过" --accept "验收检查通过" "修复重试逻辑"
python -m myagent --collaborative-memory learn --accept "回归通过" "检查相似重试问题"
```

| 模式 | 行为 |
| --- | --- |
| `off` | 不创建协作记忆目录，不采集、不注入、不增加记忆模型请求 |
| `record` | 持久化任务、协作交互和证据；不自动检索或提炼 |
| `learn` | 采集、受限检索、按角色提供历史材料，并处理有验证依据的学习队列 |

每次提交默认是一个新任务，即使文本相同。CLI 会在标准错误输出显示 `episode=q_…`。要继续同一任务，明确传入 `--continue-episode q_…`；它保留验收条件，旧验证证据转为历史，继续后的版本需要重新验证。会话恢复与任务继续是两件事，`--continue` 不会按文本自动合并任务。

桌面端在“运行设置”选择协作记忆模式。“协作记忆”面板可填写下一次任务的验收条件和继续任务 ID，也可查看历史任务、检索记录、验证依据和经验。条件留空时结果保持未知。

后台委派在提交到线程池之前登记。主 Agent 返回时还有 Fork 或 Team 工作，经历保持 `waiting`；相关工作结束后才能收尾。取消、基础设施异常和正常调用结束都不会自动变成成功或方案失败。迟到消息保留原任务 ID；已经结束的经历收到新的交互后，旧结果等待重新核对。

## 验证、学习与纠错

先查看经历和原始验收条件：

```powershell
python -m myagent --memory-action status
python -m myagent --memory-action sources --memory-episode q_替换为实际ID
python -m myagent --memory-action feedback --memory-episode q_实际ID --criterion "重试测试通过" --result passed --evidence "pytest tests/test_retry.py：6 passed，已检查退避上限"
```

`--criterion` 必须是启动时声明的一个条件。每项条件都有通过依据才是 `success`；任一条件明确失败则是 `failed`；局部通过是 `unknown`。完成相关工作后才能提交当前产物的验证，避免后台还在改文件时提前确认结果。重复导入可通过 Python API 传入稳定 `evidence_id`；同一 ID 的不同内容会被拒绝。

程序化验收使用 `MemoryStore.feedback(..., source="validator", revision=...)`，应在固定验收器检查结束后调用。普通工具成功、模型文字和 Stop 事件没有写入验证结果的权限。模型没有“标记成功”的记忆工具。

获得证据后，持久化队列处理当前经历版本。`learn` 模式使用最多一次受限模型调用提炼一批经验；失败最多重试配置次数，耗尽后保留 `failed` 作业及原因。下一次启动会继续未完成队列；运行中的作业用短租约恢复。CLI 单次任务退出时可能仍有待处理作业，下一次以 `learn` 启动会继续处理。

提炼结果必须包含适用条件、角色和真实来源事件，并引用当前有效的验证事件。成功和失败都有机会产生经验；没有可靠依据时允许不产生经验。相同文本、条件和角色的经验会合并来源，历史版本可追溯。被检索、被提供、明确引用、证据支持和反例分别记录，不会因为“曾经注入”就视为已证实。

```powershell
python -m myagent --memory-action disable --memory-insight i_实际ID --reason "新的边界案例否定了这条建议"
python -m myagent --memory-action revise --memory-insight i_实际ID --reason "收紧适用条件" --replacement "先读取当前部署配置，再确定重试次数，不复用历史常量。"
python -m myagent --memory-action delete --memory-insight i_实际ID --reason "错误经验"
```

Python 管理 API 的 `evidence_id` 可将修订关联到具体反例。模型调用 `change_collaborative_insight` 仍经过 ToolRegistry、PreToolUse 和 PermissionHook，默认需要一次写入批准。自动采集和学习由显式运行时模式授权，不把自动任务用作模型修改权限的替代通道。

自动提炼也可以对明确引用过的经验提出支持、修订或停用。每项更新必须关联本任务的有效验收证据：支持要求通过证据，修订或停用要求具体失败证据及解释。仅被注入、未明确引用的经验不会因此被改动；同一证据重复处理不会重复增加版本。

停用或删除会使投影、检索记录和该来源版本的待处理学习失效；仍在运行的提炼不能通过改写措辞重新引入同一来源版本。当前请求前会重新检查经验版本。记忆包是临时的用户层历史材料，不写入持久协议 history，也不会成为开发者指令。

## 数据和检索

数据位于项目的 `.myagent/collaborative-memory/`：

| 内容 | 责任 |
| --- | --- |
| `memory.sqlite3` | 任务、运行、交互、证据、经验版本、显式关系、使用记录、学习队列的权威来源 |
| `projections/*.txt` | 可删除重建的 UTF-8 搜索投影，含稳定 ID 与版本 |
| `blobs/*.txt` | 较大交互的受控内容引用，读取时检查路径、大小和 SHA-256 |

Git worktree 使用共同仓库的项目记忆，同时记录实际执行目录和 Git revision。非 Git 工作区按目录隔离。不跨项目自动召回。路径拒绝符号链接／目录联接，模型检索工具只接受命中过的记录 ID，不接受文件路径或 Shell 命令。

检索模型可以选择错误信息、符号、文件名和主题别名，查看命中后改写查询，也可读取有限正文。只提供两个只读工具 `collaborative_grep`、`collaborative_read`，执行经过独立的受限 ToolRegistry 和 PermissionHook。首版只支持字面量，不支持正则。

模型选择种子任务后，沿有理由的任务关系最多扩展一跳，向上读取经验，向下提取相关角色的交互和关系。来源图只把同一次运行的事件标为顺序；跨 Agent 的委派、实际消息交付和工具输出引用单独标记。经验间关系保留作为依据的任务 ID。

默认预算见示例配置：3 轮搜索、每轮 8 个命中、合计读取 16000 字符／扫描 2 MB、4 个种子任务、8 个扩展节点、4 次检索模型调用、6000 字符注入、15 秒检索时限、每次 1500 输出 Token。一次交互最多保存 65536 字符，超出会明确标注截断。预算允许调小或在明确硬上限内调大。默认值经过定向边界测试；尚未根据真实收益调优。

空库不调用检索模型；无关任务、无可靠命中、超时、损坏投影可以返回空包并留下诊断。每次检索保存词、轮次、ID、筛选理由、关系、注入内容、读取量、调用次数、Token 用量及耗时。提供方没有返回 Token 用量时，评测报告保持未知，不记为零成本。

## 恢复与删除

```powershell
python -m myagent --memory-action rebuild
python -m myagent --memory-action retry
python -m myagent --memory-action sources --memory-episode q_实际ID
python -m myagent --memory-action delete_episode --memory-episode q_实际ID
```

`rebuild` 从 SQLite 重建投影；`retry` 重新允许失败的提炼作业尝试。删除任务会清理它的关系和作业，仅删除已经没有来源的经验及没有引用的内容文件。共享来源不会随另一个任务一起被删除。

若进程意外退出，运行轨迹仍在数据库中。确认没有同项目的活动任务后，可执行 `--memory-action recover`，将残留运行标为中断并释放学习作业。它不会据此判定方案失败。自动恢复提炼租约与人工恢复任务运行分开，避免一个桌面会话启动时误结束另一个会话的任务。

## 定向测试与效果评测

```powershell
python -m unittest tests.test_collaborative_memory -v
python -m myagent.evaluation.collaborative --provider replay --allow-code-execution
python -m myagent.evaluation.collaborative --provider openai --model 你的固定模型ID --allow-code-execution --output eval-results/memory-live.json
```

评测按 `evaluations/collaborative-memory/plan.json` 固定顺序运行：先积累同家族任务，再插入无关任务，最后检查不同策略的保留任务和修订场景。保留任务的部署参数与训练题不同，验收文件在工作区外；每组从空快照独立开始，每题结束才把已观察轨迹复制给下一题。代码固定实施／验收两个角色、模型与任务工具轮次。

对照组为关闭、现有长期记忆、完整协作记忆；另有去掉角色筛选、经验层、交互层的三组消融。报告逐题通过结果、同家族重复失败、无关来源、检索轮数、前台时间、后台提炼时间和总 Token。总 Token 包含记忆请求。

离线回放报告标为 `mechanics_only`，只检查流程与隔离，不生成“已经学得更好”的结论。真实收益门槛在代码中预先固定：至少 20 个保留测试，完整方案比关闭组通过率提高至少 5 个百分点，无关注入不超过 5%，总 Token 不超过关闭组 2 倍。随附小计划用于冒烟检查，不足以通过收益门槛。没有可复核的真实收益时默认关闭。

目前测试覆盖：主／子／Fork／Team 归属、成员复用、消息消费、证据覆盖、取消与迟到、并发与重放、投影重建、查询改写与预算、角色包、重启学习、停用删除、worktree 归属和关闭兼容。完整仓库检查入口仍是 `scripts/quality-gate.ps1`。
