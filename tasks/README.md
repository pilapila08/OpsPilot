# Task Index

一个工作单代表一次边界明确、可独立验收的改动。开发会话开始时读取 `AGENTS.md`、`docs/STATUS.md`、当前工作单和工作单引用的架构文档。

| ID | 任务 | 状态 | 依赖 |
|---|---|---|---|
| V0-001 | [核心状态与 Schema](V0-001-core-state-and-schemas.md) | Done | 无 |
| V0-002 | [Tool Protocol 与 Registry](V0-002-tool-protocol-and-registry.md) | Done | V0-001 |
| V0-003 | [Evidence 与 Error Taxonomy](V0-003-evidence-and-errors.md) | Done | V0-001 |
| V0-004 | [存储模型](V0-004-storage-model.md) | Done | V0-001、V0-003 |
| V0-005 | [首个可复现故障 Case](V0-005-crashloop-case.md) | Done | V0-002、V0-003 |
| V1-001 | [Kubernetes Client Boundary 与目标解析](V1-001-kubernetes-client-boundary.md) | Done | V0-001、V0-002、V0-003 |
| V1-002 | [五个只读 Kubernetes Tool](V1-002-five-readonly-kubernetes-tools.md) | Done | V1-001 |
| V1-003 | [Structured Model Gateway 与 Intent Router](V1-003-model-gateway-and-router.md) | Done | V0-001、V0-003、V0-004 |
| V1-004 | [Planner 与 Plan Validator](V1-004-planner-and-plan-validator.md) | Done | V1-002、V1-003 |
| V1-005 | [Bounded Executor、Evidence Extraction 与持久化](V1-005-executor-evidence-persistence.md) | Done | V0-004、V1-002、V1-004 |
| V1-006 | [CrashLoopBackOff Diagnosis 与基础 Verifier](V1-006-crashloop-diagnosis-verifier.md) | Ready | V1-003、V1-005 |
| V1-007 | [Runtime Orchestration 与 Offline E2E](V1-007-runtime-orchestration-offline-e2e.md) | Planned | V1-004、V1-005、V1-006 |
| V1-008 | [FastAPI、Live Smoke Test 与 V1 验收](V1-008-fastapi-live-acceptance.md) | Planned | V1-007 |

任务状态使用：`Planned`、`Ready`、`In Progress`、`Blocked`、`Done`。
