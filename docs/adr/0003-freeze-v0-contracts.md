# ADR 0003: 冻结 V0.1 核心契约基线

- Status: Accepted
- Date: 2026-09-22

## Context

V0 已完成 Runtime、Tool、Evidence、Error、Storage 和 Case 的首版实现，并通过 114 项测试与 strict mypy。V1 将同时引入 Kubernetes SDK、LLM 结构化调用、Executor、Verifier 和 API；如果各工作单直接修改 V0 Schema，跨模块契约会快速漂移，Offline Replay 和数据库历史也会失去兼容性。

## Decision

将提交 `252bfe50078b7fa63e1d5468ab8f70a53bae9503` 对应的公共契约定义为 `v0.1` 基线，详细清单记录在 `docs/architecture/v0-contract-baseline.md`。

V1 可以增加新模块和向后兼容字段，但破坏性变更必须同时具备：

1. 新的 Schema、Tool 或 Case 版本。
2. 数据库变化对应的新 Alembic revision。
3. 兼容、迁移或明确拒绝旧数据的测试。
4. 说明动机和影响的新 ADR。
5. 对架构文档、工作单和状态文档的同步更新。

已接受的 V0 迁移、Case v1 数据和 ADR 不得原地改写。Risk、预算、Evidence 绑定和只读基础设施边界不得以兼容性扩展为名放宽。

## Consequences

- V1 工作可以并行围绕稳定接口拆分，减少上下文和集成冲突。
- 一些便利性改动需要新增 V1 Schema，而不能直接重塑 V0 类型。
- 兼容测试和迁移会增加工作量，但 Replay、Trace 和历史数据保持可解释。
- 基线不是永久 API 稳定承诺；必要的破坏性变化仍可通过版本化 ADR 完成。
