# Storage Model

## 目的

核心存储保存一次诊断从任务入口到最终结论的可审计记录。`agent_runs.trace_id` 是运行级追踪标识；调用、证据和结果通过外键归属于同一次运行。

## 核心表

| 表 | 责任 |
|---|---|
| `diagnosis_tasks` | 保存用户请求、目标、状态和幂等键 |
| `agent_runs` | 保存任务的一次有界执行及唯一 Trace |
| `prompt_versions` | 保存 Router、Planner、Verifier 等组件使用的 Prompt 版本 |
| `llm_calls` | 保存 LLM 请求、响应、Token、成本、延迟和错误 |
| `tool_calls` | 保存受控 Tool 调用、风险等级、输入输出和错误 |
| `evidence` | 保存由 Tool Call 产生的不可原地修改的事实证据 |
| `diagnosis_results` | 保存一次运行的最终或部分诊断结果 |

## 追踪关系

```text
diagnosis_tasks
  -> agent_runs (trace_id)
       -> llm_calls -> prompt_versions
       -> tool_calls -> evidence
       -> diagnosis_results
```

- 一个任务可以有多次运行，`task_id + attempt_no` 唯一。
- 一次运行中的 LLM 和 Tool 调用分别按 `sequence_no` 唯一排序。
- Evidence 同时引用 `run_id` 和 `tool_call_id`；组合外键保证 Tool Call 属于同一次运行。
- Diagnosis Result 同时引用 `task_id` 和 `run_id`；组合外键防止结果挂到错误任务。
- 一个运行最多产生一条当前诊断结果；需要保留新的结论时创建新的运行。
- V1 新迁移为 `tool_calls` 增加 `logical_call_id` 和 `attempt_no`；`run_id + logical_call_id + attempt_no` 唯一。每次重试保留同一 logical ID，但拥有独立记录 ID 和全局 `sequence_no`。既有行回填为 `logical_call_id=id`、`attempt_no=1`。
- 成功 Evidence 的 `tool_call_id` 指向具体成功尝试。一次 Tool 尝试与其 Evidence 在同一事务提交，后续失败不回滚已提交历史。

## 一致性与索引

- 主键使用应用生成的字符串 ID，便于在写入前建立跨层引用。
- 所有持久化结构包含显式版本字段，时间使用带时区的 UTC 时间。
- 状态、Trace、任务运行、调用时间、Tool 名称、Evidence 来源和资源均有面向查询路径的索引。
- Token、成本、延迟、重试、风险等级、置信度和 Schema 版本由数据库检查约束保护。
- 删除采用限制型外键；V0 不实现级联清理、归档或分区。

`SQLAlchemyModelAuditRepository` 复用 `prompt_versions` 和 `llm_calls`：Prompt component + version 不可原地更换内容；每次模型尝试独立追加并提交，包括 Schema 失败。审计请求保存 query hash/length 而非 query 原文，不保存凭据或 provider 异常正文。

## Evidence 追加写

Evidence 不提供更新时间字段。SQLAlchemy 在 flush 前拒绝更新或删除 `EvidenceRecord`，PostgreSQL 使用触发器拒绝 `evidence` 表的 `UPDATE` 和 `DELETE`。修正或补充事实必须创建新 Evidence，并由后续结果引用新的 Evidence ID。

## 迁移验证

- 临时 SQLite：验证从空库升级、回滚到 base、再次升级，以及迁移与 ORM 元数据无漂移。
- PostgreSQL 方言：验证所有模型可编译为 DDL。
- Alembic 离线 SQL：验证 PostgreSQL Evidence 追加写触发器包含在首个迁移中。
- V1 增量迁移：验证既有 Tool Call 的回填、唯一约束、升级/回滚以及 ORM 元数据一致性；不改写首个迁移。
