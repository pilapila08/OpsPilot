# ADR 0002: 采用 SQLAlchemy 与 Alembic 管理核心存储

- Status: Accepted
- Date: 2026-09-22

## Context

OpsPilot 需要把任务、运行、LLM 调用、Tool 调用、Evidence、诊断结果和 Prompt 版本持久化为可查询、可迁移的 PostgreSQL 模型。存储层必须保留完整 Trace，支持迁移回滚，并在数据库边界保护 Evidence 的追加写语义。

## Decision

采用 SQLAlchemy 2.0 Declarative 定义关系模型，使用 Alembic 管理 Schema 迁移，PostgreSQL 驱动采用 psycopg 3。ORM 模型保持同步 API 中立，可由后续同步或异步 Session 使用；V0 不在存储模型中绑定具体 Web 框架或任务执行器。

生产数据库以 PostgreSQL 为准。集成测试使用临时 SQLite 数据库验证迁移的升级、回滚和 ORM 元数据漂移，并离线编译 PostgreSQL DDL。SQLite 仅是快速测试替身，不代表生产兼容性承诺。

Evidence 在 ORM 层拒绝更新和删除，首个 PostgreSQL 迁移同时安装 `BEFORE UPDATE OR DELETE` 触发器。纠正 Evidence 时必须追加新记录，不得原地改写历史事实。

## Consequences

- ORM、迁移和 PostgreSQL 方言形成单一、可审查的 Schema 演进路径。
- Trace 可通过 `agent_runs.trace_id` 关联调用、证据和最终结果。
- 每次修改模型都必须同步 Alembic 迁移，并通过元数据漂移检测。
- Evidence 的数据库触发器是 PostgreSQL 专属能力；SQLite 测试依赖 ORM 事件验证同一语义。
- V0 不引入 pgvector、分区、归档或 RAG 表，后续需要时另行决策。
