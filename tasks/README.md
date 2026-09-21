# Task Index

一个工作单代表一次边界明确、可独立验收的改动。开发会话开始时读取 `AGENTS.md`、`docs/STATUS.md`、当前工作单和工作单引用的架构文档。

| ID | 任务 | 状态 | 依赖 |
|---|---|---|---|
| V0-001 | [核心状态与 Schema](V0-001-core-state-and-schemas.md) | Done | 无 |
| V0-002 | [Tool Protocol 与 Registry](V0-002-tool-protocol-and-registry.md) | Ready | V0-001 |
| V0-003 | [Evidence 与 Error Taxonomy](V0-003-evidence-and-errors.md) | Planned | V0-001 |
| V0-004 | [存储模型](V0-004-storage-model.md) | Planned | V0-001、V0-003 |
| V0-005 | [首个可复现故障 Case](V0-005-crashloop-case.md) | Planned | V0-002、V0-003 |

任务状态使用：`Planned`、`Ready`、`In Progress`、`Blocked`、`Done`。
