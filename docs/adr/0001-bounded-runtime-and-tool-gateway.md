# ADR 0001: 采用受限状态机与 Tool Gateway

- Status: Accepted
- Date: 2026-09-21

## Context

OpsPilot 需要让 LLM 参与故障诊断，同时保证执行可控、可回放、可评测和可审计。直接允许模型执行命令会扩大权限边界，也无法稳定验证参数、预算和结果来源。

## Decision

使用显式 `AgentState` 驱动 Router、Planner、Executor 和 Verifier。所有外部访问统一通过预注册 Tool Gateway，调用前执行 Schema、Policy、权限与预算校验。V1 Tool 全部只读，LLM 不持有基础设施凭据。

## Consequences

- 每一步都有稳定状态和 Trace，便于恢复与 Offline Replay。
- Tool 数量和 Schema 维护成本增加，但权限和错误边界更加清晰。
- 无法使用任意 Shell 快速扩展能力；新增能力必须实现并测试专用 Tool。
- 后续写操作需要独立权限、风险分级和人工审批，不得复用只读执行路径绕过策略。

