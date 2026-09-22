# V0 Contract Baseline

## 状态

- Baseline: `v0.1`
- Accepted: 2026-09-22
- Source commit: `252bfe50078b7fa63e1d5468ab8f70a53bae9503`
- Change policy: [ADR 0003](../adr/0003-freeze-v0-contracts.md)

本文件冻结 V0 已验证的公共契约。冻结表示后续实现必须保持兼容或显式升级版本，不表示代码永远不能演进。

## Runtime Contract

公共入口位于 `opspilot.agent`：

- `StrictSchema`：`extra=forbid`、`frozen=true`、`strict=true`、字符串去空白并校验默认值。
- `Target`：Kubernetes namespace 与 resource。
- `IntentOutput`：`diagnose` 意图、受限 domain、problem type 和 Target。
- `PlanStep` / `Plan`：有序步骤、注册 Tool 名称和原因，最多 8 步。
- `BudgetLimits` / `BudgetState`：步骤、Tool、重试、Token、成本和总耗时上限。
- `AgentState`：任务、Trace、Intent、Plan、调用、Evidence、诊断、验证、预算、状态和时间线。
- `AgentStatus`：CREATED、ROUTING、PLANNING、EXECUTING、VERIFYING、WAITING_APPROVAL、COMPLETED、PARTIAL、FAILED、BUDGET_EXCEEDED、POLICY_REJECTED。

V1 的可执行 Tool 参数不得破坏 `PlanStep`。需要参数化计划时，应新增 V1 Schema 或使用带默认值的兼容扩展，并保留 V0 反序列化测试。

## Tool Contract

公共入口位于 `opspilot.tools`：

- `ToolDefinition`：名称、描述、Risk、输入/输出 Pydantic 模型、异步 Handler、来源、超时、重试和版本。
- `ToolDescriptor`：不暴露 Handler 的可序列化规划视图。
- `ToolInvocation`：`call_id`、Tool 名称和 JSON-only arguments。
- `ToolResponse`：`success`、`data`、`metadata`、`error` 的互斥成功/失败信封。
- `ToolMetadata`：call ID、Tool 名称、来源、耗时和 Tool 版本。
- `ToolRegistry`：白名单查找、Risk 0 限制、输入/输出校验、超时和异常归一化。

V0 Registry 每次只尝试一次。分类重试、预算扣减、Trace 持久化和 Evidence 生成属于 V1 Executor，不得塞入具体 Kubernetes Handler。

## Evidence Contract

公共入口位于 `opspilot.evidence`：

- `Evidence`：Trace、Tool Call、来源、资源、观察/采集时间、内容、来源置信度、原始结果引用和不可变属性。
- `Claim`：文本、至少一个唯一 Evidence ID 和推断置信度。
- `Verification`：支持状态、验证置信度、已检查 Evidence、缺失项、冲突和理由。
- `MissingEvidence` / `Contradiction`：不足和冲突的结构化表达。

Evidence 创建后不可原地修改。事实修正必须追加新 Evidence；最终事实性结论必须引用 Evidence ID。

## Error Contract

`ErrorCode`、`ErrorCategory`、`ErrorAction`、`ErrorPolicy` 和 `ErrorInfo` 是跨 Router、Planner、Executor、Tool 与 API 的统一错误语言。V1 只能映射到现有错误码或通过兼容扩展新增错误码，不得在子模块维护另一套错误字符串。

## Storage Contract

首个数据库版本为 Alembic revision `20260922_0001`，包含：

```text
diagnosis_tasks
agent_runs
prompt_versions
llm_calls
tool_calls
evidence
diagnosis_results
```

`agent_runs.trace_id` 是运行级关联键。调用、Evidence 和 Result 必须归属同一 Run；Evidence 在 ORM 与 PostgreSQL 层均为追加写。Schema 变化只能通过新迁移完成，禁止改写已接受迁移。

## Case Contract

`CaseDefinition.schema_version=1` 是首个 Replay 格式。一个 Case 必须包含：

- 版本、Case ID、Trace、Target 和故障参数。
- 故障/修复清单引用。
- 有序 Tool Invocation 与固定 ToolResponse。
- 绑定同一 Trace 和 fault-phase Tool Call 的 Evidence。
- required Evidence 到预期 Claim、Verification 的完整引用。
- 可机器验证的恢复条件。

Fixture 引用必须留在 Case 目录内。外部日志和清单一律作为不可信数据处理。

## 兼容性规则

| 变更 | 要求 |
|---|---|
| 新增可选字段 | 必须有默认值并保留旧数据反序列化测试 |
| 新增枚举值或错误码 | 必须定义处理策略并验证未知旧消费者的安全行为 |
| 删除、重命名、改变字段语义 | 新 Schema 版本、ADR 和迁移 |
| 数据库字段或约束变化 | 新 Alembic revision，验证 upgrade/downgrade 与漂移 |
| Case 格式变化 | 提升 `schema_version`，旧 Case 仍可加载或提供转换器 |
| Tool 输入输出变化 | 提升 Tool version，保留旧版本或明确迁移所有调用方 |
| Agent 状态转换变化 | 更新状态协议、转换矩阵和回归测试 |

禁止为方便单个 V1 实现而原地放宽 Risk、Evidence 或预算边界。
