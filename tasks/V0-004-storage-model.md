# V0-004: PostgreSQL 核心存储模型

- Status: Planned
- Phase: V0
- Depends on: V0-001, V0-003

## 目标

把任务、运行、LLM 调用、Tool 调用、Evidence 和诊断结果映射为可迁移、可查询的 PostgreSQL 模型。

## 范围

- 选择 ORM 与迁移工具并记录 ADR。
- 定义 `diagnosis_tasks`、`agent_runs`、`llm_calls`、`tool_calls`、`evidence`、`diagnosis_results` 和 `prompt_versions`。
- 定义主键、外键、时间字段、版本字段和必要索引。
- 提供可在测试数据库执行的首个迁移。

## 不做

- 不实现 RAG 的 documents/chunks。
- 不引入 pgvector。
- 不实现长期归档和分区策略。

## 验收条件

- [ ] 空数据库可升级到最新版本并可回滚首个迁移。
- [ ] Trace 可以关联所有调用、证据和结果。
- [ ] Evidence 记录采用追加式写入语义。
- [ ] 数据库集成测试通过并更新项目状态。

